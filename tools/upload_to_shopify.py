"""Upload Shopify products from a Matrixify-style CSV via Admin REST API.

Reads `output/shopify_export_*/shopify_products.csv` (the canonical export),
groups rows by Handle (1 product per handle, many variant/image rows), then:
  1. POST /admin/api/2024-10/products.json — create product with all variants
     and images in one shot (Shopify supports this).
  2. POST /admin/api/2024-10/products/{id}/metafields.json — attach each
     metafield (Shopify ignores metafields in the product create payload,
     so this is a separate per-product loop).

Resumable: tracks completed handles in a state file
(`<csv_dir>/shopify_upload_state.json`). Re-running skips them.

ENV vars (read from .env via tools/config_dotenv.py or process env):
  SHOPIFY_STORE_DOMAIN=yourstore.myshopify.com
  SHOPIFY_ADMIN_TOKEN=shpat_xxxxxxxxxxxxxxxx

Token scopes required (Custom App in Shopify Admin → Apps → Develop apps):
  write_products, read_products

Usage (start with dry-run, then small live batch):
  python tools/upload_to_shopify.py \
    --csv output/shopify_export_cat_multispecies/shopify_products.csv \
    --dry-run --limit 5

  python tools/upload_to_shopify.py \
    --csv output/shopify_export_cat_multispecies/shopify_products.csv \
    --limit 10

  python tools/upload_to_shopify.py \
    --csv output/shopify_export_cat_multispecies/shopify_products.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

import requests

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except AttributeError:
        pass

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

API_VERSION = "2024-10"
RATE_LIMIT_BACKOFF = 2.0  # seconds — bumped on 429
INITIAL_SLEEP = 0.5       # seconds between calls (~2 req/s base, REST limit)


def load_env_file(path: Path) -> dict:
    """Tiny .env loader — only KEY=VALUE lines, no quoting tricks."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def shop_auth() -> tuple[str, str]:
    env = load_env_file(BASE_DIR / ".env")
    domain = os.environ.get("SHOPIFY_STORE_DOMAIN") or env.get("SHOPIFY_STORE_DOMAIN")
    token = os.environ.get("SHOPIFY_ADMIN_TOKEN") or env.get("SHOPIFY_ADMIN_TOKEN")
    if not domain or not token:
        sys.exit("ERROR: Missing SHOPIFY_STORE_DOMAIN or SHOPIFY_ADMIN_TOKEN in .env or environment.")
    return domain, token


def shopify_request(method: str, url: str, token: str, **kwargs) -> requests.Response:
    """Wrap requests with auth, JSON content-type, and 429 backoff."""
    headers = kwargs.pop("headers", {})
    headers["X-Shopify-Access-Token"] = token
    headers["Content-Type"] = "application/json"
    headers["Accept"] = "application/json"

    sleep = INITIAL_SLEEP
    for attempt in range(6):
        resp = requests.request(method, url, headers=headers, timeout=60, **kwargs)
        if resp.status_code == 429:
            retry = float(resp.headers.get("Retry-After", RATE_LIMIT_BACKOFF))
            time.sleep(retry)
            continue
        if resp.status_code >= 500:
            time.sleep(sleep)
            sleep *= 2
            continue
        return resp
    return resp


def test_auth(domain: str, token: str) -> dict:
    url = f"https://{domain}/admin/api/{API_VERSION}/shop.json"
    r = shopify_request("GET", url, token)
    if r.status_code != 200:
        sys.exit(f"ERROR: Auth test failed ({r.status_code}): {r.text[:300]}")
    return r.json().get("shop", {})


# ---------- CSV → payload mapping ----------

PRODUCT_COLS = ["Title", "Body (HTML)", "Vendor", "Type", "Tags", "Published", "Status",
                "Product Category", "SEO Title", "SEO Description", "Gift Card"]
VARIANT_COLS = ["Option1 Name", "Option1 Value", "Variant SKU", "Variant Grams",
                "Variant Inventory Tracker", "Variant Inventory Qty",
                "Variant Inventory Policy", "Variant Fulfillment Service",
                "Variant Price", "Variant Compare At Price", "Variant Requires Shipping",
                "Variant Taxable", "Variant Barcode", "Variant Weight Unit",
                "Variant Tax Code", "Cost per item"]


