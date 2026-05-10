import json
import os
import csv
from collections import defaultdict
from typing import Any, Dict, List
from pathlib import Path
import job_store


BLOCKING_PAUSE_ERRORS = {
    "all_profiles_exhausted",
    "white_screen_block",
    "captcha_or_manual_intervention",
}


def _dedupe_preserve_order(urls: List[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for url in urls:
        clean = str(url or "").strip()
        if not clean or clean in seen:
            continue
        seen.add(clean)
        out.append(clean)
    return out

def validate_category_discovery(category_job_id: str) -> Dict[str, Any]:
    job = job_store.get_category_job(category_job_id)
    if not job:
        return {"error": f"Job {category_job_id} not found."}

    items = job_store.get_category_items(category_job_id)
    
    out_dir = job.get("output_dir", "")
    pages_dir = os.path.join(out_dir, "pages") if out_dir else ""
    
    page_summaries = []
    total_raw_cards = 0
    total_organic_cards = 0
    total_sponsored_excluded = 0
    
    if pages_dir and os.path.exists(pages_dir):
        for fname in os.listdir(pages_dir):
            if fname.endswith("_summary.json"):
                with open(os.path.join(pages_dir, fname), "r", encoding="utf-8") as f:
                    try:
                        p_sum = json.load(f)
                        page_summaries.append(p_sum)
                        total_raw_cards += p_sum.get("raw_card_count", 0)
                        total_organic_cards += p_sum.get("organic_card_count", 0)
                        total_sponsored_excluded += p_sum.get("excluded_card_count", 0)
                    except:
                        pass
    page_summaries.sort(key=lambda x: x.get("page_number", 0))

    # Base counts
    total_items_in_db = len(items)
    unique_urls = set()
    duplicate_count = 0
    invalid_urls_count = 0
    missing_id_count = 0
    missing_title_count = 0
    missing_price_count = 0
    missing_image_count = 0
    
    # Filter counts
    filtered_in_count = 0
    filtered_out_count = 0
    ambiguous_price_kept_count = 0
    
    status_counts = defaultdict(int)
    
    # Sample lists
    sample_filtered_in = []
    sample_filtered_out = []
    sample_ambiguous = []
    sample_invalid = []
    sample_duplicates = []
    sample_already_extracted = []
    sample_global_duplicates = []
    sample_new_products = []
    
    global_duplicates_found = 0
    already_extracted_count = 0
    skipped_already_extracted_count = 0
    new_products_count = 0
    previously_seen_but_not_extracted_count = 0
    internal_duplicates_count = 0

    import config
    
    csv_rows = []
    discovered_urls = []
    filtered_urls = []

    for item in items:
        url = item.get("product_url", "")
        status = item.get("status", "")
        title = item.get("title", "")
        price_raw = item.get("card_price_raw", "")
        img = item.get("image_url", "")
        pid = item.get("product_id", "")
        
        meta = {}
        try:
            if item.get("metadata_json"):
                meta = json.loads(item.get("metadata_json")) if isinstance(item.get("metadata_json"), str) else item.get("metadata_json")
        except: pass
        
        is_sponsored = meta.get("is_sponsored", False)
        
        if not url.startswith("http") or "chewy.com" not in url:
            invalid_urls_count += 1
            if len(sample_invalid) < 10: sample_invalid.append(url)
            
        if not pid:
            try:
                extracted_pid = url.rstrip("/").split("/")[-1]
                if not extracted_pid.isdigit():
                    missing_id_count += 1
            except:
                missing_id_count += 1
                
        if not title: missing_title_count += 1
        if not price_raw: missing_price_count += 1
        if not img: missing_image_count += 1
        
        status_counts[status] += 1
        discovered_urls.append(url)
        
        if pid in unique_urls or url in unique_urls:
            internal_duplicates_count += 1
            if len(sample_duplicates) < 10: sample_duplicates.append(url)
            continue # Don't count for other global stats
        else:
            if pid: 
                unique_urls.add(pid)
            else:
                unique_urls.add(url)
            
        if pid:
            with job_store.connect() as conn:
                # Need to check if it existed before this job started
                # Wait, discovery count > 1 means it was discovered by another job too
                reg_row = conn.execute("SELECT discovery_count, extraction_status, created_at FROM chewy_product_registry WHERE product_id = ?", (pid,)).fetchone()
            if reg_row:
                # If created_at is older than this job's created_at, it's a global duplicate
                # But we can approximate by checking if discovery_count > 1
                if reg_row[0] > 1:
                    global_duplicates_found += 1
                    if len(sample_global_duplicates) < 10: sample_global_duplicates.append(url)
                    if reg_row[1] == "extracted_success":
                        already_extracted_count += 1
                    else:
                        previously_seen_but_not_extracted_count += 1
                else:
                    new_products_count += 1
                    if len(sample_new_products) < 10: sample_new_products.append(url)
            else:
                new_products_count += 1
                if len(sample_new_products) < 10: sample_new_products.append(url)
            
        if status in ("filtered_in", "discovered"):
            filtered_urls.append(url)
            filtered_in_count += 1
            if item.get("card_price_min") is None:
                ambiguous_price_kept_count += 1
                if len(sample_ambiguous) < 10: sample_ambiguous.append(url)
            else:
                if len(sample_filtered_in) < 10: sample_filtered_in.append(url)
        elif status == "filtered_out":
            filtered_out_count += 1
            if len(sample_filtered_out) < 10: sample_filtered_out.append(url)
        elif status == "duplicate_existing_success":
            skipped_already_extracted_count += 1
            if len(sample_already_extracted) < 10: sample_already_extracted.append(url)
            
        csv_rows.append({
            "product_id": pid,
            "product_url": url,
            "title": title,
            "card_price_raw": price_raw,
            "card_price_min": item.get("card_price_min"),
            "card_price_max": item.get("card_price_max"),
            "status": status,
            "filter_reason": item.get("filter_reason", ""),
            "is_sponsored": is_sponsored,
            "valid_for_pdp_job": status in ("filtered_in", "discovered")
        })

    valid_for_pdp_job_count = filtered_in_count
    not_valid_for_pdp_job_count = total_items_in_db - valid_for_pdp_job_count
    
    dup_rate = internal_duplicates_count / max(1, total_items_in_db)
    miss_id_rate = missing_id_count / max(1, total_items_in_db)
    miss_price_rate = missing_price_count / max(1, total_items_in_db)
    
    score = 100
    warnings = []
    
    if total_items_in_db == 0:
        score -= 20
        warnings.append("No items found in DB.")
        
    stale_pages = [p for p in page_summaries if p.get("page_status") == "stale_repeated_page"]
    if stale_pages:
        warnings.append(f"Found {len(stale_pages)} stale/repeating pages in pagination.")
        score -= 10
        
    if new_products_count == 0 and global_duplicates_found == 0 and total_items_in_db > 0:
        warnings.append("No new products and no global duplicates, registry logic might be failing.")
    
    if valid_for_pdp_job_count == 0:
        score -= 15
        warnings.append("No valid URLs available for PDP extraction.")
        
    if miss_price_rate > 0.50 and job.get("mode") == "card_price_prefilter":
        score -= 10
        warnings.append("High missing price rate while using strict card_price_prefilter.")

    score = max(0, score)
    
    if score >= 90:
        validation_status = "excellent"
    elif score >= 75:
        validation_status = "good"
    elif score >= 50:
        validation_status = "warning"
    else:
        validation_status = "failed"
        
    safe_to_create = score >= 50 and valid_for_pdp_job_count > 0
    
    recs = []
    if safe_to_create:
        recs.append("Có thể tạo PDP extraction job từ filtered_urls.txt.")
    else:
        recs.append("Không nên tạo PDP job ngay. Hãy kiểm tra report.")
        
    disc_path = os.path.join(out_dir, "discovered_urls.txt")
    filt_path = os.path.join(out_dir, "filtered_urls.txt")

    discovered_urls = _dedupe_preserve_order(discovered_urls)
    filtered_urls = _dedupe_preserve_order(filtered_urls)

    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        with open(disc_path, "w", encoding="utf-8") as f:
            f.write("\n".join(discovered_urls))
        with open(filt_path, "w", encoding="utf-8") as f:
            f.write("\n".join(filtered_urls))

    finalized_from_paused = False
    previous_last_error = job.get("last_error")
    if (
        job.get("status") == "paused"
        and total_items_in_db > 0
        and valid_for_pdp_job_count > 0
        and str(previous_last_error or "") not in BLOCKING_PAUSE_ERRORS
    ):
        job_store.update_category_job(
            category_job_id,
            status="completed",
            last_error=f"Auto-finalized by validation. Previous pause reason: {previous_last_error or 'none'}",
        )
        job = job_store.get_category_job(category_job_id) or job
        finalized_from_paused = True
    
    report = {
        "category_job_id": category_job_id,
        "category_url": job.get("category_url", ""),
        "status": job.get("status", ""),
        "created_at": job.get("created_at", ""),
        "validated_at": job_store.utc_now(),
        "price_filter": {
            "price_min": job.get("price_min"),
            "price_max": job.get("price_max"),
            "mode": job.get("mode")
        },
        "summary": {
            "total_raw_cards_found": total_raw_cards,
            "total_organic_cards_found": total_organic_cards,
            "total_sponsored_cards_excluded": total_sponsored_excluded,
            "total_db_items": total_items_in_db,
            "unique_product_urls": len(unique_urls),
            "internal_duplicates_count": internal_duplicates_count,
            "invalid_product_urls": invalid_urls_count,
            "filtered_in_count": filtered_in_count,
            "filtered_out_count": filtered_out_count,
            "ambiguous_price_kept_count": ambiguous_price_kept_count,
            "valid_for_pdp_job_count": valid_for_pdp_job_count,
            "global_duplicates_found": global_duplicates_found,
            "already_extracted_count": already_extracted_count,
            "skipped_already_extracted_count": skipped_already_extracted_count,
            "new_products_count": new_products_count,
            "reprocess_existing_enabled": config.CHEWY_REPROCESS_EXISTING
        },
        "pages": page_summaries,
        "quality": {
            "missing_product_id_count": missing_id_count,
            "duplicate_rate": round(dup_rate, 4),
            "missing_price_rate": round(miss_price_rate, 4)
        },
        "output_files": {
            "discovered_urls_exists": os.path.exists(disc_path),
            "filtered_urls_exists": os.path.exists(filt_path),
            "discovered_urls_count": len(discovered_urls),
            "filtered_urls_count": len(filtered_urls)
        },
        "validation": {
            "validation_score": score,
            "validation_status": validation_status,
            "safe_to_create_pdp_job": safe_to_create,
            "finalized_from_paused": finalized_from_paused,
            "previous_last_error": previous_last_error,
            "warnings": warnings,
            "recommendations": recs
        }
    }
    
    if out_dir:
        with open(os.path.join(out_dir, "category_validation_report.json"), "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)
            
        with open(os.path.join(out_dir, "category_validation_items.csv"), "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=[
                "product_id", "product_url", "title", "card_price_raw", 
                "card_price_min", "card_price_max", "status", "filter_reason", 
                "is_sponsored", "valid_for_pdp_job"
            ])
            writer.writeheader()
            writer.writerows(csv_rows)
            
    return report

def print_validation_report(report: Dict[str, Any]) -> None:
    print("==================================================")
    print("CATEGORY DISCOVERY VALIDATION REPORT")
    print(f"Job ID: {report['category_job_id']}")
    print(f"Category URL: {report['category_url']}")
    
    print("\nPer-page:")
    for p in report.get('pages', []):
        print(f"Page {p['page_number']}: raw={p['raw_card_count']}, sponsored={p['sponsored_card_count']}, organic={p['organic_card_count']}, unique={p['unique_product_urls_on_page']}, new={p['new_urls_added_on_page']}")
    
    print("\nSummary:")
    s = report['summary']
    print(f"- Total raw cards: {s['total_raw_cards_found']}")
    print(f"- Total organic cards: {s['total_organic_cards_found']}")
    print(f"- Sponsored excluded: {s['total_sponsored_cards_excluded']}")
    print(f"- Unique PDP URLs: {s['unique_product_urls']}")
    print(f"- Filtered in: {s['filtered_in_count']}")
    print(f"- Filtered out by price: {s['filtered_out_count']}")
    print(f"- Internal duplicates: {s['internal_duplicates_count']}")
    print(f"- Global duplicates: {s['global_duplicates_found']}")
    print(f"- Already extracted: {s['already_extracted_count']}")
    print(f"- New products: {s['new_products_count']}")
    print("==================================================")
