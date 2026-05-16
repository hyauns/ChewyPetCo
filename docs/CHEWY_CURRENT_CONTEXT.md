# Chewy Scraper — Current Context

**Updated:** 2026-05-16 (session 2 continued — ending at commit `efa4b7c`)
**Repo:** https://github.com/hyauns/ChewyPetCo
**Purpose of this file:** Single source of truth for a future Claude session to pick up where this one left off, without re-reading the full chat. Read this first.

> **🚨 BIG ARCHITECTURAL CHANGE THIS SESSION:** The two-pass pipeline (scrape → enrich) was merged into a **single-pass scraper**. `chewy_enrich.py` is now DEPRECATED. Per-product enrichment now runs inline inside `chewy_product_scraper.py`, writing `normalized + grouped + validation` files per pid. Final aggregation is `tools/build_shopify_jsonl.py` (dedupe across pages → Shopify JSONL). The decision to re-scrape from scratch was driven by the discovery that ~25–50% of legacy 5-digit variant IDs in the original 3115 dataset had been delisted from Chewy and return 404.

---

## 1. TL;DR

This project scrapes Chewy.com product pages **and enriches them in a single pass** into Shopify-import-ready files. The historical dataset of ~3,115 normalized files is being re-scraped from scratch (variant IDs aged out). New canonical output is per-pid `output/{normalized,grouped,validation}_products/chewy_*.json`; final Shopify-ready feed is the aggregated JSONL from `tools/build_shopify_jsonl.py`.

**Current state of the code (after session 2):**
- **Single-pass pipeline.** `chewy_product_scraper.scrape_single_product` does: fetch HTML → parse Apollo → normalize → `enrich_variants_from_api` (variant-API content + entry_id backfill + stock fields) → `split_product_by_flavor` → `validate_normalized_product` → `sanitize_product`. Three files written per pid: normalized, grouped (Shopify-shaped, with `validation` embedded), validation.
- **`chewy_enrich.py` is DEPRECATED** (kept as a thin module re-exporting helpers for backward compat; its CLI still works on old-format normalized files but is no longer the canonical flow).
- **Scraper helpers (`FLAVOR_KEYWORDS`, `has_real_images`, `detect_flavor_mismatch`, `sanitize_product`) moved into `chewy_next_json_extractor.py`** — one source of truth. `chewy_enrich.py` re-exports for any caller that imports `chewy_enrich.X`.
- **Subprocess wiring fixed.** `resumable_scraper_runner.py` used to spawn `test_single_product.py`, which was deleted in commit `f3abfb7` months ago. Symptom: scraper "completed" in seconds without doing work, because `existing_json_output_ok` short-circuited before the subprocess ever ran. Now spawns `chewy_product_scraper.py --url <url>`, reusing the runner's already-open browser via `ADSP_BROWSER_WS_URL`.
- **Proxy-dead → profile rebuild.** When `page.goto` raises `ERR_CONNECTION_CLOSED/RESET/REFUSED/ABORTED/PROXY/TUNNEL/SOCKS/TIMED_OUT/NETWORK_CHANGED`, or `scrape_single_product` raises `WhiteScreenException`, the scraper emits the existing `[WHITE_SCREEN_RESULT]` marker line. The runner already knows how to handle it: quarantine profile, mark item pending, rebuild slot, retry. Same exit code = item failed without quarantine — that was the old behavior that left bad profiles stuck.
- **Missing `__NEXT_DATA__` is treated as white-screen** (`2e4374d`). When Chewy/Akamai serves a soft-block page (HTTP 200 + challenge body, no Apollo state), the scraper now calls `detect_white_screen_block` and raises `WhiteScreenException`. Even if that detection misses, a `next_data is None` after the build_id fallback also raises — on Chewy PDPs every legitimate page has Apollo, so a missing one is almost always profile-throttled. Without this fix all 3 workers were churning through hundreds of URLs in a row, every one failing with "Failed to extract __NEXT_DATA__", with no profile rebuild.
- **Profile-leak fix in `auto_rebuild_profile`** (`efa4b7c`). Previously each rebuild was leaking an AdsPower profile to the user's 55-profile plan limit because (a) the DB template's `adspower_profile_id` was stale across sessions and pointed at a long-deleted profile (delete-by-id silently no-oped), and (b) the new helper that scans AdsPower by display_name to find the actual orphan was hitting AdsPower's per-second rate limit (`code=-1 "Too many request per second"`) and silently returning empty. Fixed by adding retry+backoff to `_list_adspower_profile_ids_by_name`, and by collecting **all** stale ids per slot (DB id + every AdsPower profile sharing the slot's `display_name`) before creating the new one. Net result on local: 25 → 23 profiles after one CW_1 rebuild that cleaned up 3 orphans; each slot now ends with exactly 1 profile after a rebuild.
- **BOM-safe URL ingestion.** `read_urls_file` now reads with `utf-8-sig` so a PowerShell `Set-Content -Encoding utf8` (utf-8-with-BOM) file doesn't leak `﻿` into the first URL.
- **Compounded products filtered out** from the input list (`chewy_enrich.py` for the old path; the URL list emitted by `tools/prepare_rescrape.py` is unfiltered because all-product compounded URLs still scrape fine — the filter only mattered for the deprecated enrich path; the user does not sell Chewy-exclusive compounded meds on Shopify).

**Pipeline runtime sanity (verified on local demo this session):**
- 10 URLs × 3 workers: 9/10 succeed, ~3.9 minutes. The one failure (`pid 607894 Nulo Freestyle Turkey`) was a legitimate "no title" parse — the product appears delisted on Chewy.
- All Shopify-critical fields populate correctly on a re-scrape: `source_entry_id`, `out_of_stock`, `stock_state`, `stock_reason`, `shopify_inventory_policy`, **`transition_instructions`** (which was empty across the old enrichment runs), ingredients/GA/feeding per variant.
- Resume verified end-to-end: cancel job mid-flight → resume picks up only the pending/failed items, skipping done ones via `existing_json_output_ok`.

**What's running on VPS** (`C:\Users\Administrator\Downloads\ChewyPetCo`):
- Latest commit: `efa4b7c`.
- Workflow now: `prepare_rescrape.py` → `resumable_scraper_runner.py create --urls urls_all.txt` → `start --workers 3` → `tools/build_shopify_jsonl.py`. See §12.
- Profile IDs and proxies live in `.env` only (never in git).
- **Recommended after pulling the orphan-leak fix:** manually trigger `auto_rebuild_profile` for each CW_1/CW_2/CW_3 slot once before resuming the scrape, to clean up any orphan profiles already accumulated on AdsPower. One-liner included in §12 runbook.