def group_rows_by_handle(csv_path: Path) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = defaultdict(list)
    with open(csv_path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            h = (row.get("Handle") or "").strip()
            if not h:
                continue
            groups[h].append(row)
    return groups


def parse_metafield_col(col: str) -> tuple[str, str, str] | None:
    """'Metafield: custom.foo_json [json]' → ('custom', 'foo_json', 'json')."""
    if not col.startswith("Metafield:"):
        return None
    rest = col[len("Metafield:"):].strip()
    if "[" not in rest or "]" not in rest:
        return None
    name, _, rest2 = rest.partition("[")
    name = name.strip()
    mf_type = rest2.rstrip("]").strip()
    if "." not in name:
        return None
    namespace, _, key = name.partition(".")
    return (namespace.strip(), key.strip(), mf_type)


def build_product_payload(handle: str, rows: list[dict]) -> dict:
    """Build Shopify REST product create payload from CSV rows for one handle."""
    head = rows[0]

    # Variants: rows where Variant SKU is non-empty (or has Option1 Value)
    variants = []
    seen_skus = set()
    for r in rows:
        sku = (r.get("Variant SKU") or "").strip()
        opt1 = (r.get("Option1 Value") or "").strip()
        # First row of each handle always has variant info; later rows that have either SKU or option add new variants.
        # Skip image-only rows (no SKU + no option).
        if not sku and not opt1:
            continue
        key = sku or opt1
        if key in seen_skus:
            continue
        seen_skus.add(key)
        v: dict = {}
        if opt1:
            v["option1"] = opt1
        if sku:
            v["sku"] = sku
        price = (r.get("Variant Price") or "").strip()
        if price:
            v["price"] = price
        cap = (r.get("Variant Compare At Price") or "").strip()
        if cap:
            v["compare_at_price"] = cap
        bar = (r.get("Variant Barcode") or "").strip()
        if bar:
            v["barcode"] = bar
        grams = (r.get("Variant Grams") or "").strip()
        if grams:
            try:
                v["grams"] = int(float(grams))
            except ValueError:
                pass
        v["inventory_management"] = (r.get("Variant Inventory Tracker") or "").strip() or None
        v["inventory_policy"] = (r.get("Variant Inventory Policy") or "deny").strip() or "deny"
        try:
            v["inventory_quantity"] = int(r.get("Variant Inventory Qty") or 0)
        except (ValueError, TypeError):
            v["inventory_quantity"] = 0
        req_ship = (r.get("Variant Requires Shipping") or "TRUE").strip().upper()
        v["requires_shipping"] = req_ship != "FALSE"
        taxable = (r.get("Variant Taxable") or "TRUE").strip().upper()
        v["taxable"] = taxable != "FALSE"
        weight_unit = (r.get("Variant Weight Unit") or "").strip()
        if weight_unit:
            v["weight_unit"] = weight_unit
        cost = (r.get("Cost per item") or "").strip()
        if cost:
            v["cost"] = cost
        variants.append(v)

    # Images: collect Image Src from any row (dedupe by URL, preserve order by Image Position)
    images: list[dict] = []
    seen_imgs = set()
    img_rows = [r for r in rows if (r.get("Image Src") or "").strip()]
    img_rows.sort(key=lambda r: int(r.get("Image Position") or 999))
    for r in img_rows:
        src = r.get("Image Src", "").strip()
        if src in seen_imgs:
            continue
        seen_imgs.add(src)
        img: dict = {"src": src}
        alt = (r.get("Image Alt Text") or "").strip()
        if alt:
            img["alt"] = alt
        images.append(img)

    # Option1
    option_name = (head.get("Option1 Name") or "").strip()
    options = [{"name": option_name}] if option_name else []

    # Status
    status_csv = (head.get("Status") or "draft").strip().lower()
    if status_csv not in ("active", "draft", "archived"):
        status_csv = "draft"

    payload = {
        "title": head.get("Title", "").strip(),
        "body_html": head.get("Body (HTML)", "") or "",
        "vendor": head.get("Vendor", "").strip(),
        "product_type": head.get("Type", "").strip(),
        "tags": head.get("Tags", "").strip(),
        "handle": handle,
        "status": status_csv,
        "published": (head.get("Published") or "FALSE").strip().upper() != "FALSE",
    }
    if options:
        payload["options"] = options
    if variants:
        payload["variants"] = variants
    if images:
        payload["images"] = images
    seo_title = (head.get("SEO Title") or "").strip()
    seo_desc = (head.get("SEO Description") or "").strip()
    if seo_title:
        payload["metafields_global_title_tag"] = seo_title
    if seo_desc:
        payload["metafields_global_description_tag"] = seo_desc

    return {"product": payload}


def build_metafields(rows: list[dict], col_meta: list[tuple[str, str, str, str]]) -> list[dict]:
    """col_meta = [(csv_col, namespace, key, type), ...]. Read from row[0]."""
    head = rows[0]
    out = []
    for col, ns, key, mf_type in col_meta:
        val = (head.get(col) or "").strip()
        if not val:
            continue
        out.append({
            "namespace": ns,
            "key": key,
            "type": mf_type,
            "value": val,
        })
    return out


# ---------- State (resume) ----------

def load_state(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"completed_handles": [], "failed_handles": [], "stats": {}}


def save_state(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------- Main upload loop ----------

def upload_one(handle: str, rows: list[dict], col_meta, domain, token, dry_run: bool, verbose: bool):
    product_payload = build_product_payload(handle, rows)
    metafields = build_metafields(rows, col_meta)
    title = product_payload["product"]["title"]
    n_variants = len(product_payload["product"].get("variants", []))
    n_images = len(product_payload["product"].get("images", []))
    n_mfs = len(metafields)

    if dry_run:
        print(f"  [DRY] {handle:<60} title={title[:50]!r} variants={n_variants} images={n_images} metafields={n_mfs}")
        return {"success": True, "product_id": None, "dry": True}

    # 1. Create product
    url = f"https://{domain}/admin/api/{API_VERSION}/products.json"
    r = shopify_request("POST", url, token, json=product_payload)
    if r.status_code not in (200, 201):
        return {"success": False, "step": "product", "status": r.status_code, "body": r.text[:500]}
    product = r.json().get("product", {})
    pid = product.get("id")
    if verbose:
        print(f"  ✅ product id={pid}  variants={len(product.get('variants',[]))}")

    # 2. Attach metafields one-by-one
    mf_errors = []
    for mf in metafields:
        mf_url = f"https://{domain}/admin/api/{API_VERSION}/products/{pid}/metafields.json"
        mr = shopify_request("POST", mf_url, token, json={"metafield": mf})
        if mr.status_code not in (200, 201):
            mf_errors.append({"key": f'{mf["namespace"]}.{mf["key"]}', "status": mr.status_code, "body": mr.text[:200]})
        time.sleep(INITIAL_SLEEP)

    return {"success": True, "product_id": pid, "metafields_attached": len(metafields) - len(mf_errors), "metafield_errors": mf_errors}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True, help="Path to shopify_products.csv")
    ap.add_argument("--dry-run", action="store_true", help="Parse + show summary, do not call API")
    ap.add_argument("--limit", type=int, default=None, help="Only process the first N unique handles (test mode)")
    ap.add_argument("--start-from", type=int, default=0, help="Skip the first N handles (resume / chunked runs)")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    csv_path = Path(args.csv).resolve()
    if not csv_path.exists():
        sys.exit(f"ERROR: CSV not found at {csv_path}")

    if args.dry_run:
        domain, token = "", ""
        print("DRY-RUN — skipping ENV check and auth test.")
    else:
        domain, token = shop_auth()
        shop = test_auth(domain, token)
        print(f"✅ Auth OK — shop: {shop.get('name')!r} ({shop.get('myshopify_domain')})  plan: {shop.get('plan_name')}")
        print()

    # Group CSV rows by Handle
    groups = group_rows_by_handle(csv_path)
    handles = list(groups.keys())  # CSV order preserved by dict insertion
    print(f"Loaded {sum(len(v) for v in groups.values())} CSV rows → {len(handles)} unique handles")

    # Discover metafield columns once
    with open(csv_path, "r", encoding="utf-8", newline="") as f:
        first_cols = next(csv.reader(f))
    col_meta = []
    for col in first_cols:
        parsed = parse_metafield_col(col)
        if parsed:
            ns, key, mf_type = parsed
            col_meta.append((col, ns, key, mf_type))
    print(f"Found {len(col_meta)} metafield columns")

    # State file
    state_path = csv_path.parent / "shopify_upload_state.json"
    state = load_state(state_path)
    done = set(state.get("completed_handles", []))
    print(f"State file: {state_path} — {len(done)} already completed")
    print()

    # Filter to unfinished + apply --start-from + --limit
    pending = [h for h in handles if h not in done]
    if args.start_from:
        pending = pending[args.start_from:]
    if args.limit is not None:
        pending = pending[:args.limit]
    print(f"Pending in this run: {len(pending)} handles")
    if args.dry_run:
        print(f"DRY-RUN MODE — no API calls\n")

    success_count = 0
    fail_count = 0
    for i, h in enumerate(pending, start=1):
        prefix = f"[{i}/{len(pending)}]"
        try:
            res = upload_one(h, groups[h], col_meta, domain, token, args.dry_run, args.verbose)
        except Exception as e:
            res = {"success": False, "step": "exception", "error": str(e)}

        if res.get("success"):
            success_count += 1
            if not args.dry_run:
                done.add(h)
                state["completed_handles"] = sorted(done)
                if res.get("metafield_errors"):
                    state.setdefault("metafield_warnings", []).append({"handle": h, "errors": res["metafield_errors"]})
                if i % 5 == 0:
                    save_state(state_path, state)
                print(f"{prefix} ✅ {h}  (pid={res.get('product_id')}, mfs={res.get('metafields_attached')})")
        else:
            fail_count += 1
            state.setdefault("failed_handles", []).append({"handle": h, "result": res})
            save_state(state_path, state)
            print(f"{prefix} ❌ {h}  step={res.get('step')}  status={res.get('status')}")
            print(f"     body: {(res.get('body') or res.get('error') or '')[:200]}")

        if not args.dry_run:
            time.sleep(INITIAL_SLEEP)

    if not args.dry_run:
        state["stats"] = {
            "last_run_ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "success_this_run": success_count,
            "fail_this_run": fail_count,
            "total_completed": len(done),
        }
        save_state(state_path, state)

    print()
    print(f"=== Done ===")
    print(f"  Success: {success_count}")
    print(f"  Failed:  {fail_count}")
    print(f"  Total completed (across runs): {len(done)}")
    if fail_count:
        print(f"  Failed handles logged in: {state_path}")


if __name__ == "__main__":
    main()
