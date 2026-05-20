"""One-step Chewy category discovery + multi-worker PDP scrape.

Replaces the 5-step `category_job_runner` workflow with a single command:

    python tools/scrape_category.py "https://www.chewy.com/b/dry-food-294" --workers 3

Pipeline (in-process, no category DB tables):
  1. Open AdsPower browser via ADSPOWER_PROFILE_ID
  2. Crawl category pages 1..N, extract product cards
  3. Filter sponsored + by price (--price-min/--price-max)
  4. Dedupe against output/normalized_products/chewy_<pid>.json (filesystem)
  5. Write `output/category_urls/<ts>_<slug>.urls.txt` (one URL/line)
  6. Write `output/category_urls/<ts>_<slug>.summary.json` (audit)
  7. Hand off URL file to `resumable_scraper_runner create` + `start --workers N`

Use --dry-run to stop after step 6 (review URLs before scraping).

The DB-backed scrape phase (step 7) still uses scrape_jobs/scrape_job_items
because that's what gives us 3-worker atomic claim + resume — those tables
ARE useful. The category-side DB tables are skipped entirely.
"""
from __future__ import annotations

import argparse
import asyncio
import glob
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)

from playwright.async_api import async_playwright  # noqa: E402

import adspower  # noqa: E402
import adsp_profile_pool_manager  # noqa: E402
import adsp_profile_recovery_manager as recovery  # noqa: E402
import config  # noqa: E402
import category_price_filter  # noqa: E402
from category_discovery import extract_product_cards  # noqa: E402

URL_DIR = os.path.join("output", "category_urls")
NORMALIZED_DIR = os.path.join("output", "normalized_products")
# Reuse slot CW_1 infrastructure for discovery: auto-rebuild on missing profile,
# auto-rebuild on white-screen, all via the .env proxy already configured for CW_1.
DISCOVERY_SLOT = "CW_1"
MAX_WHITE_SCREEN_REBUILDS_PER_PAGE = 3


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-") or "category"


def extract_pid(url: str) -> str | None:
    m = re.search(r"/dp/(\d+)", url or "")
    return m.group(1) if m else None


def load_existing_pids() -> set[str]:
    """Return pids already scraped (chewy_<pid>.json exists)."""
    pids: set[str] = set()
    for f in glob.glob(os.path.join(NORMALIZED_DIR, "chewy_*.json")):
        m = re.search(r"chewy_(\d+)\.json$", f)
        if m:
            pids.add(m.group(1))
    return pids


def _get_slot_profile(slot_id: str) -> str | None:
    row = recovery.get_template(slot_id)
    return str(row["adspower_profile_id"]) if row and row.get("adspower_profile_id") else None


async def _start_browser_with_rebuild(p, slot_id: str, current_pid: str | None):
    """Start AdsPower browser with auto-rebuild on 'profile does not exist'.

    Mirrors parallel_resumable_runner._start_worker_browser. Returns
    (browser, context, working_profile_id). Raises RuntimeError if even the
    rebuilt profile cannot be started.
    """
    pid = current_pid
    if not pid:
        ensure = recovery.ensure_slot_profile(slot_id, delay_seconds=0)
        if not ensure.get("success"):
            raise RuntimeError(f"Slot {slot_id} unavailable: {ensure.get('message')}")
        pid = _get_slot_profile(slot_id)
        if not pid:
            raise RuntimeError(f"Slot {slot_id} has no profile id after ensure_slot_profile.")

    for attempt in range(2):  # original + 1 rebuild retry
        try:
            profile_data = adspower.start_profile(pid)
            ws_url = adspower.get_ws_endpoint(profile_data)
            browser = await p.chromium.connect_over_cdp(ws_url)
            ctx = browser.contexts[0] if browser.contexts else (await browser.new_context())
            print(f"[crawl] AdsPower CDP connected  (profile {pid})")
            return browser, ctx, pid
        except Exception as exc:
            err = str(exc).lower()
            if attempt == 0 and ("does not exist" in err or "not exist" in err):
                print(f"[crawl] Profile {pid} missing on AdsPower — rebuilding via slot {slot_id} (.env proxy)...")
                rebuild = recovery.auto_rebuild_profile(
                    slot_id,
                    reason=f"profile_missing_{pid}",
                    delay_seconds=0,
                    delete_old_profile=False,
                )
                if not rebuild.get("success"):
                    raise RuntimeError(f"Profile rebuild failed: {rebuild.get('message')}")
                pid = rebuild.get("new_profile_id") or _get_slot_profile(slot_id)
                if not pid:
                    raise RuntimeError("Rebuild reported success but no new profile id available.")
                print(f"[crawl] Rebuild OK — new profile {pid}. Retrying start...")
                continue
            raise
    raise RuntimeError("Unreachable")


