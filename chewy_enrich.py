"""
Chewy Product Enrichment Pipeline
==================================
Unified enrichment tool for content, price, and image recovery.

Usage:
  # Sample mode (regression tests)
  python chewy_enrich.py --sample selected_products.json --category A
  python chewy_enrich.py --sample selected_products.json --category B
  python chewy_enrich.py --sample selected_products.json --category C

  # Batch mode
  python chewy_enrich.py --input output/normalized_products --mode content --limit 50
  python chewy_enrich.py --input output/normalized_products --mode price --limit 50
  python chewy_enrich.py --input output/normalized_products --mode image --limit 50
  python chewy_enrich.py --input output/normalized_products --mode all --limit 50
"""

import argparse
import asyncio
import json
import os
import re
import random
import sys
import traceback
from datetime import datetime
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
    extract_variant_info_from_apollo,
    enrich_variants_from_api,
    split_product_by_flavor,
    validate_normalized_product,
    WhiteScreenException,  # canonical home is the extractor module
)
from adsp_profile_pool_manager import detect_white_screen_block, mark_profile_in_use
import adsp_profile_recovery_manager
import job_store

console = Console()

# ── Shared Constants ────────────────────────────────────────────────

FLAVOR_KEYWORDS = [
    "duck", "chicken", "beef", "lamb", "salmon", "turkey", "venison",
    "pork", "catfish", "trout", "whitefish", "goat", "kangaroo", "rabbit",
    "bison", "tuna", "herring", "mackerel", "sardine", "cod", "pollock",
    "quail", "pheasant", "elk", "boar", "guinea fowl", "anchovy",
]

SAMPLE_CATEGORY_MAP = {
    "A": "needs_variant_api_enrichment",
    "B": "missing_price_test",
    "C": "missing_image_test",
}

# ── Shared Helpers ──────────────────────────────────────────────────

def has_real_images(img_list: list) -> bool:
    # Chewy CDN `moe/` URLs are real variant-specific images, not placeholders.
    # Any non-empty image URL counts as real.
    if not img_list:
        return False
    return any(isinstance(i, str) and i.strip() for i in img_list)


def detect_flavor_mismatch(product: dict) -> dict:
    """Primary-protein-aware mismatch detection."""
    flavor = (product.get("flavor") or "").strip()
    if not flavor or flavor == "Default":
        return {"mismatch": False, "declared_flavor": flavor,
                "detected_flavors_in_text": [], "fields_with_mismatch": []}

    declared_lower = flavor.lower()
    allowed = {kw for kw in FLAVOR_KEYWORDS if kw in declared_lower}
    if not allowed:
        return {"mismatch": False, "declared_flavor": flavor,
                "detected_flavors_in_text": [], "fields_with_mismatch": []}

    desc = (product.get("description") or "").lower()
    ingr = (product.get("ingredients") or "").lower()
    if not desc.strip() and not ingr.strip():
        return {"mismatch": False, "declared_flavor": flavor,
                "detected_flavors_in_text": [], "fields_with_mismatch": []}

    def _find_primary(text):
        best, earliest = None, len(text) + 1
        for kw in FLAVOR_KEYWORDS:
            m = re.search(r'\b' + re.escape(kw) + r'\b', text)
            if m and m.start() < earliest:
                earliest, best = m.start(), kw
        return best

    primary_desc = _find_primary(desc)
    primary_ingr = _find_primary(ingr)

    if primary_desc in allowed:
        return {"mismatch": False, "declared_flavor": flavor,
                "detected_flavors_in_text": [], "fields_with_mismatch": []}
    if not primary_desc and primary_ingr in allowed:
        return {"mismatch": False, "declared_flavor": flavor,
                "detected_flavors_in_text": [], "fields_with_mismatch": []}
    if not primary_desc and not primary_ingr:
        return {"mismatch": False, "declared_flavor": flavor,
                "detected_flavors_in_text": [], "fields_with_mismatch": []}

    detected, fields_hit = set(), []
    for fn in ["description", "ingredients", "guaranteed_analysis",
               "feeding_instructions"]:
        text = (product.get(fn) or "").lower()
        if not text:
            continue
        for kw in FLAVOR_KEYWORDS:
            if kw in allowed:
                continue
            if re.search(r'\b' + re.escape(kw) + r'\b', text):
                detected.add(kw)
                if fn not in fields_hit:
                    fields_hit.append(fn)

    return {"mismatch": len(detected) > 0, "declared_flavor": flavor,
            "detected_flavors_in_text": sorted(detected),
            "fields_with_mismatch": fields_hit,
            "primary_in_desc": primary_desc, "primary_in_ingr": primary_ingr}


