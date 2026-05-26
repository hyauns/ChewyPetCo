"""
PetSmart Variant Price Enricher
================================
Enriches existing scraped product JSON with per-variant pricing.
Fetches each product's PDP page to extract SKU→Size→Price mapping.

Usage:
    python petsmart_enrich_variants.py PetSmart/products.json
    python petsmart_enrich_variants.py PetSmart/products.json --workers 5
    python petsmart_enrich_variants.py PetSmart/products.json --output PetSmart/products_enriched.json
"""

import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}

# ─────────────────────────────────────────────────────────
# PDP Variant Extraction
# ─────────────────────────────────────────────────────────

def _parse_rsc(html: str) -> str:
    """Extract and decode the RSC (React Server Components) payload."""
    chunks = re.findall(
        r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', html, re.DOTALL
    )
    text = ""
    for chunk in chunks:
        try:
            text += chunk.encode().decode("unicode_escape")
        except Exception:
            text += chunk
    return text


def extract_variants(html: str) -> list[dict] | None:
    """Extract per-variant pricing from a PetSmart PDP page.

    Returns list of dicts like:
        [{"sku": "5175180", "size": "130 Count", "original_price": 39.99,
          "sale_price": 39.98, "availability": "InStock"}, ...]
    Returns None if no variant data found.
    """
    rsc_text = _parse_rsc(html)

    # 1. Size-to-SKU mapping from RSC
    #    Pattern: {"quantity:130 Count":"5175180", ...}
    #    or:      {"size:35 Lb":"5135727", ...}
    size_sku_map: dict[str, str] = {}  # sku -> size_label

    # Find all variant mapping objects
    for pattern in [
        r'\{("(?:quantity|size|flavor|color|style|count|weight)[^}]+)\}',
    ]:
        for m in re.finditer(pattern, rsc_text, re.IGNORECASE):
            raw = "{" + m.group(1) + "}"
            try:
                mapping = json.loads(raw)
                for k, v in mapping.items():
                    # k = "quantity:130 Count", v = "5175180"
                    label = k.split(":", 1)[-1] if ":" in k else k
                    size_sku_map[str(v)] = label
            except (json.JSONDecodeError, AttributeError):
                pass

    if not size_sku_map:
        return None

    # 2. SKU-to-Price mapping from RSC
    #    Pattern: "sku":"5175180","price":{"original":39.99,"sale":null,...}
    sku_price_map: dict[str, dict] = {}
    for m in re.finditer(
        r'"sku"\s*:\s*"(\d+)"\s*,\s*"price"\s*:\s*\{([^}]+)\}', rsc_text
    ):
        sku = m.group(1)
        try:
            price_data = json.loads("{" + m.group(2) + "}")
            sku_price_map[sku] = price_data
        except json.JSONDecodeError:
            pass

    # 3. Availability from JSON-LD
    sku_avail: dict[str, str] = {}
    for m in re.finditer(
        r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
        html, re.DOTALL,
    ):
        try:
            ld = json.loads(m.group(1))
            if ld.get("@type") == "Product":
                sku = str(ld.get("sku", ""))
                avail = ld.get("offers", {}).get("availability", "")
                sku_avail[sku] = "InStock" if "InStock" in avail else "OutOfStock"
        except json.JSONDecodeError:
            pass

    # 4. Combine — only keep variants with numeric SKU and price data
    variants = []
    for sku, size_label in sorted(
        size_sku_map.items(),
        key=lambda x: _sort_key(x[1]),
    ):
        # Skip non-numeric SKUs (these are label-only entries like "Salmon & Rice")
        if not sku.isdigit():
            continue
        # Skip variants without price data
        price = sku_price_map.get(sku, {})
        if not price.get("original"):
            continue
        variants.append({
            "sku": sku,
            "size": size_label,
            "original_price": price.get("original"),
            "sale_price": price.get("sale"),
            "availability": sku_avail.get(sku),
        })

    return variants if variants else None


def _sort_key(size_label: str):
    """Extract numeric value for sorting sizes."""
    m = re.search(r"([\d.]+)", size_label)
    return float(m.group(1)) if m else 0


