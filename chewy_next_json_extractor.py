import asyncio
import json
import re
import sys
from pathlib import Path
from rich.console import Console
from playwright.async_api import async_playwright

import config
import adspower
import adsp_profile_pool_manager

console = Console()
OUT_DIR = Path(config.OUTPUT_DIR)
CACHE_DIR = OUT_DIR / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

def find_json_paths(data, target_keywords, current_path="", results=None):
    if results is None:
        results = []
        
    if isinstance(data, dict):
        for k, v in data.items():
            new_path = f"{current_path}.{k}" if current_path else str(k)
            k_lower = str(k).lower()
            
            for kw in target_keywords:
                if kw.lower() in k_lower:
                    preview = str(v)[:100] + ("..." if len(str(v)) > 100 else "")
                    results.append({
                        "matched_keyword": kw,
                        "key": k,
                        "path": new_path,
                        "type": type(v).__name__,
                        "preview": preview
                    })
                    break 
                    
            find_json_paths(v, target_keywords, new_path, results)
            
    elif isinstance(data, list):
        for i, item in enumerate(data):
            new_path = f"{current_path}[{i}]"
            find_json_paths(item, target_keywords, new_path, results)
            
    return results

# 1. Refactor into clear parser functions
async def read_page_content_with_retry(page, attempts: int = 5) -> str:
    """Read page HTML, retrying when Playwright catches the page mid-navigation."""
    last_error = None
    transient_messages = (
        "page is navigating",
        "unable to retrieve content",
        "execution context was destroyed",
        "navigation",
    )

    for attempt in range(1, attempts + 1):
        try:
            if attempt > 1:
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=10000)
                except Exception:
                    pass
                try:
                    await page.wait_for_load_state("networkidle", timeout=5000)
                except Exception:
                    pass
            return await page.content()
        except Exception as exc:
            last_error = exc
            message = str(exc).lower()
            if not any(token in message for token in transient_messages):
                raise
            if attempt == attempts:
                break
            delay = min(2 * attempt, 8)
            console.print(
                f"[yellow]Page content changed during navigation; retrying HTML read "
                f"({attempt}/{attempts}) after {delay}s...[/]"
            )
            await asyncio.sleep(delay)

    raise last_error


async def fetch_initial_html(url: str, page) -> str:
    console.print(f"Fetching initial HTML for: {url}")
    try:
        await page.goto(url, timeout=config.PAGE_LOAD_TIMEOUT, wait_until="domcontentloaded")
    except Exception as exc:
        if "Timeout" in str(exc):
            console.print(f"[red]Page load timeout ({config.PAGE_LOAD_TIMEOUT}ms). Proxy có thể bị chậm hoặc kết nối bị treo.[/red]")
        else:
            console.print(f"[red]Lỗi khi tải trang: {str(exc)}[/red]")
        raise
    await asyncio.sleep(4)
    return await read_page_content_with_retry(page)

def extract_next_data_from_html(html: str) -> dict:
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">([^<]+)</script>', html)
    if m:
        try:
            return json.loads(m.group(1))
        except:
            pass
    return None

def detect_next_build_id(next_data: dict, html: str) -> str:
    if next_data and next_data.get("buildId"):
        return next_data.get("buildId")
    m = re.search(r"/_next/data/([^/]+)/", html)
    if m:
        return m.group(1)
    return None

def build_next_data_url(url: str, build_id: str) -> str:
    match = re.search(r"chewy\.com/(.*?)/dp/(\d+)", url)
    if not match:
        return None
    slug = match.group(1)
    product_id = match.group(2)
    return f"https://www.chewy.com/_next/data/{build_id}/en-US/{slug}/dp/{product_id}.json?id={product_id}&slug={slug}"

async def fetch_next_data_json(url: str, page, build_id: str, variant_id: str) -> dict:
    cache_path = CACHE_DIR / f"{variant_id}_{build_id}.json"
    if cache_path.exists():
        with open(cache_path, "r", encoding="utf-8") as f:
            return json.load(f)
            
    console.print(f"Fetching JSON from next data url: {url}")
    response = await page.request.get(url)
    if response.status == 200:
        data = await response.json()
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        return data
    else:
        console.print(f"[red]Failed to fetch JSON for {variant_id}, status: {response.status}[/]")
        return None

def detect_chewy_architecture(next_data: dict) -> str:
    props = next_data.get("props", {}).get("pageProps", {})
    has_apollo = "__APOLLO_STATE__" in props
    has_redux = "initialState" in props
    
    if has_apollo and has_redux:
        console.print("[yellow]Warning: Both Apollo and Redux states detected; Apollo selected.[/yellow]")
        return "apollo"
    elif has_apollo:
        return "apollo"
    elif has_redux:
        return "redux"
    else:
        console.print("[yellow]Diagnostic warning: Unknown architecture. Neither __APOLLO_STATE__ nor initialState found.[/yellow]")
        return "unknown"


def classify_gtin(value: str) -> dict:
    if not value:
        return {"raw": value, "normalized": value, "type": "unknown", "is_valid_length": False, "checksum_valid": None}
    
    val_str = str(value).replace(" ", "").replace("-", "")
    digits_only = "".join(c for c in val_str if c.isdigit())
    
    length = len(digits_only)
    id_type = "unknown"
    is_valid_length = False
    
    if length == 12:
        id_type = "upc"
        is_valid_length = True
    elif length == 13:
        id_type = "ean"
        is_valid_length = True
    elif length == 14:
        id_type = "gtin14"
        is_valid_length = True
        
    return {
        "raw": str(value).strip(),
        "normalized": digits_only if is_valid_length else val_str,
        "type": id_type,
        "is_valid_length": is_valid_length,
        "checksum_valid": None
    }

def build_variant_identifiers(gtin: str, source_sku: str, source_item_id: str, mpn: str = None) -> dict:
    idents = {
        "upc": None,
        "gtin": None,
        "ean": None,
        "mpn": mpn,
        "source_sku": str(source_sku) if source_sku else None,
        "source_item_id": str(source_item_id) if source_item_id else None
    }
    
    if gtin:
        classified = classify_gtin(gtin)
        if classified["is_valid_length"]:
            idents["gtin"] = classified["raw"]
            if classified["type"] == "upc":
                idents["upc"] = classified["raw"]
            elif classified["type"] == "ean":
                idents["ean"] = classified["raw"]
                
    return idents

