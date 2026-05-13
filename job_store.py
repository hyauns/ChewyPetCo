"""SQLite persistence for resumable Chewy scraper jobs."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent
DB_PATH = PROJECT_ROOT / "scraper_jobs.db"
OUTPUT_DIR = PROJECT_ROOT / "output"
JOBS_OUTPUT_DIR = OUTPUT_DIR / "jobs"

JOB_STATUSES = {"created", "running", "paused", "completed", "failed", "cancelled"}
ITEM_STATUSES = {"pending", "running", "done", "failed", "skipped", "paused"}


def utc_now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


def make_job_id() -> str:
    return f"job_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"


def connect(db_path: str | Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.OperationalError:
        pass  # WAL may already be set or DB is temporarily locked
    conn.execute("PRAGMA busy_timeout = 60000")        # 60s wait on lock contention
    conn.execute("PRAGMA synchronous = NORMAL")         # safe with WAL, faster than FULL
    conn.execute("PRAGMA wal_autocheckpoint = 1000")    # auto-checkpoint every 1000 pages
    return conn


def check_db_integrity(db_path: str | Path = DB_PATH) -> dict:
    """Run PRAGMA integrity_check. Returns {"ok": bool, "errors": list[str]}."""
    try:
        c = sqlite3.connect(str(db_path), timeout=10)
        rows = c.execute("PRAGMA integrity_check").fetchall()
        c.close()
        errors = [r[0] for r in rows if r[0] != "ok"]
        return {"ok": len(errors) == 0, "errors": errors}
    except Exception as exc:
        return {"ok": False, "errors": [str(exc)]}


def repair_db(db_path: str | Path = DB_PATH) -> bool:
    """Repair a corrupted DB.

    Strategy:
    1. Try REINDEX first (fixes index-only corruption, fast).
    2. If that fails, dump all data and rebuild into a fresh file.
    """
    db_str = str(db_path)

    # --- Phase 1: Try REINDEX (handles "wrong # of entries in index") ---
    try:
        c = sqlite3.connect(db_str, timeout=30)
        c.execute("REINDEX")
        c.close()
        # Verify after reindex
        check = check_db_integrity(db_path)
        if check["ok"]:
            print("[job_store] REINDEX fixed the DB successfully.")
            return True
        print(f"[job_store] REINDEX ran but integrity still bad: {check['errors'][:3]}")
    except Exception as exc:
        print(f"[job_store] REINDEX failed: {exc}")

    # --- Phase 2: Full dump + rebuild ---
    import shutil
    backup = db_str + ".malformed"
    recovered = db_str + ".recovered"
    sql_dump = recovered + ".sql"
    dump_conn = None
    rebuild_conn = None

    # Clean up leftover files from a previous failed repair
    for leftover in (recovered, sql_dump):
        try:
            Path(leftover).unlink(missing_ok=True)
        except Exception:
            pass

    try:
        dump_conn = sqlite3.connect(db_str)
        with open(sql_dump, "w", encoding="utf-8") as f:
            for line in dump_conn.iterdump():
                # iterdump() emits "CREATE TABLE" — make them idempotent
                fixed = line.replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ")
                fixed = fixed.replace("CREATE INDEX ", "CREATE INDEX IF NOT EXISTS ")
                fixed = fixed.replace("CREATE UNIQUE INDEX ", "CREATE UNIQUE INDEX IF NOT EXISTS ")
                f.write(fixed + "\n")
        dump_conn.close()
        dump_conn = None

        rebuild_conn = sqlite3.connect(recovered)
        with open(sql_dump, "r", encoding="utf-8") as f:
            rebuild_conn.executescript(f.read())
        rebuild_conn.close()
        rebuild_conn = None

        shutil.move(db_str, backup)
        shutil.move(recovered, db_str)
        Path(sql_dump).unlink(missing_ok=True)
        print(f"[job_store] DB recovered via dump+rebuild. Backup: {backup}")
        return True
    except Exception as exc:
        print(f"[job_store] DB dump+rebuild failed: {exc}")
        for c in (dump_conn, rebuild_conn):
            if c:
                try:
                    c.close()
                except Exception:
                    pass
        for leftover in (recovered, sql_dump):
            try:
                Path(leftover).unlink(missing_ok=True)
            except Exception:
                pass
        return False


_REPAIR_ATTEMPTED = False

def init_db(db_path: str | Path = DB_PATH) -> None:
    global _REPAIR_ATTEMPTED
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    JOBS_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    # Auto-repair if DB is malformed (only once per process to avoid loops)
    if not _REPAIR_ATTEMPTED and Path(db_path).exists():
        _REPAIR_ATTEMPTED = True
        integrity = check_db_integrity(db_path)
        if not integrity["ok"]:
            print(f"[job_store] DB integrity check failed: {integrity['errors'][:3]}")
            print("[job_store] Attempting auto-repair...")
            if repair_db(db_path):
                print("[job_store] Auto-repair succeeded.")
            else:
                print("[job_store] Auto-repair failed. DB may need manual recovery.")
    with connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS scrape_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('created','running','paused','completed','failed','cancelled')),
                total_urls INTEGER NOT NULL DEFAULT 0,
                completed_count INTEGER NOT NULL DEFAULT 0,
                failed_count INTEGER NOT NULL DEFAULT 0,
                skipped_count INTEGER NOT NULL DEFAULT 0,
                pending_count INTEGER NOT NULL DEFAULT 0,
                mode TEXT NOT NULL,
                confidence_threshold INTEGER NOT NULL DEFAULT 75,
                fallback_enabled INTEGER NOT NULL DEFAULT 0,
                save_grouped_output INTEGER NOT NULL DEFAULT 1,
                input_file_path TEXT,
                output_dir TEXT NOT NULL,
                last_error TEXT,
                notes TEXT,
                max_attempts INTEGER NOT NULL DEFAULT 3,
                delay_seconds REAL NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS scrape_job_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
                index_number INTEGER NOT NULL,
                input_url TEXT NOT NULL,
                final_url TEXT,
                source_product_id TEXT,
                detected_product_id TEXT,
                status TEXT NOT NULL CHECK(status IN ('pending','running','done','failed','skipped','paused')),
                attempts INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL DEFAULT 3,
                started_at TEXT,
                finished_at TEXT,
                updated_at TEXT NOT NULL,
                duration_seconds REAL,
                page_kind TEXT,
                architecture TEXT,
                confidence_score REAL,
                grouped_output_path TEXT,
                normalized_output_path TEXT,
                validation_output_path TEXT,
                diagnostic_output_path TEXT,
                run_log_path TEXT,
                error_type TEXT,
                error_message TEXT,
                warnings_json TEXT,
                metadata_json TEXT,
                FOREIGN KEY(job_id) REFERENCES scrape_jobs(job_id) ON DELETE CASCADE,
                UNIQUE(job_id, index_number)
            );

            CREATE INDEX IF NOT EXISTS idx_scrape_job_items_job_status
                ON scrape_job_items(job_id, status, index_number);
            CREATE INDEX IF NOT EXISTS idx_scrape_job_items_updated
                ON scrape_job_items(job_id, updated_at);

            CREATE TABLE IF NOT EXISTS category_discovery_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category_job_id TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                category_url TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('created','running','paused','completed','failed','cancelled')),
                current_page INTEGER NOT NULL DEFAULT 1,
                total_pages_discovered INTEGER NOT NULL DEFAULT 0,
                total_cards_found INTEGER NOT NULL DEFAULT 0,
                total_urls_found INTEGER NOT NULL DEFAULT 0,
                total_urls_after_price_filter INTEGER NOT NULL DEFAULT 0,
                price_min REAL,
                price_max REAL,
                mode TEXT NOT NULL,
                output_dir TEXT NOT NULL,
                max_pages INTEGER,
                last_error TEXT
            );

            CREATE TABLE IF NOT EXISTS category_discovery_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category_job_id TEXT NOT NULL,
                page_number INTEGER NOT NULL,
                source_category_url TEXT NOT NULL,
                product_url TEXT NOT NULL,
                product_id TEXT,
                title TEXT,
                brand TEXT,
                card_price REAL,
                card_price_min REAL,
                card_price_max REAL,
                card_price_raw TEXT,
                image_url TEXT,
                rating REAL,
                review_count INTEGER,
                status TEXT NOT NULL CHECK(status IN ('discovered','filtered_in','filtered_out','duplicate','invalid')),
                filter_reason TEXT,
                metadata_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(category_job_id) REFERENCES category_discovery_jobs(category_job_id) ON DELETE CASCADE,
                UNIQUE(category_job_id, product_url)
            );

            CREATE INDEX IF NOT EXISTS idx_category_items_job_status
                ON category_discovery_items(category_job_id, status);
                
            CREATE TABLE IF NOT EXISTS adsp_profile_pool (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_id TEXT NOT NULL UNIQUE,
                label TEXT,
                status TEXT NOT NULL DEFAULT 'available' CHECK(status IN ('available','in_use','quarantined','disabled')),
                last_used_at TEXT,
                last_white_screen_at TEXT,
                quarantine_until TEXT,
                total_attempts INTEGER NOT NULL DEFAULT 0,
                total_success INTEGER NOT NULL DEFAULT 0,
                total_white_screen INTEGER NOT NULL DEFAULT 0,
                notes TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS white_screen_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                job_id TEXT,
                item_id INTEGER,
                input_url TEXT,
                profile_id TEXT NOT NULL,
                proxy_uuid TEXT,
                event_type TEXT NOT NULL CHECK(event_type IN ('detected','profile_quarantined','retry_scheduled','retry_started','retry_success','retry_failed','all_profiles_exhausted','manual_resume_required')),
                detection_confidence REAL,
                signals_json TEXT,
                screenshot_path TEXT,
                html_snapshot_path TEXT,
                message TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS adsp_profile_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slot_id TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL,
                proxy_type TEXT,
                proxy_host TEXT,
                proxy_port TEXT,
                proxy_username_masked TEXT,
                adspower_profile_id TEXT,
                status TEXT NOT NULL DEFAULT 'available'
                    CHECK(status IN ('available','in_use','rebuilding','disabled','rebuild_failed')),
                last_rebuild_at TEXT,
                last_used_at TEXT,
                last_white_screen_at TEXT,
                total_rebuilds INTEGER NOT NULL DEFAULT 0,
                total_success INTEGER NOT NULL DEFAULT 0,
                total_white_screen INTEGER NOT NULL DEFAULT 0,
                notes TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS adsp_profile_rebuild_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                slot_id TEXT NOT NULL,
                old_profile_id TEXT,
                new_profile_id TEXT,
                event_type TEXT NOT NULL,
                message TEXT,
                metadata_json TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS chewy_product_registry (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id TEXT NOT NULL UNIQUE,
                canonical_url TEXT,
                latest_url TEXT,
                first_seen_category_job_id TEXT,
                first_seen_at TEXT,
                last_seen_category_job_id TEXT,
                last_seen_at TEXT,
                discovery_count INTEGER NOT NULL DEFAULT 1,
                extraction_status TEXT NOT NULL DEFAULT 'never_extracted' CHECK(extraction_status IN ('never_extracted','extracted_success','extracted_failed','skipped_existing')),
                grouped_output_path TEXT,
                normalized_output_path TEXT,
                validation_output_path TEXT,
                confidence_score REAL,
                last_pdp_job_id TEXT,
                last_error_type TEXT,
                last_error_message TEXT,
                notes TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS chewy_product_url_aliases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id TEXT NOT NULL,
                url TEXT NOT NULL,
                slug TEXT,
                category_job_id TEXT,
                seen_at TEXT NOT NULL,
                UNIQUE(product_id, url)
            );
            """
        )
        try:
            conn.execute("ALTER TABLE category_discovery_jobs ADD COLUMN max_pages INTEGER")
        except sqlite3.OperationalError:
            pass
            
        # Phase 4/6 scrape_job_items alterations. Run independently so a
        # pre-existing older column does not prevent newer columns being added.
        for ddl in [
            "ALTER TABLE scrape_job_items ADD COLUMN profile_id_used TEXT",
            "ALTER TABLE scrape_job_items ADD COLUMN profile_attempts_json TEXT",
            "ALTER TABLE scrape_job_items ADD COLUMN white_screen_count INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE scrape_job_items ADD COLUMN last_white_screen_at TEXT",
            "ALTER TABLE scrape_job_items ADD COLUMN retry_queue_status TEXT",
            "ALTER TABLE scrape_job_items ADD COLUMN profile_slot_id TEXT",
            "ALTER TABLE scrape_job_items ADD COLUMN worker_id TEXT",
        ]:
            try:
                conn.execute(ddl)
            except sqlite3.OperationalError:
                pass

        for ddl in [
            "ALTER TABLE adsp_profile_pool ADD COLUMN total_proxy_failures INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE adsp_profile_pool ADD COLUMN consecutive_proxy_failures INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE adsp_profile_pool ADD COLUMN last_proxy_failure_at TEXT",
        ]:
            try:
                conn.execute(ddl)
            except sqlite3.OperationalError:
                pass


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def normalize_json(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


def create_job(
    *,
    name: str,
    urls: list[str],
    mode: str,
    confidence_threshold: int = 75,
    fallback_enabled: bool = False,
    save_grouped_output: bool = True,
    input_file_path: str | None = None,
    output_dir: str | None = None,
    max_attempts: int = 3,
    delay_seconds: float = 0,
    notes: str = "",
    job_id: str | None = None,
) -> str:
    init_db()
    clean_urls = [url.strip() for url in urls if url and url.strip()]
    if not clean_urls:
        raise ValueError("Cannot create a job without URLs.")

    job_id = job_id or make_job_id()
    job_output_dir = Path(output_dir) if output_dir else JOBS_OUTPUT_DIR / job_id
    job_output_dir.mkdir(parents=True, exist_ok=True)
    now = utc_now()

    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            INSERT INTO scrape_jobs (
                job_id, name, created_at, updated_at, status, total_urls,
                completed_count, failed_count, skipped_count, pending_count,
                mode, confidence_threshold, fallback_enabled, save_grouped_output,
                input_file_path, output_dir, last_error, notes, max_attempts, delay_seconds
            )
            VALUES (?, ?, ?, ?, 'created', ?, 0, 0, 0, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)
            """,
            (
                job_id,
                name.strip() or job_id,
                now,
                now,
                len(clean_urls),
                len(clean_urls),
                mode,
                int(confidence_threshold),
                1 if fallback_enabled else 0,
                1 if save_grouped_output else 0,
                input_file_path,
                str(job_output_dir.resolve()),
                notes,
                int(max_attempts),
                float(delay_seconds),
            ),
        )
        for index, url in enumerate(clean_urls, start=1):
            conn.execute(
                """
                INSERT INTO scrape_job_items (
                    job_id, index_number, input_url, status, attempts, max_attempts,
                    updated_at, warnings_json, metadata_json
                )
                VALUES (?, ?, ?, 'pending', 0, ?, ?, '[]', '{}')
                """,
                (job_id, index, url, int(max_attempts), now),
            )
        conn.commit()
    update_job_counts(job_id)
    return job_id


def get_job(job_id: str) -> dict[str, Any] | None:
    init_db()
    with connect() as conn:
        row = conn.execute("SELECT * FROM scrape_jobs WHERE job_id = ?", (job_id,)).fetchone()
    return row_to_dict(row)


def list_jobs(limit: int = 200) -> list[dict[str, Any]]:
    init_db()
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM scrape_jobs ORDER BY updated_at DESC, id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    return rows_to_dicts(rows)


def get_job_items(job_id: str, status: str | None = None, limit: int | None = None) -> list[dict[str, Any]]:
    init_db()
    params: list[Any] = [job_id]
    where = "WHERE job_id = ?"
    if status and status != "all":
        where += " AND status = ?"
        params.append(status)
    sql = f"SELECT * FROM scrape_job_items {where} ORDER BY index_number ASC"
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return rows_to_dicts(rows)


def get_item(item_id: int) -> dict[str, Any] | None:
    init_db()
    with connect() as conn:
        row = conn.execute("SELECT * FROM scrape_job_items WHERE id = ?", (int(item_id),)).fetchone()
    return row_to_dict(row)


def _existing_path(path_value: Any) -> Path | None:
    if not path_value:
        return None
    try:
        path = Path(str(path_value))
    except (TypeError, ValueError):
        return None
    return path if path.exists() else None


def registry_success_has_usable_output(registry_item: dict[str, Any] | sqlite3.Row | None, threshold: int) -> bool:
    """Return True only when a registry success still points to usable local JSON output."""
    if not registry_item:
        return False
    item = row_to_dict(registry_item) if isinstance(registry_item, sqlite3.Row) else registry_item
    if item.get("extraction_status") != "extracted_success":
        return False

    product_id = str(item.get("product_id") or "").strip()
    grouped_path = _existing_path(item.get("grouped_output_path"))
    if not grouped_path and product_id:
        grouped_path = _existing_path(OUTPUT_DIR / "grouped_products" / f"chewy_grouped_by_flavor_{product_id}.json")
    if not grouped_path:
        return False

    try:
        data = json.loads(grouped_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    products = data.get("products")
    if not isinstance(products, list) or not products:
        return False
    for product in products:
        if not isinstance(product, dict) or not product.get("title"):
            return False
        variants = product.get("variants")
        if not isinstance(variants, list) or not variants:
            return False

    validation_path = _existing_path(item.get("validation_output_path"))
    if not validation_path and product_id:
        validation_path = _existing_path(OUTPUT_DIR / "validation" / f"chewy_validation_{product_id}.json")
    if not validation_path:
        return False
    try:
        validation = json.loads(validation_path.read_text(encoding="utf-8"))
        if validation.get("is_valid") is False:
            return False
        score = float(validation.get("confidence_score"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return False
    return score is not None and score >= float(threshold)


def update_job(job_id: str, **fields: Any) -> None:
    if not fields:
        return
    init_db()
    fields["updated_at"] = utc_now()
    assignments = ", ".join(f"{key} = ?" for key in fields)
    values = list(fields.values()) + [job_id]
    with connect() as conn:
        conn.execute(f"UPDATE scrape_jobs SET {assignments} WHERE job_id = ?", values)


def set_job_status(job_id: str, status: str, *, last_error: str | None = None) -> None:
    if status not in JOB_STATUSES:
        raise ValueError(f"Invalid job status: {status}")
    fields: dict[str, Any] = {"status": status}
    if last_error is not None:
        fields["last_error"] = last_error
    update_job(job_id, **fields)


def update_item(item_id: int, **fields: Any) -> None:
    if not fields:
        return
    init_db()
    if "status" in fields and fields["status"] not in ITEM_STATUSES:
        raise ValueError(f"Invalid item status: {fields['status']}")
    fields["updated_at"] = utc_now()
    assignments = ", ".join(f"{key} = ?" for key in fields)
    values = list(fields.values()) + [int(item_id)]
    with connect() as conn:
        conn.execute(f"UPDATE scrape_job_items SET {assignments} WHERE id = ?", values)


def update_item_status(item_id: int, status: str, **fields: Any) -> None:
    fields["status"] = status
    update_item(item_id, **fields)


def increment_attempt_and_start(item_id: int, *, run_log_path: str, source_product_id: str | None = None) -> int:
    init_db()
    now = utc_now()
    with connect() as conn:
        conn.execute(
            """
            UPDATE scrape_job_items
            SET attempts = attempts + 1,
                status = 'running',
                started_at = ?,
                finished_at = NULL,
                updated_at = ?,
                run_log_path = ?,
                source_product_id = COALESCE(?, source_product_id),
                error_type = NULL,
                error_message = NULL
            WHERE id = ?
            """,
            (now, now, run_log_path, source_product_id, int(item_id)),
        )
        row = conn.execute("SELECT attempts FROM scrape_job_items WHERE id = ?", (int(item_id),)).fetchone()
    return int(row["attempts"])


def update_job_counts(job_id: str) -> dict[str, int]:
    init_db()
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT status, COUNT(*) AS count
            FROM scrape_job_items
            WHERE job_id = ?
            GROUP BY status
            """,
            (job_id,),
        ).fetchall()
        counts = {row["status"]: int(row["count"]) for row in rows}
        total = sum(counts.values())
        completed = counts.get("done", 0)
        failed = counts.get("failed", 0)
        skipped = counts.get("skipped", 0)
        pending = counts.get("pending", 0) + counts.get("running", 0) + counts.get("paused", 0)
        conn.execute(
            """
            UPDATE scrape_jobs
            SET total_urls = ?, completed_count = ?, failed_count = ?,
                skipped_count = ?, pending_count = ?, updated_at = ?
            WHERE job_id = ?
            """,
            (total, completed, failed, skipped, pending, utc_now(), job_id),
        )
    return {
        "total_urls": total,
        "completed_count": completed,
        "failed_count": failed,
        "skipped_count": skipped,
        "pending_count": pending,
    }


