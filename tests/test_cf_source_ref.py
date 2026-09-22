"""Offline tests for the Cloud Function structured ``source_ref`` support.

Run with: python3 tests/test_cf_source_ref.py

Covers:
  * ``cloud_function.storage.build_object_path`` validation (SSRF-safe shape).
  * ``resolve_source_ref`` download / 404 / 5xx / missing-config behaviour.
  * ``handler._resolve_source_md`` inline-vs-ref precedence.
  * ``main.summarize`` request validation: exactly one source form.

A local ``http.server`` stands in for Supabase Storage, and a stub
``functions_framework`` module lets ``main`` import without the runtime
dependency (which is only installed for the emulator).
"""

import json
import sys
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# --- stub functions_framework before importing cloud_function.main ----------
if "functions_framework" not in sys.modules:
    ff = types.ModuleType("functions_framework")

    def _http(fn):
        return fn

    ff.http = _http
    sys.modules["functions_framework"] = ff

from cloud_function import storage  # noqa: E402
from cloud_function import handler  # noqa: E402
from cloud_function import main  # noqa: E402

BOOK_ID = "2f0c7f55-2df1-4e07-bccc-f608764f6934"
REF = {"bucket": "books", "book_id": BOOK_ID, "chapter_order": 8}


class _StorageStub(BaseHTTPRequestHandler):
    """Serves /storage/v1/object/<path>; records the last Authorization header."""

    status = 200
    payload = b"THE FATE OF British Cycling changed one day in 2003."
    last_path = None
    last_authorization = None

    def do_GET(self):  # noqa: N802 (http.server API)
        type(self).last_path = self.path
        type(self).last_authorization = self.headers.get("Authorization")
        self.send_response(self.status)
        self.end_headers()
        self.wfile.write(self.payload)

    def log_message(self, *args):  # silence
        return


def _serve(handler_cls):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def test_build_object_path():
    assert storage.build_object_path(REF) == f"books/{BOOK_ID}/original/8.md"
    # bucket defaults when omitted
    assert storage.build_object_path({"book_id": BOOK_ID, "chapter_order": 0}) == (
        f"books/{BOOK_ID}/original/0.md"
    )
    # rejects traversal / bad shapes
    for bad in (
        {"bucket": "other", "book_id": BOOK_ID, "chapter_order": 1},
        {"book_id": "not-a-uuid", "chapter_order": 1},
        {"book_id": BOOK_ID, "chapter_order": -1},
        {"book_id": BOOK_ID, "chapter_order": 1.5},
        {"book_id": BOOK_ID, "chapter_order": True},
        {"book_id": BOOK_ID},
    ):
        try:
            storage.build_object_path(bad)
        except storage.SourceRefError:
            continue
        raise AssertionError(f"expected SourceRefError for {bad!r}")
    print("test_build_object_path: OK")


def test_resolve_source_ref_success():
    server = _serve(_StorageStub)
    try:
        host, port = server.server_address
        import os

        os.environ["SUPABASE_URL"] = f"http://{host}:{port}"
        os.environ["SUPABASE_SERVICE_ROLE_KEY"] = "svc-key"
        text = storage.resolve_source_ref(REF)
        assert text.startswith("THE FATE OF British Cycling")
        assert _StorageStub.last_path == f"/storage/v1/object/books/books/{BOOK_ID}/original/8.md"
        assert _StorageStub.last_authorization == "Bearer svc-key"

        _StorageStub.status = 404
        try:
            storage.resolve_source_ref(REF)
        except storage.SourceRefError:
            pass
        else:
            raise AssertionError("expected SourceRefError on 404")

        _StorageStub.status = 500
        try:
            storage.resolve_source_ref(REF)
        except storage.SourceFetchError:
            pass
        else:
            raise AssertionError("expected SourceFetchError on 500")
        _StorageStub.status = 200
    finally:
        server.shutdown()
    print("test_resolve_source_ref_success: OK")


def test_resolve_source_ref_missing_config():
    import os

    saved = {k: os.environ.pop(k, None) for k in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY")}
    try:
        try:
            storage.resolve_source_ref(REF)
        except storage.SourceFetchError:
            pass
        else:
            raise AssertionError("expected SourceFetchError without config")
    finally:
        for key, value in saved.items():
            if value is not None:
                os.environ[key] = value
    print("test_resolve_source_ref_missing_config: OK")


def test_resolve_source_md_precedence():
    assert handler._resolve_source_md({"source_md": " inline "}) == "inline"
    assert handler._resolve_source_md({"chapter": "alias"}) == "alias"

    calls = {}
    original = storage.resolve_source_ref

    def fake(ref):
        calls["ref"] = ref
        return "from-storage"

    storage.resolve_source_ref = fake
    try:
        assert handler._resolve_source_md({"source_ref": REF}) == "from-storage"
        assert calls["ref"] == REF
    finally:
        storage.resolve_source_ref = original

    assert handler._resolve_source_md({}) == ""
    print("test_resolve_source_md_precedence: OK")


class _Req:
    def __init__(self, body, method="POST"):
        self.method = method
        self._body = body

    def get_json(self, silent=False):
        return self._body


def _call_main(body):
    result = main.summarize(_Req(body))
    payload, status, _headers = result
    return status, json.loads(payload)


def test_main_requires_exactly_one_source():
    status, body = _call_main({"source_md": "x", "source_ref": REF})
    assert status == 400, (status, body)

    status, body = _call_main({"target_words": 100})
    assert status == 400, (status, body)

    # A valid source_ref passes validation; stub the summariser so no network.
    original = main.run_summarize
    main.run_summarize = lambda body: {"success": True, "summary": {"summary_md": "ok"}}
    try:
        status, body = _call_main({"source_ref": REF, "target_words": 100})
        assert status == 200 and body["success"] is True, (status, body)
    finally:
        main.run_summarize = original
    print("test_main_requires_exactly_one_source: OK")


def main_test():
    test_build_object_path()
    test_resolve_source_ref_success()
    test_resolve_source_ref_missing_config()
    test_resolve_source_md_precedence()
    test_main_requires_exactly_one_source()
    print("all source_ref tests passed")


if __name__ == "__main__":
    main_test()
