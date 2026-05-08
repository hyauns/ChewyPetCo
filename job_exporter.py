import os
import json
import csv
import shutil
import re
import argparse
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, List, Optional

import job_store
import config

EXPORTS_DIR = Path(config.OUTPUT_DIR) / "exports"
ARCHIVE_DIR = Path(config.OUTPUT_DIR) / "archive"

def init_dirs():
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

def build_export_name(category_job_id: Optional[str] = None, pdp_job_id: Optional[str] = None, custom_name: Optional[str] = None) -> str:
    if custom_name:
        name = custom_name
    else:
        name = "export"
        if category_job_id:
            cat_job = job_store.get_category_job(category_job_id)
            if cat_job:
                cat_url = cat_job.get("category_url", "")
                cat_slug = cat_url.rstrip("/").split("/")[-1] if cat_url else ""
                if not cat_slug:
                    cat_slug = f"catjob_{category_job_id}"
                
                # Try to determine max pages / total pages
                items = job_store.get_category_items(category_job_id)
                pages = sorted(list(set([i.get("page_number") for i in items if i.get("page_number")])))
                page_str = ""
                if pages:
                    page_str = f"_pages-{pages[0]}-{pages[-1]}"
                    
                price_min = cat_job.get("price_min")
                price_str = f"_price-min-{int(price_min)}" if price_min is not None else ""
                
                name = f"{cat_slug}{page_str}{price_str}_job_{pdp_job_id or category_job_id}"
        elif pdp_job_id:
            name = f"pdp-job_{pdp_job_id}"
            
    # Sanitize filename
    name = re.sub(r'[^a-zA-Z0-9_\-]', '_', name.lower())
    return name

def _get_category_context(pdp_job_id: str) -> Optional[Dict[str, Any]]:
    # Extract category_job_id from pdp job notes
    job = job_store.get_job(pdp_job_id)
    if not job:
        return None
    notes = job.get("notes", "")
    match = re.search(r'Source category_job_id:\s*(catjob_[a-zA-Z0-9_]+)', notes)
    if not match:
        return None
        
    cat_id = match.group(1)
    cat_job = job_store.get_category_job(cat_id)
    if not cat_job:
        return None
        
    items = job_store.get_category_items(cat_id)
    pages = sorted(list(set([i.get("page_number") for i in items if i.get("page_number")])))
    cat_url = cat_job.get("category_url", "")
    
    return {
        "category_job_id": cat_id,
        "category_url": cat_url,
        "category_slug": cat_url.rstrip("/").split("/")[-1] if cat_url else "",
        "page_range": {
            "from": pages[0] if pages else None,
            "to": pages[-1] if pages else None
        },
        "price_filter": {
            "price_min": cat_job.get("price_min"),
            "price_max": cat_job.get("price_max"),
            "mode": cat_job.get("mode")
        }
    }