def sanitize_product(product: dict, counters: dict):
    """Final sanitizer: flavor mismatch check + import status assignment."""
    fm = detect_flavor_mismatch(product)
    if fm["mismatch"]:
        counters["flavor_mismatch_count"] += 1
        counters["public_content_unsafe_count"] += 1
        rejected = {}
        for field in fm["fields_with_mismatch"]:
            if product.get(field):
                rejected[field] = product[field]
                product[field] = ""
        product.setdefault("debug", {})["rejected_content"] = rejected
        product["debug"].setdefault("parser_warnings", []).append(
            "public_content_flavor_mismatch")
        product.setdefault("warnings", []).append(
            "public_content_flavor_mismatch")
        product["public_content_safe"] = False
        product["import_ready"] = False
        product["import_mode"] = "blocked"
        product["flavor_mismatch_detail"] = fm
        console.print(
            f"[red]  MISMATCH: {product.get('source_group_id')} "
            f"flavor={fm['declared_flavor']} → {fm['detected_flavors_in_text']}[/red]")
    else:
        product["public_content_safe"] = True

    # Variant-level status
    p_vars = product.get("variants", [])
    has_price = [v for v in p_vars if v.get("price")]
    no_price = [v for v in p_vars if not v.get("price")]
    for v in no_price:
        if "missing_price_unresolved" not in v.get("warnings", []):
            v.setdefault("warnings", []).append("missing_price_unresolved")
        v["variant_export_ready"] = False
    for v in has_price:
        v.setdefault("variant_export_ready", True)

    has_p_imgs = has_real_images(product.get("images", []))
    has_v_imgs = any(has_real_images(v.get("images", [])) for v in p_vars)
    has_any_img = has_p_imgs or has_v_imgs
    if not has_any_img:
        product.setdefault("warnings", []).append("missing_image_unresolved")

    # Final import mode
    if product.get("public_content_safe") is False:
        product["import_ready"] = False
        product["import_mode"] = "blocked"
    elif not has_price:
        product["import_ready"] = False
        product["import_mode"] = "needs_manual_review"
    elif not has_any_img:
        product["import_ready"] = False
        product["import_mode"] = "needs_manual_review"
    elif no_price:
        product["import_ready"] = True
        product["import_mode"] = "safe_with_warnings"
    else:
        product["import_ready"] = True
        product["import_mode"] = "safe_to_import"


# ── Mode-specific recovery ─────────────────────────────────────────

