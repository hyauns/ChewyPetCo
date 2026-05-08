import argparse
import asyncio
import json
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import config
import job_store

def utc_now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")

def load_profile_pool_from_config() -> list[str]:
    if not config.ADSP_PROFILE_POOL_ENABLED:
        return [config.ADSPOWER_PROFILE_ID]
    return config.ADSP_PROFILE_POOL_IDS

def sync_profile_pool_to_db() -> None:
    if not config.ADSP_PROFILE_POOL_ENABLED:
        return
        
    pool_ids = load_profile_pool_from_config()
    job_store.init_db()
    
    now = utc_now()
    with job_store.connect() as conn:
        for pid in pool_ids:
            conn.execute(
                """
                INSERT INTO adsp_profile_pool (
                    profile_id, label, status, created_at, updated_at
                )
                VALUES (?, ?, 'available', ?, ?)
                ON CONFLICT(profile_id) DO NOTHING
                """,
                (pid, f"Profile {pid}", now, now)
            )
        conn.commit()

def sync_template_profiles_to_pool() -> dict[str, str]:
    """Insert currently mapped CW template profile ids into the runtime pool.

    Returns {profile_id: slot_id}. If no CW templates are configured/mapped,
    callers should fall back to the legacy ADSP_PROFILE_POOL_IDS behavior.
    """
    if not getattr(config, "ADSP_PROFILE_RECOVERY_ENABLED", False):
        return {}
    try:
        import adsp_profile_recovery_manager
    except Exception:
        return {}

    adsp_profile_recovery_manager.sync_profile_templates_to_db()
    rows = adsp_profile_recovery_manager.get_profile_template_status()
    active: dict[str, str] = {}
    now = utc_now()
    with job_store.connect() as conn:
        for row in rows:
            profile_id = row.get("adspower_profile_id")
            if not profile_id or row.get("status") in {"disabled", "rebuild_failed", "rebuilding"}:
                continue
            active[str(profile_id)] = row["slot_id"]
            conn.execute(
                """
                INSERT INTO adsp_profile_pool (
                    profile_id, label, status, created_at, updated_at
                )
                VALUES (?, ?, 'available', ?, ?)
                ON CONFLICT(profile_id) DO UPDATE SET
                    label = excluded.label,
                    updated_at = excluded.updated_at
                """,
                (str(profile_id), row.get("display_name") or row["slot_id"], now, now),
            )
        conn.commit()
    return active

def release_stale_in_use_profiles() -> int:
    """Release profiles left in_use when no item is currently running with them."""
    if not config.ADSP_PROFILE_POOL_ENABLED:
        return 0

    now = utc_now()
    with job_store.connect() as conn:
        rows = conn.execute(
            """
            SELECT profile_id
            FROM adsp_profile_pool
            WHERE status = 'in_use'
            AND profile_id NOT IN (
                SELECT DISTINCT profile_id_used
                FROM scrape_job_items
                WHERE status = 'running' AND profile_id_used IS NOT NULL
            )
            """
        ).fetchall()
        for row in rows:
            conn.execute(
                """
                UPDATE adsp_profile_pool
                SET status = 'available',
                    notes = 'Auto-released stale in_use profile',
                    updated_at = ?
                WHERE profile_id = ?
                """,
                (now, row["profile_id"]),
            )
        conn.commit()
    return len(rows)

