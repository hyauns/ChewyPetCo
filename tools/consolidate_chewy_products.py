"""Consolidate ALL normalized Chewy grouped JSON files into one JSON.

The scraped+normalized Chewy data lives as `chewy_grouped_by_flavor_*.json` files
spread across many batch/output directories (heavily overlapping). This tool:

  1. Recursively collects every chewy_grouped_by_flavor_*.json under --root
     (PetSmart paths are excluded).
  2. Loads them and runs the SAME cross-page dedupe the Shopify build uses
     (chewy_next_json_extractor.dedupe_products_across_pages), so the product
     count matches what an actual import would produce.
  3. Writes one merged JSON: {"products": [...], "stats": {...}}.

No content filtering (no Rx / price / stock filters) — dedupe only, per request.

Usage:
  # trial: just count, write nothing
  python tools/consolidate_chewy_products.py --dry-run

  # write the merged file
  python tools/consolidate_chewy_products.py --out output/chewy_all_products.json
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from collections import Counter

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass

from chewy_next_json_extractor import dedupe_products_across_pages  # noqa: E402
from tools.build_shopify_csv import is_rx_title  # noqa: E402

# is_rx_title covers compounded/capsule/tablet/injection/ophthalmic/otic/
# prescription/oral suspension|solution/insulin/suppository/rx. Add "caplets",
# which it does not catch.
_EXTRA_RX = re.compile(r"\bcaplets?\b", re.I)
# Chewy private-label / exclusive: the word "chewy" in the title.
_CHEWY_BRAND = re.compile(r"\bchewy\b", re.I)


def is_rx(title: str) -> bool:
    return is_rx_title(title or "") or bool(_EXTRA_RX.search(title or ""))


def is_chewy_brand(product: dict) -> bool:
    title = product.get("title") or ""
    brand = product.get("brand") or ""
    return bool(_CHEWY_BRAND.search(title) or _CHEWY_BRAND.search(brand))


def find_files(root: str) -> list[str]:
    pat = os.path.join(root, "**", "chewy_grouped_by_flavor_*.json")
    files = glob.glob(pat, recursive=True)
    return [f for f in files if "petsmart" not in f.lower()]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=os.path.join(_BASE, "output"))
    ap.add_argument("--out", default=os.path.join(_BASE, "output", "chewy_all_products.json"))
    ap.add_argument("--dry-run", action="store_true", help="Count only, write nothing")
    ap.add_argument("--keep-rx", action="store_true",
                    help="Do NOT drop Rx products (default: drop them)")
    ap.add_argument("--keep-chewy", action="store_true",
                    help="Do NOT drop Chewy-brand products (default: drop them)")
    args = ap.parse_args()

    files = find_files(args.root)
    print(f"Found {len(files):,} grouped JSON files (PetSmart excluded)")

    all_grouped = []
    bad = 0
    raw_products = 0
    seen_source_pids: set[str] = set()
    for i, f in enumerate(files):
        if i % 5000 == 0 and i:
            print(f"  loaded {i:,}/{len(files):,} ...")
        try:
            d = json.load(open(f, encoding="utf-8"))
        except Exception:
            bad += 1
            continue
        if not isinstance(d, dict):
            bad += 1
            continue
        all_grouped.append(d)
        seen_source_pids.add(str(d.get("source_product_id") or ""))
        raw_products += len(d.get("products") or [])

    print(f"  parsed OK: {len(all_grouped):,}   parse-failed: {bad:,}")
    print(f"  distinct source_product_id (landing pages): {len(seen_source_pids):,}")
    print(f"  raw products before dedupe (sum of products[]): {raw_products:,}")

    result = dedupe_products_across_pages(all_grouped)
    kept = result["kept_products"]
    print()
    print(f"  total candidates : {result.get('total_candidates'):,}")
    print(f"  UNIQUE products  : {result.get('unique_products'):,}")
    print(f"  kept_products    : {len(kept):,}")

    # variant totals + products with empty fingerprint (no variant entry ids)
    no_fp = sum(1 for p in kept if not (p.get("canonical_source") or {}).get("fingerprint"))
    variants = sum(len(p.get("variants") or []) for p in kept)
    print(f"  total variants   : {variants:,}")
    if no_fp:
        print(f"  WARNING: {no_fp:,} kept products had no variant fingerprint "
              f"(may be over-collapsed)")

    # -- content filters: Rx + Chewy brand -----------------------------------
    n_rx = n_chewy = n_both = 0
    filtered = []
    for p in kept:
        title = p.get("title") or ""
        rx = (not args.keep_rx) and is_rx(title)
        chewy = (not args.keep_chewy) and is_chewy_brand(p)
        if rx and chewy:
            n_both += 1
        if rx:
            n_rx += 1
        if chewy:
            n_chewy += 1
        if rx or chewy:
            continue
        filtered.append(p)

    print()
    print("  -- filters --")
    print(f"  dropped as Rx          : {n_rx:,}"
          + ("  [SKIPPED: --keep-rx]" if args.keep_rx else ""))
    print(f"  dropped as Chewy brand : {n_chewy:,}"
          + ("  [SKIPPED: --keep-chewy]" if args.keep_chewy else ""))
    print(f"  (overlap Rx & Chewy)   : {n_both:,}")
    print(f"  >>> FINAL products     : {len(filtered):,}")
    final_variants = sum(len(p.get("variants") or []) for p in filtered)
    print(f"  >>> final variants     : {final_variants:,}")
    kept = filtered
    variants = final_variants

    if args.dry_run:
        print("\n[dry-run] nothing written.")
        return

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    payload = {
        "products": kept,
        "stats": {
            "files_scanned": len(files),
            "files_parsed": len(all_grouped),
            "distinct_source_product_ids": len(seen_source_pids),
            "raw_products": raw_products,
            "dropped_rx": n_rx,
            "dropped_chewy_brand": n_chewy,
            "final_products": len(kept),
            "total_variants": variants,
        },
    }
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)
    print(f"\nWrote {len(kept):,} products -> {args.out} "
          f"({os.path.getsize(args.out)/1e6:.1f} MB)")


if __name__ == "__main__":
    main()
