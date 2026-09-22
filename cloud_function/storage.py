"""Resolve a chapter's source markdown from Supabase Storage.

The Cloud Function accepts a *structured* reference only — never an arbitrary
URL — so this is not an SSRF vector. The object path is built entirely from
validated parts (``books/{book_id}/original/{chapter_order}.md``) against a host
taken from the environment.

Credentials come from the environment: ``SUPABASE_URL`` (plain env var) and
``SUPABASE_SERVICE_ROLE_KEY`` (injected from GCP Secret Manager via
``--set-secrets``). The service-role key should be scoped to the ``books``
bucket only.

Errors are split so the caller can decide whether to retry:

* :class:`SourceRefError`   — invalid/absent object (permanent; do not retry).
* :class:`SourceFetchError` — network/5xx/auth misconfiguration (retryable).
"""

from __future__ import annotations

import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Mapping

DEFAULT_BUCKET = "books"
FETCH_TIMEOUT_SECONDS = 30

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


class SourceRefError(RuntimeError):
    """The structured reference is invalid or points at a missing object."""


class SourceFetchError(RuntimeError):
    """The storage fetch failed for a transient reason (retryable)."""


def build_object_path(ref: Mapping[str, Any]) -> str:
    """Validate ``ref`` and return the in-bucket object key (raises on bad input).

    The key is ``books/{book_id}/original/{chapter_order}.md`` — note the
    leading ``books/`` folder is part of the *key*, independent of the bucket
    name. The Supabase REST URL is ``/storage/v1/object/{bucket}/{key}``, so the
    bucket segment appears twice.
    """
    bucket = str(ref.get("bucket") or DEFAULT_BUCKET).strip() or DEFAULT_BUCKET
    if bucket != DEFAULT_BUCKET:
        raise SourceRefError(f"source_ref.bucket must be {DEFAULT_BUCKET!r}")

    book_id = str(ref.get("book_id") or "").strip()
    if not _UUID_RE.match(book_id):
        raise SourceRefError("source_ref.book_id must be a UUID")

    chapter_order = ref.get("chapter_order")
    if (
        isinstance(chapter_order, bool)
        or not isinstance(chapter_order, int)
        or chapter_order < 0
    ):
        raise SourceRefError("source_ref.chapter_order must be an integer >= 0")

    return f"{DEFAULT_BUCKET}/{book_id}/original/{chapter_order}.md"


def resolve_source_ref(ref: Mapping[str, Any]) -> str:
    """Download the referenced object and return its text (UTF-8)."""
    key = build_object_path(ref)
    bucket = DEFAULT_BUCKET
    path = f"{bucket}/{key}"

    base = str(os.environ.get("SUPABASE_URL") or "").strip().rstrip("/")
    key_secret = str(os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
    if not base or not key_secret:
        raise SourceFetchError(
            "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be configured"
        )

    url = f"{base}/storage/v1/object/{urllib.parse.quote(path)}"
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {key_secret}", "apikey": key_secret},
    )
    try:
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_SECONDS) as response:  # noqa: S310 - host is fixed from env
            text = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        # Supabase returns 404 for a missing object, and some versions return
        # 400 with a not-found body — both are permanent.
        if exc.code in (400, 404):
            raise SourceRefError(f"source object not found: {path}") from exc
        raise SourceFetchError(f"storage HTTP {exc.code} for {path}") from exc
    except urllib.error.URLError as exc:
        raise SourceFetchError(f"storage fetch failed for {path}: {exc.reason}") from exc

    if not text.strip():
        raise SourceRefError(f"source object is empty: {path}")
    return text
