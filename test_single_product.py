"""
Test: Scrape a single Chewy product page and export to JSON + CSV.
Usage: python test_single_product.py
"""

import asyncio
import csv
import json
import os
import random
import sys
import uuid
from pathlib import Path

from rich.console import Console

import config
import adspower

console = Console()


import re
from scrapling.parser import Selector

async def extract_product_detail(page) -> dict:
    """Extract full product data from a Chewy product detail page using Scrapling."""
    
    html = await page.content()
    url = page.url
    selector = Selector(html)
    
    # Title from h1
    title = selector.css('h1::text').get(default='').strip()
    
    # Brand
    brand = selector.css('h1 strong::text, h1 b::text').get(default='').strip()
    
    if not brand:
        brand_link = selector.css('a[href*="/brand/"]::text').get(default='')
        if brand_link:
            brand = brand_link.strip()
            
    if not brand and title:
        brand_patterns = [
            r"^(Hill's Science Diet|Hill's Prescription Diet|Royal Canin|Purina Pro Plan|Purina ONE|Blue Buffalo|Taste of the Wild|Wellness|Merrick|Nutro|Iams|Pedigree|Rachael Ray Nutrish|Natural Balance|Orijen|Acana|Canidae|Fromm|Instinct|Nulo|Open Farm|Stella & Chewy's|Solid Gold|Victor|Zignature|Diamond|Eukanuba)"
        ]
        for pattern in brand_patterns:
            m = re.match(pattern, title, re.IGNORECASE)
            if m:
                brand = m.group(1)
                break
                
    # Breadcrumbs
    crumbs_nodes = selector.css('[class*="breadcrumb"] a::text, [aria-label*="Breadcrumb"] a::text, [class*="Breadcrumb"] a::text').getall()
    breadcrumbs = [c.strip() for c in crumbs_nodes if c and c.strip() and c.strip() != 'Home']
    
    # Price
    price = ''
    all_price_texts = selector.css('[class*="price"]::text, [class*="Price"]::text').getall()
    for text in all_price_texts:
        text = text.strip()
        if 'autoship' in text.lower():
            continue
        match = re.match(r"^\$(\d+\.?\d*)", text)
        if match and not price:
            price = match.group(1)
            
    body_nodes = selector.css('body').xpath('.//text()[not(ancestor::script) and not(ancestor::style)]').getall() if selector.css('body') else []
    body_text = ' '.join(t.strip() for t in body_nodes if t.strip())
    
    if not price:
        price_match = re.search(r"\$(\d+\.\d{2})(?:\s|\n)", body_text)
        if price_match:
            price = price_match.group(1)
            
    # Original price
    original_price = ''
    strike_texts = selector.css('s::text, del::text, [class*="was-price"]::text, [class*="strike"]::text, [class*="original"]::text').getall()
    for text in strike_texts:
        match = re.search(r"\$(\d+(?:\.\d+)?)", text)
        if match:
            original_price = match.group(1)
            break
            
    # Images
    images = []
    seen_imgs = set()
    img_nodes = selector.css('img')
    for img in img_nodes:
        src = img.attrib.get('src', '')
        if not src or 'image.chewy.com' not in src:
            continue
        if any(x in src for x in ['logo', 'icon', 'badge', 'GC-', 'cms/']):
            continue
        normalized = src.split('?')[0]
        if normalized in seen_imgs:
            continue
        seen_imgs.add(normalized)
        
        width = img.attrib.get('width', '0')
        try:
            w_int = int(width)
        except ValueError:
            w_int = 0
            
        if w_int > 50 or '_AC_' in src or '/catalog/' in src:
            images.append(src)
    images = images[:10]
    
    # Sizes / variants
    sizes = []
    flavors = []
    swatch_btns = selector.css('button[class*="swatch"], [class*="Swatch"] button, [role="radio"]')
    for btn in swatch_btns:
        text_nodes = btn.xpath('.//text()').getall()
        text = ' '.join(t.strip() for t in text_nodes if t.strip())
        if not text:
            continue
            
        if re.search(r"\d+.*(lb|oz|ct|bag|pack|kg|count)", text, re.IGNORECASE) and len(text) < 80:
            price_match = re.search(r"\$(\d+\.\d+)", text)
            size_match = re.search(r"(\d+(?:\.\d+)?-?\s*(?:lb|oz|ct|kg)\s*(?:bag|pack|bundle)?(?:\s*\(.*?\))?)", text, re.IGNORECASE)
            
            label = size_match.group(1).strip() if size_match else text.split('$')[0].strip()
            p = price_match.group(1) if price_match else ''
            
            selected = btn.attrib.get('aria-pressed') == 'true' or btn.attrib.get('aria-checked') == 'true'
            sizes.append({
                "label": label,
                "price": p,
                "selected": selected
            })
        elif not re.search(r"\d+.*(lb|oz|ct|bag)", text, re.IGNORECASE) and 2 < len(text) < 40 and '$' not in text:
            flavors.append(text)
            
    # Description
    description = ''
    all_ps = selector.css('p')
    product_ps = []
    for p in all_ps:
        text = ' '.join(t.strip() for t in p.xpath('.//text()').getall() if t.strip())
        if 80 < len(text) < 3000 and '©' not in text and 'cookie' not in text and 'Sign In' not in text and 'paypal purchases' not in text.lower():
            product_ps.append(text)
    if product_ps:
        description = '\n\n'.join(product_ps)
        
    # Key benefits
    key_benefits = []
    all_uls = selector.css('ul')
    for ul in all_uls:
        lis = ul.css('li')
        bullets = []
        for li in lis:
            text = ' '.join(t.strip() for t in li.xpath('.//text()').getall() if t.strip())
            has_link = len(li.css('a[href]')) > 0
            if 30 < len(text) < 500 and '$' not in text and 'Sign In' not in text and 'Track Order' not in text and not has_link:
                bullets.append(text)
        if len(bullets) >= 3:
            key_benefits = bullets[:10]
            break
            
    # Specs
    specs = {}
    spec_labels = ['Item Number', 'Packaging Type', 'Made In', 'Sourced From', 
                   'Lifestage', 'Breed Size', 'Food Form', 'Special Diet',
                   'Moisture', 'Crude Protein', 'Crude Fat', 'Crude Fiber',
                   'Ash', 'Vitamin C', 'Vitamin E', 'Total Omega-3 FA', 
                   'Total Omega-6 FA', 'Caloric Content']
                   
    for label in spec_labels:
        regex = re.compile(rf"{label}\s*[:\n]\s*([^\n]+)", re.IGNORECASE)
        match = regex.search(body_text)
        if match:
            val = match.group(1).strip().split('\t')[0].strip()
            if 0 < len(val) < 100:
                specs[label] = val
                
    # Item number / url parsing
    match_url = re.search(r"/dp/(\d+)", url)
    item_number = match_url.group(1) if match_url else ''
    
    # Weight
    weight_match = re.search(r"(\d+(?:\.\d+)?-(?:lb|oz|kg))", title, re.IGNORECASE)
    weight = weight_match.group(1) if weight_match else ''
    
    # Build clean product dict
    product = {
        "title": title,
        "brand": brand,
        "url": url,
        "product_id": item_number,
        "sku": item_number,
        "price": price,
        "original_price": original_price,
        "currency": "USD",
        "weight": weight,
        "description": description,
        "key_benefits": key_benefits,
        "breadcrumbs": " > ".join(breadcrumbs),
        "categories": breadcrumbs,
        "images": images,
        "sizes": sizes,
        "flavors": flavors,
        "specs": specs,
    }
    
    return product


