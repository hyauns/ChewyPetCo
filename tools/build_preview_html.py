"""Build a single self-contained HTML page that previews scraped Chewy products.

Reads output/normalized_products/chewy_*.json, samples a diverse set, and
renders an inspectable HTML at output/preview/normalized_preview.html.

Usage:
    python tools/build_preview_html.py [--limit 40] [--all] [--seed 42]
"""
from __future__ import annotations

import argparse
import glob
import html
import json
import os
import random
import re
from collections import Counter, defaultdict
from typing import Any


def md_to_html(text: str | None) -> str:
    if not text:
        return ""
    s = str(text).strip()
    if not s:
        return ""
    lines = s.split("\n")
    out: list[str] = []
    table_buf: list[str] = []
    in_list = False

    def flush_table() -> None:
        nonlocal table_buf
        if not table_buf:
            return
        rows = [r.strip().strip("|").split("|") for r in table_buf]
        rows = [[c.strip() for c in r] for r in rows]
        if len(rows) >= 2 and all(re.fullmatch(r":?-+:?", c.strip()) for c in rows[1]):
            header, body = rows[0], rows[2:]
        else:
            header, body = rows[0], rows[1:]
        out.append('<table class="md-tbl"><thead><tr>')
        for h in header:
            out.append(f"<th>{html.escape(h)}</th>")
        out.append("</tr></thead><tbody>")
        for r in body:
            out.append("<tr>")
            for c in r:
                out.append(f"<td>{html.escape(c)}</td>")
            out.append("</tr>")
        out.append("</tbody></table>")
        table_buf = []

    def flush_list() -> None:
        nonlocal in_list
        if in_list:
            out.append("</ol>")
            in_list = False

    for raw in lines:
        stripped = raw.rstrip()
        if stripped.startswith("|") and "|" in stripped[1:]:
            flush_list()
            table_buf.append(stripped)
            continue
        flush_table()
        if not stripped.strip():
            flush_list()
            out.append("")
            continue
        m = re.match(r"^\s*(\d+)\.\s+(.*)$", stripped)
        if m:
            if not in_list:
                out.append("<ol>")
                in_list = True
            out.append(f"<li>{_inline(m.group(2))}</li>")
            continue
        flush_list()
        out.append(f"<p>{_inline(stripped)}</p>")
    flush_table()
    flush_list()
    return "\n".join(out)


def _inline(s: str) -> str:
    esc = html.escape(s)
    esc = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", esc)
    esc = re.sub(r"\*([^*]+)\*", r"<em>\1</em>", esc)
    return esc


def fmt_price(v: Any) -> str:
    if v in (None, ""):
        return "—"
    return html.escape(str(v))


def sample_diverse(files: list[str], limit: int, seed: int) -> list[str]:
    """Sample limit files trying to cover multiple brands."""
    rng = random.Random(seed)
    by_brand: dict[str, list[str]] = defaultdict(list)
    for f in files:
        try:
            d = json.load(open(f, "r", encoding="utf-8"))
        except Exception:
            continue
        brand = (d.get("brand") or "unknown").strip() or "unknown"
        by_brand[brand].append(f)
    brands = list(by_brand.keys())
    rng.shuffle(brands)
    out: list[str] = []
    while len(out) < limit and brands:
        for b in list(brands):
            if not by_brand[b]:
                brands.remove(b)
                continue
            pick = by_brand[b].pop(rng.randrange(len(by_brand[b])))
            out.append(pick)
            if len(out) >= limit:
                break
    return out