async def recover_price_for_variant(variant, page, build_id):
    """Try to recover price for a single variant via API."""
    v_id = variant.get("source_variant_id")
    v_url = variant.get("variant_url")
    fail = {"recovered": False, "price": None, "autoship_price": None}

    if not v_id or not v_url:
        return {**fail, "reason": "missing_variant_id_or_url"}
    next_url = build_next_data_url(v_url, build_id)
    if not next_url:
        return {**fail, "reason": "cannot_build_next_url"}
    try:
        var_data = await fetch_next_data_json(next_url, page, build_id, v_id)
        if var_data is None:
            return {**fail, "reason": "api_404_or_null"}
        apollo = var_data.get("pageProps", {}).get("__APOLLO_STATE__", {})
        matched = None
        for rk, rv in apollo.items():
            if rk.startswith("Item:") and isinstance(rv, dict):
                if str(rv.get("partNumber", "")) == str(v_id):
                    matched = rv
                    break
        if not matched:
            return {**fail, "reason": "wrong_product_api_response"}
        price = matched.get("advertisedPrice") or matched.get("price")
        if isinstance(price, dict):
            price = price.get("salePrice") or price.get("price")
        autoship = matched.get("autoshipPrice")
        if isinstance(autoship, dict):
            autoship = autoship.get("salePrice") or autoship.get("price")
        if price:
            return {"recovered": True, "price": str(price),
                    "autoship_price": str(autoship) if autoship else None,
                    "reason": None}
        return {**fail, "reason": "api_response_has_no_price"}
    except WhiteScreenException:
        # Profile blocked (HTTP 429/403/503). Bubble up so the worker rebuilds.
        raise
    except Exception as e:
        return {**fail, "reason": f"exception: {e}"}


async def recover_images_for_variant(variant, page, build_id):
    """Try to recover images for a single variant via API."""
    v_id = variant.get("source_variant_id")
    v_url = variant.get("variant_url")
    fail = {"recovered": False, "images": []}

    if not v_id or not v_url:
        return {**fail, "reason": "missing_variant_id_or_url"}
    next_url = build_next_data_url(v_url, build_id)
    if not next_url:
        return {**fail, "reason": "cannot_build_next_url"}
    try:
        var_data = await fetch_next_data_json(next_url, page, build_id, v_id)
        if var_data is None:
            return {**fail, "reason": "api_404_or_null"}
        apollo = var_data.get("pageProps", {}).get("__APOLLO_STATE__", {})
        valid = any(str(rv.get("partNumber", "")) == str(v_id)
                    for rk, rv in apollo.items()
                    if rk.startswith("Item:") and isinstance(rv, dict))
        if not valid:
            return {**fail, "reason": "wrong_product_api_response"}
        v_info = extract_variant_info_from_apollo(var_data, target_variant_id=v_id)
        if v_info.get("images"):
            return {"recovered": True, "images": v_info["images"], "reason": None}
        return {**fail, "reason": "api_response_has_no_images"}
    except WhiteScreenException:
        # Profile blocked (HTTP 429/403/503). Bubble up so the worker rebuilds.
        raise
    except Exception as e:
        return {**fail, "reason": f"exception: {e}"}


# ── Build ID helper ─────────────────────────────────────────────────

async def get_build_id(url, page):
    """Fetch page HTML and extract build_id."""
    console.print(f"Fetching {url} to get build_id...")
    html = await fetch_initial_html(url, page)
    for _ in range(3):
        await page.mouse.wheel(0, random.randint(300, 500))
        await asyncio.sleep(random.uniform(0.5, 1.0))
    detection = await detect_white_screen_block(page, url)
    if detection["is_white_screen"]:
        console.print("[red]White screen detected[/red]")
        raise WhiteScreenException("White screen detected")
    next_data = extract_next_data_from_html(html)
    bid = detect_next_build_id(next_data, html)
    if bid:
        console.print(f"Build ID: {bid}")
    else:
        console.print("[red]Could not detect build_id[/red]")
    return bid


# ── Per-product processing ──────────────────────────────────────────

