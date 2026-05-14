"""Controlled multi-worker runner for Chewy enrichment.

Mirrors parallel_resumable_runner.py but adapted for the async enrich pipeline:
  - N worker coroutines in a single asyncio event loop (each owns one CW slot).
  - Atomic pid claim via job_store.claim_next_enrichment_pid (BEGIN IMMEDIATE).
  - Shared JSONL output stream with asyncio.Lock for race-safe append.
  - White-screen rebuild: release pid back to pending, rebuild profile via
    adsp_profile_recovery_manager.auto_rebuild_profile (uses .env proxy), restart
    browser, continue.

The runner does NOT change the pipeline content / extractor logic — it just
parallelises chewy_enrich.process_product over the worker pool.
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

from rich.console import Console
from playwright.async_api import async_playwright

import config
import adspower
import adsp_profile_recovery_manager as recovery
import job_store
import chewy_enrich

console = Console()


def _get_slot_profile(slot_id: str) -> str | None:
    row = recovery.get_template(slot_id)
    if not row:
        return None
    pid = row.get("adspower_profile_id")
    return str(pid) if pid else None


async def _start_browser_for_slot(p_obj, slot_id: str, worker_id: str):
    """Start AdsPower profile for this slot and connect Playwright.

    Returns (browser, page, profile_id) on success or (None, None, None) on
    failure. If the profile is missing, attempts an auto-rebuild via the
    recovery manager (which honours .env proxy config).
    """
    profile_id = _get_slot_profile(slot_id)
    if not profile_id:
        console.print(f"[red][{worker_id}] No profile assigned to {slot_id}[/red]")
        return None, None, None

    for attempt in range(2):
        try:
            profile_data = adspower.start_profile(profile_id)
            ws_url = adspower.get_ws_endpoint(profile_data)
            browser = await p_obj.chromium.connect_over_cdp(ws_url)
            ctx = browser.contexts[0]
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            console.print(f"[green][{worker_id}] Browser up on {slot_id}/{profile_id}[/green]")
            return browser, page, profile_id
        except Exception as e:
            err = str(e).lower()
            if attempt == 0 and ("does not exist" in err or "not exist" in err):
                console.print(f"[yellow][{worker_id}] Profile {profile_id} missing — rebuilding via .env proxy...[/yellow]")
                res = recovery.auto_rebuild_profile(
                    slot_id, reason=f"profile_missing_{profile_id}",
                    delay_seconds=0, delete_old_profile=False,
                )
                if res.get("success"):
                    profile_id = res.get("new_profile_id") or _get_slot_profile(slot_id)
                    continue
                console.print(f"[red][{worker_id}] Rebuild failed: {res.get('message')}[/red]")
                return None, None, None
            console.print(f"[red][{worker_id}] start_profile/connect failed: {e}[/red]")
            return None, None, profile_id
    return None, None, profile_id


async def _stop_browser(profile_id: str | None, worker_id: str):
    if not profile_id:
        return
    try:
        adspower.stop_profile(profile_id)
    except Exception:
        pass


async def _worker_coro(*,
                      p_obj,
                      worker_id: str,
                      slot_id: str,
                      mode: str,
                      normalized_dir: Path,
                      jsonl_file,
                      jsonl_lock: asyncio.Lock,
                      jsonl_path: Path,
                      counters: dict,
                      counter_lock: asyncio.Lock) -> dict:
    """One worker: claim → process → write → repeat. Handles white-screen rebuild."""
    console.print(f"[cyan][{worker_id}] starting on {slot_id}[/cyan]")
    processed = 0
    enrich_errors = 0

    # Ensure slot has a profile (auto-rebuild via .env if missing)
    ensure = recovery.ensure_slot_profile(slot_id, delay_seconds=0)
    if not ensure.get("success"):
        console.print(f"[red][{worker_id}] Slot {slot_id} unavailable: {ensure.get('message')}[/red]")
        return {"worker_id": worker_id, "slot_id": slot_id, "processed": 0,
                "status": "slot_unavailable"}

    browser, page, profile_id = await _start_browser_for_slot(p_obj, slot_id, worker_id)
    if not browser:
        return {"worker_id": worker_id, "slot_id": slot_id, "processed": 0,
                "status": "browser_start_failed"}

    try:
        while True:
            # Check if slot was disabled / requires rebuild mid-run
            slot = recovery.get_template(slot_id)
            if not slot or slot.get("status") in {"disabled", "rebuild_failed"}:
                console.print(f"[red][{worker_id}] Slot {slot_id} stopped: {slot.get('status') if slot else 'missing'}[/red]")
                break
            if slot.get("status") == "rebuilding" or recovery.consume_rebuild_request(slot_id):
                console.print(f"[yellow][{worker_id}] Slot {slot_id} marked rebuilding — applying[/yellow]")
                await _stop_browser(profile_id, worker_id)
                res = recovery.auto_rebuild_profile(slot_id, reason="slot_marked_rebuilding", delay_seconds=0)
                if not res.get("success"):
                    console.print(f"[red][{worker_id}] Rebuild failed: {res.get('message')}[/red]")
                    break
                browser, page, profile_id = await _start_browser_for_slot(p_obj, slot_id, worker_id)
                if not browser:
                    break
                continue

            # Claim next pid atomically
            item = job_store.claim_next_enrichment_pid(
                worker_id=worker_id, profile_slot_id=slot_id, retry_failed=True
            )
            if not item:
                console.print(f"[cyan][{worker_id}] No more pids to claim — exiting[/cyan]")
                break

            pid = item["product_id"]
            attempt = item["attempt_count"]
            console.print(f"\n[bold][{worker_id}] --- Claimed {pid} (attempt {attempt}) on {slot_id}/{profile_id} ---[/bold]")

            # Snapshot counters BEFORE so we can compute the delta
            c0 = {k: counters.get(k, 0) for k in (
                "variants_enriched", "wrong_product_api_rejected", "slug_mismatch",
                "variants_price_recovered", "variants_image_recovered",
            )}

            try:
                grouped = await chewy_enrich.process_product(pid, normalized_dir, page, counters, mode)
            except chewy_enrich.WhiteScreenException:
                console.print(f"[bold red][{worker_id}] WHITE SCREEN on {pid} — releasing claim, rebuilding profile[/bold red]")
                job_store.release_enrichment_claim(pid, reset_to="pending")
                await _stop_browser(profile_id, worker_id)
                # Mark white-screen on template + trigger rebuild
                recovery.mark_template_white_screen(slot_id, profile_id, "white_screen_in_enrich")
                res = recovery.auto_rebuild_profile(
                    slot_id, reason="white_screen_in_enrich",
                    delay_seconds=0, delete_old_profile=True,
                )
                if not res.get("success"):
                    console.print(f"[red][{worker_id}] Rebuild after white-screen failed: {res.get('message')}[/red]")
                    break
                browser, page, profile_id = await _start_browser_for_slot(p_obj, slot_id, worker_id)
                if not browser:
                    break
                continue
            except Exception as e:
                console.print(f"[red][{worker_id}] Error on {pid}: {e}[/red]")
                traceback.print_exc()
                job_store.mark_enrichment_failed(
                    pid, error_type=type(e).__name__, error_message=str(e)[:1000]
                )
                enrich_errors += 1
                continue

            if grouped is None:
                job_store.mark_enrichment_failed(
                    pid, error_type="no_grouped_result",
                    error_message="process_product returned None"
                )
                console.print(f"[red][{worker_id}] [FAIL] {pid}[/red]")
                continue

            # Stream to JSONL (race-safe append + flush + fsync)
            line = json.dumps(grouped, ensure_ascii=False)
            async with jsonl_lock:
                jsonl_file.write(line + "\n")
                jsonl_file.flush()
                try:
                    os.fsync(jsonl_file.fileno())
                except OSError:
                    pass

            # Per-product delta
            products_n = len(grouped.get("products", []))
            variants_n = sum(len(p.get("variants", [])) for p in grouped.get("products", []))
            d_enriched = counters.get("variants_enriched", 0) - c0["variants_enriched"]
            d_wrong = counters.get("wrong_product_api_rejected", 0) - c0["wrong_product_api_rejected"]
            d_slugm = counters.get("slug_mismatch", 0) - c0["slug_mismatch"]
            d_price = counters.get("variants_price_recovered", 0) - c0["variants_price_recovered"]
            d_image = counters.get("variants_image_recovered", 0) - c0["variants_image_recovered"]

            job_store.mark_enrichment_ok(
                pid, output_path=str(jsonl_path),
                product_count=products_n, variant_count=variants_n,
                enriched_count=d_enriched,
                wrong_product_rejected=d_wrong,
                slug_mismatch=d_slugm,
            )

            async with counter_lock:
                counters["products_processed"] = counters.get("products_processed", 0) + 1

            console.print(
                f"[bold green][{worker_id}] [OK] {pid} - "
                f"{products_n} products / {variants_n} variants  "
                f"(enriched={d_enriched} wrong={d_wrong} slug_mm={d_slugm})[/bold green]"
            )
            for sp in grouped.get("products", [])[:3]:
                t = (sp.get("title") or "")[:80]
                v_n = len(sp.get("variants", []))
                oos = " [OOS]" if sp.get("out_of_stock") else ""
                console.print(f"[green][{worker_id}]      |- {t}  ({v_n}v){oos}[/green]")
            if len(grouped.get("products", [])) > 3:
                console.print(f"[green][{worker_id}]      |- ... +{len(grouped['products']) - 3} more[/green]")

            processed += 1
            await asyncio.sleep(random.uniform(1.5, 3.0))
    finally:
        await _stop_browser(profile_id, worker_id)

    return {
        "worker_id": worker_id, "slot_id": slot_id,
        "processed": processed, "errors": enrich_errors,
        "status": "stopped",
    }


async def run_parallel_enrichment(*,
                                  product_ids: list,
                                  normalized_dir: Path,
                                  output_dir: Path,
                                  mode: str,
                                  label: str,
                                  workers: int = 3,
                                  source_urls: dict | None = None,
                                  force_reenrich: bool = False) -> dict:
    """Run enrichment across N workers.

    Each worker is bound to one CW slot (CW_1, CW_2, CW_3 — first `workers` of them).
    Workers share a single JSONL output file (race-safe append) and claim pids
    atomically from chewy_enrichment_state.
    """
    console.print(f"[bold]Parallel enrichment ({label}) — {len(product_ids)} pids, mode={mode}, workers={workers}[/bold]")

    job_store.init_db()

    # Reset any in_progress pids stranded by prior crashes.
    recovered = job_store.recover_stale_enrichment_states(stale_minutes=30)
    if recovered:
        console.print(f"[yellow]Recovered {recovered} stale in_progress pid(s) from previous run[/yellow]")

    # Resume mode: drop already-ok pids unless --force-reenrich.
    if force_reenrich:
        console.print("[yellow]--force-reenrich: clearing existing state for these pids[/yellow]")
        for pid in product_ids:
            job_store.reset_enrichment_state(pid)
        candidates = list(product_ids)
    else:
        candidates = [pid for pid in product_ids if not job_store.is_enrichment_done(pid)]
        skipped = len(product_ids) - len(candidates)
        if skipped:
            console.print(f"[cyan]Resume: {skipped} pids already ok in DB, queueing {len(candidates)} remaining[/cyan]")

    if not candidates:
        console.print("[green]Nothing to do.[/green]")
        return {"products_processed": 0, "skipped_done": len(product_ids)}

    # Seed pending rows so claim_next_enrichment_pid can find them.
    seeded = job_store.seed_enrichment_state(candidates, source_urls=source_urls)
    console.print(f"[cyan]Seeded {seeded} new pending row(s); queue size = {job_store.count_pending_enrichment()}[/cyan]")

    # Ensure profiles + rebuild if .env proxies changed (mirrors parallel_resumable_runner)
    if hasattr(config, "reload_from_env_file"):
        config.reload_from_env_file(override=True)
    rebuilt = recovery.rebuild_slots_with_env_proxy_changes(delay_seconds=0)
    if rebuilt.get("rebuilt_count"):
        console.print(f"[yellow]Rebuilt {rebuilt['rebuilt_count']} slot(s) for .env proxy changes[/yellow]")
    recovery.sync_profile_templates_to_db()
    recovery.restore_runtime_local_slots_from_env(delay_seconds=0)
    recovery.release_stale_template_slots()

    # Pick the first N CW slots
    workers = max(1, min(workers, recovery.MAX_TEMPLATE_SLOTS))
    slots = recovery.get_worker_slot_status(workers)
    runnable = [s for s in slots if s.get("status") != "disabled"]
    if not runnable:
        console.print("[red]No runnable CW slots — aborting[/red]")
        return {"products_processed": 0, "error": "no_runnable_slots"}

    # Open shared JSONL
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    jsonl_path = output_dir / f"result_{label}_{ts}.jsonl"
    report_path = output_dir / f"report_{label}_{ts}.json"
    jsonl_file = open(jsonl_path, "a", encoding="utf-8")
    jsonl_lock = asyncio.Lock()
    console.print(f"[cyan]JSONL -> {jsonl_path}[/cyan]")
    console.print(f"[cyan]Report -> {report_path}[/cyan]")

    counters = {k: 0 for k in [
        "products_processed", "variants_enriched", "wrong_product_api_rejected",
        "slug_mismatch", "variants_price_recovered", "variants_image_recovered",
        "flavor_mismatch_count", "public_content_unsafe_count",
        "rejected_content_leaked_count",
    ]}
    counter_lock = asyncio.Lock()

    async with async_playwright() as p_obj:
        tasks = []
        for i, slot in enumerate(runnable, start=1):
            # Stagger launches to avoid AdsPower API rate-limit collisions
            if i > 1:
                await asyncio.sleep(5)
            t = asyncio.create_task(_worker_coro(
                p_obj=p_obj, worker_id=f"worker_{i}", slot_id=slot["slot_id"],
                mode=mode, normalized_dir=normalized_dir,
                jsonl_file=jsonl_file, jsonl_lock=jsonl_lock, jsonl_path=jsonl_path,
                counters=counters, counter_lock=counter_lock,
            ))
            tasks.append(t)

        results = await asyncio.gather(*tasks, return_exceptions=True)

    # Close JSONL durably
    try:
        jsonl_file.flush()
        os.fsync(jsonl_file.fileno())
    except Exception:
        pass
    jsonl_file.close()

    summary = job_store.enrichment_state_summary()
    report = {
        "run_label": label, "mode": mode, "workers": workers,
        "timestamp": ts,
        "products_input": len(product_ids),
        "products_queued": len(candidates),
        "jsonl_path": str(jsonl_path),
        "worker_results": [r if isinstance(r, dict) else {"exception": str(r)} for r in results],
        "db_state_summary": summary,
        **counters,
    }
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    console.print("\n=== PARALLEL ENRICHMENT COMPLETED ===")
    console.print(f"DB summary: {summary}")
    for r in results:
        if isinstance(r, dict):
            console.print(f"  {r['worker_id']}: processed={r.get('processed')} status={r.get('status')}")
        else:
            console.print(f"  [red]worker exception: {r}[/red]")
    return report