---

## 2. Pipeline (high level) — single-pass since session 2

```
tools/urls_all.txt  (generated by tools/prepare_rescrape.py from source_url
                     fields in any pre-existing normalized files)
  |
  v  resumable_scraper_runner.py create --urls urls_all.txt
     (seeds scrape_jobs / scrape_job_items in scraper_jobs.db)
  |
  v  resumable_scraper_runner.py start --workers N
     -> parallel_resumable_runner._worker_loop (one CW slot per worker)
        -> claim_next_item (BEGIN IMMEDIATE)
        -> process_single_item -> spawn chewy_product_scraper.py --url <url>
           subprocess env: ADSP_BROWSER_WS_URL=<worker's open browser>,
                           ADSPOWER_PROFILE_ID=<worker's profile>
  |
  v  chewy_product_scraper.scrape_single_product (one PID, in-process)
       +- fetch_initial_html (+ retries, white-screen detection)
       +- extract_next_data_from_html / detect_next_build_id
       +- parse_apollo_product   (Apollo state → raw product dict)
       +- normalize_chewy_product  (canonical schema)
       +- enrich_variants_from_api  (one /_next/data/.../dp/{entry_id}.json
            per variant — fills description, ingredients, GA, feeding,
            calorie, transition, images, AND derives stock_state /
            out_of_stock / stock_reason / shopify_inventory_policy)
       +- split_product_by_flavor  (split by every non-size defining attr)
       +- validate_normalized_product  (confidence_score, gtin coverage, …)
       +- sanitize_product (per Shopify product: flavor_mismatch check,
            import_ready / import_mode flags)
  |
  v  THREE files written atomically per pid:
       output/normalized_products/chewy_{pid}.json           (canonical product + enriched variants)
       output/grouped_products/chewy_grouped_by_flavor_{pid}.json  (Shopify-shaped, has 'validation' embedded)
       output/validation/chewy_validation_{pid}.json         (just the validation dict)
  |
  v  tools/build_shopify_jsonl.py  (run once after the job finishes)
       +- read every grouped_products/*.json
       +- dedupe_products_across_pages (collapse multi-landing-page
            duplicates — e.g. Wysong Archetype Chicken/Quail/Rabbit
            all share the same 3 variant entry_ids)
       +- write output/shopify_import_{ts}.jsonl
            + output/shopify_import_dedupe_log_{ts}.json
```

### What `existing_json_output_ok` guards

Before spawning the scraper subprocess, the runner checks both the grouped file (valid products + variants + titles) and the validation file (confidence ≥ threshold, not marked invalid). If both pass, the item is marked `done` without scraping. This is what makes resume cheap — already-done pids are O(file existence check). To force re-scrape, pass `--reprocess-existing`.

---

## 3. File Map

### Production code (commit, run on VPS)
| File | Role |
|---|---|
| `chewy_next_json_extractor.py` (~2265 LoC) | Core extractor. Parses Apollo, normalizes, enriches via variant API, splits by discriminator, validates, sanitizes, dedupes. **All Shopify-shaping helpers live here since session 2** (was previously split with `chewy_enrich.py`). |
| `chewy_product_scraper.py` | **Single-pass scrape+enrich CLI.** `scrape_single_product` runs the full pipeline in-process; `main()` writes normalized + grouped + validation. Subprocess entry for `resumable_scraper_runner`. Reuses runner's browser via `ADSP_BROWSER_WS_URL` env var. Emits `[WHITE_SCREEN_RESULT]` markers on proxy errors so the runner rebuilds the bad profile. |
| `resumable_scraper_runner.py` | Production scrape runner. Per-URL DB state, subprocess spawn, white-screen parsing. `create / start / resume / pause / cancel / status / retry-failed / skip-current` subcommands. Reads URL files with `utf-8-sig` (BOM-safe). |
| `parallel_resumable_runner.py` | Multi-worker scrape runner. Each worker owns one CW slot, keeps an AdsPower browser open across items, atomically claims via `job_store.claim_next_item`. White-screen / throttle → slot status flips to `rebuilding` → loop rebuilds + restarts browser. |
| `chewy_enrich.py` | **DEPRECATED.** Re-exports helpers (`FLAVOR_KEYWORDS`, `has_real_images`, `detect_flavor_mismatch`, `sanitize_product`) from the extractor for backward compatibility. The CLI still works for re-enriching legacy normalized files, but the canonical flow no longer uses it. |
| `parallel_enrich_runner.py` | **DEPRECATED** (paired with `chewy_enrich.py`). Still importable, no role in the new flow. |
| `category_scraper.py` / `category_discovery*.py` | Discover product URLs by category. Not used in this session's re-scrape (URLs come from existing normalized files via `prepare_rescrape.py`). |
| `adspower.py` | AdsPower local API client. |
| `adsp_profile_pool_manager.py` | Profile pool + white-screen detection (used by both scrape and old enrich runners). |
| `adsp_profile_recovery_manager.py` | Slot template management, `auto_rebuild_profile`, proxy-soft-toggle helpers, `.env` config sync. |
| `job_store.py` | SQLite layer. `scrape_jobs / scrape_job_items` + `chewy_enrichment_state` (legacy, unused by new flow) + claim primitives + orphan reset helpers. |
| `config.py` | Reads `.env`. `ADSPOWER_PROFILE_ID`, `ADSP_CW_{1,2,3}_PROFILE_ID`, proxy URLs, slot count, timeouts. |
| `job_exporter.py` | Export normalized → grouped products (legacy, superseded by single-pass scraper writing both). |

### Tooling (added this session)
| Path | Role |
|---|---|
| `tools/prepare_rescrape.py` | Reads existing `output/normalized_products/chewy_*.json` files, extracts the deduped `source_url` set into `tools/urls_all.txt` (3115 lines) and a deterministic 20-URL pilot sample (`seed=42`) into `tools/urls_pilot.txt`. |
| `tools/build_shopify_jsonl.py` | Reads every `output/grouped_products/*.json` after a scrape job, runs `dedupe_products_across_pages`, emits `output/shopify_import_{ts}.jsonl` + a dedupe log. This is the final Shopify-ready feed. |
| `tools/urls_*.txt` | Gitignored. Re-generate on demand. |

