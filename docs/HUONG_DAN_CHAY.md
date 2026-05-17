# Hướng Dẫn Sử Dụng Chewy Scraper — CLI tiếng Việt

> Tất cả lệnh chạy trong **PowerShell**, từ thư mục project (`C:\Users\Administrator\Downloads\ChewyPetCo` trên VPS hoặc `C:\Users\admin\Documents\Scraper\Pet` trên máy local).
>
> Đọc kèm `docs/CHEWY_CURRENT_CONTEXT.md` để hiểu kiến trúc.

---

## Mục lục

1. [Sync code mới nhất](#1-sync-code-mới-nhất)
2. [3 cách scrape sản phẩm](#2-3-cách-scrape-sản-phẩm)
3. [Cách 1: 1-step `scrape_category.py` (mới, khuyến nghị)](#3-cách-1-1-step-scrape_categorypy-khuyến-nghị)
4. [Cách 2: Scrape từ file URL có sẵn](#4-cách-2-scrape-từ-file-url-có-sẵn)
5. [Cách 3: Discovery cũ 5 bước (`category_job_runner.py`)](#5-cách-3-discovery-cũ-5-bước-category_job_runnerpy)
6. [Sau khi scrape — build Shopify CSV](#6-sau-khi-scrape--build-shopify-csv)
7. [Resume / Pause / Cancel / Status](#7-resume--pause--cancel--status)
8. [Sửa DB hỏng](#8-sửa-db-hỏng)
9. [URL category Chewy gợi ý](#9-url-category-chewy-gợi-ý)
10. [Troubleshooting nhanh](#10-troubleshooting-nhanh)

---

## 1. Sync code mới nhất

```powershell
cd C:\Users\Administrator\Downloads\ChewyPetCo
git pull origin main
git log --oneline -3        # check commit mới nhất
```

---

## 2. 3 cách scrape sản phẩm

| Cách | Khi nào dùng | Lệnh chính |
|---|---|---|
| **1. 1-step `scrape_category.py`** | Có URL category Chewy, muốn nhanh | `python tools\scrape_category.py "<url>" --workers 3` |
| **2. File URL có sẵn** | Có danh sách URL từ trước (text file) | `python resumable_scraper_runner.py create --urls <file>` + `start` |
| **3. Discovery 5 bước cũ** | Muốn validate kỹ trước khi scrape | `category_job_runner.py create / start / validate / create-pdp-job / ...` |

---

## 3. Cách 1: 1-step `scrape_category.py` (khuyến nghị)

### 3.1. Lệnh đầy đủ

```powershell
# Cơ bản: discover + scrape ngay với 3 worker
python tools\scrape_category.py "https://www.chewy.com/b/dry-food-294" --workers 3

# Có filter giá (USD)
python tools\scrape_category.py "https://www.chewy.com/b/dry-food-294" --workers 3 --price-min 20 --price-max 150

# Giới hạn số page (test với category nhỏ)
python tools\scrape_category.py "https://www.chewy.com/b/dry-food-294" --max-pages 5 --workers 3

# Chỉ discover, KHÔNG scrape — review URL trước
python tools\scrape_category.py "https://www.chewy.com/b/dry-food-294" --dry-run

# Force scrape lại cả sản phẩm đã có trong output/normalized_products/
python tools\scrape_category.py "https://www.chewy.com/b/dry-food-294" --reprocess-existing --workers 3

# Đặt tên job tùy ý (mặc định auto-derive từ URL slug)
python tools\scrape_category.py "https://www.chewy.com/b/dry-food-294" --name "treats-batch-1" --workers 3

# Delay giữa các trang category (mặc định 2s, tăng nếu bị throttle)
python tools\scrape_category.py "https://www.chewy.com/b/dry-food-294" --delay-seconds 5 --workers 3
```

### 3.2. Output

Sau khi chạy, kiểm tra:

```powershell
# Folder URL list + summary
Get-ChildItem output\category_urls\*.urls.txt | Sort-Object LastWriteTime -Descending | Select-Object -First 5
Get-ChildItem output\category_urls\*.summary.json | Sort-Object LastWriteTime -Descending | Select-Object -First 5

# Folder sản phẩm đã scrape
(Get-ChildItem output\normalized_products\chewy_*.json).Count
(Get-ChildItem output\grouped_products\chewy_grouped_by_flavor_*.json).Count
```

### 3.3. Đọc summary.json gần nhất

```powershell
$latest = Get-ChildItem output\category_urls\*.summary.json | Sort-Object LastWriteTime -Descending | Select-Object -First 1
Get-Content $latest.FullName | python -m json.tool
```

---

## 4. Cách 2: Scrape từ file URL có sẵn

Dùng khi có sẵn file URL (từ tay, từ job cũ, hoặc từ `--dry-run` của cách 1).

```powershell
# Bước 1: Tạo job từ file URL
python resumable_scraper_runner.py create --name "my-batch" --urls path\to\urls.txt
# Output: {"job_id": "job_YYYYMMDD_HHMMSS_xxxx", "total_urls": N}
# → Note lại job_id

# Bước 2: Scrape với 3 worker
python resumable_scraper_runner.py start --job-id <job_id> --workers 3
```

**Ví dụ thực tế** — scrape từ file URL của 1 job category cũ:

```powershell
python resumable_scraper_runner.py create `
    --name "food-387-rescrape" `
    --urls output\jobs\catjob_20260516_155848_911d826d\filtered_urls.txt
# → ghi nhận job_id

python resumable_scraper_runner.py start --job-id <job_id> --workers 3
```

---

## 5. Cách 3: Discovery cũ 5 bước (`category_job_runner.py`)

> Em recommend dùng Cách 1 cho 95% trường hợp. Cách 3 chỉ dùng nếu anh cần validation chi tiết trước khi scrape.

### 5.1. Quy trình đầy đủ

```powershell
# Bước 1: Tạo discovery job
python category_job_runner.py create `
    --name "dry-food-batch" `
    --category-url "https://www.chewy.com/b/dry-food-294" `
    --mode hybrid

# Tùy chọn thêm:
#   --price-min 20 --price-max 150
#   --start-page 1
#   --max-pages 50
# Output: Created category job: catjob_YYYYMMDD_HHMMSS_xxxx

# Bước 2: Discovery (single profile, sequential pages)
python category_job_runner.py start --category-job-id <cat_job_id>

# Bước 3: Validate (sinh filtered_urls.txt trong output/jobs/<cat_job_id>/)
python category_job_runner.py validate --category-job-id <cat_job_id>

# Bước 4: Tạo PDP job từ filtered URLs
python category_job_runner.py create-pdp-job --category-job-id <cat_job_id>
# Output: Created PDP job job_YYYYMMDD_HHMMSS_xxxx with N URLs

# Bước 5: Scrape 3 worker
python resumable_scraper_runner.py start --job-id <pdp_job_id> --workers 3
```

### 5.2. Tùy chọn

| Flag | Mô tả |
|---|---|
| `--mode hybrid` (default) | Filter mềm |
| `--mode card_price_prefilter` | Strict — reject ngay nếu price ngoài range |
| `--mode pdp_variant_filter` | Không reject ở discovery, scrape rồi mới filter |
| `--force` (ở create-pdp-job) | Bỏ qua validation score thấp |
| `--reprocess-existing` | Force scrape lại sản phẩm đã có |

### 5.3. Status / Resume

```powershell
# Xem status job discovery
python category_job_runner.py status --category-job-id <cat_job_id>

# Resume nếu bị crash giữa discovery
python category_job_runner.py resume --category-job-id <cat_job_id>
```

### 5.4. File output của discovery cũ

Folder: `output\jobs\<cat_job_id>\`

| File | Nội dung |
|---|---|
| `discovered_urls.txt` | Tất cả URL khám phá (chưa filter giá) |
| **`filtered_urls.txt`** | URL pass filter — dùng cho scrape |
| `category_discovery_report.json` | Report full |
| `category_validation_report.json` | Validation |
| `category_validation_items.csv` | Per-URL với metadata |
| `pages/page_N_summary.json` | Debug per-page |

---

## 6. Sau khi scrape — Build Shopify CSV

```powershell
# Build product CSV + inventory CSV + dedupe log
python tools\build_shopify_csv.py
# Tùy chọn: --status active (mặc định draft), --in-stock-qty 999, --include-blocked

# Build Smart Collections CSV
python tools\build_shopify_collections.py
# Tùy chọn: --min-products 2 (skip category < 2 product)

# Preview HTML để QA trước khi import lên Shopify
python tools\build_shopify_preview_html.py --limit 40
```

### Output

`output\shopify_export\`

| File | Mục đích |
|---|---|
| `shopify_products.csv` | Import sản phẩm lần đầu (Shopify Admin → Products → Import) |
| `shopify_inventory.csv` | Resync stock mỗi 4-5 ngày |
| `shopify_collections.csv` | Smart Collections theo category tag |
| `shopify_collections_summary.txt` | Liệt kê collection + product count |
| `shopify_dedupe_log.json` | Audit dedupe |

`output\preview\`

| File | Mục đích |
|---|---|
| `shopify_split_preview.html` | Mở browser, xem 40 sample product với metafield + variant + category |

---

## 7. Resume / Pause / Cancel / Status

Áp dụng cho PDP scrape job (`resumable_scraper_runner.py`).

```powershell
# Xem trạng thái
python resumable_scraper_runner.py status --job-id <job_id>

# Resume sau khi crash / Ctrl+C / mất điện
python resumable_scraper_runner.py resume --job-id <job_id> --workers 3

# Pause (vẫn giữ state, có thể resume sau)
python resumable_scraper_runner.py pause --job-id <job_id>

# Cancel hẳn job
python resumable_scraper_runner.py cancel --job-id <job_id>

# Retry chỉ những item đã fail
python resumable_scraper_runner.py retry-failed --job-id <job_id>

# Force retry kể cả đã quá max_attempts
python resumable_scraper_runner.py retry-failed --job-id <job_id> --force

# Skip item đang stuck (bị white-screen liên tục chẳng hạn)
python resumable_scraper_runner.py skip-current --job-id <job_id>
```

### Flag hữu ích cho start/resume

```powershell
# Bypass check "đã có 3 file output rồi" → scrape lại từ đầu
python resumable_scraper_runner.py start --job-id <job_id> --workers 3 --reprocess-existing

# Reset toàn bộ profile quarantine (sau khi fix proxy ở .env)
python resumable_scraper_runner.py resume --job-id <job_id> --workers 3 --reset-profile-attempts

# Giới hạn số item scrape (test)
python resumable_scraper_runner.py start --job-id <job_id> --workers 3 --max-items 10
```

---

## 8. Sửa DB hỏng

Khi gặp lỗi `database disk image is malformed`:

```powershell
# Bước 1: Stop mọi process Python
Get-Process python* -ErrorAction SilentlyContinue | Stop-Process -Force

# Bước 2: Backup
Copy-Item scraper_jobs.db "scraper_jobs.db.before_recovery_$(Get-Date -Format yyyyMMdd_HHmmss)"

# Bước 3: Chạy recovery
python tools\db_recover.py --reseed-registry

# Bước 4: Verify
python -c "import job_store; print(job_store.check_db_integrity())"
```

### Phòng tránh tái diễn (chạy 1 lần, PowerShell Admin)

```powershell
# Loại folder + extension khỏi Windows Defender scan
Add-MpPreference -ExclusionPath "C:\Users\Administrator\Downloads\ChewyPetCo"
Add-MpPreference -ExclusionExtension ".db"
Add-MpPreference -ExclusionExtension ".db-wal"
Add-MpPreference -ExclusionExtension ".db-shm"

# Verify
Get-MpPreference | Select-Object -ExpandProperty ExclusionPath
```

### Backup online an toàn (không lock file)

```powershell
# Tốt hơn Copy-Item nhiều — dùng SQLite native backup
$ts = Get-Date -Format yyyyMMdd_HHmmss
New-Item -ItemType Directory -Path backups -Force | Out-Null
python -c "import sqlite3; s=sqlite3.connect('scraper_jobs.db'); d=sqlite3.connect('backups/scraper_jobs.db.$ts'); s.backup(d); s.close(); d.close(); print('backup OK')"

# Auto-rotate, chỉ giữ 14 backup gần nhất
Get-ChildItem backups\scraper_jobs.db.* | Sort-Object LastWriteTime -Descending | Select-Object -Skip 14 | Remove-Item
```

---

## 9. URL category Chewy gợi ý

Truy cập browser xem URL category cụ thể của Chewy rồi dùng. Một số URL phổ biến cho Dog:

| Category | URL |
|---|---|
| Dry Dog Food | `https://www.chewy.com/b/dry-food-294` |
| Wet/Canned Dog Food | `https://www.chewy.com/b/wet-food-2516` |
| Freeze-Dried & Raw Food | `https://www.chewy.com/b/fresh-raw-dog-food-8321` |
| Dog Treats | `https://www.chewy.com/b/treats-315` |
| Dental Treats | `https://www.chewy.com/b/dental-treats-chews-3043` |
| Vitamins & Supplements | `https://www.chewy.com/b/vitamins-supplements-330` |
| Joint Supplements | `https://www.chewy.com/b/hip-joint-care-3023` |
| Toys | `https://www.chewy.com/b/toys-315` |
| Beds | `https://www.chewy.com/b/beds-322` |

> **Cảnh báo:** Chewy đôi khi đổi ID category. Anh nên truy cập trang trên browser xác nhận URL trả về list dog product trước khi chạy.

---

## 10. Troubleshooting nhanh

### 10.1. Discovery dừng giữa chừng (`stopped_reason: white_screen_at_N`)

```powershell
# Profile bị Chewy throttle. Đợi 5-10 phút rồi chạy lại với --max-pages bằng số page đã đạt:
python tools\scrape_category.py "<url>" --max-pages 50 --workers 3
```

### 10.2. Scrape job stuck quá lâu (1 item đã >5 phút)

```powershell
# Skip item hiện tại
python resumable_scraper_runner.py skip-current --job-id <job_id>

# Hoặc check status xem worker nào đang stuck
python resumable_scraper_runner.py status --job-id <job_id>
```

### 10.3. Profile bị white-screen lặp đi lặp lại

```powershell
# Đếm số profile AdsPower hiện có
python -c "import requests, config; r=requests.get(f'{config.ADSPOWER_API_BASE}/api/v1/user/list', params={'page_size':100}, timeout=15).json(); print('total:', len(r['data']['list']))"

# Manual rebuild profile cho 1 slot
python -c "import adsp_profile_recovery_manager as r; print(r.auto_rebuild_profile('CW_1', reason='manual', delay_seconds=0, delete_old_profile=True))"
```

### 10.4. Job báo "0 New URLs to scrape" mặc dù discovery có cards

Nguyên nhân thường gặp:
- Tất cả sản phẩm đã có trong `output/normalized_products/` → dedupe loại hết. Dùng `--reprocess-existing`.
- Filter giá quá hẹp → mở rộng range hoặc bỏ `--price-min/--price-max`.

### 10.5. Resume sau khi máy reboot

```powershell
# Tìm job_id đang dở
python -c "import json, job_store; rows=job_store.list_recent_jobs(10); [print(r['job_id'], r['status'], r['completed_count'],'/',r['total_urls']) for r in rows]"

# Resume
python resumable_scraper_runner.py resume --job-id <job_id> --workers 3
```

---

## Quy trình hoàn chỉnh điển hình

```powershell
# === 1. Sync code ===
cd C:\Users\Administrator\Downloads\ChewyPetCo
git pull origin main

# === 2. Discover + scrape category mới (VD: Dental Treats) ===
python tools\scrape_category.py "https://www.chewy.com/b/dental-treats-chews-3043" --workers 3

# === 3. Đợi job xong... kiểm tra stats ===
(Get-ChildItem output\normalized_products\chewy_*.json).Count

# === 4. Build Shopify export ===
python tools\build_shopify_csv.py
python tools\build_shopify_collections.py

# === 5. Preview QA ===
python tools\build_shopify_preview_html.py --limit 40
# Mở: output\preview\shopify_split_preview.html

# === 6. Import lên Shopify Admin ===
# Settings → Files → Upload shopify_products.csv
# Products → Import → chọn file vừa upload
# Status mặc định là Draft — review xong đổi sang Active
```

---

## Tóm tắt file output

```
output/
├── normalized_products/     # 1 file/pid — raw scrape data
├── grouped_products/        # 1 file/pid — split by flavor, Shopify-shaped
├── validation/              # 1 file/pid — validation block
├── category_urls/           # 1-step scrape (mới)
│   ├── <ts>_<slug>.urls.txt
│   └── <ts>_<slug>.summary.json
├── jobs/                    # discovery cũ (5-bước)
│   └── catjob_<id>/
│       ├── filtered_urls.txt
│       └── ...
├── shopify_export/          # Output cho Shopify
│   ├── shopify_products.csv
│   ├── shopify_inventory.csv
│   ├── shopify_collections.csv
│   └── shopify_dedupe_log.json
├── preview/                 # HTML preview QA
│   └── shopify_split_preview.html
└── cache/                   # next_data cache (auto, KHÔNG xóa)
```

---

**Cập nhật cuối: 2026-05-17**
