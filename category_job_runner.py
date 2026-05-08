import argparse
import asyncio
import os
import sys
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except AttributeError:
        pass
from pathlib import Path
import job_store
import category_discovery

def create_category_discovery_job(name: str, url: str, price_min: float | None = None, price_max: float | None = None, mode: str = "hybrid", start_page: int = 1, max_pages: int | None = None) -> str:
    return job_store.create_category_job(
        name=name,
        category_url=url,
        price_min=price_min,
        price_max=price_max,
        mode=mode,
        start_page=start_page,
        max_pages=max_pages
    )

async def run_category_discovery_job(category_job_id: str, max_pages: int | None = None):
    job = job_store.get_category_job(category_job_id)
    if not job:
        print(f"Job not found: {category_job_id}")
        return
        
    actual_max_pages = max_pages
    if actual_max_pages is None:
        actual_max_pages = job.get("max_pages")
    
    await category_discovery.discover_category_products(
        category_job_id=category_job_id,
        category_url=job["category_url"],
        price_min=job["price_min"],
        price_max=job["price_max"],
        mode=job["mode"],
        max_pages=actual_max_pages
    )
    import category_discovery_validation
    report = category_discovery_validation.validate_category_discovery(category_job_id)
    category_discovery_validation.print_validation_report(report)

def create_pdp_job_from_discovery(category_job_id: str, pdp_mode: str = "json_extractor_with_fallback", force: bool = False) -> str | None:
    import category_discovery_validation
    job = job_store.get_category_job(category_job_id)
    if not job:
        print(f"Category job not found: {category_job_id}")
        return None
        
    report_path = os.path.join(job["output_dir"], "category_validation_report.json")
    if not os.path.exists(report_path):
        print("Validation report missing. Running validation now...")
        report = category_discovery_validation.validate_category_discovery(category_job_id)
    else:
        import json
        with open(report_path, "r", encoding="utf-8") as f:
            report = json.load(f)
            
    if not force and not report.get("validation", {}).get("safe_to_create_pdp_job", False):
        print("Validation score is too low. Use --force to create PDP job anyway.")
        return None
        
    filtered_urls_path = os.path.join(job["output_dir"], "filtered_urls.txt")
    if not os.path.exists(filtered_urls_path):
        print(f"Cannot find filtered_urls.txt at {filtered_urls_path}")
        return None
        
    with open(filtered_urls_path, "r", encoding="utf-8") as f:
        urls = [line.strip() for line in f if line.strip()]
        
    if not urls:
        print("No URLs found in filtered_urls.txt to create PDP job.")
        return None
        
    import config
    final_urls = []
    seen_products = set()
    skipped_count = 0
    duplicate_count = 0
    
    for u in urls:
        pid = job_store.extract_chewy_product_id(u)
        if config.CHEWY_DEDUP_BY_PRODUCT_ID and pid:
            if pid in seen_products:
                duplicate_count += 1
                continue
            seen_products.add(pid)
            
        if config.CHEWY_GLOBAL_DEDUP_ENABLED and pid and not config.CHEWY_REPROCESS_EXISTING:
            with job_store.connect() as conn:
                reg_row = conn.execute("SELECT extraction_status FROM chewy_product_registry WHERE product_id = ?", (pid,)).fetchone()
            if reg_row and reg_row[0] == "extracted_success":
                skipped_count += 1
                continue
                
        final_urls.append(u)
        
    if not final_urls:
        print(f"All {len(urls)} URLs were skipped (already extracted or duplicates). No PDP job created.")
        return None
        
    pdp_job_id = job_store.create_job(
        name=f"PDP extraction from category {job['name'] or category_job_id}",
        urls=final_urls,
        mode=pdp_mode,
        notes=f"Source category_job_id: {category_job_id}. Skipped {skipped_count} already extracted. Removed {duplicate_count} duplicates."
    )
    
    print(f"Created PDP job {pdp_job_id} with {len(final_urls)} URLs (Skipped {skipped_count} existing, {duplicate_count} internal duplicates).")
    return pdp_job_id

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    
    create_p = subparsers.add_parser("create")
    create_p.add_argument("--name", required=True)
    create_p.add_argument("--category-url", required=True)
    create_p.add_argument("--price-min", type=float)
    create_p.add_argument("--price-max", type=float)
    create_p.add_argument("--mode", default="hybrid", choices=["hybrid", "card_price_prefilter", "pdp_variant_filter"])
    create_p.add_argument("--start-page", type=int, default=1, help="Page number to start discovery from")
    create_p.add_argument("--max-pages", type=int, help="Optional max pages to discover")
    create_p.add_argument("--reprocess-existing", action="store_true", help="Force reprocessing of already extracted products")
    
    start_p = subparsers.add_parser("start")
    start_p.add_argument("--category-job-id", required=True)
    start_p.add_argument("--max-pages", type=int)
    
    resume_p = subparsers.add_parser("resume")
    resume_p.add_argument("--category-job-id", required=True)
    resume_p.add_argument("--max-pages", type=int)
    
    create_pdp_p = subparsers.add_parser("create-pdp-job")
    create_pdp_p.add_argument("--category-job-id", required=True)
    create_pdp_p.add_argument("--mode", default="json_extractor_with_fallback")
    create_pdp_p.add_argument("--force", action="store_true", help="Force create even if validation score is low")
    create_pdp_p.add_argument("--reprocess-existing", action="store_true", help="Force reprocessing of already extracted products")
    
    validate_p = subparsers.add_parser("validate")
    validate_p.add_argument("--category-job-id", required=True)
    
    status_p = subparsers.add_parser("status")
    status_p.add_argument("--category-job-id", required=True)
    
    subparsers.add_parser("registry-show").add_argument("--product-id", required=True)
    subparsers.add_parser("registry-search").add_argument("--status", required=True)
    
    args = parser.parse_args()
    
    import config
    if hasattr(args, "reprocess_existing") and args.reprocess_existing:
        config.CHEWY_REPROCESS_EXISTING = True
    
    if args.command == "create":
        jid = create_category_discovery_job(args.name, args.category_url, args.price_min, args.price_max, args.mode, args.start_page, args.max_pages)
        print(f"Created category job: {jid}")
    elif args.command in ("start", "resume"):
        asyncio.run(run_category_discovery_job(args.category_job_id, args.max_pages))
    elif args.command == "create-pdp-job":
        create_pdp_job_from_discovery(args.category_job_id, args.mode, args.force)
    elif args.command == "validate":
        import category_discovery_validation
        report = category_discovery_validation.validate_category_discovery(args.category_job_id)
        category_discovery_validation.print_validation_report(report)
    elif args.command == "status":
        job = job_store.get_category_job(args.category_job_id)
        if job:
            import json
            print(json.dumps(job, indent=2))
        else:
            print("Not found.")
    elif args.command == "registry-show":
        with job_store.connect() as conn:
            row = conn.execute("SELECT * FROM chewy_product_registry WHERE product_id = ?", (args.product_id,)).fetchone()
            if row:
                import json
                print(json.dumps(job_store.row_to_dict(row), indent=2))
            else:
                print(f"Product {args.product_id} not found in registry.")
    elif args.command == "registry-search":
        with job_store.connect() as conn:
            rows = conn.execute("SELECT product_id, latest_url, extraction_status FROM chewy_product_registry WHERE extraction_status = ? LIMIT 50", (args.status,)).fetchall()
            for r in rows:
                print(f"{r[0]} | {r[1]} | {r[2]}")
            print(f"Showing up to 50 results for status '{args.status}'")
