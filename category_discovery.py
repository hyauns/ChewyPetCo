import asyncio
import json
import logging
import re
from typing import Any, Dict, List
from urllib.parse import urljoin, urlparse

from playwright.async_api import async_playwright
import category_price_filter
import job_store
import adspower
import adsp_profile_pool_manager
import config

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
    
    profile_id = adsp_profile_pool_manager.get_next_available_profile()
    if not profile_id:
        logger.error("No profiles available in pool.")
        job_store.update_category_job(category_job_id, status="paused", last_error="all_profiles_exhausted")
        return
        
    adsp_profile_pool_manager.mark_profile_in_use(profile_id)
    
    async with async_playwright() as p:
        try:
            profile_data = adspower.start_profile(profile_id)
            ws_url = adspower.get_ws_endpoint(profile_data)
            browser = await p.chromium.connect_over_cdp(ws_url)
            context = browser.contexts[0]
            logger.info(f"Connected to AdsPower CDP at {ws_url}")
        except Exception as e:
            logger.info(f"Failed to connect to AdsPower CDP, falling back to launch: {e}")
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
        page = await context.new_page()
        
        try:
            previous_first_urls = []
            stale_attempts = 0
            
            while True:
                if max_pages and current_page > max_pages:
                    logger.info(f"Reached max pages limit ({max_pages}). Stopping discovery.")
                    break
                    
                page_url = category_url
                if current_page > 1:
                    sep = "&" if "?" in category_url else "?"
                    page_url = f"{category_url}{sep}page={current_page}"
                
                logger.info(f"[{category_job_id}] Discovering page {current_page}: {page_url}")
                try:
                    await page.goto(page_url, wait_until="domcontentloaded", timeout=60000)
                    # wait for products to load, either grid or main
                    await page.wait_for_timeout(3000)
                except Exception as e:
                    logger.error(f"Failed to load page {current_page}: {e}")
                    job_store.update_category_job(category_job_id, status="paused", last_error=str(e))
                    break
                
                final_url = page.url
                
                # Phase 4 - White Screen Detection
                detection_result = await adsp_profile_pool_manager.detect_white_screen_block(page, final_url)
                if detection_result["is_white_screen"]:
                    logger.error(f"White screen detected on category page {current_page}")
                    
                    if config.ADSP_SAVE_WHITE_SCREEN_SCREENSHOT:
                        try:
                            import uuid, os
                            os.makedirs("output/white_screen_events", exist_ok=True)
                            screenshot_path = f"output/white_screen_events/temp_{uuid.uuid4().hex}.png"
                            await page.screenshot(path=screenshot_path)
                            detection_result["screenshot_path"] = screenshot_path
                        except: pass
                    if config.ADSP_SAVE_WHITE_SCREEN_HTML:
                        try:
                            import uuid
                            html_path = f"output/white_screen_events/temp_{uuid.uuid4().hex}.html"
                            with open(html_path, "w", encoding="utf-8") as f:
                                f.write(await page.content())
                            detection_result["html_snapshot_path"] = html_path
                        except: pass
                        
                    adsp_profile_pool_manager.quarantine_profile(profile_id, f"White screen on {final_url}")
                    adsp_profile_pool_manager.record_white_screen_event(category_job_id, 0, final_url, profile_id, "profile_quarantined", detection_result)
                    
                    # Pause the job for manual intervention, do NOT increment page, so we retry this page later
                    job_store.update_category_job(category_job_id, status="paused", last_error="white_screen_block")
                    break
                    
                adsp_profile_pool_manager.mark_profile_success(profile_id)
                
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
                    
                # Calculate counts
                raw_card_count = len(cards_data)
                sponsored_cards = [c for c in cards_data if c.get("is_sponsored")]
                organic_cards = [c for c in cards_data if not c.get("is_sponsored")]
                organic_card_count = len(organic_cards)
                sponsored_card_count = len(sponsored_cards)
                
                # Unique URLs
                unique_urls_on_page = len(set(c["url"] for c in organic_cards))
                
                logger.info(f"Found {raw_card_count} raw cards, {organic_card_count} organic cards on page {current_page}")
                
                # Verification for stale page
                current_first_urls = [c["url"] for c in organic_cards[:5]]
                if current_page > 1 and current_first_urls == previous_first_urls:
                    logger.warning(f"Page {current_page} has identical first 5 URLs as previous page. Possible stale pagination.")
                    stale_attempts += 1
                    if stale_attempts > 1:
                        logger.error("Pagination is definitely stale/repeating. Stopping discovery.")
                        break
                else:
                    stale_attempts = 0
                previous_first_urls = current_first_urls
                
                page_summary = {
                    "category_job_id": category_job_id,
                    "page_number": current_page,
                    "target_url": page_url,
                    "final_url": final_url,
                    "page_title": await page.title(),
                    "raw_card_count": raw_card_count,
                    "organic_card_count": organic_card_count,
                    "sponsored_card_count": sponsored_card_count,
                    "excluded_card_count": 0,
                    "unique_product_urls_on_page": unique_urls_on_page,
                    "first_5_product_urls": current_first_urls,
                    "page_status": "ok" if stale_attempts == 0 else "stale_repeated_page"
                }
                
                new_urls_added = 0
                excluded_card_count = 0
                for card in cards_data:
                    # Skip sponsored if configured
                    if config.CATEGORY_EXCLUDE_SPONSORED_PRODUCTS and card.get("is_sponsored"):
                        excluded_card_count += 1
                        continue
                        
                    raw_price = card.get("price", "")
                    parsed_price = category_price_filter.parse_price_to_float(raw_price)
                    
                    filter_res = category_price_filter.product_card_matches_price_filter(
                        parsed_price, price_min, price_max, mode
                    )
                    
                    product_id = job_store.extract_chewy_product_id(card["url"])
                    if config.CHEWY_GLOBAL_DEDUP_ENABLED and product_id:
                        reg_res = job_store.check_and_update_product_registry(product_id, card["url"], category_job_id)
                        registry_item = reg_res["registry_item"]
                        
                        # Only skip if it's already extracted, output exists, and we don't force reprocess
                        if registry_item["extraction_status"] == "extracted_success" and config.CHEWY_SKIP_ALREADY_EXTRACTED and not config.CHEWY_REPROCESS_EXISTING:
                            grouped_path = registry_item.get("grouped_output_path")
                            conf_score = registry_item.get("confidence_score", 0)
                            if grouped_path and Path(grouped_path).exists() and conf_score >= config.CHEWY_JSON_CONFIDENCE_THRESHOLD:
                                filter_res["status"] = "duplicate_existing_success"
                                filter_res["reason"] = f"Product already extracted successfully (Score: {conf_score})"
                    
                    try:
                        job_store.add_category_item(
                            category_job_id=category_job_id,
                            page_number=current_page,
                            source_category_url=final_url,
                            product_url=card["url"],
                            product_id=product_id,
                            status=filter_res["status"],
                            title=card.get("title"),
                            brand=card.get("brand"),
                            card_price_raw=raw_price,
                            card_price_min=parsed_price["price_min"],
                            card_price_max=parsed_price["price_max"],
                            image_url=card.get("image"),
                            filter_reason=filter_res["reason"],
                            metadata_json={"parsed_price_confidence": parsed_price["confidence"], "product_id": product_id, "is_sponsored": card.get("is_sponsored", False)}
                        )
                        new_urls_added += 1
                    except Exception as ins_e:
                        # Ignore unique constraint errors within same page
                        pass
                
                page_summary["excluded_card_count"] = excluded_card_count
                page_summary["new_urls_added_on_page"] = new_urls_added
                
                if config.CATEGORY_DISCOVERY_SAVE_PAGE_DEBUG:
                    import os, json
                    job = job_store.get_category_job(category_job_id)
                    job_dir = job.get("output_dir")
                    if job_dir:
                        pages_dir = os.path.join(job_dir, "pages")
                        os.makedirs(pages_dir, exist_ok=True)
                        with open(os.path.join(pages_dir, f"page_{current_page}_summary.json"), "w", encoding="utf-8") as f:
                            json.dump(page_summary, f, indent=2)
                
                job_store.update_category_job(
                    category_job_id,
                    current_page=current_page + 1,
                    total_pages_discovered=current_page
                )
                job_store.update_category_job_counts(category_job_id)
                
                # Pagination check
                if organic_card_count < 10:
                    logger.info(f"Fewer than 10 organic cards on page {current_page}, assuming last page.")
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
                let url = link.href;
                
                // Exclude dynamic tracker URLs completely if they don't resolve to /dp/ immediately, 
                // but sometimes Chewy uses /api/event/p/sar/click.
                // We'll mark them as sponsored.
                let isSponsored = false;
                
                // Check for sponsored tracker URL
                if (url.includes('/api/event/') || url.includes('adsOrigin=')) {
                    isSponsored = true;
                    // Try to find the real URL in data attributes if possible, or fallback
                    if (el.dataset && el.dataset.trackUrl) {
                         // Some chewy cards have real urls in dataset
                    }
                }
                
                // Check text labels for "Sponsored"
                const textContent = el.innerText.toLowerCase();
                if (textContent.includes('sponsored') || textContent.includes('ad ')) {
                    isSponsored = true;
                }
                
                // Check aria-labels or data attributes
                if (el.getAttribute('aria-label') && el.getAttribute('aria-label').toLowerCase().includes('sponsored')) {
                    isSponsored = true;
                }
                
                // If it's not a /dp/ link and not a tracker, maybe skip
                if (!url.includes('/dp/') && !isSponsored) continue;
                
                const titleEl = el.querySelector('h2, .kib-product-title, [data-testid="product-title"]');
                const title = titleEl ? titleEl.innerText : '';
                
                const priceEl = el.querySelector('.kib-product-price, [data-testid="product-price"], .price');
                const price = priceEl ? priceEl.innerText : '';
                
                const imgEl = el.querySelector('img');
                const image = imgEl ? imgEl.src : '';
                
                results.push({url, title, price, image, is_sponsored: isSponsored});
            }
        } else {
            // Fallback: just look for all /dp/ links and find nearby price
            const links = document.querySelectorAll('a[href*="/dp/"], a[href*="/api/event/"]');
            const seen = new Set();
            for (const link of links) {
                const url = link.href;
                if (seen.has(url)) continue;
                seen.add(url);
                
                let isSponsored = url.includes('/api/event/') || url.includes('adsOrigin=');
                const container = link.closest('div');
                let price = '';
                if (container) {
                    const priceEl = container.querySelector('.price, [data-testid*="price"]');
                    if (priceEl) price = priceEl.innerText;
                    if (container.innerText.toLowerCase().includes('sponsored')) {
                        isSponsored = true;
                    }
                }
                results.push({url, title: link.innerText, price: price, image: '', is_sponsored: isSponsored});
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