async def process_product(pid, normalized_dir, page, counters, mode):
    """Process one product through the enrichment pipeline."""
    json_path = normalized_dir / f"chewy_{pid}.json"
    if not json_path.exists():
        console.print(f"[red]Missing {json_path}[/red]")
        return None

    with open(json_path, "r", encoding="utf-8") as f:
        normalized = json.load(f)

    url = normalized.get("source_url")
    if not url:
        console.print(f"[red]No URL for {pid}[/red]")
        return None

    counters["products_processed"] += 1
    variants = normalized.get("variants", [])
    needs_api = False

    # ── Content mode ────────────────────────────────────────────
    if mode in ("content", "all"):
        for v in variants:
            v["ingredients"] = ""
            v["guaranteed_analysis"] = ""
            v["description"] = ""
            v["feeding_instructions"] = ""
            v["calorie_content"] = ""
        needs_api = True

    # ── Price mode ──────────────────────────────────────────────
    missing_price = []
    if mode in ("price", "all"):
        missing_price = [v for v in variants if not v.get("price")]
        counters["variants_missing_price_before"] += len(missing_price)
        if missing_price:
            needs_api = True
            console.print(f"[yellow]{len(missing_price)}/{len(variants)} "
                          f"variants missing price[/yellow]")

    # ── Image mode ──────────────────────────────────────────────
    missing_image = []
    if mode in ("image", "all"):
        missing_image = [v for v in variants
                         if not has_real_images(v.get("images", []))]
        counters["variants_missing_image_before"] += len(missing_image)
        if missing_image:
            needs_api = True
            console.print(f"[yellow]{len(missing_image)}/{len(variants)} "
                          f"variants missing images[/yellow]")
        elif mode == "image":
            console.print(f"[green]All {len(variants)} variants have images[/green]")

    # ── Fetch build_id if needed ────────────────────────────────
    build_id = None
    if needs_api:
        build_id = await get_build_id(url, page)

    # ── Content enrichment ──────────────────────────────────────
    if mode in ("content", "all") and build_id:
        stats = await enrich_variants_from_api(normalized, page, build_id)
        counters["variants_enriched"] += stats.get("enriched", 0)
        counters["wrong_product_api_rejected"] += stats.get(
            "wrong_product_api_rejected", 0)
        counters["slug_mismatch"] += stats.get("slug_mismatch", 0)

    # ── Price recovery ──────────────────────────────────────────
    if missing_price and build_id:
        for v in missing_price:
            result = await recover_price_for_variant(v, page, build_id)
            v_id = v.get("source_variant_id")
            if result["recovered"]:
                v["price"] = result["price"]
                if result["autoship_price"]:
                    v["autoship_price"] = result["autoship_price"]
                counters["variants_price_recovered"] += 1
                console.print(f"[green]  {v_id}: price={result['price']}[/green]")
            else:
                console.print(f"[yellow]  {v_id}: no price ({result['reason']})[/yellow]")
                v.setdefault("warnings", []).append("missing_price_unresolved")
                v["variant_export_ready"] = False
                if "wrong_product" in (result["reason"] or ""):
                    counters["wrong_product_api_rejected"] += 1
            await asyncio.sleep(1.5)

    # ── Image recovery ──────────────────────────────────────────
    if missing_image and build_id:
        for v in missing_image:
            result = await recover_images_for_variant(v, page, build_id)
            v_id = v.get("source_variant_id")
            if result["recovered"]:
                v["images"] = result["images"]
                counters["variants_image_recovered"] += 1
                console.print(f"[green]  {v_id}: {len(result['images'])} images[/green]")
            else:
                console.print(f"[yellow]  {v_id}: no images ({result['reason']})[/yellow]")
                if "wrong_product" in (result["reason"] or ""):
                    counters["wrong_product_api_rejected"] += 1
            await asyncio.sleep(1.5)

    # ── Split by flavor + sanitize ──────────────────────────────
    grouped = split_product_by_flavor(normalized)
    val = validate_normalized_product(normalized, grouped)
    grouped["validation"] = val

    for product in grouped.get("products", []):
        sanitize_product(product, counters)

    # ── Aggregate counters ──────────────────────────────────────
    for product in grouped.get("products", []):
        counters["total_flavor_groups"] += 1
        counters["total_variants"] += len(product.get("variants", []))
        m = product.get("import_mode", "")
        if product.get("import_ready"):
            counters["import_ready_count"] += 1
            if m == "safe_with_warnings":
                counters["safe_with_warnings_count"] += 1
        elif m == "needs_manual_review":
            counters["needs_manual_review_count"] += 1
        elif m == "blocked":
            counters["blocked_count"] += 1

    return grouped