def mark_stale_running_items(job_id: str, stale_minutes: int = 30) -> int:
    init_db()
    cutoff = (datetime.utcnow() - timedelta(minutes=stale_minutes)).isoformat(timespec="seconds")
    warning = "Previous run was interrupted while this item was running."
    now = utc_now()
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, warnings_json, metadata_json
            FROM scrape_job_items
            WHERE job_id = ? AND status = 'running' AND updated_at < ?
            """,
            (job_id, cutoff),
        ).fetchall()
        for row in rows:
            warnings = json.loads(row["warnings_json"] or "[]")
            if warning not in warnings:
                warnings.append(warning)
            metadata = json.loads(row["metadata_json"] or "{}")
            metadata["stale_running_reset_at"] = now
            conn.execute(
                """
                UPDATE scrape_job_items
                SET status = 'pending',
                    error_type = 'unknown_error',
                    error_message = ?,
                    warnings_json = ?,
                    metadata_json = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (warning, normalize_json(warnings), normalize_json(metadata), now, int(row["id"])),
            )
    update_job_counts(job_id)
    return len(rows)


def mark_orphan_running_items(job_id: str) -> int:
    """Reset running items when the parent job is no longer running."""
    job = get_job(job_id)
    if not job or job.get("status") == "running":
        return 0
    init_db()
    warning = "Previous run stopped while this item was running."
    now = utc_now()
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, warnings_json, metadata_json
            FROM scrape_job_items
            WHERE job_id = ? AND status = 'running'
            """,
            (job_id,),
        ).fetchall()
        for row in rows:
            warnings = json.loads(row["warnings_json"] or "[]")
            if warning not in warnings:
                warnings.append(warning)
            metadata = json.loads(row["metadata_json"] or "{}")
            metadata["orphan_running_reset_at"] = now
            conn.execute(
                """
                UPDATE scrape_job_items
                SET status = 'pending',
                    error_type = 'unknown_error',
                    error_message = ?,
                    warnings_json = ?,
                    metadata_json = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (warning, normalize_json(warnings), normalize_json(metadata), now, int(row["id"])),
            )
    update_job_counts(job_id)
    return len(rows)


