"""
AdsPower Local API client.
Manages browser profile lifecycle: start, stop, status check.
"""

import time

import httpx
import config

# Transient AdsPower API error substrings that warrant a retry.
_TRANSIENT_MSG_TOKENS = [
    "updating",
    "waiting for download",
    "please try again",
    "too many request",
    "browser is starting",
    "failed to start",
]

_MAX_START_RETRIES = 5
_RETRY_BASE_DELAY = 10  # seconds


def _api_url(path: str) -> str:
    return f"{config.ADSPOWER_API_BASE}{path}"


def _is_transient_error(msg: str) -> bool:
    lower = msg.lower()
    return any(token in lower for token in _TRANSIENT_MSG_TOKENS)


def check_connection() -> bool:
    """Check if AdsPower desktop app is running and API is accessible."""
    try:
        resp = httpx.get(_api_url("/status"), timeout=5)
        return resp.status_code == 200
    except httpx.ConnectError:
        return False


def start_profile(profile_id: str | None = None) -> dict:
    """
    Start an AdsPower browser profile.
    Returns dict with 'ws' key containing puppeteer/selenium websocket URLs.
    Retries automatically on transient errors (e.g. browser updating).
    Raises RuntimeError on persistent failure.
    """
    pid = profile_id or config.ADSPOWER_PROFILE_ID
    if not pid:
        raise ValueError(
            "No profile ID. Set ADSPOWER_PROFILE_ID in config.py "
            "or pass profile_id argument."
        )

    last_msg = ""
    for attempt in range(_MAX_START_RETRIES):
        try:
            resp = httpx.get(
                _api_url("/api/v1/browser/start"),
                params={"user_id": pid},
                timeout=30,
            )
            data = resp.json()
        except (httpx.ConnectError, httpx.ReadTimeout) as exc:
            last_msg = str(exc)
            if attempt < _MAX_START_RETRIES - 1:
                delay = _RETRY_BASE_DELAY * (attempt + 1)
                print(f"[adspower] Connection error on attempt {attempt + 1}: {last_msg}. Retrying in {delay}s...")
                time.sleep(delay)
                continue
            raise RuntimeError(f"AdsPower start failed after {_MAX_START_RETRIES} attempts: {last_msg}")

        if data.get("code") == 0:
            return data["data"]

        last_msg = data.get("msg", str(data))
        if _is_transient_error(last_msg) and attempt < _MAX_START_RETRIES - 1:
            delay = _RETRY_BASE_DELAY * (attempt + 1)
            print(f"[adspower] Transient error on attempt {attempt + 1}: {last_msg}. Retrying in {delay}s...")
            time.sleep(delay)
            continue

        # Non-transient error — fail immediately
        raise RuntimeError(f"AdsPower start failed: {last_msg}")

    raise RuntimeError(f"AdsPower start failed after {_MAX_START_RETRIES} attempts: {last_msg}")


def stop_profile(profile_id: str | None = None) -> bool:
    """Stop a running AdsPower browser profile."""
    pid = profile_id or config.ADSPOWER_PROFILE_ID
    try:
        resp = httpx.get(
            _api_url("/api/v1/browser/stop"),
            params={"user_id": pid},
            timeout=10,
        )
        return resp.json().get("code") == 0
    except Exception:
        return False


def get_ws_endpoint(profile_data: dict) -> str:
    """Extract the CDP websocket URL from AdsPower start response."""
    ws = profile_data.get("ws", {})
    # Prefer puppeteer endpoint (works with Playwright connect_over_cdp)
    url = ws.get("puppeteer", ws.get("selenium", ""))
    if not url:
        raise RuntimeError(
            f"No websocket URL in AdsPower response: {profile_data}"
        )
    return url