### Audit / one-off
| File | Notes |
|---|---|
| `audit_normalized_dataset.py` | Dataset-wide audit; produces `audit_reports/`. |
| `bulk_quality_audit.py` | Batch pipeline auditor. |
| `check_slots.py` | Quick CW slot diagnostic. |

### Test/dev artifacts (in repo but not on critical path)
| Path | Notes |
|---|---|
| `test_runs/shopify_preview_20260515/dry_run_v2_batch.py` | Standalone runner used to generate 11-product dry-run JSONLs. |
| `test_runs/shopify_preview_20260515/build_preview_v2.py` | Build Shopify-style HTML preview from dry_run output. |
| `test_runs/shopify_preview_20260515/dry_run_v2_output/` | 11 sample grouped JSONs + `_deduped.json` + `_state.json`. |

### NOT in git (must rsync from old machine to VPS)
- `.env` — credentials (profile IDs, proxy host/port/user/pass).
- `output/normalized_products/` — 3,115 scraped product JSONs.
- `output/cache/` — variant-API response cache (`{entryID}_{buildId}.json`).
- `scraper_jobs.db` — runtime DB. Init fresh on VPS or transfer.

---

## 4. Database — `scraper_jobs.db`

### Schema highlights (all created by `job_store.init_db()`)

**Tables relevant to enrich + multi-worker:**

```sql
chewy_enrichment_state (
    product_id TEXT PRIMARY KEY,
    source_url TEXT,
    status TEXT CHECK(status IN ('pending','in_progress','ok','failed','skipped')),
    mode, run_label,
    last_run_at, last_started_at,
    output_path,                     -- JSONL file where this pid was written
    product_count, variant_count,
    enriched_count, wrong_product_rejected, slug_mismatch,
    attempt_count,                   -- bumped each claim; capped by --max-attempts
    error_type, error_message,
    worker_id, profile_slot_id,      -- which worker/slot processed it
    created_at, updated_at
);

adsp_profile_templates (
    slot_id TEXT UNIQUE,             -- 'CW_1', 'CW_2', 'CW_3'
    display_name, proxy_type, proxy_host, proxy_port,
    proxy_username_masked,           -- ONLY masked; real creds live in .env
    adspower_profile_id,             -- runtime profile id from AdsPower
    status, last_rebuild_at, ...
);

scrape_jobs, scrape_job_items, chewy_product_registry,
chewy_product_url_aliases, adsp_profile_pool,
adsp_profile_rebuild_events, white_screen_events,
category_discovery_jobs, category_discovery_items
```

**Gotcha (caused d10fbef bug):** `adsp_profile_templates` has `proxy_username_masked` but NO real `proxy_username` / `proxy_password` columns. To rebuild a proxy config, use `_template_by_slot(slot_id)` — that re-parses `.env` and returns real credentials.

### Key helpers in `job_store.py`

```python
# Enrichment state (resume + multi-worker)
seed_enrichment_state(pids, source_urls)            # idempotent INSERT pending rows
claim_next_enrichment_pid(worker_id, profile_slot_id, retry_failed=True, max_attempts=5)
                                                    # BEGIN IMMEDIATE atomic claim
release_enrichment_claim(pid, reset_to='pending')    # for white-screen / proxy-dead
mark_enrichment_in_progress(pid, source_url, mode, run_label)
mark_enrichment_ok(pid, output_path, product_count, variant_count, ...)
mark_enrichment_failed(pid, error_type, error_message)
recover_stale_enrichment_states(stale_minutes=30)   # reset crash-orphans
is_enrichment_done(pid), get_enrichment_state(pid)
list_enrichment_states(status=None), count_pending_enrichment()
enrichment_state_summary()                          # {status: count}
reset_enrichment_state(pid)                         # delete row (used by --force-reenrich)

# Scrape phase (existing, mirrors the same pattern)
claim_next_item(job_id, ...), mark_orphan_running_items, mark_stale_running_items
```

---

## 5. JSONL Output Format

`output/enrichment_runs/result_{label}_{ts}.jsonl` — one Shopify-shaped product per line.

Each line is a `grouped` dict with this shape (abbreviated):

```json
{
  "source": "chewy",
  "source_product_id": "101610",
  "source_url": "https://www.chewy.com/.../dp/101610",
  "architecture": "apollo",
  "grouping_strategy": "discriminator_attrs_as_product_size_as_variant",
  "is_multi_product": true,
  "validation": { },
  "products": [
    {
      "source_group_id": "101610:breed-size:large",
      "title": "Royal Canin Veterinary Diet Adult Weight Control Large Breed Dry Dog Food",
      "handle_slug": "royal-canin-veterinary-diet-adult-large",
      "flavor": null,
      "discriminator": {"breed size": "Large"},
      "brand": "Royal Canin",
      "category_path": ["..."],
      "description": "...",
      "ingredients": "...",
      "guaranteed_analysis": "...",
      "feeding_instructions": "...",
      "transition_instructions": "...",
      "specifications": {"groups": [{"title":"Specifications","items":["..."]}]},
      "product_facts": {"breed_size": "Large"},
      "content_sections": {},
      "storefront_display": {},
      "metafields_plan": {},
      "images": ["https://image.chewy.com/...moe/...jpg"],
      "variants": [
        {
          "source_entry_id": "43673",
          "source_variant_id": "58667",
          "sku": "58667",
          "identifiers": {"upc": "...", "gtin": "...", "ean": null, "mpn": null},
          "title": "Royal Canin ... Large Breed Dry Dog Food, 24.2-lb bag",
          "option1_name": "Size",
          "option1_value": "24.2-lb bag",
          "price": "$116.99",
          "autoship_price": "$111.14",
          "compare_at_price": null,
          "in_stock": true,
          "out_of_stock": false,
          "stock_reason": "in_stock_signal_true",
          "shopify_inventory_policy": "continue",
          "availability": "AVAILABLE",
          "description": "...",
          "ingredients": "...",
          "guaranteed_analysis": "...",
          "feeding_instructions": "...",
          "transition_instructions": "...",
          "calorie_content": "...",
          "images": ["..."],
          "variant_url": "https://www.chewy.com/.../dp/43673",
          "content_source": {
            "type": "apollo_variant_api",
            "source_entry_id": "43673",
            "source_variant_id": "58667",
            "confidence": "high",
            "entry_id_backfilled": true
          }
        }
      ],
      "out_of_stock": false,
      "stock_state": "in_stock",
      "debug": {
        "discriminator": {"breed size": "Large"},
        "title_source": "longest_variant_with_disc_match",
        "title_augmentations": [],
        "parser_warnings": []
      },
      "canonical_source": {
        "source_product_id": "101571",
        "source_url": "https://www.chewy.com/.../dp/101571",
        "fingerprint": ["101571"],
        "duplicate_count": 2,
        "dropped_source_pids": ["101573","101575"]
      }
    }
  ]
}
```