def render_variant(v: dict, idx: int) -> str:
    opts = v.get("option_values") or {}
    opt_chips = " ".join(
        f'<span class="chip">{html.escape(str(k))}: <strong>{html.escape(str(val))}</strong></span>'
        for k, val in opts.items()
    )
    stock_cls = "in" if v.get("in_stock") else "out"
    stock_label = "IN STOCK" if v.get("in_stock") else "OUT OF STOCK"
    inv_pol = v.get("shopify_inventory_policy") or "—"
    img = (v.get("images") or [None])[0]
    img_html = (
        f'<img loading="lazy" src="{html.escape(img)}" alt="">'
        if img
        else '<div class="noimg">no img</div>'
    )
    ids = v.get("identifiers") or {}
    upc = ids.get("upc") or ids.get("gtin") or "—"
    return f"""
<div class="variant">
  <div class="vimg">{img_html}</div>
  <div class="vbody">
    <div class="vhead">
      <div class="vtitle">#{idx}. {html.escape(v.get("title") or "")}</div>
      <span class="badge {stock_cls}">{stock_label}</span>
    </div>
    <div class="vmeta">
      <span class="chip">SKU: <code>{html.escape(str(v.get("sku") or "—"))}</code></span>
      <span class="chip">entry_id: <code>{html.escape(str(v.get("source_entry_id") or "—"))}</code></span>
      <span class="chip">UPC: <code>{html.escape(str(upc))}</code></span>
      {opt_chips}
    </div>
    <div class="vprice">
      <span class="price">{fmt_price(v.get("price"))}</span>
      <span class="muted">autoship {fmt_price(v.get("autoship_price"))}</span>
      <span class="muted">inv_policy: <code>{html.escape(str(inv_pol))}</code></span>
      <span class="muted">stock_reason: <code>{html.escape(str(v.get("stock_reason") or "—"))}</code></span>
    </div>
    <details class="vdet"><summary>variant content</summary>
      <div class="grid2">
        <section><h5>Description</h5>{md_to_html(v.get("description"))}</section>
        <section><h5>Ingredients</h5>{md_to_html(v.get("ingredients"))}</section>
        <section><h5>Guaranteed Analysis</h5>{md_to_html(v.get("guaranteed_analysis"))}</section>
        <section><h5>Feeding</h5>{md_to_html(v.get("feeding_instructions"))}</section>
        <section><h5>Transition</h5>{md_to_html(v.get("transition_instructions"))}</section>
        <section><h5>Calorie</h5>{md_to_html(v.get("calorie_content"))}</section>
      </div>
      <div class="vurl">
        <a href="{html.escape(v.get("variant_url") or "#")}" target="_blank">↗ Open on Chewy</a>
      </div>
    </details>
  </div>
</div>
"""


def render_product(d: dict, anchor: str) -> str:
    title = d.get("title") or "(no title)"
    brand = d.get("brand") or "—"
    pid = d.get("source_product_id") or "—"
    url = d.get("source_url") or "#"
    variants = d.get("variants") or []
    n_oos = sum(1 for v in variants if v.get("out_of_stock"))
    n_in = len(variants) - n_oos
    stock_summary = (
        f"<span class='badge out'>ALL {len(variants)} OOS</span>"
        if n_oos == len(variants) and len(variants) > 0
        else f"<span class='badge in'>{n_in} in</span> <span class='badge out'>{n_oos} oos</span>"
    )
    pat = d.get("product_attributes_table") or {}
    pat_chips = " ".join(
        f'<span class="chip">{html.escape(str(k))}: {html.escape(", ".join(str(x) for x in (v if isinstance(v, list) else [v])))}</span>'
        for k, v in pat.items()
    )
    imgs = d.get("images") or []
    gallery = "".join(
        f'<img loading="lazy" src="{html.escape(u)}" alt="">' for u in imgs[:8]
    )
    variants_html = "\n".join(render_variant(v, i + 1) for i, v in enumerate(variants))
    return f"""
<article class="product" id="{html.escape(anchor)}">
  <header class="phead">
    <div class="ptitle">
      <h2>{html.escape(title)}</h2>
      <div class="pmeta">
        <span class="chip"><strong>brand</strong>: {html.escape(brand)}</span>
        <span class="chip"><strong>pid</strong>: <code>{html.escape(str(pid))}</code></span>
        <span class="chip"><strong>variants</strong>: {len(variants)}</span>
        {stock_summary}
        <a class="chip linkchip" href="{html.escape(url)}" target="_blank">↗ source</a>
      </div>
      <div class="pmeta">{pat_chips}</div>
    </div>
  </header>
  <div class="gallery">{gallery}</div>
  <div class="pcontent grid2">
    <section><h4>Description</h4>{md_to_html(d.get("description"))}</section>
    <section><h4>Ingredients</h4>{md_to_html(d.get("ingredients"))}</section>
    <section><h4>Guaranteed Analysis</h4>{md_to_html(d.get("guaranteed_analysis"))}</section>
    <section><h4>Feeding Instructions</h4>{md_to_html(d.get("feeding_instructions"))}</section>
    <section><h4>Transition</h4>{md_to_html(d.get("transition_instructions"))}</section>
  </div>
  <h3 class="varhead">Variants ({len(variants)})</h3>
  <div class="variants">{variants_html}</div>
</article>
"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument(
        "--input", default="output/normalized_products", help="dir of chewy_*.json"
    )
    ap.add_argument(
        "--output", default="output/preview/normalized_preview.html"
    )
    args = ap.parse_args()

    files = sorted(glob.glob(os.path.join(args.input, "chewy_*.json")))
    total_files = len(files)
    if not args.all:
        files = sample_diverse(files, args.limit, args.seed)

    products = []
    for f in files:
        try:
            d = json.load(open(f, "r", encoding="utf-8"))
            products.append(d)
        except Exception as e:
            print(f"skip {f}: {e}")

    brands = Counter((p.get("brand") or "?").strip() for p in products)
    total_variants = sum(len(p.get("variants") or []) for p in products)
    all_oos = sum(
        1
        for p in products
        if (p.get("variants") and all(v.get("out_of_stock") for v in p["variants"]))
    )

    toc_items = []
    body_items = []
    for p in products:
        pid = p.get("source_product_id") or "x"
        anchor = f"p-{pid}"
        title = p.get("title") or "(no title)"
        brand = p.get("brand") or "—"
        toc_items.append(
            f'<li><a href="#{html.escape(anchor)}"><span class="t-brand">{html.escape(brand)}</span> {html.escape(title)}</a></li>'
        )
        body_items.append(render_product(p, anchor))

    page_title = f"Chewy normalized preview — {len(products)} products"

    css = """
