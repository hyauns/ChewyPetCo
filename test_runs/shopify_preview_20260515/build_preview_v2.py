"""
Build a Shopify-style preview HTML for the dry_run_v2 output (product 101610
processed by the NEW pipeline: entryID URLs + per-variant fetch +
TRANSITION_INSTRUCTIONS + split by Breed Size).

Output: index_v2.html in this folder.
"""
import html
import json
import re
from pathlib import Path

HERE = Path(__file__).parent
OUT_DIR = HERE / "dry_run_v2_output"
OUT = HERE / "index_v2.html"


def md_to_html(text: str) -> str:
    if not text:
        return ""
    text = text.strip()
    lines = text.split("\n")
    out_lines = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if "|" in line and i + 1 < len(lines) and re.match(r"^\s*\|?\s*[-:|\s]+\|?\s*$", lines[i + 1]):
            header = [c.strip() for c in line.strip().strip("|").split("|")]
            t = ['<table class="md-table"><thead><tr>']
            for c in header:
                t.append(f"<th>{html.escape(c)}</th>")
            t.append("</tr></thead><tbody>")
            i += 2
            while i < len(lines) and "|" in lines[i]:
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                t.append("<tr>")
                for c in cells:
                    t.append(f"<td>{html.escape(c)}</td>")
                t.append("</tr>")
                i += 1
            t.append("</tbody></table>")
            out_lines.append("".join(t))
            continue
        out_lines.append(html.escape(line))
        i += 1
    return "<br>".join(out_lines).replace("<br><table", "<table").replace("</table><br>", "</table>")


def render_variant_data(v):
    return {
        "entry_id": v.get("source_entry_id"),
        "part_number": v.get("source_variant_id"),
        "size": v.get("option1_value"),
        "price": v.get("price"),
        "autoship": v.get("autoship_price"),
        "in_stock": v.get("in_stock"),
        "out_of_stock": v.get("out_of_stock"),
        "stock_reason": v.get("stock_reason"),
        "shopify_inventory_policy": v.get("shopify_inventory_policy"),
        "availability": v.get("availability"),
        "description": v.get("description") or "",
        "ingredients": v.get("ingredients") or "",
        "guaranteed_analysis": v.get("guaranteed_analysis") or "",
        "feeding_instructions": v.get("feeding_instructions") or "",
        "transition_instructions": v.get("transition_instructions") or "",
        "calorie_content": v.get("calorie_content") or "",
        "images": v.get("images") or [],
        "variant_url": v.get("variant_url"),
        "content_source": (v.get("content_source") or {}).get("type", "none"),
        "upc": (v.get("identifiers") or {}).get("upc"),
        "gtin": (v.get("identifiers") or {}).get("gtin"),
    }


