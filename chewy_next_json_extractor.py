import asyncio
import base64
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


class WhiteScreenException(Exception):
    """Raised when Chewy blocks the current profile.

    Covers two signal classes:
      1. Page-load white-screen (empty body / block page) detected by
         adsp_profile_pool_manager.detect_white_screen_block.
      2. HTTP 429 / 403 / 503 on any Chewy /_next/data/ variant API call —
         strong signal that this profile is rate-limited or shadow-banned.

    Workers catch this and trigger auto_rebuild_profile (delete + recreate
    via .env proxy). The pid being processed is released back to 'pending'.
    """
    pass


# HTTP status codes that mean "Chewy refused to serve this profile" — they
# should be escalated the same way as a page-load white screen.
PROFILE_BLOCKED_STATUSES = frozenset({429, 403, 503})


# Chewy Apollo identity helpers
# -----------------------------
# - Apollo Item key encodes the entryID via base64: "Item:SXRlbToxMDE2MTA=" -> entryID 101610
# - entryID is what /dp/{X} URLs use; partNumber is the SKU surfaced in product data.

def decode_apollo_entry_id(apollo_key: str):
    if not apollo_key or ":" not in apollo_key:
        return None
    suffix = apollo_key.split(":", 1)[1]
    try:
        decoded = base64.b64decode(suffix).decode("utf-8", errors="replace")
    except Exception:
        return None
    if ":" in decoded:
        return decoded.split(":", 1)[1]
    return decoded


# Attribute classification for product splitting.
# Variant-axis attrs stay inside one Shopify product as variant options (Size selector).
# Everything else (Flavor, Breed Size, Life Stage, Color, ...) becomes a product
# discriminator: one Shopify product per unique value combination.
#
# Match exact attribute-name tokens, not substrings, otherwise "Breed Size" gets
# mis-classified as size-axis because it contains the word "size".
VARIANT_AXIS_KEYWORDS = frozenset({
    "size", "weight", "pack", "count", "case", "bundle", "quantity", "carton",
})


def is_variant_axis_attr(name: str) -> bool:
    """True only if every token in the attribute name is a size-axis keyword."""
    if not name:
        return False
    tokens = re.findall(r"[a-z0-9]+", str(name).lower())
    if not tokens:
        return False
    return all(t in VARIANT_AXIS_KEYWORDS for t in tokens)


_OOS_STATUS_STRINGS = frozenset({
    "OUT_OF_STOCK", "UNAVAILABLE", "DISCONTINUED",
    "TEMPORARILY_UNAVAILABLE", "TEMPORARILY_OUT_OF_STOCK",
    "PERMANENTLY_DISCONTINUED",
})


def derive_stock_fields(item_node: dict) -> dict:
    """Derive Shopify-friendly stock fields from a Chewy Apollo Item node.

    Reads multiple stock signals (inStock, isInStock, availabilityStatus,
    isUnavailable, isDiscontinued, isPermanentlyDiscontinued) and returns:
        in_stock: True/False/None
        out_of_stock: True/False (None → False)
        stock_reason: which signal made the decision
        shopify_inventory_policy: 'deny' if OOS else 'continue'
        availability: raw availabilityStatus / availability string
    """
    if not isinstance(item_node, dict):
        return {"in_stock": None, "out_of_stock": False,
                "stock_reason": "no_item_node",
                "shopify_inventory_policy": "continue", "availability": ""}

    avail_status = item_node.get("availabilityStatus") or item_node.get("availability") or ""
    avail_upper = str(avail_status).upper()
    in_stock_signals = [item_node.get("inStock"), item_node.get("isInStock")]
    unavailable_flags = [
        item_node.get("isUnavailable"),
        item_node.get("isDiscontinued"),
        item_node.get("isPermanentlyDiscontinued"),
    ]

    if any(f is True for f in unavailable_flags):
        in_stock = False
        stock_reason = "explicit_unavailable_flag"
    elif avail_upper in _OOS_STATUS_STRINGS:
        in_stock = False
        stock_reason = f"availability_status:{avail_upper}"
    elif any(s is True for s in in_stock_signals):
        in_stock = True
        stock_reason = "in_stock_signal_true"
    elif avail_upper in ("AVAILABLE", "IN_STOCK"):
        in_stock = True
        stock_reason = f"availability_status:{avail_upper}"
    elif any(s is False for s in in_stock_signals):
        in_stock = False
        stock_reason = "in_stock_signal_false"
    else:
        in_stock = None
        stock_reason = "unknown_no_signal"

    out_of_stock = (in_stock is False)
    return {
        "in_stock": in_stock,
        "out_of_stock": out_of_stock,
        "stock_reason": stock_reason,
        "shopify_inventory_policy": "deny" if out_of_stock else "continue",
        "availability": avail_status,
    }

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
        # Compact log: drop Playwright's verbose "Call log:" tail. The worker
        # decides whether this is a proxy death (→ swap to Local) or a real
        # failure (→ mark pid failed) based on the error tokens in `exc`.
        msg = str(exc).split("\nCall log:", 1)[0].split("\n", 1)[0].strip()
        if "Timeout" in str(exc):
            console.print(f"[red]Page load timeout ({config.PAGE_LOAD_TIMEOUT}ms): {msg}[/red]")
        else:
            console.print(f"[red]Page load failed: {msg}[/red]")
        raise
    await asyncio.sleep(4)
    return await read_page_content_with_retry(page)


def _collect_image_urls(img_nodes) -> list:
    out = []
    if not isinstance(img_nodes, list):
        return out
    for img in img_nodes:
        if isinstance(img, dict):
            img_url = img.get("url") or img.get("src") or img.get("originalUrl") or ""
            if not img_url:
                for ik, iv in img.items():
                    if "url" in str(ik).lower() and isinstance(iv, str) and iv.startswith("http"):
                        img_url = iv
                        break
            if img_url:
                out.append(img_url)
        elif isinstance(img, str) and img:
            out.append(img)
    return out


def _apply_usage(result: dict, usage: str, content_str: str, overwrite: bool = True):
    if not content_str or not isinstance(content_str, str):
        return
    usage = (usage or "").upper()
    field_map = {
        "INGREDIENTS": "ingredients",
        "FEEDING_INSTRUCTIONS": "feeding_instructions",
        "GUARANTEED_ANALYSIS": "guaranteed_analysis",
        "DESCRIPTION": "description",
        "TRANSITION_INSTRUCTIONS": "transition_instructions",
    }
    if "CALORI" in usage:
        if overwrite or not result.get("calorie_content"):
            result["calorie_content"] = content_str
        return
    field = field_map.get(usage)
    if not field:
        return
    if overwrite or not result.get(field):
        result[field] = content_str


