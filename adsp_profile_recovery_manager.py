"""Controlled AdsPower profile template recovery for Chewy scraper jobs.

This module manages fixed user-configured slots such as CW_1, CW_2, CW_3.
It does not create unlimited profiles, does not change proxy assignments, and
does not implement captcha solving or anti-bot bypass logic.
"""

from __future__ import annotations

import argparse
import json
import os
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import unquote, urlparse
from typing import Any

import httpx

import adspower
import config
import job_store


MAX_TEMPLATE_SLOTS = 3
VALID_TEMPLATE_STATUSES = {"available", "in_use", "rebuilding", "disabled", "rebuild_failed"}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")


def _api_url(path: str) -> str:
    return f"{config.ADSPOWER_API_BASE}{path}"


def _slot_config_value(slot_index: int, suffix: str, default: str = "") -> str:
    prefix = getattr(config, "ADSP_TEMPLATE_PREFIX", "CW")
    env_value = os.environ.get(f"ADSP_{prefix}_{slot_index}_{suffix}")
    if env_value is not None:
        return env_value
    return str(getattr(config, f"ADSP_CW_{slot_index}_{suffix}", default) or default)


def _mask_user(value: str | None) -> str:
    if not value:
        return ""
    if len(value) <= 2:
        return "***"
    return f"{value[:2]}***"


def validate_proxy_template(proxy_url: str) -> dict[str, str]:
    """Parse and validate a configured SOCKS5 proxy URL."""
    if not proxy_url:
        raise ValueError("Missing proxy URL.")
    parsed = urlparse(proxy_url)
    if parsed.scheme.lower() != "socks5":
        raise ValueError("Only socks5:// proxies are supported for CW slots.")
    if not parsed.hostname:
        raise ValueError("Proxy host is missing.")
    if not parsed.port:
        raise ValueError("Proxy port is missing.")
    return {
        "proxy_type": "socks5",
        "proxy_host": parsed.hostname,
        "proxy_port": str(parsed.port),
        "proxy_username": unquote(parsed.username or ""),
        "proxy_password": unquote(parsed.password or ""),
    }


def mask_proxy_url(proxy_url: str) -> str:
    if not proxy_url:
        return "(not configured)"
    try:
        parsed = urlparse(proxy_url)
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        if parsed.username or parsed.password:
            user = _mask_user(unquote(parsed.username or ""))
            return f"{parsed.scheme}://{user}:***@{host}{port}"
        return f"{parsed.scheme}://{host}{port}"
    except Exception:
        return "(invalid proxy)"


def get_template_slots() -> list[str]:
    prefix = getattr(config, "ADSP_TEMPLATE_PREFIX", "CW")
    return [f"{prefix}_{index}" for index in range(1, MAX_TEMPLATE_SLOTS + 1)]


def load_profile_templates_from_config() -> list[dict[str, Any]]:
    """Load the fixed CW slot templates from config/env.

    Returned dictionaries include proxy_password for internal API use only.
    UI/CLI should use get_profile_template_status(), which never returns raw
    credentials.
    """
    templates: list[dict[str, Any]] = []
    for index, slot_id in enumerate(get_template_slots(), start=1):
        display_name = _slot_config_value(index, "NAME", slot_id)
        proxy_url = _slot_config_value(index, "PROXY", "")
        profile_id = _slot_config_value(index, "PROFILE_ID", "")
        try:
            parsed = validate_proxy_template(proxy_url)
            status = "available"
            notes = ""
        except ValueError as exc:
            parsed = {
                "proxy_type": "socks5",
                "proxy_host": "",
                "proxy_port": "",
                "proxy_username": "",
                "proxy_password": "",
            }
            status = "disabled"
            notes = str(exc)
        parsed.update(
            {
                "slot_id": slot_id,
                "display_name": display_name or slot_id,
                "proxy_url_masked": mask_proxy_url(proxy_url),
                "adspower_profile_id": profile_id or None,
                "status": status,
                "notes": notes,
            }
        )
        templates.append(parsed)
    return templates


