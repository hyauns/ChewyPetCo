"""Build a WooCommerce product-import CSV from the consolidated Chewy products.

Input : output/chewy_all_products.json  (from tools/consolidate_chewy_products.py)
        output/image_url_map.json        (from tools/upload_images_r2.py) - optional

Output: output/woocommerce_products.csv

Model: each Chewy product becomes ONE WooCommerce *variable* parent row plus one
*variation* row per variant (Size / Flavor dropdowns). The Images column points
at the hosted (R2/CDN) URLs from image_url_map.json; if the map is missing a URL,
the original Chewy URL is used as a fallback (counted + warned).

WooCommerce note: the native importer splits the "Attribute N value(s)" cell on
commas, so commas inside option values (e.g. "5.5-oz, case of 24") are rewritten
to " - " to avoid being mis-split into two values.

Usage:
  python tools/build_woocommerce_csv.py
  python tools/build_woocommerce_csv.py --limit 50      # quick sample
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass

from tools.build_shopify_csv import clean_price, md_to_html, safe_str  # noqa: E402

# Fixed leading columns; attribute + meta columns appended after.
BASE_HEADER = [
    "Type", "SKU", "Name", "Published", "Is featured?", "Visibility in catalog",
    "Short description", "Description", "Tax status", "In stock?", "Stock",
    "Backorders allowed?", "Sold individually?", "Allow customer reviews?",
    "Sale price", "Regular price", "Categories", "Tags", "Images",
    "Parent", "Position", "Brands",
]
ATTR_HEADER = [
    "Attribute 1 name", "Attribute 1 value(s)", "Attribute 1 visible", "Attribute 1 global",
    "Attribute 2 name", "Attribute 2 value(s)", "Attribute 2 visible", "Attribute 2 global",
]
META_HEADER = [
    "Meta: ingredients", "Meta: guaranteed_analysis", "Meta: feeding_instructions",
    "Meta: source_url", "Meta: source_product_id",
]
HEADER = BASE_HEADER + ATTR_HEADER + META_HEADER


def attr_safe(v: str) -> str:
    """Woo splits attribute values on comma -> rewrite internal commas."""
    return safe_str(v).replace(", ", " - ").replace(",", " - ").strip()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", default=os.path.join(_BASE, "output", "chewy_all_products.json"))
    ap.add_argument("--map", default=os.path.join(_BASE, "output", "image_url_map.json"))
    ap.add_argument("--out", default=os.path.join(_BASE, "output", "woocommerce_products.csv"))
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    products = json.load(open(args.inp, encoding="utf-8")).get("products", [])
    if args.limit:
        products = products[:args.limit]
    print(f"Loaded {len(products):,} products")

    img_map = {}
    if os.path.exists(args.map):
        img_map = json.load(open(args.map, encoding="utf-8"))
        print(f"Loaded image map: {len(img_map):,} URLs")
    else:
        print("WARNING: no image_url_map.json -> using original Chewy URLs as fallback")

    unmapped = 0

    def host(url: str) -> str:
        nonlocal unmapped
        if not url:
            return ""
        m = img_map.get(url)
        if m:
            return m
        unmapped += 1
        return url  # fallback to original Chewy URL

    used_skus: set[str] = set()

    def parent_sku(p: dict, idx: int) -> str:
        base = p.get("handle_slug") or f"chewy-{idx}"
        sku = base
        n = 2
        while sku in used_skus:
            sku = f"{base}-{n}"
            n += 1
        used_skus.add(sku)
        return sku

    n_parent = n_var = 0
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(HEADER)

        for idx, p in enumerate(products):
            variants = p.get("variants") or []
            if not variants:
                continue
            psku = parent_sku(p, idx)

            # detect attribute names present across variants (option1/option2)
            a1_name = ""
            a2_name = ""
            a1_vals: list[str] = []
            a2_vals: list[str] = []
            for v in variants:
                if v.get("option1_name") and not a1_name:
                    a1_name = v["option1_name"]
                if v.get("option2_name") and not a2_name:
                    a2_name = v["option2_name"]
            for v in variants:
                v1 = attr_safe(v.get("option1_value"))
                v2 = attr_safe(v.get("option2_value"))
                if v1 and v1 not in a1_vals:
                    a1_vals.append(v1)
                if v2 and v2 not in a2_vals:
                    a2_vals.append(v2)
            if not a1_name:
                a1_name = "Size"
            cat = " > ".join(p.get("category_path") or []) if isinstance(p.get("category_path"), list) else safe_str(p.get("category_path"))
            parent_imgs = ", ".join(host(u) for u in (p.get("images") or []) if u)

            # ---- parent (variable) row ----
            row = {h: "" for h in HEADER}
            row.update({
                "Type": "variable",
                "SKU": psku,
                "Name": safe_str(p.get("title")),
                "Published": "1",
                "Is featured?": "0",
                "Visibility in catalog": "visible",
                "Description": md_to_html(p.get("description")),
                "Tax status": "taxable",
                "In stock?": "1" if any(v.get("in_stock") for v in variants) else "0",
                "Backorders allowed?": "0",
                "Sold individually?": "0",
                "Allow customer reviews?": "1",
                "Categories": cat,
                "Images": parent_imgs,
                "Brands": safe_str(p.get("brand")),
                "Attribute 1 name": a1_name,
                "Attribute 1 value(s)": ", ".join(a1_vals),
                "Attribute 1 visible": "1",
                "Attribute 1 global": "1",
                "Meta: ingredients": safe_str(p.get("ingredients")),
                "Meta: guaranteed_analysis": safe_str(p.get("guaranteed_analysis")),
                "Meta: feeding_instructions": safe_str(p.get("feeding_instructions")),
                "Meta: source_url": safe_str((p.get("canonical_source") or {}).get("source_url")),
                "Meta: source_product_id": safe_str((p.get("canonical_source") or {}).get("source_product_id")),
            })
            if a2_name:
                row.update({
                    "Attribute 2 name": a2_name,
                    "Attribute 2 value(s)": ", ".join(a2_vals),
                    "Attribute 2 visible": "1",
                    "Attribute 2 global": "1",
                })
            w.writerow([row[h] for h in HEADER])
            n_parent += 1

            # ---- variation rows ----
            for pos, v in enumerate(variants):
                vimg = host((v.get("images") or [None])[0]) if v.get("images") else ""
                vrow = {h: "" for h in HEADER}
                vrow.update({
                    "Type": "variation",
                    "SKU": safe_str(v.get("sku")),
                    "Published": "1",
                    "Visibility in catalog": "visible",
                    "Tax status": "taxable",
                    "In stock?": "1" if v.get("in_stock") else "0",
                    "Backorders allowed?": "0",
                    "Regular price": clean_price(v.get("price")),
                    "Sale price": clean_price(v.get("compare_at_price")),
                    "Images": vimg,
                    "Parent": psku,
                    "Position": str(pos),
                    "Attribute 1 name": a1_name,
                    "Attribute 1 value(s)": attr_safe(v.get("option1_value")),
                })
                if a2_name:
                    vrow.update({
                        "Attribute 2 name": a2_name,
                        "Attribute 2 value(s)": attr_safe(v.get("option2_value")),
                    })
                w.writerow([vrow[h] for h in HEADER])
                n_var += 1

    print(f"\nWrote {args.out}")
    print(f"  parent (variable) rows : {n_parent:,}")
    print(f"  variation rows         : {n_var:,}")
    print(f"  total rows             : {n_parent + n_var:,}")
    if unmapped:
        print(f"  image URLs NOT in map (used Chewy URL fallback): {unmapped:,}")


if __name__ == "__main__":
    main()
