"""Render an HTML preview of how the Shopify CSV will look.

Reads grouped_products, runs dedupe, samples a diverse set across brands and
discriminator types, and renders one card per Shopify-product-to-be with:
  - title, handle (the final one we'd import), brand, type, tags
  - product images
  - variants table (option1, sku, price, stock, inventory policy, barcode)
  - metafields_plan (collapsed, click to expand JSON)
  - body description preview

Output: output/preview/shopify_split_preview.html
"""
from __future__ import annotations

import argparse
import glob
import html
import json
import os
import random
import re
import sys
from collections import defaultdict
from typing import Any

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)

from chewy_next_json_extractor import dedupe_products_across_pages  # noqa
from tools.category_resolver import resolve_category  # noqa
from tools.build_shopify_csv import (  # noqa
    JSON_KEYS,
    LIST_TEXT_KEYS,
    URL_KEYS,
    build_tags,
    clean_price,
    disambiguate_handle,
    md_to_html,
    metafield_cell,
    metafield_column_name,
    product_type,
    shopify_metafield_type,
    short_fp,
    slugify,
)


def load_pat_index(d: str) -> dict[str, dict]:
    idx: dict[str, dict] = {}
    for f in glob.glob(os.path.join(d, "chewy_*.json")):
        try:
            data = json.load(open(f, "r", encoding="utf-8"))
        except Exception:
            continue
        pid = str(data.get("source_product_id") or "")
        if pid:
            idx[pid] = data.get("product_attributes_table") or {}
    return idx


def sample_products(
    products: list[dict], limit: int, seed: int
) -> list[dict]:
    """Diverse sample: cycle through brands, prefer products with discriminator splits."""
    rng = random.Random(seed)
    by_brand: dict[str, list[dict]] = defaultdict(list)
    for p in products:
        by_brand[(p.get("brand") or "?")].append(p)
    brands = list(by_brand.keys())
    rng.shuffle(brands)
    out: list[dict] = []
    while len(out) < limit and brands:
        for b in list(brands):
            if not by_brand[b]:
                brands.remove(b)
                continue
            # Within a brand, prefer ones with a discriminator (= split happened)
            pool = by_brand[b]
            rng.shuffle(pool)
            pool.sort(key=lambda x: 0 if (x.get("discriminator") or x.get("flavor")) else 1)
            out.append(pool.pop(0))
            if len(out) >= limit:
                break
    return out


def render_variant_row(v: dict) -> str:
    ids = v.get("identifiers") or {}
    upc = ids.get("upc") or ids.get("gtin") or "—"
    price = clean_price(v.get("price")) or "—"
    in_stock = bool(v.get("in_stock"))
    pol = v.get("shopify_inventory_policy") or ("continue" if in_stock else "deny")
    qty = 999 if in_stock else 0
    badge = (
        '<span class="badge in">IN STOCK</span>'
        if in_stock
        else '<span class="badge out">OOS</span>'
    )
    img = (v.get("images") or [None])[0]
    img_html = (
        f'<img loading="lazy" src="{html.escape(img)}" alt="">'
        if img
        else '<div class="noimg">—</div>'
    )
    return f"""
<tr>
  <td class="vimgc">{img_html}</td>
  <td>{html.escape(v.get("option1_value") or "")}</td>
  <td><code>{html.escape(str(v.get("sku") or ""))}</code></td>
  <td><code>{html.escape(str(upc))}</code></td>
  <td class="price">${html.escape(price)}</td>
  <td>{badge}</td>
  <td><code>{html.escape(pol)}</code></td>
  <td>{qty}</td>
  <td><code>{html.escape(str(v.get("source_entry_id") or ""))}</code></td>
</tr>"""


