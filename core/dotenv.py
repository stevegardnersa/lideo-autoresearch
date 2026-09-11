"""Minimal .env loader for CLI entry points.

Loads KEY=VALUE pairs from ``ROOT/.env`` into ``os.environ`` so defaults like
``--function-url`` resolve without the caller remembering to ``source .env``.
Existing environment variables always win (no clobbering).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Optional


def load_dotenv(path: Optional[Path] = None) -> Dict[str, str]:
    """Load ``.env`` into ``os.environ``; returns the keys set.

    Rules:
    - ``#`` comments and blank lines are skipped.
    - Surrounding single/double quotes are stripped from values.
    - A key already present in ``os.environ`` is never overwritten.
    """
    env_path = path or Path(__file__).resolve().parents[1] / ".env"
    if not env_path.is_file():
        return {}
    loaded: Dict[str, str] = {}
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value
            loaded[key] = value
    return loaded