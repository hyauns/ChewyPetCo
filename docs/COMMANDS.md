# Chewy Scraper — Sổ lệnh thường dùng

> Tham chiếu: tất cả lệnh chạy trong PowerShell trên Windows VPS, tại `C:\Users\Administrator\Downloads\ChewyPetCo`.
> Đọc kèm `docs/CHEWY_CURRENT_CONTEXT.md` để hiểu kiến trúc tổng thể.

---

## 1. Sync code mới nhất

```powershell
cd C:\Users\Administrator\Downloads\ChewyPetCo
git pull origin main

# Verify đúng commit
git log --oneline -3
```

---

## 2. Quản lý DB enrichment state

DB nằm ở `scraper_jobs.db` (root). Table chính cho enrich: `chewy_enrichment_state`.

### 2.1. Xem tổng quan trạng thái queue

```powershell
python -c "import job_store; print(job_store.enrichment_state_summary())"
```

Output:
```
{'ok': 120, 'pending': 50, 'in_progress': 2, 'failed': 3, 'skipped': 0}
```

### 2.2. Xoá toàn bộ enrichment state (reset queue về 0)

```powershell
# Cách nhanh nhất — dùng SQL trực tiếp
python -c "import sqlite3; c=sqlite3.connect('scraper_jobs.db'); n=c.execute('DELETE FROM chewy_enrichment_state').rowcount; c.commit(); c.close(); print(f'cleared {n} rows')"

# Verify
python -c "import job_store; print(job_store.enrichment_state_summary())"
# {'ok': 0, 'pending': 0, ...}
```

### 2.3. Xoá state của 1 pid cụ thể

```powershell
python -c "import job_store; job_store.reset_enrichment_state('1003350'); print('done')"
```

### 2.4. Xoá chỉ các row FAILED (giữ ok cho resume)

```powershell
python -c "import sqlite3; c=sqlite3.connect('scraper_jobs.db'); n=c.execute(\"DELETE FROM chewy_enrichment_state WHERE status='failed'\").rowcount; c.commit(); c.close(); print(f'cleared {n} failed rows')"
```

### 2.5. Reset row IN_PROGRESS bị stuck quá 30 phút (orphan từ crash)

```powershell
python -c "import job_store; print('reset', job_store.recover_stale_enrichment_states(30), 'stale rows')"
```

### 2.6. Liệt kê pid đang FAILED + error message

```powershell
python -c "import job_store; [print(s['product_id'], s['error_type'], (s['error_message'] or '')[:80]) for s in job_store.list_enrichment_states(status='failed', limit=20)]"
```

### 2.7. Init lại DB trống hoàn toàn (mất tất cả history)

```powershell
# Backup trước
Copy-Item scraper_jobs.db "scraper_jobs.db.backup_$(Get-Date -Format yyyyMMdd_HHmmss)"

# Xoá DB và re-init schema sạch
Remove-Item scraper_jobs.db, scraper_jobs.db-wal, scraper_jobs.db-shm -ErrorAction SilentlyContinue
python -c "import job_store; job_store.init_db(); print('DB re-initialized')"
```

---

## 3. Chạy enrichment

### 3.1. Test 20 sản phẩm với 3 worker song song (smoke test)

```powershell
# Clear DB trước cho sạch
python -c "import sqlite3; c=sqlite3.connect('scraper_jobs.db'); c.execute('DELETE FROM chewy_enrichment_state'); c.commit(); c.close()"

# Chạy
python chewy_enrich.py --input output/normalized_products --mode all --parallel --workers 3 --limit 20 --force-reenrich
```

### 3.2. Test 100 sản phẩm

```powershell
python chewy_enrich.py --input output/normalized_products --mode all --parallel --workers 3 --limit 100 --force-reenrich
```

### 3.3. Chạy TOÀN BỘ ~3,115 sản phẩm

```powershell
# Lần đầu — clear DB cho sạch
python -c "import sqlite3; c=sqlite3.connect('scraper_jobs.db'); c.execute('DELETE FROM chewy_enrichment_state'); c.commit(); c.close()"

# Chạy không --limit, không --force-reenrich (default behavior: resume nếu có row ok)
python chewy_enrich.py --input output/normalized_products --mode all --parallel --workers 3
```

