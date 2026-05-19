"""Build Shopify product CSV + inventory CSV from output/grouped_products/*.json.

Pipeline:
  1. Load all grouped files
  2. Run dedupe_products_across_pages -> drops landing-page duplicates
  3. For each kept product (one per flavor/discriminator), emit Shopify CSV
     rows (one per variant)

First variant row of each product carries product-level columns (Title, Body,
Vendor, ...). Subsequent variant rows have those cells blank, per Shopify
CSV convention.

Outputs (default output/shopify_export/):
  shopify_products.csv   - full product import (first-time)
  shopify_inventory.csv  - re-sync only (Handle, SKU, Qty, Policy)
  shopify_dedupe_log.json - which pids got collapsed into which

Skips products with import_mode == 'blocked'. Adds an 'import:...' tag for
products with 'safe_with_warnings' so user can filter them in Shopify admin.

Usage:
  python tools/build_shopify_csv.py [--input output/grouped_products]
                                    [--output-dir output/shopify_export]
                                    [--status draft|active]
                                    [--in-stock-qty 999]
                                    [--include-blocked]
"""
from __future__ import annotations

import argparse
import csv
import glob
import html
import json
import os
import re
import sys
from collections import Counter
from typing import Any

# Allow `python tools/build_shopify_csv.py` from repo root
_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)

from chewy_next_json_extractor import dedupe_products_across_pages  # noqa: E402
from tools.category_resolver import resolve_category  # noqa: E402


# -- metafields_plan key -> Shopify CSV metafield type ------------------------
JSON_KEYS = {
    "custom.ingredients_json",
    "custom.guaranteed_analysis_json",
    "custom.nutrition_json",
    "custom.feeding_instructions_json",
    "custom.specifications_json",
}
URL_KEYS = {"custom.source_url"}
# Special-cased list keys (value is a JSON-array string in metafields_plan,
# we forward as JSON list metafield).
LIST_TEXT_KEYS = {"custom.special_diet", "custom.category_path"}

# Category metafield keys we add ourselves (not in metafields_plan)
CATEGORY_KEY_PATH = "custom.category_path"
CATEGORY_KEY_LEAF = "custom.category_leaf"
CATEGORY_KEY_SOURCE = "custom.category_source"
EXTRA_MF_KEYS = [CATEGORY_KEY_PATH, CATEGORY_KEY_LEAF, CATEGORY_KEY_SOURCE]

# Other text-ish keys are emitted as single_line_text_field.


def shopify_metafield_type(key: str) -> str:
    if key in JSON_KEYS:
        return "json"
    if key in URL_KEYS:
        return "url"
    if key in LIST_TEXT_KEYS:
        return "list.single_line_text_field"
    return "single_line_text_field"


def metafield_column_name(key: str) -> str:
    """Shopify CSV column convention for product-level metafields."""
    return f"Metafield: {key} [{shopify_metafield_type(key)}]"


# -- Markdown -> HTML for Body ------------------------------------------------
def _inline(s: str) -> str:
    esc = html.escape(s)
    esc = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", esc)
    esc = re.sub(r"\*([^*]+)\*", r"<em>\1</em>", esc)
    return esc