def get_next_item(
    job_id: str,
    *,
    retry_failed: bool = False,
    include_paused: bool = False,
    reprocess_completed: bool = False,
    force_retry: bool = False,
) -> dict[str, Any] | None:
    init_db()
    statuses = ["'pending'"]
    if include_paused:
        statuses.append("'paused'")
    if reprocess_completed:
        statuses.append("'done'")
        
    status_in_clause = ",".join(statuses)
    
    failed_condition = "1=0"
    if retry_failed:
        if force_retry:
            failed_condition = "status = 'failed'"
        else:
            failed_condition = "status = 'failed' AND attempts < max_attempts"

    with connect() as conn:
        row = conn.execute(
            f"""
            SELECT * FROM scrape_job_items
            WHERE job_id = ?
            AND (status IN ({status_in_clause}) OR ({failed_condition}))
            ORDER BY
                CASE status
                    WHEN 'pending' THEN 0
                    WHEN 'failed' THEN 0
                    WHEN 'paused' THEN 1
                    WHEN 'done' THEN 2
                    ELSE 3
                END,
                index_number ASC
            LIMIT 1
            """,
            [job_id],
        ).fetchone()
    return row_to_dict(row)


def claim_next_item(
    job_id: str,
    *,
    retry_failed: bool = False,
    include_paused: bool = False,
    force_retry: bool = False,
    reprocess_completed: bool = False,
    worker_id: str | None = None,
    profile_slot_id: str | None = None,
) -> dict[str, Any] | None:
    """Atomically claim one unfinished item for a worker.

    This is used by the controlled multi-worker runner. It prevents two
    workers from selecting the same pending URL before either one has time to
    mark it running.
    """
    init_db()
    statuses = ["'pending'"]
    if include_paused:
        statuses.append("'paused'")
    if reprocess_completed:
        statuses.append("'done'")

    failed_condition = "1=0"
    if retry_failed:
        if force_retry:
            failed_condition = "status = 'failed'"
        else:
            failed_condition = "status = 'failed' AND attempts < max_attempts"

    status_in_clause = ",".join(statuses)
    now = utc_now()
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            f"""
            SELECT *
            FROM scrape_job_items
            WHERE job_id = ?
              AND (status IN ({status_in_clause}) OR ({failed_condition}))
            ORDER BY
                CASE status
                    WHEN 'pending' THEN 0
                    WHEN 'failed' THEN 0
                    WHEN 'paused' THEN 1
                    WHEN 'done' THEN 2
                    ELSE 3
                END,
                index_number ASC
            LIMIT 1
            """,
            (job_id,),
        ).fetchone()
        if not row:
            conn.commit()
            return None

        metadata = json.loads(row["metadata_json"] or "{}")
        if worker_id:
            metadata["claimed_by_worker"] = worker_id
        if profile_slot_id:
            metadata["claimed_profile_slot_id"] = profile_slot_id
        metadata["claimed_at"] = now

        conn.execute(
            """
            UPDATE scrape_job_items
            SET status = 'running',
                worker_id = ?,
                profile_slot_id = ?,
                updated_at = ?,
                metadata_json = ?
            WHERE id = ?
            """,
            (
                worker_id,
                profile_slot_id,
                now,
                normalize_json(metadata),
                int(row["id"]),
            ),
        )
        conn.commit()

    return get_item(int(row["id"]))