def get_next_available_profile(job_id: str = None, item_id: int = None) -> str | None:
    if not config.ADSP_PROFILE_POOL_ENABLED:
        return config.ADSPOWER_PROFILE_ID
        
    sync_profile_pool_to_db()
    template_profiles = sync_template_profiles_to_pool()
    release_stale_in_use_profiles()
    now = utc_now()
    
    with job_store.connect() as conn:
        # First, unquarantine profiles if time passed
        conn.execute(
            """
            UPDATE adsp_profile_pool 
            SET status = 'available', quarantine_until = NULL, updated_at = ?
            WHERE status = 'quarantined' AND quarantine_until <= ?
            """,
            (now, now)
        )
        conn.commit()
        
        if template_profiles:
            placeholders = ",".join("?" for _ in template_profiles)
            row = conn.execute(
                f"""
                SELECT profile_id FROM adsp_profile_pool
                WHERE status = 'available'
                  AND profile_id IN ({placeholders})
                ORDER BY last_used_at ASC NULLS FIRST
                LIMIT 1
                """,
                tuple(template_profiles.keys()),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT profile_id FROM adsp_profile_pool 
                WHERE status = 'available'
                ORDER BY last_used_at ASC NULLS FIRST
                LIMIT 1
                """
            ).fetchone()
        
        if row:
            return row["profile_id"]
    return None

def activate_only_profiles(profile_ids: list[str], reason: str = "Activated after auto-rebuild") -> int:
    """Make rebuilt profiles available and keep older pool profiles disabled."""
    clean_ids = [str(pid) for pid in profile_ids if pid]
    if not clean_ids:
        return 0
    now = utc_now()
    placeholders = ",".join("?" for _ in clean_ids)
    with job_store.connect() as conn:
        conn.execute(
            f"""
            UPDATE adsp_profile_pool
            SET status = 'disabled',
                notes = ?,
                quarantine_until = NULL,
                updated_at = ?
            WHERE profile_id NOT IN ({placeholders})
            """,
            (reason, now, *clean_ids),
        )
        cursor = conn.execute(
            f"""
            UPDATE adsp_profile_pool
            SET status = 'available',
                quarantine_until = NULL,
                notes = ?,
                updated_at = ?
            WHERE profile_id IN ({placeholders})
            """,
            (reason, now, *clean_ids),
        )
        conn.commit()
        return cursor.rowcount

def mark_profile_in_use(profile_id: str) -> None:
    if not config.ADSP_PROFILE_POOL_ENABLED:
        return
    now = utc_now()
    with job_store.connect() as conn:
        conn.execute(
            """
            UPDATE adsp_profile_pool
            SET status = 'in_use', last_used_at = ?, total_attempts = total_attempts + 1, updated_at = ?
            WHERE profile_id = ?
            """,
            (now, now, profile_id)
        )
        conn.commit()

def mark_profile_success(profile_id: str) -> None:
    if not config.ADSP_PROFILE_POOL_ENABLED:
        return
    now = utc_now()
    with job_store.connect() as conn:
        conn.execute(
            """
            UPDATE adsp_profile_pool
            SET status = 'available', total_success = total_success + 1, updated_at = ?
            WHERE profile_id = ? AND status = 'in_use'
            """,
            (now, profile_id)
        )
        conn.commit()

def quarantine_profile(profile_id: str, reason: str, minutes: int = None) -> None:
    if not config.ADSP_PROFILE_POOL_ENABLED:
        return
        
    if minutes is None:
        minutes = config.ADSP_PROFILE_QUARANTINE_MINUTES
        
    now = utc_now()
    quarantine_until = (datetime.utcnow() + timedelta(minutes=minutes)).isoformat(timespec="seconds")
    
    with job_store.connect() as conn:
        conn.execute(
            """
            UPDATE adsp_profile_pool
            SET status = 'quarantined', 
                last_white_screen_at = ?, 
                quarantine_until = ?, 
                total_white_screen = total_white_screen + 1,
                notes = ?,
                updated_at = ?
            WHERE profile_id = ?
            """,
            (now, quarantine_until, reason, now, profile_id)
        )
        conn.commit()

def release_profile(profile_id: str) -> None:
    now = utc_now()
    with job_store.connect() as conn:
        conn.execute(
            """
            UPDATE adsp_profile_pool
            SET status = 'available', quarantine_until = NULL, notes = 'Manually released', updated_at = ?
            WHERE profile_id = ?
            """,
            (now, profile_id)
        )
        conn.commit()

def release_all_quarantined() -> int:
    """Release all quarantined profiles back to available. Used after auto-rebuild."""
    now = utc_now()
    with job_store.connect() as conn:
        cursor = conn.execute(
            """
            UPDATE adsp_profile_pool
            SET status = 'available', quarantine_until = NULL,
                notes = 'Released after auto-rebuild', updated_at = ?
            WHERE status = 'quarantined'
            """,
            (now,)
        )
        conn.commit()
        return cursor.rowcount

def get_profile_health_summary() -> list[dict]:
    with job_store.connect() as conn:
        rows = conn.execute("SELECT * FROM adsp_profile_pool ORDER BY updated_at DESC").fetchall()
    return job_store.rows_to_dicts(rows)

def record_white_screen_event(
    job_id: str,
    item_id: int,
    input_url: str,
    profile_id: str,
    event_type: str,
    detection_result: dict,
    message: str = ""
) -> None:
    event_id = f"evt_{uuid.uuid4().hex}"
    now = utc_now()
    
    with job_store.connect() as conn:
        conn.execute(
            """
            INSERT INTO white_screen_events (
                event_id, job_id, item_id, input_url, profile_id, event_type,
                detection_confidence, signals_json, screenshot_path, html_snapshot_path,
                message, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id, job_id, item_id, input_url, profile_id, event_type,
                detection_result.get("confidence", 0.0),
                json.dumps(detection_result.get("signals", [])),
                detection_result.get("screenshot_path"),
                detection_result.get("html_snapshot_path"),
                message,
                now
            )
        )
        conn.commit()

