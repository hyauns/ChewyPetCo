"""Upload all Chewy product images to Cloudflare R2 (or any S3-compatible store).

Reads output/chewy_all_products.json, collects every unique image URL (from both
product.images[] and variant.images[]), downloads each from Chewy's CDN, and
uploads it to the bucket under a DETERMINISTIC key derived from the original URL:

    https://image.chewy.com/catalog/general/images/<slug>/img-437708.jpg
    -> key:  chewy/catalog/general/images/<slug>/img-437708.jpg
    -> URL:  {R2_PUBLIC_BASE_URL}/chewy/catalog/general/images/<slug>/img-437708.jpg

Because the key is derived from the source URL, the run is idempotent (re-runs
skip done files), images shared by many products upload only once, and the
mapping back to each product is exact (1:1 by URL).

Output: output/image_url_map.json   { chewy_url -> hosted_url }   (feeds the
WooCommerce CSV builder). Failures -> output/r2_upload_failures.json.

R2 is S3-compatible. Set these in .env (or env vars):
    R2_ACCOUNT_ID=xxxxxxxxxxxxxxxx
    R2_ACCESS_KEY_ID=...
    R2_SECRET_ACCESS_KEY=...
    R2_BUCKET=chewy-images
    R2_PUBLIC_BASE_URL=https://img.yourstore.com   (custom domain or *.r2.dev)

Requires: pip install boto3 requests

Usage:
  python tools/upload_images_r2.py --dry-run          # count images, write nothing
  python tools/upload_images_r2.py --limit 100        # test 100 images live
  python tools/upload_images_r2.py --workers 16       # full run
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlsplit

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass

import requests  # noqa: E402

CONTENT_TYPES = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".gif": "image/gif",
}


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


def url_to_key(url: str) -> str:
    """https://image.chewy.com/a/b/img.jpg -> chewy/a/b/img.jpg"""
    path = urlsplit(url).path.lstrip("/")
    return f"chewy/{path}"