def product_to_shopify_csv_row(product: dict) -> dict:
    """Convert product dict to Shopify CSV import format."""
    images = product.get("images", [])
    specs = product.get("specs", {})

    # Generate handle from URL slug
    import re
    url = product.get("url", "")
    handle_match = re.search(r"chewy\.com/([^/]+)/dp/", url)
    handle = handle_match.group(1) if handle_match else product.get("product_id", "")

    # Build tags from categories + specs
    tags = list(product.get("categories", []))
    for key in ["Lifestage", "Breed Size", "Food Form"]:
        if specs.get(key):
            tags.append(f"{key}: {specs[key]}")

    return {
        "Handle": handle,
        "Title": product.get("title", ""),
        "Body (HTML)": product.get("description", ""),
        "Vendor": product.get("brand", ""),
        "Product Category": product.get("breadcrumbs", ""),
        "Type": specs.get("Food Form", ""),
        "Tags": ", ".join(tags),
        "Published": "TRUE",
        "Variant SKU": product.get("sku", ""),
        "Variant Price": product.get("price", ""),
        "Variant Compare At Price": product.get("original_price", ""),
        "Variant Inventory Qty": "100",
        "Variant Weight": product.get("weight", "").replace("-lb", "").replace("-oz", "").replace("-kg", ""),
        "Variant Weight Unit": "lb" if "lb" in product.get("weight", "") else "oz" if "oz" in product.get("weight", "") else "",
        "Image Src": images[0] if images else "",
        "Image Position": "1",
        "Status": "active",
        "SEO Title": product.get("title", "")[:70],
        "SEO Description": product.get("description", "")[:320],
    }


