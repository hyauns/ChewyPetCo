import os
import json
import csv

NORMALIZED_DIR = r"c:\Users\admin\Documents\Scraper\Pet\output\normalized_products"
REPORTS_DIR = r"c:\Users\admin\Documents\Scraper\Pet\audit_reports"

os.makedirs(REPORTS_DIR, exist_ok=True)

stats = {
    "total_source_products": 0,
    "total_normalized_products": 0,
    "total_variants": 0,
    "complete_count": 0,
    "needs_variant_api_enrichment_count": 0,
    "needs_slug_resolution_count": 0,
    "needs_full_rescrape_count": 0,
    "needs_manual_review_count": 0,
    "blocked_bad_public_content_count": 0,
    "missing_ingredients_count": 0,
    "missing_guaranteed_analysis_count": 0,
    "missing_feeding_instructions_count": 0,
    "missing_calorie_content_count": 0,
    "missing_variant_id_count": 0,
    "missing_price_count": 0,
    "missing_image_count": 0,
    "invalid_gtin_count": 0,
    "wrong_flavor_content_count": 0,
    "empty_products_count": 0,
    "unsupported_architecture_count": 0,
}

products_complete = []
needs_variant_api_enrichment = []
needs_slug_resolution = []
needs_full_rescrape = []
needs_manual_review = []
blocked_bad_public_content = []

field_missing_matrix = []
product_issues = []

for filename in os.listdir(NORMALIZED_DIR):
    if not filename.endswith(".json") and not filename.endswith(".jsonl"):
        continue
        
    filepath = os.path.join(NORMALIZED_DIR, filename)
    with open(filepath, 'r', encoding='utf-8') as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError:
            continue
            
    # Always process as a list of items
    items = data if isinstance(data, list) else [data]

    for item in items:
        stats["total_source_products"] += 1
        
        source_product_id = item.get("source_product_id")
        source_url = item.get("source_url")
        architecture = item.get("architecture")
        
        # Check architecture
        if architecture not in ["next_json", "apollo", "nextjs", "redux"]:
            stats["unsupported_architecture_count"] += 1
            stats["needs_manual_review_count"] += 1
            needs_manual_review.append({"source_product_id": source_product_id, "url": source_url, "reason": "unsupported architecture"})
            continue
            
        if not source_product_id or "variants" not in item:
            stats["needs_full_rescrape_count"] += 1
            needs_full_rescrape.append({"source_product_id": source_product_id, "url": source_url})
            continue

        products_array = item.get("products")
        if products_array is not None and len(products_array) == 0:
            stats["empty_products_count"] += 1
            products_to_check = []
        elif products_array is None:
            products_to_check = [item]
        else:
            products_to_check = products_array
            
        is_blocked = False
        needs_slug = False
        needs_enrichment = False
        
        issue_count = 0
        
        matrix_row = {
            "source_product_id": source_product_id,
            "url": source_url,
            "missing_ingredients": 0,
            "missing_ga": 0,
            "missing_feeding": 0,
            "missing_calorie": 0
        }
        
        is_food_global = False
        
        for p in products_to_check:
            stats["total_normalized_products"] += 1
            variants = p.get("variants", [])
            stats["total_variants"] += len(variants)
            
            # Simple heuristic: if any product has food fields, consider the source product a food item
            is_food = bool(p.get("ingredients") or p.get("guaranteed_analysis") or p.get("calorie_content") or p.get("feeding_instructions"))
            if is_food:
                is_food_global = True
                
            flavor = p.get("flavor")
            import_ready = p.get("import_ready")
            public_content_safe = p.get("public_content_safe")
            
            # Data checks
            if not p.get("ingredients") and is_food:
                stats["missing_ingredients_count"] += 1
                matrix_row["missing_ingredients"] = 1
                issue_count += 1
                
            if not p.get("guaranteed_analysis") and is_food:
                stats["missing_guaranteed_analysis_count"] += 1
                matrix_row["missing_ga"] = 1
                issue_count += 1
                
            feeding_instructions = p.get("feeding_instructions")
            has_feeding = False
            if feeding_instructions:
                if isinstance(feeding_instructions, dict):
                    has_feeding = bool(feeding_instructions.get("summary") or feeding_instructions.get("raw"))
                else:
                    has_feeding = bool(str(feeding_instructions).strip())

            if not has_feeding and is_food:
                stats["missing_feeding_instructions_count"] += 1
                matrix_row["missing_feeding"] = 1
                issue_count += 1
                
            if not p.get("calorie_content") and is_food:
                stats["missing_calorie_content_count"] += 1
                matrix_row["missing_calorie"] = 1
                issue_count += 1
                
            # Content source checks
            content_source = p.get("content_source", {})
            if content_source.get("reason") == "all_candidates_failed_or_empty":
                needs_slug = True
            elif content_source.get("confidence") == "missing":
                needs_enrichment = True
                
            # Blocked checks
            warnings = p.get("warnings", [])
            for w in warnings:
                if "wrong flavor" in w.lower() or "mismatch" in w.lower():
                    is_blocked = True
                    stats["wrong_flavor_content_count"] += 1
                    
            if import_ready and public_content_safe is False:
                is_blocked = True
                
            for v in variants:
                if not v.get("source_variant_id"):
                    stats["missing_variant_id_count"] += 1
                if not v.get("price"):
                    stats["missing_price_count"] += 1
                if not v.get("image") and not p.get("images"):
                    stats["missing_image_count"] += 1
                if v.get("gtin") and "-DISC" in v.get("gtin"):
                    stats["invalid_gtin_count"] += 1
                elif v.get("raw_gtin") and "-DISC" in v.get("raw_gtin"):
                    stats["invalid_gtin_count"] += 1
                    
        field_missing_matrix.append(matrix_row)
        product_issues.append({"id": source_product_id, "issues": issue_count, "url": source_url})
        
        # Classification Rules
        if is_blocked:
            stats["blocked_bad_public_content_count"] += 1
            blocked_bad_public_content.append({"source_product_id": source_product_id, "url": source_url})
        elif products_array is not None and len(products_array) == 0:
            stats["needs_manual_review_count"] += 1
            needs_manual_review.append({"source_product_id": source_product_id, "url": source_url, "reason": "empty products array"})
        elif needs_slug:
            stats["needs_slug_resolution_count"] += 1
            needs_slug_resolution.append({"source_product_id": source_product_id, "url": source_url})
        elif needs_enrichment or (matrix_row["missing_ingredients"] and is_food_global):
            stats["needs_variant_api_enrichment_count"] += 1
            needs_variant_api_enrichment.append({"source_product_id": source_product_id, "url": source_url})
        else:
            stats["complete_count"] += 1
            products_complete.append({"source_product_id": source_product_id, "url": source_url})

