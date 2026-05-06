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
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(db_path: str | Path = DB_PATH) -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    JOBS_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
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
            """
        )


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


def get_next_item(
    job_id: str,
    *,
    retry_failed: bool = False,
    include_paused: bool = False,
    reprocess_completed: bool = False,
) -> dict[str, Any] | None:
    init_db()
    statuses = ["pending"]
    if retry_failed:
        statuses.append("failed")
    if include_paused:
        statuses.append("paused")
    if reprocess_completed:
        statuses.append("done")
    placeholders = ",".join("?" for _ in statuses)
    params: list[Any] = [job_id, *statuses]
    with connect() as conn:
        row = conn.execute(
            f"""
            SELECT * FROM scrape_job_items
            WHERE job_id = ? AND status IN ({placeholders})
            ORDER BY index_number ASC
            LIMIT 1
            """,
            params,
        ).fetchone()
    return row_to_dict(row)


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
    next_resume_index = None

    for item in items:
        if item.get("status") in {"done", "failed", "skipped", "paused"}:
            last_processed_index = item.get("index_number")
        if item.get("status") in {"pending", "running", "paused"} and next_resume_index is None:
            next_resume_index = item.get("index_number")
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
        "next_resume_index": next_resume_index,
    }


init_db()


def make_category_job_id() -> str:
    return f"catjob_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"

def create_category_job(
    *,
    name: str,
    category_url: str,
    price_min: float | None = None,
    price_max: float | None = None,
    mode: str = "hybrid",
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
                price_min, price_max, mode, output_dir
            )
            VALUES (?, ?, ?, ?, ?, 'created', ?, ?, ?, ?)
            """,
            (
                job_id, name.strip() or job_id, category_url.strip(), now, now,
                price_min, price_max, mode, str(job_output_dir.resolve())
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
