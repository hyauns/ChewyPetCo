# Chewy Scraper — Refactoring Report

> **Date:** 2026-05-15  
> **Status:** All regression tests pass ✅

---

## 1. New 3-Entry-Point Structure

| File | Purpose | Size |
|------|---------|------|
| **`category_scraper.py`** | Discover category URLs, extract product links, manage job queue | 8.7KB |
| **`chewy_product_scraper.py`** | Scrape individual product pages, extract Apollo/Redux data, save normalized JSON | 5.0KB |
| **`chewy_enrich.py`** | Enrich existing products: content, price, image recovery + sanitizer | 24KB |

### CLI Commands

```bash
# ── Category Discovery ──────────────────────────────────────────────
python category_scraper.py create --name "dry dog food" --category-url "https://www.chewy.com/b/dry-food-288"
python category_scraper.py start --category-job-id <JOB_ID>
python category_scraper.py create-pdp-job --category-job-id <JOB_ID>

# ── Product Scraping ────────────────────────────────────────────────
python chewy_product_scraper.py --url "https://www.chewy.com/.../dp/12345"
python chewy_product_scraper.py --job-id <JOB_ID> --limit 100

# ── Enrichment ──────────────────────────────────────────────────────
# Sample/test mode (regression tests)
python chewy_enrich.py --sample selected_products.json --category A
python chewy_enrich.py --sample selected_products.json --category B
python chewy_enrich.py --sample selected_products.json --category C

# Batch mode
python chewy_enrich.py --input output/normalized_products --mode content --limit 50
python chewy_enrich.py --input output/normalized_products --mode price --limit 50
python chewy_enrich.py --input output/normalized_products --mode image --limit 50
python chewy_enrich.py --input output/normalized_products --mode all --limit 50
```

---

## 2. Regression Test Results

| Test | Metric | Value | Status |
|------|--------|-------|--------|
| **A (content)** | variants_enriched | **21** | ✅ |
| | flavor_mismatch_count | 0 | ✅ |
| | public_content_unsafe_count | 0 | ✅ |
| | rejected_content_leaked_count | 0 | ✅ |
| | blocked_count | 0 | ✅ |
| | wrong_product_api_rejected | 28 | ✅ (correct) |
| | exit_code | 0 | ✅ |
| **B (price)** | variants_missing_price_before | 8 | |
| | variants_price_recovered | 0 | ✅ (discontinued) |
| | flavor_mismatch_count | 0 | ✅ |
| | public_content_unsafe_count | 0 | ✅ |
| | blocked_count | 0 | ✅ |
| | exit_code | 0 | ✅ |
| **C (image)** | variants_missing_image_before | 14 | |
| | variants_image_recovered | **5** | ✅ (improved!) |
| | flavor_mismatch_count | 0 | ✅ |
| | public_content_unsafe_count | 0 | ✅ |
| | blocked_count | 0 | ✅ |
| | exit_code | 0 | ✅ |

> **Notable improvement:** Category C now recovers 5 images (was 0 in old runner) thanks to the parameterized GraphQL URL key support in `extract_variant_info_from_apollo`.

---

## 3. What Was Merged

| Old Files | → New File | Shared Logic Extracted |
|-----------|-----------|----------------------|
| `run_category_a_test.py` | `chewy_enrich.py` | `FLAVOR_KEYWORDS`, `detect_flavor_mismatch()`, `has_real_images()`, `sanitize_product()`, hard-fail checks, report writing |
| `run_category_b_test.py` | `chewy_enrich.py` | Same + `recover_price_for_variant()` |
| `run_category_c_test.py` | `chewy_enrich.py` | Same + `recover_images_for_variant()` |
| `category_job_runner.py` | `category_scraper.py` | Direct copy (same functionality, cleaner name) |
| `test_single_product.py` | `chewy_product_scraper.py` | Scraping logic via imports from `chewy_next_json_extractor.py` |

---

## 4. Files Archived