def parse_apollo_product(next_data: dict, source_url: str) -> dict:
    props = next_data.get("props", {}).get("pageProps", {})
    apollo_state = props.get("__APOLLO_STATE__", {})
    
    match = re.search(r"chewy\.com/(.*?)/dp/(\d+)", source_url)
    slug = match.group(1) if match else "unknown"
    base_product_id = match.group(2) if match else "unknown"
    
    title = ""
    brand = ""
    desc = ""
    ingredients = ""
    guaranteed_analysis = ""
    specs = {}
    feeding_inst = None
    images = []
    breadcrumbs = []
    
    product_node = None
    item_nodes = []
    
    for k, v in apollo_state.items():
        if not isinstance(v, dict): continue
        if k.startswith("Product:"):
            product_node = v
        elif k.startswith("Item:"):
            item_nodes.append(v)
        if v.get("__typename") == "Breadcrumb":
            breadcrumbs.append(v.get("name"))
            
    if product_node:
        title = product_node.get("name", "")
        desc = product_node.get("description", "")
        brand = product_node.get("manufacturerName", "")
        
    import base64
    base64_id = base64.b64encode(f"Item:{base_product_id}".encode()).decode()
    main_item = apollo_state.get(f"Item:{base64_id}")
    if not main_item and item_nodes:
        main_item = item_nodes[0]
        
    if main_item:
        if not title: title = main_item.get("name", "")
        if not desc: desc = main_item.get("description", "")
        info_groups = main_item.get("infoGroups", [])
        for group in info_groups:
            if not isinstance(group, dict): continue
            sections = group.get("sections", [])
            for sec in sections:
                if not isinstance(sec, dict): continue
                usage = sec.get("usage", "")
                content_node = sec.get("content", {})
                content = content_node.get("content", "") if isinstance(content_node, dict) else ""
                if usage == "INGREDIENTS":
                    ingredients = content
                elif usage == "GUARANTEED_ANALYSIS":
                    guaranteed_analysis = content
                elif usage == "FEEDING_INSTRUCTIONS":
                    if feeding_inst is None: feeding_inst = content
                    else: feeding_inst += "\n\n" + content
                elif usage == "DESCRIPTION":
                    desc = content
                elif usage == "KEY_BENEFITS":
                    specs["Key Benefits"] = content
                    
    variants_data = item_nodes
    normalized_variants = []
    for v in variants_data:
        v_id = v.get("partNumber") or v.get("id")
        if not v_id: continue
        
        option_values = {}
        def_attrs = v.get("definingAttributes", [])
        if isinstance(def_attrs, list):
            for attr in def_attrs:
                if isinstance(attr, dict) and "name" in attr:
                    option_values[attr.get("name", "").lower()] = attr.get("value", "")
                    
        attr_key = next((ak for ak in v.keys() if "attributeValues" in ak), None)
        if attr_key:
            val_list = v.get(attr_key)
            if isinstance(val_list, list):
                for ref_obj in val_list:
                    ref_id = ref_obj.get("__ref")
                    if ref_id:
                        ref_data = apollo_state.get(ref_id)
                        if ref_data:
                            attr_meta = ref_data.get("attribute", {})
                            if isinstance(attr_meta, dict):
                                attr_name = attr_meta.get("name")
                                if attr_name:
                                    option_values[attr_name.lower()] = ref_data.get("value", "")
                                    
        price = v.get("advertisedPrice") or v.get("price")
        if isinstance(price, dict):
            price = price.get("salePrice") or price.get("price")
            
        in_stock = v.get("inStock")
        if in_stock is None:
            in_stock = v.get("availability") == "AVAILABLE"
            
        v_images = []
        full_img = v.get("fullImage", {})
        if isinstance(full_img, dict):
            for img_k, img_v in full_img.items():
                if "url(" in img_k and "1800" in img_k:
                    v_images.append(img_v)
            if not v_images:
                for img_k, img_v in full_img.items():
                    if "url(" in img_k:
                        v_images.append(img_v)
                        break
                        
        raw_gtin = v.get("gtin")
        mpn = v.get("manufacturerPartNumber")
        idents = build_variant_identifiers(raw_gtin, v_id, v_id, mpn)
        
        normalized_variants.append({
            "source_variant_id": v_id,
            "sku": v_id,
            "identifiers": idents,
            "title": v.get("name", ""),
            "option_values": option_values,
            "price": price,
            "compare_at_price": v.get("listPrice"),
            "autoship_price": v.get("autoshipPrice"),
            "availability": v.get("availability"),
            "in_stock": in_stock,
            "images": v_images, 
            "variant_url": f"https://www.chewy.com/{slug}/dp/{v_id}"
        })
        
    if not normalized_variants:
        price = None
        if product_node and isinstance(product_node.get("price"), dict):
            price = product_node["price"].get("salePrice") or product_node["price"].get("price")
        if not price and main_item and isinstance(main_item.get("price"), dict):
            price = main_item["price"].get("salePrice") or main_item["price"].get("price")
            
        normalized_variants.append({
            "source_variant_id": base_product_id,
            "sku": base_product_id,
            "identifiers": build_variant_identifiers(None, base_product_id, base_product_id, None),
            "title": title,
            "option_values": {},
            "price": price,
            "compare_at_price": None,
            "autoship_price": None,
            "availability": None,
            "in_stock": True,
            "images": images,
            "variant_url": source_url
        })
        
    return {
        "source": "chewy",
        "source_url": source_url,
        "source_product_id": base_product_id,
        "slug": slug,
        "architecture": "apollo",
        "title": title,
        "brand": brand,
        "category_path": breadcrumbs,
        "description": desc,
        "ingredients": ingredients,
        "guaranteed_analysis": guaranteed_analysis,
        "specifications": specs,
        "feeding_instructions": feeding_inst,
        "images": images,
        "variants": normalized_variants,
        "warnings": []
    }

