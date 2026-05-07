import argparse
import json
import logging
import re
import os
import csv
from pathlib import Path

# Add project root to path if needed or just assume running from Pet dir
import chewy_next_json_extractor
import adspower
from playwright.async_api import async_playwright
import asyncio

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

UPC_KEYWORDS = [
    "upc", "universalproductcode", "universal_product_code",
    "barcode", "bar_code", "gtin", "gtin8", "gtin12", "gtin13", "gtin14",
    "ean", "isbn"
]

MPN_KEYWORDS = [
    "mpn", "manufacturerpartnumber", "manufacturer_part_number",
    "manufacturernumber", "vendorpartnumber", "vendor_part_number"
]

SKU_KEYWORDS = [
    "sku", "partnumber", "part_number", "itemid", "item_id",
    "productid", "product_id", "catalogid", "catalogentryid"
]

ALL_KEYWORDS = UPC_KEYWORDS + MPN_KEYWORDS + SKU_KEYWORDS

def classify_identifier(key: str, value: str) -> dict:
    key_lower = key.lower()
    value_str = str(value).strip()
    length = len(value_str)
    numeric_only = value_str.isdigit()
    
    id_type = "unknown"
    confidence = "low"
    reason = "Unknown format"
    
    # Check key type
    is_upc_key = any(k in key_lower for k in UPC_KEYWORDS)
    is_mpn_key = any(k in key_lower for k in MPN_KEYWORDS)
    is_sku_key = any(k in key_lower for k in SKU_KEYWORDS)
    
    if is_upc_key:
        if numeric_only:
            if length == 12:
                id_type = "upc"
                confidence = "high"
                reason = "12-digit numeric value under UPC/GTIN key"
            elif length == 13:
                id_type = "ean"
                confidence = "high"
                reason = "13-digit numeric value under EAN/UPC key"
            elif length == 14:
                id_type = "gtin"
                confidence = "high"
                reason = "14-digit numeric value under GTIN key"
            elif length == 8:
                id_type = "gtin"
                confidence = "medium"
                reason = "8-digit numeric value under GTIN key"
            else:
                id_type = "barcode"
                confidence = "medium"
                reason = f"{length}-digit numeric value under UPC/GTIN key"
        else:
            id_type = "barcode"
            confidence = "low"
            reason = "Non-numeric value under UPC/GTIN key"
            
    elif is_mpn_key:
        id_type = "mpn"
        confidence = "medium"
        reason = "Value under MPN key"
        
    elif is_sku_key:
        id_type = "chewy_internal_id"
        confidence = "high"
        reason = "Value under SKU/Item ID key, likely Chewy internal"
        
    else:
        if numeric_only and length in [12, 13, 14]:
            id_type = "possible_upc"
            confidence = "low"
            reason = f"{length}-digit numeric value under generic key"
            
    return {
        "identifier_type": id_type,
        "confidence": confidence,
        "reason": reason,
        "numeric_only": numeric_only,
        "value_length": length
    }

def find_identifiers_recursively(data, current_path="", results=None):
    if results is None:
        results = []
        
    if isinstance(data, dict):
        for k, v in data.items():
            new_path = f"{current_path}.{k}" if current_path else str(k)
            k_lower = str(k).lower()
            
            # Check if key matches any of our target keywords
            # Exact or substring match, but avoid matching random things if possible
            matched_kw = None
            for kw in ALL_KEYWORDS:
                if kw in k_lower:
                    matched_kw = kw
                    break
                    
            if matched_kw and v is not None and str(v).strip() != "":
                # We found a candidate!
                # Only add if it's a primitive type (string, int, float)
                if isinstance(v, (str, int, float)):
                    classification = classify_identifier(str(k), str(v))
                    results.append({
                        "candidate_key": str(k),
                        "candidate_value": str(v),
                        "json_path": new_path,
                        **classification
                    })
            
            # Recurse
            find_identifiers_recursively(v, new_path, results)
            
    elif isinstance(data, list):
        for i, item in enumerate(data):
            new_path = f"{current_path}[{i}]"
            find_identifiers_recursively(item, new_path, results)
            
    return results