| File | Archive Path | Reason |
|------|-------------|--------|
| `run_category_a_test.py` | `_archive/old_scripts/` | Superseded by `chewy_enrich.py --category A` |
| `run_category_b_test.py` | `_archive/old_scripts/` | Superseded by `chewy_enrich.py --category B` |
| `run_category_c_test.py` | `_archive/old_scripts/` | Superseded by `chewy_enrich.py --category C` |
| `test_single_product.py` | `_archive/old_scripts/` | Superseded by `chewy_product_scraper.py` |
| `test_chewy_json_extractor_batch.py` | `_archive/old_scripts/` | Superseded by batch mode |
| `test_variant_api.py` | `_archive/old_scripts/` | One-off API test |
| `test_category_a_enrichment.py` | `_archive/old_scripts/` | Early test draft |
| `patch.py` | `_archive/old_patches/` | Old extraction fix |
| `patch2.py` | `_archive/old_patches/` | Old `extract_variant_info_from_apollo` draft |
| `recover_functions.py` | `_archive/old_scripts/` | Empty file |
| `print_report.py` | `_archive/old_scripts/` | One-off script |
| `fetch_next_data.py` | `_archive/old_scripts/` | One-off script |
| Old result/report JSON files | `_archive/old_test_outputs/` | Superseded by new run outputs |

---

## 5. Dependency Files Kept

| File | Role |
|------|------|
| `chewy_next_json_extractor.py` | Core parsing/enrichment engine (imported by all 3 entry points) |
| `config.py` | All configuration |
| `adspower.py` | AdsPower browser API |
| `adsp_profile_pool_manager.py` | Profile pool + white screen detection |
| `adsp_profile_recovery_manager.py` | Profile quarantine/recovery |
| `job_store.py` | SQLite job queue |
| `job_exporter.py` | Export normalized → grouped products |
| `category_discovery.py` | Category product discovery logic |
| `category_discovery_validation.py` | Category validation logic |
| `category_price_filter.py` | Price filtering rules |
| `category_job_runner.py` | Original CLI (kept; `category_scraper.py` is the new name) |
| `resumable_scraper_runner.py` | Production batch scraper runner |
| `parallel_resumable_runner.py` | Multi-profile parallel runner |
| `app_ui.py` | Web UI for scraper management |
| `ui_file_browser.py` | UI component |
| `ui_log_parser.py` | UI component |
| `ui_runner.py` | UI component |
| `bulk_quality_audit.py` | Dataset-wide audit tool |
| `audit_normalized_dataset.py` | Dataset audit |
| `check_slots.py` | Quick diagnostic |

---

## 6. Files NOT Touched

| Path | Reason |
|------|--------|
| `output/normalized_products/` (3,115 files) | Production data — DO NOT MODIFY |
| `scraper_jobs.db` | Production job database |
| `output/scraper_jobs.db` | Output job database |
| `output/cache/` | API response cache |
| `.env` | Credentials |

---

## 7. Files for Later Cleanup

| File | Recommendation |
|------|---------------|
| `category_job_runner.py` | Can be removed once `category_scraper.py` is confirmed working in production |
| `bulk_quality_audit.py` | Evaluate overlap with `chewy_enrich.py --mode all` |
| `audit_normalized_dataset.py` | Keep if used for one-off dataset audits |
| `app_ui.py` + `ui_*.py` | Keep if the web UI is still needed |

---

## 8. Readiness Assessment

| Check | Status |
|-------|--------|
| 3 entry points created | ✅ |
| Category A regression passes (21 enriched) | ✅ |
| Category B regression passes (0 mismatch) | ✅ |
| Category C regression passes (0 mismatch, 5 images recovered) | ✅ |
| Old test runners archived | ✅ |
| No production data modified | ✅ |
| Safety checks intact | ✅ |

**Ready for 50-product controlled batch test:** ✅

```bash
python chewy_enrich.py --input output/normalized_products --mode all --limit 50
```