**Field semantics:**
- `source_entry_id` = Chewy entry ID, decoded base64 from Apollo Item key `Item:{base64}`. Use this in URLs `/dp/{entry_id}`.
- `source_variant_id` = Chewy `partNumber` = the SKU.
- `out_of_stock` = explicit boolean. `stock_state` = `"in_stock"` if any variant in_stock; `"all_variants_out_of_stock"` if all OOS; `"stock_unknown"` if no signals.
- `shopify_inventory_policy` = `"deny"` if OOS else `"continue"` — feed directly to Shopify.
- `content_source.entry_id_backfilled` = True when the variant was upgraded from OLD schema (`source_entry_id` was None before this run).
- `canonical_source` = present only after `dedupe_products_across_pages`. Indicates which source page won the dedupe and which were dropped.

**Stream guarantees:** each `write(line + "\n") + flush() + fsync()` is called before claiming the next pid. Power loss / Ctrl+C preserves all completed lines.

---

## 6. Multi-Worker Architecture

### Slot mapping
- Each worker is bound to exactly one CW slot: `worker_1 -> CW_1`, `worker_2 -> CW_2`, `worker_3 -> CW_3`.
- Slot -> profile resolution priority (in `parallel_enrich_runner._get_slot_profile`):
  1. `.env` var `ADSP_CW_1_PROFILE_ID` (and `_2`, `_3`) — WINS if set
  2. `adsp_profile_templates.adspower_profile_id` in DB — fallback
  3. If both empty, worker auto-provisions via AdsPower API (initial setup only)

### Claim flow (atomic)
```
worker A                  worker B                  worker C
  |                         |                         |
  v BEGIN IMMEDIATE         v (waits)                 v (waits)
  SELECT next pending
  UPDATE status=in_progress, worker_id=A
  COMMIT
  |                         |                         |
                            v BEGIN IMMEDIATE         v (waits)
                            SELECT next pending      ...
```
SQLite WAL + `busy_timeout=60s` + `BEGIN IMMEDIATE` serializes. No duplicate claims.

### Ordering: `pending` first (ASC by `attempt_count`, then `product_id`). `failed` second (only if `attempt_count < max_attempts`).

### Shared JSONL with `asyncio.Lock`
- One file open across all workers.
- Each successful product: `async with jsonl_lock: write + flush + fsync`.
- Single-threaded asyncio + GIL, but the lock is still there for clarity.

---

## 7. Failure Recovery

### A. Crash / power loss
- Worker had pid claimed (`status='in_progress'`).
- On next CLI start: `recover_stale_enrichment_states(stale_minutes=30)` resets pids whose `last_started_at` > 30 min ago back to `pending`.
- Re-claimed by any worker.

> **NOTE — sections B, C, D below describe the OLD enrich-worker flow (`parallel_enrich_runner`).** The new scrape-subprocess flow has a different shape:
> - **White-screen / proxy-dead detection** happens inside the `chewy_product_scraper.py` subprocess. It emits a `[WHITE_SCREEN_RESULT] {json}` line and exits non-zero.
> - **Profile quarantine + slot rebuild** happens in `resumable_scraper_runner.process_single_item` (parses the marker) and `parallel_resumable_runner._worker_loop` (sees `slot.status='rebuilding'` next iteration, calls `auto_rebuild_profile`, restarts browser, retries the item — DB-backed, the slot template stays under management).
> - **Item state** is `scrape_job_items.status` in the DB, not `chewy_enrichment_state`. Reset via `mark_orphan_running_items` / `mark_stale_running_items` on resume.
> The legacy text below is still accurate for anyone running the deprecated `chewy_enrich.py --parallel`. Treat it as historical.

### B. White-screen on a pid (Akamai bot detection OR HTTP 429/403/503 throttle)
**Two trigger sources, same handler:**
- (i) HTML body looks like a block page on `page.goto` — detected by `adsp_profile_pool_manager.detect_white_screen_block` inside `chewy_enrich.get_build_id`.
- (ii) Any variant `/_next/data/.../dp/{X}.json` call returns HTTP 429/403/503 — `fetch_next_data_json` raises `WhiteScreenException` (added in `fadbcbd`). Statuses in `chewy_next_json_extractor.PROFILE_BLOCKED_STATUSES`.

Both paths raise the same `WhiteScreenException` (canonical home: `chewy_next_json_extractor.py`; re-exported from `chewy_enrich.py`).

**Worker flow (since `d8c184f`, pure AdsPower API, no DB):**
1. Worker catches `WhiteScreenException` from `chewy_enrich.process_product`.
2. `release_enrichment_claim(pid, reset_to='pending')` — pid back to queue.
3. `_stop_browser(profile_id)`.
4. `recovery.delete_profile_via_api(profile_id)` — best-effort delete on AdsPower.
5. `recovery.create_profile_via_api(slot_template)` — creates a fresh profile with `.env` proxy. Returns new profile_id.
6. Worker updates its in-memory `profile_id` to the new value. Opens browser. Continues claiming.
7. `.env` is NOT modified. Next CLI invocation starts again from the .env profile_id; if that profile doesn't exist (because the previous run rebuilt past it), the worker creates a new one on the fly via the same `_provision_profile` path. No accumulated state.

