"""
Dry-run v2 for 11 products through the NEW pipeline (entryID URLs, per-variant fetch,
TRANSITION_INSTRUCTIONS, split-by-discriminator, out_of_stock detection).

State file `_state.json` tracks per-product status so re-running picks up where it left off
after a white-screen / crash / power loss.

Profile rotation is preserved: try the .env profile first, then CW slots from the recovery
manager. If a profile white-screens we move to the next one. (We do NOT auto-rebuild profiles
in this dry-run script — that runs in the production worker.)

Outputs:
  dry_run_v2_output/grouped_{pid}.json  — one per product, NEW pipeline result
  dry_run_v2_output/_state.json         — resume manifest
"""
import asyncio
import json
import random
import re
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from rich.console import Console
from playwright.async_api import async_playwright

import config
import adspower
import adsp_profile_recovery_manager as recovery
from chewy_next_json_extractor import (
    extract_next_data_from_html,
    detect_next_build_id,
    parse_apollo_product,
    enrich_variants_from_api,
    split_product_by_flavor,
    validate_normalized_product,
    dedupe_products_across_pages,
)

console = Console()

URLS = [
    "https://www.chewy.com/wysong-archetype-chicken-formula/dp/101571",
    "https://www.chewy.com/wysong-archetype-rabbit-formula/dp/101573",
    "https://www.chewy.com/wysong-archetype-quail-formula-freeze/dp/101575",
    "https://www.chewy.com/wysong-uncanny-blend-freeze-dried-raw/dp/101591",
    "https://www.chewy.com/royal-canin-veterinary-diet-adult/dp/101610",
    "https://www.chewy.com/royal-canin-veterinary-diet-adult/dp/101612",
    "https://www.chewy.com/royal-canin-veterinary-diet-adult/dp/101613",
    "https://www.chewy.com/royal-canin-breed-health-nutrition/dp/1018830",
    "https://www.chewy.com/hills-science-diet-adult-perfect/dp/102094",
    "https://www.chewy.com/hills-science-diet-adult-sensitive/dp/102101",
    "https://www.chewy.com/blue-buffalo-wilderness-chicken/dp/32049",
]

OUT_DIR = Path(__file__).parent / "dry_run_v2_output"
OUT_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = OUT_DIR / "_state.json"


def _pid_from_url(url: str) -> str:
    m = re.search(r"/dp/(\d+)", url)
    return m.group(1) if m else "unknown"


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"products": {}}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def candidate_profiles() -> list:
    recovery.sync_profile_templates_to_db()
    seen = []
    if config.ADSPOWER_PROFILE_ID:
        seen.append(config.ADSPOWER_PROFILE_ID)
    for slot in ["CW_1", "CW_2", "CW_3"]:
        t = recovery.get_template(slot)
        if t and t.get("adspower_profile_id") and t["adspower_profile_id"] not in seen:
            seen.append(t["adspower_profile_id"])
    return seen


async def warmup(page):
    try:
        await page.goto("https://www.chewy.com/", timeout=config.PAGE_LOAD_TIMEOUT,
                        wait_until="domcontentloaded")
    except Exception as e:
        console.print(f"[yellow]warm-up goto: {e}[/yellow]")
    await asyncio.sleep(random.uniform(3, 5))
    for _ in range(3):
        await page.mouse.wheel(0, random.randint(300, 700))
        await asyncio.sleep(random.uniform(0.4, 1.0))


async def load_url(page, url):
    for attempt in range(1, 4):
        try:
            await page.goto(url, timeout=config.PAGE_LOAD_TIMEOUT, wait_until="domcontentloaded")
        except Exception as e:
            console.print(f"[yellow]goto attempt {attempt}: {e}[/yellow]")
        await asyncio.sleep(random.uniform(4, 6))
        for _ in range(3):
            await page.mouse.wheel(0, random.randint(400, 800))
            await asyncio.sleep(random.uniform(0.3, 0.8))
        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        html = await page.content()
        if "__NEXT_DATA__" in html and len(html) > 5000:
            return html
        console.print(f"[red]attempt {attempt}: HTML size={len(html)}[/red]")
        await asyncio.sleep(random.uniform(4, 6))
    return html


async def process_one(url, page, build_id_hint=None):
    """Scrape + enrich + split one product. Returns grouped dict."""
    html = await load_url(page, url)
    if "__NEXT_DATA__" not in html:
        return None, "no_next_data"

    next_data = extract_next_data_from_html(html)
    if not next_data:
        return None, "next_data_parse_failed"
    build_id = detect_next_build_id(next_data, html)
    normalized = parse_apollo_product(next_data, url)
    if not normalized.get("variants"):
        return None, "no_variants_parsed"

    stats = await enrich_variants_from_api(normalized, page, build_id)
    grouped = split_product_by_flavor(normalized)
    grouped["validation"] = validate_normalized_product(normalized, grouped)
    grouped["enrichment_stats"] = stats
    grouped["build_id"] = build_id
    return grouped, "ok"


