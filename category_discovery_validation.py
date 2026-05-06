import json
import os
import csv
from collections import defaultdict
from typing import Any, Dict, List
from pathlib import Path
import job_store

def validate_category_discovery(category_job_id: str) -> Dict[str, Any]:
    job = job_store.get_category_job(category_job_id)
    if not job:
        return {"error": f"Job {category_job_id} not found."}

    items = job_store.get_category_items(category_job_id)
    
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
    
    # Detailed CSV rows
    csv_rows = []

    for item in items:
        url = item.get("product_url", "")
        status = item.get("status", "")
        title = item.get("title", "")
        price_raw = item.get("card_price_raw", "")
        img = item.get("image_url", "")
        pid = item.get("product_id", "")
        
        # Determine valid/invalid
        if not url.startswith("http") or "chewy.com" not in url:
            invalid_urls_count += 1
            if len(sample_invalid) < 10: sample_invalid.append(url)
            
        if not pid:
            # We don't have product_id parsed directly in extraction yet usually, but maybe we do.
            # If not parsed, we can try to extract from url
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
        
        if url in unique_urls:
            duplicate_count += 1
            if len(sample_duplicates) < 10: sample_duplicates.append(url)
        else:
            unique_urls.add(url)
            
        if status in ("filtered_in", "discovered"):
            filtered_in_count += 1
            if item.get("card_price_min") is None:
                ambiguous_price_kept_count += 1
                if len(sample_ambiguous) < 10: sample_ambiguous.append(url)
            else:
                if len(sample_filtered_in) < 10: sample_filtered_in.append(url)
        elif status == "filtered_out":
            filtered_out_count += 1
            if len(sample_filtered_out) < 10: sample_filtered_out.append(url)
            
        csv_rows.append({
            "product_id": pid,
            "product_url": url,
            "title": title,
            "card_price_raw": price_raw,
            "card_price_min": item.get("card_price_min"),
            "card_price_max": item.get("card_price_max"),
            "status": status,
            "filter_reason": item.get("filter_reason", ""),
            "validation_issue": "duplicate" if url in unique_urls and csv_rows and any(r["product_url"]==url for r in csv_rows) else "",
            "valid_for_pdp_job": status in ("filtered_in", "discovered")
        })

    # Calculations
    total_cards_found = job.get("total_cards_found", 0) or total_items_in_db
    valid_for_pdp_job_count = filtered_in_count
    not_valid_for_pdp_job_count = total_items_in_db - valid_for_pdp_job_count
    
    dup_rate = duplicate_count / max(1, total_items_in_db)
    miss_id_rate = missing_id_count / max(1, total_items_in_db)
    miss_price_rate = missing_price_count / max(1, total_items_in_db)
    miss_img_rate = missing_image_count / max(1, total_items_in_db)
    miss_title_rate = missing_title_count / max(1, total_items_in_db)
    
    # Score logic
    score = 100
    warnings = []
    
    if total_items_in_db == 0:
        score -= 20
        warnings.append("No items found in DB.")
    
    if valid_for_pdp_job_count == 0:
        score -= 15
        warnings.append("No valid URLs available for PDP extraction.")
        
    if dup_rate > 0.20:
        score -= 10
        warnings.append(f"High duplicate rate: {dup_rate:.1%}")
        
    if miss_id_rate > 0.10:
        score -= 10
        warnings.append(f"High missing product ID rate: {miss_id_rate:.1%}")
        
    if miss_title_rate > 0.20:
        score -= 5
        warnings.append(f"High missing title rate: {miss_title_rate:.1%}")
        
    if miss_img_rate > 0.50:
        score -= 5
        warnings.append(f"High missing image rate: {miss_img_rate:.1%}")
        
    if miss_price_rate > 0.50 and job.get("mode") == "card_price_prefilter":
        score -= 10
        warnings.append("High missing price rate while using strict card_price_prefilter.")
        
    if job.get("mode") == "card_price_prefilter" and ambiguous_price_kept_count > (total_items_in_db * 0.3):
        score -= 10
        warnings.append("Many ambiguous prices in strict mode (not effectively filtering).")

    score = max(0, score)
    
    if score >= 90:
        validation_status = "excellent"
    elif score >= 75:
        validation_status = "good"
    elif score >= 50:
        validation_status = "warning"
    elif score >= 25:
        validation_status = "poor"
    else:
        validation_status = "failed"
        
    safe_to_create = score >= 50 and valid_for_pdp_job_count > 0
    
    # Recommendations
    recs = []
    if total_items_in_db == 0:
        recs.append("Không tìm thấy product URLs. Hãy kiểm tra Category URL, AdsPower/browser session, hoặc thử giảm blocking.")
    if duplicate_count > 0 and dup_rate > 0.20:
        recs.append("Phát hiện nhiều URL trùng. Dedupe đã xử lý, nhưng nên kiểm tra pagination/category selector.")
    if miss_price_rate > 0.50:
        if job.get("mode") == "hybrid":
            recs.append("Nhiều sản phẩm thiếu giá trên card. Hybrid mode vẫn giữ lại để kiểm tra giá chính xác ở bước PDP.")
        elif job.get("mode") == "card_price_prefilter":
            recs.append("Nhiều sản phẩm thiếu giá nhưng đang dùng strict prefilter. Có thể bị loại nhầm sản phẩm tốt. Nên chuyển sang hybrid.")
    if valid_for_pdp_job_count == 0:
        recs.append("Không có URL nào sau bộ lọc giá. Hãy giảm price_min hoặc chuyển sang hybrid/pdp_variant_filter.")
    elif safe_to_create:
        recs.append("Có thể tạo PDP extraction job từ filtered_urls.txt.")
    else:
        recs.append("Không nên tạo PDP job ngay. Hãy kiểm tra report và chạy lại category discovery.")
        
    # Check files
    out_dir = job.get("output_dir", "")
    disc_path = os.path.join(out_dir, "discovered_urls.txt")
    filt_path = os.path.join(out_dir, "filtered_urls.txt")
    
    disc_exists = os.path.exists(disc_path)
    filt_exists = os.path.exists(filt_path)
    
    # Construct report
    report = {
        "category_job_id": category_job_id,
        "category_url": job.get("category_url", ""),
        "created_at": job.get("created_at", ""),
        "validated_at": job_store.utc_now(),
        "price_filter": {
            "price_min": job.get("price_min"),
            "price_max": job.get("price_max"),
            "mode": job.get("mode")
        },
        "summary": {
            "total_cards_found": total_cards_found,
            "unique_product_urls": len(unique_urls),
            "duplicate_product_urls": duplicate_count,
            "invalid_product_urls": invalid_urls_count,
            "filtered_in_count": filtered_in_count,
            "filtered_out_count": filtered_out_count,
            "ambiguous_price_kept_count": ambiguous_price_kept_count,
            "needs_pdp_price_check_count": ambiguous_price_kept_count,
            "valid_for_pdp_job_count": valid_for_pdp_job_count,
            "not_valid_for_pdp_job_count": not_valid_for_pdp_job_count
        },
        "quality": {
            "missing_product_id_count": missing_id_count,
            "missing_title_count": missing_title_count,
            "missing_price_count": missing_price_count,
            "missing_image_count": missing_image_count,
            "duplicate_rate": round(dup_rate, 4),
            "missing_price_rate": round(miss_price_rate, 4),
            "missing_product_id_rate": round(miss_id_rate, 4)
        },
        "output_files": {
            "discovered_urls_exists": disc_exists,
            "filtered_urls_exists": filt_exists,
            "discovered_urls_count": total_items_in_db,
            "filtered_urls_count": filtered_in_count
        },
        "validation": {
            "validation_score": score,
            "validation_status": validation_status,
            "safe_to_create_pdp_job": safe_to_create,
            "warnings": warnings,
            "recommendations": recs
        },
        "sample_items": {
            "filtered_in": sample_filtered_in,
            "filtered_out": sample_filtered_out,
            "ambiguous_price": sample_ambiguous,
            "invalid": sample_invalid,
            "duplicates": sample_duplicates
        }
    }
    
    # Save Report JSON
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        report_path = os.path.join(out_dir, "category_validation_report.json")
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)
            
        csv_path = os.path.join(out_dir, "category_validation_items.csv")
        with open(csv_path, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=[
                "product_id", "product_url", "title", "card_price_raw", 
                "card_price_min", "card_price_max", "status", "filter_reason", 
                "validation_issue", "valid_for_pdp_job"
            ])
            writer.writeheader()
            writer.writerows(csv_rows)
            
    return report