### C. Proxy dead (`ERR_CONNECTION_CLOSED`, `ERR_SOCKS_*`, `ERR_TUNNEL_*`)
1. Worker catches the error, detected via `recovery.is_proxy_connection_error(e)`.
2. `release_enrichment_claim(pid, reset_to='pending')` — pid back to queue.
3. `_stop_browser(profile_id)`.
4. `recovery.switch_profile_to_local_via_api(profile_id)` — calls AdsPower `/api/v1/user/update` with `user_proxy_config={"proxy_soft": "no_proxy"}` on the **SAME** profile_id. **No delete, no create. No DB write.**
5. Worker restarts the SAME profile (now using host's network directly) and continues.
6. **No persistence:** the in-memory profile_id tracks the local state. Next CLI invocation reads .env again and starts fresh. If the proxy is back, normal flow resumes; if still dead, the same swap-to-local happens during this run.

### D. Profile missing (`does not exist` from AdsPower)
Since `d8c184f` (DB-free profile management):
- Worker tries the `.env` profile_id. If AdsPower says it doesn't exist, the worker **automatically creates a new one** via `_provision_profile` using the .env proxy config and continues. No DB read, no error stop.
- This handles the case where a previous run's white-screen handler deleted the .env profile — the worker just rebuilds and keeps going on the new id (in-memory).

---

## 8. CLI

### Canonical flow (session 2 onward)

```powershell
# 1. Generate URL list from existing normalized files
python tools\prepare_rescrape.py

# 2. Seed a job
python resumable_scraper_runner.py create --name <name> --urls tools\urls_all.txt

# 3. Run it
python resumable_scraper_runner.py start --job-id <job_id> --workers 3

# 4. Resume after a crash / Ctrl+C / pause
python resumable_scraper_runner.py resume --job-id <job_id> --workers 3

# 5. Aggregate into Shopify JSONL
python tools\build_shopify_jsonl.py
```

**`resumable_scraper_runner.py` subcommands** (see `--help`):
- `create --name NAME --urls FILE [--mode ...] [--confidence-threshold N] [--max-attempts N]`
- `start | resume --job-id ID --workers N [--retry-failed] [--reprocess-existing] [--force-retry] [--reset-profile-attempts] [--stale-minutes N] [--max-items N]`
- `status --job-id ID` — JSON summary (counts, success rate, worker results)
- `pause | cancel --job-id ID`
- `retry-failed --job-id ID [--force] [--no-start] [--max-items N]`
- `skip-current --job-id ID`

**Key flags:**
- `--reprocess-existing` — bypass `existing_json_output_ok` check, scrape every URL even if its 3 output files exist. Use when re-running with code changes that should change the output.
- `--retry-failed` — pick `failed` items whose `attempts < max_attempts`. Default-on for `resume`, default-off for `start`.
- `--force-retry` — pick `failed` items even past `max_attempts`. Use sparingly.
- `--reset-profile-attempts` (resume only) — clear `profile_attempts_json` and `white_screen_count` for pending/failed/paused items; release all quarantined profiles. Use after fixing a proxy issue at the .env level.

### Legacy CLI (deprecated — for reference only)

```powershell
# These still run; they operate on the chewy_enrichment_state table and read
# existing normalized files. They will NOT scrape from scratch — for that,
# use the canonical flow above.
python chewy_enrich.py --input output/normalized_products --mode all
python chewy_enrich.py --input output/normalized_products --mode all --parallel --workers 3 --limit 20 --force-reenrich
```

---

## 9. Critical Design Decisions / DO-NOT-TOUCH list

1. **`output/normalized_products/` is the source of truth.** Never overwrite. Enrich reads from it. New scrapes write a NEW file.
2. **`.env` holds all credentials.** Code reads via `config._load_local_env()` at import time + per-template `_template_by_slot()`. Never commit `.env` (already gitignored).
3. **`moe/` URLs in `image.chewy.com/.../moe/...` are REAL variant images.** Earlier code treated them as placeholders — that was wrong. All `moe/` filtering has been removed.
4. **Attribute classification for split:**
   - VARIANT-AXIS keywords (every token must match): `size, weight, pack, count, case, bundle, quantity, carton`. These STAY as variant options.
   - Everything else (Flavor, Breed Size, Lifestage, Color, Strength, etc.) is a PRODUCT-DISCRIMINATOR -> each value = its own Shopify product.
   - Exact-token match. "Breed Size" is NOT axis (has "breed" which isn't a size keyword).
5. **URL ID semantics:**
   - `/dp/{X}` X = **entryID** (Apollo Item key decoded from base64).
   - `partNumber` is the SKU surfaced in the Apollo Item.
   - For NEW scrapes, `variant_url` is built from entry_id. For OLD scrapes that have only partNumber, the enrich code follows the 301 redirect and backfills the canonical entry_id.
6. **Cross-page dedupe** runs at batch level via `dedupe_products_across_pages(all_grouped)`. Fingerprint = sorted tuple of variant `source_entry_id`s. Wysong's 3 landing pages (Chicken/Quail/Rabbit) all expose the same 3 variants -> would become 9 Shopify products without dedupe; correctly collapsed to 3.
7. **White-screen ALWAYS deletes profile; proxy-dead NEVER deletes profile.** This was the central UX requirement.
8. **JSONL append + flush + fsync is the only durable write.** No mid-run mega-JSON; `chewy_enrich.run_pipeline` removed the old `results.append(grouped)` accumulator to keep memory flat at 3,115 products.
9. **`adsp_profile_templates.proxy_username_masked` is the only credential-shaped column.** Reading `proxy_username` from this table will throw — use `_template_by_slot(slot_id)` to get parsed creds from `.env` instead.

---

## 10. Session History — commits & what each fixed

(Newest first.)

### Session 2 continued (2026-05-15 evening → 2026-05-16) — Profile-leak + missing __NEXT_DATA__ fixes

| Commit | What | Why |
|---|---|---|
| `efa4b7c` | Fix orphan profile leak in `auto_rebuild_profile` | The user's AdsPower account was filling up toward its 55-profile plan limit because each white-screen rebuild was leaking one profile. Two compounding bugs: (1) the DB template's `adspower_profile_id` was stale across sessions and pointed at a long-deleted id — delete-by-id silently no-oped and the actual orphan with the same `display_name` was never touched. (2) The new helper that scans AdsPower by `display_name` was hitting AdsPower's per-second rate limit (`code=-1 "Too many request per second"`) and silently returning empty — verified by running the helper twice back-to-back (3 ids then `[]`). Fixed by adding retry+backoff in `_list_adspower_profile_ids_by_name`, and by collecting **all** stale ids per slot (DB id + every AdsPower profile sharing the slot's `display_name`) before creating the new one. Tested end-to-end on local: 25 → 23 profiles after one rebuild that cleaned up 3 CW_1 orphans; each slot now ends with exactly 1 profile after a rebuild. |
| `2e4374d` | Treat missing `__NEXT_DATA__` as white-screen | When Chewy / Akamai serves a soft-block page (HTTP 200 + challenge body, no Apollo state), the scraper was just printing "Failed to extract `__NEXT_DATA__`" and returning None; main() exited 1 without emitting the `[WHITE_SCREEN_RESULT]` marker, so the runner marked the item as generic `network_error` and let the worker keep claiming URLs against the same dead profile. Production manifested as all 3 workers churning through hundreds of URLs in a row with the same failure. Fix: add `detect_white_screen_block(page, url)` right after `fetch_initial_html` (this is the same explicit check the legacy `chewy_enrich.get_build_id` used — it was dropped during the pipeline merge in `a5cb64e`). And if `next_data` is still None after the build_id fallback, raise `WhiteScreenException` — Chewy PDPs always have Apollo, a missing one is almost always a profile-throttled soft block. Both paths reuse the existing marker emit in `main()`. |
| `f59626a` | Doc: capture session 2 architecture in `docs/CHEWY_CURRENT_CONTEXT.md` | Reflect the pipeline merge, deprecated `chewy_enrich.py`, three-file-per-pid output, `tools/build_shopify_jsonl.py` as final aggregation, and the new `resumable_scraper_runner` canonical CLI. |

### Session 2 (2026-05-15 afternoon) — Pipeline merge

| Commit | What | Why |
|---|---|---|
| `9aa09cb` | Emit white-screen marker on proxy errors; strip BOM from URL files | Two issues surfaced by local 10-URL demo: (1) `page.goto` raising `ERR_CONNECTION_CLOSED` killed subprocess with a generic non-zero exit — runner marked the item failed without quarantining the dead profile, so every retry hit the same proxy. Scraper now catches these errors (and `WhiteScreenException` from the inner pipeline) and emits the `[WHITE_SCREEN_RESULT]` marker line the runner already knows how to handle: quarantine + slot rebuild + retry. (2) PowerShell `Set-Content -Encoding utf8` writes utf-8-with-BOM — the BOM leaked into URLs and crashed the runner on cp1252 console. `read_urls_file` now uses `utf-8-sig` to silently strip the BOM. Demo result after the fix: **9/10 success rate** on the same input. |
| `a5cb64e` | Merge variant-API enrichment into scraper; restore subprocess entry | The historical two-pass flow (`scrape → enrich`) was collapsed into a single pass inside `chewy_product_scraper.scrape_single_product`. Per-product helpers (`FLAVOR_KEYWORDS`, `has_real_images`, `detect_flavor_mismatch`, `sanitize_product`) moved from `chewy_enrich.py` to `chewy_next_json_extractor.py`; `chewy_enrich.py` re-exports for backward compatibility. Scraper now writes `normalized + grouped + validation` per pid. **Also fixed a latent bug**: `resumable_scraper_runner.py` was still spawning `test_single_product.py` (deleted in commit `f3abfb7` months ago) — the runner appeared to work only because `existing_json_output_ok` was short-circuiting before the subprocess ever ran. Now it spawns `chewy_product_scraper.py --url`, with `ADSP_BROWSER_WS_URL` to reuse the worker's open browser. New aggregation step: `tools/build_shopify_jsonl.py` does `dedupe_products_across_pages` and emits the final Shopify JSONL. |
| `90ec3ff` | Add `tools/prepare_rescrape.py` helper for URL extraction | Generate `tools/urls_all.txt` (3115 deduped URLs from existing normalized files) + `tools/urls_pilot.txt` (deterministic 20-URL random sample, seed=42). Generated URL files are gitignored. Motivation: we needed a clean URL list to seed the re-scrape job after discovering that legacy 5-digit variant IDs in the existing normalized files had been delisted from Chewy. |
| `9182991` | Filter out Chewy-exclusive compounded products from enrich input | The previous test batch of 20 happened to be all compounded capsules (215 such products in the dataset). The user doesn't sell Chewy-exclusive compounded meds on Shopify. The filter was added to the now-deprecated `chewy_enrich.py` CLI; it does not apply to the new scraper flow (no harm in re-scraping them — they just won't be used). |

