"""
Configuration for Pet Product Scraper (Chewy / Petco).
Update ADSPOWER_PROFILE_ID with your actual AdsPower browser profile ID.
"""

import os

# AdsPower Local API
ADSPOWER_API_BASE = "http://127.0.0.1:50325"
ADSPOWER_PROFILE_ID = os.environ.get("ADSPOWER_PROFILE_ID", "k143x098")  # <-- Set your AdsPower profile ID here

# ---------------------------------------------------------------------------
# Feature Flags - Phase 3C
# ---------------------------------------------------------------------------
USE_CHEWY_NEXT_JSON_EXTRACTOR = os.environ.get("USE_CHEWY_NEXT_JSON_EXTRACTOR", "false").lower() == "true"
CHEWY_JSON_FALLBACK_TO_OLD_SCRAPER = os.environ.get("CHEWY_JSON_FALLBACK_TO_OLD_SCRAPER", "false").lower() == "true"
CHEWY_JSON_CONFIDENCE_THRESHOLD = int(os.environ.get("CHEWY_JSON_CONFIDENCE_THRESHOLD", "75"))
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
ADSP_PROFILE_POOL_IDS = [p.strip() for p in os.environ.get("ADSP_PROFILE_POOL_IDS", "k143x098,k1bpayje,k136h396").split(",") if p.strip()]
ADSP_PROFILE_ROTATION_MODE = os.environ.get("ADSP_PROFILE_ROTATION_MODE", "controlled")
ADSP_PROFILE_MAX_ATTEMPTS_PER_ITEM = int(os.environ.get("ADSP_PROFILE_MAX_ATTEMPTS_PER_ITEM", "3"))
ADSP_PROFILE_QUARANTINE_MINUTES = int(os.environ.get("ADSP_PROFILE_QUARANTINE_MINUTES", "30"))
ADSP_WHITE_SCREEN_RETRY_DELAY_SECONDS = int(os.environ.get("ADSP_WHITE_SCREEN_RETRY_DELAY_SECONDS", "60"))
ADSP_WHITE_SCREEN_DETECTION_ENABLED = os.environ.get("ADSP_WHITE_SCREEN_DETECTION_ENABLED", "true").lower() == "true"
ADSP_SAVE_WHITE_SCREEN_SCREENSHOT = os.environ.get("ADSP_SAVE_WHITE_SCREEN_SCREENSHOT", "true").lower() == "true"
ADSP_SAVE_WHITE_SCREEN_HTML = os.environ.get("ADSP_SAVE_WHITE_SCREEN_HTML", "true").lower() == "true"
