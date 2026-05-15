# Chewy Scraper — Current Context

**Updated:** 2026-05-15 (session ending at commit `d8c184f`)
**Repo:** https://github.com/hyauns/ChewyPetCo
**Purpose of this file:** Single source of truth for a future Claude session to pick up where this one left off, without re-reading the full chat. Read this first.

---

## 1. TL;DR

This project scrapes Chewy.com product pages and enriches the resulting JSON to be import-ready for Shopify. The dataset is ~3,115 products already scraped under `output/normalized_products/chewy_{pid}.json` (NOT in git, must be rsynced to VPS). Enrichment runs on top of these files and writes a stream of Shopify-shaped products to a JSONL file.

**Current state of the code:**
- Pipeline refactored: per-variant API fetch, entryID-aware URLs, generalized split by defining attributes, out-of-stock detection, transition-instructions, cross-page dedupe, title augmentation, `moe/` images treated as real.
- Output is streaming JSONL — each enriched product is flushed + fsynced before the next is attempted (crash-safe).
- DB-backed resume via `chewy_enrichment_state` table.
- Multi-worker (`--parallel --workers N`) with atomic DB claim (BEGIN IMMEDIATE), one CW slot per worker.
- Proxy-dead recovery toggles `proxy_soft` to `no_proxy` on the SAME profile (no delete+create). Only white-screen triggers full profile rebuild.
- OLD normalized files (pre-refactor) get upgraded on-the-fly during enrich via redirect-follow + backfill of `source_entry_id` / `out_of_stock` / `stock_reason` / `shopify_inventory_policy` / `transition_instructions`.

**What's running on VPS** (`C:\Users\Administrator\Downloads\ChewyPetCo`):
- Latest commit: `d10fbef`.
- User runs: `python chewy_enrich.py --input output/normalized_products --mode all --parallel --workers 3 --limit 20 --force-reenrich` (test batch first).
- Profile IDs and proxies live in `.env` only (never in git).

---

## 2. Pipeline (high level)

```
Source pages (URLs)
  |
  v  chewy_product_scraper.py / category_scraper.py (already done for 3,115)
output/normalized_products/chewy_{pid}.json   <- source of truth, DO NOT MODIFY
  |
  v  chewy_enrich.py  (--parallel --workers N --input output/normalized_products)
       |
       +- parse_apollo_product  (per page, if rescraping)
       +- enrich_variants_from_api  (1 fetch per variant entryID)
       |     +- follow Chewy 301 (partNumber URL -> canonical /dp/{entryID})
       |     +- backfill: source_entry_id, variant_url, stock fields, transition
       |     +- fill: description, ingredients, GA, feeding, calorie, images
       +- split_product_by_flavor  (renamed semantics: split by all non-size defining attrs)
       +- JSONL streaming write + chewy_enrichment_state UPSERT
  |
  v  result_batch_all_{ts}.jsonl  <- one Shopify product per line
       (optionally) dedupe_products_across_pages -> final Shopify import set
```

---

## 3. File Map

### Production code (commit, run on VPS)
| File | Role |
|---|---|
| `chewy_next_json_extractor.py` (~1900 LoC) | Core extractor. Parses Apollo, runs enrich, splits, dedupes. Most logic lives here. |
| `chewy_enrich.py` | CLI entry. Single-worker `run_pipeline()` + dispatches to `parallel_enrich_runner` on `--parallel`. |
| `parallel_enrich_runner.py` | Multi-worker async runner. N coroutines, atomic claim, shared JSONL, white-screen + proxy-dead handling. |
| `chewy_product_scraper.py` | Scrape single product (or batch via `--job-id`). |
| `resumable_scraper_runner.py` | Production scrape runner — used by `chewy_product_scraper.py --job-id`. |
| `parallel_resumable_runner.py` | Multi-worker SCRAPE runner (sister of parallel_enrich_runner, for the scrape phase). |
| `category_scraper.py` / `category_discovery*.py` | Discover product URLs by category. |
| `adspower.py` | AdsPower local API client (start/stop/get_ws_endpoint). |
| `adsp_profile_pool_manager.py` | Profile pool + white-screen detection. |
| `adsp_profile_recovery_manager.py` | Slot template management + auto_rebuild_profile + switch_slot_to_local_runtime + restore_runtime_local_slots_from_env. |
| `job_store.py` | SQLite layer. Schema, init, all helpers. Includes `chewy_enrichment_state` table + claim primitives. |
| `config.py` | Reads `.env`. Defines `ADSPOWER_PROFILE_ID`, `ADSP_PROFILE_POOL_IDS`, slot config, timeouts. |
| `job_exporter.py` | Export normalized -> grouped products (legacy, may need update for new schema). |

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

