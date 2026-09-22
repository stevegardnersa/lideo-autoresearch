"""Offline tests for Cloud Function upstream-error classification.

Run with: python3 tests/test_cf_error_status.py

Contract (spec: nextjs-cloud-function-production-prompt-concurrent-runs.md)
--------------------------------------------------------------------------
The `summarize` Cloud Function must translate OpenRouter upstream failures into
an HTTP status the *caller* can act on, instead of collapsing everything into a
generic 500:

  * genuine exhausted credits        -> 402, ``retryable=False`` (terminal)
  * concurrency / in-flight budget   -> 429 + ``Retry-After``, ``retryable=True``
  * OpenRouter 429 rate limiting     -> 429 + ``Retry-After``, ``retryable=True``
  * upstream 5xx / model unavailable -> 503 + ``Retry-After``, ``retryable=True``

``Retry-After`` is emitted as integer seconds and mirrored in the JSON body as
``retry_after_seconds`` so callers can back off without header parsing.
"""

import json
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# --- stub functions_framework before importing cloud_function.main ----------
if "functions_framework" not in sys.modules:
    ff = types.ModuleType("functions_framework")

    def _http(fn):
        return fn

    ff.http = _http
    sys.modules["functions_framework"] = ff

from cloud_function import main  # noqa: E402
from core.openrouter_client import (  # noqa: E402
    ModelUnavailableError,
    OpenRouterAPIError,
    OpenRouterHTTPError,
    OpenRouterInsufficientCreditsError,
)

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "OK" if condition else "FAIL"
    suffix = f"  ({detail})" if detail and not condition else ""
    print(f"  {status}  {name}{suffix}")
    if not condition:
        FAILURES.append(name)


def _decode(result):
    payload, status, headers = result
    return status, json.loads(payload), headers


class _Req:
    def __init__(self, body, method="POST"):
        self.method = method
        self._body = body

    def get_json(self, silent=False):
        return self._body


def _via_run_summarize(exc: Exception) -> tuple:
    """Drive the full HTTP entry point with run_summarize raising `exc`."""
    original = main.run_summarize
    main.run_summarize = lambda body: (_ for _ in ()).throw(exc)
    try:
        return _decode(main.summarize(_Req({"source_md": "hello world"})))
    finally:
        main.run_summarize = original


def _insufficient_credits(error_type: str = "", limit_source: str = "") -> Exception:
    payload = {"error": {"message": "out of budget", "metadata": {}}}
    metadata = payload["error"]["metadata"]
    if error_type:
        metadata["error_type"] = error_type
    if limit_source:
        metadata["limit_source"] = limit_source
    return OpenRouterInsufficientCreditsError(
        402,
        "/chat/completions",
        "out of budget",
        error_payload=payload,
    )


def test_terminal_insufficient_credits_is_402():
    print("\ntest_terminal_insufficient_credits_is_402")
    status, body, headers = _via_run_summarize(
        _insufficient_credits(error_type="insufficient_credits")
    )
    check("status 402", status == 402, str(status))
    check("retryable not set", body.get("retryable") is not True, json.dumps(body))
    check(
        "error_type surfaced",
        body.get("error_type") == "insufficient_credits",
        json.dumps(body),
    )
    check("no Retry-After header", "Retry-After" not in headers, str(headers))


def test_in_flight_budget_is_429_retryable():
    print("\ntest_in_flight_budget_is_429_retryable")
    status, body, headers = _via_run_summarize(
        _insufficient_credits(error_type="in_flight_budget_exhausted", limit_source="in_flight_budget")
    )
    check("status 429", status == 429, str(status))
    check("retryable true", body.get("retryable") is True, json.dumps(body))
    check(
        "limit_source surfaced",
        body.get("limit_source") == "in_flight_budget",
        json.dumps(body),
    )
    check("retry_after_seconds default", body.get("retry_after_seconds") == 30, json.dumps(body))
    check("Retry-After header emitted", headers.get("Retry-After") == "30", str(headers))


