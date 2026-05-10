"""Controlled AdsPower profile template recovery for Chewy scraper jobs.

This module manages fixed user-configured slots such as CW_1, CW_2, CW_3.
It does not create unlimited profiles and does not implement captcha solving or
anti-bot bypass logic. Runtime Local/no_proxy fallback is bounded to the fixed
worker slots and is reset back to configured proxies on the next Start/Resume.
"""

from __future__ import annotations

import argparse
import json
import os
import threading
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
SUPPORTED_PROXY_TYPES = {"http", "https", "socks5"}
VALID_TEMPLATE_STATUSES = {"available", "in_use", "rebuilding", "disabled", "rebuild_failed"}
RUNTIME_LOCAL_FALLBACK_MARKER = "runtime_local_fallback"
_ADSPOWER_MUTATION_MIN_INTERVAL_SECONDS = float(os.environ.get("ADSP_API_MUTATION_INTERVAL_SECONDS", "2.5"))
_ADSPOWER_MUTATION_MAX_RETRIES = int(os.environ.get("ADSP_API_MUTATION_MAX_RETRIES", "5"))
_ADSPOWER_TRANSIENT_TOKENS = (
    "too many request",
    "please check",
    "being used",
    "cannot be deleted",
    "browser is starting",
    "please try again",
)


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
    """Parse and validate a configured AdsPower proxy URL."""
    if not proxy_url:
        raise ValueError("Missing proxy URL.")
    if "://" in proxy_url:
        scheme, rest = proxy_url.split("://", 1)
        proxy_type = scheme.lower()
        if proxy_type in SUPPORTED_PROXY_TYPES and "@" not in rest and rest.count(":") >= 3:
            host, port, username, password = rest.split(":", 3)
            if not host or not port:
                raise ValueError("Proxy host or port is missing.")
            return {
                "proxy_type": proxy_type,
                "proxy_host": host,
                "proxy_port": port,
                "proxy_username": username,
                "proxy_password": password,
            }
    if "://" not in proxy_url and proxy_url.count(":") >= 3:
        host, port, username, password = proxy_url.split(":", 3)
        if not host or not port:
            raise ValueError("Proxy host or port is missing.")
        return {
            "proxy_type": "socks5",
            "proxy_host": host,
            "proxy_port": port,
            "proxy_username": username,
            "proxy_password": password,
        }
    parsed = urlparse(proxy_url)
    proxy_type = parsed.scheme.lower()
    if proxy_type not in SUPPORTED_PROXY_TYPES:
        raise ValueError("Only http://, https://, and socks5:// proxies are supported for CW slots.")
    if not parsed.hostname:
        raise ValueError("Proxy host is missing.")
    if not parsed.port:
        raise ValueError("Proxy port is missing.")
    return {
        "proxy_type": proxy_type,
        "proxy_host": parsed.hostname,
        "proxy_port": str(parsed.port),
        "proxy_username": unquote(parsed.username or ""),
        "proxy_password": unquote(parsed.password or ""),
    }


def mask_proxy_url(proxy_url: str) -> str:
    if not proxy_url:
        return "(not configured)"
    try:
        if "://" in proxy_url:
            scheme, compact = proxy_url.split("://", 1)
            if scheme.lower() in SUPPORTED_PROXY_TYPES and "@" not in compact and compact.count(":") >= 3:
                host, port, username, _password = compact.split(":", 3)
                return f"{scheme.lower()}://{_mask_user(username)}:***@{host}:{port}"
        if "://" not in proxy_url and proxy_url.count(":") >= 3:
            host, port, username, _password = proxy_url.split(":", 3)
            return f"socks5://{_mask_user(username)}:***@{host}:{port}"
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


def _is_runtime_local_note(notes: str | None) -> bool:
    return RUNTIME_LOCAL_FALLBACK_MARKER in str(notes or "")


def _runtime_local_note(message: str) -> str:
    return f"{RUNTIME_LOCAL_FALLBACK_MARKER}: {message}"


def _preserve_runtime_local_note(existing_notes: str | None, replacement: str) -> str:
    if _is_runtime_local_note(existing_notes) and not _is_runtime_local_note(replacement):
        return _runtime_local_note(f"active until next Start/Resume; {replacement}")
    return replacement


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
                    if _is_runtime_local_note(existing_notes):
                        notes = existing_notes
                elif existing_status == "disabled" and (
                    existing_notes == "Manually disabled"
                    or "switched AdsPower profile" in existing_notes
                    or "Proxy failed" in existing_notes
                ):
                    status = "disabled"
                    notes = existing_notes
                elif _is_runtime_local_note(existing_notes):
                    status = existing_status if existing_status in {"available", "in_use"} else status
                    notes = existing_notes
                elif existing_status == "rebuild_failed" and template["status"] == "available":
                    status = "rebuild_failed"
                    notes = existing_notes

            profile_id = template.get("adspower_profile_id")
            if existing and _is_runtime_local_note(existing["notes"] or ""):
                profile_id = existing["adspower_profile_id"]
            elif existing and not profile_id:
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