```bash
# Single-worker (no parallel)
python chewy_enrich.py --input output/normalized_products --mode all

# Multi-worker (recommended for 3,115 products)
python chewy_enrich.py --input output/normalized_products \
    --mode all \
    --parallel --workers 3 \
    --limit 20 \
    --force-reenrich \
    --max-attempts 5

# Sample mode (legacy, reads selected_products.json by category A/B/C)
python chewy_enrich.py --sample test_runs/.../selected_products.json --category A
```

**Flags:**
- `--mode {content,price,image,all}` — what to enrich; `all` recommended.
- `--limit N` — first N pids alphabetically.
- `--force-reenrich` — `reset_enrichment_state` for each input pid before processing (clears prior `ok`/`failed`).
- `--parallel` — use `parallel_enrich_runner`.
- `--workers N` — number of CW slots to use (capped by `MAX_TEMPLATE_SLOTS`).
- `--max-attempts N` — drop pids that hit N failures (default 5; 0 = unlimited).

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

1. **Test 20-product batch on VPS** — user is running `--limit 20 --force-reenrich`. Verify output has `source_entry_id`, `out_of_stock`, `transition_instructions` populated. (Smoke command provided in chat history.)
2. **White-screen `.env` mismatch:** when `auto_rebuild_profile` creates a new profile_id, `.env` still points at the old (deleted) id. Currently the worker stops with a clear error and asks user to update `.env`. **A nicer UX would be**: either (a) auto-write the new id back to `.env`, or (b) revert to DB-first resolution after white-screen rebuilds. Decision pending.
3. **Shopify export step (CSV/JSON):** JSONL is produced but Shopify import format conversion is not yet wired. Likely a separate `chewy_to_shopify.py` script that reads the JSONL + applies `dedupe_products_across_pages` + emits Shopify CSV.
4. **Stale orphan profiles on AdsPower** from the pre-fix loop. User may have N orphan profiles per slot. Manual cleanup via AdsPower UI may be needed. (Not destructive — they just exist unused.)
5. **`output/scraper_jobs.db`** (separate file from root `scraper_jobs.db`) — 8.1 MB, untouched by us. Possibly older backup. User should verify before purging.

---

## 12. Operational Runbook (VPS)

### Fresh install
```powershell
git clone https://github.com/hyauns/ChewyPetCo.git
cd ChewyPetCo
pip install -r requirements.txt
playwright install chromium
```

### `.env` template (fill in)
```env
ADSPOWER_API_BASE=http://127.0.0.1:50325
ADSPOWER_PROFILE_ID=<fallback_profile>          # used by single-worker mode
ADSP_PROFILE_RECOVERY_ENABLED=true

# 3 CW slot profiles + their proxies (socks5h example)
ADSP_CW_1_PROFILE_ID=<profile_id_1>
ADSP_CW_1_PROXY_URL=socks5h://USER:PASS@host:port
ADSP_CW_2_PROFILE_ID=<profile_id_2>
ADSP_CW_2_PROXY_URL=socks5h://USER:PASS@host:port
ADSP_CW_3_PROFILE_ID=<profile_id_3>
ADSP_CW_3_PROXY_URL=socks5h://USER:PASS@host:port
```

