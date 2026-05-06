"""File discovery helpers for the local Streamlit scraper UI."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = PROJECT_ROOT / "output"
UI_RUNS_DIR = OUTPUT_DIR / "ui_runs"

OUTPUT_FOLDERS = {
    "Grouped products": OUTPUT_DIR / "grouped_products",
    "Normalized products": OUTPUT_DIR / "normalized_products",
    "Validation reports": OUTPUT_DIR / "validation",
    "JSON extractor failures": OUTPUT_DIR / "json_extractor_failures",
    "Batch failures": OUTPUT_DIR / "batch_failures",
    "UI runs": UI_RUNS_DIR,
}


def ensure_output_dirs() -> None:
    OUTPUT_DIR.mkdir(exist_ok=True)
    UI_RUNS_DIR.mkdir(parents=True, exist_ok=True)
    for folder in OUTPUT_FOLDERS.values():
        folder.mkdir(parents=True, exist_ok=True)


def relative_path(path: str | Path) -> str:
    path = Path(path)
    try:
        return str(path.resolve().relative_to(PROJECT_ROOT.resolve()))
    except ValueError:
        return str(path)


def file_info(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    stat = p.stat()
    return {
        "name": p.name,
        "path": str(p.resolve()),
        "relative_path": relative_path(p),
        "modified": stat.st_mtime,
        "modified_display": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)),
        "size_bytes": stat.st_size,
    }


def list_files(
    folder: str | Path,
    *,
    suffixes: tuple[str, ...] = (".json",),
    search: str = "",
    recursive: bool = False,
) -> list[dict[str, Any]]:
    root = Path(folder)
    if not root.exists():
        return []

    pattern_iter = root.rglob("*") if recursive else root.glob("*")
    search_l = search.lower().strip()
    files = []
    for path in pattern_iter:
        if not path.is_file():
            continue
        if suffixes and path.suffix.lower() not in suffixes:
            continue
        haystack = f"{path.name} {relative_path(path)}".lower()
        if search_l and search_l not in haystack:
            continue
        files.append(file_info(path))
    return sorted(files, key=lambda item: item["modified"], reverse=True)


def read_json_file(path: str | Path) -> Any:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def read_text_file(path: str | Path, limit_chars: int | None = None) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        data = handle.read()
    if limit_chars and len(data) > limit_chars:
        return data[-limit_chars:]
    return data


def write_json_file(path: str | Path, data: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)


def read_bytes(path: str | Path) -> bytes:
    with open(path, "rb") as handle:
        return handle.read()


def collect_output_files_since(
    started_at: float,
    *,
    source_id: str | None = None,
    extra_paths: list[str | Path] | None = None,
) -> list[dict[str, Any]]:
    candidates: list[Path] = []
    scan_dirs = [
        OUTPUT_DIR,
        OUTPUT_DIR / "normalized_products",
        OUTPUT_DIR / "grouped_products",
        OUTPUT_DIR / "validation",
        OUTPUT_DIR / "json_extractor_failures",
        OUTPUT_DIR / "batch_failures",
    ]
    suffixes = {".json", ".csv", ".log", ".txt"}

    for folder in scan_dirs:
        if not folder.exists():
            continue
        for path in folder.glob("*"):
            if path.is_file() and path.suffix.lower() in suffixes:
                candidates.append(path)

    extra_resolved = {str(Path(p).resolve()) for p in (extra_paths or [])}
    if extra_paths:
        candidates.extend(Path(p) for p in extra_paths)

    unique: dict[str, Path] = {}
    for path in candidates:
        if not path.exists() or not path.is_file():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        recent = stat.st_mtime >= started_at - 2
        is_extra = str(path.resolve()) in extra_resolved
        if recent or is_extra:
            unique[str(path.resolve())] = path

    return sorted((file_info(path) for path in unique.values()), key=lambda item: item["modified"], reverse=True)


def latest_batch_report(preferred: str | Path | None = None) -> Path | None:
    candidates: list[Path] = []
    if preferred:
        candidates.append(Path(preferred))
    candidates.extend(
        [
            OUTPUT_DIR / "chewy_phase3_batch_report.json",
            OUTPUT_DIR / "chewy_phase3D_fix_batch_report.json",
        ]
    )
    if OUTPUT_DIR.exists():
        candidates.extend(OUTPUT_DIR.glob("*batch_report*.json"))
    if UI_RUNS_DIR.exists():
        candidates.extend(UI_RUNS_DIR.glob("*/chewy_batch_report.json"))

    existing = [path for path in candidates if path.exists() and path.is_file()]
    if not existing:
        return None
    return max(existing, key=lambda path: path.stat().st_mtime)


def list_run_history() -> list[dict[str, Any]]:
    if not UI_RUNS_DIR.exists():
        return []

    rows: list[dict[str, Any]] = []
    for run_dir in UI_RUNS_DIR.iterdir():
        if not run_dir.is_dir():
            continue
        config_path = run_dir / "run_config.json"
        summary_path = run_dir / "run_summary.json"
        config = {}
        summary = {}
        try:
            if config_path.exists():
                config = read_json_file(config_path)
            if summary_path.exists():
                summary = read_json_file(summary_path)
        except (OSError, json.JSONDecodeError):
            pass

        rows.append(
            {
                "run_id": run_dir.name,
                "timestamp": config.get("timestamp") or summary.get("timestamp"),
                "kind": config.get("kind"),
                "mode": config.get("mode"),
                "target": config.get("url") or config.get("urls_file") or "",
                "batch_count": len(config.get("urls", [])) if isinstance(config.get("urls"), list) else None,
                "status": summary.get("status"),
                "duration_seconds": summary.get("duration_seconds"),
                "exit_code": summary.get("exit_code"),
                "run_log": str((run_dir / "run.log").resolve()),
                "run_summary": str(summary_path.resolve()),
                "modified": run_dir.stat().st_mtime,
            }
        )
    return sorted(rows, key=lambda row: row["modified"], reverse=True)
