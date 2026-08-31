#!/usr/bin/env python3
"""Smoke test for the local functions-framework dev server (no LLM cost).

Exercises the Cloud Function contract without calling the provider:
1. scoring-only mode via ``summary_md`` (echoes the summary + deterministic scoring)
2. validator rejects ``summary_md`` with no judge/rubric
3. validator rejects a generation request missing prompts

Usage:
    cloud_function/cf_smoke.py [base_url]
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from typing import Any, Dict, Tuple

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8080").rstrip("/")

SOURCE_MD = "## Intro\n\nSome chapter text with a key concept: feedback.\n"
SUMMARY_MD = "## Summary\n\nFeedback loops matter."


def post(payload: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
    req = urllib.request.Request(
        BASE,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"error": raw}


def main() -> None:
    checks = 0

    status, body = post(
        {
            "source_md": SOURCE_MD,
            "model": "test/trace-label",
            "summary_md": SUMMARY_MD,
            "target_words": 30,
            "score": True,
            "rubric": {"core_concepts": ["feedback loops"], "key_terms": ["feedback"]},
        }
    )
    assert status == 200, f"scoring-only expected 200, got {status}: {body}"
    assert body.get("success") is True, body
    assert (body.get("summary") or {}).get("summary_md") == SUMMARY_MD, body
    assert "scoring" in body, f"scoring block missing: {body}"
    assert body.get("usage", {}).get("generation_cost") == 0.0, body
    assert body.get("usage", {}).get("judge_generation_cost") == 0.0, body
    assert "meta" in body and body["meta"].get("scoring_version"), body
    checks += 1

    status, body = post({"source_md": SOURCE_MD, "model": "test/x", "summary_md": "hi"})
    assert status == 400, f"summary_md w/o judge/rubric expected 400, got {status}: {body}"
    checks += 1

    status, body = post({"source_md": SOURCE_MD, "model": "test/x"})
    assert status == 400, f"missing prompts expected 400, got {status}: {body}"
    checks += 1

    print(f"cf-smoke OK ({checks} checks)")


if __name__ == "__main__":
    main()