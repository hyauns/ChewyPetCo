import json
from pathlib import Path
import html

RESULT_FILE = Path("c:/Users/admin/Documents/Scraper/Pet/output/enrichment_runs/result_batch_all_20260515_030102.json")
PREVIEW_FILE = Path("c:/Users/admin/Documents/Scraper/Pet/test_runs/shopify_preview.html")

def escape(text):
    if text is None:
        return ""
    return html.escape(str(text))

def format_markdown_table(text):
    if not text: return ""
    # Very basic markdown table to HTML converter
    lines = text.strip().split('\n')
    if not lines: return text
    
    in_table = False
    html_out = []
    
    for line in lines:
        line = line.strip()
        if not line:
            html_out.append("<br/>")
            continue
            
        if line.startswith('|'):
            if not in_table:
                html_out.append("<table class='shopify-table'>")
                in_table = True
            
            # check if it's a separator line
            if set(line.replace('|', '').replace('-', '').replace(':', '').strip()) == set():
                continue
                
            cells = [c.strip() for c in line.split('|')[1:-1]]
            html_out.append("<tr>")
            for cell in cells:
                # Basic heuristic for th vs td
                if in_table and len(html_out) == 2: # First row
                    html_out.append(f"<th>{escape(cell)}</th>")
                else:
                    html_out.append(f"<td>{escape(cell)}</td>")
            html_out.append("</tr>")
        else:
            if in_table:
                html_out.append("</table>")
                in_table = False
            html_out.append(f"<p>{escape(line)}</p>")
            
    if in_table:
        html_out.append("</table>")
        
    return "\n".join(html_out)