def render_product(p, idx, parent_pid=None):
    title = p.get("title", "")
    handle = p.get("handle_slug", "")
    disc = p.get("discriminator") or {}
    disc_label = " · ".join(f"{k}: {v}" for k, v in disc.items()) or "—"
    brand = p.get("brand", "")
    images = p.get("images") or []
    variants = p.get("variants") or []
    p_oos = p.get("out_of_stock")
    p_stock_state = p.get("stock_state") or "?"

    card_id = f"card-{parent_pid}-{idx}" if parent_pid else f"card-{idx}"
    variants_js = [render_variant_data(v) for v in variants]
    variants_json = html.escape(json.dumps(variants_js, ensure_ascii=False))

    parent = {
        "description": p.get("description") or "",
        "ingredients": p.get("ingredients") or "",
        "guaranteed_analysis": p.get("guaranteed_analysis") or "",
        "feeding_instructions": p.get("feeding_instructions") or "",
        "transition_instructions": p.get("transition_instructions") or "",
        "images": images,
    }
    parent_json = html.escape(json.dumps(parent, ensure_ascii=False))

    size_options = "".join(
        f'<option value="{v["entry_id"]}">{html.escape(v["size"] or "Default")} '
        f'— {html.escape(v["price"] or "no price")} '
        f'{"⛔OOS" if v["out_of_stock"] else "✓"} '
        f'(SKU {v["part_number"]})</option>'
        for v in variants_js
    )

    main_img = ""
    thumbs = ""
    if images:
        main_img = f'<img id="img-{card_id}" class="main-img" src="{html.escape(images[0])}" alt="">'
        thumbs = "".join(
            f'<img class="thumb" src="{html.escape(i)}" onclick="document.getElementById(\'img-{card_id}\').src=this.src">'
            for i in images[:8]
        )
    else:
        main_img = '<div class="no-img">Chưa có ảnh</div>'

    # Specifications table
    specs_section = p.get("content_sections", {}).get("specifications", {})
    spec_rows = ""
    for grp in (specs_section.get("groups") or []):
        for item in (grp.get("items") or []):
            spec_rows += f"<tr><td>{html.escape(item.get('label',''))}</td><td>{html.escape(str(item.get('value','')))}</td></tr>"
    specs_html = f'<table class="spec-table"><tbody>{spec_rows}</tbody></table>' if spec_rows else '<i class="empty">(Chưa có specifications)</i>'

    oos_badge = (
        f'<span class="oos-badge">OUT OF STOCK</span>' if p_oos
        else f'<span class="in-stock-badge">IN STOCK</span>' if p_stock_state == 'in_stock'
        else f'<span class="unknown-badge">stock unknown</span>'
    )

    return f"""
<section class="card {'oos-card' if p_oos else ''}" id="{card_id}" data-variants="{variants_json}" data-parent="{parent_json}">
  <header class="card-head">
    <h2>{html.escape(title)} {oos_badge}</h2>
    <div class="card-meta">
      <span class="brand">{html.escape(brand)}</span>
      <span class="disc">Discriminator: <b>{html.escape(disc_label)}</b></span>
      <span class="handle">handle: <code>{html.escape(handle)}</code></span>
      <span class="gid">group_id: <code>{html.escape(p.get('source_group_id',''))}</code></span>
      <span class="stock-state">stock_state: <code>{html.escape(p_stock_state)}</code></span>
    </div>
  </header>
  <div class="card-body">
    <div class="gallery">
      {main_img}
      <div class="thumbs">{thumbs}</div>
    </div>
    <div class="info">
      <div class="row">
        <label>Size:</label>
        <select onchange="updateVariant('{card_id}', this.value)">
          {size_options}
        </select>
      </div>
      <div class="price-row">
        <div class="price" id="price-{card_id}"></div>
        <div class="autoship" id="autoship-{card_id}"></div>
      </div>
      <div class="ids">
        <span>Stock: <code id="stock-{card_id}"></code></span>
        <span>SKU: <code id="sku-{card_id}"></code></span>
        <span>UPC: <code id="upc-{card_id}"></code></span>
        <span>entryID: <code id="eid-{card_id}"></code></span>
        <span>content_source: <code id="csrc-{card_id}"></code></span>
        <span>variant_url: <a id="vurl-{card_id}" target="_blank"></a></span>
      </div>
      <details open><summary>Description</summary><div class="ac-body" id="desc-{card_id}"></div></details>
      <details><summary>Ingredients</summary><div class="ac-body" id="ingr-{card_id}"></div></details>
      <details><summary>Guaranteed Analysis</summary><div class="ac-body" id="ga-{card_id}"></div></details>
      <details><summary>Feeding Instructions</summary><div class="ac-body" id="fi-{card_id}"></div></details>
      <details><summary>Transition Instructions</summary><div class="ac-body" id="ti-{card_id}"></div></details>
      <details><summary>Specifications</summary><div class="ac-body">{specs_html}</div></details>
    </div>
  </div>
</section>
"""