def parse_redux_product(next_data: dict, source_url: str) -> dict:
    props = next_data.get("props", {}).get("pageProps", {})
    redux_state = props.get("initialState", {})
    
    match = re.search(r"chewy\.com/(.*?)/dp/(\d+)", source_url)
    slug = match.group(1) if match else "unknown"
    base_product_id = match.group(2) if match else "unknown"
    
    page_route = next_data.get("page", "")
    if "plp" in page_route.lower():
        return {
            "source": "chewy",
            "source_url": source_url,
            "source_product_id": base_product_id,
            "slug": slug,
            "architecture": "redux",
            "title": "",
            "brand": "",
            "category_path": [],
            "description": "",
            "ingredients": "",
            "guaranteed_analysis": "",
            "specifications": {},
            "feeding_instructions": "",
            "images": [],
            "variants": [],
            "warnings": ["Route is a PLP page, not a PDP. Extractor returned empty data."]
        }
        
    title = ""
    brand = ""
    desc = ""
    ingredients = ""
    guaranteed_analysis = ""
    specs = {}
    feeding_inst = None
    images = []
    breadcrumbs = []
    variants_list = []
    
    def deep_find_key(d, key_substring):
        results = []
        if isinstance(d, dict):
            for k, v in d.items():
                if key_substring.lower() in k.lower():
                    results.append(v)
                results.extend(deep_find_key(v, key_substring))
        elif isinstance(d, list):
            for item in d:
                results.extend(deep_find_key(item, key_substring))
        return results
        
    product_candidates = deep_find_key(redux_state, "product")
    best_product = None
    for cand in product_candidates:
        if isinstance(cand, dict) and cand.get("partNumber") == base_product_id:
            best_product = cand
            break
            
    if not best_product and product_candidates:
        for cand in product_candidates:
            if isinstance(cand, dict) and ("partNumber" in cand or "name" in cand):
                best_product = cand
                break
                
    if best_product:
        title = best_product.get("name", "")
        brand = best_product.get("manufacturerName", "") or best_product.get("brand", "")
        desc = best_product.get("description", "")
        ingredients = best_product.get("ingredients", "")
        images_data = best_product.get("images", [])
        if isinstance(images_data, list):
            for img in images_data:
                if isinstance(img, dict) and "url" in img:
                    images.append(img["url"])
                elif isinstance(img, str):
                    images.append(img)
                    
        variants_candidates = best_product.get("variants") or best_product.get("items")
        if variants_candidates and isinstance(variants_candidates, list):
            for v in variants_candidates:
                if not isinstance(v, dict): continue
                v_id = v.get("partNumber") or v.get("id")
                if not v_id: continue
                
                price = v.get("price") or v.get("advertisedPrice")
                if isinstance(price, dict):
                    price = price.get("salePrice") or price.get("price")
                    
                in_stock = v.get("inStock")
                if in_stock is None:
                    in_stock = v.get("availability") == "AVAILABLE"
                    
                option_values = {}
                attrs = v.get("definingAttributes", [])
                if isinstance(attrs, list):
                    for attr in attrs:
                        if isinstance(attr, dict) and "name" in attr:
                            option_values[attr.get("name", "").lower()] = attr.get("value", "")
                            
                v_imgs = []
                
                raw_gtin = v.get("gtin")
                mpn = v.get("manufacturerPartNumber")
                idents = build_variant_identifiers(raw_gtin, v_id, v_id, mpn)
                
                variants_list.append({
                    "source_variant_id": v_id,
                    "sku": v_id,
                    "identifiers": idents,
                    "title": v.get("name", title),
                    "option_values": option_values,
                    "price": price,
                    "compare_at_price": v.get("listPrice"),
                    "autoship_price": v.get("autoshipPrice"),
                    "availability": v.get("availability"),
                    "in_stock": in_stock,
                    "images": v_imgs,
                    "variant_url": f"https://www.chewy.com/{slug}/dp/{v_id}"
                })
                
    if not variants_list and best_product:
        v_id = best_product.get("partNumber", base_product_id)
        raw_gtin = best_product.get("gtin")
        mpn = best_product.get("manufacturerPartNumber")
        idents = build_variant_identifiers(raw_gtin, v_id, v_id, mpn)
        
        variants_list.append({
            "source_variant_id": v_id,
            "sku": v_id,
            "identifiers": idents,
            "title": title,
            "option_values": {},
            "price": best_product.get("price") or best_product.get("advertisedPrice"),
            "compare_at_price": best_product.get("listPrice"),
            "autoship_price": best_product.get("autoshipPrice"),
            "availability": best_product.get("availability"),
            "in_stock": best_product.get("inStock", True),
            "images": images,
            "variant_url": source_url
        })

    return {
        "source": "chewy",
        "source_url": source_url,
        "source_product_id": base_product_id,
        "slug": slug,
        "architecture": "redux",
        "title": title,
        "brand": brand,
        "category_path": breadcrumbs,
        "description": desc,
        "ingredients": ingredients,
        "guaranteed_analysis": guaranteed_analysis,
        "specifications": specs,
        "feeding_instructions": feeding_inst,
        "images": images,
        "variants": variants_list,
        "warnings": ["Redux parser used. Extracted fields may be incomplete due to diverse state shapes."]
    }

def empty_feeding_instructions() -> dict:
    return {
        "summary": "",
        "tables": [],
        "transition_instructions": {
            "plain_text": "",
            "days": []
        },
        "source_raw": ""
    }

def normalize_feeding_instructions_safe(fi_raw, warnings_list) -> dict:
    if not fi_raw:
        warnings_list.append("Feeding instructions missing or empty.")
        return empty_feeding_instructions()
        
    if isinstance(fi_raw, dict):
        out = empty_feeding_instructions()
        out["summary"] = str(fi_raw.get("summary", ""))
        tables = fi_raw.get("tables", [])
        out["tables"] = tables if isinstance(tables, list) else []
        trans = fi_raw.get("transition_instructions", {})
        if isinstance(trans, dict):
            out["transition_instructions"]["plain_text"] = str(trans.get("plain_text", ""))
            days = trans.get("days", [])
            out["transition_instructions"]["days"] = days if isinstance(days, list) else []
        out["source_raw"] = str(fi_raw.get("source_raw", json.dumps(fi_raw)))
        return out
        
    if isinstance(fi_raw, list):
        fi_raw = "\n".join([str(x) for x in fi_raw if x])
        if not fi_raw:
            warnings_list.append("Feeding instructions missing or empty.")
            return empty_feeding_instructions()

    if not isinstance(fi_raw, str):
        warnings_list.append("Feeding instructions could not be parsed.")
        return empty_feeding_instructions()

    tables = []
    summary_lines = []
    fi_lines = fi_raw.split('\n')
    in_table = False
    current_table = None
    
    for line in fi_lines:
        clean_line = line.strip()
        if clean_line.startswith("|") and clean_line.endswith("|"):
            if "---" in clean_line:
                continue
            parts = [p.strip() for p in clean_line.strip("|").split("|")]
            if not in_table:
                in_table = True
                current_table = {
                    "title": "Daily Feeding Guide",
                    "columns": parts,
                    "rows": []
                }
                tables.append(current_table)
            else:
                row_dict = {}
                for i, col in enumerate(current_table["columns"]):
                    val = parts[i] if i < len(parts) else ""
                    if val.endswith(")") and "(" not in val:
                        val = val[:-1]
                    row_dict[col] = val
                current_table["rows"].append(row_dict)
        else:
            in_table = False
            summary_lines.append(clean_line)

    summary_text = re.sub(r'<[^>]+>', '', '\n'.join(summary_lines)).strip()
    
    return {
        "summary": summary_text,
        "tables": tables,
        "transition_instructions": {
            "plain_text": "",
            "days": []
        },
        "source_raw": fi_raw
    }