# ─────────────────────────────────────────────────────────
# Build PDP URL from product data
# ─────────────────────────────────────────────────────────

def build_pdp_url(product: dict) -> str | None:
    """Build the PDP URL for a product."""
    # Use petsmart_url if available
    url = product.get("petsmart_url")
    if url:
        return url
    # Fallback: construct from name + sku
    name = product.get("name")
    sku = product.get("sku") or product.get("product_id")
    if name and sku:
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        return f"https://www.petsmart.com/{slug}-{sku}.html"
    return None


# ─────────────────────────────────────────────────────────
# Fetch single product variants
# ─────────────────────────────────────────────────────────

def fetch_product_variants(product: dict, session: requests.Session) -> list[dict] | None:
    """Fetch PDP and extract variants for a single product."""
    url = build_pdp_url(product)
    if not url:
        return None
    try:
        resp = session.get(url, headers=HEADERS, timeout=20)
        if resp.status_code != 200:
            return None
        return extract_variants(resp.text)
    except Exception:
        return None


# ─────────────────────────────────────────────────────────
# Main enrichment logic
# ─────────────────────────────────────────────────────────

def enrich_products(
    products: list[dict],
    *,
    workers: int = 3,
    delay: float = 0.2,
) -> tuple[list[dict], int, int]:
    """Enrich products with variant pricing.

    Returns (enriched_products, success_count, skip_count).
    """
    total = len(products)
    enriched = 0
    skipped = 0
    session = requests.Session()

    # Group by master_product_id to avoid duplicate PDP fetches
    master_groups: dict[int, list[int]] = {}
    for i, p in enumerate(products):
        mid = p.get("master_product_id")
        if mid:
            master_groups.setdefault(mid, []).append(i)

    unique_masters = len(master_groups)
    print(f"  {total} products, {unique_masters} unique master products to fetch")

    # Process each unique master product
    processed = 0
    for mid, indices in master_groups.items():
        # Use first product in group as representative
        rep = products[indices[0]]
        variants = fetch_product_variants(rep, session)

        if variants:
            # Apply variants to all products with same master_product_id
            for idx in indices:
                products[idx]["variants"] = variants
            enriched += len(indices)
        else:
            skipped += len(indices)

        processed += 1
        if processed % 50 == 0 or processed == unique_masters:
            pct = processed / unique_masters * 100
            print(f"  Progress: {processed}/{unique_masters} ({pct:.0f}%) — {enriched} enriched, {skipped} skipped")

        if delay > 0:
            time.sleep(delay)

    return products, enriched, skipped


# ─────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Enrich PetSmart product JSON with per-variant pricing",
    )
    parser.add_argument("input", help="Input products JSON file")
    parser.add_argument("--output", default=None, help="Output file (default: overwrite input)")
    parser.add_argument("--workers", type=int, default=1, help="Concurrent workers (default: 1)")
    parser.add_argument("--delay", type=float, default=0.2, help="Delay between requests (default: 0.2s)")
    parser.add_argument("--limit", type=int, default=None, help="Limit number of products to enrich (for testing)")
    args = parser.parse_args()

    input_path = args.input
    output_path = args.output or input_path

    print("=" * 60)
    print("PetSmart Variant Price Enricher")
    print("=" * 60)
    print(f"Input:  {input_path}")
    print(f"Output: {output_path}")

    # Load
    with open(input_path, "r", encoding="utf-8") as f:
        products = json.load(f)
    print(f"Loaded {len(products)} products")

    if args.limit:
        products = products[:args.limit]
        print(f"Limited to {args.limit} products for testing")

    start = time.time()
    products, enriched, skipped = enrich_products(
        products, workers=args.workers, delay=args.delay,
    )
    elapsed = time.time() - start

    # Save
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(products, f, indent=2, ensure_ascii=False)

    print(f"\nDone in {elapsed:.1f}s")
    print(f"  Enriched: {enriched}/{len(products)}")
    print(f"  Skipped:  {skipped}/{len(products)}")
    print(f"  Saved to: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