async def _close_browser_safely(browser, ctx, pid: str | None):
    for action in (
        lambda: ctx.close() if ctx else None,
        lambda: browser.close() if browser else None,
    ):
        try:
            res = action()
            if res is not None and hasattr(res, "__await__"):
                await res
        except Exception:
            pass
    if pid:
        try:
            adspower.stop_profile(pid)
        except Exception:
            pass


async def crawl_category(
    category_url: str,
    max_pages: int | None,
    price_min: float | None,
    price_max: float | None,
    delay_seconds: float,
    profile_id: str,
    slot_id: str = DISCOVERY_SLOT,
) -> dict:
    """Crawl category pages with auto-rebuild on missing profile / white-screen."""
    per_page_stats: list[dict] = []
    cards_organic: list[dict] = []
    stopped_reason = "completed"

    # Sync slot templates from .env before any browser ops (parity with parallel_resumable_runner).
    recovery.sync_profile_templates_to_db()

    async with async_playwright() as p:
        browser, ctx, profile_id = await _start_browser_with_rebuild(p, slot_id, profile_id)
        page = await ctx.new_page()

        previous_first_urls: list[str] = []
        stale_attempts = 0
        current_page = 1

        try:
            while True:
                if max_pages and current_page > max_pages:
                    stopped_reason = f"hit_max_pages_{max_pages}"
                    break

                page_url = category_url
                if current_page > 1:
                    sep = "&" if "?" in category_url else "?"
                    page_url = f"{category_url}{sep}page={current_page}"

                # Retry loop for this page (covers white-screen rebuild + reload)
                page_handled = False
                for ws_attempt in range(MAX_WHITE_SCREEN_REBUILDS_PER_PAGE + 1):
                    print(f"[crawl] page {current_page}: {page_url}  (attempt {ws_attempt + 1})")
                    try:
                        await page.goto(page_url, wait_until="domcontentloaded", timeout=60000)
                        await page.wait_for_timeout(3000)
                    except Exception as e:
                        print(f"[crawl] page.goto failed on page {current_page}: {e}")
                        stopped_reason = f"page_load_failed_at_{current_page}"
                        page_handled = True
                        break  # bail out of attempt loop into outer loop

                    final_url = page.url
                    ws_check = await adsp_profile_pool_manager.detect_white_screen_block(page, final_url)
                    if ws_check.get("is_white_screen"):
                        if ws_attempt >= MAX_WHITE_SCREEN_REBUILDS_PER_PAGE:
                            print(f"[crawl] WHITE SCREEN persists after {MAX_WHITE_SCREEN_REBUILDS_PER_PAGE} rebuilds. Giving up.")
                            stopped_reason = f"white_screen_unrecoverable_at_{current_page}"
                            page_handled = True
                            break
                        print(f"[crawl] WHITE SCREEN on page {current_page} — rebuilding profile {profile_id}...")
                        try:
                            await page.close()
                        except Exception:
                            pass
                        await _close_browser_safely(browser, ctx, profile_id)
                        rebuild = recovery.auto_rebuild_profile(
                            slot_id,
                            reason=f"white_screen_at_page_{current_page}",
                            delay_seconds=0,
                            delete_old_profile=True,
                        )
                        if not rebuild.get("success"):
                            print(f"[crawl] Profile rebuild FAILED: {rebuild.get('message')}")
                            stopped_reason = f"rebuild_failed_at_{current_page}"
                            page_handled = True
                            browser = ctx = None
                            break
                        new_pid = rebuild.get("new_profile_id") or _get_slot_profile(slot_id)
                        print(f"[crawl] Profile rebuilt: {profile_id} -> {new_pid}. Re-opening browser...")
                        browser, ctx, profile_id = await _start_browser_with_rebuild(p, slot_id, new_pid)
                        page = await ctx.new_page()
                        continue  # retry SAME page

                    # No white screen — process page
                    content = (await page.content()).lower()
                    if "we couldn't find any results" in content or "page not found" in content:
                        print(f"[crawl] No more products on page {current_page}.")
                        stopped_reason = f"no_results_at_{current_page}"
                        page_handled = True
                        break

                    cards_data = await extract_product_cards(page)
                    if not cards_data:
                        print(f"[crawl] zero cards extracted, stopping.")
                        stopped_reason = f"zero_cards_at_{current_page}"
                        page_handled = True
                        break

                    organic = [c for c in cards_data if not c.get("is_sponsored")]
                    sponsored = [c for c in cards_data if c.get("is_sponsored")]

                    first5 = [c["url"] for c in organic[:5]]
                    if current_page > 1 and first5 == previous_first_urls:
                        stale_attempts += 1
                        print(f"[crawl] page {current_page} matches previous page first-5. stale={stale_attempts}")
                        if stale_attempts > 1:
                            stopped_reason = f"stale_pagination_at_{current_page}"
                            page_handled = True
                            break
                    else:
                        stale_attempts = 0
                    previous_first_urls = first5

                    # Price-filter each card
                    kept = 0
                    price_filtered = 0
                    for c in organic:
                        raw_price = c.get("price", "")
                        parsed_price = category_price_filter.parse_price_to_float(raw_price)
                        filt = category_price_filter.product_card_matches_price_filter(
                            parsed_price, price_min, price_max, mode="card_price_prefilter"
                        )
                        c["price_parsed"] = parsed_price
                        c["filter"] = filt
                        if filt["status"] == "filtered_in":
                            kept += 1
                            cards_organic.append(c)
                        else:
                            price_filtered += 1

                    per_page_stats.append({
                        "page": current_page,
                        "raw_cards": len(cards_data),
                        "organic": len(organic),
                        "sponsored": len(sponsored),
                        "price_kept": kept,
                        "price_filtered_out": price_filtered,
                    })
                    print(f"  raw={len(cards_data)} organic={len(organic)} sponsored={len(sponsored)} "
                          f"price_kept={kept} filtered={price_filtered}")

                    if len(organic) < 10:
                        print(f"[crawl] <10 organic cards on page {current_page}, assuming last page.")
                        stopped_reason = f"sparse_page_{current_page}"
                        page_handled = True
                        break

                    page_handled = True
                    break  # leave attempt loop normally

                # Decide if outer loop should continue
                if not page_handled or stopped_reason != "completed":
                    if stopped_reason in (f"sparse_page_{current_page}",):
                        # treat sparse page as terminal
                        stopped_reason = "completed_sparse_last_page"
                    break

                current_page += 1
                await asyncio.sleep(delay_seconds)
        finally:
            await _close_browser_safely(browser, ctx, profile_id)

    return {
        "cards": cards_organic,
        "per_page_stats": per_page_stats,
        "stopped_reason": stopped_reason,
        "total_pages": current_page,
        "final_profile_id": profile_id,
    }


