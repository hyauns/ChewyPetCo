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
import adspower


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


def _start_worker_browser(profile_id: str, worker_id: str, log_lock: threading.Lock, on_line: Callable | None) -> str | None:
    """Start AdsPower browser for a worker and return the WS URL."""
    try:
        profile_data = adspower.start_profile(profile_id)
        ws_url = adspower.get_ws_endpoint(profile_data)
        _safe_on_line(log_lock, on_line, f"[{worker_id}] Browser started (ws={ws_url[:60]}...)")
        return ws_url
    except Exception as exc:
        _safe_on_line(log_lock, on_line, f"[{worker_id}] Failed to start browser: {exc}")
        return None


def _stop_worker_browser(profile_id: str, worker_id: str, log_lock: threading.Lock, on_line: Callable | None) -> None:
    """Stop AdsPower browser for a worker."""
    try:
        adspower.stop_profile(profile_id)
        _safe_on_line(log_lock, on_line, f"[{worker_id}] Browser stopped.")
    except Exception:
        pass


def _worker_loop(
    *,
    job_id: str,
    worker_id: str,
    slot_id: str,
    retry_failed: bool,
    resume_paused: bool,
    reprocess_completed: bool,
    reprocess_existing: bool,
    force_retry: bool,
    max_items: int | None,
    processed_counter: dict[str, int],
    counter_lock: threading.Lock,
    log_lock: threading.Lock,
    on_line: Callable[[str], None] | None,
) -> dict[str, Any]:
    processed = 0
    browser_ws_url: str | None = None
    _safe_on_line(log_lock, on_line, f"[{worker_id}] Starting with slot {slot_id}")

    ensure = recovery.ensure_slot_profile(slot_id, delay_seconds=0)
    if not ensure.get("success"):
        _safe_on_line(log_lock, on_line, f"[{worker_id}] Slot {slot_id} unavailable: {ensure.get('message')}")
        return {"worker_id": worker_id, "slot_id": slot_id, "processed": processed, "status": "unavailable"}

    profile_id = _get_slot_profile(slot_id)
    if not profile_id:
        return {"worker_id": worker_id, "slot_id": slot_id, "processed": 0, "status": "no_profile"}

    # Start browser ONCE for this worker.
    browser_ws_url = _start_worker_browser(profile_id, worker_id, log_lock, on_line)
    if not browser_ws_url:
        return {"worker_id": worker_id, "slot_id": slot_id, "processed": 0, "status": "browser_start_failed"}

    try:
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
                _stop_worker_browser(profile_id, worker_id, log_lock, on_line)
                result = recovery.auto_rebuild_profile(slot_id, manual=True, delay_seconds=0)
                _safe_on_line(log_lock, on_line, f"[{worker_id}] Manual rebuild result: {json.dumps(result, ensure_ascii=False)}")
                if not result.get("success"):
                    browser_ws_url = None
                    break
                profile_id = _get_slot_profile(slot_id)
                if not profile_id:
                    browser_ws_url = None
                    break
                browser_ws_url = _start_worker_browser(profile_id, worker_id, log_lock, on_line)
                if not browser_ws_url:
                    break
                continue
            if slot["status"] == "rebuilding":
                reason = slot.get("notes") or "slot_marked_rebuilding"
                _safe_on_line(log_lock, on_line, f"[{worker_id}] Rebuilding slot {slot_id}: {reason}")
                _stop_worker_browser(profile_id, worker_id, log_lock, on_line)
                result = recovery.auto_rebuild_profile(slot_id, reason=reason, delay_seconds=0)
                _safe_on_line(log_lock, on_line, f"[{worker_id}] Rebuild result: {json.dumps(result, ensure_ascii=False)}")
                if not result.get("success"):
                    browser_ws_url = None
                    break
                profile_id = _get_slot_profile(slot_id)
                if not profile_id:
                    browser_ws_url = None
                    break
                browser_ws_url = _start_worker_browser(profile_id, worker_id, log_lock, on_line)
                if not browser_ws_url:
                    break
                continue

            item = job_store.claim_next_item(
                job_id,
                retry_failed=retry_failed,
                include_paused=resume_paused,
                reprocess_completed=reprocess_completed,
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
                browser_ws_url=browser_ws_url,
                on_line=lambda line: _safe_on_line(log_lock, on_line, f"[{worker_id}] {line}"),
            )
            processed += 1
            with counter_lock:
                processed_counter["count"] += 1

            if result.get("status") == "paused" and result.get("error_type") in {"white_screen_block", "all_profiles_exhausted"}:
                _safe_on_line(log_lock, on_line, f"[{worker_id}] Item paused: {result.get('error_message')}")
                if result.get("error_type") == "white_screen_block":
                    # White screen: stop browser, rebuild profile, start fresh browser.
                    _safe_on_line(log_lock, on_line, f"[{worker_id}] White screen detected — restarting browser...")
                    _stop_worker_browser(profile_id, worker_id, log_lock, on_line)
                    browser_ws_url = None
                    # Profile rebuild is handled by the slot status check at top of loop.
                    # Just need to restart browser with the new/existing profile.
                    new_profile_id = _get_slot_profile(slot_id)
                    if new_profile_id:
                        profile_id = new_profile_id
                        browser_ws_url = _start_worker_browser(profile_id, worker_id, log_lock, on_line)
                    if not browser_ws_url:
                        break
                    continue
                break

            refreshed = job_store.get_job(job_id)
            delay = float(refreshed.get("delay_seconds") if refreshed else 0)
            if delay > 0:
                time.sleep(delay)
    finally:
        # Always stop browser when worker finishes.
        if browser_ws_url and profile_id:
            _stop_worker_browser(profile_id, worker_id, log_lock, on_line)

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
    reprocess_completed: bool = False,
    reprocess_existing: bool = False,
    force_retry: bool = False,
    stale_minutes: int = 30,
    max_items: int | None = None,
    requeue_fallback_done: bool = False,
    on_line: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    job = job_store.get_job(job_id)
    if not job:
        raise ValueError(f"Job not found: {job_id}")
    if hasattr(config, "reload_from_env_file"):
        config.reload_from_env_file(override=True)
        if on_line:
            on_line(f"[job {job_id}] Reloaded .env runtime config.")
    if not config.ADSP_PROFILE_RECOVERY_ENABLED:
        raise RuntimeError("ADSP_PROFILE_RECOVERY_ENABLED must be true for parallel CW workers.")

    if requeue_fallback_done:
        single_runner.requeue_fallback_done_items_for_resume(job_id, on_line=on_line)

    orphaned = job_store.mark_orphan_running_items(job_id)
    if orphaned and on_line:
        on_line(f"[job {job_id}] Reset {orphaned} orphan running item(s) to pending.")
    job_store.mark_stale_running_items(job_id, stale_minutes=stale_minutes)
    rebuilt = recovery.rebuild_slots_with_env_proxy_changes(delay_seconds=0)
    if rebuilt.get("rebuilt_count") and on_line:
        on_line(
            f"[job {job_id}] Rebuilt {rebuilt['rebuilt_count']} CW slot(s) because .env proxy config changed."
        )
    recovery.sync_profile_templates_to_db()
    restored = recovery.restore_runtime_local_slots_from_env(delay_seconds=0)
    if restored.get("restored_count") and on_line:
        on_line(
            f"[job {job_id}] Restored {restored['restored_count']} runtime Local/no_proxy slot(s) "
            "back to configured .env proxy profiles."
        )
    released = recovery.release_stale_template_slots()
    if released and on_line:
        on_line(f"[job {job_id}] Released {released} stale CW slot(s).")

    # Reset slots stuck in rebuild_failed / stale in_use so workers can retry.
    with job_store.connect() as conn:
        reset = conn.execute(
            """
            UPDATE adsp_profile_templates
            SET status = 'available',
                notes = 'Reset for new parallel run'
            WHERE status IN ('rebuild_failed', 'in_use')
            """
        )
        conn.commit()
        if reset.rowcount and on_line:
            on_line(f"[job {job_id}] Reset {reset.rowcount} stuck slot(s) (rebuild_failed/in_use) to available.")

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
    try:
        with ThreadPoolExecutor(max_workers=len(runnable_slots)) as executor:
            futures = []
            for index, slot in enumerate(runnable_slots, start=1):
                # Stagger worker launches to prevent AdsPower API rate-limit
                # collisions during ensure_slot_profile (stop → delete → create).
                if index > 1:
                    time.sleep(5)
                futures.append(
                    executor.submit(
                        _worker_loop,
                        job_id=job_id,
                        worker_id=f"worker_{index}",
                        slot_id=slot["slot_id"],
                        retry_failed=retry_failed,
                        resume_paused=resume_paused,
                        reprocess_completed=reprocess_completed,
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
            if counts["pending_count"] == 0 and counts["failed_count"] == 0:
                job_store.set_job_status(job_id, "completed")
            else:
                job_store.set_job_status(job_id, "paused", last_error="Parallel workers stopped with unfinished items.")
                
    except KeyboardInterrupt:
        if on_line:
            on_line("\n[bold red]⚠️ Tiến trình bị hủy bởi người dùng (Ctrl+C). Đang dọn dẹp và dừng các Worker một cách an toàn...[/bold red]")
        job_store.set_job_status(job_id, "paused", last_error="Dừng đột ngột bởi người dùng (KeyboardInterrupt)")

    summary = single_runner.write_job_reports(job_id)
    summary["worker_results"] = worker_results
    if on_line:
        on_line(
            f"[job {job_id}] Parallel status: {summary['status']} "
            f"completed={summary['completed_count']} failed={summary['failed_count']} pending={summary['pending_count']}"
        )
    return summary
