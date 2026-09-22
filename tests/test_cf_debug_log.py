"""Regression guard for the `summarize` Cloud Function debug logging.

Run with: python3 tests/test_cf_debug_log.py

Contract
--------
`CF_DEBUG_LOG_BODIES` is the master gate; the request-level `debug_log` flag can
only opt in *within* an open gate and can never enable logging on its own. When
logging runs it must emit one-line JSON with `event=cf_request`/`cf_response`,
redact `api_key`, and clip long strings so a request cannot blow the Cloud
Logging entry limit.
"""

import contextlib
import io
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cloud_function import cf_log  # noqa: E402

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "OK" if condition else "FAIL"
    suffix = f"  ({detail})" if detail and not condition else ""
    print(f"  {status}  {name}{suffix}")
    if not condition:
        FAILURES.append(name)


def _capture(fn) -> str:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn()
    return buf.getvalue()


def _with_gate(value, fn) -> str:
    previous = os.environ.get(cf_log.ENV_FLAG)
    if value is None:
        os.environ.pop(cf_log.ENV_FLAG, None)
    else:
        os.environ[cf_log.ENV_FLAG] = value
    try:
        return _capture(fn)
    finally:
        if previous is None:
            os.environ.pop(cf_log.ENV_FLAG, None)
        else:
            os.environ[cf_log.ENV_FLAG] = previous


BODY = {"source_md": "x" * 1000, "api_key": "sk-secret", "model": "m"}
RESULT = (json.dumps({"success": True, "summary_md": "y" * 900}), 200, {})


def _emit_pair(flag):
    cf_log.log_request(BODY, flag)
    cf_log.log_response(RESULT, flag)


def test_gate_off_stays_silent() -> None:
    print("\ntest_gate_off_stays_silent")
    check("gate off + flag True -> silent", _with_gate(None, lambda: _emit_pair(True)) == "")
    check("gate off + flag None -> silent", _with_gate(None, lambda: _emit_pair(None)) == "")
    check("gate '0' + flag True -> silent", _with_gate("0", lambda: _emit_pair(True)) == "")


def test_gate_on_requires_request_flag() -> None:
    print("\ntest_gate_on_requires_request_flag")
    check("gate on + flag False -> silent", _with_gate("1", lambda: _emit_pair(False)) == "")
    check("gate on + flag 'off' -> silent", _with_gate("1", lambda: _emit_pair("off")) == "")

    out = _with_gate("1", lambda: _emit_pair(True))
    events = [json.loads(ln)["event"] for ln in out.strip().splitlines()]
    check("gate on + flag True -> emits both events", events == ["cf_request", "cf_response"], str(events))

    legacy = _with_gate("true", lambda: _emit_pair(None))
    check("gate on + flag None -> emits (pre-parse fallback)", legacy.strip() != "")


def test_payload_redaction_and_clipping() -> None:
    print("\ntest_payload_redaction_and_clipping")
    out = _with_gate("1", lambda: _emit_pair(True))
    lines = [json.loads(ln) for ln in out.strip().splitlines()]
    request = next(rec["request"] for rec in lines if rec["event"] == "cf_request")
    check("api_key redacted", request["api_key"] == cf_log.REDACTED, str(request.get("api_key")))
    check("secret value never present", "sk-secret" not in out)
    check("long string clipped", len(request["source_md"]) < 1000 and "chars]" in request["source_md"])
    response = next(rec["response"] for rec in lines if rec["event"] == "cf_response")
    check("response status captured", next(r["status"] for r in lines if r["event"] == "cf_response") == 200)
    check("long response clipped", len(response["summary_md"]) < 900)


def test_output_is_bounded_single_line_json() -> None:
    print("\ntest_output_is_bounded_single_line_json")
    out = _with_gate("1", lambda: _emit_pair(True))
    check("every line parses as JSON", all(json.loads(ln) for ln in out.strip().splitlines() if ln))
    check("total output within cap", len(out) <= cf_log.MAX_CHARS + 64, f"len={len(out)}")

    huge = _with_gate("1", lambda: cf_log.log_request({f"k{i}": "z" * 400 for i in range(50)}, True))
    check("oversized request truncated to cap", len(huge.strip()) <= cf_log.MAX_CHARS + 64, f"len={len(huge.strip())}")


def test_main_wires_request_flag() -> None:
    print("\ntest_main_wires_request_flag")
    source = (ROOT / "cloud_function" / "main.py").read_text(encoding="utf-8")
    check("reads body['debug_log']", 'body.get("debug_log")' in source)
    check("passes flag to log_request", "log_request(body, debug_flag)" in source)
    check("passes flag to log_response", "log_response(result, debug_flag)" in source)


def main() -> None:
    print(f"Cloud Function debug-log guard ({ROOT})")
    test_gate_off_stays_silent()
    test_gate_on_requires_request_flag()
    test_payload_redaction_and_clipping()
    test_output_is_bounded_single_line_json()
    test_main_wires_request_flag()

    if FAILURES:
        print(f"\n{len(FAILURES)} GUARD CHECK(S) FAILED: {', '.join(FAILURES)}")
        sys.exit(1)
    print("\nALL DEBUG-LOG GUARD CHECKS PASSED")


if __name__ == "__main__":
    main()
