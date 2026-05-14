# Chewy Scraper — Project Context Report

> **Generated:** 2026-05-15  
> **Purpose:** Enable another AI or developer to continue this project safely.

---

## 1. Project Purpose

This is a **Chewy.com pet product scraper and enrichment pipeline**. The existing scraped dataset lives in `output/normalized_products/` (3,115 JSON files). The current phase is **data enrichment**, not scraping from scratch.

**Pipeline stages:**
1. **Scrape** product pages via AdsPower + Playwright (already done for ~3k products)
2. **Enrich** missing variant-specific content (ingredients, GA, description, images, prices) via Chewy's Next.js data API
3. **Split** multi-flavor products into one-product-per-flavor groups (flavor = product, size = variant)
4. **Sanitize** to prevent cross-flavor content contamination
5. **Validate** and generate import-ready status reports
6. *(Future)* Export to Shopify-ready format

---

## 2. Architecture

```
chewy_next_json_extractor.py    ← Core: parsing, enrichment, splitting, validation
    ├── extract_variant_info_from_apollo()  ← Extracts content from Apollo state
    ├── enrich_variants_from_api()          ← Per-variant API enrichment loop
    ├── split_product_by_flavor()           ← Flavor-as-product splitting
    ├── _parent_content_matches_flavor()    ← Primary protein validation
    ├── validate_normalized_product()       ← Quality/confidence scoring
    ├── parse_apollo_product()             ← Parse full product from Apollo
    ├── normalize_chewy_product()          ← Normalize to standard schema
    ├── build_next_data_url()              ← Build Next.js data API URL
    └── fetch_next_data_json()             ← Fetch + cache API responses

resumable_scraper_runner.py     ← Production scraper runner (single-profile)
parallel_resumable_runner.py    ← Multi-profile parallel scraper runner
config.py                       ← All configuration and thresholds
adspower.py                     ← AdsPower browser automation API
adsp_profile_pool_manager.py    ← Profile pool + white screen detection
adsp_profile_recovery_manager.py← Profile quarantine and recovery
job_store.py                    ← SQLite job queue management
job_exporter.py                 ← Export normalized → grouped products
```

### Data flow
```
Chewy HTML → __NEXT_DATA__ → Apollo/Redux state → parse → normalize
    → enrich variants via API → split by flavor → sanitize → validate
    → output/normalized_products/chewy_{id}.json
```

### API enrichment
The Chewy Next.js data API is accessed via:
```
https://www.chewy.com/_next/data/{buildId}/en-US/{slug}/dp/{variantId}.json
```
Responses are cached in `output/cache/{variantId}_{buildId}.json` (104 files, ~10MB).

---

## 3. Key Design Decisions

| Rule | Rationale |
|------|-----------|
| **Missing data is acceptable** | Blank fields are honest — they can be filled later |
| **Wrong data is never acceptable** | A Lamb product with Chicken ingredients destroys trust |
| **Never invent content** | No hallucinated ingredients, prices, images, or GTINs |
| **Parent content must not leak across flavors** | `split_product_by_flavor()` validates primary protein before applying parent content |
| **Wrong-product API responses must be rejected** | `partNumber` must match `source_variant_id` before accepting API data |
| **Content safety > completeness** | Better to ship with blank fields than wrong fields |

---

## 4. Important Fixes Completed

### Fix 1: Apollo infoGroups parsing (2026-05-14)
**File:** `chewy_next_json_extractor.py` → `extract_variant_info_from_apollo()`

**Problem:** Chewy's Apollo response uses nested structure:
```json
{
  "infoGroups": [{
    "sections": [{
      "usage": "INGREDIENTS",
      "content": {"__typename": "Markdown", "content": "Beef, Beef Broth..."}
    }]
  }]
}
```
The old parser expected flat `infoGroups[].usage` / `infoGroups[].content` (string).

**Fix:**
- Parse `infoGroups[].sections[].usage` + `infoGroups[].sections[].content.content`
- Handle `content` as both dict (`{"content": "..."}`) and string (legacy)
- Extract `description` directly from the `Item` node
- Image URLs: scan parameterized GraphQL keys like `url({"autoCrop":true,"square":600})`
- Kept legacy format fallback for backward compatibility

### Fix 2: Cross-flavor content contamination (2026-05-14)
**File:** `chewy_next_json_extractor.py` → `split_product_by_flavor()` + `_parent_content_matches_flavor()`

**Problem:** Parent product text (description, ingredients, GA, feeding instructions) was blindly copied to every flavor group. A Chicken product's ingredients appeared on Lamb, Duck, etc.

