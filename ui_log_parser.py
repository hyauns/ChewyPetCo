"""Helpers for turning scraper logs into UI-friendly status summaries."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
SCORE_RE = re.compile(r"(?:Score|confidence(?:[_ ]score)?)[^\d]*(\d+(?:\.\d+)?)", re.IGNORECASE)


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text or "")


def tail_lines(text: str, count: int = 50) -> list[str]:
    lines = strip_ansi(text).splitlines()
    return lines[-count:]


def parse_confidence_score(log_text: str) -> float | None:
    matches: list[str] = []
    for line in strip_ansi(log_text).splitlines():
        if "threshold" in line.lower():
            continue
        matches.extend(SCORE_RE.findall(line))
    if not matches:
        return None
    try:
        return float(matches[-1])
    except ValueError:
        return None


def read_json(path: str | Path) -> dict[str, Any] | None:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def first_diagnostic(output_files: list[dict[str, Any]]) -> dict[str, Any] | None:
    for file_info in output_files:
        path = Path(file_info.get("path", ""))
        normalized = str(path).replace("\\", "/")
        if "json_extractor_failures" in normalized or "batch_failures" in normalized:
            diag = read_json(path)
            if diag:
                return diag
    return None


def detect_redirected_plp(log_text: str, diagnostic: dict[str, Any] | None = None) -> bool:
    clean = strip_ansi(log_text).lower()
    if "redirected_plp" in clean or "route is a plp page" in clean:
        return True
    if "json extractor failed" in clean and "running old scraper" in clean:
        empty_product_id = re.search(r"product id:\s*(?:\r?\n|$)", clean) is not None
        generic_plp_summary = "title:        dog food" in clean or "categories:   dog supplies" in clean
        if empty_product_id and generic_plp_summary:
            return True
    if diagnostic:
        fields = [
            diagnostic.get("page_kind"),
            diagnostic.get("final_url"),
            diagnostic.get("error"),
            diagnostic.get("fallback_reason"),
            " ".join(str(w) for w in diagnostic.get("warnings", [])),
        ]
        text = " ".join(str(item) for item in fields if item).lower()
        if "redirected_plp" in text or "plp page" in text or "category/listing" in text:
            return True
        final_url = str(diagnostic.get("final_url") or "")
        if final_url and "/dp/" not in final_url:
            return True
    return False


def detect_fallback_used(log_text: str, mode: str, diagnostic: dict[str, Any] | None = None) -> bool:
    clean = strip_ansi(log_text)
    if "JSON extractor + old scraper fallback" in mode and "Running OLD Scraper" in clean:
        return True
    if diagnostic and str(diagnostic.get("fallback_used")).lower() in {"true", "yes"}:
        return True
    return False


def summarize_error(
    log_text: str,
    diagnostic: dict[str, Any] | None = None,
    exit_code: int | None = None,
) -> dict[str, str | None]:
    clean = strip_ansi(log_text)
    lower = clean.lower()
    diag_error = str(diagnostic.get("error")) if diagnostic and diagnostic.get("error") else ""
    diag_text = json.dumps(diagnostic or {}, ensure_ascii=False).lower()

    if detect_redirected_plp(clean, diagnostic):
        return {
            "reason": "URL redirected to a PLP/category page, not a real PDP.",
            "action": "Do not import this URL. Find a valid live Chewy PDP URL.",
            "details": diag_error or None,
        }

    adspower_failure = any(
        phrase in lower
        for phrase in [
            "adspower connection failed",
            "failed to connect",
            "connection refused",
            "connectionerror",
            "get_ws_endpoint",
        ]
    )
    if "adspower" in lower and adspower_failure:
        return {
            "reason": "Browser/AdsPower connection failed.",
            "action": "Check AdsPower is running and config.py has the correct profile ID.",
            "details": last_exception_line(clean),
        }

    if any(
        phrase in lower
        for phrase in [
            "net::err_socks_connection_failed",
            "net::err_proxy_connection_failed",
            "net::err_tunnel_connection_failed",
            "proxy connection failed",
            "socks connection failed",
        ]
    ):
        return {
            "reason": "The AdsPower profile proxy could not connect.",
            "action": "The resumable runner will rotate profiles after the configured proxy-failure threshold.",
            "details": last_exception_line(clean),
        }

    if "permissionerror" in lower or "access is denied" in lower or "permission denied" in lower:
        return {
            "reason": "Cannot write output files. Check folder permissions.",
            "action": "Confirm the output folder is writable and no generated file is locked.",
            "details": last_exception_line(clean),
        }

    if "confidence score below threshold" in diag_text or "validation failed" in diag_text:
        return {
            "reason": "Extraction completed but confidence is below threshold or validation failed.",
            "action": "Review validation before using output.",
            "details": diag_error or None,
        }

    if "fetch initial html failed" in lower:
        return {
            "reason": "The scraper could not fetch initial page HTML.",
            "action": "Check browser connectivity, AdsPower state, and whether the URL still loads.",
            "details": None,
        }

    if diag_error:
        return {
            "reason": diag_error,
            "action": "Review the diagnostic file and run log for the failed step.",
            "details": diagnostic.get("fallback_reason") if diagnostic else None,
        }

    if exit_code not in (None, 0):
        return {
            "reason": f"Script exited with code {exit_code}.",
            "action": "Review the last log lines and run.log for the raw error.",
            "details": last_exception_line(clean),
        }

    return {"reason": None, "action": None, "details": None}


def last_exception_line(log_text: str) -> str | None:
    for line in reversed(strip_ansi(log_text).splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        if any(token in stripped for token in ["Error", "Exception", "Traceback", "failed", "Failed"]):
            return stripped
    return None


def summarize_run(
    *,
    log_text: str,
    exit_code: int,
    mode: str,
    threshold: int,
    output_files: list[dict[str, Any]],
) -> dict[str, Any]:
    diagnostic = first_diagnostic(output_files)
    clean = strip_ansi(log_text)
    score = parse_confidence_score(clean)
    fallback_used = detect_fallback_used(clean, mode, diagnostic)
    redirected_plp = detect_redirected_plp(clean, diagnostic)
    low_confidence = score is not None and score < threshold

    if exit_code != 0:
        status = "Failed"
    elif fallback_used:
        status = "Fallback Used"
    elif redirected_plp:
        status = "Redirected PLP"
    elif low_confidence:
        status = "Low Confidence"
    elif "JSON Extractor Success" in clean or "Done! Check output/" in clean or "BATCH TEST SUMMARY" in clean:
        status = "Success"
    else:
        status = "Success"

    error_summary = summarize_error(clean, diagnostic, exit_code)

    return {
        "status": status,
        "fallback_used": fallback_used,
        "redirected_plp": redirected_plp,
        "low_confidence": low_confidence,
        "confidence_score": score,
        "diagnostic": diagnostic,
        "error_summary": error_summary,
        "last_log_lines": tail_lines(clean, 50),
    }
