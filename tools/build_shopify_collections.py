"""Generate shopify_collections.csv — Smart Collections ruled by category tags.

Each unique category segment becomes one Smart Collection. The collection rule
matches any product tagged `category:<segment>` (which the product CSV emits).

Output: output/shopify_export/shopify_collections.csv

Two flavors of CSV are written:

1. Native Shopify Admin CSV (works in `Shopify Admin -> Collections -> Import`
   if your store enables collection CSV import).
2. Matrixify Excelify-compatible columns (commented out by default, can be
   re-enabled with --matrixify) — for stores that use the Matrixify app.

A summary text file `shopify_collections_summary.txt` lists every collection
with its product count, so user can sanity-check before importing.

Usage:
  python tools/build_shopify_collections.py
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import re
import sys
from collections import Counter

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)

from chewy_next_json_extractor import dedupe_products_across_pages  # noqa
from tools.category_resolver import resolve_category  # noqa


def slugify_collection(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="output/grouped_products")
    ap.add_argument("--output-dir", default="output/shopify_export")
    ap.add_argument(
        "--min-products",
        type=int,
        default=2,
        help="skip categories with fewer than this many products",
    )
    ap.add_argument("--matrixify", action="store_true",
                    help="Emit Matrixify Excelify-style columns")
    args = ap.parse_args()

    files = sorted(
        glob.glob(os.path.join(args.input, "chewy_grouped_by_flavor_*.json"))
    )
    all_grouped = []
    for f in files:
        try:
            all_grouped.append(json.load(open(f, "r", encoding="utf-8")))
        except Exception:
            pass
    res = dedupe_products_across_pages(all_grouped)
    kept = [p for p in res["kept_products"]
            if p.get("import_mode") != "blocked" and (p.get("variants") or [])]

    # Build normalized index for resolver
    norm_index: dict[str, dict] = {}
    for nf in glob.glob("output/normalized_products/chewy_*.json"):
        try:
            nd = json.load(open(nf, "r", encoding="utf-8"))
        except Exception:
            continue
        pid = str(nd.get("source_product_id") or "")
        if pid:
            norm_index[pid] = nd

    # Resolve category for every kept product
    seg_counts: dict[str, int] = Counter()
    path_counts: dict[tuple[str, ...], int] = Counter()
    seg_parents: dict[str, set[str]] = {}
    for p in kept:
        canonical = p.get("canonical_source") or {}
        pid = canonical.get("source_product_id") or (
            (p.get("source_group_id") or "").split(":")[0]
        )
        nrm = norm_index.get(str(pid), {})
        cat = resolve_category(nrm, p)
        path = cat["path"] or []
        path_counts[tuple(path)] += 1
        for i, seg in enumerate(path):
            if not seg:
                continue
            seg_counts[seg] += 1
            # Track parent for breadcrumb-style display
            seg_parents.setdefault(seg, set())
            if i > 0:
                seg_parents[seg].add(path[i - 1])

    # Build collection list, depth-first (parent first)
    # Depth of each segment = shortest path from any root that includes it
    depth_of: dict[str, int] = {}
    for path in path_counts:
        for i, seg in enumerate(path):
            if seg and (seg not in depth_of or i < depth_of[seg]):
                depth_of[seg] = i

    segments = [
        s for s in seg_counts
        if seg_counts[s] >= args.min_products
    ]
    segments.sort(key=lambda s: (depth_of.get(s, 99), -seg_counts[s], s))

    os.makedirs(args.output_dir, exist_ok=True)
    out_csv = os.path.join(args.output_dir, "shopify_collections.csv")
    out_sum = os.path.join(args.output_dir, "shopify_collections_summary.txt")

    # Shopify native Smart Collection CSV header (the format you'll see when
    # exporting collections from Admin -> Collections -> Export).
    headers = [
        "Handle",
        "Command",
        "Title",
        "Body HTML",
        "Sort Order",
        "Template Suffix",
        "Published",
        "Published Scope",
        "Row #",
        "Top Row",
        "Image Src",
        "Image Command",
        "Image Position",
        "Image Alt Text",
        "Collection Type",
        "Rules: Column",
        "Rules: Relation",
        "Rules: Condition",
        "Must Match",
        "Metafield: app--scope--key [type]",
    ]

    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
        w.writeheader()

        for seg in segments:
            count = seg_counts[seg]
            parents = sorted(seg_parents.get(seg) or [])
            handle = slugify_collection(seg)
            title = seg
            body = ""
            if parents:
                body = "<p>Auto-generated collection. Parent categories: " + ", ".join(parents) + ".</p>"
            row = {
                "Handle": handle,
                "Command": "MERGE",
                "Title": title,
                "Body HTML": body,
                "Sort Order": "best-selling",
                "Template Suffix": "",
                "Published": "TRUE",
                "Published Scope": "web",
                "Row #": 1,
                "Top Row": "TRUE",
                "Image Src": "",
                "Image Command": "",
                "Image Position": "",
                "Image Alt Text": "",
                "Collection Type": "Smart",
                "Rules: Column": "TAG",
                "Rules: Relation": "EQUALS",
                "Rules: Condition": f"category:{seg}",
                "Must Match": "all conditions",
                "Metafield: app--scope--key [type]": "",
            }
            w.writerow(row)

    # Summary file (human-readable)
    with open(out_sum, "w", encoding="utf-8") as f:
        f.write(f"Shopify Smart Collections — {len(segments)} collections\n")
        f.write("=" * 60 + "\n\n")
        f.write(f"Filter: collections with >= {args.min_products} products\n")
        f.write(f"Total products in catalog: {len(kept)}\n\n")
        f.write(f"{'Depth':>5}  {'Count':>5}  {'Collection (handle)':40}  Parents\n")
        f.write("-" * 90 + "\n")
        for seg in segments:
            d = depth_of.get(seg, 0)
            c = seg_counts[seg]
            handle = slugify_collection(seg)
            parents = ", ".join(sorted(seg_parents.get(seg) or set())) or "—"
            f.write(f"{d:>5}  {c:>5}  {seg + ' (' + handle + ')':40s}  {parents}\n")
        f.write("\n\n--- Top resolved category PATHS ---\n")
        for path, c in path_counts.most_common(30):
            f.write(f"  {c:>5}  {' > '.join(path)}\n")

    print(f"Wrote {out_csv}")
    print(f"Wrote {out_sum}")
    print(f"Generated {len(segments)} smart collections from {len(kept)} products")


if __name__ == "__main__":
    main()