def render_product_card(
    p: dict, handle: str, pat: dict, anchor: str, cat_res: dict
) -> str:
    title = p.get("title") or "(no title)"
    brand = p.get("brand") or "—"
    cat_path = cat_res.get("path") or []
    is_bundle = bool(cat_res.get("is_bundle"))
    cat_source = cat_res.get("source") or "?"
    ptype = product_type(pat, cat_path)
    mp = p.get("metafields_plan") or {}
    canonical = p.get("canonical_source") or {}
    source_pid = canonical.get("source_product_id") or ""
    tags = build_tags(p, mp, pat, source_pid, cat_path, is_bundle)
    variants = p.get("variants") or []
    n_in = sum(1 for v in variants if v.get("in_stock"))
    n_out = len(variants) - n_in
    disc = p.get("discriminator") or {}
    disc_html = " ".join(
        f'<span class="chip"><strong>{html.escape(str(k))}</strong>: {html.escape(str(v))}</span>'
        for k, v in disc.items()
    )
    if p.get("flavor") and "flavor" not in {k.lower() for k in disc}:
        disc_html += f' <span class="chip"><strong>flavor</strong>: {html.escape(str(p["flavor"]))}</span>'
    images = list(dict.fromkeys(p.get("images") or []))[:8]
    gallery = "".join(
        f'<img loading="lazy" src="{html.escape(u)}" alt="">' for u in images
    )

    # Tag chips
    tag_chips = " ".join(
        f'<span class="chip tag">{html.escape(t.strip())}</span>'
        for t in tags.split(",")
        if t.strip()
    )

    # Metafields preview
    mf_rows: list[str] = []
    for k in sorted(mp.keys()):
        val = mp[k]
        if val in (None, "", [], {}):
            continue
        if k in LIST_TEXT_KEYS and isinstance(val, str):
            try:
                if not json.loads(val):
                    continue
            except Exception:
                continue
        tname = shopify_metafield_type(k)
        cell = metafield_cell(k, val)
        if not cell:
            continue
        display = cell if len(cell) <= 200 else (cell[:200] + " …")
        mf_rows.append(
            f"""<tr>
  <td><code>{html.escape(k)}</code></td>
  <td><code class="muted">[{tname}]</code></td>
  <td><div class="mfval">{html.escape(display)}</div></td>
</tr>"""
        )
    mf_html = (
        f"""<details class="mfd"><summary>metafields_plan ({len(mf_rows)} populated keys)</summary>
<table class="mft"><thead><tr><th>Key</th><th>Type</th><th>Value</th></tr></thead><tbody>{''.join(mf_rows)}</tbody></table>
</details>"""
        if mf_rows
        else '<div class="muted">no metafields</div>'
    )

    canonical_html = ""
    if canonical:
        dup_count = canonical.get("duplicate_count") or 0
        dropped = canonical.get("dropped_source_pids") or []
        if dup_count > 1 and dropped:
            canonical_html = f"""
<div class="dedupe">↪ <strong>dedupe</strong>: chosen from {dup_count} duplicate landing pages.
Dropped pids: <code>{', '.join(map(html.escape, dropped))}</code></div>"""

    cat_breadcrumb = " <span class='muted'>›</span> ".join(
        f'<span class="catseg">{html.escape(s)}</span>' for s in cat_path
    ) if cat_path else '<span class="muted">no category</span>'
    cat_source_label = {
        "normalized_category_path": "from scraper (rescrape)",
        "breadcrumb": "from breadcrumb cache",
        "title_regex": "from title regex",
    }.get(cat_source, cat_source)

    return f"""
<article class="product" id="{html.escape(anchor)}">
  <header class="phead">
    <h2>{html.escape(title)}</h2>
    <div class="catbar">
      <span class="muted">Category:</span> {cat_breadcrumb}
      <span class="chip catsrc">{html.escape(cat_source_label)}</span>
      {'<span class="chip bundle">bundle</span>' if is_bundle else ''}
    </div>
    <div class="pmeta">
      <span class="chip"><strong>handle</strong>: <code>{html.escape(handle)}</code></span>
      <span class="chip"><strong>vendor</strong>: {html.escape(brand)}</span>
      <span class="chip"><strong>type</strong>: {html.escape(ptype or '—')}</span>
      <span class="chip"><strong>chewy pid</strong>: <code>{html.escape(source_pid or '')}</code></span>
      <span class="badge in">{n_in} in stock</span>
      <span class="badge out">{n_out} OOS</span>
    </div>
    <div class="pmeta">{disc_html}</div>
    <div class="pmeta tags">{tag_chips}</div>
    {canonical_html}
  </header>
  <div class="gallery">{gallery}</div>

  <h3>Variants ({len(variants)})</h3>
  <table class="vartbl">
    <thead><tr>
      <th>Image</th>
      <th>Option1 Value (Size)</th>
      <th>SKU</th>
      <th>UPC / GTIN</th>
      <th>Price</th>
      <th>Stock</th>
      <th>Inv Policy</th>
      <th>Qty</th>
      <th>Entry ID</th>
    </tr></thead>
    <tbody>{''.join(render_variant_row(v) for v in variants)}</tbody>
  </table>

  <h3>Metafields</h3>
  {mf_html}

  <details class="bodyd"><summary>Body (HTML) preview</summary>
    <div class="bodyc">{md_to_html(p.get("description"))}</div>
  </details>
</article>
"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="output/grouped_products")
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument(
        "--output", default="output/preview/shopify_split_preview.html"
    )
    ap.add_argument("--pids-file", help="Path to text file with one source product_id per line. Only files whose pid is in this set are sampled.")
    ap.add_argument("--normalized-dir", default="output/normalized_products",
                    help="Directory containing normalized chewy_*.json files; searched recursively.")
    args = ap.parse_args()

    files = sorted(glob.glob(os.path.join(args.input, "chewy_grouped_by_flavor_*.json")))
    if args.pids_file:
        with open(args.pids_file, "r", encoding="utf-8") as pf:
            kept = {line.strip() for line in pf if line.strip()}
        before = len(files)
        files = [f for f in files if os.path.basename(f).removeprefix("chewy_grouped_by_flavor_").removesuffix(".json") in kept]
        print(f"Filtered by --pids-file: {before} -> {len(files)} files")
    if not files:
        print(f"no files in {args.input}")
        return
    all_grouped: list[dict] = []
    for f in files:
        try:
            all_grouped.append(json.load(open(f, "r", encoding="utf-8")))
        except Exception:
            pass

    res = dedupe_products_across_pages(all_grouped)
    kept = res["kept_products"]
    kept = [p for p in kept if p.get("import_mode") != "blocked" and (p.get("variants") or [])]

    sampled = sample_products(kept, args.limit, args.seed)

    pat_idx = load_pat_index(args.normalized_dir)
    norm_idx: dict[str, dict] = {}
    for nf in glob.glob(os.path.join(args.normalized_dir, "**", "chewy_*.json"), recursive=True):
        try:
            nd = json.load(open(nf, "r", encoding="utf-8"))
        except Exception:
            continue
        pid_n = str(nd.get("source_product_id") or "")
        if pid_n:
            norm_idx[pid_n] = nd

    # Compute handles using the same logic as the CSV exporter
    from collections import Counter as _C
    proposed = [disambiguate_handle(p, None) for p in sampled]
    counts = _C(proposed)
    handles: list[str] = []
    for p, base in zip(sampled, proposed):
        if counts[base] > 1:
            fp = short_fp(p.get("source_group_id") or p.get("title") or "")
            handles.append(f"{base}-{fp}")
        else:
            handles.append(base)

    cards: list[str] = []
    toc: list[str] = []
    for p, handle in zip(sampled, handles):
        canonical = p.get("canonical_source") or {}
        pid = canonical.get("source_product_id") or (
            (p.get("source_group_id") or "").split(":")[0]
        )
        pat = pat_idx.get(str(pid), {})
        nrm = norm_idx.get(str(pid), {})
        cat_res = resolve_category(nrm, p)
        leaf = (cat_res.get("path") or ["—"])[-1]
        anchor = f"p-{handle}"
        toc.append(
            f'<li><a href="#{html.escape(anchor)}"><span class="t-brand">{html.escape(p.get("brand") or "")}</span> <span class="t-cat">{html.escape(leaf)}</span> {html.escape(p.get("title") or "")[:80]}</a></li>'
        )
        cards.append(render_product_card(p, handle, pat, anchor, cat_res))

    css = """