def test_openrouter_429_uses_retry_after_header():
    print("\ntest_openrouter_429_uses_retry_after_header")
    exc = OpenRouterHTTPError(
        429,
        "/chat/completions",
        "rate limited",
        response_headers={"Retry-After": "17"},
    )
    status, body, headers = _via_run_summarize(exc)
    check("status 429", status == 429, str(status))
    check("retryable true", body.get("retryable") is True, json.dumps(body))
    check("honours Retry-After=17", body.get("retry_after_seconds") == 17.0, json.dumps(body))
    check("Retry-After header 17", headers.get("Retry-After") == "17", str(headers))


def test_upstream_5xx_and_model_unavailable_are_503():
    print("\ntest_upstream_5xx_and_model_unavailable_are_503")
    status, body, _ = _via_run_summarize(
        OpenRouterHTTPError(502, "/chat/completions", "bad gateway")
    )
    check("502 -> 503", status == 503, str(status))
    check("retryable true", body.get("retryable") is True, json.dumps(body))

    status, body, _ = _via_run_summarize(
        ModelUnavailableError(503, "/chat/completions", "no healthy deployments", model="m")
    )
    check("model unavailable -> 503", status == 503, str(status))
    check(
        "error_type model_unavailable",
        body.get("error_type") == "model_unavailable",
        json.dumps(body),
    )


def test_upstream_client_error_passthrough_not_retryable():
    print("\ntest_upstream_client_error_passthrough_not_retryable")
    status, body, _ = _via_run_summarize(
        OpenRouterHTTPError(400, "/chat/completions", "bad request")
    )
    check("400 stays 400", status == 400, str(status))
    check("not retryable", body.get("retryable") is not True, json.dumps(body))

    status, body, _ = _via_run_summarize(OpenRouterAPIError("network error"))
    check("transport error -> 503", status == 503, str(status))
    check("transport error retryable", body.get("retryable") is True, json.dumps(body))


def test_retry_after_http_date_is_parsed():
    print("\ntest_retry_after_http_date_is_parsed")
    exc = OpenRouterHTTPError(
        429,
        "/chat/completions",
        "rate limited",
        response_headers={"Retry-After": "Wed, 21 Oct 2099 07:28:00 GMT"},
    )
    status, body, _ = _via_run_summarize(exc)
    check("status 429", status == 429, str(status))
    check(
        "HTTP-date parsed to positive seconds",
        isinstance(body.get("retry_after_seconds"), (int, float))
        and body["retry_after_seconds"] > 0,
        json.dumps(body),
    )


def test_envelope_defaults_hide_optional_fields():
    print("\ntest_envelope_defaults_hide_optional_fields")
    status, _, headers = _decode(main._json_error(400, "bad"))
    check("status 400", status == 400, str(status))
    check("no Retry-After for terminal error", "Retry-After" not in headers, str(headers))

    status, payload, headers = _decode(
        main._json_error(
            429,
            "slow down",
            error_type="rate_limit_exceeded",
            retryable=True,
            retry_after_seconds=12.4,
        )
    )
    check("status 429", status == 429, str(status))
    check("retryable true", payload.get("retryable") is True, json.dumps(payload))
    check("seconds rounded for header", headers.get("Retry-After") == "12", str(headers))


def main_test() -> None:
    test_terminal_insufficient_credits_is_402()
    test_in_flight_budget_is_429_retryable()
    test_openrouter_429_uses_retry_after_header()
    test_upstream_5xx_and_model_unavailable_are_503()
    test_upstream_client_error_passthrough_not_retryable()
    test_retry_after_http_date_is_parsed()
    test_envelope_defaults_hide_optional_fields()

    if FAILURES:
        print(f"\n{len(FAILURES)} CHECK(S) FAILED: {', '.join(FAILURES)}")
        sys.exit(1)
    print("\nALL CF ERROR-STATUS CHECKS PASSED")


if __name__ == "__main__":
    main_test()