async def main():
    state = load_state()
    pending = [u for u in URLS if state["products"].get(_pid_from_url(u), {}).get("status") != "ok"]
    console.print(f"[bold cyan]{len(pending)}/{len(URLS)} URLs remaining (resume-aware)[/bold cyan]")

    if not pending:
        console.print("[green]All products already done. Nothing to do.[/green]")
        return

    async with async_playwright() as p_obj:
        for profile_id in candidate_profiles():
            if not pending:
                break
            console.print(f"\n[bold cyan]>>> Trying profile {profile_id}[/bold cyan]")
            try:
                pd = adspower.start_profile(profile_id)
            except Exception as e:
                console.print(f"[red]start_profile failed: {e}[/red]")
                continue

            page = None
            try:
                ws_url = adspower.get_ws_endpoint(pd)
                browser = await p_obj.chromium.connect_over_cdp(ws_url)
                ctx = browser.contexts[0]
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()

                # Warm up once per profile
                await warmup(page)

                # Quick probe to see if profile works
                probe_html = await load_url(page, pending[0])
                if "__NEXT_DATA__" not in probe_html or len(probe_html) < 5000:
                    console.print(f"[yellow]Profile {profile_id} white-screened — rotating[/yellow]")
                    adspower.stop_profile(profile_id)
                    continue

                # Process pending URLs
                while pending:
                    url = pending[0]
                    pid = _pid_from_url(url)
                    console.print(f"\n[bold]--- [{pid}] {url}[/bold]")
                    try:
                        grouped, status = await process_one(url, page)
                    except Exception as e:
                        console.print(f"[red]exception: {e}[/red]")
                        traceback.print_exc()
                        state["products"][pid] = {"status": "error", "url": url, "error": str(e)}
                        save_state(state)
                        pending.pop(0)
                        continue

                    if grouped is None:
                        # Possible white-screen → break out, try next profile
                        if status == "no_next_data":
                            console.print(f"[red][{pid}] white-screen suspected; rotating profile[/red]")
                            break
                        state["products"][pid] = {"status": "failed", "url": url, "reason": status}
                        save_state(state)
                        pending.pop(0)
                        continue

                    out_path = OUT_DIR / f"grouped_{pid}.json"
                    out_path.write_text(json.dumps(grouped, ensure_ascii=False, indent=2),
                                        encoding="utf-8")
                    products = grouped.get("products", [])
                    console.print(f"[green][{pid}] {len(products)} Shopify products, "
                                  f"{sum(len(p.get('variants',[])) for p in products)} variants total[/green]")
                    for p in products:
                        disc = p.get("discriminator") or {}
                        oos = "[red]OOS[/red]" if p.get("out_of_stock") else "[green]in_stock[/green]"
                        console.print(f"   {oos}  {p['title'][:78]}  disc={disc}  variants={len(p['variants'])}")
                    state["products"][pid] = {
                        "status": "ok",
                        "url": url,
                        "out_file": str(out_path.name),
                        "product_count": len(products),
                        "variant_count": sum(len(p.get('variants',[])) for p in products),
                    }
                    save_state(state)
                    pending.pop(0)
                    await asyncio.sleep(random.uniform(2.5, 4.5))

            finally:
                try:
                    adspower.stop_profile(profile_id)
                except Exception:
                    pass

        if pending:
            console.print(f"\n[bold red]Stopped with {len(pending)} URLs remaining (all profiles exhausted)[/bold red]")
        else:
            console.print(f"\n[bold green]All {len(URLS)} products processed[/bold green]")

    # Cross-page dedupe step
    grouped_files = sorted(OUT_DIR.glob("grouped_*.json"))
    all_grouped = [json.loads(fp.read_text(encoding="utf-8")) for fp in grouped_files]
    dedup = dedupe_products_across_pages(all_grouped)
    (OUT_DIR / "_deduped.json").write_text(
        json.dumps(dedup, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    console.print(f"\n[bold cyan]Dedupe across {len(all_grouped)} source pages:[/bold cyan]")
    console.print(f"  Candidates: {dedup['total_candidates']}")
    console.print(f"  Unique products kept: {dedup['unique_products']}")
    if dedup["duplicates_log"]:
        console.print(f"  Duplicates collapsed: {len(dedup['duplicates_log'])}")
        for entry in dedup["duplicates_log"]:
            console.print(f"    [yellow]- '{entry['product_title'][:60]}'  "
                          f"kept_from={entry['kept_from_source_pid']}  "
                          f"dropped={entry['dropped_from_source_pids']}[/yellow]")


if __name__ == "__main__":
    asyncio.run(main())
