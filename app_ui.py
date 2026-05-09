"""Streamlit control panel for the existing Chewy scraper scripts."""

from __future__ import annotations

import json
import mimetypes
import os
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any

import streamlit as st

from ui_file_browser import (
    OUTPUT_FOLDERS,
    PROJECT_ROOT,
    UI_RUNS_DIR,
    ensure_output_dirs,
    file_info,
    latest_batch_report,
    list_files,
    list_run_history,
    read_bytes,
    read_json_file,
    read_text_file,
    relative_path,
)
from ui_log_parser import strip_ansi, summarize_error
from ui_runner import (
    MODE_JSON_FALLBACK,
    MODE_JSON_ONLY,
    MODE_OLD,
    MODES,
    feature_flags,
    run_batch_test,
    run_single_product,
    validate_chewy_url,
)

import importlib

import job_store
import resumable_scraper_runner as job_runner

importlib.reload(job_store)
importlib.reload(job_runner)


st.set_page_config(page_title="Chewy Scraper Control Panel", layout="wide")
ensure_output_dirs()


STATUS_COLORS = {
    "Success": "#15803d",
    "Running": "#0369a1",
    "Failed": "#b91c1c",
    "Fallback Used": "#92400e",
    "Low Confidence": "#a16207",
    "Redirected PLP": "#b91c1c",
    "Warning": "#a16207",
}


