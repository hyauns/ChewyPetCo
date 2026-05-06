"""Resumable job runner for long Chewy PDP scraping lists.

This module intentionally wraps the existing CLI entry point:
    python test_single_product.py "<url>"

It does not import or alter extraction, normalization, grouping, or fallback
logic. State is persisted after every URL in scraper_jobs.db.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import job_store
from ui_file_browser import OUTPUT_DIR, PROJECT_ROOT, read_json_file, write_json_file
from ui_log_parser import detect_redirected_plp, strip_ansi, summarize_error, summarize_run


MODE_OLD = "old_scraper"
MODE_JSON = "json_extractor"
MODE_JSON_FALLBACK = "json_extractor_with_fallback"
JOB_MODES = [MODE_OLD, MODE_JSON, MODE_JSON_FALLBACK]

DISPLAY_MODE = {
    MODE_OLD: "Old scraper only",
    MODE_JSON: "JSON extractor only",
    MODE_JSON_FALLBACK: "JSON extractor + old scraper fallback",
}

TRANSIENT_ERROR_TYPES = {
    "network_error",
    "browser_error",
    "adspower_error",
    "output_missing",
}

HARD_ERROR_TYPES = {
    "redirected_plp",
    "unavailable_product",
    "invalid_url",
    "low_confidence",
    "validation_failed",
}


def now_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


def read_urls_file(path: str | Path) -> list[str]:
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        return [line.strip() for line in handle if line.strip() and not line.lstrip().startswith("#")]


def source_id_from_url(url: str) -> str | None:
    match = re.search(r"/dp/(\d+)", url)
    return match.group(1) if match else None


def is_valid_chewy_url(url: str) -> bool:
    return url.startswith("https://www.chewy.com/")


def mode_flags(mode: str, threshold: int, save_grouped_output: bool) -> dict[str, str]:
    if mode == MODE_OLD:
        return {
            "USE_CHEWY_NEXT_JSON_EXTRACTOR": "false",
            "CHEWY_JSON_FALLBACK_TO_OLD_SCRAPER": "false",
            "CHEWY_JSON_CONFIDENCE_THRESHOLD": str(threshold),
            "CHEWY_JSON_SAVE_GROUPED_OUTPUT": "true" if save_grouped_output else "false",
        }
    if mode == MODE_JSON:
        return {
            "USE_CHEWY_NEXT_JSON_EXTRACTOR": "true",
            "CHEWY_JSON_FALLBACK_TO_OLD_SCRAPER": "false",
            "CHEWY_JSON_CONFIDENCE_THRESHOLD": str(threshold),
            "CHEWY_JSON_SAVE_GROUPED_OUTPUT": "true" if save_grouped_output else "false",
        }
    return {
        "USE_CHEWY_NEXT_JSON_EXTRACTOR": "true",
        "CHEWY_JSON_FALLBACK_TO_OLD_SCRAPER": "true",
        "CHEWY_JSON_CONFIDENCE_THRESHOLD": str(threshold),
        "CHEWY_JSON_SAVE_GROUPED_OUTPUT": "true" if save_grouped_output else "false",
    }


def build_env(mode: str, threshold: int, save_grouped_output: bool) -> dict[str, str]:
    env = os.environ.copy()
    env.update(mode_flags(mode, threshold, save_grouped_output))
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def create_job(
    *,
    name: str,
    urls: list[str],
    mode: str,
    confidence_threshold: int = 75,
    max_attempts: int = 3,
    delay_seconds: float = 0,
    save_grouped_output: bool = True,
    input_file_path: str | None = None,
    notes: str = "",
) -> str:
    if mode not in JOB_MODES:
        raise ValueError(f"Unsupported mode: {mode}")
    fallback_enabled = mode == MODE_JSON_FALLBACK
    job_id = job_store.create_job(
        name=name,
        urls=urls,
        mode=mode,
        confidence_threshold=confidence_threshold,
        fallback_enabled=fallback_enabled,
        save_grouped_output=save_grouped_output,
        input_file_path=input_file_path,
        max_attempts=max_attempts,
        delay_seconds=delay_seconds,
        notes=notes,
    )
    job = job_store.get_job(job_id)
    assert job is not None
    output_dir = Path(job["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "logs").mkdir(exist_ok=True)
    (output_dir / "item_summaries").mkdir(exist_ok=True)
    (output_dir / "diagnostics").mkdir(exist_ok=True)
    (output_dir / "urls.txt").write_text("\n".join(urls) + "\n", encoding="utf-8")
    write_json_file(
        output_dir / "job_config.json",
        {
            "job_id": job_id,
            "name": name,
            "created_at": job["created_at"],
            "mode": mode,
            "feature_flags": mode_flags(mode, confidence_threshold, save_grouped_output),
            "confidence_threshold": confidence_threshold,
            "max_attempts": max_attempts,
            "delay_seconds": delay_seconds,
            "save_grouped_output": save_grouped_output,
            "input_file_path": input_file_path,
            "total_urls": len(urls),
            "notes": notes,
        },
    )
    return job_id


def expected_output_paths(source_id: str | None) -> dict[str, Path | None]:
    if not source_id:
        return {"grouped": None, "normalized": None, "validation": None}
    return {
        "grouped": OUTPUT_DIR / "grouped_products" / f"chewy_grouped_by_flavor_{source_id}.json",
        "normalized": OUTPUT_DIR / "normalized_products" / f"chewy_{source_id}.json",
        "validation": OUTPUT_DIR / "validation" / f"chewy_validation_{source_id}.json",
    }


def validation_confidence(path: Path | None) -> float | None:
    if not path or not path.exists():
        return None
    try:
        data = read_json_file(path)
        return float(data.get("confidence_score"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def existing_json_output_ok(source_id: str | None, threshold: int) -> tuple[bool, dict[str, Any]]:
    paths = expected_output_paths(source_id)
    grouped = paths["grouped"]
    validation = paths["validation"]
    score = validation_confidence(validation)
    ok = bool(grouped and grouped.exists() and validation and validation.exists() and score is not None and score >= threshold)
    return ok, {
        "grouped_output_path": str(grouped.resolve()) if grouped and grouped.exists() else None,
        "normalized_output_path": str(paths["normalized"].resolve()) if paths["normalized"] and paths["normalized"].exists() else None,
        "validation_output_path": str(validation.resolve()) if validation and validation.exists() else None,
        "confidence_score": score,
    }


def latest_matching_file(folder: Path, pattern: str, started_at: float) -> Path | None:
    if not folder.exists():
        return None
    matches = [
        path
        for path in folder.glob(pattern)
        if path.is_file() and path.stat().st_mtime >= started_at - 2
    ]
    if not matches:
        return None
    return max(matches, key=lambda path: path.stat().st_mtime)


def discover_item_outputs(source_id: str | None, started_at: float, mode: str, job_dir: Path) -> dict[str, Any]:
    paths = expected_output_paths(source_id)
    diagnostic_path = None
    if source_id:
        diagnostic_path = latest_matching_file(
            OUTPUT_DIR / "json_extractor_failures",
            f"chewy_failure_{source_id}*.json",
            started_at,
        )
    old_json = latest_matching_file(OUTPUT_DIR, "test_chewy_product.json", started_at)
    old_csv = latest_matching_file(OUTPUT_DIR, "test_chewy_product_shopify.csv", started_at)

    copied_diag = None
    if diagnostic_path:
        copied_diag = job_dir / "diagnostics" / diagnostic_path.name
        try:
            shutil.copy2(diagnostic_path, copied_diag)
        except OSError:
            copied_diag = diagnostic_path

    data: dict[str, Any] = {
        "grouped_output_path": str(paths["grouped"].resolve()) if paths["grouped"] and paths["grouped"].exists() else None,
        "normalized_output_path": str(paths["normalized"].resolve())
        if paths["normalized"] and paths["normalized"].exists()
        else None,
        "validation_output_path": str(paths["validation"].resolve())
        if paths["validation"] and paths["validation"].exists()
        else None,
        "diagnostic_output_path": str(copied_diag.resolve()) if copied_diag and copied_diag.exists() else None,
        "old_scraper_json_path": str(old_json.resolve()) if old_json else None,
        "old_scraper_csv_path": str(old_csv.resolve()) if old_csv else None,
        "confidence_score": validation_confidence(paths["validation"]),
    }
    return data


def read_diagnostic(path: str | None) -> dict[str, Any] | None:
    if not path:
        return None
    try:
        data = read_json_file(path)
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def detect_manual_challenge(log_text: str) -> bool:
    lower = strip_ansi(log_text).lower()
    phrases = [
        "captcha",
        "manual challenge",
        "verify you are human",
        "are you a human",
        "press and hold",
        "security check",
        "unusual traffic",
    ]
    return any(phrase in lower for phrase in phrases)


def classify_failure(
    *,
    log_text: str,
    exit_code: int,
    diagnostic: dict[str, Any] | None,
    output_paths: dict[str, Any],
    mode: str,
    threshold: int,
) -> tuple[str | None, str | None, bool]:
    """Return (error_type, error_message, is_transient)."""
    clean = strip_ansi(log_text)
    lower = clean.lower()
    diag_error = str(diagnostic.get("error")) if diagnostic and diagnostic.get("error") else ""
    diag_lower = json.dumps(diagnostic or {}, ensure_ascii=False).lower()

    if detect_manual_challenge(clean):
        return "captcha_or_manual_intervention", "Manual action required before continuing.", False
    if detect_redirected_plp(clean, diagnostic):
        return "redirected_plp", "URL redirected to a PLP/category page, not a PDP.", False
    if "unavailable_product" in diag_lower or "unavailable product" in lower:
        return "unavailable_product", diag_error or "Product appears unavailable.", False
    if "validation failed" in diag_lower:
        if output_paths.get("confidence_score") is not None and float(output_paths["confidence_score"]) < threshold:
            return "low_confidence", "Extraction confidence is below threshold.", False
        return "validation_failed", diag_error or "Validation failed.", False
    if output_paths.get("confidence_score") is not None and float(output_paths["confidence_score"]) < threshold:
        return "low_confidence", "Extraction confidence is below threshold.", False
    if "adspower" in lower and any(token in lower for token in ["failed", "refused", "connection", "get_ws_endpoint"]):
        return "adspower_error", "AdsPower/browser connection failed.", True
    if "timeout" in lower or "net::" in lower or "network" in lower:
        return "network_error", "Network timeout or page load failure.", True
    if "playwright" in lower or "browser" in lower or "target closed" in lower or "connection closed" in lower:
        return "browser_error", "Browser connection failed or closed.", True
    if "permissionerror" in lower or "permission denied" in lower or "access is denied" in lower:
        return "output_missing", "File or process permission error.", True
    if exit_code != 0:
        return "unknown_error", f"Subprocess exited with code {exit_code}.", False
    if diag_error:
        return "unknown_error", diag_error, False
    return "output_missing", "Expected output file was not found.", True


def should_mark_done(mode: str, log_text: str, output_paths: dict[str, Any], threshold: int) -> bool:
    clean = strip_ansi(log_text)
    if mode == MODE_OLD:
        return bool(output_paths.get("old_scraper_json_path") and "Done! Check output/" in clean)

    grouped = output_paths.get("grouped_output_path")
    validation = output_paths.get("validation_output_path")
    score = output_paths.get("confidence_score")
    if grouped and validation and score is not None and float(score) >= threshold:
        return True

    fallback_used = "Running OLD Scraper" in clean and "Done! Check output/" in clean
    old_output = bool(output_paths.get("old_scraper_json_path"))
    return mode == MODE_JSON_FALLBACK and fallback_used and old_output


def extract_metadata_from_log(log_text: str, diagnostic: dict[str, Any] | None) -> dict[str, Any]:
    clean = strip_ansi(log_text)
    metadata: dict[str, Any] = {
        "fallback_used": "Running OLD Scraper" in clean and "Running NEW JSON Extractor" in clean,
    }
    arch_match = re.search(r"Detected Architecture:\s*([A-Za-z0-9_-]+)", clean)
    if arch_match:
        metadata["architecture"] = arch_match.group(1).lower()
    title_match = re.search(r"Title:\s*(.+)", clean)
    if title_match:
        metadata["title"] = title_match.group(1).strip()
    if diagnostic:
        metadata["diagnostic_error"] = diagnostic.get("error")
        metadata["fallback_reason"] = diagnostic.get("fallback_reason")
    return metadata


def process_single_item(
    job_id: str,
    item_id: int,
    *,
    reprocess_existing: bool = False,
    force_retry: bool = False,
    on_line: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    job = job_store.get_job(job_id)
    item = job_store.get_item(item_id)
    if not job or not item:
        raise ValueError(f"Job/item not found: {job_id}/{item_id}")

    job_dir = Path(job["output_dir"])
    logs_dir = job_dir / "logs"
    summaries_dir = job_dir / "item_summaries"
    diagnostics_dir = job_dir / "diagnostics"
    logs_dir.mkdir(parents=True, exist_ok=True)
    summaries_dir.mkdir(parents=True, exist_ok=True)
    diagnostics_dir.mkdir(parents=True, exist_ok=True)

    url = item["input_url"]
    source_id = source_id_from_url(url)
    threshold = int(job["confidence_threshold"])
    mode = job["mode"]

    if item["status"] == "done" and not reprocess_existing:
        return item
    if item["status"] == "failed" and not force_retry and int(item["attempts"]) >= int(item["max_attempts"]):
        return item

    if not is_valid_chewy_url(url):
        summary_path = summaries_dir / f"item_{int(item['index_number']):05d}_summary.json"
        log_path = logs_dir / f"item_{int(item['index_number']):05d}_{source_id or 'invalid'}.log"
        log_path.write_text(
            f"[resumable-runner] job_id={job_id} item_id={item_id} index={item['index_number']}\n"
            f"[resumable-runner] invalid_url={url}\n",
            encoding="utf-8",
        )
        job_store.update_item_status(
            item_id,
            "failed",
            attempts=int(item["attempts"]) + 1,
            source_product_id=source_id,
            finished_at=job_store.utc_now(),
            run_log_path=str(log_path.resolve()),
            error_type="invalid_url",
            error_message="URL must start with https://www.chewy.com/",
            metadata_json=json.dumps({"input_url": url}, ensure_ascii=False),
        )
        write_json_file(
            summary_path,
            {
                "job_id": job_id,
                "item_id": item_id,
                "index_number": item["index_number"],
                "input_url": url,
                "status": "failed",
                "attempts": int(item["attempts"]) + 1,
                "error_type": "invalid_url",
                "error_message": "URL must start with https://www.chewy.com/",
            },
        )
        job_store.update_job_counts(job_id)
        return job_store.get_item(item_id) or {}

    if mode != MODE_OLD and not reprocess_existing:
        exists_ok, existing = existing_json_output_ok(source_id, threshold)
        if exists_ok:
            metadata = {"reused_existing_output": True}
            log_path = logs_dir / f"item_{int(item['index_number']):05d}_{source_id or 'unknown'}_existing.log"
            log_path.write_text(
                f"[resumable-runner] job_id={job_id} item_id={item_id} index={item['index_number']}\n"
                f"[resumable-runner] reused_existing_output=true\n"
                f"[resumable-runner] source_product_id={source_id}\n",
                encoding="utf-8",
            )
            job_store.update_item_status(
                item_id,
                "done",
                source_product_id=source_id,
                detected_product_id=source_id,
                finished_at=job_store.utc_now(),
                page_kind="pdp",
                confidence_score=existing.get("confidence_score"),
                grouped_output_path=existing.get("grouped_output_path"),
                normalized_output_path=existing.get("normalized_output_path"),
                validation_output_path=existing.get("validation_output_path"),
                run_log_path=str(log_path.resolve()),
                error_type=None,
                error_message=None,
                metadata_json=json.dumps(metadata, ensure_ascii=False),
            )
            write_json_file(
                summaries_dir / f"item_{int(item['index_number']):05d}_summary.json",
                {
                    "job_id": job_id,
                    "item_id": item_id,
                    "index_number": item["index_number"],
                    "input_url": url,
                    "status": "done",
                    "attempts": item["attempts"],
                    "source_product_id": source_id,
                    "page_kind": "pdp",
                    "confidence_score": existing.get("confidence_score"),
                    "output_paths": existing,
                    "metadata": metadata,
                },
            )
            job_store.update_job_counts(job_id)
            return job_store.get_item(item_id) or {}

    log_name = f"item_{int(item['index_number']):05d}_{source_id or 'unknown'}.log"
    log_path = logs_dir / log_name
    attempts = job_store.increment_attempt_and_start(item_id, run_log_path=str(log_path.resolve()), source_product_id=source_id)
    started_at = time.time()
    started_iso = job_store.utc_now()
    command = [sys.executable, "-u", "test_single_product.py", url]
    env = build_env(mode, threshold, bool(job["save_grouped_output"]))
    lines: list[str] = []

    with open(log_path, "w", encoding="utf-8", errors="replace") as log_file:
        log_file.write(f"[resumable-runner] job_id={job_id} item_id={item_id} index={item['index_number']}\n")
        log_file.write(f"[resumable-runner] command={' '.join(command)}\n")
        log_file.write(f"[resumable-runner] started_at={started_iso}\n")
        log_file.flush()
        process = subprocess.Popen(
            command,
            cwd=str(PROJECT_ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        assert process.stdout is not None
        for line in iter(process.stdout.readline, ""):
            lines.append(line)
            log_file.write(line)
            log_file.flush()
            if on_line:
                on_line(strip_ansi(line.rstrip("\n")))
        exit_code = process.wait()
        log_file.write(f"\n[resumable-runner] exit_code={exit_code}\n")

    duration = round(time.time() - started_at, 2)
    log_text = "".join(lines)
    output_paths = discover_item_outputs(source_id, started_at, mode, job_dir)
    diagnostic = read_diagnostic(output_paths.get("diagnostic_output_path"))
    status_info = summarize_run(
        log_text=log_text,
        exit_code=exit_code,
        mode=DISPLAY_MODE.get(mode, mode),
        threshold=threshold,
        output_files=[
            {"path": value}
            for key, value in output_paths.items()
            if key.endswith("_path") and value
        ],
    )
    metadata = extract_metadata_from_log(log_text, diagnostic)
    metadata.update(
        {
            "exit_code": exit_code,
            "attempt": attempts,
            "status_summary": status_info.get("status"),
            "old_scraper_json_path": output_paths.get("old_scraper_json_path"),
            "old_scraper_csv_path": output_paths.get("old_scraper_csv_path"),
        }
    )

    architecture = metadata.get("architecture")
    if diagnostic and diagnostic.get("detected_architecture"):
        architecture = diagnostic.get("detected_architecture")

    page_kind = diagnostic.get("page_kind") if diagnostic else None
    if detect_redirected_plp(log_text, diagnostic):
        page_kind = "redirected_plp"
    elif should_mark_done(mode, log_text, output_paths, threshold):
        page_kind = page_kind or "pdp"

    final_url = diagnostic.get("final_url") if diagnostic else None
    detected_product_id = diagnostic.get("detected_product_id") if diagnostic else source_id
    warnings = []
    if diagnostic and isinstance(diagnostic.get("warnings"), list):
        warnings = diagnostic["warnings"]
    if status_info.get("last_log_lines"):
        metadata["last_log_lines"] = status_info["last_log_lines"]

    if detect_manual_challenge(log_text):
        error_type, error_message, _ = classify_failure(
            log_text=log_text,
            exit_code=exit_code,
            diagnostic=diagnostic,
            output_paths=output_paths,
            mode=mode,
            threshold=threshold,
        )
        item_status = "paused"
        job_status = "paused"
    elif should_mark_done(mode, log_text, output_paths, threshold) and not detect_redirected_plp(log_text, diagnostic):
        error_type = None
        error_message = None
        item_status = "done"
        job_status = None
    else:
        error_type, error_message, is_transient = classify_failure(
            log_text=log_text,
            exit_code=exit_code,
            diagnostic=diagnostic,
            output_paths=output_paths,
            mode=mode,
            threshold=threshold,
        )
        if is_transient and attempts < int(item["max_attempts"]):
            item_status = "pending"
            warnings.append(f"Transient failure queued for retry attempt {attempts + 1}.")
        else:
            item_status = "failed"
        job_status = None

    job_store.update_item_status(
        item_id,
        item_status,
        final_url=final_url,
        source_product_id=source_id,
        detected_product_id=detected_product_id,
        finished_at=job_store.utc_now(),
        duration_seconds=duration,
        page_kind=page_kind,
        architecture=architecture,
        confidence_score=output_paths.get("confidence_score") or status_info.get("confidence_score"),
        grouped_output_path=output_paths.get("grouped_output_path"),
        normalized_output_path=output_paths.get("normalized_output_path"),
        validation_output_path=output_paths.get("validation_output_path"),
        diagnostic_output_path=output_paths.get("diagnostic_output_path"),
        run_log_path=str(log_path.resolve()),
        error_type=error_type,
        error_message=error_message,
        warnings_json=json.dumps(warnings, ensure_ascii=False),
        metadata_json=json.dumps(metadata, ensure_ascii=False),
    )
    if job_status:
        job_store.set_job_status(job_id, job_status, last_error=error_message)

    item_after = job_store.get_item(item_id) or {}
    item_summary = {
        "job_id": job_id,
        "item_id": item_id,
        "index_number": item_after.get("index_number"),
        "input_url": url,
        "status": item_after.get("status"),
        "attempts": item_after.get("attempts"),
        "duration_seconds": duration,
        "exit_code": exit_code,
        "source_product_id": source_id,
        "page_kind": page_kind,
        "architecture": architecture,
        "confidence_score": item_after.get("confidence_score"),
        "output_paths": output_paths,
        "error_type": error_type,
        "error_message": error_message,
        "warnings": warnings,
        "metadata": metadata,
    }
    write_json_file(summaries_dir / f"item_{int(item['index_number']):05d}_summary.json", item_summary)
    job_store.update_job_counts(job_id)
    return item_after


def write_job_reports(job_id: str) -> dict[str, Any]:
    summary = job_store.get_job_summary(job_id)
    job = job_store.get_job(job_id)
    if not job:
        raise ValueError(f"Job not found: {job_id}")
    job_dir = Path(job["output_dir"])
    items = job_store.get_job_items(job_id)
    write_json_file(job_dir / "job_summary.json", summary)

    report_path = job_dir / "job_items_report.csv"
    with open(report_path, "w", encoding="utf-8-sig", newline="") as handle:
        fieldnames = [
            "index",
            "input_url",
            "status",
            "attempts",
            "source_product_id",
            "page_kind",
            "architecture",
            "confidence_score",
            "grouped_output_path",
            "error_type",
            "error_message",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for item in items:
            writer.writerow(
                {
                    "index": item.get("index_number"),
                    "input_url": item.get("input_url"),
                    "status": item.get("status"),
                    "attempts": item.get("attempts"),
                    "source_product_id": item.get("source_product_id"),
                    "page_kind": item.get("page_kind"),
                    "architecture": item.get("architecture"),
                    "confidence_score": item.get("confidence_score"),
                    "grouped_output_path": item.get("grouped_output_path"),
                    "error_type": item.get("error_type"),
                    "error_message": item.get("error_message"),
                }
            )
    return summary


def process_job(
    job_id: str,
    *,
    retry_failed: bool = False,
    resume_paused: bool = False,
    reprocess_completed: bool = False,
    reprocess_existing: bool = False,
    force_retry: bool = False,
    stale_minutes: int = 30,
    max_items: int | None = None,
    on_line: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    job = job_store.get_job(job_id)
    if not job:
        raise ValueError(f"Job not found: {job_id}")

    job_store.mark_stale_running_items(job_id, stale_minutes=stale_minutes)
    job_store.set_job_status(job_id, "running")
    processed = 0

    while True:
        current_job = job_store.get_job(job_id)
        if not current_job or current_job["status"] in {"paused", "cancelled"}:
            break

        item = job_store.get_next_item(
            job_id,
            retry_failed=retry_failed,
            include_paused=resume_paused,
            reprocess_completed=reprocess_completed,
        )
        if not item:
            break

        if item["status"] == "failed" and not force_retry and int(item["attempts"]) >= int(item["max_attempts"]):
            break

        if on_line:
            on_line(f"[job {job_id}] Processing item {item['index_number']}: {item['input_url']}")
        result = process_single_item(
            job_id,
            int(item["id"]),
            reprocess_existing=reprocess_existing,
            force_retry=force_retry,
            on_line=on_line,
        )
        processed += 1

        current_job = job_store.get_job(job_id)
        if current_job and current_job["status"] == "paused":
            break
        if result.get("status") == "paused":
            job_store.set_job_status(job_id, "paused", last_error=result.get("error_message"))
            break
        if max_items is not None and processed >= max_items:
            job_store.set_job_status(job_id, "paused", last_error=f"Stopped after {processed} item(s).")
            break

        refreshed = job_store.get_job(job_id)
        delay = float(refreshed.get("delay_seconds") if refreshed else job.get("delay_seconds") or 0)
        if delay > 0:
            time.sleep(delay)

    counts = job_store.update_job_counts(job_id)
    current_job = job_store.get_job(job_id)
    if current_job and current_job["status"] == "running":
        if counts["pending_count"] == 0:
            job_store.set_job_status(job_id, "completed")
        else:
            job_store.set_job_status(job_id, "paused", last_error="Run stopped with unfinished items.")

    summary = write_job_reports(job_id)
    if on_line:
        on_line(f"[job {job_id}] Status: {summary['status']} completed={summary['completed_count']} failed={summary['failed_count']} pending={summary['pending_count']}")
    return summary


def resume_job(job_id: str, *, resume_paused: bool = True, **kwargs: Any) -> dict[str, Any]:
    return process_job(job_id, resume_paused=resume_paused, **kwargs)


def pause_job(job_id: str) -> None:
    job_store.set_job_status(job_id, "paused", last_error="Pause requested. Runner will stop after current item.")


def cancel_job(job_id: str) -> None:
    job_store.set_job_status(job_id, "cancelled", last_error="Cancelled by user.")


def retry_failed_items(job_id: str, *, force: bool = False, start: bool = True, **kwargs: Any) -> dict[str, Any] | int:
    reset_count = job_store.reset_failed_items(job_id, force=force)
    if not start:
        return reset_count
    return process_job(job_id, retry_failed=True, force_retry=force, **kwargs)


def skip_current_item(job_id: str) -> dict[str, Any] | None:
    item = job_store.skip_current_item(job_id)
    job_store.update_job_counts(job_id)
    write_job_reports(job_id)
    return item


def status(job_id: str) -> dict[str, Any]:
    job_store.update_job_counts(job_id)
    return job_store.get_job_summary(job_id)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Resumable Chewy scraper job runner")
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create", help="Create a resumable job")
    create.add_argument("--name", required=True)
    create.add_argument("--urls", required=True, help="Text file with one URL per line")
    create.add_argument("--mode", choices=JOB_MODES, default=MODE_JSON_FALLBACK)
    create.add_argument("--confidence-threshold", type=int, default=75)
    create.add_argument("--max-attempts", type=int, default=3)
    create.add_argument("--delay-seconds", type=float, default=0)
    create.add_argument("--no-save-grouped-output", action="store_true")
    create.add_argument("--notes", default="")

    for name in ["start", "resume"]:
        cmd = sub.add_parser(name, help=f"{name.title()} a job")
        cmd.add_argument("--job-id", required=True)
        cmd.add_argument("--retry-failed", action="store_true")
        cmd.add_argument("--reprocess-completed", action="store_true")
        cmd.add_argument("--reprocess-existing", action="store_true")
        cmd.add_argument("--force-retry", action="store_true")
        cmd.add_argument("--stale-minutes", type=int, default=30)
        cmd.add_argument("--max-items", type=int)

    retry = sub.add_parser("retry-failed", help="Reset and retry failed items")
    retry.add_argument("--job-id", required=True)
    retry.add_argument("--force", action="store_true")
    retry.add_argument("--no-start", action="store_true")
    retry.add_argument("--max-items", type=int)

    stat = sub.add_parser("status", help="Show job status")
    stat.add_argument("--job-id", required=True)

    pause = sub.add_parser("pause", help="Pause a job after current item")
    pause.add_argument("--job-id", required=True)

    cancel = sub.add_parser("cancel", help="Cancel a job")
    cancel.add_argument("--job-id", required=True)

    skip = sub.add_parser("skip-current", help="Skip current/next item")
    skip.add_argument("--job-id", required=True)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "create":
        urls = read_urls_file(args.urls)
        job_id = create_job(
            name=args.name,
            urls=urls,
            mode=args.mode,
            confidence_threshold=args.confidence_threshold,
            max_attempts=args.max_attempts,
            delay_seconds=args.delay_seconds,
            save_grouped_output=not args.no_save_grouped_output,
            input_file_path=str(Path(args.urls).resolve()),
            notes=args.notes,
        )
        print(json.dumps({"job_id": job_id, "total_urls": len(urls)}, indent=2))
        return 0

    if args.command == "start":
        summary = process_job(
            args.job_id,
            retry_failed=args.retry_failed,
            resume_paused=False,
            reprocess_completed=args.reprocess_completed,
            reprocess_existing=args.reprocess_existing,
            force_retry=args.force_retry,
            stale_minutes=args.stale_minutes,
            max_items=args.max_items,
            on_line=print,
        )
        print(json.dumps(summary, indent=2))
        return 0

    if args.command == "resume":
        summary = resume_job(
            args.job_id,
            retry_failed=args.retry_failed,
            reprocess_completed=args.reprocess_completed,
            reprocess_existing=args.reprocess_existing,
            force_retry=args.force_retry,
            stale_minutes=args.stale_minutes,
            max_items=args.max_items,
            on_line=print,
        )
        print(json.dumps(summary, indent=2))
        return 0

    if args.command == "retry-failed":
        result = retry_failed_items(
            args.job_id,
            force=args.force,
            start=not args.no_start,
            max_items=args.max_items,
            on_line=print,
        )
        print(json.dumps(result, indent=2))
        return 0

    if args.command == "status":
        print(json.dumps(status(args.job_id), indent=2))
        return 0

    if args.command == "pause":
        pause_job(args.job_id)
        print(json.dumps({"job_id": args.job_id, "status": "paused"}, indent=2))
        return 0

    if args.command == "cancel":
        cancel_job(args.job_id)
        print(json.dumps({"job_id": args.job_id, "status": "cancelled"}, indent=2))
        return 0

    if args.command == "skip-current":
        item = skip_current_item(args.job_id)
        print(json.dumps(item or {}, indent=2))
        return 0

    parser.error("Unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