def map_slot_to_profile_id(slot_id: str, profile_id: str, *, notes: str | None = None) -> None:
    template = _template_by_slot(slot_id)
    now = utc_now()
    slot_notes = notes or f"Mapped to AdsPower profile {profile_id}"
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
            (profile_id, slot_notes, now, slot_id),
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
        existing = conn.execute(
            "SELECT notes FROM adsp_profile_templates WHERE slot_id = ?",
            (slot_id,),
        ).fetchone()
        notes = _preserve_runtime_local_note(existing["notes"] if existing else None, notes)
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
        existing = conn.execute(
            "SELECT notes FROM adsp_profile_templates WHERE slot_id = ?",
            (slot_id,),
        ).fetchone()
        existing_notes = existing["notes"] if existing else None
        notes = _preserve_runtime_local_note(
            existing_notes,
            existing_notes if existing_notes == "manual_rebuild_requested" else "Last item completed without a white screen block",
        )
        conn.execute(
            """
            UPDATE adsp_profile_templates
            SET status = 'available',
                total_success = total_success + 1,
                notes = ?,
                updated_at = ?
            WHERE slot_id = ?
            """,
            (notes, now, slot_id),
        )
        conn.commit()


def mark_template_available(slot_id: str, notes: str = "Available") -> None:
    now = utc_now()
    with job_store.connect() as conn:
        existing = conn.execute(
            "SELECT notes FROM adsp_profile_templates WHERE slot_id = ?",
            (slot_id,),
        ).fetchone()
        notes = _preserve_runtime_local_note(existing["notes"] if existing else None, notes)
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
                    notes = CASE
                        WHEN notes LIKE ? THEN notes
                        ELSE 'Auto-released stale in_use slot'
                    END,
                    updated_at = ?
                WHERE slot_id = ?
                """,
                (f"%{RUNTIME_LOCAL_FALLBACK_MARKER}%", now, row["slot_id"]),
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


def detach_profile_mapping(slot_id: str, *, notes: str = "Detached stale AdsPower profile mapping") -> dict[str, Any]:
    sync_profile_templates_to_db()
    row = get_template(slot_id)
    if not row:
        return {"success": False, "slot_id": slot_id, "message": "Unknown slot."}
    old_profile_id = row.get("adspower_profile_id")
    now = utc_now()
    with job_store.connect() as conn:
        conn.execute(
            """
            UPDATE adsp_profile_templates
            SET adspower_profile_id = NULL,
                status = 'available',
                notes = ?,
                updated_at = ?
            WHERE slot_id = ?
            """,
            (notes, now, slot_id),
        )
        if old_profile_id:
            conn.execute(
                """
                UPDATE adsp_profile_pool
                SET status = 'disabled',
                    notes = ?,
                    updated_at = ?
                WHERE profile_id = ?
                """,
                (notes, now, old_profile_id),
            )
        conn.commit()
    record_profile_rebuild_event(
        slot_id,
        "profile_mapping_detached",
        old_profile_id=old_profile_id,
        message=notes,
    )
    return {
        "success": True,
        "slot_id": slot_id,
        "old_profile_id": old_profile_id,
        "message": notes,
    }


def request_rebuild(slot_id: str, *, delete_old_profile: bool = True) -> dict[str, Any]:
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
    return auto_rebuild_profile(slot_id, manual=True, delay_seconds=0, delete_old_profile=delete_old_profile)


def consume_rebuild_request(slot_id: str) -> bool:
    row = get_template(slot_id)
    if not row or row.get("notes") != "manual_rebuild_requested":
        return False
    return True


def _is_transient_adspower_error(message: str) -> bool:
    lower = str(message).lower()
    return any(token in lower for token in _ADSPOWER_TRANSIENT_TOKENS)


def _post_adspower_once(path: str, payload: dict[str, Any], timeout: float = 60) -> dict[str, Any]:
    try:
        response = adspower.safe_api_request("POST", path, json=payload, timeout=timeout)
    except Exception as exc:
        raise RuntimeError(f"AdsPower API request failed for {path}: {exc}") from exc
    try:
        data = response.json()
    except Exception as exc:
        body = response.text[:500] if response.text else ""
        raise RuntimeError(
            f"AdsPower API returned non-JSON response for {path}: "
            f"status={response.status_code} body={body}"
        ) from exc
    if data.get("code") != 0:
        raise RuntimeError(f"AdsPower API failed for {path}: {data.get('msg', data)}")
    return data


def _post_adspower(path: str, payload: dict[str, Any], timeout: float = 60) -> dict[str, Any]:
    """POST to AdsPower with serialized mutation calls and rate-limit backoff."""
    attempts = _ADSPOWER_MUTATION_MAX_RETRIES if path.startswith("/api/v1/user/") else 1
    last_error = ""
    for attempt in range(1, attempts + 1):
        try:
            return _post_adspower_once(path, payload, timeout=timeout)
        except RuntimeError as exc:
            last_error = str(exc)
            if attempt >= attempts or not _is_transient_adspower_error(last_error):
                raise
            time.sleep(min(20.0, _ADSPOWER_MUTATION_MIN_INTERVAL_SECONDS * attempt))
    raise RuntimeError(last_error or f"AdsPower API request failed for {path}")


def _delete_adspower_profile(profile_id: str) -> str | None:
    if not profile_id:
        return None
    # Ensure browser is fully stopped before deleting.
    # Retry stop up to 3 times with increasing wait to avoid "being used" errors.
    for stop_attempt in range(3):
        try:
            stopped = adspower.stop_profile(profile_id)
            if stopped:
                break
        except Exception:
            pass
        time.sleep(3 * (stop_attempt + 1))
    else:
        # Final wait even if all stop attempts returned False
        time.sleep(5)
    time.sleep(3)
    _post_adspower("/api/v1/user/delete", {"user_ids": [profile_id]}, timeout=30)
    return None


def _create_adspower_profile(template: dict[str, Any], *, use_local_network: bool = False) -> str:
    proxy_config = (
        {"proxy_soft": "no_proxy"}
        if use_local_network
        else {
            "proxy_soft": "other",
            "proxy_type": template["proxy_type"],
            "proxy_host": template["proxy_host"],
            "proxy_port": template["proxy_port"],
            "proxy_user": template.get("proxy_username") or "",
            "proxy_password": template.get("proxy_password") or "",
        }
    )
    payload = {
        "name": f"{template['display_name']} Local" if use_local_network else template["display_name"],
        "domain_name": "chewy.com",
        "open_urls": ["https://www.chewy.com/"],
        "group_id": str(getattr(config, "ADSP_PROFILE_GROUP_ID", "0")),
        "remark": f"Controlled Chewy worker slot {template['slot_id']}",
        "user_proxy_config": proxy_config,
        # Keep generated profiles on desktop UA systems. AdsPower can randomize
        # Android/iOS UAs, but those profiles white-screen Chewy in this flow.
        "fingerprint_config": {
            "automatic_timezone": "1",
            "random_ua": {
                "ua_browser": ["chrome"],
                "ua_system_version": [
                    "Windows 10",
                    "Windows 11",
                    "Mac OS X 12",
                    "Mac OS X 13",
                    "Linux",
                ],
            },
            "browser_kernel_config": {"version": "ua_auto", "type": "chrome"},
        },
    }
    data = _post_adspower("/api/v1/user/create", payload, timeout=90)
    profile_id = (
        (data.get("data") or {}).get("id")
        or (data.get("data") or {}).get("profile_id")
        or (data.get("data") or {}).get("user_id")
    )
    if not profile_id:
        raise RuntimeError(f"AdsPower create returned no profile id: {data}")
    return str(profile_id)


def switch_profile_to_local(
    profile_id: str,
    *,
    slot_id: str | None = None,
    reason: str = "proxy_connection_failed",
) -> dict[str, Any]:
    """Switch an AdsPower profile to Local/No Proxy mode and disable its slot."""
    if not profile_id:
        return {"success": False, "profile_id": profile_id, "slot_id": slot_id, "message": "Missing profile id."}

    try:
        adspower.stop_profile(profile_id)
    except Exception:
        pass

    try:
        _post_adspower(
            "/api/v1/user/update",
            {"user_id": profile_id, "user_proxy_config": {"proxy_soft": "no_proxy"}},
            timeout=60,
        )
    except Exception as exc:
        message = str(exc)
        if slot_id:
            now = utc_now()
            with job_store.connect() as conn:
                conn.execute(
                    """
                    UPDATE adsp_profile_templates
                    SET status = 'disabled',
                        notes = ?,
                        updated_at = ?
                    WHERE slot_id = ?
                    """,
                    (f"Proxy failed; AdsPower Local/no_proxy switch failed for {profile_id}: {message}", now, slot_id),
                )
                conn.commit()
            record_profile_rebuild_event(
                slot_id,
                "proxy_local_switch_failed",
                old_profile_id=profile_id,
                message=message,
                metadata={"reason": reason},
            )
        return {"success": False, "profile_id": profile_id, "slot_id": slot_id, "message": message}

    now = utc_now()
    if slot_id:
        with job_store.connect() as conn:
            conn.execute(
                """
                UPDATE adsp_profile_templates
                SET status = 'disabled',
                    notes = ?,
                    updated_at = ?
                WHERE slot_id = ?
                """,
                (f"Proxy failed; switched AdsPower profile {profile_id} to Local. {reason}", now, slot_id),
            )
            conn.commit()
        record_profile_rebuild_event(
            slot_id,
            "proxy_switched_to_local",
            old_profile_id=profile_id,
            message=f"AdsPower profile {profile_id} switched to Local/no_proxy after proxy failures.",
            metadata={"reason": reason},
        )

    return {
        "success": True,
        "profile_id": profile_id,
        "slot_id": slot_id,
        "message": "AdsPower profile switched to Local/no_proxy.",
    }


def auto_rebuild_profile(
    slot_id: str,
    *,
    reason: str = "white_screen_block",
    manual: bool = False,
    delay_seconds: int | None = None,
    delete_old_profile: bool = True,
    use_local_network: bool = False,
) -> dict[str, Any]:
    if not getattr(config, "ADSP_PROFILE_RECOVERY_ENABLED", True):
        return {"success": False, "slot_id": slot_id, "message": "Profile recovery is disabled."}

    sync_profile_templates_to_db()
    template = _template_by_slot(slot_id)
    row = get_template(slot_id)
    old_profile_id = row.get("adspower_profile_id") if row else template.get("adspower_profile_id")
    event_type = (
        "runtime_local_fallback_requested"
        if use_local_network
        else ("manual_rebuild_requested" if manual else "auto_rebuild_triggered")
    )
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
        metadata={
            "masked_proxy": "Local/no_proxy" if use_local_network else template["proxy_url_masked"],
            "reason": safe_reason,
            "runtime_local_fallback": use_local_network,
        },
    )

    delay = config.ADSP_REBUILD_DELAY_SECONDS if delay_seconds is None else delay_seconds
    if delay > 0:
        time.sleep(delay)

    delete_warning = None
    try:
        if old_profile_id and delete_old_profile:
            try:
                delete_warning = _delete_adspower_profile(str(old_profile_id))
            except Exception as exc:
                delete_warning = _sanitize_message(str(exc), template)
                lower_warning = delete_warning.lower()
                best_effort_delete_errors = (
                    "not found",
                    "not exist",
                    "does not exist",
                    "being used",
                    "cannot be deleted",
                )
                if not any(token in lower_warning for token in best_effort_delete_errors):
                    raise RuntimeError(f"Could not delete old AdsPower profile {old_profile_id}: {delete_warning}")
        elif old_profile_id and not delete_old_profile:
            delete_warning = f"Skipped deleting old AdsPower profile {old_profile_id}"

        new_profile_id = _create_adspower_profile(template, use_local_network=use_local_network)
        now = utc_now()
        success_notes = (
            _runtime_local_note(f"using Local/no_proxy until next Start/Resume; reason={safe_reason}")
            if use_local_network
            else f"Rebuild successful. Proxy: {template['proxy_url_masked']}"
        )
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
                    success_notes,
                    now,
                    slot_id,
                ),
            )
            conn.commit()
        map_slot_to_profile_id(slot_id, new_profile_id, notes=success_notes)
        record_profile_rebuild_event(
            slot_id,
            "runtime_local_fallback_success" if use_local_network else "rebuild_success",
            old_profile_id=old_profile_id,
            new_profile_id=new_profile_id,
            message=(
                "AdsPower runtime Local/no_proxy profile created successfully."
                if use_local_network
                else "AdsPower profile rebuilt successfully."
            ),
            metadata={
                "masked_proxy": "Local/no_proxy" if use_local_network else template["proxy_url_masked"],
                "delete_warning": delete_warning,
                "runtime_local_fallback": use_local_network,
            },
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
            "message": "Runtime Local/no_proxy profile created." if use_local_network else "Rebuild successful.",
            "runtime_local_fallback": use_local_network,
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
            "runtime_local_fallback_failed" if use_local_network else "rebuild_failed",
            old_profile_id=old_profile_id,
            message=message,
            metadata={
                "masked_proxy": "Local/no_proxy" if use_local_network else template["proxy_url_masked"],
                "reason": safe_reason,
                "runtime_local_fallback": use_local_network,
            },
        )
        return {"success": False, "slot_id": slot_id, "old_profile_id": old_profile_id, "message": message}


def rebuild_all_slots(*, reason: str = "all_profiles_white_screen") -> dict[str, Any]:
    """Rebuild all CW profile slots. Returns summary with success bool."""
    sync_profile_templates_to_db()
    results = []
    all_ok = True
    for slot_id in get_template_slots():
        row = get_template(slot_id)
        if row and row.get("status") == "disabled":
            results.append({"slot_id": slot_id, "success": False, "message": "disabled"})
            all_ok = False
            continue
        res = auto_rebuild_profile(slot_id, reason=reason, delay_seconds=5)
        results.append(res)
        if not res.get("success"):
            all_ok = False
    new_profile_ids = [
        str(res["new_profile_id"])
        for res in results
        if res.get("success") and res.get("new_profile_id")
    ]
    if not new_profile_ids:
        all_ok = False
    return {"success": all_ok, "slots": results, "new_profile_ids": new_profile_ids}


def rebuild_slots_with_env_proxy_changes(*, delay_seconds: int = 0) -> dict[str, Any]:
    """Rebuild mapped slots whose stored proxy no longer matches current .env config."""
    job_store.init_db()
    templates = {template["slot_id"]: template for template in load_profile_templates_from_config()}
    with job_store.connect() as conn:
        rows = job_store.rows_to_dicts(
            conn.execute("SELECT * FROM adsp_profile_templates ORDER BY slot_id ASC").fetchall()
        )

    results = []
    all_ok = True
    for row in rows:
        slot_id = row["slot_id"]
        template = templates.get(slot_id)
        if not template or template.get("status") != "available":
            continue
        if row.get("status") in {"in_use", "rebuilding"}:
            continue
        if not row.get("adspower_profile_id"):
            continue

        current_proxy = (
            row.get("proxy_type") or "",
            row.get("proxy_host") or "",
            str(row.get("proxy_port") or ""),
            row.get("proxy_username_masked") or "",
        )
        env_proxy = (
            template.get("proxy_type") or "",
            template.get("proxy_host") or "",
            str(template.get("proxy_port") or ""),
            _mask_user(template.get("proxy_username")),
        )
        if current_proxy == env_proxy:
            continue

        result = auto_rebuild_profile(
            slot_id,
            reason="proxy_config_changed_in_env",
            delay_seconds=delay_seconds,
        )
        results.append(result)
        if not result.get("success"):
            all_ok = False

    return {"success": all_ok, "rebuilt_count": len([r for r in results if r.get("success")]), "slots": results}


def restore_runtime_local_slots_from_env(*, delay_seconds: int = 0) -> dict[str, Any]:
    """On a fresh Start/Resume, replace runtime Local/no_proxy profiles with configured proxy profiles."""
    sync_profile_templates_to_db()
    with job_store.connect() as conn:
        rows = job_store.rows_to_dicts(
            conn.execute(
                """
                SELECT slot_id
                FROM adsp_profile_templates
                WHERE notes LIKE ?
                  AND status NOT IN ('in_use','rebuilding')
                ORDER BY slot_id ASC
                """,
                (f"%{RUNTIME_LOCAL_FALLBACK_MARKER}%",),
            ).fetchall()
        )

    results = []
    all_ok = True
    for row in rows:
        result = auto_rebuild_profile(
            row["slot_id"],
            reason="resume_reload_proxy_from_env_after_runtime_local_fallback",
            delay_seconds=delay_seconds,
        )
        results.append(result)
        if not result.get("success"):
            all_ok = False
    return {"success": all_ok, "restored_count": len([r for r in results if r.get("success")]), "slots": results}


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
    rebuild.add_argument(
        "--skip-delete-old",
        action="store_true",
        help="Create/map a new AdsPower profile without deleting the old mapped profile id.",
    )

    detach = sub.add_parser("detach", help="Detach a stale AdsPower profile id from one slot without calling AdsPower delete")
    detach.add_argument("--slot", required=True)
    detach.add_argument("--rebuild", action="store_true", help="Create a new profile immediately after detaching")

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
        print(json.dumps(request_rebuild(args.slot, delete_old_profile=not args.skip_delete_old), indent=2))
        return 0
    if args.command == "detach":
        result = detach_profile_mapping(args.slot)
        if args.rebuild and result.get("success"):
            result["rebuild_result"] = auto_rebuild_profile(
                args.slot,
                reason="detached_stale_profile_mapping",
                manual=True,
                delay_seconds=0,
                delete_old_profile=False,
            )
        print(json.dumps(result, indent=2))
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