**Fix:**
- Added `_parent_content_matches_flavor()` — checks if primary protein (first keyword in text) matches the declared flavor
- Non-matching content is moved to `debug.rejected_content` with warning `parent_content_not_applicable_to_flavor`
- Public fields remain blank rather than contaminated
- Minor ingredients (e.g., "Chicken Fat" in Lamb) are tolerated — only the primary protein matters

---

## 5. Test Results Summary

Three controlled tests were run against 5 products each from `test_runs/chewy_backfill_sample_001/`.

### Category A — Variant API Content Enrichment
| Metric | Value |
|--------|-------|
| products_processed | 5 |
| variants_enriched_count | **21** |
| wrong_product_api_rejected | 28 |
| flavor_mismatch_count | **0** |
| public_content_unsafe_count | **0** |
| rejected_content_leaked_count | **0** |
| blocked_count | **0** |
| import_ready_count | 30 |
| exit_code | **0** |

### Category B — Missing Price Recovery
| Metric | Value |
|--------|-------|
| products_processed | 5 |
| variants_missing_price_before | 8 |
| variants_price_recovered | 0 (discontinued) |
| flavor_mismatch_count | **0** |
| public_content_unsafe_count | **0** |
| rejected_content_leaked_count | **0** |
| blocked_count | **0** |
| exit_code | **0** |

### Category C — Image Recovery
| Metric | Value |
|--------|-------|
| products_processed | 5 |
| variants_missing_image_before | 14 |
| variants_image_recovered | 0 |
| flavor_mismatch_count | **0** |
| public_content_unsafe_count | **0** |
| rejected_content_leaked_count | **0** |
| blocked_count | **0** |
| import_ready_count | 12 |
| exit_code | **0** |

---

## 6. Known Limitations

