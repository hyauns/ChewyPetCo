"""Subprocess runner used by the local Streamlit UI."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from ui_file_browser import (
    PROJECT_ROOT,
    UI_RUNS_DIR,
    collect_output_files_since,
    ensure_output_dirs,
    file_info,
    write_json_file,
)
from ui_log_parser import strip_ansi, summarize_run


MODE_OLD = "Old scraper only"
MODE_JSON_ONLY = "JSON extractor only"
MODE_JSON_FALLBACK = "JSON extractor + old scraper fallback"
MODES = [MODE_OLD, MODE_JSON_ONLY, MODE_JSON_FALLBACK]


def validate_chewy_url(url: str) -> tuple[bool, str | None]:
    if not url.strip():
        return False, "Please enter a Chewy PDP URL."
    if not url.strip().startswith("https://www.chewy.com/"):
        return False, "URL must start with https://www.chewy.com/"
    return True, None


def source_id_from_url(url: str) -> str | None:
    match = re.search(r"/dp/(\d+)", url)
    return match.group(1) if match else None


def make_run_id(kind: str) -> str:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{timestamp}_{kind}_{uuid.uuid4().hex[:8]}"


def create_run_dir(kind: str) -> Path:
    ensure_output_dirs()
    run_dir = UI_RUNS_DIR / make_run_id(kind)
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def feature_flags(mode: str, threshold: int, save_grouped_output: bool) -> dict[str, str]:
    if mode == MODE_OLD:
        return {
            "USE_CHEWY_NEXT_JSON_EXTRACTOR": "false",
            "CHEWY_JSON_FALLBACK_TO_OLD_SCRAPER": "false",
            "CHEWY_JSON_CONFIDENCE_THRESHOLD": str(threshold),
            "CHEWY_JSON_SAVE_GROUPED_OUTPUT": "true" if save_grouped_output else "false",
        }
    if mode == MODE_JSON_ONLY:
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
    env.update(feature_flags(mode, threshold, save_grouped_output))
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def write_run_config(run_dir: Path, config: dict[str, Any]) -> Path:
    path = run_dir / "run_config.json"
    write_json_file(path, config)
    return path


def run_subprocess(
    command: list[str],
    *,
    run_dir: Path,
    env: dict[str, str],
    on_line: Callable[[str], None] | None = None,
) -> tuple[int, float, str]:
    started = time.time()
    log_path = run_dir / "run.log"
    lines: list[str] = []

    with open(log_path, "w", encoding="utf-8", errors="replace") as log_file:
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

    return exit_code, time.time() - started, "".join(lines)


def finalize_summary(
    *,
    run_dir: Path,
    kind: str,
    mode: str,
    threshold: int,
    started_at: float,
    duration: float,
    exit_code: int,
    log_text: str,
    source_id: str | None = None,
    extra_output_paths: list[str | Path] | None = None,
) -> dict[str, Any]:
    output_files = collect_output_files_since(started_at, source_id=source_id, extra_paths=extra_output_paths)
    status_info = summarize_run(
        log_text=log_text,
        exit_code=exit_code,
        mode=mode,
        threshold=threshold,
        output_files=output_files,
    )
    summary = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "kind": kind,
        "status": status_info["status"],
        "duration_seconds": round(duration, 2),
        "exit_code": exit_code,
        "run_log": str((run_dir / "run.log").resolve()),
        "output_files": output_files,
        **status_info,
    }
    write_json_file(run_dir / "run_summary.json", summary)
    return summary


def run_single_product(
    *,
    url: str,
    mode: str,
    threshold: int,
    save_grouped_output: bool,
    on_line: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    run_dir = create_run_dir("single")
    started_at = time.time()
    flags = feature_flags(mode, threshold, save_grouped_output)
    command = [sys.executable, "-u", "test_single_product.py", url]
    config = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "kind": "single",
        "url": url,
        "mode": mode,
        "feature_flags": flags,
        "threshold": threshold,
        "save_grouped_output": save_grouped_output,
        "command": command,
    }
    write_run_config(run_dir, config)

    env = build_env(mode, threshold, save_grouped_output)
    exit_code, duration, log_text = run_subprocess(command, run_dir=run_dir, env=env, on_line=on_line)
    summary = finalize_summary(
        run_dir=run_dir,
        kind="single",
        mode=mode,
        threshold=threshold,
        started_at=started_at,
        duration=duration,
        exit_code=exit_code,
        log_text=log_text,
        source_id=source_id_from_url(url),
    )
    summary["run_dir"] = str(run_dir.resolve())
    return summary


def run_batch_test(
    *,
    urls: list[str],
    mode: str,
    threshold: int,
    save_grouped_output: bool,
    limit: int,
    delay_ms: int,
    on_line: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    run_dir = create_run_dir("batch")
    started_at = time.time()
    urls_file = run_dir / "urls.txt"
    urls_file.write_text("\n".join(urls) + "\n", encoding="utf-8")
    report_path = run_dir / "chewy_batch_report.json"
    flags = feature_flags(mode, threshold, save_grouped_output)
    command = [
        sys.executable,
        "-u",
        "test_chewy_json_extractor_batch.py",
        str(urls_file),
        "--limit",
        str(limit),
        "--delay-ms",
        str(delay_ms),
        "--output",
        str(report_path),
    ]
    config = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "kind": "batch",
        "urls": urls,
        "urls_file": str(urls_file.resolve()),
        "mode": mode,
        "feature_flags": flags,
        "threshold": threshold,
        "save_grouped_output": save_grouped_output,
        "limit": limit,
        "delay_ms": delay_ms,
        "command": command,
    }
    write_run_config(run_dir, config)

    env = build_env(mode, threshold, save_grouped_output)
    exit_code, duration, log_text = run_subprocess(command, run_dir=run_dir, env=env, on_line=on_line)
    summary = finalize_summary(
        run_dir=run_dir,
        kind="batch",
        mode=mode,
        threshold=threshold,
        started_at=started_at,
        duration=duration,
        exit_code=exit_code,
        log_text=log_text,
        extra_output_paths=[urls_file, report_path],
    )
    summary["run_dir"] = str(run_dir.resolve())
    summary["batch_report"] = file_info(report_path) if report_path.exists() else None
    write_json_file(run_dir / "run_summary.json", summary)
    return summary