### Data sync from old machine
```powershell
# From old machine — these are NOT in git
rsync (or scp) output/normalized_products/  ->  VPS:output/normalized_products/
rsync (or scp) output/cache/  ->  VPS:output/cache/             # optional, speeds up enrich
# Initialize fresh scraper_jobs.db on VPS (or transfer)
python -c "import job_store; job_store.init_db()"
```

### Run order (typical)
```powershell
# 1. Pull latest
git pull origin main

# 2. (Optional) clear enrichment state from any prior run
python -c "import sqlite3; c=sqlite3.connect('scraper_jobs.db'); n=c.execute('DELETE FROM chewy_enrichment_state').rowcount; c.commit(); c.close(); print(f'cleared {n} rows')"

# 3. Test 20 products
python chewy_enrich.py --input output/normalized_products --mode all --parallel --workers 3 --limit 20 --force-reenrich

# 4. Verify (smoke check the latest JSONL)
$jsonl = Get-ChildItem output/enrichment_runs/result_batch_all_*.jsonl | Sort-Object LastWriteTime | Select-Object -Last 1
python -c "import json; d=json.loads(open(r'$($jsonl.FullName)','r',encoding='utf-8').readline()); v=d['products'][0]['variants'][0]; print('entry_id:',v.get('source_entry_id')); print('out_of_stock:',v.get('out_of_stock')); print('inv_policy:',v.get('shopify_inventory_policy'))"

# 5. Full run (remove --limit)
python chewy_enrich.py --input output/normalized_products --mode all --parallel --workers 3
```

### Crash recovery
Just re-run the same command — DB state ensures `ok` pids are skipped. Stale `in_progress` pids (worker crashed) are reset on startup.

### Updating `.env` after white-screen rebuild
```powershell
# 1. Find the new profile id in AdsPower UI (look for the most recent CW_X profile)
# 2. Edit .env, replace ADSP_CW_X_PROFILE_ID with the new id
# 3. Restart the run
```

### Common quick checks
```powershell
# Queue status
python -c "import job_store; print(job_store.enrichment_state_summary())"

# How many products in latest JSONL?
(Get-Content (Get-ChildItem output/enrichment_runs/result_batch_all_*.jsonl | Sort LastWriteTime | Select -Last 1)).Count

# Slot status
python -c "import adsp_profile_recovery_manager as r; r.sync_profile_templates_to_db(); print([r.get_template(s) for s in ['CW_1','CW_2','CW_3']])"
```

---

## 13. Quick-reference: where to look up what

| Question | Where to look |
|---|---|
| How does a variant URL get built? | `chewy_next_json_extractor.parse_apollo_product` ("variant_url"). Uses entry_id. |
| How does enrich decide a variant is OOS? | `derive_stock_fields()` in `chewy_next_json_extractor.py`. Multiple signals. |
| How do workers claim work? | `job_store.claim_next_enrichment_pid` — BEGIN IMMEDIATE. |
| How does white-screen rebuild a profile? | `adsp_profile_recovery_manager.auto_rebuild_profile(delete_old_profile=True)`. |
| How does proxy-dead switch to local? | `adsp_profile_recovery_manager.switch_slot_to_local_runtime` — no delete. |
| How does proxy come back from local? | `adsp_profile_recovery_manager.restore_runtime_local_slots_from_env` — toggle proxy_soft. |
| Where is `.env` parsed? | `config.py` `_load_local_env()`. Templates re-parse via `load_profile_templates_from_config()`. |
| Where is the JSONL line written? | `parallel_enrich_runner._worker_coro` "Stream to JSONL" block (or `chewy_enrich.run_pipeline` single-worker path). |
| How is split-by-attribute done? | `split_product_by_flavor` (function name kept for backward compat). Uses `is_variant_axis_attr` to classify each defining attribute. |
| How is cross-page dedupe done? | `dedupe_products_across_pages` — fingerprint by sorted variant entry_ids. |