def dedupe_and_filter(
    cards: list[dict],
    skip_existing: bool,
) -> dict:
    existing_pids = load_existing_pids() if skip_existing else set()
    seen: set[str] = set()
    unique_cards: list[dict] = []
    skipped_existing = 0
    skipped_internal_dup = 0
    no_pid = 0
    for c in cards:
        pid = extract_pid(c.get("url") or "")
        if not pid:
            no_pid += 1
            continue
        if pid in seen:
            skipped_internal_dup += 1
            continue
        seen.add(pid)
        if pid in existing_pids:
            skipped_existing += 1
            continue
        c["product_id"] = pid
        unique_cards.append(c)
    return {
        "unique_cards": unique_cards,
        "skipped_internal_dup": skipped_internal_dup,
        "skipped_existing": skipped_existing,
        "no_pid": no_pid,
    }


def write_outputs(
    out_dir: str,
    base_name: str,
    cards: list[dict],
    crawl_result: dict,
    dedupe_result: dict,
    cli_args: dict,
    category_url: str,
) -> tuple[str, str]:
    os.makedirs(out_dir, exist_ok=True)
    urls_path = os.path.join(out_dir, f"{base_name}.urls.txt")
    summary_path = os.path.join(out_dir, f"{base_name}.summary.json")

    with open(urls_path, "w", encoding="utf-8") as f:
        for c in cards:
            url = (c.get("url") or "").split("?")[0]
            f.write(url + "\n")

    total_raw = sum(s["raw_cards"] for s in crawl_result["per_page_stats"])
    total_organic = sum(s["organic"] for s in crawl_result["per_page_stats"])
    total_sponsored = sum(s["sponsored"] for s in crawl_result["per_page_stats"])
    total_price_kept = sum(s["price_kept"] for s in crawl_result["per_page_stats"])
    total_price_filtered = sum(s["price_filtered_out"] for s in crawl_result["per_page_stats"])

    summary = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "category_url": category_url,
        "cli_args": cli_args,
        "result": {
            "pages_crawled": len(crawl_result["per_page_stats"]),
            "stopped_reason": crawl_result["stopped_reason"],
            "total_raw_cards": total_raw,
            "total_organic_cards": total_organic,
            "total_sponsored_cards": total_sponsored,
            "price_filter": {
                "kept": total_price_kept,
                "filtered_out": total_price_filtered,
            },
            "dedupe": {
                "internal_duplicates": dedupe_result["skipped_internal_dup"],
                "already_scraped_existing": dedupe_result["skipped_existing"],
                "no_product_id": dedupe_result["no_pid"],
            },
            "final_url_count": len(cards),
        },
        "per_page": crawl_result["per_page_stats"],
    }
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)

    return urls_path, summary_path


