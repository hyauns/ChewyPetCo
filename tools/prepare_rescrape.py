"""Extract source URLs from normalized products for re-scraping.

Reads every output/normalized_products/chewy_*.json, pulls source_url,
writes a deduped list to tools/urls_all.txt, and writes a deterministic
20-URL random sample (seed=42) to tools/urls_pilot.txt.

Run from the repo root:
    python tools/prepare_rescrape.py
"""

import json
import random
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
NORMALIZED_DIR = BASE_DIR / "output" / "normalized_products"
OUT_DIR = BASE_DIR / "tools"
OUT_ALL = OUT_DIR / "urls_all.txt"
OUT_PILOT = OUT_DIR / "urls_pilot.txt"
PILOT_SIZE = 20
SEED = 42


def main() -> int:
    if not NORMALIZED_DIR.exists():
        print(f"ERROR: {NORMALIZED_DIR} does not exist.", file=sys.stderr)
        return 1

    files = sorted(NORMALIZED_DIR.glob("chewy_*.json"))
    if not files:
        print(f"ERROR: no chewy_*.json files in {NORMALIZED_DIR}", file=sys.stderr)
        return 1

    urls: list[str] = []
    seen: set[str] = set()
    parse_errors = 0
    no_url = 0

    for f in files:
        try:
            with open(f, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, OSError):
            parse_errors += 1
            continue
        url = (data.get("source_url") or "").strip()
        if not url:
            no_url += 1
            continue
        if url in seen:
            continue
        seen.add(url)
        urls.append(url)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(OUT_ALL, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(urls) + "\n")

    rng = random.Random(SEED)
    pilot_size = min(PILOT_SIZE, len(urls))
    pilot = rng.sample(urls, pilot_size)
    with open(OUT_PILOT, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(pilot) + "\n")

    print(f"Scanned files       : {len(files)}")
    print(f"Parse errors        : {parse_errors}")
    print(f"Missing source_url  : {no_url}")
    print(f"Unique URLs         : {len(urls)}")
    print(f"Wrote               : {OUT_ALL} ({len(urls)} lines)")
    print(f"Wrote               : {OUT_PILOT} ({pilot_size} lines, seed={SEED})")
    print()
    print("Pilot URLs:")
    for u in pilot:
        print(f"  {u}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
