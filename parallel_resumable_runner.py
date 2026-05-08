"""Controlled multi-worker runner for resumable Chewy PDP jobs.

Each worker owns exactly one configured AdsPower template slot, for example:
worker_1 -> CW_1, worker_2 -> CW_2, worker_3 -> CW_3.

The runner wraps resumable_scraper_runner.process_single_item() and does not
change PDP parser, grouping, normalization, or Shopify behavior.
"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

import config
import job_store
import adsp_profile_recovery_manager as recovery
import resumable_scraper_runner as single_runner


def _safe_on_line(lock: threading.Lock, callback: Callable[[str], None] | None, line: str) -> None:
    if not callback:
        return
    with lock:
        callback(line)


def _get_slot_profile(slot_id: str) -> str | None:
    row = recovery.get_template(slot_id)
    if not row:
        return None
    profile_id = row.get("adspower_profile_id")
    return str(profile_id) if profile_id else None


def _worker_loop(
    *,
    job_id: str,
    worker_id: str,
    slot_id: str,
    retry_failed: bool,
    resume_paused: bool,
    reprocess_existing: bool,
    force_retry: bool,
    max_items: int | None,
    processed_counter: dict[str, int],
    counter_lock: threading.Lock,
    log_lock: threading.Lock,
    on_line: Callable[[str], None] | None,
) -> dict[str, Any]:
    processed = 0
    _safe_on_line(log_lock, on_line, f"[{worker_id}] Starting with slot {slot_id}")

    ensure = recovery.ensure_slot_profile(slot_id, delay_seconds=0)
    if not ensure.get("success"):
        _safe_on_line(log_lock, on_line, f"[{worker_id}] Slot {slot_id} unavailable: {ensure.get('message')}")
        return {"worker_id": worker_id, "slot_id": slot_id, "processed": processed, "status": "unavailable"}

    while True:
        current_job = job_store.get_job(job_id)
        if not current_job or current_job["status"] in {"paused", "cancelled", "failed"}:
            break

        with counter_lock:
            if max_items is not None and processed_counter["count"] >= max_items:
                break

        slot = recovery.get_template(slot_id)
        if not slot:
            break
        if slot["status"] in {"disabled", "rebuild_failed"}:
            _safe_on_line(log_lock, on_line, f"[{worker_id}] Slot {slot_id} stopped: {slot['status']} {slot.get('notes') or ''}")
            break
        if recovery.consume_rebuild_request(slot_id):
            _safe_on_line(log_lock, on_line, f"[{worker_id}] Manual rebuild request detected for {slot_id}")
            result = recovery.auto_rebuild_profile(slot_id, manual=True, delay_seconds=0)
            _safe_on_line(log_lock, on_line, f"[{worker_id}] Manual rebuild result: {json.dumps(result, ensure_ascii=False)}")
            if not result.get("success"):
                break
            continue
        if slot["status"] == "rebuilding":
            _safe_on_line(log_lock, on_line, f"[{worker_id}] Waiting for slot {slot_id} rebuild")
            time.sleep(5)
            continue

        profile_id = _get_slot_profile(slot_id)
        if not profile_id:
            result = recovery.ensure_slot_profile(slot_id, delay_seconds=0)
            _safe_on_line(log_lock, on_line, f"[{worker_id}] Initial profile build result: {json.dumps(result, ensure_ascii=False)}")
            if not result.get("success"):
                break
            profile_id = _get_slot_profile(slot_id)
        if not profile_id:
            break

        item = job_store.claim_next_item(
            job_id,
            retry_failed=retry_failed,
            include_paused=resume_paused,
            force_retry=force_retry,
            worker_id=worker_id,
            profile_slot_id=slot_id,
        )
        if not item:
            break

        _safe_on_line(
            log_lock,
            on_line,
            f"[{worker_id}] Processing item {item['index_number']} with {slot_id}/{profile_id}: {item['input_url']}",
        )
        result = single_runner.process_single_item(
            job_id,
            int(item["id"]),
            reprocess_existing=reprocess_existing,
            force_retry=force_retry,
            profile_id_override=profile_id,
            profile_slot_id=slot_id,
            worker_id=worker_id,
            on_line=lambda line: _safe_on_line(log_lock, on_line, f"[{worker_id}] {line}"),
        )
        processed += 1
        with counter_lock:
            processed_counter["count"] += 1

        if result.get("status") == "paused" and result.get("error_type") in {"white_screen_block", "all_profiles_exhausted"}:
            _safe_on_line(log_lock, on_line, f"[{worker_id}] Item paused: {result.get('error_message')}")
            if result.get("error_type") == "white_screen_block":
                continue
            break

        refreshed = job_store.get_job(job_id)
        delay = float(refreshed.get("delay_seconds") if refreshed else 0)
        if delay > 0:
            time.sleep(delay)

    final_slot = recovery.get_template(slot_id)
    if final_slot and final_slot["status"] not in {"disabled", "rebuild_failed", "rebuilding"}:
        recovery.mark_template_available(slot_id, f"{worker_id} stopped")
    return {"worker_id": worker_id, "slot_id": slot_id, "processed": processed, "status": "stopped"}


def process_job_parallel(
    job_id: str,
    *,
    worker_count: int | None = None,
    retry_failed: bool = True,
    resume_paused: bool = False,
    reprocess_existing: bool = False,
    force_retry: bool = False,
    stale_minutes: int = 30,
    max_items: int | None = None,
    on_line: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    job = job_store.get_job(job_id)
    if not job:
        raise ValueError(f"Job not found: {job_id}")
    if not config.ADSP_PROFILE_RECOVERY_ENABLED:
        raise RuntimeError("ADSP_PROFILE_RECOVERY_ENABLED must be true for parallel CW workers.")

    orphaned = job_store.mark_orphan_running_items(job_id)
    if orphaned and on_line:
        on_line(f"[job {job_id}] Reset {orphaned} orphan running item(s) to pending.")
    job_store.mark_stale_running_items(job_id, stale_minutes=stale_minutes)
    recovery.sync_profile_templates_to_db()
    released = recovery.release_stale_template_slots()
    if released and on_line:
        on_line(f"[job {job_id}] Released {released} stale CW slot(s).")
    job_store.set_job_status(job_id, "running")

    worker_count = worker_count or config.ADSP_WORKER_COUNT
    worker_count = max(1, min(int(worker_count), recovery.MAX_TEMPLATE_SLOTS))
    slots = recovery.get_worker_slot_status(worker_count)
    runnable_slots = [slot for slot in slots if slot["status"] != "disabled"]
    if not runnable_slots:
        job_store.set_job_status(job_id, "paused", last_error="No configured CW profile slots are available.")
        return single_runner.write_job_reports(job_id)

    log_lock = threading.Lock()
    counter_lock = threading.Lock()
    processed_counter = {"count": 0}
    if on_line:
        on_line(f"[job {job_id}] Starting controlled parallel run with {len(runnable_slots)} worker(s).")

    worker_results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=len(runnable_slots)) as executor:
        futures = []
        for index, slot in enumerate(runnable_slots, start=1):
            futures.append(
                executor.submit(
                    _worker_loop,
                    job_id=job_id,
                    worker_id=f"worker_{index}",
                    slot_id=slot["slot_id"],
                    retry_failed=retry_failed,
                    resume_paused=resume_paused,
                    reprocess_existing=reprocess_existing,
                    force_retry=force_retry,
                    max_items=max_items,
                    processed_counter=processed_counter,
                    counter_lock=counter_lock,
                    log_lock=log_lock,
                    on_line=on_line,
                )
            )
        for future in as_completed(futures):
            worker_results.append(future.result())

    counts = job_store.update_job_counts(job_id)
    current_job = job_store.get_job(job_id)
    if current_job and current_job["status"] == "running":
        if counts["pending_count"] == 0:
            job_store.set_job_status(job_id, "completed")
        else:
            job_store.set_job_status(job_id, "paused", last_error="Parallel workers stopped with unfinished items.")

    summary = single_runner.write_job_reports(job_id)
    summary["worker_results"] = worker_results
    if on_line:
        on_line(
            f"[job {job_id}] Parallel status: {summary['status']} "
            f"completed={summary['completed_count']} failed={summary['failed_count']} pending={summary['pending_count']}"
        )
    return summary
