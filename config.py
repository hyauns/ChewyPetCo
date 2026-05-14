"""
Configuration for Pet Product Scraper (Chewy / Petco).
Update ADSPOWER_PROFILE_ID with your actual AdsPower browser profile ID.
"""

import os
from pathlib import Path


def _load_local_env(path: str = ".env", *, override: bool = False) -> None:
    """Load local .env values without adding a dependency.

    Existing process environment variables win over values in .env unless
    override=True. Runtime job starts use override=True so edited proxies are
    picked up without restarting the UI process.
    """
    env_path = Path(__file__).resolve().parent / path
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and (override or key not in os.environ):
            os.environ[key] = value


_load_local_env()

# AdsPower Local API
ADSPOWER_API_BASE = os.environ.get("ADSPOWER_API_BASE", "http://127.0.0.1:50325")
ADSPOWER_PROFILE_ID = os.environ.get("ADSPOWER_PROFILE_ID", "k1chlcc3")  # <-- Set your AdsPower profile ID here

# ---------------------------------------------------------------------------
# Feature Flags - Phase 3C
# ---------------------------------------------------------------------------
USE_CHEWY_NEXT_JSON_EXTRACTOR = os.environ.get("USE_CHEWY_NEXT_JSON_EXTRACTOR", "false").lower() == "true"
CHEWY_JSON_FALLBACK_TO_OLD_SCRAPER = os.environ.get("CHEWY_JSON_FALLBACK_TO_OLD_SCRAPER", "false").lower() == "true"
CHEWY_JSON_CONFIDENCE_THRESHOLD = int(os.environ.get("CHEWY_JSON_CONFIDENCE_THRESHOLD", "75"))

# Category Discovery settings
CATEGORY_DISCOVERY_SAVE_PAGE_DEBUG = os.environ.get("CATEGORY_DISCOVERY_SAVE_PAGE_DEBUG", "true").lower() == "true"
CATEGORY_EXCLUDE_SPONSORED_PRODUCTS = os.environ.get("CATEGORY_EXCLUDE_SPONSORED_PRODUCTS", "true").lower() == "true"
CHEWY_JSON_SAVE_GROUPED_OUTPUT = os.environ.get("CHEWY_JSON_SAVE_GROUPED_OUTPUT", "true").lower() == "true"

# Scraping behavior — conservative for Akamai
REQUEST_DELAY_MIN = 5.0   # seconds between page loads
REQUEST_DELAY_MAX = 12.0  # seconds between page loads
SCROLL_DELAY_MIN = 0.8    # seconds between scroll steps
SCROLL_DELAY_MAX = 2.0
PAGE_LOAD_TIMEOUT = 60000  # ms
MAX_PAGES_PER_SESSION = 30
WARM_UP_SITES = [
    "https://www.google.com",
    "https://www.youtube.com",
    "https://www.amazon.com",
]

# Price filter options (Chewy facet IDs may change, but text labels are stable)
CHEWY_PRICE_FILTERS = [
    "Less than $10",
    "$10 to $20",
    "$20 to $30",
    "$30 to $40",
    "$40 to $50",
    "$50 to $75",
    "$75 to $100",
    "$100 & Above",
]

# Output
OUTPUT_DIR = "output"