def _score_white_screen_snapshot(html: str, title: str, body_text: str) -> dict:
    signals = []
    confidence = 0.0

    body_len = len(body_text.strip())
    has_next_data = "__NEXT_DATA__" in html
    has_product_content = "chewy.com" in html.lower() and ("Product Details" in html or "data-testid=" in html or "price" in html.lower() or "Price" in html)

    if body_len < 200:
        signals.append("extremely_short_body_text")
        confidence += 0.5

    if not has_next_data and "apollo" not in html.lower() and "redux" not in html.lower():
        signals.append("missing_next_data")
        confidence += 0.2

    if title == "Access Denied" or title == "":
        signals.append("generic_or_empty_title")
        confidence += 0.3

    if not has_product_content:
        signals.append("missing_product_content")
        confidence += 0.3

    if "Pardon Our Interruption" in html or "PerimeterX" in html or "Datadome" in html:
        signals.append("known_block_page")
        confidence += 0.8

    confidence = min(1.0, confidence)
    return {
        "is_white_screen": confidence >= 0.7,
        "confidence": confidence,
        "signals": signals,
        "body_text_length": body_len,
        "title": title,
        "has_next_data": has_next_data,
        "has_product_content": has_product_content,
    }


async def _read_page_snapshot(page) -> dict:
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=5000)
    except Exception:
        pass
    try:
        html = await page.content()
    except Exception:
        html = ""
    try:
        title = await page.title()
    except Exception:
        title = ""
    try:
        body_text = await page.evaluate("document.body.innerText") or ""
    except Exception:
        body_text = ""
    result = _score_white_screen_snapshot(html, title, body_text)
    result["html_length"] = len(html or "")
    return result


