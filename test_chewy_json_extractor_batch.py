import asyncio
import json
import re
import sys
import argparse
from pathlib import Path
from rich.console import Console
from playwright.async_api import async_playwright

import config
import adspower

# Import functions from extractor
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
    validate_normalized_product
)

console = Console()
OUT_DIR = Path(config.OUTPUT_DIR)
FAILURES_DIR = OUT_DIR / "batch_failures"
FAILURES_DIR.mkdir(parents=True, exist_ok=True)

SMOKE_URLS = [
    "https://www.chewy.com/hills-science-diet-adult-sensitive/dp/3861718", # Apollo, many variants
    "https://www.chewy.com/purina-pro-plan-high-protein-chicken/dp/52620", # Negative test (PLP redirect)
    "https://www.chewy.com/kong-classic-dog-toy/dp/38486", # Apollo, sizes
    "https://www.chewy.com/fancy-feast-seafood-classic-pate/dp/104044", # Cat food pack
    "https://www.chewy.com/frisco-fold-carry-double-door/dp/116518", # Dog crate
    "https://www.chewy.com/blue-buffalo-life-protection-formula/dp/49257", # Dog food sizes
    "https://www.chewy.com/greenies-original-regular-dental-dog/dp/33580", # Treats counts
    "https://www.chewy.com/wellness-core-grain-free-original/dp/37166", # Dry food
    "https://www.chewy.com/seresto-flea-tick-collar-dogs/dp/136974", # Collars packs
    "https://www.chewy.com/american-journey-salmon-sweet-potato/dp/135985" # Single or limited
]

def classify_page(input_url: str, final_url: str, next_data: dict, architecture: str, normalized: dict) -> str:
    if not next_data:
        return "unknown"
        
    page_route = next_data.get("page", "")
    
    is_plp_route = "plp" in page_route.lower()
    redirected = input_url != final_url
    
    if is_plp_route:
        return "redirected_plp" if redirected else "plp"
        
    if normalized.get("warnings") and any("Route is a PLP page" in w for w in normalized["warnings"]):
        return "redirected_plp" if redirected else "plp"
        
    if not normalized.get("title") and not normalized.get("variants"):
        return "unavailable_product"
        
    if redirected and "dp" in final_url:
        return "redirected_pdp"
        
    return "pdp"