def normalize_chewy_product(raw_product: dict) -> dict:
    content_sections = {}
    
    desc_raw = raw_product.get("description", "")
    content_sections["description"] = {
        "plain_text": re.sub(r'<[^>]+>', '', desc_raw).strip() if desc_raw else "",
        "html": desc_raw,
        "rewrite_required": True,
        "source_raw": desc_raw
    }
    
    ing_raw = raw_product.get("ingredients", "")
    plain_text = re.sub(r'<[^>]+>', '', ing_raw).strip() if ing_raw else ""
    items = [i.strip() for i in re.split(r',\s*', plain_text) if i.strip()] if plain_text else []
    primary = items[:5] if items else []
    
    title_lower = raw_product.get("title", "").lower()
    specs_str = str(raw_product.get("specifications", {})).lower()
    
    contains = {
        "grain_free": "grain-free" in title_lower or "grain free" in title_lower or "grain free" in specs_str,
        "contains_chicken": "chicken" in plain_text.lower(),
        "contains_salmon": "salmon" in plain_text.lower(),
        "contains_beef": "beef" in plain_text.lower(),
        "contains_lamb": "lamb" in plain_text.lower(),
        "contains_turkey": "turkey" in plain_text.lower(),
        "contains_duck": "duck" in plain_text.lower(),
        "contains_tuna": "tuna" in plain_text.lower(),
        "contains_whitefish": "whitefish" in plain_text.lower(),
        "contains_rabbit": "rabbit" in plain_text.lower(),
        "contains_poultry": "poultry" in plain_text.lower(),
        "contains_corn": "corn" in plain_text.lower(),
        "contains_wheat": "wheat" in plain_text.lower(),
        "contains_soy": "soy" in plain_text.lower(),
        "contains_rice": "rice" in plain_text.lower()
    }
    
    content_sections["ingredients"] = {
        "plain_text": plain_text,
        "items": items,
        "primary_ingredients": primary,
        "contains": contains,
        "source_raw": ing_raw
    }
    
    ga_raw = raw_product.get("guaranteed_analysis", "")
    ga_rows = []
    lines = [L.strip() for L in re.sub(r'<[^>]+>', '\n', ga_raw).split('\n') if L.strip()]
    for line in lines:
        if line.startswith("|") and line.endswith("|"):
            if "---" in line: continue
            parts = [p.strip() for p in line.strip("|").split("|")]
            if len(parts) >= 2:
                nutrient = parts[0]
                raw_val = parts[1]
                match = re.search(r"([\d\.]+)\s*(%|ppm|IU/kg|mg/kg|kcal/kg|kcal/cup|kcal/can|kcal/oz|g/kg|mcg/kg).*?(min|max|approx|not less than|not more than)", raw_val, re.IGNORECASE)
                if match:
                    amount = match.group(1).strip()
                    unit = match.group(2).strip()
                    basis = match.group(3).lower()
                else:
                    amount, unit, basis = None, None, None
                ga_rows.append({
                    "nutrient": nutrient,
                    "amount": amount,
                    "unit": unit,
                    "basis": basis,
                    "raw_value": raw_val
                })
        else:
            if "min" in line.lower() or "max" in line.lower() or "%" in line:
                match = re.search(r"^(.*?)\s+([\d\.]+)\s*(%|ppm|IU/kg|mg/kg|kcal/kg|kcal/cup|kcal/can|kcal/oz|g/kg|mcg/kg).*?(min|max|approx|not less than|not more than)", line, re.IGNORECASE)
                if match:
                    ga_rows.append({
                        "nutrient": match.group(1).strip(' .'),
                        "amount": match.group(2).strip(),
                        "unit": match.group(3).strip(),
                        "basis": match.group(4).lower(),
                        "raw_value": line
                    })
                else:
                    ga_rows.append({
                        "nutrient": line,
                        "amount": None,
                        "unit": None,
                        "basis": None,
                        "raw_value": line
                    })
                
    content_sections["guaranteed_analysis"] = {
        "rows": ga_rows,
        "source_raw": ga_raw
    }
    
    fi_raw = raw_product.get("feeding_instructions")
    if "warnings" not in raw_product:
        raw_product["warnings"] = []
    content_sections["feeding_instructions"] = normalize_feeding_instructions_safe(fi_raw, raw_product["warnings"])
    
    cal_content = {
        "kcal_per_kg": None,
        "kcal_per_cup": None,
        "kcal_per_can": None,
        "kcal_per_oz": None,
        "raw_text": ""
    }
    full_text = f"{desc_raw} {specs_str} {ga_raw} {fi_raw} {title_lower}"
    m_kcal_kg = re.search(r"([\d,]+)\s*kcal/kg", full_text, re.IGNORECASE)
    m_kcal_cup = re.search(r"([\d\.]+)\s*kcal/cup", full_text, re.IGNORECASE)
    m_kcal_can = re.search(r"([\d\.]+)\s*kcal/can", full_text, re.IGNORECASE)
    m_kcal_oz = re.search(r"([\d\.]+)\s*kcal/oz", full_text, re.IGNORECASE)
    
    if m_kcal_kg: cal_content["kcal_per_kg"] = m_kcal_kg.group(1).replace(",", "")
    if m_kcal_cup: cal_content["kcal_per_cup"] = m_kcal_cup.group(1)
    if m_kcal_can: cal_content["kcal_per_can"] = m_kcal_can.group(1)
    if m_kcal_oz: cal_content["kcal_per_oz"] = m_kcal_oz.group(1)
    
    cal_raw = ""
    m_raw = re.search(r"([^\.\n\|]*kcal[^\.\n\|]*\.)", full_text, re.IGNORECASE)
    if m_raw:
        cal_raw = m_raw.group(1).strip()
    if not cal_raw and any([m_kcal_kg, m_kcal_cup, m_kcal_can, m_kcal_oz]):
        cal_raw = "Calorie content extracted."
        
    cal_content["raw_text"] = cal_raw
    
    content_sections["nutrition"] = {
        "calorie_content": cal_content,
        "nutrients": [],
        "source_raw": ""
    }
    
    specs_raw = raw_product.get("specifications", {})
    groups = []
    if isinstance(specs_raw, dict) and specs_raw:
        items = []
        for k, v in specs_raw.items():
            if isinstance(v, str):
                items.append({"label": k, "value": v, "normalized_key": re.sub(r'[^a-z0-9]', '_', k.lower()).strip('_')})
        if items:
            groups.append({"title": "Product Details", "items": items})
            
    content_sections["specifications"] = {
        "groups": groups,
        "source_raw": str(specs_raw)
    }
    
    product_facts = {
        "pet_type": None,
        "food_form": None,
        "life_stage": None,
        "breed_size": None,
        "primary_flavor": None,
        "special_diet": [],
        "package_type": None,
        "health_feature": [],
        "protein_source": []
    }
    
    path_str = " ".join(raw_product.get("category_path", [])).lower()
    
    if "dog" in path_str or "dog" in title_lower: product_facts["pet_type"] = "Dog"
    elif "cat" in path_str or "cat" in title_lower: product_facts["pet_type"] = "Cat"
    
    if "dry food" in path_str or "dry food" in title_lower: product_facts["food_form"] = "Dry Food"
    elif "wet food" in path_str or "wet food" in title_lower: product_facts["food_form"] = "Wet Food"
    elif "treat" in path_str or "treat" in title_lower: product_facts["food_form"] = "Treats"
    elif "toy" in path_str or "toy" in title_lower: product_facts["food_form"] = "Toy"
    elif "crate" in path_str or "crate" in title_lower: product_facts["food_form"] = "Crate"
    elif "collar" in path_str or "collar" in title_lower: product_facts["food_form"] = "Collar"
    
    if "puppy" in title_lower: product_facts["life_stage"] = "Puppy"
    elif "kitten" in title_lower: product_facts["life_stage"] = "Kitten"
    elif "adult" in title_lower: product_facts["life_stage"] = "Adult"
    elif "senior" in title_lower: product_facts["life_stage"] = "Senior"
    
    if "small breed" in title_lower: product_facts["breed_size"] = "Small Breeds"
    elif "large breed" in title_lower: product_facts["breed_size"] = "Large Breeds"
    
    if "bag" in title_lower: product_facts["package_type"] = "Bag"
    elif "can" in title_lower: product_facts["package_type"] = "Can"
    elif "pouch" in title_lower: product_facts["package_type"] = "Pouch"
    
    if contains["grain_free"]: product_facts["special_diet"].append("Grain-Free")
    
    for protein, has_protein in contains.items():
        if protein.startswith("contains_") and has_protein:
            product_facts["protein_source"].append(protein.replace("contains_", "").title())
            
    storefront_display = {
        "highlights": [],
        "accordion_sections": [
            {
                "key": "description",
                "title": "Description",
                "display_type": "rich_text",
                "enabled": bool(desc_raw)
            },
            {
                "key": "ingredients",
                "title": "Ingredients",
                "display_type": "paragraph_with_expand",
                "enabled": bool(ing_raw)
            },
            {
                "key": "guaranteed_analysis",
                "title": "Guaranteed Analysis",
                "display_type": "table",
                "enabled": bool(ga_raw)
            },
            {
                "key": "nutrition",
                "title": "Nutrition",
                "display_type": "calorie_card_plus_table",
                "enabled": bool(cal_content["raw_text"])
            },
            {
                "key": "feeding_instructions",
                "title": "Feeding Guide",
                "display_type": "table",
                "enabled": bool(content_sections["feeding_instructions"].get("tables")) or bool(content_sections["feeding_instructions"].get("summary"))
            },
            {
                "key": "specifications",
                "title": "Specifications",
                "display_type": "key_value_grid",
                "enabled": bool(groups)
            }
        ]
    }
    
    metafields_plan = {
        "custom.ingredients_json": content_sections["ingredients"],
        "custom.guaranteed_analysis_json": content_sections["guaranteed_analysis"],
        "custom.nutrition_json": content_sections["nutrition"],
        "custom.feeding_instructions_json": content_sections["feeding_instructions"],
        "custom.specifications_json": content_sections["specifications"],
        "custom.pet_type": product_facts["pet_type"],
        "custom.food_form": product_facts["food_form"],
        "custom.life_stage": product_facts["life_stage"],
        "custom.breed_size": product_facts["breed_size"],
        "custom.primary_flavor": None,
        "custom.special_diet": json.dumps(product_facts["special_diet"]),
        "custom.package_type": product_facts["package_type"],
        "custom.source_url": raw_product.get("source_url"),
        "custom.source_product_id": raw_product.get("source_product_id"),
        "custom.source_flavor": None
    }
    
    raw_product["content_sections"] = content_sections
    raw_product["product_facts"] = product_facts
    raw_product["storefront_display"] = storefront_display
    raw_product["metafields_plan"] = metafields_plan
    
    return raw_product