import sys
import traceback
from datetime import datetime

async def main():
    test_url = sys.argv[1] if len(sys.argv) > 1 else "https://www.chewy.com/hills-science-diet-adult-sensitive/dp/3861718"
    out_dir = Path(config.OUTPUT_DIR)
    out_dir.mkdir(exist_ok=True)

    console.print(f"[bold cyan]Testing single product scrape: {test_url}[/]")

    # Start AdsPower
    profile_data = adspower.start_profile(config.ADSPOWER_PROFILE_ID)
    ws_url = adspower.get_ws_endpoint(profile_data)
    console.print(f"[green]Connected to AdsPower[/]")

    try:
        from playwright.async_api import async_playwright

        async with async_playwright() as p:
            browser = None
            for attempt in range(3):
                try:
                    browser = await p.chromium.connect_over_cdp(ws_url, timeout=15000)
                    break
                except Exception as e:
                    console.print(f"[yellow]CDP Connection failed (attempt {attempt+1}/3): {e}[/yellow]")
                    await asyncio.sleep(2)
            
            if not browser:
                console.print(f"[red]Playwright Error: Failed to connect to CDP at {ws_url} after 3 attempts.[/red]")
                sys.exit(1)

            context = browser.contexts[0]
            page = context.pages[0] if context.pages else await context.new_page()

            # Navigate
            console.print(f"Navigating to product page...")
            try:
                await page.goto(test_url, timeout=config.PAGE_LOAD_TIMEOUT, wait_until="domcontentloaded")
            except Exception as e:
                console.print(f"[red]Playwright Navigation Error: {str(e)}[/red]")
                sys.exit(1)
            await asyncio.sleep(random.uniform(3, 5))

            # Scroll down to load all content
            for _ in range(3):
                await page.mouse.wheel(0, random.randint(300, 500))
                await asyncio.sleep(random.uniform(0.5, 1.0))
                
            # Phase 4 - White Screen Detection
            from adsp_profile_pool_manager import detect_white_screen_block
            console.print(
                f"[cyan]Checking for white screen conservatively "
                f"(up to {getattr(config, 'ADSP_WHITE_SCREEN_MAX_WAIT_SECONDS', 90)}s for slow proxy load)...[/]"
            )
            detection_result = await detect_white_screen_block(page, test_url)
            if detection_result["is_white_screen"]:

                if getattr(config, 'ADSP_SAVE_WHITE_SCREEN_SCREENSHOT', True):
                    try:
                        os.makedirs("output/white_screen_events", exist_ok=True)
                        screenshot_path = f"output/white_screen_events/temp_{uuid.uuid4().hex}.png"
                        await page.screenshot(path=screenshot_path)
                        detection_result["screenshot_path"] = screenshot_path
                    except: pass
                if getattr(config, 'ADSP_SAVE_WHITE_SCREEN_HTML', True):
                    try:
                        html_path = f"output/white_screen_events/temp_{uuid.uuid4().hex}.html"
                        with open(html_path, "w", encoding="utf-8") as f:
                            f.write(await page.content())
                        detection_result["html_path"] = html_path
                    except: pass
                console.print("[red][WHITE_SCREEN_DETECTED][/red]")
                print(f"[WHITE_SCREEN_RESULT] {json.dumps(detection_result)}")
                # We must abort extraction so the runner can catch this and quarantine
                return

            # Phase 3C Integration
            from chewy_next_json_extractor import (
                fetch_initial_html,
                extract_next_data_from_html,
                detect_next_build_id,
                build_next_data_url,
                fetch_next_data_json,
                detect_chewy_architecture,
                parse_apollo_product,
                parse_redux_product,
                normalize_chewy_product,
                split_product_by_flavor,
                validate_normalized_product
            )
            
            use_new_json = config.USE_CHEWY_NEXT_JSON_EXTRACTOR
            fallback_to_old = config.CHEWY_JSON_FALLBACK_TO_OLD_SCRAPER
            threshold = config.CHEWY_JSON_CONFIDENCE_THRESHOLD
            
            console.print(f"[cyan]USE_CHEWY_NEXT_JSON_EXTRACTOR: {use_new_json}[/]")
            console.print(f"[cyan]CHEWY_JSON_FALLBACK_TO_OLD_SCRAPER: {fallback_to_old}[/]")
            console.print(f"[cyan]CHEWY_JSON_CONFIDENCE_THRESHOLD: {threshold}[/]")
            
            run_old_scraper = True
            
            if use_new_json:
                console.print("[bold yellow]Running NEW JSON Extractor...[/]")
                
                # Fetch and parse
                html = await fetch_initial_html(test_url, page)
                
                next_data = extract_next_data_from_html(html)
                
                match = re.search(r"chewy\.com/(.*?)/dp/(\d+)", test_url)
                source_id = match.group(2) if match else "unknown"
                
                if not next_data:
                    build_id = detect_next_build_id(None, html)
                    if build_id:
                        next_url = build_next_data_url(test_url, build_id)
                        if next_url:
                            next_data = await fetch_next_data_json(next_url, page, build_id, source_id)
                
                if next_data:
                    arch = detect_chewy_architecture(next_data)
                    
                    if arch == "apollo":
                        parsed = parse_apollo_product(next_data, test_url)
                    elif arch == "redux":
                        parsed = parse_redux_product(next_data, test_url)
                    else:
                        parsed = {"warnings": ["Unknown architecture"], "title": ""}
                        
                    if parsed and parsed.get("title") and not "Unknown architecture" in parsed.get("warnings", []):
                        normalized = normalize_chewy_product(parsed)
                        grouped = split_product_by_flavor(normalized)
                        val = validate_normalized_product(normalized, grouped)
                        
                        score = val.get("confidence_score", 0)
                        
                        # Logging
                        console.print(f"  Detected Architecture: {arch}")
                        console.print(f"  Source Product ID: {source_id}")
                        console.print(f"  Title: {normalized.get('title')}")
                        console.print(f"  Original Variants: {len(normalized.get('variants', []))}")
                        console.print(f"  Grouped Products: {len(grouped.get('products', []))}")
                        
                        for i, p in enumerate(grouped.get('products', [])):
                            console.print(f"    Group {i} ({p.get('flavor')}): {len(p.get('variants', []))} variants")
                            
                        # --- Fallback decision ---
                        # Only fallback when critical data is genuinely missing:
                        # title, images, description, price.
                        # Score and validate_normalized_product warnings are informational only.
                        has_title = bool(normalized.get("title"))
                        has_images = bool(normalized.get("images"))
                        has_description = bool(normalized.get("description"))
                        has_price = any(v.get("price") for v in normalized.get("variants", []))
                        has_grouped = len(grouped.get("products", [])) > 0

                        no_mixed_flavors = True
                        for i, p in enumerate(grouped.get('products', [])):
                            for v in p.get('variants', []):
                                if v.get('option1_name') == 'Flavor' or v.get('option_values', {}).get('flavor'):
                                    no_mixed_flavors = False
                                    if "warnings" not in val: val["warnings"] = []
                                    val["warnings"].append(f"Group {i} variant incorrectly retains Flavor option.")

                        critical_pass = has_title and has_images and has_description and has_price and has_grouped and no_mixed_flavors

                        if critical_pass:
                            console.print(f"[bold green]JSON Extractor Success! Score: {score}[/]")
                            run_old_scraper = False
                            
                            if config.CHEWY_JSON_SAVE_GROUPED_OUTPUT:
                                out_dir_normalized = Path("output/normalized_products")
                                out_dir_normalized.mkdir(parents=True, exist_ok=True)
                                p1 = out_dir_normalized / f"chewy_{source_id}.json"
                                with open(p1, "w", encoding="utf-8") as f:
                                    json.dump(normalized, f, indent=2, ensure_ascii=False)
                                    
                                out_dir_grouped = Path("output/grouped_products")
                                out_dir_grouped.mkdir(parents=True, exist_ok=True)
                                p2 = out_dir_grouped / f"chewy_grouped_by_flavor_{source_id}.json"
                                with open(p2, "w", encoding="utf-8") as f:
                                    json.dump(grouped, f, indent=2, ensure_ascii=False)
                                    
                                out_dir_val = Path("output/validation")
                                out_dir_val.mkdir(parents=True, exist_ok=True)
                                p3 = out_dir_val / f"chewy_validation_{source_id}.json"
                                with open(p3, "w", encoding="utf-8") as f:
                                    json.dump(val, f, indent=2, ensure_ascii=False)
                                console.print(f"  Outputs saved.")
                        else:
                            console.print(f"[bold red]JSON Extractor Missing Critical Fields. Score: {score}[/]")
                            console.print(f"  Has Title: {has_title}")
                            console.print(f"  Has Images: {has_images}")
                            console.print(f"  Has Description: {has_description}")
                            console.print(f"  Has Price: {has_price}")
                            console.print(f"  Has Grouped: {has_grouped}")
                            console.print(f"  Mixed flavors: {not no_mixed_flavors}")
                            if val.get('warnings'):
                                console.print(f"  Warnings: {val['warnings']}")
                            
                            # Diagnostic output
                            diag = {
                                "input_url": test_url,
                                "error": "Critical fields missing",
                                "detected_architecture": arch,
                                "confidence_score": score,
                                "has_title": has_title,
                                "has_images": has_images,
                                "has_description": has_description,
                                "has_price": has_price,
                                "has_grouped": has_grouped,
                                "warnings": val.get("warnings", []),
                                "fallback_used": fallback_to_old,
                                "fallback_reason": "Missing title, images, description, or price"
                            }
                            
                            diag_dir = Path("output/json_extractor_failures")
                            diag_dir.mkdir(parents=True, exist_ok=True)
                            p4 = diag_dir / f"chewy_failure_{source_id}.json"
                            with open(p4, "w", encoding="utf-8") as f:
                                json.dump(diag, f, indent=2, ensure_ascii=False)
                                
                            if not fallback_to_old:
                                run_old_scraper = False
                                console.print("[red]Fallback disabled. Aborting.[/]")
                    else:
                        console.print("[bold red]JSON Extractor Failed.[/]")
                        
                        diag = {
                            "input_url": test_url,
                            "error": "Parsing failed",
                            "detected_architecture": arch,
                            "fallback_used": fallback_to_old,
                            "fallback_reason": f"Parsing failed"
                        }
                        
                        diag_dir = Path("output/json_extractor_failures")
                        diag_dir.mkdir(parents=True, exist_ok=True)
                        ts = datetime.now().strftime("%Y%m%d%H%M%S")
                        p5 = diag_dir / f"chewy_failure_{source_id}_{ts}.json"
                        with open(p5, "w", encoding="utf-8") as f:
                            json.dump(diag, f, indent=2, ensure_ascii=False)
                            
                        if not fallback_to_old:
                            run_old_scraper = False
                            console.print("[red]Fallback disabled. Aborting.[/]")
                else:
                    console.print("[bold red]Fetch initial HTML failed.[/]")
                    if not fallback_to_old:
                        run_old_scraper = False
                        console.print("[red]Fallback disabled. Aborting.[/]")
            
            if run_old_scraper:
                console.print("[bold blue]Running OLD Scraper (Fallback or Primary)...[/]")
                console.print("Extracting product data...")
                product = await extract_product_detail(page)

                # Save fallback output per product ID into grouped_products
                fallback_id = source_id or product.get("product_id") or product.get("sku") or "unknown"
                
                # Normalize fallback data into grouped_products format
                fallback_grouped = {
                    "source": "chewy",
                    "source_product_id": fallback_id,
                    "source_url": test_url,
                    "architecture": "fallback_html",
                    "grouping_strategy": "fallback_single_product",
                    "products": [{
                        "source_group_id": fallback_id,
                        "title": product.get("title", ""),
                        "flavor": None,
                        "brand": product.get("brand", ""),
                        "handle_slug": product.get("slug", ""),
                        "category_path": product.get("breadcrumbs", "").split(" > ") if product.get("breadcrumbs") else [],
                        "description": product.get("description", ""),
                        "ingredients": product.get("ingredients", ""),
                        "guaranteed_analysis": "",
                        "feeding_instructions": "",
                        "specifications": product.get("specs", {}),
                        "images": product.get("images", []),
                        "variants": [{
                            "source_variant_id": fallback_id,
                            "sku": product.get("sku", fallback_id),
                            "title": product.get("title", ""),
                            "option1_name": "Size",
                            "option1_value": "Default Title",
                            "price": product.get("price"),
                            "compare_at_price": product.get("original_price"),
                            "in_stock": True,
                            "images": product.get("images", []),
                            "variant_url": test_url,
                        }],
                    }]
                }

                out_dir_grouped = Path("output/grouped_products")
                out_dir_grouped.mkdir(parents=True, exist_ok=True)
                grouped_path = out_dir_grouped / f"chewy_grouped_by_flavor_{fallback_id}.json"
                with open(grouped_path, "w", encoding="utf-8") as f:
                    json.dump(fallback_grouped, f, indent=2, ensure_ascii=False)
                console.print(f"[green]Grouped JSON saved: {grouped_path}[/]")

                # Also save raw fallback JSON for debugging
                json_path = out_dir / "test_chewy_product.json"
                with open(json_path, "w", encoding="utf-8") as f:
                    json.dump(product, f, indent=2, ensure_ascii=False)
                console.print(f"[green]JSON saved: {json_path}[/]")

                # Save Shopify CSV
                csv_path = out_dir / "test_chewy_product_shopify.csv"
                row = product_to_shopify_csv_row(product)
                with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
                    writer = csv.DictWriter(f, fieldnames=row.keys())
                    writer.writeheader()
                    writer.writerow(row)
                console.print(f"[green]Shopify CSV saved: {csv_path}[/]")

                # Print summary
                console.print(f"\n{'='*60}")
                console.print(f"[bold]Product Summary[/]")
                console.print(f"  Title:        {product['title']}")
                console.print(f"  Brand:        {product['brand']}")
                console.print(f"  Price:        ${product['price']}")
                console.print(f"  Original:     ${product['original_price']}")
                console.print(f"  SKU:          {product['sku']}")
                console.print(f"  Product ID:   {product['product_id']}")
                console.print(f"  Weight:       {product['weight']}")
                console.print(f"  Images:       {len(product['images'])}")
                console.print(f"  Sizes:        {product['sizes']}")
                console.print(f"  Flavors:      {product['flavors']}")
                console.print(f"  Categories:   {product['breadcrumbs']}")
                console.print(f"  Description:  {product['description'][:150]}...")
                console.print(f"  Key Benefits: {len(product['key_benefits'])}")
                console.print(f"  Specs:        {product['specs']}")
                console.print(f"{'='*60}")

    finally:
        adspower.stop_profile(config.ADSPOWER_PROFILE_ID)

    console.print("[bold green]Done! Check output/ folder for JSON and CSV files.[/]")

if __name__ == "__main__":
    asyncio.run(main())
