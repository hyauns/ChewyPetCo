"""Import consolidated Chewy products into WooCommerce via the REST API.

Creates each product as a VARIABLE product + one VARIATION per variant, with
images served externally from R2 via the FIFU plugin (WooCommerce never downloads
them). Specs are written as product meta_data so a headless / Next.js frontend can
read them.

IMAGES (FIFU):
  - Parent: meta_data key `fifu_list_url` = "url1|url2|..." (1st = featured image,
    rest = product gallery).        [gallery needs FIFU PRO]
  - Variation: meta_data key `fifu_image_url` = single hosted URL.
                                     [variation image needs FIFU PRO]
  FIFU keeps these as external URLs - WordPress media library stays empty.

SPECS -> FRONTEND (important):
  Specs from metafields_plan are written as product meta_data (keys without the
  "custom." prefix, e.g. ingredients, guaranteed_analysis, pet_type, ...). These
  ARE returned by the authenticated /wc/v3 API. To also expose them on the public
  Store API your frontend reads, register each key once on the WP side, e.g.:
      register_post_meta('product', 'ingredients', ['show_in_rest'=>true, 'single'=>true, 'type'=>'string']);
  (see --print-meta-keys to list every key this importer emits).

ENV (.env):
  WC_STORE_URL=https://yourstore.com
  WC_CONSUMER_KEY=ck_xxx
  WC_CONSUMER_SECRET=cs_xxx

Requires: pip install requests

Usage:
  python tools/import_woocommerce_api.py --print-meta-keys     # list spec keys
  python tools/import_woocommerce_api.py --limit 5 --dry-run    # show payload, no API
  python tools/import_woocommerce_api.py --limit 5 --status draft
  python tools/import_woocommerce_api.py --status publish --workers 6
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass

import requests  # noqa: E402
from tools.build_shopify_csv import clean_price, md_to_html, safe_str  # noqa: E402


def load_env(path: str) -> dict:
    out = {}
    if not os.path.exists(path):
        return out
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


# Canonical spec meta keys (consistent across all products so the frontend never
# needs to re-normalize). Maps every raw metafields_plan key variant -> one key.
KEY_MAP = {
    "ingredients_json": "ingredients",
    "guaranteed_analysis_json": "guaranteed_analysis",
    "feeding_instructions_json": "feeding_instructions",
    "nutrition_json": "nutrition",
    "specifications_json": "specifications",
    "product-form": "food_form",
    "food_form": "food_form",
    "lifestage": "life_stage",
    "life_stage": "life_stage",
    "breed-size": "breed_size",
    "breed_size": "breed_size",
    "pet-weight": "pet_weight",
    "primary_flavor": "flavor",
    "flavor": "flavor",
    "pet_type": "pet_type",
    "special_diet": "special_diet",
    "package_type": "package_type",
    "color": "color",
    "scent": "scent",
    "source_url": "source_url",
    "source_product_id": "source_product_id",
}
# Stable list of canonical keys the mu-plugin should expose (+ image/brand below).
SPEC_CANON = sorted(set(KEY_MAP.values()))


def spec_meta(p: dict) -> list[dict]:
    """metafields_plan -> meta_data with canonical keys. JSON values are dumped to
    clean JSON strings so the frontend can JSON.parse them directly."""
    mp = p.get("metafields_plan") or {}
    collected: dict[str, object] = {}
    for k, v in mp.items():
        if v in (None, ""):
            continue
        if isinstance(v, (list, dict)) and not v:   # drop empty [] / {}
            continue
        raw = k.split(".", 1)[1] if "." in k else k
        key = KEY_MAP.get(raw, raw)
        val = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v
        if key not in collected:                     # first non-empty wins on collision
            collected[key] = val
    return [{"key": kk, "value": vv} for kk, vv in collected.items()]


def precompute_skus(products: list) -> list[str]:
    """Deterministic unique parent SKU per product (handle_slug, de-collided)."""
    used: set[str] = set()
    skus = []
    for i, p in enumerate(products):
        base = p.get("handle_slug") or f"chewy-{i}"
        sku = base
        n = 2
        while sku in used:
            sku = f"{base}-{n}"; n += 1
        used.add(sku)
        skus.append(sku)
    return skus


class WC:
    def __init__(self, base: str, ck: str, cs: str):
        self.base = base.rstrip("/") + "/wp-json/wc/v3"
        self.auth = (ck, cs)
        self.s = requests.Session()

    def post(self, path: str, payload: dict) -> dict:
        r = self.s.post(f"{self.base}{path}", auth=self.auth, json=payload, timeout=60)
        if r.status_code >= 400:
            raise RuntimeError(f"{r.status_code} {path}: {r.text[:300]}")
        return r.json()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", default=os.path.join(_BASE, "output", "chewy_all_products.json"))
    ap.add_argument("--map", default=os.path.join(_BASE, "output", "image_url_map.json"))
    ap.add_argument("--state", default=os.path.join(_BASE, "output", "wc_import_state.json"))
    ap.add_argument("--failures", default=os.path.join(_BASE, "output", "wc_import_failures.json"))
    ap.add_argument("--status", choices=["draft", "publish"], default="draft")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--print-meta-keys", action="store_true")
    args = ap.parse_args()

    products = json.load(open(args.inp, encoding="utf-8")).get("products", [])
    if args.limit:
        products = products[:args.limit]

    if args.print_meta_keys:
        keys = set()
        for p in products:
            keys.update(m["key"] for m in spec_meta(p))
        keys.update({"fifu_list_url", "fifu_image_alt", "brand"})
        print("Meta keys emitted (register these with show_in_rest for frontend):")
        for k in sorted(keys):
            print(" ", k)
        return

    img_map = json.load(open(args.map, encoding="utf-8")) if os.path.exists(args.map) else {}
    if not img_map:
        print("WARNING: no image_url_map.json -> images will use original Chewy URLs")

    def host(u: str) -> str:
        return img_map.get(u, u) if u else ""

    skus = precompute_skus(products)

    if args.dry_run:
        for p, sku in list(zip(products, skus))[:args.limit or 5]:
            variants = p.get("variants") or []
            imgs = [host(u) for u in (p.get("images") or []) if u]
            print(f"\n=== {sku} | {p.get('title','')[:60]} | {len(variants)} variations")
            print("  categories:", p.get("category_path"))
            print("  fifu_list_url:", (imgs[0] if imgs else "")[:80], f"(+{max(0,len(imgs)-1)} gallery)")
            print("  attribute:", variants[0].get("option1_name"), "=",
                  [v.get("option1_value") for v in variants][:6])
            print("  specs meta keys:", [m["key"] for m in spec_meta(p)])
        print("\n[dry-run] nothing sent to WooCommerce.")
        return

    env = load_env(os.path.join(_BASE, ".env"))
    base = os.environ.get("WC_STORE_URL") or env.get("WC_STORE_URL")
    ck = os.environ.get("WC_CONSUMER_KEY") or env.get("WC_CONSUMER_KEY")
    cs = os.environ.get("WC_CONSUMER_SECRET") or env.get("WC_CONSUMER_SECRET")
    if not all([base, ck, cs]):
        sys.exit("ERROR: missing WC_STORE_URL / WC_CONSUMER_KEY / WC_CONSUMER_SECRET in .env")
    wc = WC(base, ck, cs)

    state = json.load(open(args.state, encoding="utf-8")) if os.path.exists(args.state) else {}
    failures = json.load(open(args.failures, encoding="utf-8")) if os.path.exists(args.failures) else {}
    print(f"Importing {len(products):,} products (status={args.status}). Resume: {len(state):,} done.")

    lock = threading.Lock()
    cat_cache: dict[tuple, int] = {}
    counters = {"ok": 0, "skip": 0, "fail": 0}

    def ensure_category(path: list) -> int | None:
        """Create category hierarchy, return leaf id. Cached + locked."""
        if not path:
            return None
        key = tuple(path)
        with lock:
            if key in cat_cache:
                return cat_cache[key]
        parent_id = None
        acc: list = []
        for name in path:
            acc.append(name)
            ck_t = tuple(acc)
            with lock:
                cached = cat_cache.get(ck_t)
            if cached:
                parent_id = cached
                continue
            payload = {"name": name}
            if parent_id:
                payload["parent"] = parent_id
            try:
                res = wc.post("/products/categories", payload)
                cid = res["id"]
            except RuntimeError as e:
                # already exists (race) -> WooCommerce returns the existing term id
                # as resource_id in the error body; use it directly.
                cid = None
                msg = str(e)
                if "term_exists" in msg:
                    import re
                    m = re.search(r'"resource_id":\s*(\d+)', msg)
                    if m:
                        cid = int(m.group(1))
                    else:
                        r = wc.s.get(f"{wc.base}/products/categories", auth=wc.auth,
                                     params={"search": name, "parent": parent_id or 0}, timeout=60)
                        for c in r.json():
                            if c.get("name", "").lower() == name.lower():
                                cid = c["id"]; break
                if not cid:
                    raise
            with lock:
                cat_cache[ck_t] = cid
            parent_id = cid
        return parent_id

    def import_one(idx: int) -> None:
        p = products[idx]
        sku = skus[idx]
        if sku in state:
            with lock:
                counters["skip"] += 1
            return
        try:
            variants = p.get("variants") or []
            if not variants:
                with lock:
                    counters["skip"] += 1
                return
            a1 = next((v.get("option1_name") for v in variants if v.get("option1_name")), "Size")
            a2 = next((v.get("option2_name") for v in variants if v.get("option2_name")), None)
            a1_opts, a2_opts = [], []
            for v in variants:
                o1, o2 = safe_str(v.get("option1_value")), safe_str(v.get("option2_value"))
                if o1 and o1 not in a1_opts:
                    a1_opts.append(o1)
                if o2 and o2 not in a2_opts:
                    a2_opts.append(o2)

            attrs = [{"name": a1, "variation": True, "visible": True, "options": a1_opts}]
            if a2 and a2_opts:
                attrs.append({"name": a2, "variation": True, "visible": True, "options": a2_opts})

            imgs = [host(u) for u in (p.get("images") or []) if u]
            meta = spec_meta(p)
            if imgs:
                meta.append({"key": "fifu_list_url", "value": "|".join(imgs)})
                meta.append({"key": "fifu_image_alt", "value": safe_str(p.get("title"))})
            if p.get("brand"):
                meta.append({"key": "brand", "value": safe_str(p.get("brand"))})

            payload = {
                "name": safe_str(p.get("title")),
                "type": "variable",
                "status": args.status,
                "sku": sku,
                "description": md_to_html(p.get("description")),
                "attributes": attrs,
                "meta_data": meta,
            }
            cat_path = p.get("category_path")
            if isinstance(cat_path, list) and cat_path:
                leaf = ensure_category(cat_path)
                if leaf:
                    payload["categories"] = [{"id": leaf}]

            created = wc.post("/products", payload)
            pid = created["id"]

            # batch-create variations
            var_payload = {"create": []}
            for v in variants:
                vimg = host((v.get("images") or [None])[0]) if v.get("images") else ""
                vattrs = [{"name": a1, "option": safe_str(v.get("option1_value"))}]
                if a2:
                    vattrs.append({"name": a2, "option": safe_str(v.get("option2_value"))})
                vmeta = []
                if vimg:
                    vmeta.append({"key": "fifu_image_url", "value": vimg})
                var_payload["create"].append({
                    "sku": safe_str(v.get("sku")),
                    "regular_price": clean_price(v.get("price")),
                    "manage_stock": False,
                    "stock_status": "instock" if v.get("in_stock") else "outofstock",
                    "attributes": vattrs,
                    "meta_data": vmeta,
                })
            if var_payload["create"]:
                wc.post(f"/products/{pid}/variations/batch", var_payload)

            with lock:
                state[sku] = pid
                counters["ok"] += 1
                failures.pop(sku, None)
        except Exception as e:
            with lock:
                failures[sku] = str(e)
                counters["fail"] += 1

    processed = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(import_one, i): i for i in range(len(products))}
        for _ in as_completed(futs):
            processed += 1
            if processed % 50 == 0:
                with lock:
                    print(f"  {processed:,}/{len(products):,}  ok={counters['ok']:,} "
                          f"skip={counters['skip']:,} fail={counters['fail']:,}")
                    json.dump(state, open(args.state, "w"))
                    json.dump(failures, open(args.failures, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    json.dump(state, open(args.state, "w"))
    json.dump(failures, open(args.failures, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\nDONE  ok={counters['ok']:,}  skip={counters['skip']:,}  fail={counters['fail']:,}")
    print(f"  state -> {args.state}")
    if failures:
        print(f"  failures -> {args.failures} ({len(failures):,}) - re-run to retry")


if __name__ == "__main__":
    main()
