"""Debug logging for the `summarize` Cloud Function.

The ``CF_DEBUG_LOG_BODIES`` env var is the master gate. When it is truthy the
caller may opt in per request by sending ``"debug_log": true`` in the body;
that flag can never enable logging on its own. With the gate off, nothing is
logged regardless of what the client sends.

When enabled, emits single-line JSON to stdout so Cloud Run / Cloud Logging
parses it into ``jsonPayload`` with an ``event`` field (``cf_request`` /
``cf_response``). Request and response bodies are clipped per-string and capped
in total bytes so a full request never approaches the Cloud Logging entry limit.
Secrets are never logged: ``api_key`` values are redacted by key.
"""

from __future__ import annotations

import json
import os
from typing import Any

ENV_FLAG = "CF_DEBUG_LOG_BODIES"
TRUTHY = {"1", "true", "yes", "on"}

STR_LIMIT = 400
MAX_CHARS = 8000
REDACT_KEYS = {"api_key"}
REDACTED = "***redacted***"


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in TRUTHY


def gate_open() -> bool:
    """True when the operator has enabled body logging for this deployment."""
    return _truthy(os.environ.get(ENV_FLAG, ""))


def enabled(request_flag: Any = None) -> bool:
    """True when logging should run for a request.

    ``request_flag`` is the client-supplied ``debug_log`` value. It is only
    honoured when the env gate is open, so a caller cannot turn logging on.
    ``None`` means "no per-request signal" and falls back to the gate alone
    (used for pre-parse error responses).
    """
    if not gate_open():
        return False
    if request_flag is None:
        return True
    return _truthy(request_flag)


def _clip(value: str) -> str:
    if len(value) <= STR_LIMIT:
        return value
    return f"{value[:STR_LIMIT]}\u2026[+{len(value) - STR_LIMIT} chars]"


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict = {}
        for key, item in value.items():
            if isinstance(key, str) and key.lower() in REDACT_KEYS:
                out[key] = REDACTED
            else:
                out[key] = _sanitize(item)
        return out
    if isinstance(value, (list, tuple)):
        return [_sanitize(item) for item in value]
    if isinstance(value, str):
        return _clip(value)
    return value


def emit(event: str, payload: dict, request_flag: Any = None) -> None:
    if not enabled(request_flag):
        return
    record = {"severity": "INFO", "event": event}
    record.update(payload)
    try:
        text = json.dumps(record, ensure_ascii=False, default=str)
    except Exception:
        return
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + "\u2026[truncated]"
    print(text, flush=True)


def log_request(body: Any, request_flag: Any = None) -> None:
    emit("cf_request", {"request": _sanitize(body)}, request_flag)


def log_response(result: Any, request_flag: Any = None) -> None:
    status = None
    parsed: Any
    try:
        payload, status, _headers = result
        parsed = json.loads(payload)
    except Exception:
        parsed = {"raw": str(result)[:STR_LIMIT]}
    emit("cf_response", {"status": status, "response": _sanitize(parsed)}, request_flag)
