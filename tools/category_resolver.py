"""Resolve Chewy-style 3-tier category for each product.

Priority:
  1. Apollo `Product.breadcrumbs` from cached next_data (output/cache/) —
     authoritative (Chewy's own classification).
  2. Title-regex fallback using ProductCategory + title keywords +
     product_facts (food_form, package_type, life_stage).

Output for each product: list[str] like
    ["Dog Supplies", "Dog Food", "Dry Dog Food"]

The first segment is always present, the deeper levels are best-effort.

Usage as a module:
    from tools.category_resolver import resolve_category
    path = resolve_category(normalized_dict, grouped_product_dict)

Usage as a CLI (for inspection):
    python tools/category_resolver.py [--limit 50] [--show-source]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
from collections import Counter
from typing import Any

CACHE_DIR = "output/cache"
NORMALIZED_DIR = "output/normalized_products"

# -- Breadcrumb extraction from cached next_data -----------------------------

_BREADCRUMB_INDEX: dict[str, list[str]] | None = None


def _build_breadcrumb_index() -> dict[str, list[str]]:
    """Map source_pid -> breadcrumb chain (list of name strings)."""
    idx: dict[str, list[str]] = {}
    if not os.path.isdir(CACHE_DIR):
        return idx
    pattern = re.compile(r"^(?P<pid>\d+)_chewy-pdp-ui-")
    for f in os.listdir(CACHE_DIR):
        m = pattern.match(f)
        if not m:
            continue
        pid = m.group("pid")
        try:
            d = json.load(open(os.path.join(CACHE_DIR, f), "r", encoding="utf-8"))
        except Exception:
            continue
        apollo = ((d.get("pageProps") or {}).get("__APOLLO_STATE__")) or {}
        for k, v in apollo.items():
            if not (k.startswith("Product:") and isinstance(v, dict)):
                continue
            bc = v.get("breadcrumbs")
            if isinstance(bc, list) and bc:
                names = [b.get("name") for b in bc if isinstance(b, dict) and b.get("name")]
                if names:
                    idx[pid] = names
                    break
    return idx


def breadcrumb_for(pid: str) -> list[str] | None:
    global _BREADCRUMB_INDEX
    if _BREADCRUMB_INDEX is None:
        _BREADCRUMB_INDEX = _build_breadcrumb_index()
    return _BREADCRUMB_INDEX.get(str(pid))


# -- Title regex rules --------------------------------------------------------
# Each rule: (regex, top, mid, leaf). Order matters — first match wins.
# top is always 'Dog Supplies' since dataset is 100% Dog.

FOOD_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bfreeze[\s\-]?dried\b", re.I), "Freeze-Dried Food"),
    (re.compile(r"\bair[\s\-]?dried\b", re.I), "Air-Dried Food"),
    (re.compile(r"\bfresh\s+refrigerated\b|\bfresh\s+dog\s+food\b|\brefrigerated\b", re.I), "Fresh Dog Food"),
    (re.compile(r"\bveterinary\s+diet\b|\bvet\s+diet\b|\bprescription\s+diet\b|\brx\s+diet\b", re.I), "Veterinary Diet"),
    (re.compile(r"\bfood\s+topper\b|\btopper\b", re.I), "Food Topper"),
    (re.compile(r"\bcanned\b|\bwet\s+dog\s+food\b|\bin\s+gravy\b|\bin\s+gel\b|\bpate\b|\bstew\b|\bentree\b|\b\d+\.?\d*[\-\s]?oz\s+can\b", re.I), "Wet Dog Food"),
    (re.compile(r"\bdry\s+dog\s+food\b|\bkibble\b|\b\d+[\-\s]?lb\s+bag\b", re.I), "Dry Dog Food"),
    (re.compile(r"\braw\s+(dog\s+)?food\b|\bfrozen\s+raw\b|\bfrozen\s+dog\s+food\b", re.I), "Raw Dog Food"),
    (re.compile(r"\bvariety\s+pack\b", re.I), "Variety Pack"),
]

TREAT_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bdental\s+(treat|chew|stick|bone)\b|\bdental\s+treats\b", re.I), "Dental Treats"),
    (re.compile(r"\btraining\s+treat\b", re.I), "Training Treats"),
    (re.compile(r"\bjerky\b", re.I), "Jerky Treats"),
    (re.compile(r"\bbiscuit\b|\bcookie\b", re.I), "Biscuits & Cookies"),
    (re.compile(r"\brawhide\b", re.I), "Rawhide & Chews"),
    (re.compile(r"\bsoft\s+(&\s+)?chew", re.I), "Soft & Chewy Treats"),
    (re.compile(r"\bbully\s+stick\b", re.I), "Bully Sticks"),
]

SUPPLEMENT_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bhip\s+(&|and)\s+joint\b|\bmobility\b|\bglucosamine\b", re.I), "Joint Supplements"),
    (re.compile(r"\bskin\s+(&|and)\s+coat\b", re.I), "Skin & Coat Supplements"),
    (re.compile(r"\bdigest|\bprobiotic|\bgut\b", re.I), "Digestive Supplements"),
    (re.compile(r"\bcalming\b|\banxiet|\bstress\b", re.I), "Calming Supplements"),
    (re.compile(r"\bmultivitamin|\bvitamin\b", re.I), "Multivitamins"),
    (re.compile(r"\bimmun(e|ity)\b", re.I), "Immune Support"),
    (re.compile(r"\bdental\s+water|\boral\s+rinse\b|\boral\s+care\b", re.I), "Dental Care"),
    (re.compile(r"\bear\s+(cleaner|drops|wash)\b", re.I), "Ear Care"),
    (re.compile(r"\bheart\b|\bcardiac\b", re.I), "Heart Health"),
    (re.compile(r"\bthyroid\b|\bhormone\b", re.I), "Thyroid & Hormone"),
]

TOY_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bplush\b|\bsqueaky\b", re.I), "Plush Toys"),
    (re.compile(r"\bball\b|\bfetch\b", re.I), "Fetch Toys"),
    (re.compile(r"\bchew\s+toy\b|\bnylabone\b|\bkong\b", re.I), "Chew Toys"),
    (re.compile(r"\bpuzzle\b", re.I), "Puzzle Toys"),
]

GROOMING_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bshampoo\b", re.I), "Shampoo & Conditioner"),
    (re.compile(r"\bbrush\b|\bcomb\b", re.I), "Brushes & Combs"),
    (re.compile(r"\bnail\s+(clipper|grinder|trimmer)\b", re.I), "Nail Care"),
    (re.compile(r"\bwipes\b", re.I), "Wipes"),
]


def _apply_rules(rules: list[tuple[re.Pattern[str], str]], title: str) -> str | None:
    for rx, leaf in rules:
        if rx.search(title):
            return leaf
    return None


def _is_bundle(title: str) -> bool:
    return bool(
        re.search(r"\+\s+\d+\s+items?\b", title)
        or re.search(r"\s&\s.*[A-Z][a-z]+.*\s&\s", title)
    )


def title_regex_category(
    title: str,
    product_category: str | None,
    product_facts: dict | None = None,
) -> list[str]:
    """Title-based category fallback. Returns 2-3 segment list."""
    t = title or ""
    pc = (product_category or "").strip()
    pf = product_facts or {}

    # ProductCategory drives top mid-tier.
    if pc == "Treats":
        leaf = _apply_rules(TREAT_RULES, t) or "Dog Treats"
        return ["Dog Supplies", "Dog Treats", leaf] if leaf != "Dog Treats" else ["Dog Supplies", "Dog Treats"]

    if pc in ("Health & Wellness", "Health and Wellness", "Pharmacy"):
        leaf = _apply_rules(SUPPLEMENT_RULES, t) or "Dog Supplements"
        return ["Dog Supplies", "Dog Supplements", leaf] if leaf != "Dog Supplements" else ["Dog Supplies", "Dog Supplements"]

    if pc == "Toys":
        leaf = _apply_rules(TOY_RULES, t) or "Dog Toys"
        return ["Dog Supplies", "Dog Toys", leaf] if leaf != "Dog Toys" else ["Dog Supplies", "Dog Toys"]

    if pc == "Grooming":
        leaf = _apply_rules(GROOMING_RULES, t) or "Dog Grooming"
        return ["Dog Supplies", "Dog Grooming", leaf] if leaf != "Dog Grooming" else ["Dog Supplies", "Dog Grooming"]

    # Default to Food (covers 'Food' + 'Unknown' which look like food bundles)
    leaf = _apply_rules(FOOD_RULES, t)
    if not leaf:
        # Use product_facts hints
        ff = (pf.get("food_form") or "").strip().lower()
        if "treat" in ff:
            return ["Dog Supplies", "Dog Treats"]
        if "dry" in ff:
            leaf = "Dry Dog Food"
        elif "wet" in ff:
            leaf = "Wet Dog Food"
        elif "raw" in ff:
            leaf = "Raw Dog Food"
        else:
            # Package_type fallback
            pkg = (pf.get("package_type") or "").strip().lower()
            if pkg == "can":
                leaf = "Wet Dog Food"
            elif pkg == "bag":
                leaf = "Dry Dog Food"
    if not leaf:
        leaf = "Dog Food"
    return ["Dog Supplies", "Dog Food", leaf] if leaf != "Dog Food" else ["Dog Supplies", "Dog Food"]


# -- Public API ---------------------------------------------------------------
def resolve_category(
    normalized: dict, grouped_product: dict | None = None
) -> dict:
    """Resolve category for a product.

    Returns dict:
        {
          'path': [...],         # 2-3 segment list
          'source': 'normalized_category_path'|'breadcrumb'|'title_regex',
          'is_bundle': bool,     # heuristic for bundle products
        }
    """
    pid = str(normalized.get("source_product_id") or "")
    title = (
        (grouped_product or {}).get("title") or normalized.get("title") or ""
    )
    bundle = _is_bundle(title)

    # Highest priority: category_path already in normalized file (post-rescrape
    # with the breadcrumb fix). Already authoritative; no need to look further.
    cp = normalized.get("category_path") or []
    if cp:
        return {"path": list(cp), "source": "normalized_category_path", "is_bundle": bundle}

    bc = breadcrumb_for(pid) if pid else None
    if bc:
        return {"path": bc, "source": "breadcrumb", "is_bundle": bundle}

    pat = normalized.get("product_attributes_table") or {}
    pcs = pat.get("ProductCategory") or []
    pc = pcs[0] if pcs else None
    pf = (grouped_product or {}).get("product_facts") or {}
    path = title_regex_category(title, pc, pf)
    return {"path": path, "source": "title_regex", "is_bundle": bundle}


# -- CLI inspection -----------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--show-source", action="store_true")
    ap.add_argument("--input", default=NORMALIZED_DIR)
    args = ap.parse_args()

    files = sorted(glob.glob(os.path.join(args.input, "chewy_*.json")))
    # Warm breadcrumb cache
    breadcrumb_for("warmup")
    bc_idx = _BREADCRUMB_INDEX or {}
    print(f"Breadcrumb cache: {len(bc_idx)} pids")
    print()

    source_count = Counter()
    leaf_count = Counter()
    sample_lines: list[str] = []
    for f in files:
        try:
            d = json.load(open(f, "r", encoding="utf-8"))
        except Exception:
            continue
        res = resolve_category(d, None)
        source_count[res["source"]] += 1
        leaf_count[" > ".join(res["path"])] += 1
        if len(sample_lines) < args.limit:
            tag = f"[{res['source']}]" if args.show_source else ""
            bundle = " (bundle)" if res["is_bundle"] else ""
            sample_lines.append(
                f"  {tag:18} {' > '.join(res['path']):60s}  {(d.get('title') or '')[:60]}{bundle}"
            )

    print(f"Source distribution: {dict(source_count)}")
    print()
    print(f"Top 20 resolved category paths:")
    for k, c in leaf_count.most_common(20):
        print(f"  {c:5d}  {k}")
    print()
    print(f"--- Sample ({len(sample_lines)}) ---")
    for line in sample_lines:
        print(line)


if __name__ == "__main__":
    main()