def md_to_html(text: str | None) -> str:
    if not text:
        return ""
    s = str(text).strip()
    if not s:
        return ""
    lines = s.split("\n")
    out: list[str] = []
    tbl: list[str] = []
    in_list = False

    def flush_tbl() -> None:
        nonlocal tbl
        if not tbl:
            return
        rows = [[c.strip() for c in r.strip().strip("|").split("|")] for r in tbl]
        if len(rows) >= 2 and all(re.fullmatch(r":?-+:?", c.strip() or "") for c in rows[1]):
            header, body = rows[0], rows[2:]
        else:
            header, body = rows[0], rows[1:]
        out.append("<table><thead><tr>")
        out.extend(f"<th>{html.escape(h)}</th>" for h in header)
        out.append("</tr></thead><tbody>")
        for r in body:
            out.append("<tr>")
            out.extend(f"<td>{html.escape(c)}</td>" for c in r)
            out.append("</tr>")
        out.append("</tbody></table>")
        tbl = []

    def flush_list() -> None:
        nonlocal in_list
        if in_list:
            out.append("</ol>")
            in_list = False

    for raw in lines:
        line = raw.rstrip()
        if line.startswith("|") and "|" in line[1:]:
            flush_list()
            tbl.append(line)
            continue
        flush_tbl()
        if not line.strip():
            flush_list()
            continue
        m = re.match(r"^\s*(\d+)\.\s+(.*)$", line)
        if m:
            if not in_list:
                out.append("<ol>")
                in_list = True
            out.append(f"<li>{_inline(m.group(2))}</li>")
            continue
        flush_list()
        out.append(f"<p>{_inline(line)}</p>")
    flush_tbl()
    flush_list()
    return "".join(out)


# -- Helpers ------------------------------------------------------------------
def clean_price(p: Any) -> str:
    if p in (None, ""):
        return ""
    s = str(p).replace("$", "").replace(",", "").strip()
    try:
        return f"{float(s):.2f}"
    except Exception:
        return ""


def slugify(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def safe_str(v: Any) -> str:
    return "" if v is None else str(v)


def build_tags(
    p: dict,
    mp: dict,
    pat: dict,
    source_pid: str | None = None,
    category_path: list[str] | None = None,
    is_bundle: bool = False,
) -> str:
    tags: list[str] = []
    pf = p.get("product_facts") or {}
    pt = mp.get("custom.pet_type") or pf.get("pet_type")
    if pt:
        tags.append(f"pet:{pt}")
    brand = p.get("brand")
    if brand:
        tags.append(f"brand:{brand}")
    flv = p.get("flavor") or mp.get("custom.primary_flavor") or mp.get("custom.flavor")
    if flv:
        tags.append(f"flavor:{flv}")
    disc = p.get("discriminator") or {}
    for k, v in disc.items():
        tags.append(f"{slugify(k)}:{v}")
    ls = mp.get("custom.life_stage") or mp.get("custom.lifestage")
    if ls:
        tags.append(f"lifestage:{ls}")
    bs = mp.get("custom.breed_size") or mp.get("custom.breed-size")
    if bs:
        tags.append(f"breed-size:{bs}")
    ff = mp.get("custom.food_form")
    if ff:
        tags.append(f"food-form:{ff}")
    pkg = mp.get("custom.package_type")
    if pkg:
        tags.append(f"package:{pkg}")
    sd = pf.get("special_diet") or []
    for x in sd:
        tags.append(f"diet:{x}")
    hf = pf.get("health_feature") or []
    for x in hf:
        tags.append(f"health:{x}")
    # Category tags: one tag per segment of the resolved path
    if category_path:
        for seg in category_path:
            if seg:
                tags.append(f"category:{seg}")
        # Also a single 'category-leaf:<leaf>' so we can target the leaf-most level
        if category_path:
            tags.append(f"category-leaf:{category_path[-1]}")
    else:
        cat = (pat.get("ProductCategory") or [None])[0]
        if cat:
            tags.append(f"category:{cat}")
    if is_bundle:
        tags.append("bundle")
    if source_pid:
        tags.append(f"chewy-pid:{source_pid}")
    im = p.get("import_mode")
    if im and im != "safe_to_import":
        tags.append(f"import:{im}")
    # dedupe, preserve order
    seen = set()
    out = []
    for t in tags:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return ", ".join(out)


def product_type(pat: dict, category_path: list[str] | None = None) -> str:
    """Shopify Type column: use the leaf of the resolved category path if available.

    Fall back to ProductCategory from product_attributes_table.
    """
    if category_path:
        # Skip the species top segment ("Dog Supplies", "Cat Supplies",
        # "Horse Supplies", ...) — Shopify Type column wants the leaf.
        meaningful = [s for s in category_path[1:] if s]
        if meaningful:
            return meaningful[-1]
    vals = pat.get("ProductCategory") or []
    if vals:
        return str(vals[0])
    return ""


def metafield_cell(key: str, value: Any) -> str:
    if value in (None, "", [], {}):
        return ""
    if key in JSON_KEYS:
        # Already a dict — emit JSON string
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False)
    if key in LIST_TEXT_KEYS:
        if isinstance(value, str):
            # Could already be a JSON-array string, or a plain string treated as 1-element list
            try:
                parsed = json.loads(value)
                if isinstance(parsed, list):
                    if not parsed:
                        return ""
                    return json.dumps(parsed, ensure_ascii=False)
            except Exception:
                pass
            return json.dumps([value], ensure_ascii=False)
        if isinstance(value, list):
            if not value:
                return ""
            return json.dumps(value, ensure_ascii=False)
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


