import os
import threading
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
    "being used",
]

_MAX_START_RETRIES = 5
_RETRY_BASE_DELAY = 10  # seconds

_API_LOCK = threading.Lock()
_LAST_API_CALL = 0.0
_API_MIN_INTERVAL = float(os.environ.get("ADSP_API_INTERVAL_SECONDS", "1.5"))


def safe_api_request(method: str, path: str, **kwargs) -> httpx.Response:
    global _LAST_API_CALL
    with _API_LOCK:
        elapsed = time.monotonic() - _LAST_API_CALL
        if elapsed < _API_MIN_INTERVAL:
            time.sleep(_API_MIN_INTERVAL - elapsed)
        
        try:
            if method.upper() == "GET":
                resp = httpx.get(_api_url(path), **kwargs)
            else:
                resp = httpx.post(_api_url(path), **kwargs)
            return resp
        finally:
            _LAST_API_CALL = time.monotonic()


def _api_url(path: str) -> str:
    return f"{config.ADSPOWER_API_BASE}{path}"


def _is_transient_error(msg: str) -> bool:
    lower = msg.lower()
    return any(token in lower for token in _TRANSIENT_MSG_TOKENS)


def check_connection() -> bool:
    """Check if AdsPower desktop app is running and API is accessible."""
    try:
        resp = safe_api_request("GET", "/status", timeout=5)
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
            resp = safe_api_request("GET", "/api/v1/browser/start", params={"user_id": pid}, timeout=30)
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
    
    last_msg = ""
    for attempt in range(3):
        try:
            resp = safe_api_request("GET", "/api/v1/browser/stop", params={"user_id": pid}, timeout=15)
            data = resp.json()
            if data.get("code") == 0:
                return True
                
            last_msg = data.get("msg", str(data))
            # "not open" / "does not exist" = browser is already closed = success.
            lower_msg = last_msg.lower()
            if "not open" in lower_msg or "does not exist" in lower_msg or "not exist" in lower_msg:
                return True
            if _is_transient_error(last_msg):
                time.sleep(2)
                continue
            # Non-transient error
            break
        except Exception as exc:
            last_msg = str(exc)
            time.sleep(2)
            continue
            
    print(f"[adspower] Failed to stop profile {pid}: {last_msg}")
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