# ── Main pipeline ───────────────────────────────────────────────────

async def run_pipeline(product_ids, normalized_dir, output_dir, mode, label,
                       force_reenrich: bool = False):
    """Run the enrichment pipeline for a list of product IDs.

    Resume safety: each product's status is tracked in chewy_enrichment_state
    (SQLite). Products with status='ok' are skipped on re-run; only the survivors
    are processed. Pass force_reenrich=True to ignore the DB and reprocess all.
    """
    console.print(f"[bold]Starting enrichment ({label}) for "
                  f"{len(product_ids)} products, mode={mode}...[/bold]")

    job_store.init_db()

    # Filter out products already enriched successfully (resume-after-crash).
    if force_reenrich:
        product_queue = list(product_ids)
        skipped_done = 0
        console.print("[yellow]--force-reenrich: ignoring DB state[/yellow]")
    else:
        product_queue = []
        skipped_done = 0
        for pid in product_ids:
            if job_store.is_enrichment_done(pid):
                skipped_done += 1
            else:
                product_queue.append(pid)
        if skipped_done:
            console.print(f"[cyan]Resume: skipping {skipped_done} products already enriched "
                          f"(status='ok' in chewy_enrichment_state). "
                          f"{len(product_queue)} remaining.[/cyan]")
        summary = job_store.enrichment_state_summary()
        console.print(f"[cyan]DB state summary: {summary}[/cyan]")

    if not product_queue:
        console.print("[green]Nothing to do. All requested products already enriched.[/green]")
        return {"products_processed": 0, "skipped_done": skipped_done}

    # Streaming JSONL output: append one product per line and flush after each
    # successful enrichment. Survives crashes / power loss — work-so-far is on
    # disk before we attempt the next product.
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    jsonl_path = output_dir / f"result_{label}_{ts}.jsonl"
    report_path = output_dir / f"report_{label}_{ts}.json"
    jsonl_file = open(jsonl_path, "a", encoding="utf-8")
    jsonl_lines_written = 0
    console.print(f"[cyan]Streaming JSONL -> {jsonl_path}[/cyan]")
    console.print(f"[cyan]Report -> {report_path}[/cyan]")

    # Try to get profile from CW_1 template slot first
    adsp_profile_recovery_manager.sync_profile_templates_to_db()
    cw1 = adsp_profile_recovery_manager.get_template("CW_1")
    current_profile = cw1.get("adspower_profile_id") if cw1 else config.ADSPOWER_PROFILE_ID

    counters = {k: 0 for k in [
        "products_processed", "total_flavor_groups", "total_variants",
        "variants_enriched", "wrong_product_api_rejected", "slug_mismatch",
        "variants_missing_price_before", "variants_price_recovered",
        "variants_missing_image_before", "variants_image_recovered",
        "flavor_mismatch_count", "public_content_unsafe_count",
        "rejected_content_leaked_count",
        "import_ready_count", "safe_with_warnings_count",
        "needs_manual_review_count", "blocked_count",
    ]}

    while product_queue:
        mark_profile_in_use(current_profile)
        console.print(f"[cyan]Using AdsPower Profile: {current_profile}[/cyan]")
        
        try:
            profile_data = adspower.start_profile(current_profile)
        except Exception as e:
            console.print(f"[bold red]Failed to start profile {current_profile}: {e}[/bold red]")
            slot_id = adsp_profile_recovery_manager.get_slot_for_profile_id(current_profile) or "CW_1"
            console.print(f"[yellow]Triggering auto-rebuild for slot {slot_id}[/yellow]")
            rebuild_res = adsp_profile_recovery_manager.auto_rebuild_profile(
                slot_id, reason="Profile does not exist or failed to start", delete_old_profile=False
            )
            if rebuild_res.get("success"):
                current_profile = rebuild_res.get("new_profile_id")
                console.print(f"[green]Rebuild successful. New profile: {current_profile}[/green]")
                continue
            else:
                console.print(f"[bold red]Rebuild failed: {rebuild_res.get('message')}. Pausing.[/bold red]")
                break

        ws_url = adspower.get_ws_endpoint(profile_data)
        
        white_screen_hit = False

        async with async_playwright() as p:
            try:
                browser = await p.chromium.connect_over_cdp(ws_url)
                context = browser.contexts[0]
                page = context.pages[0] if context.pages else await context.new_page()

                while product_queue:
                    pid = product_queue[0]
                    # Last-chance resume check — protects against multi-worker races
                    # where another worker finishes the same pid in parallel.
                    if not force_reenrich and job_store.is_enrichment_done(pid):
                        console.print(f"[yellow]Product {pid} already ok in DB, skipping[/yellow]")
                        product_queue.pop(0)
                        continue

                    # Source URL is needed for the DB row; resolve from the
                    # normalized file (already on disk for the existing dataset).
                    src_url = None
                    try:
                        with open(normalized_dir / f"chewy_{pid}.json", "r", encoding="utf-8") as _f:
                            src_url = json.load(_f).get("source_url")
                    except Exception:
                        pass

                    job_store.mark_enrichment_in_progress(
                        pid, source_url=src_url, mode=mode, run_label=label
                    )
                    try:
                        console.print(f"\n--- Processing Product {pid} ---")
                        # Snapshot global counters BEFORE so we can compute the delta
                        # contributed by this product alone (process_product mutates them).
                        c0 = {k: counters.get(k, 0) for k in (
                            "variants_enriched", "wrong_product_api_rejected", "slug_mismatch",
                            "variants_price_recovered", "variants_image_recovered",
                        )}
                        grouped = await process_product(pid, normalized_dir, page, counters, mode)
                        if grouped:
                            # Stream this product to JSONL immediately (one line, flushed +
                            # fsync). Power loss after this point still preserves the row.
                            line = json.dumps(grouped, ensure_ascii=False)
                            jsonl_file.write(line + "\n")
                            jsonl_file.flush()
                            try:
                                os.fsync(jsonl_file.fileno())
                            except OSError:
                                pass
                            jsonl_lines_written += 1

                            # Per-product delta counters
                            products_n = len(grouped.get("products", []))
                            variants_n = sum(len(p.get("variants", [])) for p in grouped.get("products", []))
                            d_enriched = counters.get("variants_enriched", 0) - c0["variants_enriched"]
                            d_wrong = counters.get("wrong_product_api_rejected", 0) - c0["wrong_product_api_rejected"]
                            d_slugm = counters.get("slug_mismatch", 0) - c0["slug_mismatch"]
                            d_price = counters.get("variants_price_recovered", 0) - c0["variants_price_recovered"]
                            d_image = counters.get("variants_image_recovered", 0) - c0["variants_image_recovered"]

                            # Title list for the success log (truncated for readability)
                            title_lines = []
                            for sp in grouped.get("products", [])[:4]:
                                t = (sp.get("title") or "")[:80]
                                v_n = len(sp.get("variants", []))
                                oos = " [OOS]" if sp.get("out_of_stock") else ""
                                title_lines.append(f"      |- {t}  ({v_n} variants){oos}")
                            if len(grouped.get("products", [])) > 4:
                                title_lines.append(f"      |- … +{len(grouped['products']) - 4} more")

                            console.print(
                                f"[bold green][OK] ENRICHED {pid} - "
                                f"{products_n} Shopify products / {variants_n} variants[/bold green]"
                            )
                            for tl in title_lines:
                                console.print(f"[green]{tl}[/green]")
                            console.print(
                                f"[green]      API: enriched={d_enriched}  "
                                f"wrong_product={d_wrong}  slug_mismatch={d_slugm}  "
                                f"price_recovered={d_price}  image_recovered={d_image}[/green]"
                            )
                            console.print(
                                f"[green]      -> JSONL line {jsonl_lines_written} in {jsonl_path.name}[/green]"
                            )

                            job_store.mark_enrichment_ok(
                                pid,
                                output_path=str(jsonl_path),
                                product_count=products_n,
                                variant_count=variants_n,
                                enriched_count=d_enriched,
                                wrong_product_rejected=d_wrong,
                                slug_mismatch=d_slugm,
                            )
                        else:
                            console.print(f"[red][FAIL] {pid} — process_product returned None[/red]")
                            job_store.mark_enrichment_failed(
                                pid, error_type="no_grouped_result",
                                error_message="process_product returned None"
                            )
                        product_queue.pop(0)
                        await asyncio.sleep(2)
                    except WhiteScreenException:
                        console.print(f"[bold red]White screen on {pid}. Rotating profile...[/bold red]")
                        # Leave pid in queue — DB row stays in_progress; next loop iteration
                        # picks it up after profile rebuild.
                        white_screen_hit = True
                        break
                    except Exception as e:
                        console.print(f"[bold red]Error on {pid}: {e}[/bold red]")
                        traceback.print_exc()
                        job_store.mark_enrichment_failed(
                            pid, error_type=type(e).__name__, error_message=str(e)[:1000]
                        )
                        product_queue.pop(0)  # Skip product on other unexpected errors
            except Exception as e:
                console.print(f"[bold red]Playwright connection error: {e}[/bold red]")

        adspower.stop_profile(current_profile)
        
        if white_screen_hit:
            slot_id = adsp_profile_recovery_manager.get_slot_for_profile_id(current_profile)
            if slot_id:
                console.print(f"[yellow]Triggering profile rebuild for {current_profile} (slot {slot_id})[/yellow]")
                rebuild_res = adsp_profile_recovery_manager.auto_rebuild_profile(
                    slot_id, reason="White screen in chewy_enrich", delete_old_profile=True
                )
                if rebuild_res.get("success"):
                    current_profile = rebuild_res.get("new_profile_id")
                    console.print(f"[green]Rebuild successful. New profile: {current_profile}[/green]")
                else:
                    console.print(f"[bold red]Rebuild failed: {rebuild_res.get('message')}. Pausing.[/bold red]")
                    break
            else:
                console.print(f"[bold red]Profile {current_profile} has no mapped CW template slot. Cannot auto-rebuild. Pausing.[/bold red]")
                break
        elif not product_queue:
            break

    # ── Close JSONL + write report ────────────────────────────────
    try:
        jsonl_file.flush()
        os.fsync(jsonl_file.fileno())
    except Exception:
        pass
    jsonl_file.close()

    db_state_summary = job_store.enrichment_state_summary()
    report = {"run_label": label, "mode": mode, "timestamp": ts,
              "skipped_already_done": skipped_done,
              "products_written_to_jsonl": jsonl_lines_written,
              "jsonl_path": str(jsonl_path),
              "db_state_summary": db_state_summary,
              **counters}

    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    console.print("\n=== COMPLETED ===")
    console.print(f"JSONL ({jsonl_lines_written} products): {jsonl_path}")
    console.print(f"Report: {report_path}")
    console.print(json.dumps(report, indent=2))

    # ── Hard-fail checks ────────────────────────────────────────
    fail = False
    for key in ["public_content_unsafe_count", "flavor_mismatch_count",
                "rejected_content_leaked_count"]:
        if report[key] > 0:
            console.print(f"[bold red]HARD FAIL: {key} = {report[key]}[/bold red]")
            fail = True
    if fail:
        sys.exit(1)

    return report