def _template_by_slot(slot_id: str) -> dict[str, Any]:
    for template in load_profile_templates_from_config():
        if template["slot_id"] == slot_id:
            return template
    raise ValueError(f"Unknown profile template slot: {slot_id}")


def _sanitize_message(message: str, template: dict[str, Any] | None = None) -> str:
    clean = str(message)
    if template:
        for secret in [template.get("proxy_password"), template.get("proxy_username")]:
            if secret:
                clean = clean.replace(str(secret), "***")
    return clean


def sync_profile_templates_to_db() -> None:
    """Synchronize configured fixed slots into scraper_jobs.db."""
    job_store.init_db()
    now = utc_now()
    templates = load_profile_templates_from_config()
    with job_store.connect() as conn:
        for template in templates:
            existing = conn.execute(
                "SELECT status, notes, adspower_profile_id FROM adsp_profile_templates WHERE slot_id = ?",
                (template["slot_id"],),
            ).fetchone()
            status = template["status"]
            notes = template["notes"]
            if existing:
                existing_status = existing["status"]
                existing_notes = existing["notes"] or ""
                if existing_status in {"in_use", "rebuilding"}:
                    status = existing_status
                elif existing_status == "disabled" and existing_notes == "Manually disabled":
                    status = "disabled"
                    notes = existing_notes
                elif existing_status == "rebuild_failed" and template["status"] == "available":
                    status = "rebuild_failed"
                    notes = existing_notes

            profile_id = template.get("adspower_profile_id")
            if existing and not profile_id:
                profile_id = existing["adspower_profile_id"]

            conn.execute(
                """
                INSERT INTO adsp_profile_templates (
                    slot_id, display_name, proxy_type, proxy_host, proxy_port,
                    proxy_username_masked, adspower_profile_id, status, notes,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(slot_id) DO UPDATE SET
                    display_name = excluded.display_name,
                    proxy_type = excluded.proxy_type,
                    proxy_host = excluded.proxy_host,
                    proxy_port = excluded.proxy_port,
                    proxy_username_masked = excluded.proxy_username_masked,
                    adspower_profile_id = COALESCE(excluded.adspower_profile_id, adsp_profile_templates.adspower_profile_id),
                    status = excluded.status,
                    notes = excluded.notes,
                    updated_at = excluded.updated_at
                """,
                (
                    template["slot_id"],
                    template["display_name"],
                    template["proxy_type"],
                    template["proxy_host"],
                    template["proxy_port"],
                    _mask_user(template.get("proxy_username")),
                    profile_id,
                    status,
                    notes,
                    now,
                    now,
                ),
            )
            if profile_id:
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
                    (profile_id, template["display_name"], now, now),
                )
        conn.commit()