async def process_url(url: str, page, delay_ms: int):
    console.print(f"\n[bold cyan]Processing:[/bold cyan] {url}")
    
    match = re.search(r"chewy\.com/(.*?)/dp/(\d+)", url)
    source_product_id = match.group(2) if match else "unknown"
    slug = match.group(1) if match else "unknown"
    
    result = {
        "input_url": url,
        "final_url": url,
        "canonical_url": None,
        "source_product_id": source_product_id,
        "detected_product_id": None,
        "slug": slug,
        "page_kind": "unknown",
        "detected_architecture": "unknown",
        "build_id": None,
        "extraction_success": False,
        "parser_used": "none",
        "is_valid": False,
        "validation_confidence_score": 0,
        "missing_required_fields": [],
        "missing_preferred_fields": [],
        "warnings": [],
        "error": None,
        
        # Product extraction fields
        "title_found": False,
        "brand_found": False,
        "description_found": False,
        "ingredients_found": False,
        "guaranteed_analysis_found": False,
        "nutrition_found": False,
        "feeding_instructions_found": False,
        "specifications_found": False,
        
        # Variant/grouping fields
        "original_variant_count": 0,
        "grouped_products_count": 0,
        "variants_per_group": [],
        "flavors_detected": [],
        "price_found": False,
        "availability_found": False,
        "images_found": False,
        "flavor_specific_images_found": False,
        "products_using_fallback_images": 0,
        "variants_missing_flavor": 0,
        "title_cleanup_success": False,
        
        # Fallback/classification
        "fallback_used": "no",
        "fallback_reason": None,
        "is_negative_test": "no"
    }
    
    try:
        html = await fetch_initial_html(url, page)
        final_url = page.url
        result["final_url"] = final_url
        
        canonical_match = re.search(r'<link\s+rel="canonical"\s+href="([^"]+)"', html)
        if canonical_match:
            result["canonical_url"] = canonical_match.group(1)
            
        next_data = extract_next_data_from_html(html)
        if not next_data:
            build_id = detect_next_build_id(None, html)
            if build_id:
                next_url = build_next_data_url(url, build_id)
                if next_url:
                    next_data = await fetch_next_data_json(next_url, page, build_id, source_product_id)
            
        if not next_data:
            result["error"] = "Failed to extract next_data from HTML or JSON endpoint"
            return result, None
            
        build_id = detect_next_build_id(next_data, html)
        result["build_id"] = build_id
        
        arch = detect_chewy_architecture(next_data)
        result["detected_architecture"] = arch
        
        if arch == "apollo":
            normalized = parse_apollo_product(next_data, final_url)
            result["parser_used"] = "apollo"
        elif arch == "redux":
            normalized = parse_redux_product(next_data, final_url)
            result["parser_used"] = "redux"
        else:
            normalized = {
                "source_product_id": source_product_id,
                "warnings": ["Unknown architecture. Could not parse product."]
            }
            
        page_kind = classify_page(url, final_url, next_data, arch, normalized)
        result["page_kind"] = page_kind
        
        if page_kind in ["plp", "redirected_plp", "unavailable_product"]:
            result["is_negative_test"] = "yes"
            result["warnings"].extend(normalized.get("warnings", []))
            return result, next_data
            
        grouped = split_product_by_flavor(normalized)
        val_report = validate_normalized_product(normalized, grouped)
        
        result["detected_product_id"] = normalized.get("source_product_id")
        result["extraction_success"] = val_report["is_valid"]
        result["is_valid"] = val_report["is_valid"]
        result["validation_confidence_score"] = val_report["confidence_score"]
        result["missing_required_fields"] = val_report["missing_required_fields"]
        result["missing_preferred_fields"] = val_report["missing_preferred_fields"]
        result["warnings"].extend(val_report["warnings"])
        
        result["title_found"] = bool(normalized.get("title"))
        result["brand_found"] = bool(normalized.get("brand"))
        result["description_found"] = bool(normalized.get("description"))
        result["ingredients_found"] = bool(normalized.get("ingredients"))
        result["guaranteed_analysis_found"] = bool(normalized.get("guaranteed_analysis"))
        result["nutrition_found"] = bool(normalized.get("content_sections", {}).get("nutrition", {}).get("calorie_content", {}).get("raw_text"))
        result["feeding_instructions_found"] = bool(normalized.get("feeding_instructions"))
        result["specifications_found"] = bool(normalized.get("content_sections", {}).get("specifications", {}).get("groups", []))
        
        raw_variants = normalized.get("variants", [])
        result["original_variant_count"] = len(raw_variants)
        result["price_found"] = any(v.get("price") for v in raw_variants)
        result["availability_found"] = any(v.get("availability") or v.get("in_stock") is not None for v in raw_variants)
        result["images_found"] = bool(normalized.get("images")) or any(v.get("images") for v in raw_variants)
        
        grouped_products = grouped.get("products", [])
        result["grouped_products_count"] = len(grouped_products)
        
        for p in grouped_products:
            result["variants_per_group"].append(len(p.get("variants", [])))
            if p.get("flavor"):
                result["flavors_detected"].append(p["flavor"])
            debug_info = p.get("debug", {})
            if debug_info.get("image_source") == "variant_flavor_images":
                result["flavor_specific_images_found"] = True
            elif debug_info.get("image_source") == "base_product_fallback":
                result["products_using_fallback_images"] += 1
            if debug_info.get("title_cleanup_applied"):
                result["title_cleanup_success"] = True
                
        if delay_ms > 0:
            await asyncio.sleep(delay_ms / 1000.0)
            
        return result, None
        
    except Exception as e:
        result["error"] = str(e)
        return result, None

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("urls_file", nargs="?", help="Text file with URLs")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--delay-ms", type=int, default=1500)
    parser.add_argument("--output", default="output/chewy_phase3D_fix_batch_report.json")
    args = parser.parse_args()
    
    urls = SMOKE_URLS
    if args.urls_file:
        with open(args.urls_file, "r") as f:
            urls = [line.strip() for line in f if line.strip() and not line.startswith("#")]
            
    urls = urls[:args.limit]
    
    console.print(f"Starting batch test with {len(urls)} URLs")
    
    profile_data = adspower.start_profile(config.ADSPOWER_PROFILE_ID)
    ws_url = adspower.get_ws_endpoint(profile_data)
    
    report = {
        "summary": {
            "total_urls": len(urls),
            "success_count": 0,
            "fail_count": 0,
            "pdp_count": 0,
            "plp_count": 0,
            "redirected_plp_count": 0,
            "redirected_pdp_count": 0,
            "unavailable_product_count": 0,
            "unknown_count": 0,
            "apollo_count": 0,
            "redux_count": 0,
            "unknown_architecture_count": 0,
            "average_confidence_score": 0,
            "products_generated": 0,
            "products_with_flavor_specific_images": 0,
            "products_missing_feeding_instructions": 0,
            "products_requiring_fallback": 0,
            "real_redux_pdp_tested": False
        },
        "results": []
    }
    
    total_confidence = 0
    
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(ws_url)
        context = browser.contexts[0]
        page = context.pages[0] if context.pages else await context.new_page()
        
        for i, url in enumerate(urls):
            res, raw_next_data = await process_url(url, page, args.delay_ms)
            report["results"].append(res)
            
            # Update summary metrics
            kind = res["page_kind"]
            if kind == "pdp": report["summary"]["pdp_count"] += 1
            elif kind == "plp": report["summary"]["plp_count"] += 1
            elif kind == "redirected_plp": report["summary"]["redirected_plp_count"] += 1
            elif kind == "redirected_pdp": report["summary"]["redirected_pdp_count"] += 1
            elif kind == "unavailable_product": report["summary"]["unavailable_product_count"] += 1
            else: report["summary"]["unknown_count"] += 1
            
            arch = res["detected_architecture"]
            if arch == "apollo": report["summary"]["apollo_count"] += 1
            elif arch == "redux": 
                report["summary"]["redux_count"] += 1
                if kind in ["pdp", "redirected_pdp"] and res["extraction_success"]:
                    report["summary"]["real_redux_pdp_tested"] = True
            else: report["summary"]["unknown_architecture_count"] += 1
            
            if res["extraction_success"]:
                report["summary"]["success_count"] += 1
            else:
                report["summary"]["fail_count"] += 1
                if not res.get("is_negative_test") == "yes":
                    # Save diagnostics
                    diag = {
                        "input_url": res["input_url"],
                        "final_url": res["final_url"],
                        "page_kind": res["page_kind"],
                        "detected_architecture": res["detected_architecture"],
                        "error": res["error"],
                        "warnings": res["warnings"],
                        "confidence_score": res["validation_confidence_score"],
                        "fallback_used": res["fallback_used"]
                    }
                    if raw_next_data:
                        diag["top_level_next_data_keys"] = list(raw_next_data.keys())
                        props = raw_next_data.get("props", {}).get("pageProps", {})
                        diag["available_page_props_keys"] = list(props.keys())
                        
                    diag_path = FAILURES_DIR / f"chewy_failure_{res['source_product_id']}.json"
                    with open(diag_path, "w", encoding="utf-8") as f:
                        json.dump(diag, f, indent=2)
                        
            total_confidence += res["validation_confidence_score"]
            report["summary"]["products_generated"] += res["grouped_products_count"]
            if res["flavor_specific_images_found"]:
                report["summary"]["products_with_flavor_specific_images"] += 1
            if not res["feeding_instructions_found"] and res["extraction_success"]:
                report["summary"]["products_missing_feeding_instructions"] += 1
            report["summary"]["products_requiring_fallback"] += res["products_using_fallback_images"]
            
    if report["summary"]["total_urls"] > 0:
        report["summary"]["average_confidence_score"] = total_confidence / report["summary"]["total_urls"]
        
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
        
    adspower.stop_profile(config.ADSPOWER_PROFILE_ID)
    
    # Print human readable summary
    s = report["summary"]
    console.print("\n" + "="*50)
    console.print("[bold]BATCH TEST SUMMARY[/bold]")
    console.print("="*50)
    console.print(f"Total URLs: {s['total_urls']}")
    console.print(f"PDP count: {s['pdp_count']}")
    console.print(f"PLP count: {s['plp_count']}")
    console.print(f"Redirected PLP count: {s['redirected_plp_count']}")
    console.print(f"Redirected PDP count: {s['redirected_pdp_count']}")
    console.print(f"Unavailable product count: {s['unavailable_product_count']}")
    console.print(f"Unknown count: {s['unknown_count']}")
    console.print("")
    console.print(f"Apollo count: {s['apollo_count']}")
    console.print(f"Redux count: {s['redux_count']}")
    console.print(f"Unknown architecture count: {s['unknown_architecture_count']}")
    console.print(f"Real Redux PDP tested: {'yes' if s['real_redux_pdp_tested'] else 'no'}")
    console.print("")
    console.print(f"Success count: {s['success_count']}")
    console.print(f"Fail count: {s['fail_count']}")
    console.print(f"Average confidence score: {s['average_confidence_score']:.1f}")
    console.print("")
    console.print(f"Total grouped products generated: {s['products_generated']}")
    console.print(f"Products with flavor-specific images: {s['products_with_flavor_specific_images']}")
    console.print(f"Products missing feeding instructions: {s['products_missing_feeding_instructions']}")
    console.print(f"Products requiring fallback: {s['products_requiring_fallback']}")
    console.print("="*50)

if __name__ == "__main__":
    asyncio.run(main())
