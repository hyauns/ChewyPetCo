import asyncio
import json
import logging
import re
from typing import Any, Dict, List
from urllib.parse import urljoin, urlparse

from playwright.async_api import async_playwright
import category_price_filter
import job_store

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

async def discover_category_products(
    category_job_id: str,
    category_url: str,
    price_min: float | None = None,
    price_max: float | None = None,
    mode: str = "hybrid",
    max_pages: int | None = None,
    delay_seconds: float = 2.0
) -> None:
    job = job_store.get_category_job(category_job_id)
    if not job:
        logger.error(f"Category job not found: {category_job_id}")
        return

    job_store.update_category_job(category_job_id, status="running")
    current_page = job["current_page"] or 1
    
    # Extract base URL without page
    parsed = urlparse(category_url)
    
    async with async_playwright() as p:
        try:
            browser = await p.chromium.connect_over_cdp("http://127.0.0.1:9222")
            context = browser.contexts[0]
            logger.info("Connected to AdsPower CDP")
        except Exception as e:
            logger.info(f"Failed to connect to AdsPower CDP, falling back to launch: {e}")
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
        page = await context.new_page()
        
        try:
            while True:
                if max_pages and current_page > max_pages:
                    logger.info(f"Reached max pages limit ({max_pages}). Stopping discovery.")
                    break
                    
                page_url = category_url
                if current_page > 1:
                    sep = "&" if "?" in category_url else "?"
                    page_url = f"{category_url}{sep}p={current_page}"
                
                logger.info(f"[{category_job_id}] Discovering page {current_page}: {page_url}")
                try:
                    await page.goto(page_url, wait_until="domcontentloaded", timeout=60000)
                    # wait for products to load, either grid or main
                    await page.wait_for_timeout(3000)
                except Exception as e:
                    logger.error(f"Failed to load page {current_page}: {e}")
                    job_store.update_category_job(category_job_id, status="paused", last_error=str(e))
                    break
                
                # Check if it's a 404 or no products
                content = await page.content()
                if "we couldn't find any results" in content.lower() or "page not found" in content.lower():
                    logger.info(f"No more products found on page {current_page}. Ending discovery.")
                    break
                
                # Extract product cards
                cards_data = await extract_product_cards(page)
                if not cards_data:
                    logger.info(f"No product cards extracted on page {current_page}. Ending discovery.")
                    break
                    
                logger.info(f"Found {len(cards_data)} cards on page {current_page}")
                
                for card in cards_data:
                    raw_price = card.get("price", "")
                    parsed_price = category_price_filter.parse_price_to_float(raw_price)
                    
                    filter_res = category_price_filter.product_card_matches_price_filter(
                        parsed_price, price_min, price_max, mode
                    )
                    
                    job_store.add_category_item(
                        category_job_id=category_job_id,
                        page_number=current_page,
                        source_category_url=page_url,
                        product_url=card["url"],
                        status=filter_res["status"],
                        title=card.get("title"),
                        brand=card.get("brand"),
                        card_price_raw=raw_price,
                        card_price_min=parsed_price["price_min"],
                        card_price_max=parsed_price["price_max"],
                        image_url=card.get("image"),
                        filter_reason=filter_res["reason"],
                        metadata_json={"parsed_price_confidence": parsed_price["confidence"]}
                    )
                
                job_store.update_category_job(
                    category_job_id,
                    current_page=current_page + 1,
                    total_pages_discovered=current_page
                )
                job_store.update_category_job_counts(category_job_id)
                
                # Pagination check
                # Typically chewy pagination has "Next" button or we just check if cards < 10 (usually 36 per page)
                if len(cards_data) < 10:
                    logger.info(f"Fewer than 10 cards on page {current_page}, assuming last page.")
                    break
                    
                current_page += 1
                await asyncio.sleep(delay_seconds)
                
            # Done
            if job_store.get_category_job(category_job_id)["status"] == "running":
                job_store.update_category_job(category_job_id, status="completed")
                generate_category_report(category_job_id)
                
        except Exception as e:
            logger.exception("Error during category discovery")
            job_store.update_category_job(category_job_id, status="failed", last_error=str(e))
        finally:
            await page.close()

