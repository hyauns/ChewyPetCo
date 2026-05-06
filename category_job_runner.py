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

def create_category_discovery_job(name: str, url: str, price_min: float | None = None, price_max: float | None = None, mode: str = "hybrid") -> str:
    return job_store.create_category_job(
        name=name,
        category_url=url,
        price_min=price_min,
        price_max=price_max,
        mode=mode
    )

async def run_category_discovery_job(category_job_id: str, max_pages: int | None = None):
    job = job_store.get_category_job(category_job_id)
    if not job:
        print(f"Job not found: {category_job_id}")
        return
        
    await category_discovery.discover_category_products(
        category_job_id=category_job_id,
        category_url=job["category_url"],
        price_min=job["price_min"],
        price_max=job["price_max"],
        mode=job["mode"],
        max_pages=max_pages
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
        
    pdp_job_id = job_store.create_job(
        name=f"PDP extraction from category {job['name'] or category_job_id}",
        urls=urls,
        mode=pdp_mode,
        notes=f"Source category_job_id: {category_job_id}"
    )
    
    print(f"Created PDP job {pdp_job_id} with {len(urls)} URLs.")
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
    
    validate_p = subparsers.add_parser("validate")
    validate_p.add_argument("--category-job-id", required=True)
    
    status_p = subparsers.add_parser("status")
    status_p.add_argument("--category-job-id", required=True)
    
    args = parser.parse_args()
    
    if args.command == "create":
        jid = create_category_discovery_job(args.name, args.category_url, args.price_min, args.price_max, args.mode)
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