# ── CLI ─────────────────────────────────────────────────────────────

def parse_args():
    ap = argparse.ArgumentParser(
        description="Chewy Product Enrichment Pipeline")
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--sample", metavar="FILE",
                     help="Path to selected_products.json for sample/test mode")
    grp.add_argument("--input", metavar="DIR",
                     help="Path to normalized_products directory for batch mode")

    ap.add_argument("--category", choices=["A", "B", "C"],
                    help="Category from sample file (A=content, B=price, C=image)")
    ap.add_argument("--mode", choices=["content", "price", "image", "all"],
                    default="all",
                    help="Enrichment mode for batch processing")
    ap.add_argument("--limit", type=int, default=0,
                    help="Max products to process (0 = all)")
    ap.add_argument("--output-dir", default=None,
                    help="Output directory for result/report files")
    ap.add_argument("--force-reenrich", action="store_true",
                    help="Ignore chewy_enrichment_state and re-process every product")
    ap.add_argument("--parallel", action="store_true",
                    help="Run with multiple workers (one per CW slot) via parallel_enrich_runner")
    ap.add_argument("--workers", type=int, default=3,
                    help="Number of parallel workers when --parallel is set (max = MAX_TEMPLATE_SLOTS)")
    ap.add_argument("--max-attempts", type=int, default=5,
                    help="Max retries per pid before workers stop claiming it (parallel mode). 0 = unlimited")
    return ap.parse_args()