def build_entry_section(pid, grouped):
    products = grouped.get("products", [])
    stats = grouped.get("enrichment_stats") or {}
    val = grouped.get("validation") or {}
    all_dropped = grouped.get("_all_dropped_as_duplicates")

    sections = "\n".join(render_product(p, i, parent_pid=pid) for i, p in enumerate(products))
    if all_dropped and not sections:
        sections = (
            '<div class="all-dropped">Tất cả sản phẩm scrape từ trang này đã bị '
            'dedupe (trùng với trang khác). Không có sản phẩm Shopify nào được giữ '
            'cho source page này — xem dedupe log ở phần Summary.</div>'
        )

    src_url = grouped.get("source_url", "")
    meta_items = [
        f"source_url: <a target='_blank' href='{html.escape(src_url)}'>{html.escape(src_url)}</a>",
        f"build_id: <code>{html.escape(grouped.get('build_id') or '')}</code>",
        f"is_multi_product: <b>{grouped.get('is_multi_product')}</b>",
        f"Shopify products kept (post-dedupe): <b>{len(products)}</b> · variants: <b>{sum(len(p.get('variants',[])) for p in products)}</b>",
    ]
    if stats:
        f = stats.get("fields_filled", {})
        meta_items.append(
            f"API: enriched={stats.get('enriched')}, failed={stats.get('failed')}, "
            f"wrong_product={stats.get('wrong_product_api_rejected')}, slug_mismatch={stats.get('slug_mismatch')}"
        )
        meta_items.append(
            f"Filled per-variant: feeding={f.get('feeding_instructions',0)}, "
            f"transition={f.get('transition_instructions',0)}, ingredients={f.get('ingredients',0)}, "
            f"GA={f.get('guaranteed_analysis',0)}, description={f.get('description',0)}, images={f.get('images',0)}"
        )

    meta_html = "<ul>" + "".join(f"<li>{x}</li>" for x in meta_items) + "</ul>"
    return f"""
<section class="entry">
  <h1 class="entry-title">#{html.escape(pid)} — {html.escape(grouped.get('source_product_id') or '')}</h1>
  <div class="entry-meta">{meta_html}</div>
  <div class="cards">{sections}</div>
</section>
"""