# -- Header construction ------------------------------------------------------
# Discover ALL metafield keys actually present in dataset so the CSV is stable.
def discover_metafield_keys(products: list[dict]) -> list[str]:
    keys: set[str] = set()
    for p in products:
        for k, v in (p.get("metafields_plan") or {}).items():
            if v in (None, "", [], {}):
                continue
            # treat empty JSON list-string "[]" as missing
            if k in LIST_TEXT_KEYS and isinstance(v, str):
                try:
                    if not json.loads(v):
                        continue
                except Exception:
                    continue
            keys.add(k)
    return sorted(keys)


def _dedupe_tokens(slug: str) -> str:
    """Remove tokens that appear more than once in a slug, preserving first occurrence."""
    seen: set[str] = set()
    out: list[str] = []
    for tok in slug.split("-"):
        if not tok:
            continue
        if tok in seen:
            continue
        seen.add(tok)
        out.append(tok)
    return "-".join(out)


def disambiguate_handle(p: dict, source_pid: str | None) -> str:
    """Build a stable, unique-per-product handle.

    Start with handle_slug. If the scraper's slug omits the discriminator value
    (common for Royal Canin breed-health), append it.  Always append a short
    fingerprint of the source_group_id so distinct products that happen to
    share a base slug stay distinct.
    """
    base = (p.get("handle_slug") or "").strip()
    if not base:
        base = slugify(p.get("title") or "product")

    # Collect candidate discriminator values:  flavor field + discriminator dict
    disc_vals: list[str] = []
    if p.get("flavor"):
        disc_vals.append(str(p["flavor"]))
    for v in (p.get("discriminator") or {}).values():
        if v is not None:
            disc_vals.append(str(v))

    extras: list[str] = []
    seen_tokens = set(base.split("-"))
    for raw in disc_vals:
        slug = slugify(raw)
        if not slug:
            continue
        slug_tokens = set(slug.split("-"))
        # Skip if all tokens are already in the base slug
        if slug_tokens.issubset(seen_tokens):
            continue
        extras.append(slug)
        seen_tokens.update(slug_tokens)
    if extras:
        base = f"{base}-{'-'.join(extras)}"

    return _dedupe_tokens(base)


def short_fp(s: str, n: int = 6) -> str:
    """Short deterministic fingerprint."""
    import hashlib

    return hashlib.md5(s.encode("utf-8")).hexdigest()[:n]


PRODUCT_HEADER = [
    "Handle",
    "Title",
    "Body (HTML)",
    "Vendor",
    "Product Category",
    "Type",
    "Tags",
    "Published",
    "Status",
    "Option1 Name",
    "Option1 Value",
    "Variant SKU",
    "Variant Grams",
    "Variant Inventory Tracker",
    "Variant Inventory Qty",
    "Variant Inventory Policy",
    "Variant Fulfillment Service",
    "Variant Price",
    "Variant Compare At Price",
    "Variant Requires Shipping",
    "Variant Taxable",
    "Variant Barcode",
    "Image Src",
    "Image Position",
    "Image Alt Text",
    "Gift Card",
    "SEO Title",
    "SEO Description",
    "Variant Weight Unit",
    "Variant Tax Code",
    "Cost per item",
]