async def detect_white_screen_block(page, url: str) -> dict:
    if not config.ADSP_WHITE_SCREEN_DETECTION_ENABLED:
        return {"is_white_screen": False, "confidence": 0.0}

    min_wait = max(0, int(getattr(config, "ADSP_WHITE_SCREEN_MIN_WAIT_SECONDS", 30)))
    max_wait = max(min_wait, int(getattr(config, "ADSP_WHITE_SCREEN_MAX_WAIT_SECONDS", 90)))
    poll_seconds = max(1, int(getattr(config, "ADSP_WHITE_SCREEN_POLL_SECONDS", 5)))
    required_empty_checks = max(1, int(getattr(config, "ADSP_WHITE_SCREEN_REQUIRED_EMPTY_CHECKS", 3)))

    started = time.monotonic()
    checks: list[dict] = []
    consecutive_empty = 0
    last_result: dict = {
        "is_white_screen": False,
        "confidence": 0.0,
        "signals": [],
        "body_text_length": 0,
        "title": "",
        "has_next_data": False,
        "has_product_content": False,
    }

    while True:
        result = await _read_page_snapshot(page)
        elapsed = round(time.monotonic() - started, 2)
        result["elapsed_seconds"] = elapsed
        checks.append(
            {
                "elapsed_seconds": elapsed,
                "confidence": result.get("confidence"),
                "signals": result.get("signals"),
                "body_text_length": result.get("body_text_length"),
                "html_length": result.get("html_length"),
                "title": result.get("title"),
                "has_next_data": result.get("has_next_data"),
                "has_product_content": result.get("has_product_content"),
            }
        )
        last_result = result

        known_block = "known_block_page" in (result.get("signals") or [])
        access_denied = str(result.get("title") or "").strip().lower() == "access denied"
        meaningful_dom = (
            not known_block
            and not access_denied
            and (
                result.get("has_next_data")
                or result.get("has_product_content")
                or int(result.get("body_text_length") or 0) >= 500
            )
        )
        # Any meaningful Chewy/product DOM means this is a slow load, not a white screen.
        if meaningful_dom:
            result.update(
                {
                    "is_white_screen": False,
                    "confidence": 0.0,
                    "signals": ["meaningful_dom_detected"],
                    "checks": checks,
                    "waited_seconds": elapsed,
                    "screenshot_path": None,
                    "html_snapshot_path": None,
                }
            )
            return result

        if result.get("is_white_screen"):
            consecutive_empty += 1
        else:
            consecutive_empty = 0

        if elapsed >= min_wait and consecutive_empty >= required_empty_checks:
            result["is_white_screen"] = True
            result["checks"] = checks
            result["waited_seconds"] = elapsed
            result["required_empty_checks"] = required_empty_checks
            result["min_wait_seconds"] = min_wait
            result["max_wait_seconds"] = max_wait
            result["screenshot_path"] = None
            result["html_snapshot_path"] = None
            return result

        if elapsed >= max_wait:
            last_result["is_white_screen"] = bool(last_result.get("is_white_screen") and consecutive_empty >= required_empty_checks)
            last_result["checks"] = checks
            last_result["waited_seconds"] = elapsed
            last_result["required_empty_checks"] = required_empty_checks
            last_result["min_wait_seconds"] = min_wait
            last_result["max_wait_seconds"] = max_wait
            last_result["screenshot_path"] = None
            last_result["html_snapshot_path"] = None
            return last_result

        await asyncio.sleep(poll_seconds)

    return {
        **last_result,
        "screenshot_path": None,
        "html_snapshot_path": None,
    }

def main():
    parser = argparse.ArgumentParser(description="AdsPower Profile Pool Manager")
    parser.add_argument("action", choices=["list", "sync", "quarantine", "release", "events"], help="Action to perform")
    parser.add_argument("--profile-id", type=str, help="AdsPower Profile ID")
    parser.add_argument("--minutes", type=int, default=30, help="Quarantine duration in minutes")
    
    args = parser.parse_args()
    
    if args.action == "sync":
        sync_profile_pool_to_db()
        print("Profile pool synced from config to database.")
    elif args.action == "list":
        profiles = get_profile_health_summary()
        for p in profiles:
            print(f"{p['profile_id']} ({p['status']}) - Success: {p['total_success']}, White Screen: {p['total_white_screen']}")
    elif args.action == "quarantine":
        if not args.profile_id:
            print("Error: --profile-id required")
            return
        quarantine_profile(args.profile_id, "Manually quarantined via CLI", args.minutes)
        print(f"Profile {args.profile_id} quarantined for {args.minutes} minutes.")
    elif args.action == "release":
        if not args.profile_id:
            print("Error: --profile-id required")
            return
        release_profile(args.profile_id)
        print(f"Profile {args.profile_id} released.")
    elif args.action == "events":
        with job_store.connect() as conn:
            rows = conn.execute("SELECT * FROM white_screen_events ORDER BY created_at DESC LIMIT 20").fetchall()
        for r in job_store.rows_to_dicts(rows):
            print(f"{r['created_at']} - {r['profile_id']} - {r['event_type']} - Confidence: {r['detection_confidence']}")

if __name__ == "__main__":
    main()
