import argparse
import json
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

def get_next_available_profile(job_id: str = None, item_id: int = None) -> str | None:
    if not config.ADSP_PROFILE_POOL_ENABLED:
        return config.ADSPOWER_PROFILE_ID
        
    sync_profile_pool_to_db()
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

async def detect_white_screen_block(page, url: str) -> dict:
    if not config.ADSP_WHITE_SCREEN_DETECTION_ENABLED:
        return {"is_white_screen": False, "confidence": 0.0}
        
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
    is_white_screen = confidence >= 0.7
    
    return {
        "is_white_screen": is_white_screen,
        "confidence": confidence,
        "signals": signals,
        "body_text_length": body_len,
        "title": title,
        "has_next_data": has_next_data,
        "has_product_content": has_product_content,
        "screenshot_path": None,
        "html_snapshot_path": None
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