:root {
  color-scheme: light dark;
  --bg: #0b0c10;
  --panel: #14161b;
  --panel-2: #1b1e25;
  --border: #262a33;
  --text: #e6e8eb;
  --muted: #8b93a2;
  --accent: #4ea3ff;
  --good: #25c469;
  --bad: #ee4f4f;
  --chip: #232733;
}
@media (prefers-color-scheme: light) {
  :root {
    --bg: #f6f7f8;
    --panel: #ffffff;
    --panel-2: #fafbfc;
    --border: #e3e6ea;
    --text: #16181d;
    --muted: #5e6470;
    --accent: #1a64d8;
    --chip: #eef0f4;
  }
}
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--text); font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
header.page { padding:24px 32px; border-bottom:1px solid var(--border); background:var(--panel); position:sticky; top:0; z-index:10; backdrop-filter:blur(6px); }
header.page h1 { margin:0 0 6px; font-size:18px; }
header.page .stats { color:var(--muted); font-size:13px; display:flex; gap:18px; flex-wrap:wrap; }
.layout { display:grid; grid-template-columns:280px 1fr; }
nav.toc { border-right:1px solid var(--border); padding:16px; max-height:calc(100vh - 88px); overflow:auto; position:sticky; top:88px; }
nav.toc h3 { margin:0 0 8px; font-size:12px; color:var(--muted); text-transform:uppercase; letter-spacing:.08em; }
nav.toc ul { list-style:none; padding:0; margin:0; font-size:12px; }
nav.toc li { margin-bottom:4px; }
nav.toc a { color:var(--text); text-decoration:none; display:block; padding:4px 6px; border-radius:4px; }
nav.toc a:hover { background:var(--panel-2); color:var(--accent); }
nav.toc .t-brand { color:var(--accent); font-weight:600; margin-right:6px; }
main { padding:24px 32px; max-width:1200px; }
article.product { background:var(--panel); border:1px solid var(--border); border-radius:10px; padding:20px; margin-bottom:24px; }
article.product h2 { margin:0 0 6px; font-size:18px; }
.pmeta { display:flex; gap:6px; flex-wrap:wrap; margin-top:6px; align-items:center; }
.chip { background:var(--chip); padding:3px 8px; border-radius:999px; font-size:12px; color:var(--text); }
.chip code { font-size:11px; }
.chip.linkchip { color:var(--accent); text-decoration:none; }
.badge { padding:3px 8px; border-radius:4px; font-size:11px; font-weight:600; }
.badge.in { background:rgba(37,196,105,.15); color:var(--good); }
.badge.out { background:rgba(238,79,79,.15); color:var(--bad); }
.gallery { display:flex; gap:8px; overflow-x:auto; padding:14px 0; }
.gallery img { width:120px; height:120px; object-fit:contain; background:var(--panel-2); border:1px solid var(--border); border-radius:6px; }
.pcontent.grid2 { display:grid; grid-template-columns: 1fr 1fr; gap:16px; }
.pcontent section { background:var(--panel-2); border:1px solid var(--border); border-radius:8px; padding:12px 14px; }
.pcontent h4 { margin:0 0 8px; font-size:13px; color:var(--muted); text-transform:uppercase; letter-spacing:.05em; }
.varhead { font-size:14px; margin:20px 0 10px; color:var(--muted); text-transform:uppercase; letter-spacing:.08em; }
.variants { display:flex; flex-direction:column; gap:10px; }
.variant { display:grid; grid-template-columns: 100px 1fr; gap:14px; background:var(--panel-2); border:1px solid var(--border); border-radius:8px; padding:12px; }
.vimg img { width:100px; height:100px; object-fit:contain; background:var(--bg); border-radius:6px; }
.vimg .noimg { width:100px; height:100px; display:flex; align-items:center; justify-content:center; color:var(--muted); font-size:11px; border:1px dashed var(--border); border-radius:6px; }
.vhead { display:flex; gap:10px; align-items:center; justify-content:space-between; }
.vtitle { font-weight:600; font-size:13px; }
.vmeta { display:flex; gap:6px; flex-wrap:wrap; margin:6px 0; }
.vprice { display:flex; gap:14px; align-items:baseline; flex-wrap:wrap; font-size:12px; }
.price { color:var(--accent); font-weight:700; font-size:15px; }
.muted { color:var(--muted); }
.vdet { margin-top:8px; }
.vdet summary { cursor:pointer; color:var(--muted); font-size:12px; padding:4px 0; }
.vdet[open] summary { color:var(--accent); }
.grid2 { display:grid; grid-template-columns:1fr 1fr; gap:10px; margin-top:10px; }
.grid2 section { background:var(--bg); border:1px solid var(--border); border-radius:6px; padding:10px 12px; font-size:12px; }
.grid2 h5 { margin:0 0 6px; font-size:11px; color:var(--muted); text-transform:uppercase; letter-spacing:.06em; }
.md-tbl { width:100%; border-collapse:collapse; font-size:12px; margin:6px 0; }
.md-tbl th, .md-tbl td { border:1px solid var(--border); padding:4px 8px; text-align:left; }
.md-tbl th { background:var(--panel); }
.vurl { margin-top:8px; }
.vurl a { color:var(--accent); font-size:12px; text-decoration:none; }
p { margin:6px 0; }
code { background:var(--chip); padding:1px 5px; border-radius:3px; font-size:11px; }
"""

    html_doc = f"""<!doctype html>
<html lang="vi">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(page_title)}</title>
<style>{css}</style>
</head>
<body>
<header class="page">
  <h1>{html.escape(page_title)}</h1>
  <div class="stats">
    <span><strong>Total scraped:</strong> {total_files} products</span>
    <span><strong>Showing:</strong> {len(products)}</span>
    <span><strong>Variants:</strong> {total_variants}</span>
    <span><strong>All-OOS products:</strong> {all_oos}</span>
    <span><strong>Brands in sample:</strong> {len(brands)}</span>
  </div>
</header>
<div class="layout">
  <nav class="toc">
    <h3>Sản phẩm ({len(products)})</h3>
    <ul>{''.join(toc_items)}</ul>
  </nav>
  <main>
    {''.join(body_items)}
  </main>
</div>
</body>
</html>
"""

    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as fh:
        fh.write(html_doc)
    size = os.path.getsize(args.output)
    print(f"wrote {args.output} ({size/1024:.1f} KB, {len(products)} products)")


if __name__ == "__main__":
    main()