def split_product_by_flavor(normalized_product: dict) -> dict:
    variants = normalized_product.get("variants", [])
    groups = {}
    for v in variants:
        opts = v.get("option_values", {})
        flavor = None
        for k, val in opts.items():
            if "flavor" in k.lower():
                flavor = val
                break
        if not flavor:
            flavor = "Default"
            
        if flavor not in groups:
            groups[flavor] = []
        groups[flavor].append(v)
        
    products_out = []
    
    for flavor, flavor_variants in groups.items():
        base_title = flavor_variants[0].get("title") or normalized_product.get("title", "")
        clean_title = base_title
        
        suffix_pattern = r"(?:,\s*|\s+)(?:\d+(?:\.\d+)?(?:-| )?(?:lb|oz|kg|g)\s*(?:bag|can|pouch|tray|bottle|box|carton)s?|\d+\s*(?:cans?|count|pack|pouches?)|\b(?:case|pack)\s+of\s+\d+).*?$"
        clean_title = re.sub(suffix_pattern, "", clean_title, flags=re.IGNORECASE)
        
        slug_flavor = re.sub(r'[^a-z0-9]+', '-', str(flavor).lower()).strip('-')
        handle_slug = f"{normalized_product.get('slug', 'product')}"
        if flavor != "Default":
            handle_slug += f"-{slug_flavor}"
            
        group_id = f"{normalized_product.get('source_product_id')}"
        if flavor != "Default":
            group_id += f":flavor:{slug_flavor}"
            
        new_variants = []
        for v in flavor_variants:
            new_v = v.copy()
            size_val = "Default Title"
            opts = new_v.get("option_values", {})
            for k, val in opts.items():
                if "size" in k.lower() or "weight" in k.lower() or "count" in k.lower() or "pack" in k.lower():
                    size_val = val
                    break
            if "option_values" in new_v:
                del new_v["option_values"]
            new_v["option1_name"] = "Size"
            new_v["option1_value"] = size_val
            new_variants.append(new_v)
            
        flavor_images = []
        for v in flavor_variants:
            if v.get("images"):
                flavor_images.extend(v["images"])
        
        seen_images = set()
        deduped_images = []
        for img in flavor_images:
            if img not in seen_images:
                deduped_images.append(img)
                seen_images.add(img)
                
        if not deduped_images:
            deduped_images = normalized_product.get("images", [])
            
        debug = {
            "architecture": normalized_product.get("architecture"),
            "original_variant_count": len(flavor_variants),
            "image_source": "variant_flavor_images" if flavor_images else "base_product_fallback",
            "original_title": base_title,
            "cleaned_title": clean_title,
            "title_cleanup_applied": clean_title != base_title,
            "title_cleanup_removed_suffix": base_title[len(clean_title):] if clean_title != base_title else "",
            "parser_warnings": normalized_product.get("warnings", []).copy()
        }
            
        p_facts = normalized_product.get("product_facts", {}).copy()
        if flavor != "Default":
            p_facts["primary_flavor"] = flavor
            
        content_sections = normalized_product.get("content_sections", {}).copy()
        specs = content_sections.get("specifications", {}).copy()
        if not specs.get("groups"):
            fb_items = []
            if normalized_product.get("brand"): fb_items.append({"label": "Brand", "value": normalized_product["brand"], "normalized_key": "brand"})
            if p_facts.get("pet_type"): fb_items.append({"label": "Pet Type", "value": p_facts["pet_type"], "normalized_key": "pet_type"})
            if p_facts.get("food_form"): fb_items.append({"label": "Food Form", "value": p_facts["food_form"], "normalized_key": "food_form"})
            if p_facts.get("life_stage"): fb_items.append({"label": "Life Stage", "value": p_facts["life_stage"], "normalized_key": "life_stage"})
            if flavor != "Default": fb_items.append({"label": "Primary Flavor", "value": flavor, "normalized_key": "primary_flavor"})
            if p_facts.get("package_type"): fb_items.append({"label": "Package Type", "value": p_facts["package_type"], "normalized_key": "package_type"})
            
            if fb_items:
                specs["groups"] = [{"title": "Product Details", "items": fb_items}]
                specs["source_raw"] = "Fallback generated."
                debug["parser_warnings"].append("Specifications fallback generated from normalized product facts.")
        
        content_sections["specifications"] = specs
            
        m_plan = normalized_product.get("metafields_plan", {}).copy()
        m_plan["custom.primary_flavor"] = p_facts.get("primary_flavor")
        m_plan["custom.source_flavor"] = flavor
        
        storefront_display = normalized_product.get("storefront_display", {}).copy()
        highlights = []
        if p_facts.get("primary_flavor"): highlights.append(p_facts["primary_flavor"])
        if p_facts.get("life_stage"): highlights.append(p_facts["life_stage"])
        if p_facts.get("pet_type"): highlights.append(p_facts["pet_type"])
        storefront_display["highlights"] = highlights
        
        for idx, sec in enumerate(storefront_display.get("accordion_sections", [])):
            if sec["key"] == "specifications" and specs.get("groups"):
                storefront_display["accordion_sections"][idx]["enabled"] = True
            
        products_out.append({
            "source_group_id": group_id,
            "title": clean_title,
            "flavor": flavor if flavor != "Default" else None,
            "brand": normalized_product.get("brand", ""),
            "handle_slug": handle_slug,
            "category_path": normalized_product.get("category_path", []),
            "description": normalized_product.get("description", ""),
            "ingredients": normalized_product.get("ingredients", ""),
            "guaranteed_analysis": normalized_product.get("guaranteed_analysis", ""),
            "feeding_instructions": normalized_product.get("feeding_instructions", ""),
            "specifications": normalized_product.get("specifications", {}),
            "product_facts": p_facts,
            "content_sections": content_sections,
            "storefront_display": storefront_display,
            "metafields_plan": m_plan,
            "images": deduped_images,
            "variants": new_variants,
            "debug": debug
        })
        
    return {
        "source": normalized_product.get("source"),
        "source_product_id": normalized_product.get("source_product_id"),
        "source_url": normalized_product.get("source_url"),
        "architecture": normalized_product.get("architecture"),
        "grouping_strategy": "flavor_as_product_size_as_variant",
        "products": products_out
    }