1. **Chewy API variant ID recycling** — `/dp/{id}` may resolve to a completely different product. The pipeline correctly rejects these via partNumber validation, but many variants cannot be enriched.
2. **Image recovery is ineffective** — The variant API rarely returns images for these products. Future: dedicated image rescrape queue.
3. **Some variants are permanently discontinued** — API returns 404/null for prices. These are marked `missing_price_unresolved`.
4. **Blank content after split** — If parent content doesn't match a flavor and no variant-specific content was recovered, the flavor group has empty text fields. This is correct behavior.
5. **`moe/` placeholder images** — Treated as missing. Products with only placeholder images get `needs_manual_review`.
6. **Cache invalidation** — `output/cache/` is keyed by `{variantId}_{buildId}`. If `buildId` changes, new cache files are created. Old cache entries for wrong products persist but are harmless (they're rejected by partNumber validation).

---

## 7. Recommended Next Steps

1. **Do not run full backfill yet** — project cleanup is done; next step is a 50-product controlled batch
2. **Keep `output/normalized_products/` untouched** — this is the source of truth
3. **Run 50-product controlled batch** with hard-fail criteria:
   - `public_content_unsafe_count > 0` → FAIL
   - `flavor_mismatch_count > 0` → FAIL
   - `rejected_content_leaked_count > 0` → FAIL
   - Wrong-product API response accepted → FAIL
4. **Consider merging A/B/C test runners** into `run_backfill_test.py --category A|B|C|all` (see §8)
5. **Build image rescrape queue** for products with only `moe/` placeholders
6. **Define price/export rules** for unpriced variants before Shopify import

---

## 8. File Map

### A. Essential Production Files
| File | Purpose |
|------|---------|
| `chewy_next_json_extractor.py` | Core parsing, enrichment, splitting, validation |
| `resumable_scraper_runner.py` | Production scraper runner |
| `parallel_resumable_runner.py` | Multi-profile parallel runner |
| `config.py` | All configuration |
| `adspower.py` | AdsPower browser API |
| `adsp_profile_pool_manager.py` | Profile pool + white screen detection |
| `adsp_profile_recovery_manager.py` | Profile quarantine/recovery |
| `job_store.py` | SQLite job queue |
| `job_exporter.py` | Export normalized → grouped |
| `category_discovery.py` | Category URL discovery |
| `category_discovery_validation.py` | Category validation |
| `category_job_runner.py` | Category-based job runner |
| `category_price_filter.py` | Price filtering rules |
| `app_ui.py` | Web UI for scraper management |
| `ui_file_browser.py` | UI file browser component |
| `ui_log_parser.py` | UI log parser component |
| `ui_runner.py` | UI runner component |
| `scraper_jobs.db` | Root job database — **DO NOT MODIFY** |
| `output/scraper_jobs.db` | Output job database — **DO NOT MODIFY** |
| `output/normalized_products/` | 3,115 product JSON files — **DO NOT MODIFY** |
| `output/cache/` | 104 cached API responses |
| `.env` | Environment variables |
| `.gitignore` | Git ignore rules |
| `requirements.txt` | Python dependencies |

### B. Test Files (Keep for Regression)
| File | Purpose |
|------|---------|
| `test_runs/chewy_backfill_sample_001/run_category_a_test.py` | Category A test runner |
| `test_runs/chewy_backfill_sample_001/run_category_b_test.py` | Category B test runner |
| `test_runs/chewy_backfill_sample_001/run_category_c_test.py` | Category C test runner |
| `test_runs/chewy_backfill_sample_001/selected_products.json` | Test product selection |
| `test_runs/chewy_backfill_sample_001/selection_report.md` | Selection criteria docs |
| `test_runs/chewy_backfill_sample_001/result_category_*.json` | Test results |
| `test_runs/chewy_backfill_sample_001/report_category_*.json` | Test reports |

### C. Useful but Potentially Redundant
| File | Notes |
|------|-------|
| `test_single_product.py` | Full single-product test with old HTML scraper fallback. Contains legacy `extract_product_detail()`. Still useful for manual testing. |
| `test_chewy_json_extractor_batch.py` | Batch testing for JSON extractor. May overlap with bulk_quality_audit. |
| `bulk_quality_audit.py` | Batch pipeline for auditing normalized products. Useful. |
| `audit_normalized_dataset.py` | Dataset-wide audit. Useful for generating audit_reports/. |
| `check_slots.py` | Quick diagnostic for CW slot status. Tiny, harmless. |
| `CHEWY_SCRAPER_USAGE_GUIDE_VI.md` | Vietnamese usage guide. Keep. |

### D. Archived Files
| Original Path | Archive Path | Reason |
|--------------|-------------|--------|
| `patch.py` | `_archive/old_patches/` | Old extraction fix, superseded by current extractor |
| `patch2.py` | `_archive/old_patches/` | Old `extract_variant_info_from_apollo` draft, superseded |
| `recover_functions.py` | `_archive/old_scripts/` | Empty file |
| `print_report.py` | `_archive/old_scripts/` | Tiny one-off report printer |
| `fetch_next_data.py` | `_archive/old_scripts/` | Tiny standalone fetch script |
| `test_variant_api.py` | `_archive/old_scripts/` | One-off API test, superseded by test runners |
| `test_category_a_enrichment.py` | `_archive/old_scripts/` | Early Category A test, superseded by `run_category_a_test.py` |
| `bulk_audit_20_products_full.json` | `_archive/old_test_outputs/` | 827KB old audit output |

### E. Candidates for Merge
The three test runners share ~120 lines of identical code:
- `FLAVOR_KEYWORDS` list
- `detect_flavor_mismatch()` function
- `has_real_images()` function
- Hard-fail safety checks
- Report aggregation logic

**Recommended merge:** Create `run_backfill_test.py --category A|B|C|all` that extracts shared helpers into a common module. This is not urgent but reduces maintenance burden.

### F. Files That Should NOT Be Touched
- `output/normalized_products/` — source of truth
- `scraper_jobs.db` (root) — production job database
- `output/scraper_jobs.db` — output job database
- `.env` — credentials
- `output/cache/` — valid API response cache

---

## 9. Duplicate Detection Results

| Function | Canonical Location | Duplicates Found |
|----------|-------------------|-----------------|
| `extract_variant_info_from_apollo` | `chewy_next_json_extractor.py:102` | `patch2.py` (archived), `test_single_product.py:402` (inline import) |
| `split_product_by_flavor` | `chewy_next_json_extractor.py` | None |
| `_parent_content_matches_flavor` | `chewy_next_json_extractor.py` | None |
| `detect_flavor_mismatch` | *(not in extractor)* | Duplicated in all 3 test runners |
| `FLAVOR_KEYWORDS` | *(not in extractor)* | Duplicated in all 3 test runners |
| `has_real_images` | *(not in extractor)* | Duplicated in all 3 test runners |

> [!WARNING]
> `detect_flavor_mismatch`, `FLAVOR_KEYWORDS`, and `has_real_images` are **only** in the test runners. If these are needed for production, they should be extracted to a shared module (e.g., `chewy_sanitizer.py`).

---

## 10. Readiness Assessment

| Check | Status |
|-------|--------|
| Core extractor functions verified | ✅ All 13 key functions present |
| Cross-flavor contamination fixed | ✅ split_product_by_flavor is safe |
| Apollo parsing fixed | ✅ Nested sections + Markdown content handled |
| Category A test passes | ✅ 21 enriched, 0 mismatch |
| Category B test passes | ✅ 0 mismatch, honest price handling |
| Category C test passes | ✅ 0 mismatch, honest image handling |
| Old clutter archived | ✅ 8 files moved to _archive/ |
| No production data modified | ✅ normalized_products untouched |

**Recommendation: Ready for 50-product controlled batch test.** ✅
