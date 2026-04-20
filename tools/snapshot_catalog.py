#!/usr/bin/env python3
"""Snapshot the current OpenRouter model catalog and derived pricing table."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.openrouter_client import OpenRouterClient
from core.versioning import derive_price_snapshot_from_catalog, save_json


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-key-env", default="OPENROUTER_API_KEY")
    parser.add_argument("--catalog-dir", type=Path, default=PROJECT_ROOT / "snapshots" / "catalog")
    parser.add_argument("--price-dir", type=Path, default=PROJECT_ROOT / "snapshots" / "pricing")
    parser.add_argument("--referer", default="")
    parser.add_argument("--title", default="autoresearch-book-summary-benchmark")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    client = OpenRouterClient.from_env(
        api_key_env=args.api_key_env,
        referer=args.referer,
        title=args.title,
    )
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    catalog = client.fetch_models(refresh=True)
    catalog_path = args.catalog_dir / f"{timestamp}.json"
    price_path = args.price_dir / f"{timestamp}.json"

    save_json(
        catalog_path,
        {
            "captured_at_utc": timestamp,
            "models": {model_id: info.raw for model_id, info in sorted(catalog.items())},
        },
    )
    save_json(price_path, derive_price_snapshot_from_catalog(catalog))

    print(f"Wrote catalog snapshot: {catalog_path}")
    print(f"Wrote pricing snapshot: {price_path}")


if __name__ == "__main__":
    main()