### 3.4. Resume sau crash / Ctrl+C / mất điện

```powershell
# Chỉ chạy lại lệnh cũ — DB tự skip pid đã 'ok'
python chewy_enrich.py --input output/normalized_products --mode all --parallel --workers 3
```

### 3.5. Force re-enrich tất cả (kể cả pid đã ok)

```powershell
python chewy_enrich.py --input output/normalized_products --mode all --parallel --workers 3 --force-reenrich
```

### 3.6. Chạy với cap retry cao hơn (cho proxy yếu, dễ 429)

```powershell
# Mặc định mỗi pid retry tối đa 5 lần. Tăng lên 10:
python chewy_enrich.py --input output/normalized_products --mode all --parallel --workers 3 --max-attempts 10

# Tắt hoàn toàn cap (cẩn thận — có thể loop):
python chewy_enrich.py --input output/normalized_products --mode all --parallel --workers 3 --max-attempts 0
```

### 3.7. Single-worker mode (debug, không parallel)

```powershell
python chewy_enrich.py --input output/normalized_products --mode all --limit 5
```

### 3.8. Mode khác (chỉ price, chỉ image, chỉ content)

```powershell
# Chỉ enrich content (description/ingredients/feeding)
python chewy_enrich.py --input output/normalized_products --mode content --parallel --workers 3 --limit 50

# Chỉ enrich price
python chewy_enrich.py --input output/normalized_products --mode price --parallel --workers 3 --limit 50

# Chỉ enrich image
python chewy_enrich.py --input output/normalized_products --mode image --parallel --workers 3 --limit 50
```

---

## 4. Kiểm tra kết quả

### 4.1. Đếm số sản phẩm đã enrich xong (trong JSONL)

```powershell
# Tìm file JSONL mới nhất
$jsonl = Get-ChildItem output\enrichment_runs\result_batch_all_*.jsonl | Sort-Object LastWriteTime | Select-Object -Last 1
$jsonl.FullName

# Đếm dòng
(Get-Content $jsonl).Count
```

### 4.2. Inspect 1 sản phẩm xem có đủ field mới không

```powershell
$jsonl = Get-ChildItem output\enrichment_runs\result_batch_all_*.jsonl | Sort-Object LastWriteTime | Select-Object -Last 1
python -c "
import json
with open(r'$($jsonl.FullName)', 'r', encoding='utf-8') as f:
    line = f.readline()
d = json.loads(line)
v = d['products'][0]['variants'][0]
print('pid:', d['source_product_id'])
print('source_entry_id (NOT None means backfill OK):', v.get('source_entry_id'))
print('out_of_stock:', v.get('out_of_stock'))
print('stock_reason:', v.get('stock_reason'))
print('shopify_inventory_policy:', v.get('shopify_inventory_policy'))
print('variant_url (should be /dp/{entry_id}):', v.get('variant_url'))
print('feeding_len:', len(v.get('feeding_instructions') or ''))
print('transition_len:', len(v.get('transition_instructions') or ''))
print('content_source:', v.get('content_source'))
"
```

### 4.3. Gộp tất cả JSONL từ nhiều run thành 1 file (chuẩn bị export Shopify)

```powershell
Get-Content output\enrichment_runs\result_batch_all_*.jsonl | Set-Content all_products.jsonl
(Get-Content all_products.jsonl).Count
# = tổng số dòng sản phẩm đã enrich
```

### 4.4. Đếm sản phẩm OOS (out of stock) trong JSONL mới nhất

```powershell
$jsonl = Get-ChildItem output\enrichment_runs\result_batch_all_*.jsonl | Sort-Object LastWriteTime | Select-Object -Last 1
python -c "
import json
oos_prod, oos_var, total_var = 0, 0, 0
with open(r'$($jsonl.FullName)', 'r', encoding='utf-8') as f:
    for line in f:
        d = json.loads(line)
        for p in d.get('products', []):
            if p.get('out_of_stock'): oos_prod += 1
            for v in p.get('variants', []):
                total_var += 1
                if v.get('out_of_stock'): oos_var += 1
print(f'OOS products: {oos_prod}')
print(f'OOS variants: {oos_var} / {total_var}')
"
```

### 4.5. Xem report tổng kết của run mới nhất