def extract_variant_info_from_apollo(next_data: dict,
                                     target_variant_id: str = None,
                                     target_entry_id: str = None) -> dict:
    """Extract variant content from the Item node matching target_entry_id and/or target_variant_id (partNumber).

    Matching priority (when both provided): entry_id (decoded Apollo key) AND partNumber.
    When only one provided, use whichever matches.
    """
    result = {
        "ingredients": "",
        "guaranteed_analysis": "",
        "description": "",
        "feeding_instructions": "",
        "transition_instructions": "",
        "calorie_content": "",
        "images": []
    }
    apollo_state = next_data.get("pageProps", {}).get("__APOLLO_STATE__", {})

    for k, v in apollo_state.items():
        if not (k.startswith("Item:") and isinstance(v, dict)):
            continue
        if target_variant_id is not None or target_entry_id is not None:
            entry_id = decode_apollo_entry_id(k)
            part_number = str(v.get("partNumber", ""))
            matches_entry = target_entry_id is None or str(entry_id) == str(target_entry_id)
            matches_part = target_variant_id is None or part_number == str(target_variant_id)
            if not (matches_entry and matches_part):
                continue

        v_imgs = _collect_image_urls(v.get("images") or v.get("media"))
        if not v_imgs:
            full_img = v.get("fullImage")
            if isinstance(full_img, dict):
                # parameterized GraphQL keys like url({"autoCrop":true,"square":1800})
                for ik, iv in full_img.items():
                    if "url(" in str(ik) and isinstance(iv, str) and iv.startswith("http"):
                        v_imgs.append(iv)
        if not v_imgs:
            for pk, pv in apollo_state.items():
                if pk.startswith("Product:") and isinstance(pv, dict):
                    v_imgs = _collect_image_urls(pv.get("images") or pv.get("media"))
                    break
        if v_imgs:
            result["images"] = v_imgs

        desc = v.get("description")
        if isinstance(desc, str) and desc.strip():
            result["description"] = desc.strip()

        for group in (v.get("infoGroups") or []):
            if not isinstance(group, dict):
                continue
            for section in (group.get("sections") or []):
                if not isinstance(section, dict):
                    continue
                content_obj = section.get("content")
                content_str = ""
                if isinstance(content_obj, dict):
                    content_str = content_obj.get("content", "")
                elif isinstance(content_obj, str):
                    content_str = content_obj
                _apply_usage(result, section.get("usage"), content_str, overwrite=True)
            # Legacy fallback (flat usage/content on the group itself)
            content_raw = group.get("content")
            content_str = ""
            if isinstance(content_raw, dict):
                content_str = content_raw.get("content", "")
            elif isinstance(content_raw, str):
                content_str = content_raw
            _apply_usage(result, group.get("usage"), content_str, overwrite=False)

        break
    return result