def invoke_scraper(urls_file: str, job_name: str, workers: int) -> int:
    """Hand off URL file to resumable_scraper_runner (still DB-backed for resume)."""
    print(f"\n=== Creating scrape job from {urls_file} ===")
    res = subprocess.run(
        [sys.executable, "resumable_scraper_runner.py", "create",
         "--name", job_name, "--urls", urls_file],
        capture_output=True, text=True,
    )
    sys.stdout.write(res.stdout)
    sys.stderr.write(res.stderr)
    if res.returncode != 0:
        print("[scrape] create failed.")
        return res.returncode

    # Try to parse job_id from output
    m = re.search(r'"job_id"\s*:\s*"([^"]+)"', res.stdout) or re.search(
        r"job_id[:\s=]+(\S+)", res.stdout
    )
    if not m:
        print("[scrape] could not parse job_id from create output. Aborting.")
        return 2
    job_id = m.group(1).strip().rstrip(",}\"")
    print(f"[scrape] job_id = {job_id}")

    print(f"\n=== Starting scrape with {workers} workers ===")
    return subprocess.call(
        [sys.executable, "resumable_scraper_runner.py", "start",
         "--job-id", job_id, "--workers", str(workers)]
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("category_url", help="Chewy category URL")
    ap.add_argument("--workers", type=int, default=3,
                    help="Worker count for PDP scrape (default 3)")
    ap.add_argument("--max-pages", type=int, default=None,
                    help="Cap discovery pages (default: until last page)")
    ap.add_argument("--price-min", type=float, default=None)
    ap.add_argument("--price-max", type=float, default=None)
    ap.add_argument("--delay-seconds", type=float, default=2.0,
                    help="Sleep between category pages (default 2s)")
    ap.add_argument("--reprocess-existing", action="store_true",
                    help="Don't dedupe against output/normalized_products/")
    ap.add_argument("--dry-run", action="store_true",
                    help="Stop after writing URL file + summary; skip the scrape phase")
    ap.add_argument("--name", default=None,
                    help="Job name (default: derived from URL slug)")
    args = ap.parse_args()

    if hasattr(config, "reload_from_env_file"):
        config.reload_from_env_file(override=True)

    # Discovery uses slot CW_1 infrastructure (auto-rebuild on missing profile,
    # auto-rebuild on white-screen). Initial profile_id preference:
    #   1. .env ADSPOWER_PROFILE_ID (legacy single-profile var)
    #   2. .env ADSP_CW_1_PROFILE_ID (slot CW_1)
    #   3. None — _start_browser_with_rebuild will ensure_slot_profile()
    profile_id = (
        config.ADSPOWER_PROFILE_ID
        or getattr(config, "ADSP_CW_1_PROFILE_ID", "")
        or None
    )
    slot_id = DISCOVERY_SLOT

    parsed = urlparse(args.category_url)
    slug_match = re.search(r"/b/([^/?]+)", parsed.path)
    slug = slugify(slug_match.group(1) if slug_match else parsed.path)
    ts = time.strftime("%Y%m%d_%H%M%S")
    base_name = f"{ts}_{slug}"

    print(f"=== Discover: {args.category_url} ===")
    print(f"=== Slot: {slot_id}  Initial profile: {profile_id or '(auto-provision)'}  "
          f"max_pages={args.max_pages}  price=[{args.price_min},{args.price_max}] ===\n")

    crawl_result = asyncio.run(crawl_category(
        args.category_url,
        args.max_pages,
        args.price_min,
        args.price_max,
        args.delay_seconds,
        profile_id,
        slot_id=slot_id,
    ))

    dedupe_result = dedupe_and_filter(
        crawl_result["cards"],
        skip_existing=not args.reprocess_existing,
    )
    unique = dedupe_result["unique_cards"]

    urls_path, summary_path = write_outputs(
        URL_DIR, base_name, unique, crawl_result, dedupe_result,
        cli_args=vars(args), category_url=args.category_url,
    )

    print()
    print("=" * 60)
    print(f"Crawled {len(crawl_result['per_page_stats'])} pages "
          f"({crawl_result['stopped_reason']})")
    print(f"Organic cards : {sum(s['organic'] for s in crawl_result['per_page_stats'])}")
    print(f"Price filtered: {sum(s['price_filtered_out'] for s in crawl_result['per_page_stats'])}")
    print(f"Already scraped (skipped): {dedupe_result['skipped_existing']}")
    print(f"Internal duplicates dropped: {dedupe_result['skipped_internal_dup']}")
    print(f"New URLs to scrape: {len(unique)}")
    print(f"URL list: {urls_path}")
    print(f"Summary : {summary_path}")
    print("=" * 60)

    if not unique:
        print("Nothing to scrape. Done.")
        return 0
    if args.dry_run:
        print("--dry-run: skipping scrape phase.")
        return 0

    job_name = args.name or f"category_{slug}_{ts}"
    return invoke_scraper(urls_path, job_name, args.workers)


if __name__ == "__main__":
    sys.exit(main())