### Session 1 (2026-05-15 morning) — Pipeline refactor & multi-worker

| Commit | What | Why |
|---|---|---|
| `d8c184f` | Strip DB profile tracking — worker holds profile_id in memory only | User wanted simpler model. New helpers `template_for_slot`, `create_profile_via_api`, `delete_profile_via_api`, `switch_profile_to_local_via_api`, `switch_profile_to_env_proxy_via_api` — pure AdsPower API, no DB. `_worker_coro` rewritten: parse template once, init profile_id from .env (or create new), on white-screen delete+create new, on proxy-dead switch SAME profile to no_proxy. All in-memory; .env never modified. Removed startup DB syncs (sync_profile_templates_to_db, restore_runtime_local_slots_from_env, rebuild_slots_with_env_proxy_changes, release_stale_template_slots, get_worker_slot_status, get_template, consume_rebuild_request, mark_template_white_screen, ensure_slot_profile). Scrape pipeline's DB profile pool is unchanged. |
| `fadbcbd` | Escalate HTTP 429/403/503 to WhiteScreenException | Worker was hammering throttled profile because variant API errors returned None instead of raising. Now `fetch_next_data_json` raises on `PROFILE_BLOCKED_STATUSES`; `enrich_variants_from_api` + `recover_*_for_variant` re-raise (was being swallowed by generic `except Exception`). Worker's existing white-screen handler then rebuilds the profile. |
| `d10fbef` | Fix SQL: load proxy creds from `.env` not DB | `restore_runtime_local_slots_from_env` tried to SELECT non-existent columns (`proxy_username`, `proxy_password`). |
| `d6f7273` | Backfill new fields in enrich + fix proxy-dead orphan-profile loop | OLD normalized files had no `source_entry_id` / `out_of_stock` / `transition_instructions`. Enrich now follows Chewy's 301 from partNumber URL, matches by partNumber, backfills canonical entry_id + stock fields + content. Also fixed `restore_runtime_local_slots_from_env` to toggle `proxy_soft` instead of delete+create (was leaking orphan profiles every CLI start). And `_start_browser_for_slot` no longer silently rebuilds env-managed profiles when "does not exist". |
| `6b8b23e` | Proxy-dead recovery + env-first profile resolution (made by a parallel agent session) | Detect proxy errors, swap slot to no_proxy via `switch_slot_to_local_runtime` (no delete). Workers resolve profile from `.env` first, DB fallback. Compact error logs (no full traceback per page). |
| `e2d611d` | Fix parallel enrich KeyError + add max_attempts cap | `counters = defaultdict(int)` so any key process_product touches works (previous bug: `'variants_missing_price_before'` KeyError caused stuck pid at attempt 97). Added `--max-attempts 5` default in claim function. |
| `82c8c06` | Multi-worker enrichment: DB claim lock + parallel runner | New `claim_next_enrichment_pid` (BEGIN IMMEDIATE atomic), `release_enrichment_claim`, `recover_stale_enrichment_states`. New `parallel_enrich_runner.py` mirroring the scrape side's `parallel_resumable_runner.py` pattern (N async coroutines, one CW slot each). |
| `f3abfb7` | Pipeline refactor: entryID URLs, per-variant fetch, splitting, OOS, JSONL streaming, DB resume | Massive refactor. Decode Apollo entry_id, build URLs with it, fetch per-variant (1 per pid instead of 1 per flavor), add TRANSITION_INSTRUCTIONS, generalize split by all non-size defining attrs, out_of_stock detection, cross-page dedupe, title augmentation, JSONL streaming output, chewy_enrichment_state DB table + helpers for resume, removed all `moe/` filtering. |
| `b67cfd5`, `9197711` | Pre-session: original Next.js API enrichment baseline | (Existing before this session.) |