```powershell
$report = Get-ChildItem output\enrichment_runs\report_batch_all_*.json | Sort-Object LastWriteTime | Select-Object -Last 1
Get-Content $report.FullName | python -m json.tool
```

---

## 5. Diagnostic / Troubleshooting

### 5.1. Liệt kê 3 CW slot đang config trong .env

```powershell
python -c "import adsp_profile_recovery_manager as r; print('slots:', r.get_template_slots()); [print(s, '->', r.template_for_slot(s).get('proxy_url_masked')) for s in r.get_template_slots()]"
```

### 5.2. Check AdsPower API có hoạt động không

```powershell
python -c "import adspower; print('connection:', adspower.check_connection())"
```

### 5.3. Start thử 1 profile manual (xem có lỗi gì không)

```powershell
python -c "import adspower; d = adspower.start_profile('REPLACE_WITH_PROFILE_ID'); print(d)"
```

### 5.4. Tail log enrich đang chạy (tab khác)

Không cần — runner in trực tiếp ra stdout. Chạy lệnh enrich trong 1 PowerShell window, mở thêm window khác cho diagnostic.

### 5.5. Đếm số file trong output/normalized_products (input dataset)

```powershell
(Get-ChildItem output\normalized_products\chewy_*.json).Count
# Mong: 3,115 (hoặc tùy số đã scrape)
```

### 5.6. Tìm pid bị stuck FAILED nhiều lần

```powershell
python -c "
import job_store
rows = [s for s in job_store.list_enrichment_states(status='failed') if (s.get('attempt_count') or 0) >= 5]
for s in rows[:20]:
    print(s['product_id'], 'attempts:', s['attempt_count'], 'err:', (s['error_message'] or '')[:60])
print(f'total stuck (attempts>=5): {len(rows)}')
"
```

### 5.7. Tìm pid bị stuck IN_PROGRESS (worker chưa kết thúc / orphan)

```powershell
python -c "import job_store; [print(s['product_id'], 'started:', s['last_started_at'], 'worker:', s.get('worker_id')) for s in job_store.list_enrichment_states(status='in_progress')]"
```

---

## 6. Profile / AdsPower emergency

### 6.1. Liệt kê tất cả profile trong AdsPower (qua API)

```powershell
python -c "
import httpx
r = httpx.post('http://127.0.0.1:50325/api/v1/user/list', json={'page':1,'page_size':50})
print(r.json())
"
```

### 6.2. Xoá thủ công 1 profile orphan

```powershell
python -c "
import adsp_profile_recovery_manager as r
res = r.delete_profile_via_api('REPLACE_WITH_PROFILE_ID')
print('result:', res)
"
```

### 6.3. Tạo profile mới cho 1 slot (qua .env proxy)

```powershell
python -c "
import adsp_profile_recovery_manager as r
template = r.template_for_slot('CW_1')
new_id = r.create_profile_via_api(template)
print('new profile id:', new_id)
print('-> update ADSP_CW_1_PROFILE_ID in .env if anh want to persist')
"
```

### 6.4. Switch 1 profile sang local network (proxy chết tạm thời)

```powershell
python -c "import adsp_profile_recovery_manager as r; r.switch_profile_to_local_via_api('REPLACE_WITH_PROFILE_ID'); print('switched to no_proxy')"
```

### 6.5. Switch 1 profile về dùng .env proxy

```powershell
python -c "
import adsp_profile_recovery_manager as r
pid = 'REPLACE_WITH_PROFILE_ID'
template = r.template_for_slot('CW_1')
r.switch_profile_to_env_proxy_via_api(pid, template)
print('switched back to .env proxy')
"
```

---

## 7. Backup / Restore

### 7.1. Backup DB trước khi làm gì rủi ro

```powershell
Copy-Item scraper_jobs.db "scraper_jobs.db.backup_$(Get-Date -Format yyyyMMdd_HHmmss)"
Get-ChildItem scraper_jobs.db.backup_*
```

### 7.2. Restore DB từ backup gần nhất

```powershell
$latest = Get-ChildItem scraper_jobs.db.backup_* | Sort-Object LastWriteTime | Select-Object -Last 1
Copy-Item $latest.FullName scraper_jobs.db
Write-Host "Restored from $($latest.Name)"
```

### 7.3. Backup JSONL output trước khi clear