:root { color-scheme: light dark; }
* { box-sizing:border-box; }
body { margin:0; background:#0d0f14; color:#e9ecf2; font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif; }
@media (prefers-color-scheme: light){
  body { background:#f6f7f9; color:#1a1d23; }
  .product { background:#fff; border-color:#e3e6ea !important; }
  .chip { background:#eef0f4 !important; color:#1a1d23 !important; }
  nav.toc, header.page { background:#fff !important; border-color:#e3e6ea !important; }
  .vartbl th, .mft th { background:#f0f2f5 !important; }
}
header.page { padding:18px 26px; border-bottom:1px solid #262a33; background:#14161b; position:sticky; top:0; z-index:9; }
header.page h1 { margin:0 0 4px; font-size:18px; }
header.page .stats { color:#8b93a2; font-size:12px; display:flex; gap:16px; flex-wrap:wrap; }
.layout { display:grid; grid-template-columns: 300px 1fr; }
nav.toc { border-right:1px solid #262a33; padding:14px; max-height: calc(100vh - 70px); overflow:auto; position:sticky; top:70px; background:#14161b; }
nav.toc h3 { margin:0 0 8px; font-size:12px; color:#8b93a2; text-transform:uppercase; letter-spacing:.06em; }
nav.toc ul { list-style:none; margin:0; padding:0; font-size:12px; }
nav.toc a { color:inherit; text-decoration:none; display:block; padding:4px 6px; border-radius:4px; }
nav.toc a:hover { background:#1b1e25; color:#4ea3ff; }
.t-brand { color:#4ea3ff; font-weight:600; margin-right:6px; }
.t-cat { color:#25c469; font-size:10px; padding:1px 5px; background:rgba(37,196,105,.10); border-radius:3px; margin-right:6px; }
.catbar { margin:8px 0; padding:8px 12px; background:rgba(37,196,105,.06); border-left:3px solid #25c469; border-radius:4px; font-size:13px; display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
.catseg { font-weight:600; color:#25c469; }
.catsrc { background:rgba(37,196,105,.12) !important; color:#25c469 !important; font-size:10px; }
.chip.bundle { background:rgba(238,79,79,.16) !important; color:#ee4f4f !important; }
main { padding:20px 28px; max-width:1300px; }
article.product { background:#14161b; border:1px solid #262a33; border-radius:10px; padding:18px; margin-bottom:22px; }
article.product h2 { margin:0 0 6px; font-size:17px; }
article.product h3 { margin:18px 0 8px; font-size:13px; color:#8b93a2; text-transform:uppercase; letter-spacing:.06em; }
.pmeta { display:flex; gap:6px; flex-wrap:wrap; align-items:center; margin-top:6px; }
.pmeta.tags { margin-top:8px; }
.chip { background:#232733; padding:3px 8px; border-radius:999px; font-size:11px; }
.chip code, .chip strong { font-size:11px; }
.chip.tag { background:rgba(78,163,255,.10); color:#4ea3ff; }
.badge { padding:3px 8px; border-radius:4px; font-size:11px; font-weight:600; }
.badge.in { background:rgba(37,196,105,.16); color:#25c469; }
.badge.out { background:rgba(238,79,79,.16); color:#ee4f4f; }
.gallery { display:flex; gap:8px; overflow-x:auto; padding:12px 0; }
.gallery img { width:110px; height:110px; object-fit:contain; background:#0d0f14; border:1px solid #262a33; border-radius:6px; }
.vartbl, .mft { width:100%; border-collapse:collapse; font-size:12px; }
.vartbl th, .vartbl td, .mft th, .mft td { border:1px solid #262a33; padding:6px 8px; text-align:left; vertical-align:top; }
.vartbl th, .mft th { background:#1b1e25; font-weight:600; }
.vartbl .vimgc { width:60px; }
.vartbl .vimgc img { width:50px; height:50px; object-fit:contain; background:#0d0f14; border-radius:4px; }
.vartbl .vimgc .noimg { width:50px; height:50px; color:#8b93a2; display:flex; align-items:center; justify-content:center; }
.vartbl .price { font-weight:600; color:#4ea3ff; }
code { background:rgba(255,255,255,.04); padding:1px 4px; border-radius:3px; font-size:11px; }
.muted { color:#8b93a2; }
.mfval { max-width:600px; word-break:break-word; font-family: monospace; font-size:11px; color:#b9c1ce; }
.mfd summary, .bodyd summary { cursor:pointer; color:#8b93a2; font-size:12px; padding:6px 0; }
.mfd[open] summary, .bodyd[open] summary { color:#4ea3ff; }
.bodyc { background:#0d0f14; border:1px solid #262a33; border-radius:6px; padding:14px; margin-top:8px; font-size:13px; }
.bodyc table { border-collapse:collapse; }
.bodyc table th, .bodyc table td { border:1px solid #262a33; padding:3px 8px; }
.dedupe { margin-top:10px; padding:8px 12px; background:rgba(78,163,255,.07); border-left:3px solid #4ea3ff; font-size:12px; border-radius:4px; }
p { margin:6px 0; }
"""

    total = len(kept)
    page = f"""<!doctype html>
<html lang="vi"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Shopify split preview — {len(sampled)} / {total}</title>
<style>{css}</style>
</head><body>
<header class="page">
  <h1>Shopify split preview — {len(sampled)} of {total} unique products</h1>
  <div class="stats">
    <span>After dedupe: <strong>{total}</strong> Shopify products</span>
    <span>Sample size: <strong>{len(sampled)}</strong></span>
    <span>Status default: <strong>draft</strong></span>
    <span>In-stock qty: <strong>999</strong></span>
  </div>
</header>
<div class="layout">
  <nav class="toc">
    <h3>Products ({len(sampled)})</h3>
    <ul>{''.join(toc)}</ul>
  </nav>
  <main>{''.join(cards)}</main>
</div>
</body></html>
"""

    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as fh:
        fh.write(page)
    print(f"wrote {args.output}  ({os.path.getsize(args.output)/1024:.1f} KB)")


if __name__ == "__main__":
    main()