# Save reports
with open(os.path.join(REPORTS_DIR, "audit_summary.json"), "w") as f:
    json.dump(stats, f, indent=2)

with open(os.path.join(REPORTS_DIR, "audit_summary.csv"), "w", newline="") as f:
    writer = csv.writer(f)
    for k, v in stats.items():
        writer.writerow([k, v])

def write_jsonl(filename, data):
    with open(os.path.join(REPORTS_DIR, filename), "w") as f:
        for item in data:
            f.write(json.dumps(item) + "\n")

write_jsonl("products_complete.jsonl", products_complete)
write_jsonl("needs_variant_api_enrichment.jsonl", needs_variant_api_enrichment)
write_jsonl("needs_slug_resolution.jsonl", needs_slug_resolution)
write_jsonl("needs_full_rescrape.jsonl", needs_full_rescrape)
write_jsonl("needs_manual_review.jsonl", needs_manual_review)
write_jsonl("blocked_bad_public_content.jsonl", blocked_bad_public_content)

with open(os.path.join(REPORTS_DIR, "field_missing_matrix.csv"), "w", newline="") as f:
    if field_missing_matrix:
        writer = csv.DictWriter(f, fieldnames=field_missing_matrix[0].keys())
        writer.writeheader()
        writer.writerows(field_missing_matrix)

# Top 20 worst products
product_issues.sort(key=lambda x: x["issues"], reverse=True)
print("=== TOP 20 WORST PRODUCTS BY MISSING FIELDS ===")
for p in product_issues[:20]:
    print(f"ID: {p['id']}, URL: {p['url']}, Issues: {p['issues']}")

print("\n=== RECOMMENDATION ===")
print(f"Total Source Products: {stats['total_source_products']}")
print(f"Complete as-is: {stats['complete_count']}")
print(f"Needs API Enrichment: {stats['needs_variant_api_enrichment_count']}")
print(f"Needs Slug Resolution: {stats['needs_slug_resolution_count']}")
print(f"Needs Full Rescrape: {stats['needs_full_rescrape_count']}")

if stats['blocked_bad_public_content_count'] > 0:
    print(f"\nWARNING: {stats['blocked_bad_public_content_count']} products have blocked bad public content! Do not run backfill yet.")
else:
    print("\nIt is mostly safe to run a backfill job for complete products.")
