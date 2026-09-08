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
from cloud_function.handler import build_response, run_summarize


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

    required = ["source_md", "model"]
    missing = [f for f in required if not body.get(f)]
    summary_md = str(body.get("summary_md") or "").strip()
    if not summary_md:
        for f in ("system_prompt", "user_prompt"):
            if not body.get(f):
                missing.append(f)
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
    except Exception as exc:
        tb = traceback.format_exc()
        print(f"Unhandled error: {tb}", flush=True)
        return _json_error(500, f"Internal error: {exc}")