---

## 11. Open Questions / Pending Work

1. **Run full 3115 re-scrape on VPS.** User has the commands (§12). Pilot of 20 verified shape; local 10-URL smoke verified runtime behavior including resume after cancel. Expected outcome: ~95–98% success rate, a small number of delisted-product fails (acceptable). After it finishes: run `tools/build_shopify_jsonl.py` to get the Shopify-import JSONL.
2. **Shopify CSV import — DONE.** `tools/build_shopify_csv.py` reads `output/grouped_products/`, runs dedupe, resolves category, writes `output/shopify_export/shopify_products.csv` (~28MB, 3,643 unique products × 9,100 variants) + `shopify_inventory.csv` (rescraping-resync flow) + `shopify_dedupe_log.json`. Companion: `tools/build_shopify_collections.py` (Smart Collections ruled by `category:<segment>` tags) and `tools/category_resolver.py` (breadcrumb cache → title-regex fallback, 28 distinct categories). Re-run after each rescrape.

3. **Scraper now captures `Product.breadcrumbs` (commit pending).** Previously the breadcrumb extraction in `parse_apollo_product` scanned only top-level Apollo state keys; Chewy stores breadcrumbs as an inline list inside the `Product:<id>` node, so `category_path` came back empty. Fix: after locating `product_node`, also iterate `product_node.get("breadcrumbs")` for inline Breadcrumb objects. After the next full rescrape, `output/normalized_products/*.json` will have populated `category_path` and `category_resolver.py` will prefer it over title regex automatically (no code change needed in the resolver).
3. **White-screen `.env` mismatch (carryover).** `auto_rebuild_profile` creates a new profile_id but `.env` still points at the old one. Worker auto-creates a fresh profile on each run (since `d8c184f`), so this is not blocking — but `.env` drifts away from reality. Decide: (a) auto-write new id back, (b) accept the drift and document.
4. **`chewy_enrich.py` and `parallel_enrich_runner.py` are formally deprecated.** Decide when to delete them outright vs. keep as compatibility shims. The helpers they re-export now live in `chewy_next_json_extractor.py`. Leaving them in for now buys backward compatibility for any in-flight scripts.
5. **~~Stale orphan profiles on AdsPower~~** — RESOLVED in `efa4b7c`. `auto_rebuild_profile` now cleans up all orphans sharing the slot's `display_name` before creating a new one. After pulling the fix, run the one-liner in §12 ("Cleanup orphan profiles") once to reclaim the slots already accumulated on the user's account.
6. **`output/scraper_jobs.db`** (separate file from root `scraper_jobs.db`) — 8.1 MB, untouched. Possibly older backup. Verify before purging.

---

## 12. Operational Runbook (VPS)

### Cleanup orphan profiles (one-time after pulling `efa4b7c`)

Before the fix, every white-screen rebuild leaked an AdsPower profile under
the slot's `display_name` (CW_1 / CW_2 / CW_3). Run this once after pull to
reclaim the leaked plan slots (the fix runs the same cleanup automatically
on every subsequent rebuild, so this is only catching up):

```powershell
python -c @"
import time
import adsp_profile_recovery_manager as r
for slot in ('CW_1','CW_2','CW_3'):
    print(f'Rebuilding {slot}...')
    result = r.auto_rebuild_profile(slot, reason='post_fix_cleanup', delay_seconds=0, delete_old_profile=True)
    print(f'  success={result.get(\"success\")} new={result.get(\"new_profile_id\")}')
    time.sleep(3)
"@

# Verify: should be exactly one profile per slot
python -c @"
import requests, config
profiles = requests.get(f'{config.ADSPOWER_API_BASE}/api/v1/user/list', params={'page_size': 100}, timeout=15).json()['data']['list']
for name in ('CW_1','CW_2','CW_3'):
    items = [p['user_id'] for p in profiles if p.get('name')==name]
    print(f'{name}: {items} (count={len(items)})')
print(f'Total AdsPower profiles: {len(profiles)}')
"@
```

### Run order — full re-scrape (3115 URLs)

```powershell
# 1. Pull latest (commits from session 2: 9182991, 90ec3ff, a5cb64e, 9aa09cb,
#    f59626a, 2e4374d, efa4b7c)
git pull origin main

# 2. Generate URL list FROM existing normalized files (must run BEFORE deleting them)
python tools\prepare_rescrape.py
# Writes tools/urls_all.txt (3115) + tools/urls_pilot.txt (20, seed=42).

# 3. Backup output + DB (3 output folders + sqlite)
$ts = Get-Date -Format "yyyyMMdd_HHmmss"
Compress-Archive `
    -Path output\normalized_products\*,output\grouped_products\*,output\validation\* `
    -DestinationPath "output\backup_pre_full_rescrape_$ts.zip" -Force
Copy-Item scraper_jobs.db "scraper_jobs.db.bak_$ts"

# 4. Clean output (must delete ALL THREE — runner's existing_json_output_ok
#    checks grouped + validation; deleting only normalized doesn't force re-scrape)
Remove-Item output\normalized_products\*.json -Force
Remove-Item output\grouped_products\*.json -Force
Remove-Item output\validation\*.json -Force

# 5. Create the job and note the job_id from the output
python resumable_scraper_runner.py create --name fullrescrape_3115 --urls tools\urls_all.txt
# {"job_id": "job_YYYYMMDD_HHMMSS_xxxx", "total_urls": 3115}

# 6. Start scraping (replace <job_id>)
python resumable_scraper_runner.py start --job-id <job_id> --workers 3

# 7. After the job finishes, aggregate into the final Shopify JSONL
python tools\build_shopify_jsonl.py
# -> output\shopify_import_<ts>.jsonl + output\shopify_import_dedupe_log_<ts>.json
```