def validate_normalized_product(normalized_product: dict, grouped_product: dict) -> dict:
    required = ["source_product_id", "source_url", "title"]
    preferred = ["brand", "price", "availability", "images", "description", "ingredients", "guaranteed_analysis", "feeding_instructions", "specifications"]
    
    missing_required = [f for f in required if not normalized_product.get(f)]
    missing_preferred = []
    
    for f in ["brand", "description", "ingredients", "guaranteed_analysis", "feeding_instructions"]:
        if not normalized_product.get(f):
            missing_preferred.append(f)
            
    if not normalized_product.get("images"):
        missing_preferred.append("images")
        
    if not normalized_product.get("specifications"):
        missing_preferred.append("specifications")
        
    variants = normalized_product.get("variants", [])
    
    total_variants = 0
    variants_with_gtin = 0
    variants_with_upc = 0
    variants_with_ean = 0
    variants_missing_gtin = 0
    
    if not variants:
        missing_required.append("variants")
    else:
        has_price = any(v.get("price") for v in variants)
        has_avail = any(v.get("availability") or v.get("in_stock") is not None for v in variants)
        if not has_price: missing_preferred.append("price")
        if not has_avail: missing_preferred.append("availability")
        
    warnings = normalized_product.get("warnings", [])
    grouped_products = grouped_product.get("products", [])
    
    content = normalized_product.get("content_sections", {})
    facts = normalized_product.get("product_facts", {})
    is_food = facts.get("food_form") in ["Dry Food", "Wet Food", "Treats"]
    
    if content.get("ingredients", {}).get("plain_text") and not content.get("ingredients", {}).get("items"):
        warnings.append("ingredients.items is empty despite having plain text.")
        
    ga = content.get("guaranteed_analysis", {})
    if ga.get("source_raw") and not ga.get("rows"):
        warnings.append("guaranteed_analysis raw found but rows parsed empty.")
        
    for r in ga.get("rows", []):
        if "|" in r.get("nutrient", ""):
            warnings.append("guaranteed_analysis.rows contains pipe-delimited markdown.")
            break
            
    fi = content.get("feeding_instructions", {})
    if fi.get("source_raw") and "|" in fi.get("source_raw") and not fi.get("tables"):
        warnings.append("feeding_instructions contains markdown table but tables[] is empty.")
        
    specs_groups = content.get("specifications", {}).get("groups", [])
    if normalized_product.get("specifications") and not specs_groups:
        warnings.append("specifications exist but no groups parsed.")
        
    if not grouped_products:
        warnings.append("Grouped products list is empty.")
    else:
        for i, p in enumerate(grouped_products):
            if not p.get("variants"):
                warnings.append(f"Group {i} has no variants.")
            else:
                for v in p["variants"]:
                    if v.get("option1_name") == "Flavor" or v.get("option_values", {}).get("flavor"):
                        warnings.append(f"Group {i} variant incorrectly retains Flavor option.")
                        
                    total_variants += 1
                    idents = v.get("identifiers")
                    if not idents:
                        warnings.append(f"Variant {v.get('source_variant_id', 'unknown')} missing identifiers object.")
                        variants_missing_gtin += 1
                    else:
                        if v.get("sku") and not idents.get("source_sku"):
                            warnings.append(f"Variant {v.get('source_variant_id', 'unknown')} identifiers.source_sku missing when sku exists.")
                        if v.get("source_variant_id") and not idents.get("source_item_id"):
                            warnings.append(f"Variant {v.get('source_variant_id', 'unknown')} identifiers.source_item_id missing when source_variant_id exists.")
                            
                        gtin = idents.get("gtin")
                        if gtin:
                            variants_with_gtin += 1
                            if not isinstance(gtin, str):
                                warnings.append(f"Variant {v.get('source_variant_id', 'unknown')} identifiers.gtin is not a string.")
                            
                            digits_only = "".join(c for c in str(gtin) if c.isdigit())
                            if len(digits_only) not in [12, 13, 14]:
                                warnings.append(f"Variant {v.get('source_variant_id', 'unknown')} has invalid gtin length ({len(digits_only)} digits).")
                                
                            if len(str(gtin)) > 0 and str(gtin)[0] != digits_only[0]:
                                pass # Non-digit leading character is odd but maybe possible
                        else:
                            warnings.append(f"Variant {v.get('source_variant_id', 'unknown')} missing gtin.")
                            variants_missing_gtin += 1
                            
                        upc = idents.get("upc")
                        if upc:
                            variants_with_upc += 1
                            upc_digits = "".join(c for c in str(upc) if c.isdigit())
                            if len(upc_digits) != 12:
                                warnings.append(f"Variant {v.get('source_variant_id', 'unknown')} identifiers.upc must be 12 digits.")
                            if upc_digits in [str(v.get("sku", "")), str(v.get("source_variant_id", ""))]:
                                warnings.append(f"Internal sku was mapped as UPC for variant {v.get('source_variant_id', 'unknown')}.")
                                
                        ean = idents.get("ean")
                        if ean:
                            variants_with_ean += 1
                            ean_digits = "".join(c for c in str(ean) if c.isdigit())
                            if len(ean_digits) != 13:
                                warnings.append(f"Variant {v.get('source_variant_id', 'unknown')} identifiers.ean must be 13 digits.")
            
            t = p.get("title", "").lower()
            if re.search(r'(bag|can|pouch|tray|bottle|box|cartons?|count|pack|pouches?)$', t):
                warnings.append(f"Group {i} title may still contain size suffix.")
                
            flavor = p.get("flavor")
            p_facts = p.get("product_facts", {})
            m_plan = p.get("metafields_plan", {})
            
            if flavor and flavor != "Default":
                if p_facts.get("primary_flavor") != flavor:
                    warnings.append(f"Group {i} product_facts primary_flavor does not match grouped flavor.")
                if m_plan.get("custom.primary_flavor") != flavor:
                    warnings.append(f"Group {i} custom.primary_flavor does not match grouped flavor.")
                if m_plan.get("custom.source_flavor") != flavor:
                    warnings.append(f"Group {i} custom.source_flavor does not match grouped flavor.")
                    
            if facts and not p.get("content_sections", {}).get("specifications", {}).get("groups"):
                warnings.append(f"Group {i} specifications fallback missing despite facts existing.")
                
            if not m_plan or "custom.ingredients_json" not in m_plan:
                warnings.append(f"Group {i} metafields_plan missing complex fields.")
                
    score = 100
    score -= len(missing_required) * 25
    
    for f in missing_preferred:
        if not is_food and f in ["feeding_instructions", "guaranteed_analysis", "ingredients", "nutrition"]:
            score -= 1 
        else:
            score -= 5
            
    score -= len(warnings) * 2
    score = max(0, score)
    
    gtin_coverage = 0
    if total_variants > 0:
        gtin_coverage = (variants_with_gtin / total_variants) * 100
        
    return {
        "is_valid": len(missing_required) == 0,
        "confidence_score": score,
        "missing_required_fields": missing_required,
        "missing_preferred_fields": missing_preferred,
        "warnings": warnings,
        "identifier_coverage": {
            "total_variants": total_variants,
            "variants_with_gtin": variants_with_gtin,
            "variants_with_upc": variants_with_upc,
            "variants_with_ean": variants_with_ean,
            "variants_missing_gtin": variants_missing_gtin,
            "gtin_coverage_percent": gtin_coverage
        }
    }


