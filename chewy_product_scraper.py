"""
Chewy Product Scraper
=====================
Scrape Chewy product pages and extract structured data via Next.js / Apollo.

Usage:
  # Single product
  python chewy_product_scraper.py --url "https://www.chewy.com/.../dp/12345"

  # Batch from job queue
  python chewy_product_scraper.py --job-id chewy_cat_001 --limit 100

  # Resume interrupted batch
  python chewy_product_scraper.py --job-id chewy_cat_001 --limit 100 --resume
"""

import argparse
import asyncio
import json
import os
import re
import random
import sys
from pathlib import Path

from rich.console import Console
from playwright.async_api import async_playwright

sys.path.insert(0, str(Path(__file__).parent))
import config
import adspower
from chewy_next_json_extractor import (
    fetch_initial_html,
    extract_next_data_from_html,
    detect_next_build_id,
    build_next_data_url,
    fetch_next_data_json,
    detect_chewy_architecture,
    parse_apollo_product,
    parse_redux_product,
    normalize_chewy_product,
    enrich_variants_from_api,
    split_product_by_flavor,
    validate_normalized_product,
    sanitize_product,
    WhiteScreenException,
)

console = Console()


async def scrape_single_product(url: str, page) -> dict | None:
    """Scrape one Chewy product page, enrich variants, and return all 3 artifacts.

    Single-pass pipeline (replaces the legacy scrape→enrich two-step):
      1. Fetch HTML + Apollo state
      2. Parse + normalize
      3. Per-variant API enrichment (description, ingredients, GA, feeding,
         calorie, transition, stock fields, source_entry_id backfill)
      4. Split by discriminator → Shopify-shaped grouped products
      5. Validate + sanitize each product (flavor mismatch, import_ready)

    Returns a dict with three artifacts ready for disk:
        {"normalized": ..., "grouped": ..., "validation": ...}
    or None on unrecoverable failure. WhiteScreenException propagates so the
    runner can rebuild the profile.
    """
    console.print(f"[cyan]Scraping: {url}[/cyan]")

    html = await fetch_initial_html(url, page)
    next_data = extract_next_data_from_html(html)
    build_id = detect_next_build_id(next_data, html)

    if not next_data:
        if build_id:
            next_url = build_next_data_url(url, build_id)
            if next_url:
                match = re.search(r"/dp/(\d+)", url)
                sid = match.group(1) if match else "unknown"
                next_data = await fetch_next_data_json(
                    next_url, page, build_id, sid)

    if not next_data:
        console.print("[red]Failed to extract __NEXT_DATA__[/red]")
        return None

    arch = detect_chewy_architecture(next_data)
    if arch == "apollo":
        parsed = parse_apollo_product(next_data, url)
    elif arch == "redux":
        parsed = parse_redux_product(next_data, url)
    else:
        console.print(f"[red]Unknown architecture: {arch}[/red]")
        return None

    if not parsed or not parsed.get("title"):
        console.print("[red]Parsing failed — no title[/red]")
        return None

    normalized = normalize_chewy_product(parsed)
    console.print(f"[green]OK: {normalized.get('title', '')[:80]}[/green]")
    console.print(f"  Variants: {len(normalized.get('variants', []))}")

    # Per-variant API enrichment — fills description/GA/feeding/calorie/
    # transition per variant + backfills source_entry_id + stock fields.
    if build_id:
        try:
            stats = await enrich_variants_from_api(normalized, page, build_id)
            console.print(
                f"  Enriched variants: {stats.get('enriched', 0)} "
                f"(wrong_product={stats.get('wrong_product_api_rejected', 0)}, "
                f"slug_mm={stats.get('slug_mismatch', 0)})"
            )
        except WhiteScreenException:
            raise
        except Exception as e:
            console.print(f"[yellow]  Variant enrichment soft-failed: {e}[/yellow]")
    else:
        console.print("[yellow]  No build_id — skipping variant enrichment[/yellow]")

    # Shopify-shaped grouped products + per-product validation + sanitize.
    grouped = split_product_by_flavor(normalized)
    validation = validate_normalized_product(normalized, grouped)
    grouped["validation"] = validation
    for product in grouped.get("products", []):
        sanitize_product(product)

    return {
        "normalized": normalized,
        "grouped": grouped,
        "validation": validation,
    }