def export_job_products(pdp_job_id: str, custom_name: Optional[str] = None, archive_raw: bool = False, delete_after_archive: bool = False) -> Optional[Dict[str, Any]]:
    init_dirs()
    job = job_store.get_job(pdp_job_id)
    if not job:
        print(f"Error: Job {pdp_job_id} not found.")
        return None
        
    cat_context = _get_category_context(pdp_job_id)
    cat_id = cat_context["category_job_id"] if cat_context else None
    export_name = build_export_name(category_job_id=cat_id, pdp_job_id=pdp_job_id, custom_name=custom_name)
    
    items = job_store.get_job_items(pdp_job_id)
    successful_items = [i for i in items if i.get("status") == "done" or (i.get("status") == "skipped" and i.get("grouped_output_path"))]
    failed_items = [i for i in items if i.get("status") in ("failed", "pending", "running", "paused")]
    skipped_items = [i for i in items if i.get("status") == "skipped"]
    
    consolidated_products = []
    manifest_rows = []
    
    total_variants = 0
    generated_grouped_products = 0
    
    conf_scores = []
    
    for item in items:
        # Build manifest row regardless of success
        is_success = item.get("status") == "done" or (item.get("status") == "skipped" and item.get("grouped_output_path"))
        
        grouped_path = item.get("grouped_output_path")
        val_path = item.get("validation_output_path")
        score = item.get("confidence_score")
        if score is not None:
            conf_scores.append(score)
            
        manifest_row = {
            "source_product_id": item.get("source_product_id") or job_store.extract_chewy_product_id(item.get("input_url", "")),
            "input_url": item.get("input_url"),
            "final_url": item.get("final_url"),
            "grouped_output_path": grouped_path,
            "validation_output_path": val_path,
            "confidence_score": score,
            "grouped_products_count": 0,
            "variants_count": 0,
            "flavors": "",
            "status": item.get("status"),
            "error_type": item.get("error_type"),
            "error_message": item.get("error_message")
        }
        
        if is_success and grouped_path and os.path.exists(grouped_path):
            try:
                with open(grouped_path, "r", encoding="utf-8") as f:
                    grouped_data = json.load(f)
                    
                consolidated_products.append(grouped_data)
                
                products = grouped_data.get("products", [])
                manifest_row["grouped_products_count"] = len(products)
                
                v_count = 0
                flavors = []
                for p in products:
                    v_count += len(p.get("variants", []))
                    flavors.append(p.get("flavor", "Unknown"))
                    
                manifest_row["variants_count"] = v_count
                manifest_row["flavors"] = " | ".join(flavors)
                
                generated_grouped_products += len(products)
                total_variants += v_count
            except Exception as e:
                manifest_row["error_type"] = "export_read_error"
                manifest_row["error_message"] = str(e)
                
        manifest_rows.append(manifest_row)
        
    # Write Success JSON
    success_json_path = EXPORTS_DIR / f"{export_name}_success.json"
    consolidated_json = {
        "export_type": "chewy_grouped_products",
        "export_version": "1.0",
        "export_name": export_name,
        "created_at": datetime.now().isoformat() + "Z",
        "category_context": cat_context,
        "pdp_job_context": {
            "pdp_job_id": pdp_job_id,
            "mode": job.get("mode"),
            "confidence_threshold": job.get("confidence_threshold")
        },
        "summary": {
            "successful_base_products": len(consolidated_products),
            "generated_grouped_products": generated_grouped_products,
            "total_variants": total_variants,
            "failed_items": len(failed_items),
            "skipped_items": len(skipped_items),
            "duplicates_skipped": len([i for i in skipped_items if not i.get("grouped_output_path")])
        },
        "products": consolidated_products
    }
    
    with open(success_json_path, "w", encoding="utf-8") as f:
        json.dump(consolidated_json, f, indent=2)
        
    # Write Manifest CSV
    manifest_csv_path = EXPORTS_DIR / f"{export_name}_manifest.csv"
    if manifest_rows:
        with open(manifest_csv_path, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=manifest_rows[0].keys())
            writer.writeheader()
            writer.writerows(manifest_rows)
            
    # Write Failed CSV
    failed_csv_path = EXPORTS_DIR / f"{export_name}_failed.csv"
    if failed_items:
        with open(failed_csv_path, "w", encoding="utf-8", newline="") as f:
            failed_keys = ["index_number", "input_url", "status", "error_type", "error_message", "diagnostic_output_path", "run_log_path", "attempts"]
            writer = csv.DictWriter(f, fieldnames=failed_keys)
            writer.writeheader()
            for fi in failed_items:
                writer.writerow({k: fi.get(k) for k in failed_keys})
                
    # Write Summary JSON
    summary_json_path = EXPORTS_DIR / f"{export_name}_summary.json"
    summary_data = {
        "export_name": export_name,
        "pdp_job_id": pdp_job_id,
        "category_job_id": cat_id,
        "created_at": datetime.now().isoformat() + "Z",
        "successful_base_products": len(consolidated_products),
        "generated_grouped_products": generated_grouped_products,
        "total_variants": total_variants,
        "failed_items": len(failed_items),
        "skipped_items": len(skipped_items),
        "duplicates_skipped": len([i for i in skipped_items if not i.get("grouped_output_path")]),
        "price_filter": cat_context.get("price_filter") if cat_context else None,
        "validation": {
            "average_confidence_score": sum(conf_scores) / len(conf_scores) if conf_scores else 0,
            "min_confidence_score": min(conf_scores) if conf_scores else 0,
            "max_confidence_score": max(conf_scores) if conf_scores else 0
        },
        "files": {
            "success_json": str(success_json_path),
            "manifest_csv": str(manifest_csv_path),
            "failed_csv": str(failed_csv_path) if failed_items else None
        }
    }
    
    with open(summary_json_path, "w", encoding="utf-8") as f:
        json.dump(summary_data, f, indent=2)
        
    if archive_raw:
        archive_job_files(pdp_job_id, export_name, move=delete_after_archive)
        
    return summary_data

def archive_job_files(pdp_job_id: str, export_name: str, move: bool = False):
    job_archive_dir = ARCHIVE_DIR / export_name
    job_archive_dir.mkdir(parents=True, exist_ok=True)
    
    items = job_store.get_job_items(pdp_job_id)
    files_to_archive = []
    
    for item in items:
        for key in ["grouped_output_path", "normalized_output_path", "validation_output_path", "diagnostic_output_path", "run_log_path"]:
            p = item.get(key)
            if p and os.path.exists(p):
                files_to_archive.append(p)
                
    for src_path in set(files_to_archive):
        filename = os.path.basename(src_path)
        dest_path = job_archive_dir / filename
        try:
            if move:
                shutil.move(src_path, dest_path)
            else:
                shutil.copy2(src_path, dest_path)
        except Exception as e:
            print(f"Warning: Could not archive {src_path}: {e}")
            
    print(f"Archived {len(files_to_archive)} files to {job_archive_dir}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Export a scraped PDP job into a consolidated format.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    
    export_p = subparsers.add_parser("export")
    export_p.add_argument("--job-id", required=True, help="PDP Job ID to export")
    export_p.add_argument("--category-job-id", help="Optional Category Job ID (usually inferred from notes)")
    export_p.add_argument("--name", help="Optional custom export name")
    export_p.add_argument("--archive-raw", action="store_true", help="Copy raw per-product files to archive dir")
    export_p.add_argument("--delete-after-archive", action="store_true", help="Move instead of copy raw files (destructive)")
    
    archive_p = subparsers.add_parser("archive")
    archive_p.add_argument("--job-id", required=True)
    archive_p.add_argument("--name", required=True)
    archive_p.add_argument("--move", action="store_true", help="Move files instead of copying")
    
    args = parser.parse_args()
    
    if args.command == "export":
        print(f"Exporting job {args.job_id}...")
        summary = export_job_products(
            pdp_job_id=args.job_id,
            custom_name=args.name,
            archive_raw=args.archive_raw,
            delete_after_archive=args.delete_after_archive
        )
        if summary:
            print(f"Export completed. {summary['successful_base_products']} products exported.")
            print(f"Success JSON: {summary['files']['success_json']}")
    elif args.command == "archive":
        print(f"Archiving files for job {args.job_id}...")
        archive_job_files(args.job_id, args.name, move=args.move)