async def extract_chewy_product(url: str):
    console.print(f"\n[bold cyan]Phase 3A Extraction for: {url}[/]")
    
    profile_data = adspower.start_profile(config.ADSPOWER_PROFILE_ID)
    ws_url = adspower.get_ws_endpoint(profile_data)
    
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(ws_url)
        context = browser.contexts[0]
        page = context.pages[0] if context.pages else await context.new_page()
        
        html = await fetch_initial_html(url, page)
        
        # Phase 4 - White Screen Detection
        detection_result = await adsp_profile_pool_manager.detect_white_screen_block(page, url)
        if detection_result["is_white_screen"]:
            if config.ADSP_SAVE_WHITE_SCREEN_SCREENSHOT:
                try:
                    import uuid
                    import os
                    os.makedirs("output/white_screen_events", exist_ok=True)
                    screenshot_path = f"output/white_screen_events/temp_{uuid.uuid4().hex}.png"
                    await page.screenshot(path=screenshot_path)
                    detection_result["screenshot_path"] = screenshot_path
                except Exception:
                    pass
            if config.ADSP_SAVE_WHITE_SCREEN_HTML:
                try:
                    import uuid
                    html_path = f"output/white_screen_events/temp_{uuid.uuid4().hex}.html"
                    with open(html_path, "w", encoding="utf-8") as f:
                        f.write(html)
                    detection_result["html_snapshot_path"] = html_path
                except Exception:
                    pass
            console.print("[red][WHITE_SCREEN_DETECTED][/red]")
            print(f"[WHITE_SCREEN_RESULT] {json.dumps(detection_result)}")
            return
            
        next_data = extract_next_data_from_html(html)
        
        build_id = detect_next_build_id(next_data, html)
        if not build_id:
            console.print("[red]Could not detect Next.js buildId[/red]")
            return
            
        console.print(f"[green]Build ID: {build_id}[/green]")
        
        if not next_data:
            next_url = build_next_data_url(url, build_id)
            if next_url:
                match = re.search(r"chewy\.com/(.*?)/dp/(\d+)", url)
                variant_id = match.group(2) if match else "unknown"
                next_data = await fetch_next_data_json(next_url, page, build_id, variant_id)
                
        if not next_data:
            console.print("[red]Failed to obtain next_data JSON[/red]")
            return
            
        match = re.search(r"chewy\.com/(.*?)/dp/(\d+)", url)
        base_product_id = match.group(2) if match else "unknown"
        
        arch = detect_chewy_architecture(next_data)
        console.print(f"Detected Architecture: {arch}")
        
        if arch == "apollo":
            normalized = parse_apollo_product(next_data, url)
        elif arch == "redux":
            normalized = parse_redux_product(next_data, url)
        else:
            normalized = {
                "source": "chewy",
                "source_url": url,
                "source_product_id": base_product_id,
                "title": "",
                "architecture": "unknown",
                "warnings": ["Unknown architecture. Could not parse product."]
            }
            
        normalized = normalize_chewy_product(normalized)
        grouped = split_product_by_flavor(normalized)
        val_report = validate_normalized_product(normalized, grouped)
        
        # Save files
        norm_out = OUT_DIR / f"chewy_normalized_{base_product_id}.json"
        with open(norm_out, "w", encoding="utf-8") as f:
            json.dump(normalized, f, indent=2)
            
        grouped_out = OUT_DIR / f"chewy_grouped_by_flavor_{base_product_id}.json"
        with open(grouped_out, "w", encoding="utf-8") as f:
            json.dump(grouped, f, indent=2)
            
        val_out = OUT_DIR / f"chewy_validation_{base_product_id}.json"
        with open(val_out, "w", encoding="utf-8") as f:
            json.dump(val_report, f, indent=2)
            

        # Generate content normalization report
        report_data = {
            'source_product_id': base_product_id,
            'products_count': len(grouped.get('products', [])),
            'products': []
        }
        for p in grouped.get('products', []):
            debug = p.get('debug', {})
            content = p.get('content_sections', {})
            report_data['products'].append({
                'flavor': p.get('flavor'),
                'title': p.get('title'),
                'original_title': debug.get('original_title'),
                'title_cleanup_applied': debug.get('title_cleanup_applied'),
                'title_cleanup_removed_suffix': debug.get('title_cleanup_removed_suffix'),
                'ingredients_parsed': bool(content.get('ingredients', {}).get('plain_text')),
                'ingredients_items_count': len(content.get('ingredients', {}).get('items', [])),
                'guaranteed_analysis_rows_count': len(content.get('guaranteed_analysis', {}).get('rows', [])),
                'nutrition_calorie_content_found': bool(content.get('nutrition', {}).get('calorie_content', {}).get('raw_text')),
                'feeding_instruction_tables_count': len(content.get('feeding_instructions', {}).get('tables', [])),
                'specifications_items_count': sum(len(g.get('items', [])) for g in content.get('specifications', {}).get('groups', [])),
                'product_facts': p.get('product_facts', {}),
                'storefront_sections_generated': [s['key'] for s in p.get('storefront_display', {}).get('accordion_sections', []) if s.get('enabled')],
                'metafields_plan_generated': bool(p.get('metafields_plan')),
                'warnings': debug.get('parser_warnings', [])
            })
            
        norm_rep_out = OUT_DIR / f'chewy_content_normalization_report_{base_product_id}.json'
        with open(norm_rep_out, 'w', encoding='utf-8') as f:
            json.dump(report_data, f, indent=2)
            
        # Report
        console.print("\n[bold]Validation Report[/bold]")
        console.print(f"URL: {url}")
        console.print(f"Detected Architecture: {arch}")
        console.print(f"Products Generated: {len(grouped.get('products', []))}")
        console.print(f"Confidence Score: {val_report['confidence_score']}/100")
        console.print(f"Is Valid: {val_report['is_valid']}")
        if val_report['warnings']:
            console.print(f"Warnings: {val_report['warnings']}")
            
        console.print(f"Outputs saved to {OUT_DIR}")

async def main():
    urls = [
        "https://www.chewy.com/hills-science-diet-adult-sensitive/dp/3861718", # Apollo
        "https://www.chewy.com/purina-pro-plan-high-protein-chicken/dp/52620"  # Redux (PLP redirect)
    ]
    if len(sys.argv) > 1:
        urls = [sys.argv[1]]
        
    for url in urls:
        try:
            await extract_chewy_product(url)
        except Exception as e:
            console.print(f"[red]Error processing {url}: {e}[/red]")
            
    adspower.stop_profile(config.ADSPOWER_PROFILE_ID)

if __name__ == "__main__":
    asyncio.run(main())