# ---------------------------------------------------------------------------
# Phase 4 - Profile Pool & White Screen Recovery
# ---------------------------------------------------------------------------
ADSP_PROFILE_POOL_ENABLED = os.environ.get("ADSP_PROFILE_POOL_ENABLED", "true").lower() == "true"
ADSP_PROFILE_POOL_IDS = [p.strip() for p in os.environ.get("ADSP_PROFILE_POOL_IDS", "k1chlbmn,k1chlbol,k1chlcc3").split(",") if p.strip()]
ADSP_PROFILE_ROTATION_MODE = os.environ.get("ADSP_PROFILE_ROTATION_MODE", "controlled")
ADSP_PROFILE_MAX_ATTEMPTS_PER_ITEM = int(os.environ.get("ADSP_PROFILE_MAX_ATTEMPTS_PER_ITEM", "3"))
ADSP_PROFILE_QUARANTINE_MINUTES = int(os.environ.get("ADSP_PROFILE_QUARANTINE_MINUTES", "30"))
ADSP_PROXY_FAILURES_BEFORE_LOCAL = int(os.environ.get("ADSP_PROXY_FAILURES_BEFORE_LOCAL", "3"))
ADSP_WHITE_SCREEN_RETRY_DELAY_SECONDS = int(os.environ.get("ADSP_WHITE_SCREEN_RETRY_DELAY_SECONDS", "60"))
ADSP_WHITE_SCREEN_DETECTION_ENABLED = os.environ.get("ADSP_WHITE_SCREEN_DETECTION_ENABLED", "true").lower() == "true"
ADSP_SAVE_WHITE_SCREEN_SCREENSHOT = os.environ.get("ADSP_SAVE_WHITE_SCREEN_SCREENSHOT", "true").lower() == "true"
ADSP_SAVE_WHITE_SCREEN_HTML = os.environ.get("ADSP_SAVE_WHITE_SCREEN_HTML", "true").lower() == "true"
ADSP_WHITE_SCREEN_MIN_WAIT_SECONDS = int(os.environ.get("ADSP_WHITE_SCREEN_MIN_WAIT_SECONDS", "30"))
ADSP_WHITE_SCREEN_MAX_WAIT_SECONDS = int(os.environ.get("ADSP_WHITE_SCREEN_MAX_WAIT_SECONDS", "90"))
ADSP_WHITE_SCREEN_POLL_SECONDS = int(os.environ.get("ADSP_WHITE_SCREEN_POLL_SECONDS", "5"))
ADSP_WHITE_SCREEN_REQUIRED_EMPTY_CHECKS = int(os.environ.get("ADSP_WHITE_SCREEN_REQUIRED_EMPTY_CHECKS", "3"))

# ---------------------------------------------------------------------------
# Phase 6 - AdsPower Profile Template Recovery & Controlled Workers
# ---------------------------------------------------------------------------
ADSP_PROFILE_RECOVERY_ENABLED = os.environ.get("ADSP_PROFILE_RECOVERY_ENABLED", "true").lower() == "true"
ADSP_TEMPLATE_PREFIX = os.environ.get("ADSP_TEMPLATE_PREFIX", "CW")
ADSP_WORKER_COUNT = int(os.environ.get("ADSP_WORKER_COUNT", "3"))

ADSP_CW_1_PROXY = os.environ.get("ADSP_CW_1_PROXY", "")
ADSP_CW_2_PROXY = os.environ.get("ADSP_CW_2_PROXY", "")
ADSP_CW_3_PROXY = os.environ.get("ADSP_CW_3_PROXY", "")

ADSP_CW_1_NAME = os.environ.get("ADSP_CW_1_NAME", "CW_1")
ADSP_CW_2_NAME = os.environ.get("ADSP_CW_2_NAME", "CW_2")
ADSP_CW_3_NAME = os.environ.get("ADSP_CW_3_NAME", "CW_3")

# Optional existing AdsPower profile IDs for the fixed slots. If left empty,
# the recovery manager can create a profile for that slot through AdsPower API.
ADSP_CW_1_PROFILE_ID = os.environ.get("ADSP_CW_1_PROFILE_ID", "k1chlcdn")
ADSP_CW_2_PROFILE_ID = os.environ.get("ADSP_CW_2_PROFILE_ID", "k1chlcc3")
ADSP_CW_3_PROFILE_ID = os.environ.get("ADSP_CW_3_PROFILE_ID", "k1chlbol")