def print_validation_report(report: Dict[str, Any]) -> None:
    print("==================================================")
    print("CATEGORY DISCOVERY VALIDATION REPORT")
    print(f"Job ID: {report['category_job_id']}")
    print(f"Category URL: {report['category_url']}")
    
    pf = report['price_filter']
    p_min = pf['price_min']
    p_max = pf['price_max']
    min_str = f"min=${p_min}" if p_min is not None else "no_min"
    max_str = f", max=${p_max}" if p_max is not None else ""
    print(f"Price Filter: {pf['mode']}, {min_str}{max_str}")
    print("\nSummary:")
    s = report['summary']
    print(f"- Total cards found: {s['total_cards_found']}")
    print(f"- Unique PDP URLs: {s['unique_product_urls']}")
    print(f"- Duplicates removed: {s['duplicate_product_urls']}")
    print(f"- Filtered in: {s['filtered_in_count']}")
    print(f"- Filtered out by price: {s['filtered_out_count']}")
    print(f"- Ambiguous price kept: {s['ambiguous_price_kept_count']}")
    print(f"- Invalid URLs: {s['invalid_product_urls']}")
    
    q = report['quality']
    print(f"- Missing product ID: {q['missing_product_id_count']}")
    
    v = report['validation']
    print("\nQuality:")
    print(f"- Validation score: {v['validation_score']}/100")
    print(f"- Status: {v['validation_status']}")
    print(f"- Safe to create PDP job: {'yes' if v['safe_to_create_pdp_job'] else 'no'}")
    
    if v['warnings']:
        print("\nWarnings:")
        for w in v['warnings']:
            print(f"- {w}")
            
    print("\nRecommendation:")
    for r in v['recommendations']:
        print(f"- {r}")
        
    print("\nFiles:")
    print("- category_validation_report.json")
    print("- category_validation_items.csv")
    print("- filtered_urls.txt")
    print("==================================================")
    
    if not v['safe_to_create_pdp_job']:
        print("\nKhông nên tạo PDP job ngay. Hãy kiểm tra report trước.")
