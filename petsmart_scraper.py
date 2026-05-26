"""
PetSmart Category Scraper — Algolia API
========================================
Scrape toàn bộ sản phẩm từ PetSmart category URL.
Không cần browser, không cần proxy, chỉ dùng HTTP requests.

Usage:
    python petsmart_scraper.py --url "https://www.petsmart.com/dog/food/dry-food"
    python petsmart_scraper.py --url "https://www.petsmart.com/dog/food/dry-food" --output output/petsmart
    python petsmart_scraper.py --url "https://www.petsmart.com/dog/food/dry-food" --delay 0.5
"""

import argparse
import json
import os
import re
import time
from datetime import datetime, timezone
from html import unescape
from typing import Any
from urllib.parse import urlparse

import requests

# ─────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────

ALGOLIA_URL = "https://www.petsmart.com/api/search/1/indexes/r-US_products_best-sellers/query"
HITS_PER_PAGE = 1000
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Content-Type": "application/json",
    "Accept": "application/json",
}
IMAGE_BASE = "https://s7d2.scene7.com/is/image/PetSmart"
VIDEO_BASE = "https://s7d2.scene7.com/is/content/PetSmart"


# ─────────────────────────────────────────────────────────
# URL → Algolia Category Path
# ─────────────────────────────────────────────────────────

def url_to_category_path(category_url: str) -> str:
    """Convert a PetSmart URL to its Algolia `custom_category_names` path.

    /dog/food/dry-food  →  Dog > Food > Dry Food
    """
    path = urlparse(category_url).path.strip("/")
    path = re.sub(r"\.html$", "", path)
    segments = path.split("/")
    return " > ".join(seg.replace("-", " ").title() for seg in segments)


def _fetch_all_category_paths() -> dict[str, int]:
    """Fetch all Algolia category paths and their product counts."""
    payload = {"params": 'hitsPerPage=0&facets=["custom_category_names"]'}
    try:
        resp = requests.post(ALGOLIA_URL, json=payload, headers=HEADERS, timeout=15)
        data = resp.json()
        return data.get("facets", {}).get("custom_category_names", {})
    except Exception:
        return {}


def resolve_category_path(category_url: str) -> str:
    """Resolve a PetSmart URL to the actual Algolia category path.

    First tries the literal conversion.  If that yields 0 hits,
    fetches all Algolia category facets and finds the best match
    based on the URL segments.
    """
    literal = url_to_category_path(category_url)

    # Quick check: does the literal path return results?
    payload = {"params": f'hitsPerPage=0&filters=custom_category_names:"{literal}"'}
    try:
        resp = requests.post(ALGOLIA_URL, json=payload, headers=HEADERS, timeout=10)
        if resp.json().get("nbHits", 0) > 0:
            return literal
    except Exception:
        pass

    # Literal didn't work — fuzzy match against all Algolia categories
    print(f"  Literal path '{literal}' returned 0 hits. Auto-resolving...")
    all_cats = _fetch_all_category_paths()
    if not all_cats:
        return literal  # fallback

    # Build keywords from URL segments (lowercased)
    url_path = urlparse(category_url).path.strip("/")
    url_path = re.sub(r"\.html$", "", url_path)
    segments = [seg.replace("-", " ").lower() for seg in url_path.split("/")]
    # First segment is the pet type (dog, cat, etc.)
    pet_type = segments[0] if segments else ""
    # Last segment is the leaf category
    leaf = segments[-1] if segments else ""

    best_path = literal
    best_score = -1

    for cat_path, count in all_cats.items():
        cat_lower = cat_path.lower()
        # Must start with the pet type
        if pet_type and not cat_lower.startswith(pet_type):
            continue
        # Must contain the leaf category
        if leaf and leaf not in cat_lower:
            continue
        # Score: number of URL segments matched + depth bonus
        score = sum(1 for seg in segments if seg in cat_lower)
        depth = cat_lower.count(">")
        # Prefer deeper matches (more specific)
        combined = score * 100 + depth * 10 + (count > 0)
        if combined > best_score:
            best_score = combined
            best_path = cat_path

    if best_path != literal:
        print(f"  Resolved to: '{best_path}'")
    return best_path


# ─────────────────────────────────────────────────────────
# HTML Description → Structured Fields
# ─────────────────────────────────────────────────────────

_FIELD_PATTERN = re.compile(
    r"<b>\s*(.*?)\s*:?\s*</b>\s*(.*?)(?=<p>\s*<b>|$)",
    re.DOTALL | re.IGNORECASE,
)


