"""Aggregate per-product grouped files into a single Shopify-import JSONL.

Reads every output/grouped_products/chewy_grouped_by_flavor_*.json, runs
dedupe_products_across_pages (collapses multi-landing-page products like
Wysong Archetype Chicken/Quail/Rabbit), and writes one JSON line per Shopify
product to output/shopify_import_{ts}.jsonl.

Run from the repo root after the unified scraper finishes:
    python tools/build_shopify_jsonl.py
"""

import json
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from chewy_next_json_extractor import dedupe_products_across_pages  # noqa: E402

GROUPED_DIR = BASE_DIR / "output" / "grouped_products"
OUT_DIR = BASE_DIR / "output"


def main() -> int:
    if not GROUPED_DIR.exists():
        print(f"ERROR: {GROUPED_DIR} does not exist.", file=sys.stderr)
        return 1

    files = sorted(GROUPED_DIR.glob("chewy_grouped_by_flavor_*.json"))
    if not files:
        print(f"ERROR: no grouped files in {GROUPED_DIR}", file=sys.stderr)
        return 1

    all_grouped: list[dict] = []
    parse_errors = 0
    for f in files:
        try:
            with open(f, "r", encoding="utf-8") as fh:
                all_grouped.append(json.load(fh))
        except (json.JSONDecodeError, OSError):
            parse_errors += 1

    print(f"Loaded {len(all_grouped)} grouped files ({parse_errors} parse errors)")

    dedupe_result = dedupe_products_across_pages(all_grouped)
    kept = dedupe_result["kept_products"]
    duplicates_log = dedupe_result["duplicates_log"]

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = OUT_DIR / f"shopify_import_{ts}.jsonl"
    log_path = OUT_DIR / f"shopify_import_dedupe_log_{ts}.json"
    with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
        for product in kept:
            fh.write(json.dumps(product, ensure_ascii=False) + "\n")
    with open(log_path, "w", encoding="utf-8") as fh:
        json.dump({
            "total_candidates": dedupe_result["total_candidates"],
            "unique_products": dedupe_result["unique_products"],
            "duplicates_log": duplicates_log,
        }, fh, indent=2, ensure_ascii=False)

    print(f"Candidates : {dedupe_result['total_candidates']}")
    print(f"After dedupe: {dedupe_result['unique_products']} ({len(duplicates_log)} collapsed groups)")
    print(f"Wrote       : {out_path}")
    print(f"Dedupe log  : {log_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