ADSP_PROFILE_GROUP_ID = os.environ.get("ADSP_PROFILE_GROUP_ID", "0")
ADSP_AUTO_REBUILD_ON_BLOCKED = os.environ.get("ADSP_AUTO_REBUILD_ON_BLOCKED", "true").lower() == "true"
ADSP_AUTO_RESUME_AFTER_REBUILD = os.environ.get("ADSP_AUTO_RESUME_AFTER_REBUILD", "true").lower() == "true"
ADSP_REBUILD_DELAY_SECONDS = int(os.environ.get("ADSP_REBUILD_DELAY_SECONDS", "30"))
ADSP_MAX_REBUILD_ROUNDS_PER_ITEM = int(os.environ.get("ADSP_MAX_REBUILD_ROUNDS_PER_ITEM", "3"))

# ---------------------------------------------------------------------------
# Phase 5 - Global Product Registry & Dedupe
# ---------------------------------------------------------------------------
CHEWY_GLOBAL_DEDUP_ENABLED = os.environ.get("CHEWY_GLOBAL_DEDUP_ENABLED", "true").lower() == "true"
CHEWY_SKIP_ALREADY_EXTRACTED = os.environ.get("CHEWY_SKIP_ALREADY_EXTRACTED", "true").lower() == "true"
CHEWY_REPROCESS_EXISTING = os.environ.get("CHEWY_REPROCESS_EXISTING", "false").lower() == "true"
CHEWY_DEDUP_BY_PRODUCT_ID = os.environ.get("CHEWY_DEDUP_BY_PRODUCT_ID", "true").lower() == "true"
CHEWY_JSON_CONFIDENCE_THRESHOLD = int(os.environ.get("CHEWY_JSON_CONFIDENCE_THRESHOLD", "70"))
CHEWY_AUTO_EXPORT_ON_JOB_COMPLETE = os.environ.get("CHEWY_AUTO_EXPORT_ON_JOB_COMPLETE", "false").lower() == "true"
CHEWY_REPROCESS_EXISTING = os.environ.get("CHEWY_REPROCESS_EXISTING", "false").lower() == "true"
CHEWY_DEDUP_BY_PRODUCT_ID = os.environ.get("CHEWY_DEDUP_BY_PRODUCT_ID", "true").lower() == "true"


def _env_bool(name: str, default: str) -> bool:
    return os.environ.get(name, default).lower() == "true"


def _env_int(name: str, default: str) -> int:
    return int(os.environ.get(name, default))


def _env_csv(name: str, default: str) -> list[str]:
    return [p.strip() for p in os.environ.get(name, default).split(",") if p.strip()]


