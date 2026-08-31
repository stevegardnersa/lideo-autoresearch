from __future__ import annotations

import json
import traceback
from typing import Any, Dict, Optional

import functions_framework
from cloud_function.handler import run_summarize


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

    required = ["source_md", "model", "system_prompt", "user_prompt"]
    missing = [f for f in required if not body.get(f)]
    if missing:
        return _json_error(400, f"Missing required fields: {', '.join(missing)}")

    try:
        result = run_summarize(body)
        payload = json.dumps(result, ensure_ascii=False, default=str)
        return (payload, 200, {"Content-Type": "application/json; charset=utf-8"})
    except Exception as exc:
        tb = traceback.format_exc()
        print(f"Unhandled error: {tb}", flush=True)
        return _json_error(500, f"Internal error: {exc}")