def generate_preview():
    with open(RESULT_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    products = []
    for group in data:
        for p in group.get("products", []):
            products.append(p)
            
    html_content = """<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>Shopify Import Preview</title>
    <style>
        :root { --primary: #008060; --text: #202223; --bg: #f4f6f8; --border: #c4cdd5; }
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; margin: 0; padding: 0; display: flex; height: 100vh; color: var(--text); background: var(--bg); }
        .sidebar { width: 350px; background: white; border-right: 1px solid var(--border); overflow-y: auto; display: flex; flex-direction: column; }
        .sidebar-header { padding: 20px; border-bottom: 1px solid var(--border); background: #f9fafb; position: sticky; top: 0; z-index: 10; }
        .sidebar-header h2 { margin: 0; font-size: 16px; }
        .product-list { list-style: none; padding: 0; margin: 0; }
        .product-item { padding: 15px 20px; border-bottom: 1px solid #f1f2f3; cursor: pointer; transition: background 0.2s; }
        .product-item:hover { background: #f9fafb; }
        .product-item.active { background: #e3f1df; border-left: 4px solid var(--primary); }
        .product-item-title { font-weight: 600; font-size: 13px; margin-bottom: 4px; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
        .product-item-meta { font-size: 11px; color: #6d7175; display: flex; justify-content: space-between; }
        .badge { padding: 2px 6px; border-radius: 10px; font-size: 10px; font-weight: 600; }
        .badge.safe { background: #aee9d1; color: #007f5f; }
        .badge.warning { background: #ffea8a; color: #8a6116; }
        .badge.blocked { background: #fed3d1; color: #bf0711; }
        
        .main-content { flex: 1; overflow-y: auto; padding: 40px; }
        .preview-container { max-width: 1000px; margin: 0 auto; background: white; border-radius: 8px; box-shadow: 0 0 0 1px rgba(63, 63, 68, 0.05), 0 1px 3px 0 rgba(63, 63, 68, 0.15); display: none; }
        .preview-container.active { display: block; }
        
        /* Shopify Product Layout Simulation */
        .product-top { display: flex; padding: 30px; gap: 40px; border-bottom: 1px solid var(--border); }
        .product-gallery { flex: 0 0 400px; }
        .main-image { width: 100%; aspect-ratio: 1; object-fit: contain; border: 1px solid #f1f2f3; border-radius: 8px; padding: 10px; margin-bottom: 10px; }
        .thumbnails { display: flex; gap: 10px; overflow-x: auto; padding-bottom: 5px; }
        .thumb { width: 60px; height: 60px; object-fit: contain; border: 1px solid var(--border); border-radius: 4px; cursor: pointer; }
        .thumb:hover { border-color: var(--primary); }
        
        .product-info { flex: 1; }
        .vendor { color: #6d7175; font-size: 14px; margin-bottom: 5px; text-transform: uppercase; letter-spacing: 0.5px; }
        .product-title { font-size: 24px; font-weight: 600; margin: 0 0 15px 0; line-height: 1.3; }
        .product-price { font-size: 20px; font-weight: 600; margin-bottom: 25px; }
        
        .variant-selector { margin-bottom: 25px; }
        .variant-label { font-size: 13px; font-weight: 600; margin-bottom: 8px; display: block; }
        .variant-options { display: flex; flex-wrap: wrap; gap: 10px; }
        .variant-btn { padding: 8px 15px; border: 1px solid var(--border); background: white; border-radius: 4px; cursor: pointer; font-size: 14px; }
        .variant-btn.selected { border-color: var(--primary); outline: 1px solid var(--primary); }
        
        .buy-button { width: 100%; padding: 12px; background: var(--primary); color: white; border: none; border-radius: 4px; font-size: 16px; font-weight: 600; cursor: pointer; margin-bottom: 10px; }
        .buy-button:hover { background: #006e52; }
        
        .product-bottom { padding: 0; }
        .tabs { display: flex; border-bottom: 1px solid var(--border); background: #fafbfc; }
        .tab-btn { padding: 15px 20px; background: none; border: none; border-bottom: 2px solid transparent; cursor: pointer; font-size: 14px; font-weight: 600; color: #6d7175; }
        .tab-btn.active { color: var(--primary); border-bottom-color: var(--primary); }
        .tab-btn:hover:not(.active) { color: var(--text); }
        
        .tab-content { padding: 30px; display: none; font-size: 14px; line-height: 1.6; }
        .tab-content.active { display: block; }
        
        .shopify-table { width: 100%; border-collapse: collapse; margin-top: 15px; }
        .shopify-table th, .shopify-table td { border: 1px solid var(--border); padding: 8px 12px; text-align: left; }
        .shopify-table th { background: #f9fafb; font-weight: 600; }
        
        .status-banner { padding: 10px 15px; margin-bottom: 20px; border-radius: 4px; font-size: 13px; font-weight: 500; }
        .status-banner.safe { background: #e3f1df; color: #007f5f; border: 1px solid #aee9d1; }
        .status-banner.warning { background: #fff5ea; color: #8a6116; border: 1px solid #ffea8a; }
        .status-banner.blocked { background: #ffe4e5; color: #bf0711; border: 1px solid #fed3d1; }
        
        /* Empty state */
        #empty-state { text-align: center; margin-top: 100px; color: #6d7175; }
        #empty-state h2 { border: none; color: #202223; }
    </style>
</head>
<body>
    <div class="sidebar">
        <div class="sidebar-header">
            <h2>Products Preview ({len(products)})</h2>
        </div>
        <ul class="product-list">
"""

    # Generate Sidebar Items
    for idx, p in enumerate(products):
        title = escape(p.get("title", ""))
        flavor = escape(p.get("flavor", "Default"))
        group_id = escape(p.get("source_group_id", ""))
        import_mode = p.get("import_mode", "unknown")
        
        badge_class = "safe"
        if import_mode == "needs_manual_review" or import_mode == "safe_with_warnings":
            badge_class = "warning"
        elif import_mode == "blocked":
            badge_class = "blocked"
            
        html_content += f"""            <li class="product-item" onclick="showProduct({idx})" id="nav-{idx}">
                <div class="product-item-title">{title}</div>
                <div class="product-item-meta">
                    <span>{flavor}</span>
                    <span class="badge {badge_class}">{import_mode.replace('_', ' ')}</span>
                </div>
            </li>\n"""
            
    html_content += """        </ul>
    </div>
    
    <div class="main-content">
        <div id="empty-state">
            <h2>Select a product from the left to preview</h2>
            <p>This simulates how the data will appear when imported to Shopify.</p>
        </div>
"""

    # Generate Product Containers
    for idx, p in enumerate(products):
        title = escape(p.get("title", ""))
        vendor = escape(p.get("brand", "Unknown Vendor"))
        
        # Images
        all_imgs = p.get("images", [])
        for v in p.get("variants", []):
            all_imgs.extend(v.get("images", []))
            
        real_imgs = [i for i in all_imgs if isinstance(i, str) and i.strip()]
        
        main_img = real_imgs[0] if real_imgs else "https://cdn.shopify.com/s/images/admin/no-image-large.gif"
        
        thumbnails_html = ""
        unique_imgs = []
        for img in real_imgs:
            if img not in unique_imgs:
                unique_imgs.append(img)
                thumbnails_html += f'<img src="{escape(img)}" class="thumb" onclick="document.getElementById(\'main-img-{idx}\').src=this.src">'
                
        if not unique_imgs:
            thumbnails_html = '<span style="font-size:12px;color:#6d7175">No real images found</span>'

        # Variants
        variants = p.get("variants", [])
        variant_html = ""
        first_price = "N/A"
        
        # Get variant option name
        option_name = "Size"
        if variants and variants[0].get("option1_name"):
            option_name = escape(variants[0].get("option1_name"))
            
        if variants:
            first_price = variants[0].get("price") or "0.00"
            for v_idx, v in enumerate(variants):
                size = escape(v.get("option1_value", f"Variant {v_idx+1}"))
                sel_class = "selected" if v_idx == 0 else ""
                variant_html += f'<button class="variant-btn {sel_class}">{size}</button>'
                
        # Status Banner
        import_mode = p.get("import_mode", "unknown")
        status_class = "safe"
        if import_mode == "needs_manual_review" or import_mode == "safe_with_warnings":
            status_class = "warning"
        elif import_mode == "blocked":
            status_class = "blocked"
            
        reason = p.get("debug", {}).get("rejected_reason", "")
        warnings = ", ".join(p.get("debug", {}).get("parser_warnings", []))
        banner_msg = f"<strong>Status: {import_mode}</strong>"
        if reason: banner_msg += f"<br>Reason: {escape(reason)}"
        if warnings: banner_msg += f"<br>Warnings: {escape(warnings)}"
        
        # Content
        desc = p.get("description", "<p><em>No description</em></p>")
        if not desc.startswith("<p"): desc = f"<p>{desc}</p>"
        
        ingr = p.get("ingredients", "<p><em>No ingredients listed</em></p>")
        ga = p.get("guaranteed_analysis", "")
        ga_html = format_markdown_table(ga) if ga else "<p><em>No guaranteed analysis</em></p>"
        feed = p.get("feeding_instructions", "")
        feed_html = format_markdown_table(feed) if feed else "<p><em>No feeding instructions</em></p>"
        
        source_type = escape(p.get("content_source", {}).get("type", "parent"))

        html_content += f"""        <div class="preview-container" id="prod-{idx}">
            <div class="product-top">
                <div class="product-gallery">
                    <img src="{escape(main_img)}" class="main-image" id="main-img-{idx}">
                    <div class="thumbnails">
                        {thumbnails_html}
                    </div>
                </div>
                
                <div class="product-info">
                    <div class="status-banner {status_class}">
                        {banner_msg}
                        <br><span style="font-size: 11px; opacity: 0.8; margin-top: 5px; display: block;">Content Source: {source_type}</span>
                    </div>
                    
                    <div class="vendor">{vendor}</div>
                    <h1 class="product-title">{title}</h1>
                    <div class="product-price">${escape(first_price)}</div>
                    
                    <div class="variant-selector">
                        <span class="variant-label">{option_name}</span>
                        <div class="variant-options">
                            {variant_html}
                        </div>
                    </div>
                    
                    <button class="buy-button">Add to cart</button>
                </div>
            </div>
            
            <div class="product-bottom">
                <div class="tabs">
                    <button class="tab-btn active" onclick="switchTab(this, 'desc-{idx}')">Description</button>
                    <button class="tab-btn" onclick="switchTab(this, 'ingr-{idx}')">Ingredients</button>
                    <button class="tab-btn" onclick="switchTab(this, 'ga-{idx}')">Guaranteed Analysis</button>
                    <button class="tab-btn" onclick="switchTab(this, 'feed-{idx}')">Feeding Instructions</button>
                </div>
                
                <div class="tab-content active" id="desc-{idx}">
                    {desc}
                </div>
                
                <div class="tab-content" id="ingr-{idx}">
                    <p>{escape(ingr)}</p>
                </div>
                
                <div class="tab-content" id="ga-{idx}">
                    {ga_html}
                </div>
                
                <div class="tab-content" id="feed-{idx}">
                    {feed_html}
                </div>
            </div>
        </div>\n"""

    html_content += """    </div>

    <script>
        function showProduct(idx) {
            document.getElementById('empty-state').style.display = 'none';
            
            // Hide all products
            document.querySelectorAll('.preview-container').forEach(el => {
                el.classList.remove('active');
            });
            
            // Remove active class from nav
            document.querySelectorAll('.product-item').forEach(el => {
                el.classList.remove('active');
            });
            
            // Show selected
            document.getElementById('prod-' + idx).classList.add('active');
            document.getElementById('nav-' + idx).classList.add('active');
            
            // Reset tabs for selected product
            const container = document.getElementById('prod-' + idx);
            const firstTab = container.querySelector('.tab-btn');
            if (firstTab) {
                switchTab(firstTab, 'desc-' + idx);
            }
        }
        
        function switchTab(btn, contentId) {
            const container = btn.closest('.product-bottom');
            
            // Reset buttons
            container.querySelectorAll('.tab-btn').forEach(el => {
                el.classList.remove('active');
            });
            btn.classList.add('active');
            
            // Reset content
            container.querySelectorAll('.tab-content').forEach(el => {
                el.classList.remove('active');
            });
            document.getElementById(contentId).classList.add('active');
        }
    </script>
</body>
</html>
"""

    with open(PREVIEW_FILE, "w", encoding="utf-8") as f:
        f.write(html_content)
    print(f"Generated Shopify Preview at {PREVIEW_FILE}")

if __name__ == "__main__":
    generate_preview()