def build_row_template(headers: list[str]) -> dict:
    return {h: "" for h in headers}


def load_pat_index(input_dir_norm: str) -> dict[str, dict]:
    """Map source_pid -> product_attributes_table from normalized files."""
    idx: dict[str, dict] = {}
    for f in glob.glob(os.path.join(input_dir_norm, "chewy_*.json")):
        try:
            d = json.load(open(f, "r", encoding="utf-8"))
        except Exception:
            continue
        pid = d.get("source_product_id") or ""
        if pid:
            idx[str(pid)] = d.get("product_attributes_table") or {}
    return idx


# -- Exporters ----------------------------------------------------------------
def export_csv(
    grouped_files: list[str],
    out_products: str,
    out_inventory: str,
    out_dedupe_log: str,
    status: str,
    in_stock_qty: int,
    include_blocked: bool,
    normalized_dir: str = "output/normalized_products",
) -> dict:
    stats = Counter()
    pat_index = load_pat_index(normalized_dir)

    # 1. Load all grouped wrappers
    all_grouped: list[dict] = []
    for f in grouped_files:
        try:
            all_grouped.append(json.load(open(f, "r", encoding="utf-8")))
        except Exception:
            stats["file_load_error"] += 1

    # 2. Dedupe across landing pages
    dedupe_res = dedupe_products_across_pages(all_grouped)
    kept: list[dict] = dedupe_res["kept_products"]
    stats["dedupe_total_candidates"] = dedupe_res["total_candidates"]
    stats["dedupe_unique_products"] = dedupe_res["unique_products"]
    stats["dedupe_collapsed_groups"] = len(dedupe_res["duplicates_log"])

    # Save dedupe log
    with open(out_dedupe_log, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "total_candidates": dedupe_res["total_candidates"],
                "unique_products": dedupe_res["unique_products"],
                "duplicates_log": dedupe_res["duplicates_log"],
            },
            fh,
            indent=2,
            ensure_ascii=False,
        )

    # 3. Filter import_mode == blocked
    candidates: list[dict] = []
    for p in kept:
        if p.get("import_mode") == "blocked" and not include_blocked:
            stats["skipped_blocked"] += 1
            continue
        if not (p.get("variants") or []):
            stats["skipped_no_variants"] += 1
            continue
        candidates.append(p)

    # 4. Assign unique handles. Start with disambiguate_handle, then add
    #    a deterministic short fingerprint suffix on remaining collisions.
    proposed: list[str] = [disambiguate_handle(p, None) for p in candidates]
    counts = Counter(proposed)
    handles: list[str] = []
    for p, base in zip(candidates, proposed):
        if counts[base] > 1:
            fp = short_fp(p.get("source_group_id") or p.get("title") or "")
            handles.append(f"{base}-{fp}")
            stats["handle_disambiguated"] += 1
        else:
            handles.append(base)

    # Verify uniqueness
    if len(set(handles)) != len(handles):
        # Fall back to indexed suffix
        seen: dict[str, int] = {}
        for i, h in enumerate(handles):
            if h in seen:
                seen[h] += 1
                handles[i] = f"{h}-{seen[h]}"
                stats["handle_indexed_suffix"] += 1
            else:
                seen[h] = 1

    # 4b. Resolve category for every candidate (breadcrumb cache → title regex)
    # Index normalized files by pid for resolver lookups (recursive — supports per-species subfolders).
    norm_index: dict[str, dict] = {}
    for nf in glob.glob(os.path.join(normalized_dir, "**", "chewy_*.json"), recursive=True):
        try:
            nd = json.load(open(nf, "r", encoding="utf-8"))
        except Exception:
            continue
        pid_n = str(nd.get("source_product_id") or "")
        if pid_n:
            norm_index[pid_n] = nd

    resolved_categories: list[dict] = []
    cat_source_count = Counter()
    for p in candidates:
        canonical = p.get("canonical_source") or {}
        pid = canonical.get("source_product_id") or (
            (p.get("source_group_id") or "").split(":")[0]
        )
        nrm = norm_index.get(str(pid), {})
        res = resolve_category(nrm, p)
        resolved_categories.append(res)
        cat_source_count[res["source"]] += 1
        # Inject category metafields directly into product's metafields_plan
        mp = p.setdefault("metafields_plan", {})
        mp[CATEGORY_KEY_PATH] = res["path"]
        if res["path"]:
            mp[CATEGORY_KEY_LEAF] = res["path"][-1]
        mp[CATEGORY_KEY_SOURCE] = res["source"]

    stats["category_breadcrumb"] = cat_source_count.get("breadcrumb", 0)
    stats["category_title_regex"] = cat_source_count.get("title_regex", 0)

    # 5. Discover metafield keys after dedupe (smaller, cleaner)
    mf_keys = discover_metafield_keys(candidates)
    headers = PRODUCT_HEADER + [metafield_column_name(k) for k in mf_keys]

    with (
        open(out_products, "w", newline="", encoding="utf-8") as fp,
        open(out_inventory, "w", newline="", encoding="utf-8") as fi,
    ):
        w = csv.DictWriter(fp, fieldnames=headers, extrasaction="ignore")
        w.writeheader()
        inv = csv.DictWriter(
            fi,
            fieldnames=[
                "Handle",
                "Variant SKU",
                "Variant Inventory Tracker",
                "Variant Inventory Qty",
                "Variant Inventory Policy",
            ],
        )
        inv.writeheader()

        for p, handle, cat_res in zip(candidates, handles, resolved_categories):
            canonical = p.get("canonical_source") or {}
            source_pid = canonical.get("source_product_id") or (
                p.get("source_group_id") or ""
            ).split(":")[0]
            pat = pat_index.get(str(source_pid), {})

            title = p.get("title") or ""
            vendor = p.get("brand") or ""
            body_html = md_to_html(p.get("description"))
            cat_path = cat_res.get("path") or []
            is_bundle = bool(cat_res.get("is_bundle"))
            ptype = product_type(pat, cat_path)
            mp = p.get("metafields_plan") or {}
            tags = build_tags(p, mp, pat, source_pid, cat_path, is_bundle)
            variants = p.get("variants") or []

            mf_cells = {
                metafield_column_name(k): metafield_cell(k, mp.get(k))
                for k in mf_keys
            }
            p_imgs = list(dict.fromkeys(p.get("images") or []))[:8]

            for vi, v in enumerate(variants):
                row = build_row_template(headers)
                row["Handle"] = handle
                if vi == 0:
                    row["Title"] = title
                    row["Body (HTML)"] = body_html
                    row["Vendor"] = vendor
                    row["Type"] = ptype
                    row["Product Category"] = ""
                    row["Tags"] = tags
                    row["Published"] = "TRUE" if status.lower() == "active" else "FALSE"
                    row["Status"] = status.lower()
                    row["SEO Title"] = title[:70]
                    row["SEO Description"] = (
                        (p.get("description") or "").strip().replace("\n", " ")[:155]
                    )
                    row["Gift Card"] = "FALSE"
                    for col, val in mf_cells.items():
                        row[col] = val

                row["Option1 Name"] = v.get("option1_name") or "Size"
                row["Option1 Value"] = v.get("option1_value") or "Default"
                row["Variant SKU"] = safe_str(v.get("sku"))
                row["Variant Grams"] = "0"
                row["Variant Inventory Tracker"] = "shopify"
                in_stock = bool(v.get("in_stock"))
                row["Variant Inventory Qty"] = str(in_stock_qty if in_stock else 0)
                row["Variant Inventory Policy"] = v.get(
                    "shopify_inventory_policy"
                ) or ("continue" if in_stock else "deny")
                row["Variant Fulfillment Service"] = "manual"
                row["Variant Price"] = clean_price(v.get("price"))
                row["Variant Compare At Price"] = clean_price(
                    v.get("compare_at_price")
                )
                row["Variant Requires Shipping"] = "TRUE"
                row["Variant Taxable"] = "TRUE"
                ids = v.get("identifiers") or {}
                row["Variant Barcode"] = safe_str(ids.get("upc") or ids.get("gtin"))
                row["Variant Weight Unit"] = "lb"
                v_imgs = v.get("images") or []
                img_src = (
                    v_imgs[0] if v_imgs else (p_imgs[0] if p_imgs else "")
                ) or ""
                row["Image Src"] = img_src
                row["Image Position"] = str(vi + 1)
                row["Image Alt Text"] = (
                    f"{title} – {row['Option1 Value']}"[:512] if title else ""
                )

                w.writerow(row)
                stats["variant_rows"] += 1

                inv.writerow(
                    {
                        "Handle": handle,
                        "Variant SKU": row["Variant SKU"],
                        "Variant Inventory Tracker": "shopify",
                        "Variant Inventory Qty": row["Variant Inventory Qty"],
                        "Variant Inventory Policy": row["Variant Inventory Policy"],
                    }
                )

            used_imgs = set()
            for v in variants:
                for u in v.get("images") or []:
                    used_imgs.add(u)
            extra = [u for u in p_imgs if u not in used_imgs][:6]
            for i, img in enumerate(extra):
                row = build_row_template(headers)
                row["Handle"] = handle
                row["Image Src"] = img
                row["Image Position"] = str(len(variants) + i + 1)
                w.writerow(row)
                stats["extra_image_rows"] += 1

            stats["shopify_products"] += 1

    return {"stats": dict(stats), "metafield_keys": mf_keys}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="output/grouped_products")
    ap.add_argument("--output-dir", default="output/shopify_export")
    ap.add_argument("--status", default="draft", choices=["draft", "active"])
    ap.add_argument("--in-stock-qty", type=int, default=999)
    ap.add_argument("--include-blocked", action="store_true")
    ap.add_argument("--pids-file", help="Path to a text file with one source product_id per line. Only files whose pid is in this set are exported.")
    ap.add_argument("--normalized-dir", default="output/normalized_products",
                    help="Directory containing normalized chewy_*.json files (used to enrich resolver with category_path); searched recursively.")
    args = ap.parse_args()

    files = sorted(
        glob.glob(os.path.join(args.input, "chewy_grouped_by_flavor_*.json"))
    )
    if not files:
        print(f"No grouped files in {args.input}")
        return

    if args.pids_file:
        with open(args.pids_file, "r", encoding="utf-8") as pf:
            kept = {line.strip() for line in pf if line.strip()}
        before = len(files)
        files = [f for f in files if os.path.basename(f).removeprefix("chewy_grouped_by_flavor_").removesuffix(".json") in kept]
        print(f"Filtered by --pids-file: {before} -> {len(files)} files")
        if not files:
            print("No files matched the pid filter.")
            return

    os.makedirs(args.output_dir, exist_ok=True)
    out_products = os.path.join(args.output_dir, "shopify_products.csv")
    out_inv = os.path.join(args.output_dir, "shopify_inventory.csv")
    out_log = os.path.join(args.output_dir, "shopify_dedupe_log.json")

    result = export_csv(
        files,
        out_products,
        out_inv,
        out_log,
        args.status,
        args.in_stock_qty,
        args.include_blocked,
        args.normalized_dir,
    )

    print(f"Read {len(files)} grouped files")
    print(f"Discovered {len(result['metafield_keys'])} metafield keys:")
    for k in result["metafield_keys"]:
        print(f"  - {k}  ({shopify_metafield_type(k)})")
    print()
    print("Stats:")
    for k, v in result["stats"].items():
        print(f"  {k}: {v}")
    print()
    print(f"Wrote {out_products}  ({os.path.getsize(out_products)/1024/1024:.2f} MB)")
    print(f"Wrote {out_inv}       ({os.path.getsize(out_inv)/1024/1024:.2f} MB)")
    print(f"Wrote {out_log}")


if __name__ == "__main__":
    main()
