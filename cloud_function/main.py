from __future__ import annotations

import json
import os
import sys
import traceback
from typing import Any, Dict, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import functions_framework
from cloud_function import cf_log, storage
from cloud_function.handler import PromptVersionError, build_response, run_summarize
from core.openrouter_client import (
    ModelUnavailableError,
    OpenRouterAPIError,
    OpenRouterHTTPError,
    OpenRouterInsufficientCreditsError,
)

# Transient capacity/latency errors (no queue-wide cooldown): the caller should
# retry the same job soon.
DEFAULT_TRANSIENT_RETRY_AFTER_SECONDS = 5
# OpenRouter concurrency / rate-limit back-pressure: the caller should pause or
# slow the queue instead of hammering.
DEFAULT_RATE_LIMIT_RETRY_AFTER_SECONDS = 30

# Limit sources that mean "you are over your concurrency / request budget right
# now", as opposed to genuine exhausted credits.
_TRANSIENT_LIMIT_SOURCES = {
    "in_flight_budget",
    "in_flight_budget_exhausted",
    "concurrency",
    "request_budget",
    "rate_limit",
    "rate_limit_exceeded",
}


def _json_error(
    status: int,
    message: str,
    *,
    error_type: str = "",
    limit_source: str = "",
    retryable: bool = False,
    retry_after_seconds: Optional[float] = None,
) -> tuple:
    """JSON error envelope.

    ``retryable`` and ``error_type`` let the caller classify the failure without
    string matching. A ``Retry-After`` header is emitted (as integer seconds)
    whenever a retry hint is known, so callers can back off precisely.
    """
    payload: Dict[str, Any] = {"success": False, "error": message}
    if error_type:
        payload["error_type"] = error_type
    if limit_source:
        payload["limit_source"] = limit_source
    if retryable:
        payload["retryable"] = True
    if retry_after_seconds is not None:
        retry_after_seconds = max(0.0, float(retry_after_seconds))
        payload["retry_after_seconds"] = retry_after_seconds
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if retryable and retry_after_seconds is not None:
        headers["Retry-After"] = str(int(round(retry_after_seconds)))
    body = json.dumps(payload, ensure_ascii=False)
    return (body, status, headers)


def _openrouter_error_response(exc: Exception) -> tuple:
    """Map an OpenRouter client error to an HTTP status the caller can act on.

    - Genuine exhausted credits -> 402 (terminal, not retryable).
    - Concurrency / rate-limit back-pressure -> 429 + Retry-After (retryable).
    - Upstream 5xx / provider unavailable -> 503 + Retry-After (retryable).
    - Upstream client errors (400/401/403/404) -> non-retryable passthrough.
    """
    message = str(exc)[:1200]

    if isinstance(exc, OpenRouterInsufficientCreditsError):
        error_type = exc.error_type
        limit_source = exc.limit_source
        is_transient = (
            error_type in _TRANSIENT_LIMIT_SOURCES
            or limit_source in _TRANSIENT_LIMIT_SOURCES
        )
        if is_transient:
            retry_after = exc.retry_after_seconds
            if retry_after is None:
                retry_after = DEFAULT_RATE_LIMIT_RETRY_AFTER_SECONDS
            return _json_error(
                429,
                message,
                error_type=error_type or limit_source or "rate_limit_exceeded",
                limit_source=limit_source,
                retryable=True,
                retry_after_seconds=retry_after,
            )
        return _json_error(
            402,
            message,
            error_type=error_type or "insufficient_credits",
            limit_source=limit_source,
            retryable=False,
        )

    if isinstance(exc, ModelUnavailableError):
        retry_after = exc.retry_after_seconds
        if retry_after is None:
            retry_after = DEFAULT_TRANSIENT_RETRY_AFTER_SECONDS
        return _json_error(
            503,
            message,
            error_type=exc.error_type or "model_unavailable",
            retryable=True,
            retry_after_seconds=retry_after,
        )

    if isinstance(exc, OpenRouterHTTPError):
        status = int(exc.status_code)
        retry_after = exc.retry_after_seconds
        if status == 402:
            return _json_error(
                402,
                message,
                error_type=exc.error_type or "insufficient_credits",
                limit_source=exc.limit_source,
                retryable=False,
            )
        if status == 429:
            if retry_after is None:
                retry_after = DEFAULT_RATE_LIMIT_RETRY_AFTER_SECONDS
            return _json_error(
                429,
                message,
                error_type=exc.error_type or "rate_limit_exceeded",
                limit_source=exc.limit_source,
                retryable=True,
                retry_after_seconds=retry_after,
            )
        if status in {408, 409, 500, 502, 503, 504}:
            if retry_after is None:
                retry_after = DEFAULT_TRANSIENT_RETRY_AFTER_SECONDS
            return _json_error(
                503,
                message,
                error_type=exc.error_type or "upstream_unavailable",
                retryable=True,
                retry_after_seconds=retry_after,
            )
        return _json_error(
            status,
            message,
            error_type=exc.error_type,
            retryable=False,
        )

    if isinstance(exc, OpenRouterAPIError):
        return _json_error(
            503,
            message,
            error_type="upstream_error",
            retryable=True,
            retry_after_seconds=DEFAULT_TRANSIENT_RETRY_AFTER_SECONDS,
        )

    return _json_error(500, f"Internal error: {message}", retryable=False)