```powershell
$ts = Get-Date -Format yyyyMMdd_HHmmss
Move-Item output\enrichment_runs\result_*.jsonl output\enrichment_runs\old_$ts\
Move-Item output\enrichment_runs\report_*.json output\enrichment_runs\old_$ts\
```

---

## 8. Quick reference — flag CLI

```
chewy_enrich.py [-h]
    (--sample FILE | --input DIR)
    [--category {A,B,C}]
    [--mode {content,price,image,all}]   # mặc định 'all'
    [--limit LIMIT]                       # cap số pid, 0 = không cap
    [--output-dir DIR]                    # mặc định output/enrichment_runs/
    [--force-reenrich]                    # xoá row DB của pid input trước khi process
    [--parallel]                          # bật multi-worker
    [--workers N]                         # default 3, max = số slot CW trong .env
    [--max-attempts N]                    # cap retry/pid, default 5, 0 = unlimited
```

Common combos:
```powershell
# Test nhỏ
python chewy_enrich.py --input output/normalized_products --mode all --parallel --workers 3 --limit 20 --force-reenrich

# Full batch resume-safe
python chewy_enrich.py --input output/normalized_products --mode all --parallel --workers 3

# Force re-do mọi thứ
python chewy_enrich.py --input output/normalized_products --mode all --parallel --workers 3 --force-reenrich

# Single-worker debug
python chewy_enrich.py --input output/normalized_products --mode all --limit 1
```

---

## 9. Khi anh thấy log lạ — đối chiếu nhanh

| Log message | Nghĩa | Action |
|---|---|---|
| `[OK] ENRICHED {pid} - N products / M variants` | Sản phẩm enrich xong, ghi vào JSONL | OK, không làm gì |
| `WHITE SCREEN / THROTTLE on {pid} ... deleting profile X` | Profile bị Chewy block, worker đang xoá + tạo mới | Đợi vài giây, worker tự recover |
| `PROXY DEAD on {pid} ... switching profile X to Local` | Proxy chết, worker chuyển profile sang mạng local (không xoá) | Đợi vài giây, worker tự continue |
| `Profile X from .env (slot CW_Y) does NOT exist in AdsPower` | (Đã KHÔNG còn xảy ra từ commit `d8c184f`) Worker đã tự tạo mới | Không cần làm gì |
| `Failed to fetch JSON for X, status: 404` | Sản phẩm Chewy đã xoá / không tồn tại | Bỏ qua, không phải bug |
| `Failed to fetch JSON for X, status: 429` | (Đã KHÔNG còn xảy ra từ commit `fadbcbd`) Nay raise WhiteScreenException → worker rebuild | Tự xử lý |
| `Resume: skipping N products already enriched` | DB có sẵn N pid 'ok', sẽ bỏ qua | Đúng behavior |
| `Recovered N stale in_progress pid(s) from previous run` | Crash trước đó, N pid bị stuck → reset về pending | Tự xử lý |

---

## 10. Pipeline tổng thể (luồng dữ liệu)

```
.env  (profile id + proxy creds)
  |
  v
[chewy_enrich.py --parallel --workers N --input output/normalized_products]
  |
  +-> seed pending rows vào chewy_enrichment_state
  +-> spawn N worker, mỗi worker bind 1 CW slot
  |
  +-> Worker:
  |     |
  |     +-> đọc .env -> profile_id (hoặc tạo mới nếu không có)
  |     +-> mở AdsPower profile -> connect Playwright
  |     |
  |     +-> Loop:
  |     |     +-> claim_next_enrichment_pid (BEGIN IMMEDIATE atomic)
  |     |     +-> process_product(pid):
  |     |     |     +-> get_build_id (load HTML, detect white-screen)
  |     |     |     +-> enrich_variants_from_api (per-variant API fetch)
  |     |     |     +-> split_product_by_flavor (chia thành Shopify product)
  |     |     +-> append JSONL + flush + fsync
  |     |     +-> mark_enrichment_ok
  |     |
  |     +-> Exception handler:
  |           +-> WhiteScreenException -> delete profile + create new + continue
  |           +-> ProxyConnectionError -> switch profile to local + continue
  |
  v
output/enrichment_runs/result_batch_all_{ts}.jsonl
output/enrichment_runs/report_batch_all_{ts}.json
```
