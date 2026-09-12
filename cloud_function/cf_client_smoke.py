#!/usr/bin/env python3
"""End-to-end CloudFunctionClient smoke test (no provider cost).

Boots the local ``functions-framework`` dev server plus an in-process
OpenRouter stub, then drives the real :class:`CloudFunctionClient` through:

1. scoring-only round trip (judge_only) — zero generation cost, scoring block,
   ``meta.scoring_version`` surfaced on the client
2. generation round trip — real handler generation path against the stub LLM,
   no ``Authorization`` header over plain http
3. ``reasoning_effort`` passthrough — forwarded to the stub, no ``extra_body``
4. ``reasoning`` dict passthrough — forwarded to the stub, no ``extra_body``
5. ``thinking=False`` (legacy) — maps to ``reasoning.enabled=false``
6. ``reasoning_effort="none"`` — maps to ``reasoning.enabled=false``
7. ``thinking=True`` (legacy) — maps to ``reasoning.enabled=true``
8. ``reasoning={"effort": "none"}`` — collapses to ``reasoning.enabled=false``
9. plain payload (no reasoning signal) — no ``reasoning`` field in the stub body
10. mandatory-reasoning model (stepfun) — disabling reasoning raises client-side

Uses only the standard library plus the already-required project deps and
``functions-framework`` (installed by the ``cf-client-smoke`` Makefile target).

Usage:
    cloud_function/cf_client_smoke.py
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.cf_client import CloudFunctionClient  # noqa: E402
from scoring import Rubric  # noqa: E402

SOURCE_MD = "## Intro\n\nSome chapter text with a key concept: feedback.\n"
CANNED_SUMMARY = "## Summary\n\nFeedback loops matter for concept retention."

CANNED_RESPONSE: Dict[str, Any] = {
    "id": "stub-1",
    "model": "test/echo",
    "choices": [{"message": {"content": CANNED_SUMMARY}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18, "cost": 0.0},
}

_stub: Dict[str, Any] = {"lock": threading.Lock(), "last_body": None, "last_headers": {}}


class StubHandler(BaseHTTPRequestHandler):
    """OpenRouter chat-completions stub; records the last request for assertions."""

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except json.JSONDecodeError:
            body = {"_raw": raw.decode("utf-8", "replace")}
        with _stub["lock"]:
            _stub["last_body"] = body
            _stub["last_headers"] = dict(self.headers)
        payload = json.dumps(CANNED_RESPONSE).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: Any) -> None:  # quiet the stub
        pass


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _probe_payload() -> Dict[str, Any]:
    return {
        "source_md": "probe",
        "model": "test/x",
        "summary_md": "hi",
        "target_words": 1,
        "score": True,
        "rubric": {"core_concepts": ["feedback"]},
    }


def wait_ready(url: str, *, timeout: float = 60.0) -> None:
    deadline = time.time() + timeout
    body = json.dumps(_probe_payload()).encode("utf-8")
    while time.time() < deadline:
        try:
            req = urllib.request.Request(
                url,
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                resp.read()
                return
        except urllib.error.HTTPError:
            # Any HTTP response means the server is up and routing requests.
            return
        except Exception:
            time.sleep(0.5)
    raise RuntimeError(f"Cloud Function dev server not ready at {url}")


def last_stub_body() -> Dict[str, Any]:
    with _stub["lock"]:
        return dict(_stub["last_body"] or {})


def last_stub_headers() -> Dict[str, str]:
    with _stub["lock"]:
        return dict(_stub["last_headers"] or {})


def main() -> None:
    stub = ThreadingHTTPServer(("127.0.0.1", 0), StubHandler)
    stub_port = stub.server_address[1]
    threading.Thread(target=stub.serve_forever, daemon=True).start()

    cf_port = free_port()
    env = dict(os.environ)
    env.update(
        {
            "LLM_API_KEY": "sk-placeholder",
            "LLM_BASE_URL": f"http://127.0.0.1:{stub_port}",
        }
    )
    log_file = tempfile.NamedTemporaryFile(prefix="cf_client_smoke_", suffix=".log", delete=False)
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "functions_framework",
            "--target",
            "summarize",
            "--signature-type",
            "http",
            "--source",
            "cloud_function/main.py",
            "--port",
            str(cf_port),
        ],
        cwd=str(ROOT),
        env=env,
        stdout=log_file,
        stderr=log_file,
    )
    cf_url = f"http://127.0.0.1:{cf_port}"
    checks = 0
    try:
        wait_ready(cf_url)
        client = CloudFunctionClient(function_url=cf_url, auth_mode="none", timeout=60)
        rubric = Rubric(core_concepts=("feedback loops",), key_terms=("feedback",))

        # 1. scoring-only round trip (judge_only path, zero generation cost).
        result = client.chat_completion(
            {"model": "test/trace-label"},
            source_md=SOURCE_MD,
            summary_md=CANNED_SUMMARY,
            score=True,
            rubric=rubric,
            target_words=30,
        )
        assert result.summary_md == CANNED_SUMMARY, result.summary_md
        assert result.usage.generation_cost == 0.0, result.usage
        assert "scoring" in result.raw_response, result.raw_response
        assert client.last_meta.get("scoring_version"), client.last_meta
        assert not client.scoring_version_mismatch
        checks += 1

        # 2. generation round trip through the stub; no auth header over http.
        gen = client.chat_completion(
            {
                "model": "test/echo",
                "messages": [
                    {"role": "system", "content": "Summarize the chapter."},
                    {"role": "user", "content": SOURCE_MD},
                ],
            },
            source_md=SOURCE_MD,
            score=False,
            target_words=0,
        )
        assert gen.summary_md == CANNED_SUMMARY, gen.summary_md
        assert gen.estimated_visible_words > 0, gen.estimated_visible_words
        # The Function's own LLM_API_KEY reaches the provider hop.
        assert last_stub_headers().get("Authorization") == "Bearer sk-placeholder", last_stub_headers()
        checks += 1

        # 3. reasoning_effort -> native reasoning.effort; no litellm extra_body.
        client.chat_completion(
            {
                "model": "test/echo",
                "reasoning_effort": "high",
                "messages": [
                    {"role": "system", "content": "Summarize the chapter."},
                    {"role": "user", "content": SOURCE_MD},
                ],
            },
            source_md=SOURCE_MD,
            score=False,
            target_words=0,
        )
        stub_body = last_stub_body()
        assert stub_body.get("reasoning") == {"effort": "high"}, stub_body
        assert "reasoning_effort" not in stub_body and "extra_body" not in stub_body, stub_body
        checks += 1

        # 4. reasoning dict passthrough.
        reasoning = {"enabled": True, "effort": "high"}
        client.chat_completion(
            {
                "model": "test/echo",
                "reasoning": reasoning,
                "messages": [
                    {"role": "system", "content": "Summarize the chapter."},
                    {"role": "user", "content": SOURCE_MD},
                ],
            },
            source_md=SOURCE_MD,
            score=False,
            target_words=0,
        )
        stub_body = last_stub_body()
        assert stub_body.get("reasoning") == reasoning, stub_body
        assert "extra_body" not in stub_body, stub_body
        checks += 1

        # 5. thinking-only (legacy disabled) maps to native reasoning.enabled=false.
        client.chat_completion(
            {
                "model": "test/echo",
                "messages": [
                    {"role": "system", "content": "Summarize the chapter."},
                    {"role": "user", "content": SOURCE_MD},
                ],
            },
            source_md=SOURCE_MD,
            score=False,
            target_words=0,
            thinking=False,
        )
        stub_body = last_stub_body()
        assert stub_body.get("reasoning") == {"enabled": False}, stub_body
        assert "extra_body" not in stub_body and "reasoning_effort" not in stub_body, stub_body
        checks += 1

        # 6. reasoning_effort "none" maps to native reasoning.enabled=false.
        client.chat_completion(
            {
                "model": "test/echo",
                "reasoning_effort": "none",
                "messages": [
                    {"role": "system", "content": "Summarize the chapter."},
                    {"role": "user", "content": SOURCE_MD},
                ],
            },
            source_md=SOURCE_MD,
            score=False,
            target_words=0,
        )
        stub_body = last_stub_body()
        assert stub_body.get("reasoning") == {"enabled": False}, stub_body
        assert "reasoning_effort" not in stub_body and "extra_body" not in stub_body, stub_body
        checks += 1

        # 7. thinking-only (legacy enabled) maps to native reasoning.enabled=true.
        client.chat_completion(
            {
                "model": "test/echo",
                "messages": [
                    {"role": "system", "content": "Summarize the chapter."},
                    {"role": "user", "content": SOURCE_MD},
                ],
            },
            source_md=SOURCE_MD,
            score=False,
            target_words=0,
            thinking=True,
        )
        stub_body = last_stub_body()
        assert stub_body.get("reasoning") == {"enabled": True}, stub_body
        assert "extra_body" not in stub_body and "reasoning_effort" not in stub_body, stub_body
        checks += 1

        # 8. reasoning={"effort": "none"} collapses to native reasoning.enabled=false.
        client.chat_completion(
            {
                "model": "test/echo",
                "reasoning": {"effort": "none"},
                "messages": [
                    {"role": "system", "content": "Summarize the chapter."},
                    {"role": "user", "content": SOURCE_MD},
                ],
            },
            source_md=SOURCE_MD,
            score=False,
            target_words=0,
        )
        stub_body = last_stub_body()
        assert stub_body.get("reasoning") == {"enabled": False}, stub_body
        assert "reasoning_effort" not in stub_body and "extra_body" not in stub_body, stub_body
        checks += 1

        # 9. plain payload (no reasoning signal) -> no reasoning field in the stub body.
        client.chat_completion(
            {
                "model": "test/echo",
                "messages": [
                    {"role": "system", "content": "Summarize the chapter."},
                    {"role": "user", "content": SOURCE_MD},
                ],
            },
            source_md=SOURCE_MD,
            score=False,
            target_words=0,
        )
        stub_body = last_stub_body()
        assert "reasoning" not in stub_body, stub_body
        assert "reasoning_effort" not in stub_body and "extra_body" not in stub_body, stub_body
        checks += 1

        # 10. mandatory-reasoning model: trying to disable reasoning fails loudly
        #     client-side (registered via candidate_spec.MANDATORY_REASONING_MODELS),
        #     instead of silently sending a plain request that reasons at default effort.
        try:
            client.chat_completion(
                {
                    "model": "stepfun/step-3.7-flash",
                    "reasoning": {"enabled": False},
                    "messages": [
                        {"role": "system", "content": "Summarize the chapter."},
                        {"role": "user", "content": SOURCE_MD},
                    ],
                },
                source_md=SOURCE_MD,
                score=False,
                target_words=0,
            )
        except ValueError as e:
            assert "mandatory reasoning" in str(e), e
            checks += 1
        else:
            raise AssertionError(
                "expected client-side ValueError for disabling reasoning on a "
                "mandatory-reasoning model (stepfun/step-3.7-flash)"
            )

        print(f"cf-client-smoke OK ({checks} checks)")
    except Exception:
        log_file.flush()
        log_path = log_file.name
        print(f"cf-client-smoke FAILED; dev-server log: {log_path}", file=sys.stderr)
        try:
            tail = Path(log_path).read_text(encoding="utf-8", errors="replace").strip().splitlines()[-20:]
            for line in tail:
                print(f"  {line}", file=sys.stderr)
        except OSError:
            pass
        raise
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_file.close()
        stub.shutdown()
        stub.server_close()


if __name__ == "__main__":
    main()