async def main():
    args = parse_args()
    base_dir = Path(__file__).parent

    if args.sample:
        # Sample mode — load product IDs from selected_products.json
        sample_path = Path(args.sample)
        if not sample_path.is_absolute():
            sample_path = base_dir / sample_path
        if not args.category:
            console.print("[red]--category is required with --sample[/red]")
            sys.exit(1)

        with open(sample_path, "r", encoding="utf-8") as f:
            selected = json.load(f)
        cat_key = SAMPLE_CATEGORY_MAP[args.category]
        items = selected.get("categories", {}).get(cat_key, [])
        product_ids = [item["source_product_id"] for item in items]
        mode = {"A": "content", "B": "price", "C": "image"}[args.category]
        label = f"category_{args.category.lower()}"
        out_dir = Path(args.output_dir) if args.output_dir else sample_path.parent

    else:
        # Batch mode — scan normalized_products directory
        input_dir = Path(args.input)
        if not input_dir.is_absolute():
            input_dir = base_dir / input_dir
        product_ids = []
        skipped_compounded = 0
        for f in sorted(input_dir.glob("chewy_*.json")):
            pid = f.stem.replace("chewy_", "")
            # Chewy-exclusive compounded medications are not sold on Shopify.
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    src_url = json.load(fh).get("source_url") or ""
            except Exception:
                src_url = ""
            if "compounded" in src_url.lower():
                skipped_compounded += 1
                continue
            product_ids.append(pid)
        if skipped_compounded:
            console.print(
                f"[yellow]Filter: excluded {skipped_compounded} compounded products "
                f"from input ({len(product_ids)} remain).[/yellow]"
            )
        mode = args.mode
        label = f"batch_{mode}"
        out_dir = Path(args.output_dir) if args.output_dir else (
            base_dir / "output" / "enrichment_runs")

    if args.limit > 0:
        product_ids = product_ids[:args.limit]

    normalized_dir = base_dir / "output" / "normalized_products"
    if args.parallel:
        # Multi-worker path: delegate to parallel_enrich_runner
        import parallel_enrich_runner
        # Resolve source_urls from normalized files so seeded rows have URLs
        source_urls = {}
        for pid in product_ids:
            fp = normalized_dir / f"chewy_{pid}.json"
            if fp.exists():
                try:
                    with open(fp, "r", encoding="utf-8") as f:
                        source_urls[str(pid)] = json.load(f).get("source_url")
                except Exception:
                    pass
        await parallel_enrich_runner.run_parallel_enrichment(
            product_ids=product_ids,
            normalized_dir=normalized_dir,
            output_dir=out_dir,
            mode=mode,
            label=label,
            workers=args.workers,
            source_urls=source_urls,
            force_reenrich=args.force_reenrich,
            max_attempts=args.max_attempts,
        )
    else:
        await run_pipeline(product_ids, normalized_dir, out_dir, mode, label,
                           force_reenrich=args.force_reenrich)


if __name__ == "__main__":
    asyncio.run(main())