@functions_framework.http
def summarize(request):
    if request.method != "POST":
        result = _json_error(405, f"Method {request.method} not allowed")
        cf_log.log_response(result)
        return result

    try:
        body: Optional[Dict[str, Any]] = request.get_json(silent=True)
    except Exception:
        result = _json_error(400, "Invalid JSON body")
        cf_log.log_response(result)
        return result

    if not body or not isinstance(body, dict):
        result = _json_error(400, "Request body must be a JSON object")
        cf_log.log_response(result)
        return result

    debug_flag = body.get("debug_log")
    cf_log.log_request(body, debug_flag)
    result = _handle(body)
    cf_log.log_response(result, debug_flag)
    return result


def _handle(body: Dict[str, Any]) -> tuple:
    # model and user_prompt are optional: the CF resolves the production
    # version (chapter-p18), which bakes the model, reasoning default, and a
    # bundled user_template rendered from source_md/chapter_title/target_words.
    # The source is supplied either inline (`source_md`) or as a structured
    # storage pointer (`source_ref`) — exactly one, never both.
    source_md = str(body.get("source_md") or body.get("chapter") or "").strip()
    source_ref = body.get("source_ref")
    has_source_ref = isinstance(source_ref, dict) and len(source_ref) > 0
    summary_md = str(body.get("summary_md") or "").strip()

    if source_md and has_source_ref:
        return _json_error(400, "Provide exactly one of source_md or source_ref")

    missing: list = []
    if not summary_md and not source_md and not has_source_ref:
        missing.append("source_md")
    if missing:
        return _json_error(400, f"Missing required fields: {', '.join(missing)}")

    if summary_md:
        has_judge = bool(body.get("judge", False))
        has_rubric = bool(body.get("rubric"))
        target_words = int(body["target_words"]) if body.get("target_words") else 0
        has_score_input = has_rubric and target_words > 0
        if not has_judge and not has_score_input:
            return _json_error(
                400,
                "judge or rubric+target_words required when summary_md is provided",
            )

    try:
        result = run_summarize(body)
        payload = json.dumps(result, ensure_ascii=False, default=str)
        return (payload, 200, {"Content-Type": "application/json; charset=utf-8"})
    except PromptVersionError as exc:
        body_payload: Dict[str, Any] = {"success": False, "error": exc.error, "message": str(exc)}
        if exc.allowed:
            body_payload["allowed"] = exc.allowed
        return (
            json.dumps(body_payload, ensure_ascii=False),
            exc.status_code,
            {"Content-Type": "application/json; charset=utf-8"},
        )
    except storage.SourceRefError as exc:
        # Bad/missing source object: permanent, do not retry.
        return _json_error(400, str(exc))
    except storage.SourceFetchError as exc:
        # Transient storage failure: 5xx so the caller's retry policy applies.
        return _json_error(503, str(exc), error_type="source_fetch_failed", retryable=True, retry_after_seconds=DEFAULT_TRANSIENT_RETRY_AFTER_SECONDS)
    except (OpenRouterHTTPError, ModelUnavailableError, OpenRouterAPIError) as exc:
        # Classify upstream model failures into retryable (429/503, with a
        # Retry-After hint) vs terminal (402/4xx) so the queue can back off
        # precisely instead of failing every job.
        return _openrouter_error_response(exc)
    except Exception as exc:
        tb = traceback.format_exc()
        print(f"Unhandled error: {tb}", flush=True)
        return _json_error(500, f"Internal error: {exc}")
