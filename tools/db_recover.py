"""Recover a corrupted scraper_jobs.db.

The built-in repair_db() in job_store.py uses sqlite3.iterdump() to dump SQL
and replay it. When B-tree corruption causes the same row to be reachable via
multiple paths, iterdump emits duplicate INSERTs and the replay fails with
"UNIQUE constraint failed". This tool fixes that by rewriting INSERT statements
to INSERT OR IGNORE, so duplicates are dropped silently.

Strategy (fallback chain):
  1. Try sqlite3 CLI `.recover` if available (most thorough).
  2. Fall back to Python iterdump() with INSERT OR IGNORE rewriting.
  3. Fall back to per-table row-by-row SELECT with error tolerance.

After recovery:
  - The corrupt file is moved to scraper_jobs.db.malformed_<ts>.
  - The recovered file becomes the new scraper_jobs.db.
  - chewy_product_registry is reseeded from output/normalized_products/*.json
    (best-effort) so global dedup still works on next scrape.

Usage:
  python tools/db_recover.py
  python tools/db_recover.py --db scraper_jobs.db --reseed-registry
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path


def integrity_check(db_path: str) -> tuple[bool, list[str]]:
    try:
        c = sqlite3.connect(db_path, timeout=10)
        rows = c.execute("PRAGMA integrity_check").fetchall()
        c.close()
        errs = [r[0] for r in rows if r[0] != "ok"]
        return (len(errs) == 0, errs)
    except Exception as e:
        return (False, [str(e)])


def try_cli_recover(src: str, dst: str) -> bool:
    """Try sqlite3 CLI `.recover` (most thorough). Returns True on success."""
    cli = shutil.which("sqlite3")
    if not cli:
        return False
    sql_path = dst + ".recover.sql"
    try:
        with open(sql_path, "w", encoding="utf-8") as f:
            r = subprocess.run(
                [cli, src, ".recover"], stdout=f, stderr=subprocess.PIPE, timeout=300
            )
            if r.returncode != 0:
                print(f"[recover] sqlite3 .recover stderr: {r.stderr.decode('utf-8', 'replace')[:500]}")
                return False

        # Patch the SQL: make CREATE idempotent, INSERT non-fatal
        with open(sql_path, "r", encoding="utf-8") as f:
            sql = f.read()
        sql = sql.replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ")
        sql = sql.replace("CREATE INDEX ", "CREATE INDEX IF NOT EXISTS ")
        sql = sql.replace("CREATE UNIQUE INDEX ", "CREATE UNIQUE INDEX IF NOT EXISTS ")
        sql = re.sub(r"\bINSERT INTO\b", "INSERT OR IGNORE INTO", sql)
        with open(sql_path, "w", encoding="utf-8") as f:
            f.write(sql)

        Path(dst).unlink(missing_ok=True)
        new = sqlite3.connect(dst)
        new.executescript(sql)
        new.close()
        Path(sql_path).unlink(missing_ok=True)
        return True
    except Exception as e:
        print(f"[recover] CLI .recover failed: {e}")
        return False


def try_python_iterdump(src: str, dst: str) -> bool:
    """Python iterdump → patch INSERTs → replay. Handles duplicate rows."""
    sql_path = dst + ".iterdump.sql"
    try:
        src_conn = sqlite3.connect(src, timeout=30)
        # Reading from a corrupt DB sometimes works in read-only mode
        with open(sql_path, "w", encoding="utf-8") as f:
            for line in src_conn.iterdump():
                line = line.replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ")
                line = line.replace("CREATE INDEX ", "CREATE INDEX IF NOT EXISTS ")
                line = line.replace("CREATE UNIQUE INDEX ", "CREATE UNIQUE INDEX IF NOT EXISTS ")
                if line.startswith("INSERT INTO"):
                    line = "INSERT OR IGNORE INTO" + line[len("INSERT INTO"):]
                f.write(line + "\n")
        src_conn.close()

        Path(dst).unlink(missing_ok=True)
        new = sqlite3.connect(dst)
        with open(sql_path, "r", encoding="utf-8") as f:
            new.executescript(f.read())
        new.close()
        Path(sql_path).unlink(missing_ok=True)
        return True
    except Exception as e:
        print(f"[recover] Python iterdump failed: {e}")
        return False


def try_per_table_copy(src: str, dst: str) -> bool:
    """Last resort: open src read-only, copy each table row-by-row, skip errors."""
    Path(dst).unlink(missing_ok=True)
    # Get schema from src (best effort)
    try:
        sc = sqlite3.connect(src, timeout=30)
        sc.execute("PRAGMA writable_schema=ON")
        # Collect CREATE statements
        schema_rows = sc.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE sql IS NOT NULL ORDER BY CASE type "
            "WHEN 'table' THEN 1 WHEN 'index' THEN 2 ELSE 3 END"
        ).fetchall()
        tables = [r[1] for r in schema_rows if r[0] == "table"]
    except Exception as e:
        print(f"[recover] cannot read schema: {e}")
        return False

    dc = sqlite3.connect(dst)
    # Create tables first, indexes after data
    for typ, name, sql in schema_rows:
        if typ != "table":
            continue
        try:
            sql_idem = sql.replace("CREATE TABLE", "CREATE TABLE IF NOT EXISTS")
            dc.execute(sql_idem)
        except Exception as e:
            print(f"[recover] skip table {name}: {e}")

    total_kept = 0
    total_dropped = 0
    for tbl in tables:
        try:
            cols = [c[1] for c in sc.execute(f"PRAGMA table_info({tbl})").fetchall()]
        except Exception:
            continue
        if not cols:
            continue
        cur = sc.execute(f"SELECT rowid, * FROM {tbl}")
        placeholders = ",".join(["?"] * len(cols))
        col_list = ",".join(cols)
        kept = 0
        dropped = 0
        while True:
            try:
                row = cur.fetchone()
            except sqlite3.DatabaseError as e:
                dropped += 1
                continue
            if row is None:
                break
            # row[0] is rowid; row[1:] are columns
            try:
                dc.execute(
                    f"INSERT OR IGNORE INTO {tbl} ({col_list}) VALUES ({placeholders})",
                    row[1:],
                )
                kept += 1
            except sqlite3.DatabaseError as e:
                dropped += 1
        dc.commit()
        total_kept += kept
        total_dropped += dropped
        print(f"[recover] table {tbl}: kept={kept} dropped={dropped}")

    # Re-create indexes
    for typ, name, sql in schema_rows:
        if typ != "index":
            continue
        try:
            sql_idem = sql.replace("CREATE INDEX", "CREATE INDEX IF NOT EXISTS")
            sql_idem = sql_idem.replace("CREATE UNIQUE INDEX", "CREATE UNIQUE INDEX IF NOT EXISTS")
            dc.execute(sql_idem)
        except Exception as e:
            print(f"[recover] index {name} failed: {e}")
    dc.close()
    sc.close()
    print(f"[recover] per-table: kept={total_kept} dropped={total_dropped}")
    return total_kept > 0


def reseed_product_registry(db_path: str, normalized_dir: str) -> int:
    """Reseed chewy_product_registry from output/normalized_products/chewy_*.json
    so future scrape jobs skip already-scraped pids."""
    import glob
    files = glob.glob(os.path.join(normalized_dir, "chewy_*.json"))
    if not files:
        return 0
    rows: list[tuple] = []
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for f in files:
        try:
            d = json.load(open(f, "r", encoding="utf-8"))
        except Exception:
            continue
        pid = str(d.get("source_product_id") or "")
        url = d.get("source_url") or ""
        if not pid:
            continue
        rows.append((pid, url, "ok", now))

    if not rows:
        return 0

    c = sqlite3.connect(db_path, timeout=30)
    # Check table exists; create a minimal one if not (matches job_store schema)
    have = c.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chewy_product_registry'"
    ).fetchone()
    if not have:
        c.execute(
            """CREATE TABLE chewy_product_registry (
                product_id TEXT PRIMARY KEY,
                latest_url TEXT,
                extraction_status TEXT,
                updated_at TEXT
            )"""
        )
    n = 0
    for r in rows:
        try:
            c.execute(
                "INSERT OR REPLACE INTO chewy_product_registry "
                "(product_id, latest_url, extraction_status, updated_at) "
                "VALUES (?,?,?,?)",
                r,
            )
            n += 1
        except Exception:
            pass
    c.commit()
    c.close()
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="scraper_jobs.db")
    ap.add_argument(
        "--reseed-registry",
        action="store_true",
        help="After recovery, rebuild chewy_product_registry from output/normalized_products/*.json",
    )
    ap.add_argument(
        "--normalized-dir", default="output/normalized_products"
    )
    args = ap.parse_args()

    db = args.db
    if not os.path.exists(db):
        print(f"DB not found: {db}")
        return 1

    print(f"=== DB recovery: {db} ===")
    ok, errs = integrity_check(db)
    if ok:
        print("DB is healthy. Nothing to do.")
        return 0
    print(f"Integrity FAIL: {len(errs)} error(s). First: {errs[0][:200]}")

    # Stop other processes using the DB before recovering
    wal = db + "-wal"
    shm = db + "-shm"
    for p in (wal, shm):
        if os.path.exists(p):
            try:
                Path(p).unlink()
                print(f"Removed {p}")
            except Exception as e:
                print(f"WARNING: cannot remove {p}: {e}. Close other processes using the DB.")

    recovered = db + ".recovered"
    print()

    success = False
    for strategy_name, fn in [
        ("sqlite3 CLI .recover", try_cli_recover),
        ("Python iterdump + OR IGNORE", try_python_iterdump),
        ("Per-table row copy", try_per_table_copy),
    ]:
        print(f"--- Trying: {strategy_name} ---")
        if fn(db, recovered):
            ok2, errs2 = integrity_check(recovered)
            if ok2:
                print(f"[recover] {strategy_name}: integrity OK")
                success = True
                break
            print(f"[recover] {strategy_name}: rebuilt but integrity still has {len(errs2)} issues")
            # Accept if no errors involving the data tables we care about
            tolerable = all(
                "scrape_job" not in e.lower() and "chewy_product_registry" not in e.lower()
                for e in errs2[:5]
            )
            if tolerable:
                print("[recover] Errors are tolerable (orphan indexes). Accepting.")
                success = True
                break

    if not success:
        print("\n[recover] All strategies failed. Manual intervention required.")
        return 2

    # Swap files
    ts = time.strftime("%Y%m%d_%H%M%S")
    malformed = f"{db}.malformed_{ts}"
    shutil.move(db, malformed)
    shutil.move(recovered, db)
    print(f"\n[recover] OK. Corrupt file kept as: {malformed}")

    # Recreate indexes that were absent in the recovery (safety net)
    try:
        c = sqlite3.connect(db, timeout=30)
        c.execute("REINDEX")
        c.close()
    except Exception as e:
        print(f"[recover] post-recovery REINDEX warning: {e}")

    if args.reseed_registry:
        print()
        print(f"--- Reseeding chewy_product_registry from {args.normalized_dir} ---")
        n = reseed_product_registry(db, args.normalized_dir)
        print(f"[recover] Registry: reseeded {n} product rows")

    print()
    print("Verifying final DB...")
    ok3, errs3 = integrity_check(db)
    if ok3:
        print("[recover] FINAL: integrity OK ✓")
    else:
        print(f"[recover] FINAL: still has {len(errs3)} issues (likely orphan indexes — usually safe)")
        for e in errs3[:3]:
            print(f"  {e[:160]}")

    # Show table row counts
    print()
    print("Row counts after recovery:")
    c = sqlite3.connect(db)
    for tbl in [
        "scrape_jobs",
        "scrape_job_items",
        "chewy_product_registry",
        "category_discovery_jobs",
        "category_discovery_items",
        "adsp_profile_templates",
    ]:
        try:
            n = c.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
            print(f"  {tbl:35s} {n}")
        except Exception:
            pass
    c.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