def _strip_html(html: str) -> str:
    """Remove HTML tags and decode entities."""
    text = re.sub(r"<br\s*/?>", "\n", html)
    text = re.sub(r"</p>", "\n", text)
    text = re.sub(r"</li>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = unescape(text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def parse_description(html: str | None) -> dict[str, str]:
    """Extract labeled sections from the PetSmart long_description HTML.

    Returns a dict like:
        {"description": "...", "ingredients": "...", "guaranteed_analysis": "...", ...}
    """
    if not html:
        return {}

    fields: dict[str, str] = {}
    for match in _FIELD_PATTERN.finditer(html):
        label = _strip_html(match.group(1)).strip().rstrip(":")
        value = _strip_html(match.group(2)).strip()
        if not label or not value:
            continue
        # Normalize key
        key = label.lower().replace(" ", "_").replace("&", "and")
        key = re.sub(r"[^a-z0-9_]", "", key)
        fields[key] = value

    return fields


# ─────────────────────────────────────────────────────────
# Image URL helpers
# ─────────────────────────────────────────────────────────

def build_image_urls(hit: dict) -> list[str]:
    """Build full-resolution image URLs from Algolia hit."""
    urls: list[str] = []
    sku = hit.get("sku") or hit.get("id")
    if sku:
        urls.append(f"{IMAGE_BASE}/{sku}")
    for alt_id in hit.get("alternateImages") or []:
        urls.append(f"{IMAGE_BASE}/{alt_id}")
    return urls


def build_video_url(hit: dict) -> str | None:
    """Build video URL if available."""
    video = hit.get("video")
    if not video:
        return None
    sizes = video.get("sizes", {})
    vid_id = sizes.get("large") or sizes.get("medium") or sizes.get("small")
    if vid_id:
        return f"{VIDEO_BASE}/{vid_id}"
    return None


# ─────────────────────────────────────────────────────────
# Transform raw Algolia hit → clean product dict
# ─────────────────────────────────────────────────────────

def transform_product(hit: dict) -> dict[str, Any]:
    """Convert a raw Algolia hit into a clean, flat(ish) product dict."""
    desc_fields = parse_description(hit.get("long_description"))
    price = hit.get("price") or {}

    product: dict[str, Any] = {
        # Identity
        "product_id": hit.get("id"),
        "master_product_id": hit.get("masterProductID"),
        "sku": hit.get("sku"),
        "upc": hit.get("upc"),
        "manufacturer_sku": hit.get("manufacturerSku"),
        # Basic info
        "name": hit.get("name"),
        "brand": hit.get("brand"),
        "manufacturer": hit.get("manufacturerName"),
        "primary_category": hit.get("primary_category_name"),
        "categories": hit.get("custom_category_names"),
        # Pricing
        "price": price.get("number"),
        "price_display": price.get("formatted", {}).get("primary"),
        "price_strikethrough": price.get("formatted", {}).get("strikethrough"),
        "price_type": price.get("displayType"),
        # Variant info
        "flavor": (hit.get("flavor") or {}).get("value"),
        "size": (hit.get("size") or {}).get("solidSize") or (hit.get("size") or {}).get("fluidSize"),
        "variations": hit.get("variations"),
        "variation_data": hit.get("variationData"),
        # Pet/food attributes
        "lifestages": hit.get("dogLifestages") or hit.get("catLifestages"),
        "kibble_sizes": hit.get("kibbleSizes"),
        "nutritional_options": hit.get("nutritionalOptions"),
        "food_forms": hit.get("foodForms"),
        "food_category": hit.get("foodCategory"),
        # Media
        "images": build_image_urls(hit),
        "video_url": build_video_url(hit),
        # Ratings
        "rating": hit.get("bvAverageRating"),
        "review_count": hit.get("bvReviewCount"),
        # Shipping / availability
        "is_subscription_enabled": hit.get("isSubscriptionEnabled"),
        "is_bopis_eligible": hit.get("shoppingOptions-isBopisEligible"),
        "is_in_store_only": hit.get("shoppingOptions-isInStoreOnly"),
        # Dimensions
        "dimensions_and_weight": hit.get("dimensionsAndWeight"),
        # Promotions
        "promotions": [
            {"text": p.get("promoText"), "key": p.get("promoKey"),
             "start": p.get("promoStart"), "end": p.get("promoEnd"),
             "discount": p.get("discountDisplay")}
            for p in (hit.get("eligiblePromotions") or [])
        ] or None,
        # Parsed description fields
        **{f"detail_{k}": v for k, v in desc_fields.items()},
        # Source URL
        "petsmart_url": _build_product_url(hit),
    }
    # Remove None values to keep output clean
    return {k: v for k, v in product.items() if v is not None}


def _build_product_url(hit: dict) -> str | None:
    """Construct a PetSmart product URL from hit data."""
    name = hit.get("name")
    sku = hit.get("sku") or hit.get("id")
    if not name or not sku:
        return None
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    cat = hit.get("primary_category_name", "").lower().replace(" ", "-").replace("&", "and")
    return f"https://www.petsmart.com/{slug}-{sku}.html"


# ─────────────────────────────────────────────────────────
# Core: Fetch category products from Algolia
# ─────────────────────────────────────────────────────────

def fetch_category(
    category_url: str,
    *,
    delay: float = 0.3,
    max_pages: int | None = None,
) -> list[dict]:
    """Fetch all products from a PetSmart category via Algolia.

    Returns list of raw Algolia hit dicts.
    """
    cat_path = resolve_category_path(category_url)
    print(f"Category: {cat_path}")

    all_hits: list[dict] = []
    page = 0

    while True:
        if max_pages is not None and page >= max_pages:
            print(f"  Reached max-pages limit ({max_pages}).")
            break

        payload = {
            "params": (
                f"hitsPerPage={HITS_PER_PAGE}"
                f"&page={page}"
                f'&filters=custom_category_names:"{cat_path}"'
            )
        }

        try:
            resp = requests.post(ALGOLIA_URL, json=payload, headers=HEADERS, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            print(f"  ERROR on page {page}: {exc}")
            break

        hits = data.get("hits", [])
        total = data.get("nbHits", 0)
        nb_pages = data.get("nbPages", 0)

        all_hits.extend(hits)
        print(f"  Page {page + 1}/{nb_pages}: {len(hits)} products (total: {total})")

        if not hits or page + 1 >= nb_pages:
            break

        page += 1
        if delay > 0:
            time.sleep(delay)

    print(f"  Fetched {len(all_hits)} products total.")
    return all_hits


# ─────────────────────────────────────────────────────────
# Output
# ─────────────────────────────────────────────────────────

def save_results(
    products: list[dict],
    output_dir: str,
    category_url: str,
    category_path: str,
    elapsed: float,
) -> None:
    """Save products and summary to output directory."""
    os.makedirs(output_dir, exist_ok=True)

    # 1. Full products JSON
    products_path = os.path.join(output_dir, "products.json")
    with open(products_path, "w", encoding="utf-8") as f:
        json.dump(products, f, indent=2, ensure_ascii=False)

    # 2. URLs text file (for PDP scraper input)
    urls = [p.get("petsmart_url") for p in products if p.get("petsmart_url")]
    urls_path = os.path.join(output_dir, "product_urls.txt")
    with open(urls_path, "w", encoding="utf-8") as f:
        f.write("\n".join(urls))

    # 3. Summary report
    brands = {}
    price_values = []
    for p in products:
        b = p.get("brand", "Unknown")
        brands[b] = brands.get(b, 0) + 1
        if p.get("price"):
            price_values.append(p["price"])

    summary = {
        "category_url": category_url,
        "category_path": category_path,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "elapsed_seconds": round(elapsed, 2),
        "total_products": len(products),
        "total_urls": len(urls),
        "price_range": {
            "min": min(price_values) if price_values else None,
            "max": max(price_values) if price_values else None,
        },
        "brands": dict(sorted(brands.items(), key=lambda x: -x[1])[:20]),
        "output_files": {
            "products": products_path,
            "urls": urls_path,
        },
    }
    summary_path = os.path.join(output_dir, "summary.json")
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)

    print(f"\nOutput saved to {output_dir}/")
    print(f"  products.json     — {len(products)} products")
    print(f"  product_urls.txt  — {len(urls)} URLs")
    print(f"  summary.json      — scrape report")


# ─────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="PetSmart Category Scraper (Algolia API)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python petsmart_scraper.py --url "https://www.petsmart.com/dog/food/dry-food"
  python petsmart_scraper.py --url "https://www.petsmart.com/cat/food/wet-food" --output output/cat_wet
  python petsmart_scraper.py --url "https://www.petsmart.com/dog/treats" --delay 1 --max-pages 3
        """,
    )
    parser.add_argument("--url", required=True, help="PetSmart category URL")
    parser.add_argument("--output", default=None, help="Output directory (default: auto-generated)")
    parser.add_argument("--delay", type=float, default=0.3, help="Delay between API pages (seconds, default: 0.3)")
    parser.add_argument("--max-pages", type=int, default=None, help="Max Algolia pages to fetch")
    parser.add_argument("--raw", action="store_true", help="Also save raw Algolia response")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    category_url = args.url.rstrip("/")
    category_path = resolve_category_path(category_url)

    # Auto-generate output dir from URL path
    if args.output:
        output_dir = args.output
    else:
        slug = urlparse(category_url).path.strip("/").replace("/", "_")
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_dir = os.path.join("output", "petsmart", f"{slug}_{timestamp}")

    print("=" * 60)
    print("PetSmart Category Scraper")
    print("=" * 60)
    print(f"URL:      {category_url}")
    print(f"Category: {category_path}")
    print(f"Output:   {output_dir}")
    print()

    start = time.time()

    # Fetch raw hits
    raw_hits = fetch_category(category_url, delay=args.delay, max_pages=args.max_pages)

    if not raw_hits:
        print("\nNo products found. Check the URL.")
        return 1

    # Transform to clean format
    print(f"\nProcessing {len(raw_hits)} products...")
    products = [transform_product(h) for h in raw_hits]

    elapsed = time.time() - start

    # Save
    save_results(products, output_dir, category_url, category_path, elapsed)

    if args.raw:
        raw_path = os.path.join(output_dir, "raw_algolia_hits.json")
        with open(raw_path, "w", encoding="utf-8") as f:
            json.dump(raw_hits, f, indent=2, ensure_ascii=False)
        print(f"  raw_algolia_hits.json — raw API response")

    print(f"\nDone in {elapsed:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