async def enrich_variants_from_api(normalized_product: dict, page, build_id: str) -> dict:
    """Fetch per-variant API data (one call per variant entryID).

    Each Chewy variant has its own content (feeding_instructions can differ per
    breed-size variant), so we fetch every variant individually instead of one
    candidate per flavor. Cache keys on entryID+buildId so re-runs are free.
    """
    if not build_id or not page:
        return {"enriched": 0, "failed": 0, "reason": "no_build_id_or_page"}

    variants = normalized_product.get("variants", [])
    stats = {
        "enriched": 0,
        "failed": 0,
        "wrong_product_api_rejected": 0,
        "slug_mismatch": 0,
        "fields_filled": {
            "ingredients": 0, "guaranteed_analysis": 0,
            "feeding_instructions": 0, "transition_instructions": 0,
            "description": 0, "calorie_content": 0, "images": 0,
        },
    }

    for v in variants:
        part_number = v.get("source_variant_id")
        # OLD normalized files don't have source_entry_id. In that case we
        # ask the API by partNumber URL (Chewy redirects to the canonical
        # entryID); we then backfill source_entry_id from the matched Apollo
        # Item key for future runs.
        entry_id_known = bool(v.get("source_entry_id"))
        entry_id_in_url = v.get("source_entry_id") or part_number
        v_url = v.get("variant_url")
        if not entry_id_in_url or not v_url:
            stats["failed"] += 1
            continue

        next_url = build_next_data_url(v_url, build_id)
        if not next_url:
            stats["failed"] += 1
            continue

        try:
            var_data = await fetch_next_data_json(next_url, page, build_id, entry_id_in_url)
        except WhiteScreenException:
            # Profile blocked — bubble up so the worker can rebuild the profile.
            # Do not mark this variant as failed; the pid will be released and
            # retried on the new profile.
            raise
        except Exception as e:
            console.print(f"[red]Variant enrichment exception for {entry_id_in_url}: {e}[/red]")
            stats["failed"] += 1
            continue

        if var_data is None:
            stats["slug_mismatch"] += 1
            v.setdefault("warnings", []).append("api_enrichment_failed")
            v["content_source"] = {"type": "apollo_variant_api",
                                   "source_entry_id": v.get("source_entry_id"),
                                   "source_variant_id": part_number,
                                   "confidence": "missing",
                                   "reason": "api_404_or_null"}
            continue

        # Follow Next.js redirect once. OLD normalized files have variant_url
        # built from partNumber; Chewy 301-redirects those to the canonical
        # /dp/{entryID} URL. The first response has only __N_REDIRECT and no
        # Apollo state, so we extract the canonical entry_id from the redirect
        # target and refetch.
        pp = var_data.get("pageProps", {}) or {}
        if "__N_REDIRECT" in pp and "__APOLLO_STATE__" not in pp:
            redirect_to = pp.get("__N_REDIRECT", "")
            dp_match = re.search(r"/dp/(\d+)(?:[/?#]|$)", redirect_to)
            if dp_match:
                redirected_entry_id = dp_match.group(1)
                if redirected_entry_id != str(entry_id_in_url):
                    redirected_url = (
                        redirect_to if redirect_to.startswith("http")
                        else f"https://www.chewy.com{redirect_to}"
                    )
                    redirected_next_url = build_next_data_url(redirected_url, build_id)
                    if redirected_next_url:
                        try:
                            followed = await fetch_next_data_json(
                                redirected_next_url, page, build_id, redirected_entry_id
                            )
                        except Exception:
                            followed = None
                        if followed and "__APOLLO_STATE__" in (followed.get("pageProps") or {}):
                            var_data = followed
                            v_url = redirected_url
                            entry_id_in_url = redirected_entry_id
            else:
                # Redirect target isn't a product page (e.g. category). Variant is gone.
                stats["wrong_product_api_rejected"] += 1
                v.setdefault("warnings", []).append("variant_redirect_to_non_product")
                v["content_source"] = {"type": "apollo_variant_api",
                                       "source_entry_id": v.get("source_entry_id"),
                                       "source_variant_id": part_number,
                                       "confidence": "missing",
                                       "reason": f"redirect_to_non_product:{redirect_to[:120]}"}
                continue

        # Locate the canonical Item in the response.
        # Strict mode (entry_id known from new schema): both entry_id + partNumber must match.
        # Lenient mode (OLD normalized files): match by partNumber only and backfill entry_id.
        apollo_resp = var_data.get("pageProps", {}).get("__APOLLO_STATE__", {})
        matched_key = None
        matched_node = None
        for rk, rv in apollo_resp.items():
            if not (rk.startswith("Item:") and isinstance(rv, dict)):
                continue
            if str(rv.get("partNumber", "")) != str(part_number):
                continue
            eid_decoded = decode_apollo_entry_id(rk)
            if entry_id_known and str(eid_decoded) != str(entry_id_in_url):
                continue
            matched_key = rk
            matched_node = rv
            break

        if not matched_node:
            stats["wrong_product_api_rejected"] += 1
            v.setdefault("warnings", []).append("api_enrichment_failed")
            v["content_source"] = {"type": "apollo_variant_api",
                                   "source_entry_id": v.get("source_entry_id"),
                                   "source_variant_id": part_number,
                                   "confidence": "missing",
                                   "reason": "wrong_product_api_response"}
            continue

        # Backfill canonical entry_id + variant_url (upgrades OLD files).
        canonical_entry_id = decode_apollo_entry_id(matched_key) or entry_id_in_url
        v["source_entry_id"] = canonical_entry_id
        slug_match = re.search(r"chewy\.com/(.*?)/dp/", v_url)
        if slug_match:
            v["variant_url"] = f"https://www.chewy.com/{slug_match.group(1)}/dp/{canonical_entry_id}"

        # Backfill stock fields from the matched Item node.
        stock = derive_stock_fields(matched_node)
        v["in_stock"] = stock["in_stock"]
        v["out_of_stock"] = stock["out_of_stock"]
        v["stock_reason"] = stock["stock_reason"]
        v["shopify_inventory_policy"] = stock["shopify_inventory_policy"]
        v["availability"] = stock["availability"]

        v_info = extract_variant_info_from_apollo(
            var_data, target_variant_id=part_number, target_entry_id=canonical_entry_id
        )

        # Apply per-variant content (only fill empty fields so we don't overwrite
        # any human-curated content already on the variant).
        filled_any = False
        for field in ("ingredients", "guaranteed_analysis", "description",
                      "feeding_instructions", "transition_instructions",
                      "calorie_content"):
            if v_info.get(field) and not v.get(field):
                v[field] = v_info[field]
                stats["fields_filled"][field] += 1
                filled_any = True
        if v_info.get("images"):
            # moe/ URLs are real variant-specific images; treat all as valid.
            if not v.get("images"):
                v["images"] = v_info["images"]
                stats["fields_filled"]["images"] += 1
                filled_any = True

        if filled_any:
            stats["enriched"] += 1
            v["content_source"] = {"type": "apollo_variant_api",
                                   "source_entry_id": canonical_entry_id,
                                   "source_variant_id": part_number,
                                   "confidence": "high",
                                   "entry_id_backfilled": not entry_id_known}
        else:
            v["content_source"] = {"type": "apollo_variant_api",
                                   "source_entry_id": canonical_entry_id,
                                   "source_variant_id": part_number,
                                   "confidence": "low",
                                   "reason": "api_response_had_no_new_content"}

        await asyncio.sleep(1.5)

    return stats


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

    if response.status in PROFILE_BLOCKED_STATUSES:
        # 429 / 403 / 503 → Chewy is rate-limiting / blocking THIS profile.
        # Escalate to WhiteScreenException so the worker rebuilds the profile
        # via auto_rebuild_profile (delete + recreate with .env proxy). Without
        # this, the worker would keep hammering the throttled profile and burn
        # the rest of the queue with the same error.
        msg = f"HTTP {response.status} on /dp/{variant_id} - profile throttled"
        console.print(f"[bold red]{msg} — triggering profile rebuild[/bold red]")
        raise WhiteScreenException(msg)

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
    item_nodes = []  # list of (apollo_key, item_dict)

    for k, v in apollo_state.items():
        if not isinstance(v, dict): continue
        if k.startswith("Product:"):
            product_node = v
        elif k.startswith("Item:"):
            item_nodes.append((k, v))
        if v.get("__typename") == "Breadcrumb":
            breadcrumbs.append(v.get("name"))
        # Collect product-level images from Apollo media/image nodes
        if v.get("__typename") in ("MediaImage", "Image", "MediaAsset"):
            img_url = v.get("url") or v.get("src") or v.get("originalUrl")
            if img_url and img_url not in images:
                images.append(img_url)

    # Filter item_nodes to canonical variants only when Product.items({...}) is present.
    canonical_refs = set()
    if product_node:
        for pk, pv in product_node.items():
            if isinstance(pk, str) and pk.startswith("items(") and isinstance(pv, list):
                for it in pv:
                    if isinstance(it, dict) and it.get("__ref"):
                        canonical_refs.add(it["__ref"])
                break
        # Breadcrumbs live as an inline list inside the Product node; the
        # top-level scan above misses them. Pull them in order here.
        if not breadcrumbs:
            bc_list = product_node.get("breadcrumbs")
            if isinstance(bc_list, list):
                for bc in bc_list:
                    if isinstance(bc, dict):
                        nm = bc.get("name")
                        if nm:
                            breadcrumbs.append(nm)
    if canonical_refs:
        item_nodes = [(k, v) for k, v in item_nodes if k in canonical_refs]
            
    if product_node:
        title = product_node.get("name", "")
        desc = product_node.get("description", "")
        brand = product_node.get("manufacturerName", "")
        
        # Extract product-level images from product_node
        prod_images = product_node.get("images", []) or product_node.get("media", [])
        if isinstance(prod_images, list):
            for img in prod_images:
                if isinstance(img, dict):
                    url = img.get("url") or img.get("src") or img.get("originalUrl")
                    if url and url not in images:
                        images.append(url)
                elif isinstance(img, str) and img not in images:
                    images.append(img)
        
    base64_id = base64.b64encode(f"Item:{base_product_id}".encode()).decode()
    main_item_key = f"Item:{base64_id}"
    main_item = apollo_state.get(main_item_key)
    if not main_item and item_nodes:
        main_item_key, main_item = item_nodes[0]
        
    transition_inst = ""
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
                elif usage == "TRANSITION_INSTRUCTIONS":
                    transition_inst = content
                elif usage == "DESCRIPTION":
                    desc = content
                elif usage == "KEY_BENEFITS":
                    specs["Key Benefits"] = content

    # Structured product-level attribute table for Specifications.
    # Chewy stores these on Product as keys like attributes({"identifier":"PetType"})
    # plus a generic attributes({"includeEnsemble":true,"usage":["DEFINING"]}).
    product_attributes_table = {}
    if product_node:
        for k, v in product_node.items():
            if not (isinstance(k, str) and k.startswith("attributes(")):
                continue
            # Parse identifier from the key, e.g. attributes({"identifier":"PetType"}) -> PetType
            id_match = re.search(r'"identifier"\s*:\s*"([^"]+)"', k)
            label = id_match.group(1) if id_match else None
            values = []
            if isinstance(v, list):
                for entry in v:
                    if isinstance(entry, dict):
                        for av in (entry.get("values") or []):
                            if isinstance(av, dict) and "__ref" in av:
                                rn = apollo_state.get(av["__ref"], {})
                                val = rn.get("value")
                                if val: values.append(val)
            if label and values:
                product_attributes_table[label] = values

    variants_data = item_nodes
    normalized_variants = []
    for apollo_key, v in variants_data:
        v_id = v.get("partNumber") or v.get("id")
        if not v_id: continue
        entry_id = decode_apollo_entry_id(apollo_key) or str(v_id)

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

        stock_fields = derive_stock_fields(v)
        in_stock = stock_fields["in_stock"]
        out_of_stock = stock_fields["out_of_stock"]
        stock_reason = stock_fields["stock_reason"]
        avail_status = stock_fields["availability"]

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

        # Variant-specific content (from inline infoGroups on this Item)
        v_desc = ""
        v_ingredients = ""
        v_guaranteed = ""
        v_feeding = ""
        v_transition = ""
        v_calorie = ""
        for group in (v.get("infoGroups") or []):
            if not isinstance(group, dict): continue
            for sec in (group.get("sections") or []):
                if not isinstance(sec, dict): continue
                usage = (sec.get("usage") or "").upper()
                content_node = sec.get("content", {})
                content_str = content_node.get("content", "") if isinstance(content_node, dict) else ""
                if not content_str:
                    continue
                if usage == "DESCRIPTION":
                    v_desc = content_str
                elif usage == "INGREDIENTS":
                    v_ingredients = content_str
                elif usage == "GUARANTEED_ANALYSIS":
                    v_guaranteed = content_str
                elif usage == "FEEDING_INSTRUCTIONS":
                    v_feeding = content_str
                elif usage == "TRANSITION_INSTRUCTIONS":
                    v_transition = content_str
                elif "CALORI" in usage:
                    v_calorie = content_str

        normalized_variants.append({
            "source_entry_id": entry_id,
            "source_variant_id": v_id,
            "sku": v_id,
            "identifiers": idents,
            "title": v.get("name", ""),
            "option_values": option_values,
            "price": price,
            "compare_at_price": v.get("listPrice"),
            "description": v_desc,
            "ingredients": v_ingredients,
            "guaranteed_analysis": v_guaranteed,
            "feeding_instructions": v_feeding,
            "transition_instructions": v_transition,
            "calorie_content": v_calorie,
            "autoship_price": v.get("autoshipPrice"),
            "availability": avail_status,
            "in_stock": in_stock,
            "out_of_stock": out_of_stock,
            "stock_reason": stock_reason,
            "shopify_inventory_policy": "deny" if out_of_stock else "continue",
            "images": v_images,
            "variant_url": f"https://www.chewy.com/{slug}/dp/{entry_id}",
        })

    if not normalized_variants:
        price = None
        if product_node and isinstance(product_node.get("price"), dict):
            price = product_node["price"].get("salePrice") or product_node["price"].get("price")
        if not price and main_item and isinstance(main_item.get("price"), dict):
            price = main_item["price"].get("salePrice") or main_item["price"].get("price")

        fallback_entry = decode_apollo_entry_id(main_item_key) if main_item else None
        fallback_entry = fallback_entry or base_product_id
        normalized_variants.append({
            "source_entry_id": fallback_entry,
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
            "variant_url": f"https://www.chewy.com/{slug}/dp/{fallback_entry}",
            "feeding_instructions": "",
            "transition_instructions": "",
            "calorie_content": "",
        })
        
    # Fallback: if no product-level images found, collect from all variant images
    if not images:
        for nv in normalized_variants:
            for img in nv.get("images", []):
                if img and img not in images:
                    images.append(img)

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
        "transition_instructions": transition_inst,
        "product_attributes_table": product_attributes_table,
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
                
                v_desc = v.get("description", "")
                v_ingredients = v.get("ingredients", "")
                
                variants_list.append({
                    "source_variant_id": v_id,
                    "sku": v_id,
                    "identifiers": idents,
                    "title": v.get("name", title),
                    "option_values": option_values,
                    "price": price,
                    "compare_at_price": v.get("listPrice"),
                    "description": v_desc,
                    "ingredients": v_ingredients,
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

# Protein keywords for flavor matching
_PROTEIN_KEYWORDS = [
    "duck", "chicken", "beef", "lamb", "salmon", "turkey", "venison",
    "pork", "catfish", "trout", "whitefish", "goat", "kangaroo", "rabbit",
    "bison", "tuna", "herring", "mackerel", "sardine", "cod", "pollock",
    "quail", "pheasant", "elk", "boar", "guinea fowl", "anchovy",
]


_BREED_SIZE_KEYWORDS = ["giant", "large", "medium", "small", "mini", "toy"]


def _primary_keyword(text: str, vocab: list):
    """Return the keyword from vocab that appears earliest as a whole word in text."""
    if not text:
        return None
    earliest = len(text) + 1
    hit = None
    for kw in vocab:
        m = re.search(r'\b' + re.escape(kw) + r'\b', text)
        if m and m.start() < earliest:
            earliest = m.start()
            hit = kw
    return hit


def _content_matches_discriminator(parent_text_fields: dict,
                                   discriminator_key: str,
                                   discriminator_value: str) -> dict:
    """Generalised safety check: does parent text content match this discriminator value?

    For flavor → uses protein keyword check (Chicken vs Lamb etc.)
    For breed size → uses breed size keyword check (Large vs Medium etc.)
    Other → allow (returns safe=True)
    """
    if not discriminator_value or discriminator_value == "Default":
        return {"safe": True, "reason": "default_value"}

    key_lower = (discriminator_key or "").lower()
    val_lower = discriminator_value.lower()

    if "flavor" in key_lower:
        vocab = _PROTEIN_KEYWORDS
    elif "breed" in key_lower and "size" in key_lower:
        vocab = _BREED_SIZE_KEYWORDS
    else:
        # No protein/breed-size dimension to check — allow parent content
        return {"safe": True, "reason": "no_check_for_discriminator"}

    allowed = {kw for kw in vocab if kw in val_lower}
    if not allowed:
        return {"safe": True, "reason": "value_has_no_known_keyword"}

    text = " ".join([
        parent_text_fields.get("description", ""),
        parent_text_fields.get("ingredients", ""),
    ]).lower()
    if not text.strip():
        return {"safe": True, "reason": "no_parent_text_content"}

    primary_desc = _primary_keyword(parent_text_fields.get("description", "").lower(), vocab)
    primary_ingr = _primary_keyword(parent_text_fields.get("ingredients", "").lower(), vocab)

    if primary_desc and primary_desc in allowed:
        return {"safe": True, "reason": "primary_keyword_matches"}
    if not primary_desc and primary_ingr and primary_ingr in allowed:
        return {"safe": True, "reason": "primary_keyword_matches_ingr"}
    if not primary_desc and not primary_ingr:
        return {"safe": True, "reason": "no_known_keyword_in_text"}

    return {
        "safe": False,
        "reason": (f"primary_mismatch: dim={discriminator_key}, "
                   f"expects={sorted(allowed)}, "
                   f"desc_primary={primary_desc}, ingr_primary={primary_ingr}"),
    }


def _parent_content_matches_flavor(parent_text_fields: dict, flavor: str) -> dict:
    """Backward-compatible wrapper. Prefer _content_matches_discriminator going forward."""
    return _content_matches_discriminator(parent_text_fields, "flavor", flavor)


_SIZE_SUFFIX_PATTERN = (
    r"(?:,\s*|\s+-\s*|\s+)"
    r"(?:\d+(?:\.\d+)?(?:-| )?(?:lb|oz|kg|g)\s*(?:bag|can|pouch|tray|bottle|box|carton|tub|tubes?)s?"
    r"|\d+\s*(?:cans?|count|pack|pouches?|tubs?|tubes?)"
    r"|\b(?:case|pack|bundle)\s+of\s+\d+(?:\s*\([^)]*\))?)"
    r".*?$"
)


def _slugify(text: str) -> str:
    return re.sub(r'[^a-z0-9]+', '-', str(text or "").lower()).strip('-')


def _extract_size_value(variant: dict, fallback_title: str = "") -> str:
    """Pick the size string to display for a variant (e.g. '24.2-lb bag')."""
    title = variant.get("title") or fallback_title
    match = re.search(_SIZE_SUFFIX_PATTERN, title, flags=re.IGNORECASE)
    if match:
        return match.group(0).strip(" ,-")
    opts = variant.get("option_values", {}) or {}
    for k, val in opts.items():
        if is_variant_axis_attr(k) and val:
            return str(val)
    return "Default Title"


def split_product_by_flavor(normalized_product: dict) -> dict:
    """Split a normalized Chewy product into one Shopify product per non-Size defining-attribute
    combination. Size-like attrs (Size, Weight, Pack, Count, Case, Bundle, Quantity) stay as variant axis.

    Each Shopify product receives a stable handle/group_id and inherits parent content only when
    it's safe for that discriminator value (e.g. parent "Chicken Recipe" text not applied to Lamb).
    Variant-level content (per-variant API fetch) always takes precedence over parent.
    """
    variants = normalized_product.get("variants", [])

    parent_text_fields = {
        "description": normalized_product.get("description", "") or "",
        "ingredients": normalized_product.get("ingredients", "") or "",
        "guaranteed_analysis": normalized_product.get("guaranteed_analysis", "") or "",
        "feeding_instructions": normalized_product.get("feeding_instructions", "") or "",
        "transition_instructions": normalized_product.get("transition_instructions", "") or "",
    }

    # 1. Build discriminator tuple per variant: every non-axis defining attribute.
    #    Group key is the sorted ((attr_name, value), ...) tuple — stable & unique.
    groups = {}  # group_key -> {"discriminator": dict, "variants": list}
    for v in variants:
        opts = v.get("option_values", {}) or {}
        disc = {}
        for attr_name, attr_val in opts.items():
            if not attr_val:
                continue
            if is_variant_axis_attr(attr_name):
                continue
            disc[attr_name] = attr_val
        # Edge case: variant has no defining attrs at all → single-product fallback group
        group_key = tuple(sorted(disc.items())) if disc else ()
        if group_key not in groups:
            groups[group_key] = {"discriminator": disc, "variants": []}
        groups[group_key]["variants"].append(v)

    is_multi_product = len(groups) > 1
    products_out = []

    base_title = normalized_product.get("title", "") or ""
    # Strip size suffix off the parent title so it's reusable as a product title.
    base_clean_title = re.sub(_SIZE_SUFFIX_PATTERN, "", base_title,
                              flags=re.IGNORECASE).strip() or base_title

    for group_key, group in groups.items():
        disc = group["discriminator"]
        group_variants = group["variants"]

        # Build product title:
        #   "<base clean title> — <Attr1: Val1>, <Attr2: Val2>"
        if disc:
            disc_label_parts = []
            for attr_name, attr_val in sorted(disc.items()):
                # Title-case the attr name (already lowercase in option_values)
                pretty_name = " ".join(w.capitalize() for w in attr_name.split())
                disc_label_parts.append(f"{pretty_name}: {attr_val}")
            disc_label = " — " + ", ".join(disc_label_parts)
        else:
            disc_label = ""

        # Pick the most informative variant title for this group:
        #   1. Maximise number of discriminator values present in the title
        #   2. Then prefer the longest title (most complete)
        # This avoids Chewy's habit of dropping breed-size words on the default variant
        # (e.g. one variant is "Royal Canin … Dental Dry Dog Food" while siblings are
        # "Royal Canin … Adult Dental Medium & Large Breed Dry Dog Food").
        candidate_titles = [v.get("title", "") for v in group_variants if v.get("title")]

        def _title_score(t):
            tlow = t.lower()
            matches = sum(1 for _, val in disc.items()
                          if val and str(val).lower() in tlow)
            return (matches, len(t))

        best_title = max(candidate_titles, key=_title_score) if candidate_titles else base_title
        v_clean_title = re.sub(_SIZE_SUFFIX_PATTERN, "", best_title,
                               flags=re.IGNORECASE).strip()
        product_title = v_clean_title or (base_clean_title + disc_label)

        # Belt-and-braces: if a discriminator value is STILL missing from the chosen
        # title (e.g. all variants in the group have the truncated name), inject it
        # before the food-form anchor.
        title_aug_log = []
        for attr_name, attr_val in disc.items():
            if not attr_val or str(attr_val).lower() in product_title.lower():
                continue
            anchor = re.search(r'\b(Dry|Wet|Canned|Freeze-Dried|Soft|Raw|Semi-Moist)\b',
                               product_title, flags=re.IGNORECASE)
            is_breed_size = "breed" in attr_name.lower() and "size" in attr_name.lower()
            addition = (f"{attr_val} Breed" if is_breed_size else str(attr_val))
            if anchor:
                product_title = (
                    product_title[:anchor.start()].rstrip()
                    + " " + addition + " "
                    + product_title[anchor.start():]
                )
            else:
                product_title = product_title + " — " + addition
            title_aug_log.append({"attr": attr_name, "value": attr_val, "injected": addition})

        # Handle / slug
        handle_parts = [_slugify(normalized_product.get("slug") or product_title)]
        for _, val in sorted(disc.items()):
            handle_parts.append(_slugify(val))
        handle_slug = "-".join(p for p in handle_parts if p)

        # Group ID
        group_id = str(normalized_product.get("source_product_id", "unknown"))
        for attr_name, val in sorted(disc.items()):
            group_id += f":{_slugify(attr_name)}:{_slugify(val)}"

        # Choose primary flavor (for backward compat with `flavor` field downstream)
        primary_flavor = None
        for attr_name, val in disc.items():
            if "flavor" in attr_name.lower():
                primary_flavor = val
                break

        # 2. Build the variant list — each variant becomes a Shopify variant with option1=Size.
        new_variants = []
        for v in group_variants:
            new_v = {k: val for k, val in v.items() if k != "option_values"}
            size_val = _extract_size_value(v, fallback_title=base_title)
            new_v["option1_name"] = "Size"
            new_v["option1_value"] = size_val
            new_variants.append(new_v)

        # 3. Aggregate images (variant images take precedence; dedupe).
        seen, deduped_images = set(), []
        for v in group_variants:
            for img in (v.get("images") or []):
                if img and img not in seen:
                    seen.add(img)
                    deduped_images.append(img)
        if not deduped_images:
            deduped_images = list(normalized_product.get("images", []) or [])

        debug = {
            "architecture": normalized_product.get("architecture"),
            "original_variant_count": len(group_variants),
            "image_source": "variant_images" if any(v.get("images") for v in group_variants) else "base_product_fallback",
            "discriminator": disc,
            "group_key": list(disc.items()),
            "title_source": "longest_variant_with_disc_match",
            "title_augmentations": title_aug_log,
            "parser_warnings": list(normalized_product.get("warnings", []) or []),
        }

        # 4. Safe parent-content assignment.
        #    Variant-level content (from API enrichment) always wins per-variant.
        #    For PRODUCT-LEVEL fields, only apply parent content if it matches this discriminator.
        safe_desc = ""
        safe_ingr = ""
        safe_ga = ""
        safe_fi = ""
        safe_trans = ""

        parent_safe = True
        unsafe_reasons = []
        if is_multi_product and disc:
            for attr_name, attr_val in disc.items():
                check = _content_matches_discriminator(parent_text_fields, attr_name, attr_val)
                if not check["safe"]:
                    parent_safe = False
                    unsafe_reasons.append(check["reason"])

        if parent_safe:
            safe_desc = parent_text_fields["description"]
            safe_ingr = parent_text_fields["ingredients"]
            safe_ga = parent_text_fields["guaranteed_analysis"]
            safe_fi = parent_text_fields["feeding_instructions"]
            safe_trans = parent_text_fields["transition_instructions"]
        else:
            rejected = {k: v for k, v in parent_text_fields.items() if v}
            if rejected:
                debug["rejected_content"] = rejected
                debug["rejected_reasons"] = unsafe_reasons
            debug["parser_warnings"].append("parent_content_not_applicable_to_discriminator")

        # Variant-level content overrides — pick first non-empty across variants of this group
        for field, slot in [("description", "safe_desc"),
                            ("ingredients", "safe_ingr"),
                            ("guaranteed_analysis", "safe_ga"),
                            ("feeding_instructions", "safe_fi"),
                            ("transition_instructions", "safe_trans")]:
            current = locals()[slot]
            if current:
                continue
            for v in group_variants:
                if v.get(field):
                    if slot == "safe_desc": safe_desc = v[field]
                    elif slot == "safe_ingr": safe_ingr = v[field]
                    elif slot == "safe_ga": safe_ga = v[field]
                    elif slot == "safe_fi": safe_fi = v[field]
                    elif slot == "safe_trans": safe_trans = v[field]
                    break

        # 5. Product facts / metafields / content_sections (preserved best-effort).
        p_facts = (normalized_product.get("product_facts") or {}).copy()
        if primary_flavor:
            p_facts["primary_flavor"] = primary_flavor
        for attr_name, val in disc.items():
            if "breed" in attr_name.lower() and "size" in attr_name.lower():
                p_facts["breed_size"] = val

        content_sections = (normalized_product.get("content_sections") or {}).copy()
        specs = (content_sections.get("specifications") or {}).copy()
        if not specs.get("groups"):
            fb_items = []
            attr_table = normalized_product.get("product_attributes_table") or {}
            for label, vals in attr_table.items():
                if vals:
                    fb_items.append({"label": label, "value": ", ".join(vals),
                                     "normalized_key": _slugify(label)})
            if normalized_product.get("brand"):
                fb_items.append({"label": "Brand", "value": normalized_product["brand"],
                                 "normalized_key": "brand"})
            for attr_name, val in disc.items():
                pretty = " ".join(w.capitalize() for w in attr_name.split())
                fb_items.append({"label": pretty, "value": val,
                                 "normalized_key": _slugify(attr_name)})
            if fb_items:
                specs["groups"] = [{"title": "Specifications", "items": fb_items}]
                specs["source_raw"] = "Generated from Apollo attribute table + defining attributes."
        content_sections["specifications"] = specs

        m_plan = (normalized_product.get("metafields_plan") or {}).copy()
        m_plan["custom.primary_flavor"] = primary_flavor
        if not safe_ingr: m_plan["custom.ingredients_json"] = None
        if not safe_ga: m_plan["custom.guaranteed_analysis_json"] = None
        for attr_name, val in disc.items():
            m_plan[f"custom.{_slugify(attr_name)}"] = val

        storefront_display = (normalized_product.get("storefront_display") or {}).copy()
        highlights = []
        if primary_flavor: highlights.append(primary_flavor)
        if p_facts.get("breed_size"): highlights.append(f"Breed Size: {p_facts['breed_size']}")
        if p_facts.get("life_stage"): highlights.append(p_facts["life_stage"])
        if p_facts.get("pet_type"): highlights.append(p_facts["pet_type"])
        storefront_display["highlights"] = highlights

        for idx, sec in enumerate(storefront_display.get("accordion_sections", []) or []):
            if sec.get("key") == "specifications" and specs.get("groups"):
                storefront_display["accordion_sections"][idx]["enabled"] = True
            if sec.get("key") in ("ingredients", "guaranteed_analysis", "feeding_instructions"):
                field_map = {"ingredients": safe_ingr,
                             "guaranteed_analysis": safe_ga,
                             "feeding_instructions": safe_fi}
                if not field_map.get(sec["key"]):
                    storefront_display["accordion_sections"][idx]["enabled"] = False

        # Product-level stock summary: out_of_stock if EVERY variant is OOS.
        # If at least one variant is in_stock the product is sellable on Shopify.
        v_stocks = [v.get("out_of_stock") for v in new_variants]
        all_oos = bool(new_variants) and all(s is True for s in v_stocks)
        any_in_stock = any(s is False for s in v_stocks)
        product_out_of_stock = all_oos
        product_stock_state = (
            "all_variants_out_of_stock" if all_oos
            else "in_stock" if any_in_stock
            else "stock_unknown"
        )

        products_out.append({
            "source_group_id": group_id,
            "title": product_title,
            "flavor": primary_flavor,
            "discriminator": disc,
            "brand": normalized_product.get("brand", ""),
            "handle_slug": handle_slug,
            "category_path": normalized_product.get("category_path", []),
            "description": safe_desc,
            "ingredients": safe_ingr,
            "guaranteed_analysis": safe_ga,
            "feeding_instructions": safe_fi,
            "transition_instructions": safe_trans,
            "specifications": normalized_product.get("specifications", {}),
            "product_facts": p_facts,
            "content_sections": content_sections,
            "storefront_display": storefront_display,
            "metafields_plan": m_plan,
            "images": deduped_images,
            "variants": new_variants,
            "out_of_stock": product_out_of_stock,
            "stock_state": product_stock_state,
            "debug": debug,
        })

    return {
        "source": normalized_product.get("source"),
        "source_product_id": normalized_product.get("source_product_id"),
        "source_url": normalized_product.get("source_url"),
        "architecture": normalized_product.get("architecture"),
        "grouping_strategy": "discriminator_attrs_as_product_size_as_variant",
        "is_multi_product": is_multi_product,
        "products": products_out,
    }


def dedupe_products_across_pages(all_grouped: list) -> dict:
    """Cross-source-page deduplication of Shopify products.

    Chewy "ensemble" products are often reachable via multiple URLs — e.g. the
    Wysong Archetype family has 3 separate landing pages (Chicken / Quail / Rabbit)
    that each expose the SAME 3 variants. Without dedupe we'd import 9 products
    into Shopify when there are really only 3.

    Fingerprint = sorted tuple of variant `source_entry_id`s. Products sharing the
    same fingerprint are merged; we keep the version coming from the source page
    whose URL product_id matches one of the variant entry_ids (the "canonical"
    landing page for that flavor / breed / etc.).

    Args:
        all_grouped: list of grouped product dicts (each is one source page output
                     from split_product_by_flavor).

    Returns:
        {
            "kept_products": [shopify_product, ...],   # deduped Shopify products
            "duplicates_log": [{...}, ...],            # per-fingerprint dedupe trace
            "total_candidates": int,
            "unique_products": int,
        }
    """
    candidates = []
    for entry in all_grouped:
        source_pid = str(entry.get("source_product_id") or "")
        source_url = entry.get("source_url") or ""
        for p in (entry.get("products") or []):
            entry_ids = [v.get("source_entry_id") for v in (p.get("variants") or [])
                         if v.get("source_entry_id")]
            fingerprint = tuple(sorted(str(eid) for eid in entry_ids))
            candidates.append({
                "fingerprint": fingerprint,
                "source_pid": source_pid,
                "source_url": source_url,
                "product": p,
            })

    by_fp = {}
    for c in candidates:
        by_fp.setdefault(c["fingerprint"], []).append(c)

    kept_products = []
    duplicates_log = []
    for fp, group in by_fp.items():
        if len(group) == 1:
            winner = group[0]
            wp = dict(winner["product"])
            wp["canonical_source"] = {
                "source_product_id": winner["source_pid"],
                "source_url": winner["source_url"],
                "fingerprint": list(fp),
                "duplicate_count": 0,
            }
            kept_products.append(wp)
            continue

        def _score(cand):
            variant_ids = set(cand["fingerprint"])
            url_pid_matches_variant = 1 if cand["source_pid"] in variant_ids else 0
            return (url_pid_matches_variant, -len(cand["source_pid"]))

        ordered = sorted(group, key=_score, reverse=True)
        winner = ordered[0]
        losers = ordered[1:]
        wp = dict(winner["product"])
        wp["canonical_source"] = {
            "source_product_id": winner["source_pid"],
            "source_url": winner["source_url"],
            "fingerprint": list(fp),
            "duplicate_count": len(losers),
            "dropped_source_pids": [l["source_pid"] for l in losers],
        }
        kept_products.append(wp)
        duplicates_log.append({
            "fingerprint": list(fp),
            "product_title": winner["product"].get("title"),
            "kept_from_source_pid": winner["source_pid"],
            "dropped_from_source_pids": [l["source_pid"] for l in losers],
            "match_reason": ("source_pid_in_variant_set"
                             if winner["source_pid"] in set(fp)
                             else "first_by_score"),
        })

    return {
        "kept_products": kept_products,
        "duplicates_log": duplicates_log,
        "total_candidates": len(candidates),
        "unique_products": len(kept_products),
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


# ── Shopify import sanitization ────────────────────────────────────────
# Previously in chewy_enrich.py. Moved here so the unified scraper can run
# them inline; chewy_enrich.py still re-exports for backward compatibility.

FLAVOR_KEYWORDS = [
    "duck", "chicken", "beef", "lamb", "salmon", "turkey", "venison",
    "pork", "catfish", "trout", "whitefish", "goat", "kangaroo", "rabbit",
    "bison", "tuna", "herring", "mackerel", "sardine", "cod", "pollock",
    "quail", "pheasant", "elk", "boar", "guinea fowl", "anchovy",
]


def has_real_images(img_list: list) -> bool:
    # Chewy CDN `moe/` URLs are real variant-specific images, not placeholders.
    if not img_list:
        return False
    return any(isinstance(i, str) and i.strip() for i in img_list)


def detect_flavor_mismatch(product: dict) -> dict:
    """Primary-protein-aware mismatch detection."""
    flavor = (product.get("flavor") or "").strip()
    if not flavor or flavor == "Default":
        return {"mismatch": False, "declared_flavor": flavor,
                "detected_flavors_in_text": [], "fields_with_mismatch": []}

    declared_lower = flavor.lower()
    allowed = {kw for kw in FLAVOR_KEYWORDS if kw in declared_lower}
    if not allowed:
        return {"mismatch": False, "declared_flavor": flavor,
                "detected_flavors_in_text": [], "fields_with_mismatch": []}

    desc = (product.get("description") or "").lower()
    ingr = (product.get("ingredients") or "").lower()
    if not desc.strip() and not ingr.strip():
        return {"mismatch": False, "declared_flavor": flavor,
                "detected_flavors_in_text": [], "fields_with_mismatch": []}

    def _find_primary(text):
        best, earliest = None, len(text) + 1
        for kw in FLAVOR_KEYWORDS:
            m = re.search(r'\b' + re.escape(kw) + r'\b', text)
            if m and m.start() < earliest:
                earliest, best = m.start(), kw
        return best

    primary_desc = _find_primary(desc)
    primary_ingr = _find_primary(ingr)

    if primary_desc in allowed:
        return {"mismatch": False, "declared_flavor": flavor,
                "detected_flavors_in_text": [], "fields_with_mismatch": []}
    if not primary_desc and primary_ingr in allowed:
        return {"mismatch": False, "declared_flavor": flavor,
                "detected_flavors_in_text": [], "fields_with_mismatch": []}
    if not primary_desc and not primary_ingr:
        return {"mismatch": False, "declared_flavor": flavor,
                "detected_flavors_in_text": [], "fields_with_mismatch": []}

    detected, fields_hit = set(), []
    for fn in ["description", "ingredients", "guaranteed_analysis",
               "feeding_instructions"]:
        text = (product.get(fn) or "").lower()
        if not text:
            continue
        for kw in FLAVOR_KEYWORDS:
            if kw in allowed:
                continue
            if re.search(r'\b' + re.escape(kw) + r'\b', text):
                detected.add(kw)
                if fn not in fields_hit:
                    fields_hit.append(fn)

    return {"mismatch": len(detected) > 0, "declared_flavor": flavor,
            "detected_flavors_in_text": sorted(detected),
            "fields_with_mismatch": fields_hit,
            "primary_in_desc": primary_desc, "primary_in_ingr": primary_ingr}


def sanitize_product(product: dict, counters: dict | None = None) -> None:
    """Final sanitizer: flavor mismatch check + import status assignment.

    Mutates product in place. `counters` (optional) collects cross-product
    totals; pass None when sanitizing a single product.
    """
    if counters is None:
        counters = {}
    counters.setdefault("flavor_mismatch_count", 0)
    counters.setdefault("public_content_unsafe_count", 0)

    fm = detect_flavor_mismatch(product)
    if fm["mismatch"]:
        counters["flavor_mismatch_count"] += 1
        counters["public_content_unsafe_count"] += 1
        rejected = {}
        for field in fm["fields_with_mismatch"]:
            if product.get(field):
                rejected[field] = product[field]
                product[field] = ""
        product.setdefault("debug", {})["rejected_content"] = rejected
        product["debug"].setdefault("parser_warnings", []).append(
            "public_content_flavor_mismatch")
        product.setdefault("warnings", []).append(
            "public_content_flavor_mismatch")
        product["public_content_safe"] = False
        product["import_ready"] = False
        product["import_mode"] = "blocked"
        product["flavor_mismatch_detail"] = fm
    else:
        product["public_content_safe"] = True

    p_vars = product.get("variants", [])
    has_price = [v for v in p_vars if v.get("price")]
    no_price = [v for v in p_vars if not v.get("price")]
    for v in no_price:
        if "missing_price_unresolved" not in v.get("warnings", []):
            v.setdefault("warnings", []).append("missing_price_unresolved")
        v["variant_export_ready"] = False
    for v in has_price:
        v.setdefault("variant_export_ready", True)

    has_p_imgs = has_real_images(product.get("images", []))
    has_v_imgs = any(has_real_images(v.get("images", [])) for v in p_vars)
    has_any_img = has_p_imgs or has_v_imgs
    if not has_any_img:
        product.setdefault("warnings", []).append("missing_image_unresolved")

    if product.get("public_content_safe") is False:
        product["import_ready"] = False
        product["import_mode"] = "blocked"
    elif not has_price:
        product["import_ready"] = False
        product["import_mode"] = "needs_manual_review"
    elif not has_any_img:
        product["import_ready"] = False
        product["import_mode"] = "needs_manual_review"
    elif no_price:
        product["import_ready"] = True
        product["import_mode"] = "safe_with_warnings"
    else:
        product["import_ready"] = True
        product["import_mode"] = "safe_to_import"


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
