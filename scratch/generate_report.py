import json
import re
from collections import defaultdict
from pathlib import Path

RESULT_FILE = Path("c:/Users/admin/Documents/Scraper/Pet/output/enrichment_runs/result_batch_all_20260515_030102.json")
REPORT_FILE = Path("c:/Users/admin/Documents/Scraper/Pet/test_runs/latest_manual_check_report.md")

def has_real_images(img_list):
    if not img_list:
        return False
    return any(isinstance(i, str) and "moe/" not in i and i.strip() for i in img_list)

def extract_data():
    with open(RESULT_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    rows = []
    
    # Pre-compute group sizes (how many flavors per source product)
    source_counts = defaultdict(int)
    for group in data:
        for p in group.get("products", []):
            source_counts[p.get("source_product_id")] += 1

    for group in data:
        for p in group.get("products", []):
            source_id = p.get("source_product_id")
            source_group_id = p.get("source_group_id", "")
            if not source_id and source_group_id:
                source_id = source_group_id.split(":")[0]
            title = p.get("title", "")
            flavor = p.get("flavor", "")
            source_url = group.get("source_url", p.get("source_url", ""))
            
            variants = p.get("variants", [])
            variant_url = variants[0].get("variant_url", "") if variants else ""
            
            debug = p.get("debug", {})
            content_source_type = p.get("content_source", {}).get("type") if p.get("content_source") else "parent"
            
            # Text fields
            desc = p.get("description", "")
            ingr = p.get("ingredients", "")
            ga = p.get("guaranteed_analysis", "")
            
            rejected_content = debug.get("rejected_content", {})
            
            # Enrich Status
            if content_source_type == "variant_api":
                if not desc or not ingr or not ga:
                    enrich_status = "partial"
                else:
                    enrich_status = "enriched"
            else:
                if desc and ingr and ga and p.get("public_content_safe"):
                    enrich_status = "not_needed"
                elif desc or ingr or ga:
                    enrich_status = "partial"
                else:
                    enrich_status = "not_enriched"
                    
            # Ingredients Status
            if ingr:
                if content_source_type == "parent":
                    ingredients_status = "generic_safe"
                else:
                    ingredients_status = "has_ingredients"
            elif "ingredients" in rejected_content:
                ingredients_status = "rejected"
            else:
                ingredients_status = "missing"
                
            # GA status
            if ga:
                ga_status = "has_ga"
            elif "guaranteed_analysis" in rejected_content:
                ga_status = "rejected"
            else:
                ga_status = "missing"
                
            # Image Status
            p_imgs = p.get("images", [])
            v_imgs = []
            for v in variants:
                v_imgs.extend(v.get("images", []))
            
            all_imgs = p_imgs + v_imgs
            if has_real_images(all_imgs):
                image_status = "has_real_image"
            elif all_imgs:
                image_status = "moe_placeholder"
            else:
                image_status = "missing_image"
                
            import_mode = p.get("import_mode", "unknown")
            
            # Unenriched reason
            reason = debug.get("rejected_reason", "")
            warnings = debug.get("parser_warnings", []) + p.get("warnings", [])
            if not reason:
                for w in warnings:
                    if w in ["wrong_product_api_rejected", "slug_mismatch", "all_candidates_failed_or_empty", "missing_from_source", "no_variant_specific_content"]:
                        reason = w
                        break
            if not reason and enrich_status == "not_enriched":
                if "parent_content_not_applicable_to_flavor" in warnings:
                    reason = "parent_content_not_applicable_to_flavor"
                else:
                    reason = "unknown"
                    
            if enrich_status != "not_enriched":
                reason = "-"
            
            # Calculate Priority
            priority = 4
            if enrich_status == "not_enriched" and source_counts[source_id] > 1:
                priority = 1
            elif enrich_status == "partial":
                priority = 2
            elif image_status in ["missing_image", "moe_placeholder"]:
                priority = 3
            elif import_mode in ["safe_to_import", "safe_with_warnings"]:
                priority = 4
            else:
                priority = 4 # default

            # recommended_manual_check
            rec_check = "YES" if priority <= 2 else "NO"
            
            rows.append({
                "source_product_id": source_id,
                "source_group_id": source_group_id,
                "title": title,
                "flavor": flavor,
                "source_url": source_url,
                "variant_url": variant_url,
                "enrich_status": enrich_status,
                "content_source_type": content_source_type,
                "ingredients_status": ingredients_status,
                "ga_status": ga_status,
                "image_status": image_status,
                "import_mode": import_mode,
                "reason": reason,
                "priority": priority,
                "rec_check": rec_check
            })
            
    return rows, len(data)

def generate_markdown(rows, num_source_products):
    # Sort rows by priority
    rows.sort(key=lambda x: (x["priority"], x["source_product_id"]))
    
    total_flavor_groups = len(rows)
    enriched = sum(1 for r in rows if r["enrich_status"] == "enriched")
    not_enriched = sum(1 for r in rows if r["enrich_status"] == "not_enriched")
    partial = sum(1 for r in rows if r["enrich_status"] == "partial")
    manual_review = sum(1 for r in rows if r["import_mode"] == "needs_manual_review")
    
    md = [
        "# Manual Check Report: Batch of 11 Products\n",
        "## Product Table\n",
        "| Source ID | Group ID | Title | Flavor | Source/Variant URL | Enrich Status | Source Type | Ingredients | GA | Images | Import Mode | Reason | Check? |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|"
    ]
    
    for r in rows:
        url_col = f"[Source]({r['source_url']})"
        if r['variant_url']:
            url_col += f"<br>[Variant]({r['variant_url']})"
            
        # Clean title
        title = str(r["title"] or "").replace("|", "-")
        flavor = str(r["flavor"] or "").replace("|", "-")
        reason = str(r["reason"]).replace("|", "-")
        
        row_str = f"| {r['source_product_id']} | {r['source_group_id']} | {title} | {flavor} | {url_col} | {r['enrich_status']} | {r['content_source_type']} | {r['ingredients_status']} | {r['ga_status']} | {r['image_status']} | {r['import_mode']} | {reason} | **{r['rec_check']}** |"
        md.append(row_str)
        
    md.append("\n## Summary\n")
    md.append(f"- **Tổng số source products**: {num_source_products}")
    md.append(f"- **Tổng số flavor groups**: {total_flavor_groups}")
    md.append(f"- **Enriched**: {enriched}")
    md.append(f"- **Not Enriched**: {not_enriched}")
    md.append(f"- **Partial**: {partial}")
    md.append(f"- **Manual Review Needed**: {manual_review}")
    
    md.append("\n## Top 5 Links Cần Kiểm Tra Đầu Tiên\n")
    top_5 = [r for r in rows if r["priority"] <= 2][:5]
    if not top_5:
        top_5 = rows[:5]
        
    for idx, r in enumerate(top_5, 1):
        md.append(f"{idx}. **{r['source_group_id']}** (Flavor: {r['flavor']}) - {r['enrich_status']} ({r['reason']})")
        md.append(f"   - Variant URL: {r['variant_url'] or r['source_url']}")
        
    REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        f.write("\\n".join(md))

def generate_html(rows, num_source_products):
    HTML_FILE = REPORT_FILE.with_suffix('.html')
    
    # Sort rows by priority
    rows.sort(key=lambda x: (x["priority"], x["source_product_id"]))
    
    total_flavor_groups = len(rows)
    enriched = sum(1 for r in rows if r["enrich_status"] == "enriched")
    not_enriched = sum(1 for r in rows if r["enrich_status"] == "not_enriched")
    partial = sum(1 for r in rows if r["enrich_status"] == "partial")
    manual_review = sum(1 for r in rows if r["import_mode"] == "needs_manual_review")
    
    html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>Manual Check Report</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; margin: 20px; line-height: 1.6; color: #333; }}
        h1, h2 {{ border-bottom: 1px solid #eaecef; padding-bottom: 0.3em; }}
        table {{ border-collapse: collapse; width: 100%; margin-bottom: 20px; font-size: 14px; }}
        th, td {{ border: 1px solid #ddd; padding: 8px; text-align: left; }}
        th {{ background-color: #f6f8fa; font-weight: 600; }}
        tr:nth-child(even) {{ background-color: #f9f9f9; }}
        .priority-1 {{ background-color: #ffeef0; }} /* Light red for highest priority */
        .priority-2 {{ background-color: #fff8c5; }} /* Light yellow for partial */
        .status-badge {{ display: inline-block; padding: 2px 6px; border-radius: 12px; font-size: 12px; font-weight: 600; }}
        .enriched {{ background-color: #d1e7dd; color: #0f5132; }}
        .not_enriched {{ background-color: #f8d7da; color: #842029; }}
        .partial {{ background-color: #fff3cd; color: #664d03; }}
        .not_needed {{ background-color: #e2e3e5; color: #41464b; }}
        a {{ color: #0969da; text-decoration: none; }}
        a:hover {{ text-decoration: underline; }}
        .summary-box {{ background: #f6f8fa; padding: 15px; border-radius: 6px; border: 1px solid #d0d7de; margin-bottom: 20px; }}
    </style>
</head>
<body>
    <h1>Manual Check Report: Batch of 11 Products</h1>
    
    <div class="summary-box">
        <h2>Summary</h2>
        <ul>
            <li><strong>Tổng số source products:</strong> {num_source_products}</li>
            <li><strong>Tổng số flavor groups:</strong> {total_flavor_groups}</li>
            <li><strong>Enriched:</strong> <span class="status-badge enriched">{enriched}</span></li>
            <li><strong>Not Enriched:</strong> <span class="status-badge not_enriched">{not_enriched}</span></li>
            <li><strong>Partial:</strong> <span class="status-badge partial">{partial}</span></li>
            <li><strong>Manual Review Needed:</strong> <span class="status-badge">{manual_review}</span></li>
        </ul>
    </div>
    
    <h2>Top 5 Links Cần Kiểm Tra Đầu Tiên</h2>
    <ol>
"""
    
    top_5 = [r for r in rows if r["priority"] <= 2][:5]
    if not top_5:
        top_5 = rows[:5]
        
    for r in top_5:
        html += f"""        <li>
            <strong>{r['source_group_id']}</strong> (Flavor: {r['flavor']}) - 
            <span class="status-badge {r['enrich_status']}">{r['enrich_status']}</span>
            <em>({r['reason']})</em><br>
            Variant URL: <a href="{r['variant_url'] or r['source_url']}" target="_blank">{r['variant_url'] or r['source_url']}</a>
        </li>\n"""
        
    html += """    </ol>
    
    <h2>Product Table</h2>
    <table>
        <thead>
            <tr>
                <th>Source ID</th>
                <th>Group ID</th>
                <th>Title</th>
                <th>Flavor</th>
                <th>URLs</th>
                <th>Enrich Status</th>
                <th>Source Type</th>
                <th>Ingredients</th>
                <th>GA</th>
                <th>Images</th>
                <th>Import Mode</th>
                <th>Reason</th>
                <th>Check?</th>
            </tr>
        </thead>
        <tbody>
"""
    
    for r in rows:
        row_class = ""
        if r["priority"] == 1:
            row_class = "priority-1"
        elif r["priority"] == 2:
            row_class = "priority-2"
            
        badge_class = r["enrich_status"]
        
        urls = f'<a href="{r["source_url"]}" target="_blank">Source</a>'
        if r["variant_url"]:
            urls += f'<br><a href="{r["variant_url"]}" target="_blank">Variant</a>'
            
        html += f"""            <tr class="{row_class}">
                <td>{r['source_product_id']}</td>
                <td>{r['source_group_id']}</td>
                <td>{r['title']}</td>
                <td>{r['flavor']}</td>
                <td>{urls}</td>
                <td><span class="status-badge {badge_class}">{r['enrich_status']}</span></td>
                <td>{r['content_source_type']}</td>
                <td>{r['ingredients_status']}</td>
                <td>{r['ga_status']}</td>
                <td>{r['image_status']}</td>
                <td>{r['import_mode']}</td>
                <td style="max-width: 300px; word-wrap: break-word;">{r['reason']}</td>
                <td><strong>{r['rec_check']}</strong></td>
            </tr>\n"""
            
    html += """        </tbody>
    </table>
</body>
</html>"""

    with open(HTML_FILE, "w", encoding="utf-8") as f:
        f.write(html)
    return HTML_FILE

if __name__ == "__main__":
    rows, num_source = extract_data()
    generate_markdown(rows, num_source)
    html_file = generate_html(rows, num_source)
    print(f"Generated report at {REPORT_FILE}")
    print(f"Generated HTML report at {html_file}")