### Smoke check on the resulting data

```powershell
# Job summary
python resumable_scraper_runner.py status --job-id <job_id>

# Status counts directly from DB
python -c "import sqlite3; c=sqlite3.connect('scraper_jobs.db'); r=c.execute(\"SELECT status, COUNT(*) FROM scrape_job_items WHERE job_id='<job_id>' GROUP BY status\").fetchall(); print(dict(r))"

# Files on disk
Get-ChildItem output\grouped_products\*.json | Measure-Object | Select-Object Count

# Inspect a representative product (any food pid is a good probe)
Get-ChildItem output\grouped_products\chewy_grouped_*.json | Select-Object -First 1 | ForEach-Object {
  python -c "import json; d=json.load(open(r'$($_.FullName)',encoding='utf-8')); p=d['products'][0]; v=p['variants'][0]; print('title:',p['title'][:60]); print('entry_id:',v.get('source_entry_id')); print('inv_policy:',v.get('shopify_inventory_policy')); print('transition:',bool(p.get('transition_instructions'))); print('GA:',bool(p.get('guaranteed_analysis'))); print('feeding:',bool(p.get('feeding_instructions')))"
}
```

### Crash / Ctrl+C recovery

```powershell
python resumable_scraper_runner.py resume --job-id <job_id> --workers 3
```

Resume auto-handles:
- `running` items orphaned by the previous run → reset to `pending` (`mark_orphan_running_items`)
- `done` items with all 3 output files → skipped via `existing_json_output_ok` (no subprocess)
- `failed` items → retried if `attempts < max_attempts` (default 3)
- Multi-worker safe via `BEGIN IMMEDIATE` atomic claim

Verified end-to-end on local 10-URL demo this session.

### Fresh install (first time on a new machine)

```powershell
git clone https://github.com/hyauns/ChewyPetCo.git
cd ChewyPetCo
pip install -r requirements.txt
playwright install chromium
# create .env with ADSP_CW_{1,2,3}_PROFILE_ID + ADSP_CW_{1,2,3}_PROXY_URL
python -c "import job_store; job_store.init_db()"
```

### `.env` template

```env
ADSPOWER_API_BASE=http://127.0.0.1:50325
ADSPOWER_PROFILE_ID=<fallback_profile>          # used by single-worker mode
ADSP_PROFILE_RECOVERY_ENABLED=true

ADSP_CW_1_PROFILE_ID=<profile_id_1>
ADSP_CW_1_PROXY_URL=socks5h://USER:PASS@host:port
ADSP_CW_2_PROFILE_ID=<profile_id_2>
ADSP_CW_2_PROXY_URL=socks5h://USER:PASS@host:port
ADSP_CW_3_PROFILE_ID=<profile_id_3>
ADSP_CW_3_PROXY_URL=socks5h://USER:PASS@host:port
```

---

## 13. Quick-reference: where to look up what

| Question | Where to look |
|---|---|
| **Where's the canonical scrape→Shopify pipeline?** | `chewy_product_scraper.scrape_single_product` (in-process; all phases inline). |
| **How does the runner invoke the scraper?** | `resumable_scraper_runner.process_single_item` → `subprocess.Popen([..., 'chewy_product_scraper.py', '--url', url], env=ADSP_BROWSER_WS_URL+ADSPOWER_PROFILE_ID)`. |
| How does a variant URL get built? | `chewy_next_json_extractor.parse_apollo_product` ("variant_url"). Uses entry_id. |
| How is a variant decided OOS? | `derive_stock_fields()` in `chewy_next_json_extractor.py`. Multiple signals. |
| How do scrape workers claim work? | `job_store.claim_next_item` — BEGIN IMMEDIATE. (Old `claim_next_enrichment_pid` is for the deprecated `chewy_enrichment_state` table.) |
| How does white-screen rebuild a profile? | Scraper emits `[WHITE_SCREEN_RESULT]` marker → `resumable_scraper_runner.process_single_item` (~line 720) parses it, calls `quarantine_profile` + `mark_template_white_screen`, marks item pending → `parallel_resumable_runner._worker_loop` sees `slot.status == 'rebuilding'` next iteration → `auto_rebuild_profile`. |
| How does the scraper recognize proxy-dead errors? | `chewy_product_scraper.main()` — `PROXY_DEAD_TOKENS` set (ERR_CONNECTION_*, SOCKS, TUNNEL, TIMED_OUT, NETWORK_CHANGED) caught around `page.goto` and `scrape_single_product`. Emits the same white-screen marker. |
| How does proxy come back from local? | `adsp_profile_recovery_manager.restore_runtime_local_slots_from_env` — toggle `proxy_soft`. |
| Where is `.env` parsed? | `config.py` `_load_local_env()`. Templates re-parse via `load_profile_templates_from_config()`. |
| Where are output files written? | `chewy_product_scraper.main()` — three files per pid: `output/normalized_products/`, `output/grouped_products/`, `output/validation/`. |
| How is split-by-attribute done? | `split_product_by_flavor` (function name kept for backward compat). Uses `is_variant_axis_attr` to classify each defining attribute. |
| How is cross-page dedupe done? | `dedupe_products_across_pages` — fingerprint by sorted variant entry_ids. Invoked from `tools/build_shopify_jsonl.py`. |
| How does resume skip done items? | `resumable_scraper_runner.existing_json_output_ok` checks grouped + validation files. If both pass quality, item marked done without spawning subprocess. |
| Where does the Shopify-import feed come from? | `python tools/build_shopify_jsonl.py` after the scrape job finishes. Reads grouped, dedupes, writes `output/shopify_import_{ts}.jsonl`. |
