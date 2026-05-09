"""
Configuration for Pet Product Scraper (Chewy / Petco).
Update ADSPOWER_PROFILE_ID with your actual AdsPower browser profile ID.
"""

import os
from pathlib import Path


def _load_local_env(path: str = ".env") -> None:
    """Load local .env values without adding a dependency.

    Existing process environment variables win over values in .env.
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
        if key and key not in os.environ:
            os.environ[key] = value


_load_local_env()

# AdsPower Local API
ADSPOWER_API_BASE = os.environ.get("ADSPOWER_API_BASE", "http://127.0.0.1:50325")
ADSPOWER_PROFILE_ID = os.environ.get("ADSPOWER_PROFILE_ID", "k143x098")  # <-- Set your AdsPower profile ID here

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
ADSP_PROFILE_POOL_IDS = [p.strip() for p in os.environ.get("ADSP_PROFILE_POOL_IDS", "k143x098,k1cacstm,k1bps235").split(",") if p.strip()]
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
ADSP_CW_1_PROFILE_ID = os.environ.get("ADSP_CW_1_PROFILE_ID", "")
ADSP_CW_2_PROFILE_ID = os.environ.get("ADSP_CW_2_PROFILE_ID", "")
ADSP_CW_3_PROFILE_ID = os.environ.get("ADSP_CW_3_PROFILE_ID", "")

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