async def main():
    ap = argparse.ArgumentParser(description="Chewy Product Scraper")
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--url", help="Single product URL to scrape")
    grp.add_argument("--job-id",
                     help="Job ID from category scraper for batch mode")
    ap.add_argument("--limit", type=int, default=0,
                    help="Max products to process in batch mode")
    ap.add_argument("--resume", action="store_true",
                    help="Resume interrupted batch")
    args = ap.parse_args()

    base_dir = Path(__file__).parent
    norm_dir = base_dir / "output" / "normalized_products"
    grp_dir = base_dir / "output" / "grouped_products"
    val_dir = base_dir / "output" / "validation"
    for d in (norm_dir, grp_dir, val_dir):
        d.mkdir(parents=True, exist_ok=True)

    if args.job_id:
        # Delegate to the existing resumable runner for batch mode
        console.print("[bold]Batch mode — delegating to resumable runner[/bold]")
        cmd = [sys.executable, str(base_dir / "resumable_scraper_runner.py"),
               "--job-id", args.job_id]
        if args.limit:
            cmd += ["--limit", str(args.limit)]
        if args.resume:
            cmd += ["--resume"]
        import subprocess
        subprocess.run(cmd)
        return

    # Single product mode. When invoked as a subprocess by the resumable
    # runner, ADSP_BROWSER_WS_URL is set — reuse that browser (the runner
    # keeps it alive across items). Otherwise start an AdsPower profile.
    reused_ws = os.environ.get("ADSP_BROWSER_WS_URL")
    profile_id = os.environ.get("ADSPOWER_PROFILE_ID") or config.ADSPOWER_PROFILE_ID
    profile_started = False
    ws_url = reused_ws

    if not ws_url:
        profile_data = adspower.start_profile(profile_id)
        ws_url = adspower.get_ws_endpoint(profile_data)
        profile_started = True

    # Network errors that mean the proxy / network for this profile is dead.
    # Surface them as a white-screen marker so the resumable runner quarantines
    # the profile and rebuilds the slot instead of just marking the item failed.
    PROXY_DEAD_TOKENS = (
        "ERR_CONNECTION_CLOSED", "ERR_CONNECTION_RESET", "ERR_CONNECTION_REFUSED",
        "ERR_CONNECTION_ABORTED", "ERR_PROXY_CONNECTION_FAILED",
        "ERR_TUNNEL_CONNECTION_FAILED", "ERR_SOCKS_CONNECTION_FAILED",
        "ERR_TIMED_OUT", "ERR_NETWORK_CHANGED",
    )

    def _emit_white_screen(reason: str, error: str) -> None:
        payload = {"is_white_screen": True, "reason": reason, "error": error[:300]}
        # The marker line is parsed verbatim by resumable_scraper_runner.
        print(f"[WHITE_SCREEN_RESULT] {json.dumps(payload, ensure_ascii=False)}",
              flush=True)

    try:
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(ws_url)
            context = browser.contexts[0]
            page = (context.pages[0] if context.pages
                    else await context.new_page())

            try:
                await page.goto(args.url, timeout=config.PAGE_LOAD_TIMEOUT,
                                wait_until="domcontentloaded")
            except Exception as e:
                err = str(e)
                if any(tok in err for tok in PROXY_DEAD_TOKENS):
                    _emit_white_screen("proxy_connection_error", err)
                    sys.exit(1)
                raise
            await asyncio.sleep(random.uniform(3, 5))

            try:
                result = await scrape_single_product(args.url, page)
            except WhiteScreenException as e:
                _emit_white_screen("white_screen_exception", str(e))
                sys.exit(1)
            except Exception as e:
                err = str(e)
                if any(tok in err for tok in PROXY_DEAD_TOKENS):
                    _emit_white_screen("proxy_connection_error", err)
                    sys.exit(1)
                raise

            if result:
                normalized = result["normalized"]
                grouped = result["grouped"]
                validation = result["validation"]
                pid = normalized.get("source_product_id", "unknown")
                with open(norm_dir / f"chewy_{pid}.json", "w", encoding="utf-8") as f:
                    json.dump(normalized, f, indent=2, ensure_ascii=False)
                with open(grp_dir / f"chewy_grouped_by_flavor_{pid}.json", "w", encoding="utf-8") as f:
                    json.dump(grouped, f, indent=2, ensure_ascii=False)
                with open(val_dir / f"chewy_validation_{pid}.json", "w", encoding="utf-8") as f:
                    json.dump(validation, f, indent=2, ensure_ascii=False)
                console.print(f"[green]Saved pid={pid}: normalized + grouped + validation[/green]")
            else:
                console.print("[red]Scrape failed[/red]")
                sys.exit(1)
    finally:
        # Only stop the profile if THIS process started it.
        if profile_started:
            adspower.stop_profile(profile_id)


if __name__ == "__main__":
    asyncio.run(main())
