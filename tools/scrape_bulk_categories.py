"""One-command wrapper: discover N Chewy categories + merge URLs + create job + start workers.

Replaces the 4-step manual flow with a single command:
    python tools/scrape_bulk_categories.py --workers 3

Default category set is anh's 7-URL Dog batch (2026-05-20). Override with
`--categories-file <path>` (one URL per line) for any custom set.

Pipeline:
  1. For each category URL, run `scrape_category.py <url> --dry-run` — this
     discovers product URLs, dedupes against output/normalized_products/,
     and writes a per-category URL file under output/category_urls/.
  2. Collect URL files created in step 1 (filtered by mtime > script start
     time), concatenate + dedupe across all categories.
  3. Pipe the merged URL list to `resumable_scraper_runner.py create`,
     capturing the new job_id from its JSON output.
  4. Start `resumable_scraper_runner.py start --workers N` and stream its
     output (foreground; Ctrl+C is safe — runner persists state every URL).

Examples:
    # Default 7 Dog categories, 3 workers
    python tools/scrape_bulk_categories.py

    # Custom URL list
    python tools/scrape_bulk_categories.py --categories-file my_urls.txt --workers 3

    # Discovery only (skip scrape phase — useful for previewing URL count)
    python tools/scrape_bulk_categories.py --discovery-only

    # Cap each category at 5 pages (testing)
    python tools/scrape_bulk_categories.py --max-pages 5

    # Re-use existing merged URL file (skip discovery if you already have it)
    python tools/scrape_bulk_categories.py --skip-discovery \\
        --merged-file tools/urls_dog_full_2026-05-20.txt
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass

# Anh's default 7-URL Dog batch (2026-05-20)
DEFAULT_CATEGORIES = [
    "https://www.chewy.com/b/food-332",
    "https://www.chewy.com/b/treats-335",
    "https://www.chewy.com/b/toys-315",
    "https://www.chewy.com/b/dog-potty-cleaning-and-training-supplies-351",
    "https://www.chewy.com/b/dog-grooming-supplies-and-tools-355",
    "https://www.chewy.com/b/dog-288",
    "https://www.chewy.com/b/health-pharmacy-372",
]

CATEGORY_URLS_DIR = BASE_DIR / "output" / "category_urls"
URLS_OUT_DIR = BASE_DIR / "tools"


def load_categories_file(path: Path) -> list[str]:
    urls = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and line.startswith("http"):
            urls.append(line)
    return urls


def run_discovery(urls: list[str], max_pages: int | None) -> float:
    """Run scrape_category.py --dry-run for each URL. Returns start_time (float)."""
    start_time = time.time()
    print(f"\n{'=' * 70}")
    print(f"PHASE 1 — Discovery (--dry-run) on {len(urls)} categories")
    print(f"{'=' * 70}\n")
    for i, url in enumerate(urls, start=1):
        print(f"\n>>> [{i}/{len(urls)}] {url}")
        cmd = [sys.executable, "tools/scrape_category.py", url, "--dry-run"]
        if max_pages is not None:
            cmd += ["--max-pages", str(max_pages)]
        try:
            subprocess.run(cmd, check=False, cwd=BASE_DIR)
        except KeyboardInterrupt:
            print("\n  Interrupted — stopping discovery.")
            raise
        except Exception as e:
            print(f"  WARNING: discovery failed for {url}: {e}")
    return start_time


def merge_url_files(start_time: float, out_file: Path) -> int:
    """Concatenate URL files created during this run, dedupe, write merged file."""
    print(f"\n{'=' * 70}")
    print(f"PHASE 2 — Merge URL files")
    print(f"{'=' * 70}\n")
    if not CATEGORY_URLS_DIR.exists():
        sys.exit(f"ERROR: {CATEGORY_URLS_DIR} not found — discovery may have failed.")

    new_files = []
    for f in CATEGORY_URLS_DIR.glob("*.urls.txt"):
        if f.stat().st_mtime > start_time:
            new_files.append(f)
    new_files.sort()
    print(f"Found {len(new_files)} URL files created in this run:")
    for f in new_files:
        n = sum(1 for _ in f.open("r", encoding="utf-8"))
        print(f"  {f.name}  ({n} URLs)")

    seen: set[str] = set()
    merged: list[str] = []
    for f in new_files:
        for line in f.open("r", encoding="utf-8"):
            url = line.strip()
            if url and url not in seen:
                seen.add(url)
                merged.append(url)

    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text("\n".join(merged) + "\n", encoding="utf-8")
    print(f"\n✅ Merged {len(merged)} unique URLs -> {out_file}")
    return len(merged)


def create_job(merged_file: Path, job_name: str, mode: str) -> str:
    """Call resumable_scraper_runner.py create — parse job_id from its JSON output."""
    print(f"\n{'=' * 70}")
    print(f"PHASE 3 — Create scrape job")
    print(f"{'=' * 70}\n")
    cmd = [
        sys.executable, "resumable_scraper_runner.py", "create",
        "--name", job_name,
        "--urls", str(merged_file),
        "--mode", mode,
    ]
    print(f"$ {' '.join(cmd)}")
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=BASE_DIR)
    if proc.returncode != 0:
        sys.exit(f"ERROR: create job failed — stderr:\n{proc.stderr}\nstdout:\n{proc.stdout}")
    out = proc.stdout
    print(out)

    m = re.search(r'"job_id"\s*:\s*"([^"]+)"', out)
    if not m:
        sys.exit(f"ERROR: could not parse job_id from output:\n{out}")
    job_id = m.group(1)
    print(f"\n✅ Created job: {job_id}")
    return job_id


def start_workers(job_id: str, workers: int) -> int:
    """Foreground exec resumable_scraper_runner.py start. Ctrl+C is safe."""
    print(f"\n{'=' * 70}")
    print(f"PHASE 4 — Start {workers} worker(s) on job {job_id}")
    print(f"{'=' * 70}\n")
    cmd = [
        sys.executable, "resumable_scraper_runner.py", "start",
        "--job-id", job_id,
        "--workers", str(workers),
    ]
    print(f"$ {' '.join(cmd)}")
    print("Streaming runner output. Press Ctrl+C to pause — re-run with --skip-discovery + --resume-job-id to continue.\n")
    return subprocess.call(cmd, cwd=BASE_DIR)


def main():
    ap = argparse.ArgumentParser(formatter_class=argparse.RawDescriptionHelpFormatter, description=__doc__)
    ap.add_argument("--categories-file", help="Path to file with one category URL per line. Defaults to anh's 7-URL Dog batch.")
    ap.add_argument("--workers", type=int, default=3, help="Number of scrape workers (default 3)")
    ap.add_argument("--max-pages", type=int, default=None, help="Cap pages per category (default: no cap)")
    ap.add_argument("--mode", default="json_extractor_with_fallback",
                    choices=["old_scraper", "json_extractor", "json_extractor_with_fallback"],
                    help="Scraper mode passed to runner create (default: json_extractor_with_fallback)")
    ap.add_argument("--job-name", default=None,
                    help="Custom job name (default: auto-generated from date)")
    ap.add_argument("--merged-file", default=None,
                    help="Output path for merged URL file (default: tools/urls_bulk_<YYYY-MM-DD>.txt)")
    ap.add_argument("--skip-discovery", action="store_true",
                    help="Skip Phase 1 — assume merged-file already exists.")
    ap.add_argument("--discovery-only", action="store_true",
                    help="Run Phase 1+2 only (skip job creation + scrape phase).")
    ap.add_argument("--resume-job-id", help="Skip Phases 1-3 and just start workers on this existing job.")
    args = ap.parse_args()

    today = datetime.now().strftime("%Y-%m-%d")
    job_name = args.job_name or f"bulk-cats-{today}"
    merged_file = Path(args.merged_file) if args.merged_file else (URLS_OUT_DIR / f"urls_bulk_{today}.txt")

    # Resume-only path: just start workers on an existing job
    if args.resume_job_id:
        rc = start_workers(args.resume_job_id, args.workers)
        sys.exit(rc)

    # Phase 1 + 2
    if not args.skip_discovery:
        urls = (
            load_categories_file(Path(args.categories_file))
            if args.categories_file
            else DEFAULT_CATEGORIES
        )
        print(f"Categories to discover: {len(urls)}")
        for u in urls:
            print(f"  - {u}")

        start_time = run_discovery(urls, args.max_pages)
        total_urls = merge_url_files(start_time, merged_file)
        if total_urls == 0:
            sys.exit("No URLs discovered — aborting.")
    else:
        if not merged_file.exists():
            sys.exit(f"ERROR: --skip-discovery requires --merged-file, and {merged_file} doesn't exist.")
        total_urls = sum(1 for _ in merged_file.open("r", encoding="utf-8"))
        print(f"Skipping discovery — using existing {merged_file} ({total_urls} URLs)")

    if args.discovery_only:
        print(f"\n--discovery-only: stopping after merge.")
        print(f"   To create + start job manually:")
        print(f"   python tools/scrape_bulk_categories.py --skip-discovery --merged-file {merged_file} --workers {args.workers}")
        return

    # Phase 3 + 4
    job_id = create_job(merged_file, job_name, args.mode)
    rc = start_workers(job_id, args.workers)
    sys.exit(rc)


if __name__ == "__main__":
    main()
