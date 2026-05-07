import os
import json
import argparse
import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

import config
import adspower
import chewy_next_json_extractor

async def backfill(write_mode=False):
    grouped_dir = Path("output/grouped_products")
    if not grouped_dir.exists():
        print(f"Directory {grouped_dir} does not exist.")
        return
        
    out_dir = Path("output/grouped_products_with_identifiers")
    if write_mode:
        out_dir = grouped_dir
    else:
        out_dir.mkdir(parents=True, exist_ok=True)
        
    report = {
        "files_processed": 0,
        "files_updated": 0,
        "total_variants_updated": 0,
        "errors": []
    }
    
    profile_data = adspower.start_profile(config.ADSPOWER_PROFILE_ID)
    ws_url = adspower.get_ws_endpoint(profile_data)
    
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(ws_url)
        context = browser.contexts[0]
        page = context.pages[0] if context.pages else await context.new_page()
        
        for file_path in grouped_dir.glob("chewy_grouped_by_flavor_*.json"):
            report["files_processed"] += 1
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                
            source_url = data.get("source_url")
            if not source_url:
                report["errors"].append(f"{file_path.name}: No source_url found.")
                continue
                
            print(f"Fetching {source_url} to backfill identifiers...")
            html = await chewy_next_json_extractor.fetch_initial_html(source_url, page)
            next_data = chewy_next_json_extractor.extract_next_data_from_html(html)
            
            if not next_data:
                report["errors"].append(f"{file_path.name}: Failed to get __NEXT_DATA__.")
                continue
                
            arch = chewy_next_json_extractor.detect_chewy_architecture(next_data)
            if arch == "apollo":
                raw_prod = chewy_next_json_extractor.parse_apollo_product(next_data, source_url)
            else:
                raw_prod = chewy_next_json_extractor.parse_redux_product(next_data, source_url)
                
            # Create a lookup mapping from source_variant_id to identifiers
            idents_lookup = {}
            for v in raw_prod.get("variants", []):
                vid = v.get("source_variant_id")
                if vid and "identifiers" in v:
                    idents_lookup[vid] = v["identifiers"]
                    
            updated_variants = 0
            for p_grp in data.get("products", []):
                for v in p_grp.get("variants", []):
                    vid = v.get("source_variant_id")
                    if vid in idents_lookup:
                        v["identifiers"] = idents_lookup[vid]
                        updated_variants += 1
                        
            if updated_variants > 0:
                report["files_updated"] += 1
                report["total_variants_updated"] += updated_variants
                
                out_path = out_dir / file_path.name
                with open(out_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
                    
                print(f"[{'WRITE' if write_mode else 'DRY RUN'}] Updated {updated_variants} variants in {file_path.name}")
            else:
                print(f"No variants updated for {file_path.name}")
                
    with open("output/identifier_backfill_report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
        
    print(f"Backfill complete! Report saved to output/identifier_backfill_report.json")

def main():
    parser = argparse.ArgumentParser(description="Backfill missing UPC identifiers for existing Chewy products")
    parser.add_argument("--write", action="store_true", help="Overwrite original files in grouped_products. If false, writes to grouped_products_with_identifiers.")
    parser.add_argument("--dry-run", action="store_true", help="Explicit dry run flag (default behavior without --write)")
    
    args = parser.parse_args()
    
    write_mode = args.write
    asyncio.run(backfill(write_mode=write_mode))

if __name__ == "__main__":
    main()
