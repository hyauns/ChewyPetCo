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
    split_product_by_flavor,
    validate_normalized_product,
)

console = Console()


async def scrape_single_product(url: str, page) -> dict | None:
    """Scrape one Chewy product page and return normalized data."""
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
    return normalized


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
    out_dir = base_dir / "output" / "normalized_products"
    out_dir.mkdir(parents=True, exist_ok=True)

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

    # Single product mode
    profile_data = adspower.start_profile(config.ADSPOWER_PROFILE_ID)
    ws_url = adspower.get_ws_endpoint(profile_data)

    try:
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(ws_url)
            context = browser.contexts[0]
            page = (context.pages[0] if context.pages
                    else await context.new_page())

            await page.goto(args.url, timeout=config.PAGE_LOAD_TIMEOUT,
                            wait_until="domcontentloaded")
            await asyncio.sleep(random.uniform(3, 5))

            normalized = await scrape_single_product(args.url, page)

            if normalized:
                pid = normalized.get("source_product_id", "unknown")
                path = out_dir / f"chewy_{pid}.json"
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(normalized, f, indent=2, ensure_ascii=False)
                console.print(f"[green]Saved: {path}[/green]")
            else:
                console.print("[red]Scrape failed[/red]")
                sys.exit(1)
    finally:
        adspower.stop_profile(config.ADSPOWER_PROFILE_ID)


if __name__ == "__main__":
    asyncio.run(main())