def inject_styles() -> None:
    st.markdown(
        """
        <style>
        .status-pill {
            display: inline-block;
            padding: 0.2rem 0.55rem;
            border-radius: 999px;
            color: white;
            font-size: 0.82rem;
            font-weight: 700;
            margin-right: 0.35rem;
            margin-bottom: 0.25rem;
        }
        .file-path {
            font-size: 0.86rem;
            color: #374151;
            word-break: break-all;
        }
        .warning-box {
            border-left: 4px solid #a16207;
            background: #fffbeb;
            padding: 0.8rem 1rem;
            margin: 0.6rem 0 1rem 0;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def status_pill(label: str) -> str:
    color = STATUS_COLORS.get(label, "#4b5563")
    return f'<span class="status-pill" style="background:{color}">{label}</span>'


def display_mode(mode: str) -> str:
    if mode == MODE_OLD:
        return "Old Scraper"
    if mode == MODE_JSON_ONLY:
        return "JSON Extractor"
    if mode == MODE_JSON_FALLBACK:
        return "JSON Extractor + Fallback"
    return mode


def render_header(mode: str) -> None:
    st.title("Chewy Scraper Control Panel")
    st.caption("Local UI wrapper for Chewy PDP JSON extractor and fallback scraper.")
    st.markdown(status_pill(display_mode(mode)), unsafe_allow_html=True)
    st.markdown(
        '<div class="warning-box"><strong>Warning:</strong> This UI does not push to Shopify. '
        "It only runs scraper and displays local output.</div>",
        unsafe_allow_html=True,
    )


def open_in_default_app(path: str | Path) -> tuple[bool, str | None]:
    try:
        p = str(Path(path).resolve())
        if os.name == "nt":
            os.startfile(p)  # type: ignore[attr-defined]
        elif os.name == "posix":
            opener = "open" if os.uname().sysname == "Darwin" else "xdg-open"
            subprocess.Popen([opener, p], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True, None
    except Exception as exc:  # pragma: no cover - depends on desktop shell
        return False, str(exc)


def download_button(path: str | Path, *, label: str = "Download", key_suffix: str = "") -> None:
    p = Path(path)
    mime = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
    key = f"download-{key_suffix}-{p}" if key_suffix else f"download-{label}-{p}"
    st.download_button(label, data=read_bytes(p), file_name=p.name, mime=mime, key=key)


def render_file_actions(file_info: dict[str, Any], prefix: str) -> None:
    path = file_info["path"]
    cols = st.columns([5, 1, 1])
    with cols[0]:
        st.markdown(f'<div class="file-path">{file_info["relative_path"]}</div>', unsafe_allow_html=True)
    with cols[1]:
        if st.button("Open", key=f"{prefix}-open-{path}"):
            ok, error = open_in_default_app(path)
            if not ok:
                st.error(f"Could not open file: {error}")
    with cols[2]:
        download_button(path, label="Download", key_suffix=prefix)


def render_output_files(output_files: list[dict[str, Any]], prefix: str) -> None:
    if not output_files:
        st.warning("Scraper completed, but expected output file was not found. Check run.log.")
        return
    st.subheader("Generated Output Files")
    for idx, info in enumerate(output_files):
        with st.expander(f"{info['name']} - {info['modified_display']}", expanded=idx == 0):
            render_file_actions(info, f"{prefix}-{idx}")
            if Path(info["path"]).suffix.lower() == ".json":
                try:
                    st.json(read_json_file(info["path"]), expanded=False)
                except Exception as exc:
                    st.error(f"Could not preview JSON: {exc}")


def make_live_log_callback(container: st.delta_generator.DeltaGenerator):
    lines: list[str] = []
    last_update = 0.0

    def on_line(line: str) -> None:
        nonlocal last_update
        lines.append(strip_ansi(line))
        now = time.time()
        if now - last_update >= 0.25 or len(lines) % 20 == 0:
            container.code("\n".join(lines[-300:]), language="text")
            last_update = now

    return on_line, lines


def is_db_malformed_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    return "database disk image is malformed" in message or "malformed" in message


def render_database_error(context: str, exc: BaseException) -> None:
    st.error(f"SQLite DB error while {context}: {exc}")
    if is_db_malformed_error(exc):
        st.warning(
            "scraper_jobs.db appears corrupted. Stop every running scraper/UI process, "
            "back up the DB, then repair or restore it before continuing."
        )
        if os.name == "nt":
            repair_commands = [
                "copy scraper_jobs.db scraper_jobs.db.bak",
                "sqlite3 scraper_jobs.db \"PRAGMA integrity_check;\"",
                "sqlite3 scraper_jobs.db \".recover\" > recovered.sql",
                "ren scraper_jobs.db scraper_jobs.db.corrupt",
                "sqlite3 scraper_jobs.db < recovered.sql",
                "python -c \"import job_store; job_store.init_db(); print('db_migration_ok')\"",
            ]
        else:
            repair_commands = [
                "cp scraper_jobs.db scraper_jobs.db.bak",
                "sqlite3 scraper_jobs.db \"PRAGMA integrity_check;\"",
                "sqlite3 scraper_jobs.db \".recover\" > recovered.sql",
                "mv scraper_jobs.db scraper_jobs.db.corrupt",
                "sqlite3 scraper_jobs.db < recovered.sql",
                "python3 -c \"import job_store; job_store.init_db(); print('db_migration_ok')\"",
            ]
        st.code(
            "\n".join(repair_commands),
            language="powershell" if os.name == "nt" else "bash",
        )


def safe_db_call(context: str, func, *args: Any, default: Any = None, **kwargs: Any) -> Any:
    try:
        return func(*args, **kwargs)
    except sqlite3.DatabaseError as exc:
        render_database_error(context, exc)
        return default


def render_tab_safely(label: str, func, *args: Any, **kwargs: Any) -> None:
    try:
        func(*args, **kwargs)
    except sqlite3.DatabaseError as exc:
        render_database_error(f"rendering {label}", exc)


def render_realtime_refresh_controls(jobs: list[dict[str, Any]]) -> tuple[bool, float]:
    running_jobs = [job for job in jobs if job.get("status") == "running"]
    cols = st.columns([1, 1, 3])
    enabled = cols[0].checkbox("Auto refresh", value=True, key="job-auto-refresh-enabled")
    interval = cols[1].number_input(
        "Refresh seconds",
        min_value=1.0,
        max_value=30.0,
        value=3.0,
        step=1.0,
        key="job-auto-refresh-seconds",
    )
    if running_jobs:
        cols[2].info(f"{len(running_jobs)} job(s) currently running. UI will refresh automatically.")
    elif enabled:
        cols[2].caption("Auto refresh is on, so CLI-started jobs will appear without pressing F5.")
    return enabled, float(interval)


def maybe_autorefresh(enabled: bool, interval_seconds: float) -> None:
    if not enabled:
        return
    time.sleep(max(1.0, float(interval_seconds)))
    st.rerun()


def render_run_summary(summary: dict[str, Any], *, prefix: str) -> None:
    status = summary.get("status", "Unknown")
    st.markdown(status_pill(status), unsafe_allow_html=True)

    flags = []
    if summary.get("fallback_used"):
        flags.append("Fallback Used")
    if summary.get("redirected_plp"):
        flags.append("Redirected PLP")
    if summary.get("low_confidence"):
        flags.append("Low Confidence")
    if flags:
        st.markdown(" ".join(status_pill(flag) for flag in flags), unsafe_allow_html=True)

    cols = st.columns(4)
    cols[0].metric("Exit Code", summary.get("exit_code"))
    cols[1].metric("Duration", f"{summary.get('duration_seconds', 0)}s")
    cols[2].metric("Confidence", summary.get("confidence_score") or "n/a")
    cols[3].metric("Outputs", len(summary.get("output_files", [])))

    error_summary = summary.get("error_summary") or {}
    if error_summary.get("reason"):
        st.error(f"Reason: {error_summary['reason']}")
        if error_summary.get("action"):
            st.info(f"Action: {error_summary['action']}")
        if error_summary.get("details"):
            st.caption(str(error_summary["details"]))

    st.caption("Run log")
    st.code(summary.get("run_log", ""), language="text")
    with st.expander("Last 50 log lines"):
        st.code("\n".join(summary.get("last_log_lines", [])), language="text")

    render_output_files(summary.get("output_files", []), prefix)


def parse_urls(text: str, uploaded_file: Any) -> list[str]:
    raw = text or ""
    if uploaded_file is not None:
        raw += "\n" + uploaded_file.getvalue().decode("utf-8", errors="replace")
    urls = []
    seen = set()
    for line in raw.splitlines():
        value = line.strip()
        if not value or value.startswith("#"):
            continue
        if value not in seen:
            urls.append(value)
            seen.add(value)
    return urls


def validate_urls(urls: list[str]) -> tuple[bool, str | None]:
    if not urls:
        return False, "Please enter at least one Chewy PDP URL."
    for url in urls:
        valid, message = validate_chewy_url(url)
        if not valid:
            return False, f"{message} Invalid entry: {url}"
    return True, None


def job_mode_from_ui_mode(mode: str) -> str:
    if mode == MODE_OLD:
        return job_runner.MODE_OLD
    if mode == MODE_JSON_ONLY:
        return job_runner.MODE_JSON
    return job_runner.MODE_JSON_FALLBACK


def ui_mode_label_from_job_mode(mode: str) -> str:
    return job_runner.DISPLAY_MODE.get(mode, mode)


def render_batch_report(report_path: str | Path | None, *, prefix: str = "batch-report") -> None:
    if not report_path:
        st.warning("No batch report found.")
        return
    path = Path(report_path)
    if not path.exists():
        st.warning("Batch report path does not exist yet.")
        return

    report = read_json_file(path)
    summary = report.get("summary", {})
    st.subheader("Batch Summary")
    metric_keys = [
        "total_urls",
        "success_count",
        "fail_count",
        "pdp_count",
        "redirected_pdp_count",
        "redirected_plp_count",
        "apollo_count",
        "redux_count",
        "average_confidence_score",
        "products_generated",
        "products_with_flavor_specific_images",
        "products_missing_feeding_instructions",
    ]
    for chunk_start in range(0, len(metric_keys), 4):
        cols = st.columns(4)
        for col, key in zip(cols, metric_keys[chunk_start : chunk_start + 4]):
            value = summary.get(key, "n/a")
            if isinstance(value, float):
                value = round(value, 1)
            col.metric(key, value)

    rows = []
    for item in report.get("results", []):
        rows.append(
            {
                "URL": item.get("input_url"),
                "page_kind": item.get("page_kind"),
                "architecture": item.get("detected_architecture"),
                "success": item.get("extraction_success"),
                "confidence": item.get("validation_confidence_score"),
                "grouped_products_count": item.get("grouped_products_count"),
                "warnings": "; ".join(str(w) for w in item.get("warnings", [])),
                "error": item.get("error"),
            }
        )
    st.subheader("Batch Results")
    st.dataframe(rows, width="stretch", hide_index=True)
    with st.expander("Raw batch report"):
        st.json(report, expanded=False)
    download_button(path, label="Download batch report", key_suffix=prefix)


def render_markdown_or_text(value: Any) -> None:
    if value in (None, "", [], {}):
        st.caption("No data found.")
    elif isinstance(value, str):
        st.markdown(value)
    else:
        st.json(value, expanded=False)


def content_text(product: dict[str, Any], key: str) -> Any:
    section = product.get("content_sections", {}).get(key)
    if isinstance(section, dict):
        return section.get("plain_text") or section.get("raw_text") or section
    return product.get(key) or section


def render_table_section(product: dict[str, Any], key: str, fallback_key: str | None = None) -> None:
    section = product.get("content_sections", {}).get(key)
    if isinstance(section, dict):
        rows = section.get("rows") or section.get("groups")
        if isinstance(rows, list) and rows:
            st.dataframe(rows, width="stretch", hide_index=True)
            return
    render_markdown_or_text(product.get(fallback_key or key) or section)


def render_grouped_product(data: dict[str, Any]) -> None:
    cols = st.columns(4)
    cols[0].metric("source_product_id", data.get("source_product_id", "n/a"))
    cols[1].metric("architecture", data.get("architecture", "n/a"))
    cols[2].metric("grouping_strategy", data.get("grouping_strategy", "n/a"))
    cols[3].metric("products count", len(data.get("products", [])))
    st.caption(data.get("source_url", ""))

    for idx, product in enumerate(data.get("products", []), start=1):
        title = product.get("title") or f"Product {idx}"
        flavor = product.get("flavor") or "No flavor"
        with st.expander(f"{idx}. {flavor} - {title}", expanded=idx == 1):
            cols = st.columns(5)
            cols[0].write(f"**Flavor:** {flavor}")
            cols[1].write(f"**Brand:** {product.get('brand', '')}")
            cols[2].write(f"**Images:** {len(product.get('images', []))}")
            cols[3].write(f"**Variants:** {len(product.get('variants', []))}")
            cols[4].write(f"**Facts:** {len(product.get('product_facts', {}) or {})}")

            with st.expander("Product Facts"):
                st.json(product.get("product_facts", {}), expanded=True)

            variants = []
            for variant in product.get("variants", []):
                variants.append(
                    {
                        "option1_name": variant.get("option1_name"),
                        "option1_value": variant.get("option1_value"),
                        "sku": variant.get("sku"),
                        "price": variant.get("price"),
                        "compare_at_price": variant.get("compare_at_price"),
                        "autoship_price": variant.get("autoship_price"),
                        "in_stock": variant.get("in_stock"),
                        "variant_url": variant.get("variant_url"),
                    }
                )
            st.subheader("Variants")
            st.dataframe(variants, width="stretch", hide_index=True)

            st.subheader("Description")
            render_markdown_or_text(content_text(product, "description"))

            st.subheader("Ingredients")
            render_markdown_or_text(content_text(product, "ingredients"))

            st.subheader("Guaranteed Analysis")
            render_table_section(product, "guaranteed_analysis")

            st.subheader("Nutrition")
            render_markdown_or_text(product.get("content_sections", {}).get("nutrition") or product.get("nutrition"))

            st.subheader("Feeding Instructions")
            render_table_section(product, "feeding_instructions")

            st.subheader("Specifications")
            render_markdown_or_text(
                product.get("specifications") or product.get("content_sections", {}).get("specifications")
            )

            st.subheader("Storefront Display Config")
            render_markdown_or_text(product.get("storefront_display") or product.get("storefront_display_config"))

            st.subheader("Metafields Plan")
            render_markdown_or_text(product.get("metafields_plan"))


def render_validation_report(data: dict[str, Any], threshold: int) -> None:
    is_valid = bool(data.get("is_valid"))
    score = data.get("confidence_score", 0)
    warnings = data.get("warnings", [])
    if is_valid and score >= threshold and not warnings:
        st.success("Valid and confidence is at or above threshold.")
    elif is_valid and score >= threshold:
        st.warning("Valid, but warnings exist.")
    else:
        st.error("Invalid or confidence is below threshold.")

    cols = st.columns(3)
    cols[0].metric("is_valid", str(is_valid))
    cols[1].metric("confidence_score", score)
    cols[2].metric("threshold", threshold)
    st.write("**missing_required_fields**")
    st.json(data.get("missing_required_fields", []))
    st.write("**missing_preferred_fields**")
    st.json(data.get("missing_preferred_fields", []))
    st.write("**warnings**")
    st.json(warnings)


def select_json_file(label: str, files: list[dict[str, Any]], key: str) -> dict[str, Any] | None:
    if not files:
        st.warning("No JSON files found.")
        return None
    selected = st.selectbox(label, files, format_func=lambda item: item["relative_path"], key=key)
    return selected


def tab_single(default_mode: str, default_threshold: int, default_save_grouped: bool) -> None:
    st.subheader("Single Product Scraper")
    url = st.text_input(
        "Chewy PDP URL",
        value="https://www.chewy.com/hills-science-diet-adult-sensitive/dp/3861718",
        key="single-url",
    )
    cols = st.columns([2, 1, 1])
    mode = cols[0].selectbox("Mode", MODES, index=MODES.index(default_mode), key="single-mode")
    threshold = cols[1].number_input("Confidence threshold", min_value=0, max_value=100, value=default_threshold)
    save_grouped = cols[2].checkbox("Save grouped output", value=default_save_grouped)

    if st.button("Run single scraper", type="primary"):
        valid, message = validate_chewy_url(url)
        if not valid:
            st.error(message)
            return

        st.markdown(status_pill("Running"), unsafe_allow_html=True)
        log_box = st.empty()
        callback, live_lines = make_live_log_callback(log_box)
        with st.spinner("Running scraper subprocess..."):
            summary = run_single_product(
                url=url.strip(),
                mode=mode,
                threshold=int(threshold),
                save_grouped_output=save_grouped,
                on_line=callback,
            )
        log_box.code("\n".join(live_lines[-300:]), language="text")
        st.session_state["last_single_summary"] = summary
        render_run_summary(summary, prefix="single-run")

    if st.session_state.get("last_single_summary"):
        with st.expander("Last single run summary", expanded=False):
            render_run_summary(st.session_state["last_single_summary"], prefix="single-last")


def tab_batch(default_mode: str, default_threshold: int, default_save_grouped: bool) -> None:
    st.subheader("Batch Test")
    st.caption("Batch uses the existing test_chewy_json_extractor_batch.py harness.")
    url_text = st.text_area("Chewy PDP URLs, one per line", height=160)
    uploaded = st.file_uploader("Optional urls.txt upload", type=["txt"], accept_multiple_files=False)
    cols = st.columns([2, 1, 1, 1])
    mode = cols[0].selectbox("Feature flag mode", MODES, index=MODES.index(default_mode), key="batch-mode")
    limit = cols[1].number_input("Limit", min_value=1, max_value=500, value=10)
    delay_ms = cols[2].number_input("Delay between URLs (ms)", min_value=0, max_value=60000, value=1500, step=250)
    threshold = cols[3].number_input("Threshold", min_value=0, max_value=100, value=default_threshold, key="batch-threshold")
    save_grouped = st.checkbox("Save grouped output flag", value=default_save_grouped, key="batch-save-grouped")

    if st.button("Run batch test", type="primary"):
        urls = parse_urls(url_text, uploaded)
        valid, message = validate_urls(urls)
        if not valid:
            st.error(message)
            return

        st.markdown(status_pill("Running"), unsafe_allow_html=True)
        log_box = st.empty()
        callback, live_lines = make_live_log_callback(log_box)
        with st.spinner("Running batch subprocess..."):
            summary = run_batch_test(
                urls=urls,
                mode=mode,
                threshold=int(threshold),
                save_grouped_output=save_grouped,
                limit=int(limit),
                delay_ms=int(delay_ms),
                on_line=callback,
            )
        log_box.code("\n".join(live_lines[-300:]), language="text")
        st.session_state["last_batch_summary"] = summary
        render_run_summary(summary, prefix="batch-run")
        report_info = summary.get("batch_report") or {}
        render_batch_report(report_info.get("path"), prefix="batch-run-report")

    if st.session_state.get("last_batch_summary"):
        with st.expander("Last batch report", expanded=False):
            report_info = st.session_state["last_batch_summary"].get("batch_report") or {}
            render_batch_report(report_info.get("path"), prefix="batch-last-report")


def render_job_progress(job_id: str) -> dict[str, Any]:
    try:
        job_store.update_job_counts(job_id)
        summary = job_runner.status(job_id)
    except sqlite3.DatabaseError as exc:
        render_database_error("loading job progress", exc)
        return {}
    cols = st.columns(6)
    cols[0].metric("Status", summary.get("status"))
    cols[1].metric("Completed", f"{summary.get('completed_count')} / {summary.get('total_urls')}")
    cols[2].metric("Failed", summary.get("failed_count"))
    cols[3].metric("Skipped", summary.get("skipped_count"))
    cols[4].metric("Pending", summary.get("pending_count"))
    cols[5].metric("Next index", summary.get("next_resume_index") or "n/a")
    progress_total = max(int(summary.get("total_urls") or 0), 1)
    progress_done = int(summary.get("completed_count") or 0) + int(summary.get("skipped_count") or 0)
    st.progress(min(progress_done / progress_total, 1.0))
    return summary


def render_job_file(path_value: str | None, label: str, key_prefix: str) -> None:
    if not path_value:
        return
    path = Path(path_value)
    if not path.exists():
        st.caption(f"{label}: {path_value} (not found)")
        return
    st.write(f"**{label}**")
    render_file_actions(file_info(path), key_prefix)


def render_selected_job_item(job_id: str, items: list[dict[str, Any]]) -> None:
    if not items:
        return
    selected = st.selectbox(
        "Inspect item",
        items,
        format_func=lambda item: f"#{item['index_number']} {item['status']} - {item['input_url'][:90]}",
        key=f"inspect-item-{job_id}",
    )
    if not selected:
        return

    cols = st.columns(4)
    cols[0].metric("Index", selected.get("index_number"))
    cols[1].metric("Status", selected.get("status"))
    cols[2].metric("Attempts", selected.get("attempts"))
    cols[3].metric("Confidence", selected.get("confidence_score") or "n/a")

    if selected.get("error_type") or selected.get("error_message"):
        st.error(f"{selected.get('error_type') or 'error'}: {selected.get('error_message') or ''}")

    render_job_file(selected.get("run_log_path"), "Item log", f"job-{job_id}-log-{selected['id']}")
    render_job_file(selected.get("grouped_output_path"), "Grouped output", f"job-{job_id}-grouped-{selected['id']}")
    render_job_file(selected.get("normalized_output_path"), "Normalized output", f"job-{job_id}-normalized-{selected['id']}")
    render_job_file(selected.get("validation_output_path"), "Validation output", f"job-{job_id}-validation-{selected['id']}")
    render_job_file(selected.get("diagnostic_output_path"), "Diagnostic output", f"job-{job_id}-diagnostic-{selected['id']}")

    if selected.get("run_log_path") and Path(selected["run_log_path"]).exists():
        with st.expander("Item log tail"):
            st.code(read_text_file(selected["run_log_path"], limit_chars=20000), language="text")
    if selected.get("metadata_json"):
        with st.expander("Item metadata"):
            try:
                st.json(json.loads(selected["metadata_json"]), expanded=False)
            except json.JSONDecodeError:
                st.code(selected["metadata_json"], language="json")
    if selected.get("profile_attempts_json"):
        with st.expander("Profile attempt history"):
            try:
                st.json(json.loads(selected["profile_attempts_json"]), expanded=False)
            except json.JSONDecodeError:
                st.code(selected["profile_attempts_json"], language="json")


def _metadata_flag(item: dict[str, Any], key: str) -> Any:
    try:
        metadata = json.loads(item.get("metadata_json") or "{}")
    except json.JSONDecodeError:
        return None
    return metadata.get(key)


def render_live_job_report(job_id: str, items: list[dict[str, Any]]) -> None:
    st.subheader("Live Run Report")
    if not items:
        st.info("No items found for this job.")
        return

    running_items = [item for item in items if item.get("status") == "running"]
    recent_items = sorted(
        items,
        key=lambda item: (item.get("updated_at") or "", int(item.get("index_number") or 0)),
        reverse=True,
    )[:50]
    rows = [
        {
            "updated": item.get("updated_at"),
            "index": item.get("index_number"),
            "status": item.get("status"),
            "attempts": item.get("attempts"),
            "worker": item.get("worker_id"),
            "slot": item.get("profile_slot_id"),
            "profile": item.get("profile_id_used"),
            "confidence": item.get("confidence_score"),
            "fallback": _metadata_flag(item, "fallback_used"),
            "duration_s": item.get("duration_seconds"),
            "error": item.get("error_type"),
            "message": item.get("error_message"),
            "url": item.get("input_url"),
        }
        for item in recent_items
    ]
    st.dataframe(rows, width="stretch", hide_index=True)

    if running_items:
        with st.expander("Running item log tails", expanded=True):
            for item in running_items[:5]:
                st.caption(
                    f"#{item.get('index_number')} {item.get('worker_id') or ''} "
                    f"{item.get('profile_slot_id') or ''} {item.get('input_url')}"
                )
                log_path = item.get("run_log_path")
                if log_path and Path(log_path).exists():
                    st.code(read_text_file(log_path, limit_chars=12000), language="text")
                else:
                    st.caption("No run log written yet.")


def tab_category_discovery() -> None:
    st.subheader("Category Discovery")
    st.caption("Discover product URLs from a Chewy category page, filter by price, and enqueue to PDP extraction.")
    
    with st.expander("Create Discovery Job", expanded=True):
        job_name = st.text_input("Job Name", value="Cat Discovery Job", key="cat-disc-name")
        cat_url = st.text_input("Category URL", value="https://www.chewy.com/b/toys-315", key="cat-disc-url")
        cols = st.columns(3)
        price_min = cols[0].number_input("Price Min ($)", value=40.0, step=1.0, key="cat-disc-min")
        price_max = cols[1].number_input("Price Max ($)", value=0.0, step=1.0, key="cat-disc-max")
        mode = cols[2].selectbox("Price Filter Mode", ["hybrid", "card_price_prefilter", "pdp_variant_filter"], index=0, key="cat-disc-mode")
        
        page_cols = st.columns(2)
        start_page = page_cols[0].number_input("Start Page", min_value=1, value=1, step=1, key="cat-disc-start-page")
        max_pages = page_cols[1].number_input("Max Pages (0 = No limit)", min_value=0, value=0, step=1, key="cat-disc-max-pages")
        
        if st.button("Create Discovery Job", type="primary", key="cat-disc-create"):
            import category_job_runner
            max_price_val = None if price_max == 0 else price_max
            max_pages_val = None if max_pages == 0 else max_pages
            
            jid = category_job_runner.create_category_discovery_job(
                name=job_name, url=cat_url, price_min=price_min, price_max=max_price_val, mode=mode, start_page=start_page, max_pages=max_pages_val
            )
            st.success(f"Created category discovery job: {jid}")
            st.session_state["selected_cat_job"] = jid

    jobs = job_store.list_category_jobs()
    if not jobs:
        st.info("No category jobs found.")
        return
        
    st.subheader("Discovery Jobs")
    job_rows = []
    for j in jobs:
        job_rows.append({
            "Job ID": j["category_job_id"],
            "Name": j["name"],
            "Status": j["status"],
            "Pages": j["total_pages_discovered"],
            "Found URLs": j["total_urls_found"],
            "Filtered In": j["total_urls_after_price_filter"],
            "Updated": j["updated_at"]
        })
    st.dataframe(job_rows, width="stretch", hide_index=True)
    
    selected_job_idx = 0
    if st.session_state.get("selected_cat_job"):
        for i, j in enumerate(jobs):
            if j["category_job_id"] == st.session_state["selected_cat_job"]:
                selected_job_idx = i
                break
                
    selected = st.selectbox("Select Job", jobs, format_func=lambda j: j["category_job_id"] + " - " + j["status"], index=selected_job_idx, key="cat-disc-select")
    if not selected:
        return
        
    jid = selected["category_job_id"]
    st.session_state["selected_cat_job"] = jid
    
    st.subheader("Controls")
    cols2 = st.columns(3)
    if cols2[0].button("Start / Resume (CLI msg)", key=f"cat-start-{jid}"):
        st.warning(f"Please run this in CLI:\\n`python category_job_runner.py start --category-job-id {jid}`")
        
    if cols2[1].button("Generate Validation Report", key=f"cat-val-{jid}"):
        import category_discovery_validation
        category_discovery_validation.validate_category_discovery(jid)
        st.success("Report generated.")
        st.rerun()

    # Load validation report if exists
    import os
    import json
    job = next(j for j in jobs if j["category_job_id"] == jid)
    report_path = os.path.join(job["output_dir"], "category_validation_report.json")
    report = None
    if os.path.exists(report_path):
        with open(report_path, "r", encoding="utf-8") as f:
            report = json.load(f)

    if report:
        safe_to_create = report.get("validation", {}).get("safe_to_create_pdp_job", False)
        if cols2[2].button("Create PDP Job from Results", key=f"cat-pdp-{jid}", disabled=not safe_to_create):
            import category_job_runner
            pdp_jid = category_job_runner.create_pdp_job_from_discovery(jid)
            if pdp_jid:
                st.success(f"Created PDP Job: {pdp_jid}")
            else:
                st.error("Could not create PDP Job. Are there filtered URLs?")
                
        st.subheader("Discovery Validation Report")
        
        # Explain price filter
        st.info("Chewy category price filters may not appear as stable URL parameters. This tool does not rely on Chewy filter URLs. It performs local filtering based on product card prices and later confirms real variant prices during PDP extraction.")
        
        v = report.get("validation", {})
        s = report.get("summary", {})
        q = report.get("quality", {})
        
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Validation Score", f"{v.get('validation_score')}/100")
        c2.metric("Status", v.get("validation_status"))
        c3.metric("Safe to create PDP job", "Yes" if safe_to_create else "No")
        c4.metric("Unique URLs", s.get("unique_product_urls"))
        
        c5, c6, c7, c8 = st.columns(4)
        c5.metric("Filtered In", s.get("filtered_in_count"))
        c6.metric("Filtered Out", s.get("filtered_out_count"))
        c7.metric("Duplicates", s.get("duplicate_product_urls", s.get("internal_duplicates_count", 0)))
        c8.metric("Sponsored Excluded", s.get("total_sponsored_cards_excluded", 0))

        c9, c10, c11, c12 = st.columns(4)
        c9.metric("Global Duplicates", s.get("global_duplicates_found", 0))
        c10.metric("Already Extracted", s.get("already_extracted_count", 0))
        c11.metric("New Products", s.get("new_products_count", 0))
        c12.metric("Missing Prices", q.get("missing_price_count", 0))
        
        for rec in v.get("recommendations", []):
            if "Không nên" in rec or "kiểm tra" in rec.lower():
                st.warning(rec)
            else:
                st.success(rec)
                

        pages = report.get("pages", [])
        if pages:
            st.subheader("Per-Page Breakdown")
            page_rows = []
            for p in pages:
                page_rows.append({
                    "Page": p.get("page_number"),
                    "Raw Cards": p.get("raw_card_count"),
                    "Organic": p.get("organic_card_count"),
                    "Sponsored": p.get("sponsored_card_count"),
                    "Unique URLs": p.get("unique_product_urls_on_page"),
                    "New URLs": p.get("new_urls_added_on_page"),
                    "Status": p.get("page_status", "ok")
                })
            st.dataframe(page_rows, hide_index=True)
            
            if st.checkbox("Show First 5 URLs per page (Debug)"):
                for p in pages:
                    with st.expander(f"Page {p.get('page_number')} First URLs"):
                        st.json(p.get("first_5_product_urls", []))
                        
        with st.expander("Detailed Metrics"):
            st.json(report)
            
        csv_path = os.path.join(job["output_dir"], "category_validation_items.csv")
        if os.path.exists(csv_path):
            st.download_button("Download CSV Report", data=open(csv_path, "rb").read(), file_name="category_validation_items.csv", mime="text/csv", key=f"cat-csv-{jid}")
            
    else:
        st.info("Validation report not generated yet. Click 'Generate Validation Report'.")

    st.subheader("Discovery Items")
    items = job_store.get_category_items(jid)
    st.write(f"Total items: {len(items)}")
    if items:
        item_rows = []
        for it in items:
            item_rows.append({
                "Page": it["page_number"],
                "URL": it["product_url"],
                "Price": it["card_price_raw"],
                "Min": it["card_price_min"],
                "Status": it["status"],
                "Reason": it["filter_reason"]
            })
        st.dataframe(item_rows, width="stretch", hide_index=True)


def tab_resumable_jobs(default_mode: str, default_threshold: int, default_save_grouped: bool) -> None:
    st.subheader("Resumable Jobs")
    st.caption("Persistent SQLite job queue for long Chewy PDP URL lists. Each URL is committed after it finishes.")

    with st.expander("Create New Job", expanded=True):
        job_name = st.text_input("Job name", value="overnight run", key="job-create-name")
        url_text = st.text_area("Chewy PDP URLs, one per line", height=150, key="job-create-urls")
        uploaded = st.file_uploader("Optional urls.txt upload", type=["txt"], key="job-create-upload")
        cols = st.columns([2, 1, 1, 1])
        mode = cols[0].selectbox("Mode", MODES, index=MODES.index(default_mode), key="job-create-mode")
        threshold = cols[1].number_input(
            "Confidence threshold",
            min_value=0,
            max_value=100,
            value=default_threshold,
            key="job-create-threshold",
        )
        max_attempts = cols[2].number_input("Max attempts", min_value=1, max_value=10, value=3, key="job-create-attempts")
        delay_seconds = cols[3].number_input(
            "Delay between items (sec)",
            min_value=0.0,
            max_value=3600.0,
            value=0.0,
            step=0.5,
            key="job-create-delay",
        )
        save_grouped = st.checkbox("Save grouped output", value=default_save_grouped, key="job-create-save-grouped")
        notes = st.text_input("Notes", key="job-create-notes")

        if st.button("Create Job", type="primary", key="job-create-button"):
            urls = parse_urls(url_text, uploaded)
            if not urls:
                st.error("Please enter at least one URL.")
            else:
                invalid = [url for url in urls if not url.startswith("https://www.chewy.com/")]
                if invalid:
                    st.warning(f"{len(invalid)} URL(s) are not valid Chewy URLs and will fail as invalid_url.")
                job_id = job_runner.create_job(
                    name=job_name,
                    urls=urls,
                    mode=job_mode_from_ui_mode(mode),
                    confidence_threshold=int(threshold),
                    max_attempts=int(max_attempts),
                    delay_seconds=float(delay_seconds),
                    save_grouped_output=save_grouped,
                    notes=notes,
                )
                st.session_state["selected_job_id"] = job_id
                st.success(f"Created job {job_id} with {len(urls)} URLs.")

    jobs = safe_db_call("loading resumable jobs", job_store.list_jobs, default=None)
    if jobs is None:
        return
    if not jobs:
        st.info("No resumable jobs found yet.")
        return

    st.subheader("Job History")
    auto_refresh_enabled, refresh_interval = render_realtime_refresh_controls(jobs)
    st.session_state["resumable_auto_refresh_enabled"] = auto_refresh_enabled
    st.session_state["resumable_auto_refresh_interval"] = refresh_interval
    job_rows = [
        {
            "job_id": job["job_id"],
            "name": job["name"],
            "status": job["status"],
            "mode": ui_mode_label_from_job_mode(job["mode"]),
            "total_urls": job["total_urls"],
            "completed": job["completed_count"],
            "failed": job["failed_count"],
            "pending": job["pending_count"],
            "created_at": job["created_at"],
            "updated_at": job["updated_at"],
        }
        for job in jobs
    ]
    st.dataframe(job_rows, width="stretch", hide_index=True)

    default_job_id = st.session_state.get("selected_job_id")
    default_index = 0
    if default_job_id:
        for idx, job in enumerate(jobs):
            if job["job_id"] == default_job_id:
                default_index = idx
                break

    selected_job = st.selectbox(
        "Select job",
        jobs,
        index=default_index,
        format_func=lambda job: f"{job['job_id']} - {job['name']} ({job['status']})",
        key="job-select",
    )
    if not selected_job:
        return
    job_id = selected_job["job_id"]
    st.session_state["selected_job_id"] = job_id

    st.subheader("Job Controls")
    summary = render_job_progress(job_id)
    if not summary:
        return
    all_items = safe_db_call("loading job items", job_store.get_job_items, job_id, default=[])
    current_item = next((item for item in all_items if item["status"] in {"running", "paused", "pending"}), None)
    if current_item:
        st.caption(f"Current/next: #{current_item['index_number']} {current_item['input_url']}")

    render_live_job_report(job_id, all_items)

    retry_failed = st.checkbox("Include failed items when starting/resuming", value=True, key=f"job-retry-failed-{job_id}")
    resume_paused = st.checkbox("Include paused items after pending/failed items", value=True, key=f"job-resume-paused-{job_id}")
    force_retry = st.checkbox("Force retry beyond max attempts", value=False, key=f"job-force-retry-{job_id}")
    reprocess_completed = st.checkbox("Reprocess completed items", value=False, key=f"job-reprocess-completed-{job_id}")
    reprocess_existing = st.checkbox("Ignore existing output check", value=False, key=f"job-reprocess-existing-{job_id}")
    parallel_worker_count = st.number_input(
        "Controlled CW workers",
        min_value=1,
        max_value=3,
        value=1,
        step=1,
        key=f"job-parallel-workers-{job_id}",
        help="Use 2-3 only when CW profile templates and proxies are configured.",
    )

    controls = st.columns(5)
    log_box = st.empty()

    with controls[0]:
        if st.button("Start", key=f"job-start-{job_id}"):
            callback, live_lines = make_live_log_callback(log_box)
            with st.spinner("Processing resumable job..."):
                result = job_runner.process_job(
                    job_id,
                    retry_failed=retry_failed,
                    resume_paused=False,
                    reprocess_completed=reprocess_completed,
                    reprocess_existing=reprocess_existing,
                    force_retry=force_retry,
                    on_line=callback,
                )
            log_box.code("\n".join(live_lines[-300:]), language="text")
            st.json(result, expanded=False)
    with controls[1]:
        if st.button("Pause", key=f"job-pause-{job_id}"):
            job_runner.pause_job(job_id)
            st.warning("Pause requested. A running job will stop after the current item finishes.")
            st.rerun()
    with controls[2]:
        if st.button("Resume", key=f"job-resume-{job_id}"):
            callback, live_lines = make_live_log_callback(log_box)
            with st.spinner("Resuming job..."):
                result = job_runner.resume_job(
                    job_id,
                    retry_failed=True,
                    resume_paused=resume_paused,
                    reprocess_completed=reprocess_completed,
                    reprocess_existing=reprocess_existing,
                    force_retry=force_retry,
                    on_line=callback,
                )
            log_box.code("\n".join(live_lines[-300:]), language="text")
            st.json(result, expanded=False)
    with controls[3]:
        if st.button("Retry Failed", key=f"job-retry-button-{job_id}"):
            callback, live_lines = make_live_log_callback(log_box)
            with st.spinner("Retrying failed items..."):
                result = job_runner.retry_failed_items(job_id, force=force_retry, start=True, on_line=callback)
            log_box.code("\n".join(live_lines[-300:]), language="text")
            st.json(result, expanded=False)
    with controls[4]:
        if st.button("Skip Current", key=f"job-skip-{job_id}"):
            skipped = job_runner.skip_current_item(job_id)
            if skipped:
                st.warning(f"Skipped item #{skipped['index_number']}.")
            else:
                st.info("No current item to skip.")
            st.rerun()

    if parallel_worker_count > 1:
        if st.button("Start / Resume Parallel Workers", key=f"job-parallel-start-{job_id}"):
            import parallel_resumable_runner
            callback, live_lines = make_live_log_callback(log_box)
            with st.spinner("Processing with controlled CW workers..."):
                result = parallel_resumable_runner.process_job_parallel(
                    job_id,
                    worker_count=int(parallel_worker_count),
                    retry_failed=True,
                    resume_paused=resume_paused,
                    reprocess_existing=reprocess_existing,
                    force_retry=force_retry,
                    requeue_fallback_done=True,
                    on_line=callback,
                )
            log_box.code("\n".join(live_lines[-300:]), language="text")
            st.json(result, expanded=False)

    st.subheader("Job Exporter")
    with st.expander("Consolidate & Export Job", expanded=False):
        export_name = st.text_input("Custom Export Name (Optional)", key=f"export-name-{job_id}")
        archive_raw = st.checkbox("Archive raw per-product JSON files", value=False, key=f"export-archive-{job_id}")
        delete_after = st.checkbox("Delete raw files after archiving (Destructive)", value=False, key=f"export-delete-{job_id}")
        
        if st.button("Export Job", key=f"export-btn-{job_id}"):
            with st.spinner("Consolidating products..."):
                import job_exporter
                summary = job_exporter.export_job_products(
                    pdp_job_id=job_id,
                    custom_name=export_name if export_name else None,
                    archive_raw=archive_raw,
                    delete_after_archive=delete_after
                )
                if summary:
                    st.success(f"Export completed! {summary['successful_base_products']} products exported.")
                    st.json(summary)
                else:
                    st.error("Export failed. See logs.")
                    
    job = safe_db_call("loading selected job", job_store.get_job, job_id, default=None)
    if job:
        job_dir = Path(job["output_dir"])
        with st.expander("Job files"):
            render_job_file(str(job_dir / "job_config.json"), "job_config.json", f"job-config-{job_id}")
            render_job_file(str(job_dir / "job_summary.json"), "job_summary.json", f"job-summary-{job_id}")
            render_job_file(str(job_dir / "job_items_report.csv"), "job_items_report.csv", f"job-report-{job_id}")
            render_job_file(str(job_dir / "urls.txt"), "urls.txt", f"job-urls-{job_id}")

    st.subheader("Items")
    status_filter = st.selectbox(
        "Filter",
        ["all", "pending", "running", "done", "failed", "skipped", "paused"],
        key=f"job-item-filter-{job_id}",
    )
    items = safe_db_call(
        "loading filtered job items",
        job_store.get_job_items,
        job_id,
        status=None if status_filter == "all" else status_filter,
        default=[],
    )
    item_rows = [
        {
            "index": item["index_number"],
            "status": item["status"],
            "URL": item["input_url"],
            "attempts": item["attempts"],
            "confidence score": item["confidence_score"],
            "page_kind": item["page_kind"],
            "architecture": item["architecture"],
            "profile slot": item.get("profile_slot_id"),
            "profile id": item.get("profile_id_used"),
            "worker": item.get("worker_id"),
            "white screens": item.get("white_screen_count"),
            "output path": item["grouped_output_path"],
            "error type": item["error_type"],
            "error message": item["error_message"],
        }
        for item in items
    ]
    st.dataframe(item_rows, width="stretch", hide_index=True)
    render_selected_job_item(job_id, items)


def tab_output_browser() -> None:
    st.subheader("Output Browser")
    folder_label = st.selectbox("Output folder", list(OUTPUT_FOLDERS.keys()), index=0)
    folder = OUTPUT_FOLDERS[folder_label]
    search = st.text_input("Search by product ID or filename")
    files = list_files(folder, suffixes=(".json",), search=search, recursive=folder == UI_RUNS_DIR)
    selected = select_json_file("JSON file", files, key="output-browser-select")
    if not selected:
        return
    render_file_actions(selected, "output-browser")
    try:
        st.json(read_json_file(selected["path"]), expanded=False)
    except Exception as exc:
        st.error(f"Could not preview JSON: {exc}")


def tab_grouped_preview() -> None:
    st.subheader("Grouped Product Preview")
    search = st.text_input("Search grouped product files", key="grouped-search")
    files = list_files(OUTPUT_FOLDERS["Grouped products"], suffixes=(".json",), search=search)
    selected = select_json_file("Grouped product JSON", files, key="grouped-select")
    if not selected:
        return
    render_file_actions(selected, "grouped")
    try:
        render_grouped_product(read_json_file(selected["path"]))
    except Exception as exc:
        st.error(f"Could not render grouped preview: {exc}")


def tab_validation(default_threshold: int) -> None:
    st.subheader("Validation Viewer")
    threshold = st.number_input("Validation threshold", min_value=0, max_value=100, value=default_threshold, key="validation-threshold")
    search = st.text_input("Search validation files", key="validation-search")
    files = list_files(OUTPUT_FOLDERS["Validation reports"], suffixes=(".json",), search=search)
    selected = select_json_file("Validation JSON", files, key="validation-select")
    if not selected:
        return
    render_file_actions(selected, "validation")
    try:
        data = read_json_file(selected["path"])
        render_validation_report(data, int(threshold))
        with st.expander("Raw validation JSON"):
            st.json(data, expanded=False)
    except Exception as exc:
        st.error(f"Could not render validation report: {exc}")


def tab_diagnostics() -> None:
    st.subheader("Error / Diagnostic Viewer")
    folder_label = st.selectbox("Diagnostic folder", ["JSON extractor failures", "Batch failures"])
    search = st.text_input("Search diagnostics", key="diagnostic-search")
    files = list_files(OUTPUT_FOLDERS[folder_label], suffixes=(".json",), search=search)
    selected = select_json_file("Diagnostic JSON", files, key="diagnostic-select")
    if not selected:
        return
    render_file_actions(selected, "diagnostic")
    try:
        data = read_json_file(selected["path"])
        summary = summarize_error(json.dumps(data, ensure_ascii=False), data)
        if summary.get("reason"):
            st.error(f"Reason: {summary['reason']}")
        if summary.get("action"):
            st.info(f"Action: {summary['action']}")

        cols = st.columns(4)
        cols[0].metric("page_kind", data.get("page_kind", "n/a"))
        cols[1].metric("architecture", data.get("detected_architecture", "n/a"))
        cols[2].metric("confidence", data.get("confidence_score", data.get("validation_confidence_score", "n/a")))
        cols[3].metric("fallback_used", str(data.get("fallback_used", "n/a")))
        st.write("**Input URL**")
        st.code(str(data.get("input_url", "")), language="text")
        st.write("**Final URL**")
        st.code(str(data.get("final_url", "")), language="text")
        st.write("**Error**")
        st.code(str(data.get("error", "")), language="text")
        st.write("**Warnings**")
        st.json(data.get("warnings", []))
        st.write("**Fallback reason**")
        st.code(str(data.get("fallback_reason", "")), language="text")
        with st.expander("Raw diagnostic JSON"):
            st.json(data, expanded=False)
    except Exception as exc:
        st.error(f"Could not render diagnostic file: {exc}")


def tab_run_history() -> None:
    st.subheader("Run History")
    rows = list_run_history()
    if not rows:
        st.info("No UI runs found yet.")
        return
    table_rows = [
        {
            "run_id": row["run_id"],
            "timestamp": row["timestamp"],
            "kind": row["kind"],
            "mode": row["mode"],
            "URL or file": row["target"],
            "batch count": row["batch_count"],
            "status": row["status"],
            "duration": row["duration_seconds"],
            "exit code": row["exit_code"],
        }
        for row in rows
    ]
    st.dataframe(table_rows, width="stretch", hide_index=True)

    selected = st.selectbox("Select run", rows, format_func=lambda row: f"{row['run_id']} - {row.get('status')}")
    run_dir = UI_RUNS_DIR / selected["run_id"]
    cols = st.columns(2)
    with cols[0]:
        st.write("**run.log**")
        st.code(selected["run_log"], language="text")
        if Path(selected["run_log"]).exists():
            if st.button("Open run.log", key=f"open-log-{selected['run_id']}"):
                open_in_default_app(selected["run_log"])
            download_button(selected["run_log"], label="Download run.log", key_suffix=f"history-log-{selected['run_id']}")
    with cols[1]:
        st.write("**run_summary.json**")
        st.code(selected["run_summary"], language="text")
        if Path(selected["run_summary"]).exists():
            if st.button("Open run_summary.json", key=f"open-summary-{selected['run_id']}"):
                open_in_default_app(selected["run_summary"])
            download_button(
                selected["run_summary"],
                label="Download summary",
                key_suffix=f"history-summary-{selected['run_id']}",
            )

    config_path = run_dir / "run_config.json"
    if config_path.exists():
        with st.expander("run_config.json"):
            st.json(read_json_file(config_path), expanded=False)
    if Path(selected["run_summary"]).exists():
        with st.expander("run_summary.json"):
            st.json(read_json_file(selected["run_summary"]), expanded=False)
    if Path(selected["run_log"]).exists():
        with st.expander("run.log tail"):
            st.code(read_text_file(selected["run_log"], limit_chars=20000), language="text")


def tab_adsp_pool() -> None:
    st.header("AdsPower Pool / Profile Recovery")
    st.warning("This tool does not bypass captcha or anti-bot systems. Profile rotation is limited to user-configured profiles and controlled retries. If all profiles are blocked, job will pause for manual action.")
    
    import adsp_profile_pool_manager
    import adsp_profile_recovery_manager
    import config
    
    if not config.ADSP_PROFILE_POOL_ENABLED:
        st.info("Profile Pool is disabled in config. To enable it, set ADSP_PROFILE_POOL_ENABLED=true")

    st.subheader("Profile Templates")
    st.caption("Fixed slots only: CW_1, CW_2, CW_3. Proxy credentials are masked.")
    tcol1, tcol2 = st.columns([2, 1])
    with tcol1:
        if st.button("Sync Templates From Config", key="btn_sync_templates"):
            adsp_profile_recovery_manager.sync_profile_templates_to_db()
            st.rerun()
    with tcol2:
        st.metric("Configured workers", getattr(config, "ADSP_WORKER_COUNT", 3))

    templates = adsp_profile_recovery_manager.get_profile_template_status()
    template_rows = [
        {
            "slot_id": row.get("slot_id"),
            "profile_id": row.get("adspower_profile_id"),
            "masked_proxy": row.get("masked_proxy"),
            "status": row.get("status"),
            "last_used_at": row.get("last_used_at"),
            "last_white_screen_at": row.get("last_white_screen_at"),
            "total_success": row.get("total_success"),
            "total_white_screen": row.get("total_white_screen"),
            "total_rebuilds": row.get("total_rebuilds"),
            "success_rate": row.get("success_rate"),
            "notes": row.get("notes"),
        }
        for row in templates
    ]
    st.dataframe(template_rows, use_container_width=True)

    st.subheader("Template Manual Override")
    if templates:
        selected_slot = st.selectbox("Select Slot", [row["slot_id"] for row in templates], key="selected-template-slot")
        m1, m2, m3, m4 = st.columns(4)
        with m1:
            if st.button("Force Rebuild Slot", key="btn_force_rebuild_slot"):
                result = adsp_profile_recovery_manager.request_rebuild(selected_slot)
                st.json(result, expanded=False)
                st.rerun()
        with m2:
            if st.button("Release Slot", key="btn_release_slot"):
                adsp_profile_recovery_manager.release_template(selected_slot)
                st.rerun()
        with m3:
            if st.button("Disable Slot", key="btn_disable_slot"):
                adsp_profile_recovery_manager.disable_template(selected_slot)
                st.rerun()
        with m4:
            if st.button("Test Start Profile", key="btn_test_start_slot"):
                st.json(adsp_profile_recovery_manager.test_start_profile(selected_slot), expanded=False)

    st.subheader("Parallel Worker Settings")
    worker_rows = adsp_profile_recovery_manager.get_worker_slot_status(getattr(config, "ADSP_WORKER_COUNT", 3))
    st.dataframe(
        [
            {
                "worker": f"worker_{idx}",
                "slot_id": row.get("slot_id"),
                "profile_id": row.get("adspower_profile_id"),
                "status": row.get("status"),
                "notes": row.get("notes"),
            }
            for idx, row in enumerate(worker_rows, start=1)
        ],
        use_container_width=True,
    )
    
    col1, col2 = st.columns([3, 1])
    with col1:
        if st.button("Refresh Profile Pool", key="btn_refresh_pool"):
            adsp_profile_pool_manager.sync_profile_pool_to_db()
            st.rerun()
            
    profiles = adsp_profile_pool_manager.get_profile_health_summary()
    st.dataframe(profiles, use_container_width=True)
    
    st.subheader("Manual Controls")
    c1, c2, c3 = st.columns(3)
    
    selected_profile = st.selectbox("Select Profile ID", [p["profile_id"] for p in profiles]) if profiles else None
    
    with c1:
        if st.button("Release Quarantined Profile") and selected_profile:
            adsp_profile_pool_manager.release_profile(selected_profile)
            st.rerun()
            
    with c2:
        if st.button("Quarantine Profile (30m)") and selected_profile:
            adsp_profile_pool_manager.quarantine_profile(selected_profile, "Manual UI Quarantine", 30)
            st.rerun()
            
    with c3:
        if st.button("Disable Profile") and selected_profile:
            import job_store
            with job_store.connect() as conn:
                conn.execute("UPDATE adsp_profile_pool SET status = 'disabled' WHERE profile_id = ?", (selected_profile,))
                conn.commit()
            st.rerun()
            
    st.divider()
    st.subheader("White Screen Events")
    import job_store
    try:
        with job_store.connect() as conn:
            events = job_store.rows_to_dicts(conn.execute("SELECT * FROM white_screen_events ORDER BY created_at DESC LIMIT 50").fetchall())
        st.dataframe(events, use_container_width=True)
    except Exception:
        st.info("No white screen events table found yet.")

def tab_exports():
    st.header("Consolidated Exports")
    st.write("Manage your generated job exports in `output/exports/`.")
    
    exports_dir = Path(OUTPUT_FOLDERS["exports"]) if "exports" in OUTPUT_FOLDERS else Path("output/exports")
    if not exports_dir.exists():
        st.info("No exports directory found.")
        return
        
    export_files = [f for f in exports_dir.glob("*_summary.json")]
    if not export_files:
        st.info("No export summaries found.")
        return
        
    export_files.sort(key=lambda x: x.stat().st_mtime, reverse=True)
    
    for sum_file in export_files:
        with st.expander(f"Export: {sum_file.name.replace('_summary.json', '')}", expanded=False):
            try:
                import json
                with open(sum_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                
                st.write(f"**Created At:** {data.get('created_at')}")
                st.write(f"**Job ID:** {data.get('pdp_job_id')}")
                st.write(f"**Products:** {data.get('successful_base_products')} | **Variants:** {data.get('total_variants')}")
                
                files = data.get("files", {})
                st.json(data)
                
                # Show file downloads
                for k, path in files.items():
                    if path and os.path.exists(path):
                        with open(path, "rb") as bf:
                            st.download_button(f"Download {k}", bf, file_name=os.path.basename(path), key=f"dl-{sum_file.name}-{k}")
            except Exception as e:
                st.error(f"Could not read {sum_file.name}: {e}")

def tab_registry_browser():
    st.header("Global Product Registry Browser")
    st.write("Browse and search the global product deduplication registry.")
    
    col1, col2 = st.columns(2)
    with col1:
        search_pid = st.text_input("Search by Product ID", "")
    with col2:
        status_filter = st.selectbox(
            "Filter by Status", 
            ["all", "never_extracted", "extracted_success", "extracted_failed", "skipped_existing"]
        )
        
    if st.button("Refresh / Search", key="reg_refresh"):
        import job_store
        import pandas as pd
        try:
            with job_store.connect() as conn:
                query = "SELECT product_id, latest_url, extraction_status, discovery_count, updated_at FROM chewy_product_registry"
                params = []
                conditions = []
                
                if search_pid:
                    conditions.append("product_id LIKE ?")
                    params.append(f"%{search_pid}%")
                if status_filter != "all":
                    conditions.append("extraction_status = ?")
                    params.append(status_filter)
                    
                if conditions:
                    query += " WHERE " + " AND ".join(conditions)
                    
                query += " ORDER BY updated_at DESC LIMIT 200"
                
                df = pd.read_sql_query(query, conn, params=params)
            
            st.dataframe(df, use_container_width=True)
        except Exception as e:
            st.error(f"Error loading registry: {e}")

def sidebar_settings() -> tuple[str, int, bool]:
    st.sidebar.header("Run Settings")
    mode = st.sidebar.selectbox("Default mode", MODES, index=MODES.index(MODE_JSON_FALLBACK))
    threshold = st.sidebar.number_input("Default confidence threshold", min_value=0, max_value=100, value=75)
    save_grouped = st.sidebar.checkbox("Save grouped output", value=True)
    st.sidebar.subheader("Feature Flags")
    st.sidebar.json(feature_flags(mode, int(threshold), save_grouped), expanded=True)
    st.sidebar.caption(f"Project root: {relative_path(PROJECT_ROOT)}")
    return mode, int(threshold), save_grouped


def main() -> None:
    inject_styles()
    default_mode, default_threshold, default_save_grouped = sidebar_settings()
    render_header(default_mode)

    tabs = st.tabs(
        [
            "Single Product",
            "Batch Test",
            "Category Discovery",
            "Resumable Jobs",
            "Output Browser",
            "Grouped Output Preview",
            "Validation",
            "Diagnostics",
            "AdsPower Pool",
            "Registry Browser",
            "Exports",
            "Run History",
        ]
    )
    with tabs[0]:
        render_tab_safely("Single Product", tab_single, default_mode, default_threshold, default_save_grouped)
    with tabs[1]:
        render_tab_safely("Batch Test", tab_batch, default_mode, default_threshold, default_save_grouped)
        with st.expander("Latest known batch report"):
            render_batch_report(latest_batch_report(), prefix="batch-latest-report")
    with tabs[2]:
        render_tab_safely("Category Discovery", tab_category_discovery)
    with tabs[3]:
        render_tab_safely("Resumable Jobs", tab_resumable_jobs, default_mode, default_threshold, default_save_grouped)
    with tabs[4]:
        render_tab_safely("Output Browser", tab_output_browser)
    with tabs[5]:
        render_tab_safely("Grouped Output Preview", tab_grouped_preview)
    with tabs[6]:
        render_tab_safely("Validation", tab_validation, default_threshold)
    with tabs[7]:
        render_tab_safely("Diagnostics", tab_diagnostics)
    with tabs[8]:
        render_tab_safely("AdsPower Pool", tab_adsp_pool)
    with tabs[9]:
        render_tab_safely("Registry Browser", tab_registry_browser)
    with tabs[10]:
        render_tab_safely("Exports", tab_exports)
    with tabs[11]:
        render_tab_safely("Run History", tab_run_history)

    maybe_autorefresh(
        bool(st.session_state.get("resumable_auto_refresh_enabled", False)),
        float(st.session_state.get("resumable_auto_refresh_interval", 3.0)),
    )


if __name__ == "__main__":
    main()