def build_html(grouped_list, dedup_info=None):
    """grouped_list: list of (pid, grouped_dict) tuples."""
    total_products = sum(len(g.get('products',[])) for _, g in grouped_list)
    total_variants = sum(sum(len(p.get('variants',[])) for p in g.get('products',[]))
                         for _, g in grouped_list)
    total_oos_variants = sum(
        sum(1 for p in g.get('products',[]) for v in p.get('variants',[]) if v.get('out_of_stock'))
        for _, g in grouped_list
    )
    total_oos_products = sum(
        sum(1 for p in g.get('products',[]) if p.get('out_of_stock'))
        for _, g in grouped_list
    )
    total_enriched = sum(g.get('enrichment_stats',{}).get('enriched',0) for _, g in grouped_list)
    total_wrong = sum(g.get('enrichment_stats',{}).get('wrong_product_api_rejected',0) for _, g in grouped_list)
    total_slugm = sum(g.get('enrichment_stats',{}).get('slug_mismatch',0) for _, g in grouped_list)

    dedup_lines = ""
    if dedup_info:
        dedup_lines = (
            f"<li>Cross-page dedupe: <b>{dedup_info['total_candidates']}</b> candidates → "
            f"<b style='color:#166c2e;'>{dedup_info['unique_products']}</b> unique products, "
            f"<b style='color:#a86b00;'>{len(dedup_info['duplicates_log'])}</b> duplicate-sets collapsed</li>"
        )
        if dedup_info.get("duplicates_log"):
            dup_items = "".join(
                f"<li><code>{html.escape(str(e['kept_from_source_pid']))}</code> kept · "
                f"dropped from <code>{html.escape(', '.join(str(x) for x in e['dropped_from_source_pids']))}</code> · "
                f"{html.escape((e['product_title'] or '')[:70])}</li>"
                for e in dedup_info["duplicates_log"]
            )
            dedup_lines += f"<li>Duplicates detail:<ul class='dedup-list'>{dup_items}</ul></li>"

    summary_html = f"""
<ul>
  <li>Source product pages processed: <b>{len(grouped_list)}</b></li>
  <li>Shopify products produced (post-dedupe): <b>{total_products}</b></li>
  <li>Total variants: <b>{total_variants}</b></li>
  <li>Variants OUT OF STOCK: <b style="color:#a02020;">{total_oos_variants}</b></li>
  <li>Products fully OUT OF STOCK: <b style="color:#a02020;">{total_oos_products}</b></li>
  <li>API fetches: enriched=<b>{total_enriched}</b>, wrong_product_rejected=<b>{total_wrong}</b>, slug_mismatch=<b>{total_slugm}</b></li>
  {dedup_lines}
</ul>
"""

    sections = "\n".join(build_entry_section(pid, g) for pid, g in grouped_list)

    return f"""<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="UTF-8">
<title>Dry-run v2 — 11 products (new pipeline)</title>
<style>
* {{ box-sizing: border-box; }}
body {{ font-family: -apple-system, "Segoe UI", sans-serif; margin: 0; background: #f4f5f7; color: #222; }}
header.top {{ background: #1a1a2e; color: #fff; padding: 24px 32px; }}
header.top h1 {{ margin: 0; font-size: 22px; }}
header.top .sub {{ color: #aab; font-size: 13px; margin-top: 4px; }}
main {{ padding: 24px 32px; max-width: 1400px; margin: 0 auto; }}
.summary {{ background: #fff; border-radius: 8px; padding: 16px 24px; margin-bottom: 24px; box-shadow: 0 1px 3px rgba(0,0,0,.06); }}
.summary ul {{ margin: 0; padding-left: 18px; font-size: 14px; line-height: 1.7; }}
.summary li code {{ background: #f0f1f5; padding: 1px 6px; border-radius: 3px; }}
.entry {{ background: #fff; border-radius: 10px; padding: 18px 24px; margin-bottom: 36px; box-shadow: 0 1px 5px rgba(0,0,0,.08); }}
.entry-title {{ font-size: 18px; margin: 0 0 8px; color: #1a1a2e; }}
.entry-meta ul {{ list-style: none; padding: 0; margin: 0 0 14px; font-size: 13px; }}
.entry-meta li {{ color: #333; padding: 1px 0; }}
.entry-meta code {{ background: #f0f1f5; padding: 1px 6px; border-radius: 3px; }}
.dedup-list {{ margin: 4px 0 0 0; padding-left: 18px; font-size: 12px; }}
.dedup-list li {{ color: #555; }}
.all-dropped {{ background: #fff8e6; border-left: 4px solid #d6a200; padding: 12px 16px; border-radius: 4px; color: #6a4f00; font-size: 13px; }}
.cards {{ display: grid; gap: 14px; }}
.card {{ background: #fff; border: 1px solid #e0e0e6; border-radius: 8px; overflow: hidden; }}
.card.oos-card {{ background: #fff8f8; border-color: #f5b5b5; }}
.card-head {{ padding: 14px 18px; background: #fafbfc; border-bottom: 1px solid #e0e0e6; }}
.oos-card .card-head {{ background: #fdecec; }}
.oos-badge {{ background: #ffd6d6; color: #a02020; padding: 2px 8px; border-radius: 10px; font-size: 11px; margin-left: 8px; vertical-align: middle; font-weight: 600; }}
.in-stock-badge {{ background: #d1f0db; color: #166c2e; padding: 2px 8px; border-radius: 10px; font-size: 11px; margin-left: 8px; vertical-align: middle; font-weight: 600; }}
.unknown-badge {{ background: #eee; color: #888; padding: 2px 8px; border-radius: 10px; font-size: 11px; margin-left: 8px; vertical-align: middle; }}
.stock-state code {{ font-size: 11px; }}
.card-head h2 {{ margin: 0 0 6px; font-size: 18px; }}
.card-meta {{ display: flex; flex-wrap: wrap; gap: 14px; font-size: 13px; }}
.card-meta .disc b {{ color: #2c5fb5; }}
.card-meta code {{ background: #f0f1f5; padding: 1px 6px; border-radius: 3px; }}
.card-body {{ display: grid; grid-template-columns: 380px 1fr; gap: 28px; padding: 20px; }}
.gallery .main-img {{ width: 100%; max-width: 380px; height: 380px; object-fit: contain; background: #fff; border: 1px solid #eee; border-radius: 6px; }}
.gallery .no-img {{ width: 100%; height: 380px; background: #f0f0f0; display:flex; align-items:center; justify-content:center; color:#999; border-radius:6px; }}
.thumbs {{ display: flex; gap: 6px; margin-top: 8px; flex-wrap: wrap; }}
.thumb {{ width: 52px; height: 52px; object-fit: cover; border: 1px solid #ddd; border-radius: 4px; cursor: pointer; }}
.thumb:hover {{ border-color: #2c5fb5; }}
.info .row {{ margin-bottom: 12px; }}
.info label {{ font-weight: 600; margin-right: 8px; }}
.info select {{ padding: 6px 10px; min-width: 320px; font-size: 14px; }}
.price-row {{ display: flex; gap: 16px; align-items: baseline; margin-bottom: 10px; }}
.price {{ font-size: 26px; font-weight: 700; }}
.autoship {{ color: #1f8a4e; font-size: 13px; }}
.ids {{ display: flex; gap: 14px; flex-wrap: wrap; font-size: 12px; color: #555; margin-bottom: 14px; }}
.ids code {{ background: #f0f1f5; padding: 1px 6px; border-radius: 3px; }}
.ids a {{ color: #2c5fb5; }}
details {{ border-top: 1px solid #eee; padding: 8px 0; }}
details summary {{ cursor: pointer; font-weight: 600; padding: 4px 0; }}
.ac-body {{ font-size: 13px; line-height: 1.55; padding: 8px 0 4px; max-height: 420px; overflow-y: auto; }}
.empty {{ color: #b00; font-style: italic; }}
.md-table {{ border-collapse: collapse; margin: 6px 0; font-size: 12px; }}
.md-table th, .md-table td {{ border: 1px solid #ddd; padding: 4px 8px; text-align: left; }}
.md-table th {{ background: #fafbfc; }}
.spec-table {{ border-collapse: collapse; font-size: 13px; }}
.spec-table td {{ border: 1px solid #ddd; padding: 4px 10px; }}
.spec-table td:first-child {{ font-weight: 600; background: #fafbfc; }}
</style>
</head>
<body>
<header class="top">
  <h1>Dry-run v2 — 101610 (new pipeline)</h1>
  <div class="sub">entryID-aware URLs · Per-variant API fetch · TRANSITION_INSTRUCTIONS · Split by Breed Size</div>
</header>
<main>
  <div class="summary">{summary_html}</div>
  {sections}
</main>
<script>
function escapeHtml(s) {{
  if (s == null) return '';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}}
function mdToHtml(text) {{
  if (!text) return '';
  text = text.trim();
  const lines = text.split('\\n');
  const out = [];
  let i = 0;
  while (i < lines.length) {{
    const line = lines[i];
    if (line.indexOf('|') !== -1 && i + 1 < lines.length && /^\\s*\\|?\\s*[-:|\\s]+\\|?\\s*$/.test(lines[i+1])) {{
      const headerCells = line.trim().replace(/^\\||\\|$/g,'').split('|').map(c => c.trim());
      let t = '<table class="md-table"><thead><tr>';
      headerCells.forEach(c => t += '<th>' + escapeHtml(c) + '</th>');
      t += '</tr></thead><tbody>';
      i += 2;
      while (i < lines.length && lines[i].indexOf('|') !== -1) {{
        const cells = lines[i].trim().replace(/^\\||\\|$/g,'').split('|').map(c => c.trim());
        t += '<tr>';
        cells.forEach(c => t += '<td>' + escapeHtml(c) + '</td>');
        t += '</tr>';
        i++;
      }}
      t += '</tbody></table>';
      out.push(t);
      continue;
    }}
    out.push(escapeHtml(line));
    i++;
  }}
  return out.join('<br>').replace(/<br><table/g,'<table').replace(/<\\/table><br>/g,'</table>');
}}

function setField(id, text, label) {{
  const el = document.getElementById(id);
  if (!el) return;
  if (!text) {{
    el.innerHTML = '<i class="empty">Chưa có ' + label + '</i>';
  }} else {{
    el.innerHTML = mdToHtml(text);
  }}
}}

function updateVariant(cardId, entryId) {{
  const card = document.getElementById(cardId);
  if (!card) return;
  const variants = JSON.parse(card.dataset.variants);
  const parent = JSON.parse(card.dataset.parent);
  const v = variants.find(x => String(x.entry_id) === String(entryId));
  if (!v) return;
  document.getElementById('price-' + cardId).textContent = v.price || 'Chưa có giá';
  document.getElementById('autoship-' + cardId).textContent = v.autoship ? ('Autoship: ' + v.autoship) : '';
  const stockEl = document.getElementById('stock-' + cardId);
  if (v.out_of_stock === true) {{
    stockEl.textContent = 'OUT OF STOCK (' + (v.stock_reason || '?') + ')';
    stockEl.style.color = '#a02020';
    stockEl.style.fontWeight = '600';
  }} else if (v.out_of_stock === false) {{
    stockEl.textContent = 'in_stock (' + (v.stock_reason || '?') + ')';
    stockEl.style.color = '#166c2e';
    stockEl.style.fontWeight = '600';
  }} else {{
    stockEl.textContent = 'unknown';
    stockEl.style.color = '#888';
  }}
  document.getElementById('sku-' + cardId).textContent = v.part_number || '-';
  document.getElementById('upc-' + cardId).textContent = v.upc || '-';
  document.getElementById('eid-' + cardId).textContent = v.entry_id || '-';
  document.getElementById('csrc-' + cardId).textContent = v.content_source || 'none';
  const vurl = document.getElementById('vurl-' + cardId);
  if (vurl) {{
    vurl.href = v.variant_url || '#';
    vurl.textContent = v.variant_url || '(no url)';
  }}
  setField('desc-' + cardId, v.description || parent.description, 'description');
  setField('ingr-' + cardId, v.ingredients || parent.ingredients, 'ingredients');
  setField('ga-' + cardId, v.guaranteed_analysis || parent.guaranteed_analysis, 'guaranteed analysis');
  setField('fi-' + cardId, v.feeding_instructions || parent.feeding_instructions, 'feeding instructions');
  setField('ti-' + cardId, v.transition_instructions || parent.transition_instructions, 'transition instructions');
  if (v.images && v.images.length) {{
    const img = document.getElementById('img-' + cardId);
    if (img) img.src = v.images[0];
  }} else if (parent.images && parent.images.length) {{
    const img = document.getElementById('img-' + cardId);
    if (img) img.src = parent.images[0];
  }}
}}

document.addEventListener('DOMContentLoaded', () => {{
  document.querySelectorAll('.card').forEach(card => {{
    const sel = card.querySelector('select');
    if (sel) updateVariant(card.id, sel.value);
  }});
}});
</script>
</body>
</html>
"""