async def extract_product_cards(page) -> List[Dict[str, Any]]:
    # Look for common product card selectors on Chewy.
    # Usually they are inside something like div[data-testid="product-list"] -> article or a
    
    # We can inject JS to find all links that look like products and grab their price text
    cards = await page.evaluate("""() => {
        const results = [];
        // Attempt to find product cards. Chewy often uses article or divs with class starting with 'ProductCard' or 'kib-product-card'
        const cardElements = document.querySelectorAll('article, .kib-product-card, div[data-testid="product-card"]');
        
        if (cardElements.length > 0) {
            for (const el of cardElements) {
                const link = el.querySelector('a');
                if (!link) continue;
                const url = link.href;
                if (!url.includes('/dp/')) continue;
                
                const titleEl = el.querySelector('h2, .kib-product-title, [data-testid="product-title"]');
                const title = titleEl ? titleEl.innerText : '';
                
                const priceEl = el.querySelector('.kib-product-price, [data-testid="product-price"], .price');
                const price = priceEl ? priceEl.innerText : '';
                
                const imgEl = el.querySelector('img');
                const image = imgEl ? imgEl.src : '';
                
                results.push({url, title, price, image});
            }
        } else {
            // Fallback: just look for all /dp/ links and find nearby price
            const links = document.querySelectorAll('a[href*="/dp/"]');
            const seen = new Set();
            for (const link of links) {
                const url = link.href;
                if (seen.has(url)) continue;
                seen.add(url);
                
                const container = link.closest('div');
                let price = '';
                if (container) {
                    const priceEl = container.querySelector('.price, [data-testid*="price"]');
                    if (priceEl) price = priceEl.innerText;
                }
                results.push({url, title: link.innerText, price: price, image: ''});
            }
        }
        return results;
    }""")
    
    # dedupe by url
    unique = {}
    for c in cards:
        u = c["url"].split("?")[0]
        if u not in unique:
            unique[u] = c
    return list(unique.values())

def generate_category_report(category_job_id: str):
    job = job_store.get_category_job(category_job_id)
    if not job:
        return
        
    items = job_store.get_category_items(category_job_id)
    
    report = {
        "category_job_id": job["category_job_id"],
        "category_url": job["category_url"],
        "status": job["status"],
        "price_filter": {
            "price_min": job["price_min"],
            "price_max": job["price_max"],
            "mode": job["mode"]
        },
        "summary": {
            "pages_processed": job["total_pages_discovered"],
            "total_cards_found": job["total_cards_found"],
            "unique_urls_found": job["total_urls_found"],
            "filtered_in": job["total_urls_after_price_filter"],
            "filtered_out": len([i for i in items if i["status"] == "filtered_out"]),
            "ambiguous_price_kept": len([i for i in items if i["status"] == "filtered_in" and i["card_price_min"] is None]),
            "duplicates": len([i for i in items if i["status"] == "duplicate"])
        },
        "items": []
    }
    
    discovered_urls = []
    filtered_urls = []
    
    for item in items:
        report["items"].append({
            "product_url": item["product_url"],
            "title": item["title"],
            "card_price_raw": item["card_price_raw"],
            "status": item["status"],
            "filter_reason": item["filter_reason"]
        })
        
        discovered_urls.append(item["product_url"])
        if item["status"] in ("discovered", "filtered_in"):
            filtered_urls.append(item["product_url"])
            
    out_dir = job["output_dir"]
    import os
    os.makedirs(out_dir, exist_ok=True)
    
    with open(os.path.join(out_dir, "category_discovery_report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
        
    with open(os.path.join(out_dir, "discovered_urls.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(discovered_urls))
        
    with open(os.path.join(out_dir, "filtered_urls.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(filtered_urls))