def reload_from_env_file(path: str = ".env", *, override: bool = True) -> dict[str, str]:
    """Reload .env-backed runtime settings into this already-imported module."""
    _load_local_env(path, override=override)

    global ADSPOWER_API_BASE, ADSPOWER_PROFILE_ID
    global USE_CHEWY_NEXT_JSON_EXTRACTOR, CHEWY_JSON_FALLBACK_TO_OLD_SCRAPER, CHEWY_JSON_CONFIDENCE_THRESHOLD
    global CATEGORY_DISCOVERY_SAVE_PAGE_DEBUG, CATEGORY_EXCLUDE_SPONSORED_PRODUCTS, CHEWY_JSON_SAVE_GROUPED_OUTPUT
    global OUTPUT_DIR
    global ADSP_PROFILE_POOL_ENABLED, ADSP_PROFILE_POOL_IDS, ADSP_PROFILE_ROTATION_MODE
    global ADSP_PROFILE_MAX_ATTEMPTS_PER_ITEM, ADSP_PROFILE_QUARANTINE_MINUTES, ADSP_PROXY_FAILURES_BEFORE_LOCAL
    global ADSP_WHITE_SCREEN_RETRY_DELAY_SECONDS, ADSP_WHITE_SCREEN_DETECTION_ENABLED, ADSP_SAVE_WHITE_SCREEN_SCREENSHOT
    global ADSP_SAVE_WHITE_SCREEN_HTML, ADSP_WHITE_SCREEN_MIN_WAIT_SECONDS, ADSP_WHITE_SCREEN_MAX_WAIT_SECONDS
    global ADSP_WHITE_SCREEN_POLL_SECONDS, ADSP_WHITE_SCREEN_REQUIRED_EMPTY_CHECKS
    global ADSP_PROFILE_RECOVERY_ENABLED, ADSP_TEMPLATE_PREFIX, ADSP_WORKER_COUNT
    global ADSP_CW_1_PROXY, ADSP_CW_2_PROXY, ADSP_CW_3_PROXY
    global ADSP_CW_1_NAME, ADSP_CW_2_NAME, ADSP_CW_3_NAME
    global ADSP_CW_1_PROFILE_ID, ADSP_CW_2_PROFILE_ID, ADSP_CW_3_PROFILE_ID
    global ADSP_PROFILE_GROUP_ID, ADSP_AUTO_REBUILD_ON_BLOCKED, ADSP_AUTO_RESUME_AFTER_REBUILD
    global ADSP_REBUILD_DELAY_SECONDS, ADSP_MAX_REBUILD_ROUNDS_PER_ITEM
    global CHEWY_GLOBAL_DEDUP_ENABLED, CHEWY_SKIP_ALREADY_EXTRACTED, CHEWY_REPROCESS_EXISTING
    global CHEWY_DEDUP_BY_PRODUCT_ID, CHEWY_AUTO_EXPORT_ON_JOB_COMPLETE

    ADSPOWER_API_BASE = os.environ.get("ADSPOWER_API_BASE", "http://127.0.0.1:50325")
    ADSPOWER_PROFILE_ID = os.environ.get("ADSPOWER_PROFILE_ID", "k143x098")

    USE_CHEWY_NEXT_JSON_EXTRACTOR = _env_bool("USE_CHEWY_NEXT_JSON_EXTRACTOR", "false")
    CHEWY_JSON_FALLBACK_TO_OLD_SCRAPER = _env_bool("CHEWY_JSON_FALLBACK_TO_OLD_SCRAPER", "false")
    CHEWY_JSON_CONFIDENCE_THRESHOLD = _env_int("CHEWY_JSON_CONFIDENCE_THRESHOLD", "70")

    CATEGORY_DISCOVERY_SAVE_PAGE_DEBUG = _env_bool("CATEGORY_DISCOVERY_SAVE_PAGE_DEBUG", "true")
    CATEGORY_EXCLUDE_SPONSORED_PRODUCTS = _env_bool("CATEGORY_EXCLUDE_SPONSORED_PRODUCTS", "true")
    CHEWY_JSON_SAVE_GROUPED_OUTPUT = _env_bool("CHEWY_JSON_SAVE_GROUPED_OUTPUT", "true")
    OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "output")

    ADSP_PROFILE_POOL_ENABLED = _env_bool("ADSP_PROFILE_POOL_ENABLED", "true")
    ADSP_PROFILE_POOL_IDS = _env_csv("ADSP_PROFILE_POOL_IDS", "k143x098,k1cacstm,k1bps235")
    ADSP_PROFILE_ROTATION_MODE = os.environ.get("ADSP_PROFILE_ROTATION_MODE", "controlled")
    ADSP_PROFILE_MAX_ATTEMPTS_PER_ITEM = _env_int("ADSP_PROFILE_MAX_ATTEMPTS_PER_ITEM", "3")
    ADSP_PROFILE_QUARANTINE_MINUTES = _env_int("ADSP_PROFILE_QUARANTINE_MINUTES", "30")
    ADSP_PROXY_FAILURES_BEFORE_LOCAL = _env_int("ADSP_PROXY_FAILURES_BEFORE_LOCAL", "3")
    ADSP_WHITE_SCREEN_RETRY_DELAY_SECONDS = _env_int("ADSP_WHITE_SCREEN_RETRY_DELAY_SECONDS", "60")
    ADSP_WHITE_SCREEN_DETECTION_ENABLED = _env_bool("ADSP_WHITE_SCREEN_DETECTION_ENABLED", "true")
    ADSP_SAVE_WHITE_SCREEN_SCREENSHOT = _env_bool("ADSP_SAVE_WHITE_SCREEN_SCREENSHOT", "true")
    ADSP_SAVE_WHITE_SCREEN_HTML = _env_bool("ADSP_SAVE_WHITE_SCREEN_HTML", "true")
    ADSP_WHITE_SCREEN_MIN_WAIT_SECONDS = _env_int("ADSP_WHITE_SCREEN_MIN_WAIT_SECONDS", "30")
    ADSP_WHITE_SCREEN_MAX_WAIT_SECONDS = _env_int("ADSP_WHITE_SCREEN_MAX_WAIT_SECONDS", "90")
    ADSP_WHITE_SCREEN_POLL_SECONDS = _env_int("ADSP_WHITE_SCREEN_POLL_SECONDS", "5")
    ADSP_WHITE_SCREEN_REQUIRED_EMPTY_CHECKS = _env_int("ADSP_WHITE_SCREEN_REQUIRED_EMPTY_CHECKS", "3")

    ADSP_PROFILE_RECOVERY_ENABLED = _env_bool("ADSP_PROFILE_RECOVERY_ENABLED", "true")
    ADSP_TEMPLATE_PREFIX = os.environ.get("ADSP_TEMPLATE_PREFIX", "CW")
    ADSP_WORKER_COUNT = _env_int("ADSP_WORKER_COUNT", "3")
    ADSP_CW_1_PROXY = os.environ.get("ADSP_CW_1_PROXY", "")
    ADSP_CW_2_PROXY = os.environ.get("ADSP_CW_2_PROXY", "")
    ADSP_CW_3_PROXY = os.environ.get("ADSP_CW_3_PROXY", "")
    ADSP_CW_1_NAME = os.environ.get("ADSP_CW_1_NAME", "CW_1")
    ADSP_CW_2_NAME = os.environ.get("ADSP_CW_2_NAME", "CW_2")
    ADSP_CW_3_NAME = os.environ.get("ADSP_CW_3_NAME", "CW_3")
    ADSP_CW_1_PROFILE_ID = os.environ.get("ADSP_CW_1_PROFILE_ID", "")
    ADSP_CW_2_PROFILE_ID = os.environ.get("ADSP_CW_2_PROFILE_ID", "")
    ADSP_CW_3_PROFILE_ID = os.environ.get("ADSP_CW_3_PROFILE_ID", "")

    ADSP_PROFILE_GROUP_ID = os.environ.get("ADSP_PROFILE_GROUP_ID", "0")
    ADSP_AUTO_REBUILD_ON_BLOCKED = _env_bool("ADSP_AUTO_REBUILD_ON_BLOCKED", "true")
    ADSP_AUTO_RESUME_AFTER_REBUILD = _env_bool("ADSP_AUTO_RESUME_AFTER_REBUILD", "true")
    ADSP_REBUILD_DELAY_SECONDS = _env_int("ADSP_REBUILD_DELAY_SECONDS", "30")
    ADSP_MAX_REBUILD_ROUNDS_PER_ITEM = _env_int("ADSP_MAX_REBUILD_ROUNDS_PER_ITEM", "3")

    CHEWY_GLOBAL_DEDUP_ENABLED = _env_bool("CHEWY_GLOBAL_DEDUP_ENABLED", "true")
    CHEWY_SKIP_ALREADY_EXTRACTED = _env_bool("CHEWY_SKIP_ALREADY_EXTRACTED", "true")
    CHEWY_REPROCESS_EXISTING = _env_bool("CHEWY_REPROCESS_EXISTING", "false")
    CHEWY_DEDUP_BY_PRODUCT_ID = _env_bool("CHEWY_DEDUP_BY_PRODUCT_ID", "true")
    CHEWY_AUTO_EXPORT_ON_JOB_COMPLETE = _env_bool("CHEWY_AUTO_EXPORT_ON_JOB_COMPLETE", "false")

    return {
        "ADSP_CW_1_PROXY": ADSP_CW_1_PROXY,
        "ADSP_CW_2_PROXY": ADSP_CW_2_PROXY,
        "ADSP_CW_3_PROXY": ADSP_CW_3_PROXY,
    }