def reset_failed_items(job_id: str, *, force: bool = False) -> int:
    init_db()
    now = utc_now()
    condition = "status = 'failed'"
    if not force:
        condition += " AND attempts < max_attempts"
    with connect() as conn:
        cur = conn.execute(
            f"""
            UPDATE scrape_job_items
            SET status = 'pending',
                error_message = CASE
                    WHEN error_message IS NULL THEN 'Queued for retry.'
                    ELSE error_message || ' Queued for retry.'
                END,
                updated_at = ?
            WHERE job_id = ? AND {condition}
            """,
            (now, job_id),
        )
        count = cur.rowcount
    update_job_counts(job_id)
    return int(count)


def skip_item(item_id: int, reason: str = "Skipped manually.") -> None:
    update_item_status(
        item_id,
        "skipped",
        finished_at=utc_now(),
        error_type="skipped",
        error_message=reason,
    )


def skip_current_item(job_id: str) -> dict[str, Any] | None:
    init_db()
    with connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM scrape_job_items
            WHERE job_id = ? AND status IN ('running','paused','pending')
            ORDER BY CASE status WHEN 'running' THEN 0 WHEN 'paused' THEN 1 ELSE 2 END, index_number ASC
            LIMIT 1
            """,
            (job_id,),
        ).fetchone()
    item = row_to_dict(row)
    if item:
        skip_item(int(item["id"]))
        update_job_counts(job_id)
    return item


def get_job_summary(job_id: str) -> dict[str, Any]:
    job = get_job(job_id)
    if not job:
        raise ValueError(f"Job not found: {job_id}")
    items = get_job_items(job_id)
    confidence_scores = [
        float(item["confidence_score"])
        for item in items
        if item.get("confidence_score") is not None and item.get("status") == "done"
    ]
    failure_breakdown: dict[str, int] = {}
    products_generated = 0
    last_processed_index = None
    first_active_resume_index = None
    first_paused_index = None

    for item in items:
        if item.get("status") in {"done", "failed", "skipped", "paused"}:
            last_processed_index = item.get("index_number")
        if item.get("status") in {"pending", "running", "failed"} and first_active_resume_index is None:
            first_active_resume_index = item.get("index_number")
        if item.get("status") == "paused" and first_paused_index is None:
            first_paused_index = item.get("index_number")
        if item.get("status") == "done" and item.get("grouped_output_path"):
            products_generated += 1
        if item.get("status") == "failed":
            key = item.get("error_type") or "unknown_error"
            failure_breakdown[key] = failure_breakdown.get(key, 0) + 1

    total = int(job["total_urls"] or 0)
    completed = int(job["completed_count"] or 0)
    skipped = int(job["skipped_count"] or 0)
    failed = int(job["failed_count"] or 0)
    duration = None
    try:
        created = datetime.fromisoformat(job["created_at"])
        updated = datetime.fromisoformat(job["updated_at"])
        duration = round((updated - created).total_seconds(), 2)
    except (TypeError, ValueError):
        duration = None
    return {
        "job_id": job_id,
        "status": job["status"],
        "total_urls": total,
        "completed_count": completed,
        "failed_count": failed,
        "skipped_count": skipped,
        "pending_count": int(job["pending_count"] or 0),
        "duration": duration,
        "success_rate": round((completed / total) * 100, 2) if total else 0,
        "average_confidence_score": round(sum(confidence_scores) / len(confidence_scores), 2)
        if confidence_scores
        else 0,
        "products_generated": products_generated,
        "failure_breakdown_by_error_type": failure_breakdown,
        "last_processed_index": last_processed_index,
        "next_resume_index": first_active_resume_index or first_paused_index,
    }


try:
    init_db()
except sqlite3.DatabaseError as exc:
    print(f"[job_store] init_db failed: {exc}")


def make_category_job_id() -> str:
    return f"catjob_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"

def create_category_job(
    *,
    name: str,
    category_url: str,
    price_min: float | None = None,
    price_max: float | None = None,
    mode: str = "hybrid",
    start_page: int = 1,
    max_pages: int | None = None,
    output_dir: str | None = None
) -> str:
    init_db()
    job_id = make_category_job_id()
    job_output_dir = Path(output_dir) if output_dir else JOBS_OUTPUT_DIR / job_id
    job_output_dir.mkdir(parents=True, exist_ok=True)
    now = utc_now()
    
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO category_discovery_jobs (
                category_job_id, name, category_url, created_at, updated_at, status,
                price_min, price_max, mode, output_dir, current_page, max_pages
            )
            VALUES (?, ?, ?, ?, ?, 'created', ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id, name.strip() or job_id, category_url.strip(), now, now,
                price_min, price_max, mode, str(job_output_dir.resolve()), start_page, max_pages
            )
        )
        conn.commit()
    return job_id

def get_category_job(job_id: str) -> dict[str, Any] | None:
    init_db()
    with connect() as conn:
        row = conn.execute("SELECT * FROM category_discovery_jobs WHERE category_job_id = ?", (job_id,)).fetchone()
    return row_to_dict(row)

def update_category_job(job_id: str, **fields: Any) -> None:
    if not fields:
        return
    init_db()
    fields["updated_at"] = utc_now()
    assignments = ", ".join(f"{key} = ?" for key in fields)
    values = list(fields.values()) + [job_id]
    with connect() as conn:
        conn.execute(f"UPDATE category_discovery_jobs SET {assignments} WHERE category_job_id = ?", values)

def add_category_item(
    category_job_id: str,
    page_number: int,
    source_category_url: str,
    product_url: str,
    status: str,
    **kwargs: Any
) -> None:
    init_db()
    now = utc_now()
    cols = ["category_job_id", "page_number", "source_category_url", "product_url", "status", "created_at", "updated_at"]
    vals = [category_job_id, page_number, source_category_url, product_url, status, now, now]
    
    for k, v in kwargs.items():
        cols.append(k)
        if isinstance(v, (dict, list)):
            vals.append(normalize_json(v))
        else:
            vals.append(v)
            
    placeholders = ", ".join("?" for _ in cols)
    col_str = ", ".join(cols)
    
    with connect() as conn:
        conn.execute(
            f"""
            INSERT INTO category_discovery_items ({col_str})
            VALUES ({placeholders})
            ON CONFLICT(category_job_id, product_url) DO UPDATE SET
                updated_at = excluded.updated_at,
                status = excluded.status
            """,
            vals
        )
        conn.commit()

def update_category_job_counts(job_id: str) -> None:
    init_db()
    with connect() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS count FROM category_discovery_items WHERE category_job_id = ? GROUP BY status",
            (job_id,)
        ).fetchall()
        counts = {r["status"]: r["count"] for r in rows}
        
        total_found = sum(counts.values())
        filtered_in = counts.get("filtered_in", 0) + counts.get("discovered", 0)
        
        conn.execute(
            """
            UPDATE category_discovery_jobs
            SET total_urls_found = ?,
                total_urls_after_price_filter = ?,
                updated_at = ?
            WHERE category_job_id = ?
            """,
            (total_found, filtered_in, utc_now(), job_id)
        )
        conn.commit()

def list_category_jobs(limit: int = 200) -> list[dict[str, Any]]:
    init_db()
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM category_discovery_jobs ORDER BY updated_at DESC, id DESC LIMIT ?",
            (int(limit),)
        ).fetchall()
    return rows_to_dicts(rows)

def get_category_items(job_id: str) -> list[dict[str, Any]]:
    init_db()
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM category_discovery_items WHERE category_job_id = ? ORDER BY page_number ASC, id ASC",
            (job_id,)
        ).fetchall()
    return rows_to_dicts(rows)

import re

def extract_chewy_product_id(url: str) -> str | None:
    if not url:
        return None
    match = re.search(r"/dp/(\d+)", url)
    if match:
        return match.group(1)
    return None

def check_and_update_product_registry(product_id: str, url: str, category_job_id: str) -> dict[str, Any]:
    now = utc_now()
    with connect() as conn:
        row = conn.execute("SELECT * FROM chewy_product_registry WHERE product_id = ?", (product_id,)).fetchone()
        if not row:
            conn.execute(
                """
                INSERT INTO chewy_product_registry (
                    product_id, canonical_url, latest_url, first_seen_category_job_id, first_seen_at,
                    last_seen_category_job_id, last_seen_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (product_id, url, url, category_job_id, now, category_job_id, now, now, now)
            )
            registry_item = row_to_dict(conn.execute("SELECT * FROM chewy_product_registry WHERE product_id = ?", (product_id,)).fetchone())
            is_new = True
        else:
            conn.execute(
                """
                UPDATE chewy_product_registry 
                SET discovery_count = discovery_count + 1,
                    last_seen_category_job_id = ?,
                    last_seen_at = ?,
                    latest_url = ?,
                    updated_at = ?
                WHERE product_id = ?
                """,
                (category_job_id, now, url, now, product_id)
            )
            registry_item = row_to_dict(conn.execute("SELECT * FROM chewy_product_registry WHERE product_id = ?", (product_id,)).fetchone())
            is_new = False
            
        conn.execute(
            """
            INSERT INTO chewy_product_url_aliases (product_id, url, category_job_id, seen_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(product_id, url) DO NOTHING
            """,
            (product_id, url, category_job_id, now)
        )
        conn.commit()
    
    return {"is_new": is_new, "registry_item": registry_item}

def update_registry_extraction_status(product_id: str, status: str, pdp_job_id: str, **kwargs: Any) -> None:
    now = utc_now()
    update_fields = ["extraction_status = ?", "last_pdp_job_id = ?", "updated_at = ?"]
    params = [status, pdp_job_id, now]
    
    for k, v in kwargs.items():
        update_fields.append(f"{k} = ?")
        params.append(v)
        
    params.append(product_id)
    
    try:
        with connect() as conn:
            conn.execute(f"UPDATE chewy_product_registry SET {', '.join(update_fields)} WHERE product_id = ?", tuple(params))
            conn.commit()
    except sqlite3.OperationalError:
        pass  # Non-critical: registry update failed due to DB lock, item processing continues