def record_profile_rebuild_event(
    slot_id: str,
    event_type: str,
    *,
    old_profile_id: str | None = None,
    new_profile_id: str | None = None,
    message: str = "",
    metadata: dict[str, Any] | None = None,
) -> None:
    job_store.init_db()
    event_id = f"rebuild_{uuid.uuid4().hex}"
    with job_store.connect() as conn:
        conn.execute(
            """
            INSERT INTO adsp_profile_rebuild_events (
                event_id, slot_id, old_profile_id, new_profile_id, event_type,
                message, metadata_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                slot_id,
                old_profile_id,
                new_profile_id,
                event_type,
                message,
                json.dumps(metadata or {}, ensure_ascii=False),
                utc_now(),
            ),
        )
        conn.commit()


def get_profile_template_status() -> list[dict[str, Any]]:
    sync_profile_templates_to_db()
    template_masks = {t["slot_id"]: t["proxy_url_masked"] for t in load_profile_templates_from_config()}
    with job_store.connect() as conn:
        rows = job_store.rows_to_dicts(
            conn.execute("SELECT * FROM adsp_profile_templates ORDER BY slot_id ASC").fetchall()
        )
    for row in rows:
        row["masked_proxy"] = template_masks.get(row["slot_id"], "(not configured)")
        successes = int(row.get("total_success") or 0)
        white_screens = int(row.get("total_white_screen") or 0)
        attempts = successes + white_screens
        row["success_rate"] = round((successes / attempts) * 100, 2) if attempts else None
    return rows


def get_slot_for_profile_id(profile_id: str | None) -> str | None:
    if not profile_id:
        return None
    sync_profile_templates_to_db()
    with job_store.connect() as conn:
        row = conn.execute(
            "SELECT slot_id FROM adsp_profile_templates WHERE adspower_profile_id = ?",
            (profile_id,),
        ).fetchone()
    return row["slot_id"] if row else None


def get_template(slot_id: str) -> dict[str, Any] | None:
    sync_profile_templates_to_db()
    with job_store.connect() as conn:
        row = conn.execute("SELECT * FROM adsp_profile_templates WHERE slot_id = ?", (slot_id,)).fetchone()
    return job_store.row_to_dict(row)


def map_slot_to_profile_id(slot_id: str, profile_id: str) -> None:
    template = _template_by_slot(slot_id)
    now = utc_now()
    with job_store.connect() as conn:
        conn.execute(
            """
            UPDATE adsp_profile_templates
            SET adspower_profile_id = ?,
                status = 'available',
                notes = ?,
                updated_at = ?
            WHERE slot_id = ?
            """,
            (profile_id, f"Mapped to AdsPower profile {profile_id}", now, slot_id),
        )
        conn.execute(
            """
            INSERT INTO adsp_profile_pool (
                profile_id, label, status, created_at, updated_at
            )
            VALUES (?, ?, 'available', ?, ?)
            ON CONFLICT(profile_id) DO UPDATE SET
                label = excluded.label,
                status = 'available',
                updated_at = excluded.updated_at
            """,
            (profile_id, template["display_name"], now, now),
        )
        conn.commit()


def mark_template_in_use(slot_id: str, worker_id: str | None = None) -> None:
    now = utc_now()
    notes = f"In use by {worker_id}" if worker_id else "In use"
    with job_store.connect() as conn:
        conn.execute(
            """
            UPDATE adsp_profile_templates
            SET status = 'in_use', last_used_at = ?, notes = ?, updated_at = ?
            WHERE slot_id = ? AND status IN ('available','in_use')
            """,
            (now, notes, now, slot_id),
        )
        conn.commit()


def mark_template_success(slot_id: str) -> None:
    now = utc_now()
    with job_store.connect() as conn:
        conn.execute(
            """
            UPDATE adsp_profile_templates
            SET status = 'available',
                total_success = total_success + 1,
                notes = CASE
                    WHEN notes = 'manual_rebuild_requested' THEN notes
                    ELSE 'Last item completed without a white screen block'
                END,
                updated_at = ?
            WHERE slot_id = ?
            """,
            (now, slot_id),
        )
        conn.commit()


def mark_template_available(slot_id: str, notes: str = "Available") -> None:
    now = utc_now()
    with job_store.connect() as conn:
        conn.execute(
            """
            UPDATE adsp_profile_templates
            SET status = 'available', notes = ?, updated_at = ?
            WHERE slot_id = ?
            """,
            (notes, now, slot_id),
        )
        conn.commit()


def release_stale_template_slots() -> int:
    """Release template slots left in_use when no DB item is running on them."""
    now = utc_now()
    with job_store.connect() as conn:
        rows = conn.execute(
            """
            SELECT slot_id
            FROM adsp_profile_templates
            WHERE status = 'in_use'
              AND slot_id NOT IN (
                  SELECT DISTINCT profile_slot_id
                  FROM scrape_job_items
                  WHERE status = 'running' AND profile_slot_id IS NOT NULL
              )
            """
        ).fetchall()
        for row in rows:
            conn.execute(
                """
                UPDATE adsp_profile_templates
                SET status = 'available',
                    notes = 'Auto-released stale in_use slot',
                    updated_at = ?
                WHERE slot_id = ?
                """,
                (now, row["slot_id"]),
            )
        conn.commit()
    return len(rows)


def mark_template_white_screen(slot_id: str, profile_id: str | None, message: str) -> None:
    now = utc_now()
    with job_store.connect() as conn:
        conn.execute(
            """
            UPDATE adsp_profile_templates
            SET status = 'rebuilding',
                last_white_screen_at = ?,
                total_white_screen = total_white_screen + 1,
                notes = ?,
                updated_at = ?
            WHERE slot_id = ?
            """,
            (now, message, now, slot_id),
        )
        conn.commit()
    record_profile_rebuild_event(
        slot_id,
        "auto_rebuild_triggered",
        old_profile_id=profile_id,
        message=message,
    )


def disable_template(slot_id: str, notes: str = "Manually disabled") -> None:
    now = utc_now()
    with job_store.connect() as conn:
        conn.execute(
            """
            UPDATE adsp_profile_templates
            SET status = 'disabled', notes = ?, updated_at = ?
            WHERE slot_id = ?
            """,
            (notes, now, slot_id),
        )
        conn.commit()


def release_template(slot_id: str) -> None:
    mark_template_available(slot_id, "Manually released")


def request_rebuild(slot_id: str) -> dict[str, Any]:
    sync_profile_templates_to_db()
    row = get_template(slot_id)
    if not row:
        return {"success": False, "slot_id": slot_id, "message": "Unknown slot."}
    if row["status"] == "in_use":
        now = utc_now()
        with job_store.connect() as conn:
            conn.execute(
                """
                UPDATE adsp_profile_templates
                SET notes = 'manual_rebuild_requested', updated_at = ?
                WHERE slot_id = ?
                """,
                (now, slot_id),
            )
            conn.commit()
        record_profile_rebuild_event(slot_id, "manual_rebuild_requested", old_profile_id=row["adspower_profile_id"])
        return {"success": True, "slot_id": slot_id, "message": "Rebuild requested after current item finishes."}
    return auto_rebuild_profile(slot_id, manual=True, delay_seconds=0)


def consume_rebuild_request(slot_id: str) -> bool:
    row = get_template(slot_id)
    if not row or row.get("notes") != "manual_rebuild_requested":
        return False
    return True


def _post_adspower(path: str, payload: dict[str, Any], timeout: float = 60) -> dict[str, Any]:
    response = httpx.post(_api_url(path), json=payload, timeout=timeout)
    data = response.json()
    if data.get("code") != 0:
        raise RuntimeError(f"AdsPower API failed: {data.get('msg', data)}")
    return data


def _delete_adspower_profile(profile_id: str) -> None:
    if not profile_id:
        return
    try:
        adspower.stop_profile(profile_id)
    except Exception:
        pass
    _post_adspower("/api/v1/user/delete", {"user_ids": [profile_id]}, timeout=30)


def _create_adspower_profile(template: dict[str, Any]) -> str:
    payload = {
        "name": template["display_name"],
        "domain_name": "chewy.com",
        "open_urls": ["https://www.chewy.com/"],
        "group_id": str(getattr(config, "ADSP_PROFILE_GROUP_ID", "0")),
        "remark": f"Controlled Chewy worker slot {template['slot_id']}",
        "user_proxy_config": {
            "proxy_type": template["proxy_type"],
            "proxy_host": template["proxy_host"],
            "proxy_port": template["proxy_port"],
            "proxy_user": template.get("proxy_username") or "",
            "proxy_password": template.get("proxy_password") or "",
        },
        # Minimal AdsPower fingerprint config required by the profile API.
        # This does not implement stealth or captcha solving logic.
        "fingerprint_config": {"automatic_timezone": "1"},
    }
    data = _post_adspower("/api/v1/user/create", payload, timeout=90)
    profile_id = (data.get("data") or {}).get("id") or (data.get("data") or {}).get("user_id")
    if not profile_id:
        raise RuntimeError(f"AdsPower create returned no profile id: {data}")
    return str(profile_id)


def auto_rebuild_profile(
    slot_id: str,
    *,
    reason: str = "white_screen_block",
    manual: bool = False,
    delay_seconds: int | None = None,
) -> dict[str, Any]:
    if not getattr(config, "ADSP_PROFILE_RECOVERY_ENABLED", True):
        return {"success": False, "slot_id": slot_id, "message": "Profile recovery is disabled."}

    sync_profile_templates_to_db()
    template = _template_by_slot(slot_id)
    row = get_template(slot_id)
    old_profile_id = row.get("adspower_profile_id") if row else template.get("adspower_profile_id")
    event_type = "manual_rebuild_requested" if manual else "auto_rebuild_triggered"
    safe_reason = _sanitize_message(reason, template)

    if template["status"] == "disabled":
        message = template.get("notes") or "Template slot is disabled."
        record_profile_rebuild_event(
            slot_id,
            "rebuild_failed",
            old_profile_id=old_profile_id,
            message=message,
            metadata={"reason": safe_reason},
        )
        return {"success": False, "slot_id": slot_id, "old_profile_id": old_profile_id, "message": message}

    now = utc_now()
    with job_store.connect() as conn:
        conn.execute(
            """
            UPDATE adsp_profile_templates
            SET status = 'rebuilding', notes = ?, updated_at = ?
            WHERE slot_id = ?
            """,
            (f"Rebuilding: {safe_reason}", now, slot_id),
        )
        conn.commit()

    record_profile_rebuild_event(
        slot_id,
        event_type,
        old_profile_id=old_profile_id,
        message=f"Triggering auto-rebuild for slot {slot_id}",
        metadata={"masked_proxy": template["proxy_url_masked"], "reason": safe_reason},
    )

    delay = config.ADSP_REBUILD_DELAY_SECONDS if delay_seconds is None else delay_seconds
    if delay > 0:
        time.sleep(delay)

    delete_warning = None
    try:
        if old_profile_id:
            try:
                _delete_adspower_profile(str(old_profile_id))
            except Exception as exc:
                delete_warning = _sanitize_message(str(exc), template)
                lower_warning = delete_warning.lower()
                if "not found" not in lower_warning and "not exist" not in lower_warning and "does not exist" not in lower_warning:
                    raise RuntimeError(f"Could not delete old AdsPower profile {old_profile_id}: {delete_warning}")

        new_profile_id = _create_adspower_profile(template)
        now = utc_now()
        with job_store.connect() as conn:
            conn.execute(
                """
                UPDATE adsp_profile_templates
                SET adspower_profile_id = ?,
                    status = 'available',
                    last_rebuild_at = ?,
                    total_rebuilds = total_rebuilds + 1,
                    notes = ?,
                    updated_at = ?
                WHERE slot_id = ?
                """,
                (
                    new_profile_id,
                    now,
                    f"Rebuild successful. Proxy: {template['proxy_url_masked']}",
                    now,
                    slot_id,
                ),
            )
            conn.commit()
        map_slot_to_profile_id(slot_id, new_profile_id)
        record_profile_rebuild_event(
            slot_id,
            "rebuild_success",
            old_profile_id=old_profile_id,
            new_profile_id=new_profile_id,
            message="AdsPower profile rebuilt successfully.",
            metadata={"masked_proxy": template["proxy_url_masked"], "delete_warning": delete_warning},
        )
        if getattr(config, "ADSP_AUTO_RESUME_AFTER_REBUILD", True):
            record_profile_rebuild_event(
                slot_id,
                "auto_resume_triggered",
                old_profile_id=old_profile_id,
                new_profile_id=new_profile_id,
                message=f"Slot {slot_id} is available for worker auto-resume.",
            )
        return {
            "success": True,
            "slot_id": slot_id,
            "old_profile_id": old_profile_id,
            "new_profile_id": new_profile_id,
            "message": "Rebuild successful.",
        }
    except Exception as exc:
        message = _sanitize_message(str(exc), template)
        now = utc_now()
        with job_store.connect() as conn:
            conn.execute(
                """
                UPDATE adsp_profile_templates
                SET status = 'rebuild_failed',
                    notes = ?,
                    updated_at = ?
                WHERE slot_id = ?
                """,
                (message, now, slot_id),
            )
            conn.commit()
        record_profile_rebuild_event(
            slot_id,
            "rebuild_failed",
            old_profile_id=old_profile_id,
            message=message,
            metadata={"masked_proxy": template["proxy_url_masked"], "reason": safe_reason},
        )
        return {"success": False, "slot_id": slot_id, "old_profile_id": old_profile_id, "message": message}


def ensure_slot_profile(slot_id: str, *, delay_seconds: int = 0) -> dict[str, Any]:
    sync_profile_templates_to_db()
    row = get_template(slot_id)
    if not row:
        return {"success": False, "slot_id": slot_id, "message": "Unknown slot."}
    if row["status"] in {"disabled", "rebuild_failed"}:
        return {"success": False, "slot_id": slot_id, "message": row.get("notes") or row["status"]}
    if row.get("adspower_profile_id"):
        return {"success": True, "slot_id": slot_id, "profile_id": row["adspower_profile_id"], "message": "Profile already mapped."}
    return auto_rebuild_profile(slot_id, reason="initial_profile_create", delay_seconds=delay_seconds)


def get_worker_slot_status(worker_count: int | None = None) -> list[dict[str, Any]]:
    count = worker_count or getattr(config, "ADSP_WORKER_COUNT", 3)
    count = max(1, min(int(count), MAX_TEMPLATE_SLOTS))
    statuses = get_profile_template_status()
    return statuses[:count]


def get_rebuild_events(limit: int = 100) -> list[dict[str, Any]]:
    job_store.init_db()
    with job_store.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM adsp_profile_rebuild_events ORDER BY created_at DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    return job_store.rows_to_dicts(rows)


def test_start_profile(slot_id: str) -> dict[str, Any]:
    row = get_template(slot_id)
    if not row or not row.get("adspower_profile_id"):
        return {"success": False, "slot_id": slot_id, "message": "No AdsPower profile mapped."}
    profile_id = row["adspower_profile_id"]
    try:
        data = adspower.start_profile(profile_id)
        adspower.stop_profile(profile_id)
        return {"success": True, "slot_id": slot_id, "profile_id": profile_id, "message": "Profile started successfully.", "data_keys": list(data.keys())}
    except Exception as exc:
        return {"success": False, "slot_id": slot_id, "profile_id": profile_id, "message": str(exc)}


def main() -> int:
    parser = argparse.ArgumentParser(description="AdsPower fixed profile recovery manager")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("sync", help="Sync CW templates from config/env into scraper_jobs.db")
    sub.add_parser("list", help="List configured profile templates")
    sub.add_parser("events", help="Show recent profile rebuild events")

    rebuild = sub.add_parser("rebuild", help="Force rebuild one fixed profile slot")
    rebuild.add_argument("--slot", required=True)

    release = sub.add_parser("release", help="Release a slot back to available")
    release.add_argument("--slot", required=True)

    disable = sub.add_parser("disable", help="Disable a slot")
    disable.add_argument("--slot", required=True)

    test = sub.add_parser("test-start", help="Test start/stop for a mapped slot profile")
    test.add_argument("--slot", required=True)

    args = parser.parse_args()

    if args.command == "sync":
        sync_profile_templates_to_db()
        print(json.dumps({"status": "synced", "slots": get_profile_template_status()}, indent=2))
        return 0
    if args.command == "list":
        print(json.dumps(get_profile_template_status(), indent=2))
        return 0
    if args.command == "events":
        print(json.dumps(get_rebuild_events(), indent=2))
        return 0
    if args.command == "rebuild":
        print(json.dumps(request_rebuild(args.slot), indent=2))
        return 0
    if args.command == "release":
        release_template(args.slot)
        print(json.dumps({"slot_id": args.slot, "status": "available"}, indent=2))
        return 0
    if args.command == "disable":
        disable_template(args.slot)
        print(json.dumps({"slot_id": args.slot, "status": "disabled"}, indent=2))
        return 0
    if args.command == "test-start":
        print(json.dumps(test_start_profile(args.slot), indent=2))
        return 0
    parser.error("Unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
