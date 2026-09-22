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
from cloud_function import storage
from cloud_function.handler import PromptVersionError, build_response, run_summarize


def _json_error(status: int, message: str) -> tuple:
    body = json.dumps({"success": False, "error": message}, ensure_ascii=False)
    return (body, status, {"Content-Type": "application/json; charset=utf-8"})


@functions_framework.http
def summarize(request):
    if request.method != "POST":
        return _json_error(405, f"Method {request.method} not allowed")

    try:
        body: Optional[Dict[str, Any]] = request.get_json(silent=True)
    except Exception:
        return _json_error(400, "Invalid JSON body")

    if not body or not isinstance(body, dict):
        return _json_error(400, "Request body must be a JSON object")

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
        return _json_error(503, str(exc))
    except Exception as exc:
        tb = traceback.format_exc()
        print(f"Unhandled error: {tb}", flush=True)
        return _json_error(500, f"Internal error: {exc}")
