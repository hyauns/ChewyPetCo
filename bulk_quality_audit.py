import asyncio
import json
import argparse
import re
from pathlib import Path
from rich.console import Console
from playwright.async_api import async_playwright

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
    extract_variant_info_from_apollo
)

console = Console()

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=50)
    args = parser.parse_args()
    
    urls = []
    
    try:
        with open("test.txt", "r") as f:
            for line in f:
                if line.strip() and line.strip() not in urls:
                    urls.append(line.strip())
                    if len(urls) >= args.limit:
                        break
    except Exception:
        pass
        
    console.print(f"Running bulk audit on {len(urls)} URLs...")
    
    profile_data = adspower.start_profile(config.ADSPOWER_PROFILE_ID)
    ws_url = adspower.get_ws_endpoint(profile_data)
    
    stats = {
        "source_products_processed": 0,
        "empty_product_output_count": 0,
        "products_created": 0,
        "variants_created": 0,
        "import_ready_count": 0,
        "clean_import_count": 0,
        "safe_with_warnings_count": 0,
        "needs_review_count": 0,
        "blocked_count": 0,
        "public_content_safe_count": 0,
        "public_content_unsafe_count": 0,
        "public_content_flavor_mismatch_count": 0,
        "rejected_content_leaked_count": 0,
        "product_content_mismatch_count": 0,
        "public_contaminated_content_count": 0,
        "contaminated_variant_count": 0,
        "stale_source_raw_removed_count": 0,
        "missing_ingredients_count": 0,
        "missing_guaranteed_analysis_count": 0,
        "invalid_gtin_count": 0,
        "products_with_blank_description": 0,
        "products_with_generic_safe_description": 0,
        "food_products_missing_required_content_count": 0,
        "non_food_missing_food_fields_ignored_count": 0,
        "unsupported_or_failed_architecture_count": 0,
        "hard_failed": False,
        "failures": []
    }
    
    warnings_report = []
    all_grouped_products = []
    
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(ws_url)
        context = browser.contexts[0]
        page = context.pages[0] if context.pages else await context.new_page()
        
        for url in urls:
            try:
                stats["source_products_processed"] += 1
                console.print(f"Processing: {url}")
                
                match = re.search(r"chewy\.com/(.*?)/dp/(\d+)", url)
                source_id = match.group(2) if match else "unknown"
                
                html = await fetch_initial_html(url, page)
                
                # Scroll down to load all content and satisfy Kasada
                import random
                for _ in range(3):
                    await page.mouse.wheel(0, random.randint(300, 500))
                    await asyncio.sleep(random.uniform(0.5, 1.0))
                    
                from adsp_profile_pool_manager import detect_white_screen_block
                detection_result = await detect_white_screen_block(page, url)
                if detection_result["is_white_screen"]:
                    console.print("[red]White screen detected, skipping...[/red]")
                    stats["failures"].append(url)
                    continue
                    
                html = await page.content() # refresh HTML after wait
                next_data = extract_next_data_from_html(html)
                build_id = detect_next_build_id(next_data, html)
                
                if not next_data and build_id:
                    next_url = build_next_data_url(url, build_id)
                    if next_url:
                        next_data = await fetch_next_data_json(next_url, page, build_id, source_id)
                        
                if not next_data:
                    raise Exception("Failed to fetch next_data")
                    
                arch = detect_chewy_architecture(next_data)
                if arch == "apollo":
                    parsed = parse_apollo_product(next_data, url)
                elif arch == "redux":
                    parsed = parse_redux_product(next_data, url)
                else:
                    raise Exception("Unknown architecture")
                    
                normalized = normalize_chewy_product(parsed)
                
                # Enrich variants with per-variant API content before flavor split
                if arch == "apollo" and build_id:
                    enrichment_stats = await enrich_variants_from_api(normalized, page, build_id)
                    normalized["enrichment_stats"] = enrichment_stats
                                    
                grouped = split_product_by_flavor(normalized)
                val = validate_normalized_product(normalized, grouped)
                
                qr = val.get("quality_report", {})
                
                all_grouped_products.append(grouped)
                
                stats["missing_ingredients_count"] += qr.get("variants_missing_ingredients", 0)
                stats["invalid_gtin_count"] += qr.get("invalid_gtin_count", 0)
                stats["contaminated_variant_count"] += qr.get("variants_with_contaminated_content", 0)
                stats["products_with_generic_safe_description"] += qr.get("products_using_generic_safe_description", 0)
                
                # Phase 5: Check for empty redux output
                if not grouped.get("products"):
                    stats["empty_product_output_count"] += 1
                    if grouped.get("architecture") == "redux":
                        stats["unsupported_or_failed_architecture_count"] += 1
                
                for p in grouped.get("products", []):
                    stats["products_created"] += 1
                    stats["variants_created"] += len(p.get("variants", []))
                    
                    qc = p.get("quality_checks", {})
                    if qc.get("stale_source_raw_removed"): stats["stale_source_raw_removed_count"] += 1
                    
                    if not qc.get("content_sections_match_product_content") or not qc.get("metafields_match_product_content"):
                        stats["product_content_mismatch_count"] += 1
                        
                    if not qc.get("contaminated_variant_content_removed"):
                        stats["public_contaminated_content_count"] += 1
                        
                    if not p.get("description"):
                        stats["products_with_blank_description"] += 1
                        
                    p_warnings = p.get("debug", {}).get("parser_warnings", [])
                        
                    if "public_content_flavor_mismatch" in p_warnings:
                        stats["public_content_flavor_mismatch_count"] += 1
                    if "rejected_content_leaked_to_public_fields" in p_warnings:
                        stats["rejected_content_leaked_count"] += 1
                        
                    is_food = p.get("product_type") in ["food", "supplement"]
                    
                    if is_food:
                        if not p.get("ingredients") or not p.get("guaranteed_analysis"):
                            stats["food_products_missing_required_content_count"] += 1
                    else:
                        if not p.get("ingredients") and not p.get("guaranteed_analysis"):
                            stats["non_food_missing_food_fields_ignored_count"] += 1
                    
                    # Use pre-computed values from validate_normalized_product
                    public_safe = p.get("public_content_safe", False)
                    import_mode = p.get("import_mode", "needs_review")
                    import_ready = p.get("import_ready", False)
                    final_audit = qc.get("final_public_field_audit_passed", False)
                    
                    if public_safe:
                        stats["public_content_safe_count"] += 1
                    else:
                        stats["public_content_unsafe_count"] += 1
                    
                    if import_mode == "blocked":
                        stats["blocked_count"] += 1
                    elif import_mode == "needs_review":
                        stats["needs_review_count"] += 1
                    elif import_mode == "safe_with_warnings":
                        stats["safe_with_warnings_count"] += 1
                        stats["import_ready_count"] += 1
                    elif import_mode == "clean":
                        stats["clean_import_count"] += 1
                        stats["import_ready_count"] += 1
                        
                    # Build warnings report
                    all_warnings = list(p_warnings)
                    for v in p.get("variants", []):
                        if v.get("warnings"):
                            all_warnings.extend(v["warnings"])
                            
                    if not public_safe or all_warnings:
                        if not public_safe:
                            errors = []
                            if not qc.get("product_content_consistent"): errors.append("product_content_inconsistent")
                            if not qc.get("content_sections_match_product_content"): errors.append("content_sections_mismatch")
                            if not qc.get("metafields_match_product_content"): errors.append("metafields_mismatch")
                            if not qc.get("product_facts_match_content"): errors.append("product_facts_mismatch")
                            if not qc.get("rejected_content_not_public"): errors.append("rejected_content_leaked")
                            if not final_audit: errors.append("final_public_field_audit_failed")
                            for e in errors:
                                warnings_report.append({
                                    "source_product_id": source_id,
                                    "source_group_id": p.get("source_group_id"),
                                    "flavor": p.get("flavor"),
                                    "warning_type": "blocking_error",
                                    "warning_message": e,
                                    "public_fields_affected": True,
                                    "action_taken": "blocked"
                                })
                        
                        for w in set(all_warnings):
                            warnings_report.append({
                                "source_product_id": source_id,
                                "source_group_id": p.get("source_group_id"),
                                "flavor": p.get("flavor"),
                                "warning_type": "warning",
                                "warning_message": w,
                                "public_fields_affected": False,
                                "action_taken": "warned"
                            })
                            
            except Exception as e:
                console.print(f"[red]Failed {url}: {e}[/red]")
                stats["failures"].append(url)
                
            await asyncio.sleep(3.5)
                
    adspower.stop_profile(config.ADSPOWER_PROFILE_ID)
    
    # Hard fail if any import_ready product has unsafe public content
    if stats["public_content_unsafe_count"] > 0 and stats["blocked_count"] != stats["public_content_unsafe_count"]:
        stats["hard_failed"] = True
    if stats["rejected_content_leaked_count"] > 0:
        stats["hard_failed"] = True
        
    with open("output/bulk_quality_audit.json", "w", encoding="utf-8") as f:
        json.dump({
            "stats": stats,
            "warnings_report": warnings_report
        }, f, indent=2)
        
    with open("bulk_audit_20_products_full.json", "w", encoding="utf-8") as f:
        json.dump(all_grouped_products, f, indent=2)
        
    console.print("\n=== BULK QUALITY AUDIT REPORT ===")
    for k, v in stats.items():
        if k != "failures":
            console.print(f"{k}: {v}")
            
    if stats["hard_failed"]:
        console.print("\n[bold red]AUDIT HARD FAILED due to public contamination or mismatches![/bold red]")
    else:
        console.print("\n[bold green]AUDIT PASSED! No public contamination or mismatches.[/bold green]")

if __name__ == "__main__":
    asyncio.run(main())