async def audit_url(url: str):
    logger.info(f"Auditing URL: {url}")
    # Extract ID
    match = re.search(r'/dp/(\d+)', url)
    product_id = match.group(1) if match else "unknown"
    
    async with async_playwright() as p:
        try:
            profile_data = adspower.start_profile()
            ws_url = adspower.get_ws_endpoint(profile_data)
            browser = await p.chromium.connect_over_cdp(ws_url)
            context = browser.contexts[0]
            logger.info("Connected to AdsPower CDP")
        except Exception as e:
            logger.info(f"Fallback to headless: {e}")
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context()
            
        page = await context.new_page()
        try:
            html = await chewy_next_json_extractor.fetch_initial_html(url, page)
            next_data = chewy_next_json_extractor.extract_next_data_from_html(html)
            if not next_data:
                logger.error("Could not extract __NEXT_DATA__")
                return None
                
            results = find_identifiers_recursively(next_data)
            return {
                "source_file": url,
                "product_id": product_id,
                "candidates": results
            }
        finally:
            await page.close()
            try:
                adspower.stop_profile()
            except:
                pass

def audit_raw_json(file_path: str):
    logger.info(f"Auditing file: {file_path}")
    with open(file_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
        
    product_id = "unknown"
    match = re.search(r'_(\d+)\.json', file_path)
    if match:
        product_id = match.group(1)
        
    results = find_identifiers_recursively(data)
    return {
        "source_file": file_path,
        "product_id": product_id,
        "candidates": results
    }

def audit_folder(folder_path: str):
    logger.info(f"Auditing folder: {folder_path}")
    all_results = []
    
    path = Path(folder_path)
    if not path.exists():
        logger.error(f"Folder {folder_path} does not exist.")
        return all_results
        
    for file_path in path.glob('*.json'):
        res = audit_raw_json(str(file_path))
        if res and res["candidates"]:
            all_results.append(res)
            
    return all_results

def main():
    parser = argparse.ArgumentParser(description="Audit Chewy JSON for UPC/GTIN fields")
    parser.add_argument("--url", help="Audit a specific Chewy PDP URL")
    parser.add_argument("--raw-json", help="Audit a specific JSON file")
    parser.add_argument("--folder", help="Audit a folder of JSON files")
    args = parser.parse_args()
    
    out_dir = Path("output/upc_audit")
    out_dir.mkdir(parents=True, exist_ok=True)
    
    results = []
    
    if args.url:
        res = asyncio.run(audit_url(args.url))
        if res: results.append(res)
    elif args.raw_json:
        res = audit_raw_json(args.raw_json)
        if res: results.append(res)
    elif args.folder:
        results = audit_folder(args.folder)
    else:
        logger.error("Please provide --url, --raw-json, or --folder")
        return
        
    # Process results into lines and summary
    lines = []
    summary_stats = {
        "total_products_audited": len(results),
        "total_variants_audited": 0, # Difficult to count precisely without schema knowledge, will approximate
        "products_with_upc": 0,
        "variants_with_upc": 0,
        "products_with_gtin": 0,
        "products_with_only_internal_ids": 0,
        "no_identifier_found_count": 0
    }
    
    for r in results:
        has_upc = False
        has_gtin = False
        has_internal = False
        
        for c in r["candidates"]:
            # Try to guess if it's variant level (path contains array index)
            is_variant_level = "[" in c["json_path"] and "]" in c["json_path"]
            
            line = {
                "source_file": r["source_file"],
                "product_id": r["product_id"],
                "variant_id": None, # Hard to extract universally, leave None
                "grouped_flavor": None,
                "candidate_key": c["candidate_key"],
                "candidate_value": c["candidate_value"],
                "json_path": c["json_path"],
                "value_length": c["value_length"],
                "numeric_only": c["numeric_only"],
                "identifier_type": c["identifier_type"],
                "confidence": c["confidence"],
                "reason": c["reason"]
            }
            lines.append(line)
            
            t = c["identifier_type"]
            if t in ["upc", "ean", "barcode"]:
                has_upc = True
                if is_variant_level: summary_stats["variants_with_upc"] += 1
            elif t == "gtin":
                has_gtin = True
            elif t == "chewy_internal_id":
                has_internal = True
                
        if has_upc: summary_stats["products_with_upc"] += 1
        if has_gtin: summary_stats["products_with_gtin"] += 1
        if not has_upc and not has_gtin and has_internal: summary_stats["products_with_only_internal_ids"] += 1
        if not r["candidates"]: summary_stats["no_identifier_found_count"] += 1

    # Save candidates
    with open(out_dir / "upc_audit_candidates.jsonl", "w", encoding="utf-8") as f:
        for l in lines:
            f.write(json.dumps(l) + "\n")
            
    # Save CSV
    if lines:
        with open(out_dir / "upc_audit_summary.csv", "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=lines[0].keys())
            writer.writeheader()
            writer.writerows(lines)
            
    # Save Report
    report = {
        "summary": summary_stats,
        "recommendation": "TBD after review"
    }
    with open(out_dir / "upc_audit_report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
        
    logger.info(f"Audit complete! Check {out_dir}")

if __name__ == "__main__":
    main()