def main():
    files = sorted(OUT_DIR.glob("grouped_*.json"))
    if not files:
        print("No grouped_*.json files found")
        return
    raw_grouped_list = []
    for fp in files:
        pid = fp.stem.replace("grouped_", "")
        grouped = json.loads(fp.read_text(encoding="utf-8"))
        raw_grouped_list.append((pid, grouped))

    # Load deduped result if present; replace each source-page's products with
    # the kept ones whose canonical_source matches that page.
    dedup_path = OUT_DIR / "_deduped.json"
    if dedup_path.exists():
        dedup = json.loads(dedup_path.read_text(encoding="utf-8"))
        kept_by_source = {}
        for p in dedup["kept_products"]:
            src = (p.get("canonical_source") or {}).get("source_product_id")
            kept_by_source.setdefault(str(src), []).append(p)
        grouped_list = []
        for pid, grouped in raw_grouped_list:
            kept = kept_by_source.get(str(pid), [])
            if not kept:
                # All this page's products were dropped as duplicates
                grouped = dict(grouped)
                grouped["products"] = []
                grouped["_all_dropped_as_duplicates"] = True
            else:
                grouped = dict(grouped)
                grouped["products"] = kept
            grouped_list.append((pid, grouped))
        OUT.write_text(build_html(grouped_list, dedup), encoding="utf-8")
        print(f"Wrote {OUT}  ({len(raw_grouped_list)} source pages, "
              f"{dedup['unique_products']} deduped Shopify products, "
              f"{len(dedup['duplicates_log'])} duplicates collapsed)")
    else:
        OUT.write_text(build_html(raw_grouped_list, None), encoding="utf-8")
        print(f"Wrote {OUT} ({len(raw_grouped_list)} source product pages, "
              f"{sum(len(g.get('products',[])) for _, g in raw_grouped_list)} Shopify products) — no dedupe file")


if __name__ == "__main__":
    main()