def collect_urls(products: list) -> list[str]:
    seen = set()
    ordered = []
    for p in products:
        for u in (p.get("images") or []):
            if u and u not in seen:
                seen.add(u); ordered.append(u)
        for v in (p.get("variants") or []):
            for u in (v.get("images") or []):
                if u and u not in seen:
                    seen.add(u); ordered.append(u)
    return ordered


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", default=os.path.join(_BASE, "output", "chewy_all_products.json"))
    ap.add_argument("--map", default=os.path.join(_BASE, "output", "image_url_map.json"))
    ap.add_argument("--state", default=os.path.join(_BASE, "output", "r2_upload_state.json"))
    ap.add_argument("--failures", default=os.path.join(_BASE, "output", "r2_upload_failures.json"))
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-existing", action="store_true",
                    help="HEAD the bucket and skip objects already there (slower, safest)")
    args = ap.parse_args()

    products = json.load(open(args.inp, encoding="utf-8")).get("products", [])
    urls = collect_urls(products)
    if args.limit:
        urls = urls[:args.limit]

    public_base = ""
    env = load_env(os.path.join(_BASE, ".env"))
    public_base = (os.environ.get("R2_PUBLIC_BASE_URL") or env.get("R2_PUBLIC_BASE_URL") or "").rstrip("/")
    if public_base and not public_base.startswith(("http://", "https://")):
        public_base = "https://" + public_base

    print(f"Unique image URLs: {len(urls):,}")
    print(f"Sample key: {url_to_key(urls[0])}" if urls else "(no images)")
    if public_base:
        print(f"Sample hosted URL: {public_base}/{url_to_key(urls[0])}" if urls else "")

    if args.dry_run:
        print("\n[dry-run] nothing uploaded. (Set R2_* in .env then run without --dry-run.)")
        return

    # ---- real upload path needs boto3 + creds ----
    try:
        import boto3
        from botocore.config import Config
    except ImportError:
        sys.exit("ERROR: pip install boto3")

    acct = os.environ.get("R2_ACCOUNT_ID") or env.get("R2_ACCOUNT_ID")
    akid = os.environ.get("R2_ACCESS_KEY_ID") or env.get("R2_ACCESS_KEY_ID")
    secret = os.environ.get("R2_SECRET_ACCESS_KEY") or env.get("R2_SECRET_ACCESS_KEY")
    bucket = os.environ.get("R2_BUCKET") or env.get("R2_BUCKET")
    if not all([acct, akid, secret, bucket, public_base]):
        sys.exit("ERROR: missing R2_ACCOUNT_ID / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY / "
                 "R2_BUCKET / R2_PUBLIC_BASE_URL in .env")

    endpoint = f"https://{acct}.r2.cloudflarestorage.com"
    s3 = boto3.client(
        "s3", endpoint_url=endpoint, aws_access_key_id=akid,
        aws_secret_access_key=secret, region_name="auto",
        config=Config(retries={"max_attempts": 3, "mode": "standard"}),
    )

    done = set()
    if os.path.exists(args.state):
        done = set(json.load(open(args.state, encoding="utf-8")))
    print(f"Resume: {len(done):,} already done")

    lock = threading.Lock()
    url_map: dict[str, str] = {}
    if os.path.exists(args.map):
        url_map = json.load(open(args.map, encoding="utf-8"))
    failures: dict[str, str] = {}
    sess = requests.Session()
    sess.headers.update({
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
        "Referer": "https://www.chewy.com/",
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    counters = {"ok": 0, "skip": 0, "fail": 0}

    def hosted(key: str) -> str:
        return f"{public_base}/{key}"

    def worker(url: str) -> None:
        key = url_to_key(url)
        if url in done:
            with lock:
                url_map[url] = hosted(key); counters["skip"] += 1
            return
        try:
            if args.skip_existing:
                try:
                    s3.head_object(Bucket=bucket, Key=key)
                    with lock:
                        url_map[url] = hosted(key); done.add(url); counters["skip"] += 1
                    return
                except Exception:
                    pass
            r = None
            for attempt in range(4):
                r = sess.get(url, timeout=30)
                if r.status_code in (403, 429, 500, 502, 503):
                    import time
                    time.sleep(1.5 * (attempt + 1))
                    continue
                break
            r.raise_for_status()
            ext = os.path.splitext(urlsplit(url).path)[1].lower()
            ctype = CONTENT_TYPES.get(ext, "image/jpeg")
            s3.put_object(Bucket=bucket, Key=key, Body=r.content,
                          ContentType=ctype, CacheControl="public, max-age=31536000")
            with lock:
                url_map[url] = hosted(key); done.add(url); counters["ok"] += 1
        except Exception as e:
            with lock:
                failures[url] = str(e); counters["fail"] += 1

    todo = [u for u in urls]
    print(f"Uploading {len(todo):,} images with {args.workers} workers...\n")
    processed = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(worker, u): u for u in todo}
        for _ in as_completed(futs):
            processed += 1
            if processed % 1000 == 0:
                with lock:
                    print(f"  {processed:,}/{len(todo):,}  ok={counters['ok']:,} "
                          f"skip={counters['skip']:,} fail={counters['fail']:,}")
                    json.dump(sorted(done), open(args.state, "w"))
                    json.dump(url_map, open(args.map, "w", encoding="utf-8"), ensure_ascii=False)

    json.dump(sorted(done), open(args.state, "w"))
    json.dump(url_map, open(args.map, "w", encoding="utf-8"), ensure_ascii=False)
    if failures:
        json.dump(failures, open(args.failures, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    print(f"\nDONE  ok={counters['ok']:,}  skip={counters['skip']:,}  fail={counters['fail']:,}")
    print(f"  image map -> {args.map} ({len(url_map):,} URLs)")
    if failures:
        print(f"  failures  -> {args.failures} ({len(failures):,}) - re-run to retry")


if __name__ == "__main__":
    main()
