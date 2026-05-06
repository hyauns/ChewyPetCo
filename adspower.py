"""
AdsPower Local API client.
Manages browser profile lifecycle: start, stop, status check.
"""

import httpx
import config


def _api_url(path: str) -> str:
    return f"{config.ADSPOWER_API_BASE}{path}"


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
    Raises RuntimeError on failure.
    """
    pid = profile_id or config.ADSPOWER_PROFILE_ID
    if not pid:
        raise ValueError(
            "No profile ID. Set ADSPOWER_PROFILE_ID in config.py "
            "or pass profile_id argument."
        )

    resp = httpx.get(
        _api_url("/api/v1/browser/start"),
        params={"user_id": pid},
        timeout=30,
    )
    data = resp.json()

    if data.get("code") != 0:
        raise RuntimeError(f"AdsPower start failed: {data.get('msg', data)}")

    return data["data"]


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
