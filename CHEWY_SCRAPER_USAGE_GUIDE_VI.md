# Hướng Dẫn Sử Dụng Chewy Scraper (Phase 3C)

## 1. Tổng quan tool này dùng để làm gì
Tool này được sử dụng để cào (scrape) dữ liệu sản phẩm chi tiết (PDP) từ Chewy.com.
Dựa trên những phát hiện từ Phase 1, Chewy có chứa dữ liệu product/variant rất sạch và đầy đủ trong các object JSON của Next.js (chứa trong `_next/data` hoặc Apollo/Redux state). 
Do đó, chúng ta ưu tiên dùng **Next.js JSON Extractor** vì nó nhanh, sạch và ổn định hơn rất nhiều so với việc cào dữ liệu từ giao diện HTML (DOM scraping).

Hiện tại, Old Scraper (trích xuất bằng Scrapling) vẫn được giữ lại làm phương án dự phòng (fallback). Output hiện tại chưa được đẩy lên Shopify, mà dừng ở mức xuất ra các file JSON đã được làm sạch và chuẩn hóa, sẵn sàng cho khâu tích hợp ở Phase tiếp theo.

## 2. Kiến trúc hiện tại
Flow của hệ thống hiện tại diễn ra như sau:

```text
Chewy PDP URL
→ test_single_product.py
  → Nếu USE_CHEWY_NEXT_JSON_EXTRACTOR=false: chạy old scraper (Scrapling)
  → Nếu USE_CHEWY_NEXT_JSON_EXTRACTOR=true: chạy chewy_next_json_extractor.py
      → Detect Apollo/Redux/page_kind
      → Normalize data (chuẩn hóa dữ liệu thô)
      → Split product by Flavor (chia tách sản phẩm theo Mùi Vị)
      → Validate confidence score (chấm điểm độ tin cậy)
      → Save grouped output (lưu file JSON)
      → Nếu fail: fallback về old scraper (nếu cờ fallback được bật)
```

## 3. Giải thích các Feature Flags
Hệ thống sử dụng các biến môi trường (trong `config.py` hoặc qua terminal) để bật/tắt tính năng.

| Flag | Giá trị | Mặc định | Ý nghĩa | Khi nào dùng |
|------|---------|----------|----------|--------------|
| `USE_CHEWY_NEXT_JSON_EXTRACTOR` | `true`/`false` | `false` | Bật/tắt việc dùng JSON Extractor mới. | Khi muốn trích xuất dữ liệu JSON sạch. |
| `CHEWY_JSON_FALLBACK_TO_OLD_SCRAPER` | `true`/`false` | `false` | Có dùng Old Scraper nếu JSON Extractor thất bại không. | Luôn nên bật để hốt vét data cơ bản khi Next.js data lỗi. |
| `CHEWY_JSON_CONFIDENCE_THRESHOLD` | số (0-100) | `75` | Ngưỡng điểm an toàn để chấp nhận output của JSON. | Giữ mức 75 để đảm bảo chất lượng trước khi đẩy lên Shopify. |
| `CHEWY_JSON_SAVE_GROUPED_OUTPUT` | `true`/`false` | `true` | Có lưu file grouped JSON ra ổ cứng không. | Luôn bật trong Phase này để lấy file phục vụ Phase 4. |

## 4. Cách chạy Old Scraper Mode
Dùng lệnh sau trên PowerShell (Windows):
```powershell
$env:USE_CHEWY_NEXT_JSON_EXTRACTOR="false"
python test_single_product.py "https://www.chewy.com/hills-science-diet-adult-sensitive/dp/3861718"
```
**Giải thích:**
- Dùng khi muốn test lại pipeline cũ hoặc trích xuất list CSV cũ.
- Không yêu cầu JSON extractor can thiệp.
- Output cũ (file `test_chewy_product.json` và `test_chewy_product_shopify.csv`) vẫn giữ nguyên.

## 5. Cách chạy JSON Extractor Mode
Dùng lệnh sau trên PowerShell (Windows):
```powershell
$env:USE_CHEWY_NEXT_JSON_EXTRACTOR="true"
$env:CHEWY_JSON_FALLBACK_TO_OLD_SCRAPER="true"
$env:CHEWY_JSON_CONFIDENCE_THRESHOLD="75"
python test_single_product.py "https://www.chewy.com/hills-science-diet-adult-sensitive/dp/3861718"
```
**Expected Result:**
- JSON Extractor sẽ chạy trước.
- Nếu điểm validation `>= 75`, Old Scraper sẽ bị bypass (bỏ qua hoàn toàn).
- File grouped output hoàn chỉnh được lưu tại: `output/grouped_products/chewy_grouped_by_flavor_<id>.json`.

## 6. Cách đọc output Grouped Product
Đường dẫn: `output/grouped_products/chewy_grouped_by_flavor_<id>.json`

**Cấu trúc chính:**
```json
{
  "source": "chewy",
  "source_product_id": "...",
  "source_url": "...",
  "grouping_strategy": "flavor_as_product_size_as_variant",
  "products": []
}
```
**Giải thích:**
- `products[]`: Là danh sách các sản phẩm Shopify-ready đã được tách theo Flavor.
- Mỗi product tương ứng một Flavor riêng biệt.
- `variants[]` bên trong chỉ còn chứa các option thuộc tính kích cỡ (Size / Weight / Count / Pack).
- Flavor **không** còn nằm trong variant option.
- `images` chứa ảnh tương ứng đúng với Flavor đó.
- `content_sections` là nội dung chi tiết (ingredients, nutrition...) đã được chuẩn hóa.
- `metafields_plan` chứa kế hoạch map dữ liệu sang cấu trúc Metafields của Shopify ở Phase tiếp theo.

## 7. Ví dụ dễ hiểu về Split by Flavor
Giả sử trên Chewy, một PDP có các option sau:
- Chicken / 4-lb
- Chicken / 12-lb
- Salmon / 4-lb
- Salmon / 12-lb

Hệ thống sẽ **tách thành 2 Product riêng biệt** như sau:

**Product 1:**
- Title: `... Chicken Recipe ...`
- Flavor: `Chicken`
- Variants:
  - `4-lb`
  - `12-lb`

**Product 2:**
- Title: `... Salmon Recipe ...`
- Flavor: `Salmon`
- Variants:
  - `4-lb`
  - `12-lb`

**Lý do cần làm vậy:**
- Sản phẩm hiển thị trên Shopify sẽ gọn gàng hơn.
- Tránh việc Variant Matrix bị rối (Ví dụ: gà có size 4lb nhưng cá hồi lại không có, dẫn đến lỗi UX).
- Ảnh sản phẩm hiển thị chính xác theo Flavor.
- Dễ dàng tạo Landing page hoặc Category theo flavor (ví dụ khách hàng muốn tìm toàn bộ thức ăn vị cá hồi).

## 8. Cách đọc Validation Report
Đường dẫn: `output/validation/chewy_validation_<id>.json`

**Giải thích các field:**
- `is_valid`: Boolean xác nhận sản phẩm có pass ngưỡng an toàn không.
- `confidence_score`: Điểm tin cậy (trên thang 100).
- `missing_required_fields`: Các trường bắt buộc bị thiếu (nếu có, sẽ bị trừ điểm nặng).
- `missing_preferred_fields`: Các trường nên có nhưng thiếu (bị trừ điểm nhẹ).
- `warnings`: Các cảnh báo về cấu trúc hoặc nội dung (ví dụ: variant dính Flavor).

**Quy tắc:**
- `confidence_score >= 75`: Có thể tin tưởng và dùng output để đẩy Shopify.
- Nếu dưới 75: Tool sẽ báo lỗi, nên kiểm tra file diagnostic hoặc fallback về Old Scraper.
- Thiếu `nutrition` không phải lúc nào cũng là lỗi (nhiều sản phẩm trên web không cung cấp mục này).
- Đồ chơi/phụ kiện (Non-food product) sẽ không có `ingredients`, `feeding_instructions`, hay `guaranteed_analysis` nên không sao.

## 9. Cách xử lý URL bị redirect PLP hoặc fail
**Giải thích:** 
Một số URL Chewy cũ khi bị hết hàng hoặc xóa sẽ tự động redirect về trang danh mục (Category/PLP). Tool của chúng ta rất thông minh, sẽ tự detect được `page_kind = redirected_plp` và **không tạo ra sản phẩm rác**.
Nếu JSON Extractor fail và cờ fallback đang bật, Old Scraper sẽ khởi động để cố gắng cào những text cơ bản nhất.

**Checklist xử lý lỗi:**
- [ ] Kiểm tra `final_url` xem có bị redirect không.
- [ ] Kiểm tra `page_kind`.
- [ ] Xem xét `confidence_score`.
- [ ] Kiểm tra nội dung Diagnostic file (lưu tại `output/json_extractor_failures/`).
- [ ] Nếu URL đích thực sự là trang PLP, tuyệt đối không dùng output đó để import vào Shopify.

## 10. Cách chạy Batch Test nhiều URL
Lệnh PowerShell:
```powershell
python test_chewy_json_extractor_batch.py urls.txt
```
**Giải thích:**
- File `urls.txt`: Bạn tự tạo, mỗi dòng chứa một Chewy PDP URL. Hệ thống sẽ không tự động đi crawl list category mà chỉ test đúng các URL được cung cấp.

**File Output sau khi chạy Batch Test:**
- `output/chewy_phase3_batch_report.json`
- `output/chewy_phase3D_fix_batch_report.json`

**Giải thích các chỉ số trong Batch Report:**
- `total_urls`: Số lượng URL đã quét.
- `success_count` / `fail_count`: Số lượng lấy JSON thành công/thất bại.
- `pdp_count`: Số lượng là trang chi tiết (PDP) thực sự.
- `redirected_pdp_count` / `redirected_plp_count`: Số link bị đổi hướng.
- `apollo_count` / `redux_count`: Số lượng trang web Chewy sử dụng kiến trúc Apollo/Redux.
- `average_confidence_score`: Điểm tin cậy trung bình của cả batch.
- `products_generated`: Tổng số product sinh ra (đã nhân lên sau khi tách Flavor).
- `products_with_flavor_specific_images`: Các product bắt được chính xác bộ ảnh theo từng vị.
- `products_missing_feeding_instructions`: Các product thiếu bảng hướng dẫn cho ăn.

## 11. Cách kiểm tra Product đã sẵn sàng cho Shopify chưa
Một product được coi là "Shopify-Ready" (sẵn sàng chuyển sang Phase 4) nếu đạt toàn bộ các điều kiện sau:
- [ ] Điểm `validation confidence_score` >= 75.
- [ ] File Grouped Output có mảng `products[]` hợp lệ.
- [ ] Mỗi product trong mảng có ít nhất 1 variant.
- [ ] Product `title` **không** còn dính các đuôi size như `", 12-lb bag"`.
- [ ] `Flavor` nằm ở cấp độ Product (Product-level).
- [ ] Variant option chỉ là Size / Weight / Count / Pack, không phải Flavor.
- [ ] Mảng `images` không bị lẫn lộn giữa các Flavor.
- [ ] Mảng `content_sections` tồn tại.
- [ ] `guaranteed_analysis` (nếu có) phải là các rows sạch, không còn chứa ký tự `|` markdown.
- [ ] `feeding_instructions` (nếu có) phải được đẩy vào mảng `tables[]` hoàn chỉnh.
- [ ] `product_facts.primary_flavor` không bị Null nếu group đó có flavor thực sự.
- [ ] `metafields_plan` chứa `custom.source_flavor` và `custom.primary_flavor`.

## 12. Các lỗi thường gặp và cách xử lý

| Lỗi / Dấu hiệu | Nguyên nhân | Cách xử lý |
|----------------|-------------|------------|
| `confidence_score` thấp | Thiếu quá nhiều trường quan trọng hoặc cấu trúc JSON mới. | Check file diagnostic. Có thể cấu trúc web thực tế bị thiếu nội dung, hoặc cần dùng fallback old scraper. |
| page_kind là `redirected_plp` | Sản phẩm cũ đã bị xóa, đẩy về Category. | Bỏ qua link này, không cần import Shopify. |
| `grouped products` rỗng | Không map được variants hoặc kiến trúc bị lỗi. | Cần kiểm tra trong repo file diagnostic. Dùng old scraper backup. |
| Title vẫn còn size suffix | Cụm regex làm sạch title chưa phát hiện ra suffix lạ. | Báo lại dev để cập nhật từ khóa trong hàm title_cleanup. |
| `product_facts.primary_flavor` bị `null` | Sản phẩm không có fact này. | Đã được fix ở Phase 3D-Fix bằng cơ chế fallback tự gán. |
| `guaranteed_analysis` còn dính dấu pipe (`\|`) | Table markdown quá dị biệt. | Đã được giải quyết ở Phase 3D-Fix. Nếu xuất hiện lại, cần dev điều chỉnh table parser. |
| `feeding_instructions` có table nhưng `tables[]` rỗng | Web không vẽ table chuẩn mà dùng thẻ text đặc biệt. | Mở file json xem `summary` có raw text để tự chỉnh tay hoặc báo dev bổ sung regex. |
| `images` bị null/rỗng | Lỗi load object từ _next/data. | Để Old Scraper tự động fallback hốt lại ảnh. |
| Old scraper không chạy fallback | Flag `CHEWY_JSON_FALLBACK_TO_OLD_SCRAPER` tắt. | Hãy đảm bảo cờ này được set thành `true`. |
| Env Flag không ăn trên Windows PowerShell | Lỗi syntax dòng lệnh. | Nhớ dùng cú pháp `$env:TEN_BIEN="giá trị"`. |

## 13. Những điều KHÔNG được làm ở giai đoạn này
- **Chưa đẩy dữ liệu lên Shopify:** Output mới dừng lại ở JSON local, KHÔNG gọi Shopify Admin API.
- **Chưa write dữ liệu vào Production DB.**
- **Chưa Publish sản phẩm thật.**
- **Không tự động crawl Category hàng loạt** nếu chưa có danh sách URL PDP cụ thể. Hãy dùng `scrape_chewy.py` để trích xuất file CSV danh sách URL trước.
- **Không dùng proxy luân phiên/captcha bypass/stealth plugin** ở bước này, vì tool chạy thông qua session AdsPower đã được làm ấm (warm-up).
- **Không sửa output format của Old Scraper** khi feature flag OFF, để bảo toàn tính toàn vẹn của hệ thống cũ.

## 14. Workflow khuyến nghị cho anh
Để vận hành tool hiệu quả, anh có thể theo các bước sau:

1. **Bước 1:** Chuẩn bị danh sách các URL cần cào và cho vào file `urls.txt`.
2. **Bước 2:** Chạy lệnh batch test: `python test_chewy_json_extractor_batch.py urls.txt`.
3. **Bước 3:** Kiểm tra file report `output/chewy_phase3D_fix_batch_report.json`. Lọc ra những URL thành công (`success`) có `confidence_score >= 75`.
4. **Bước 4:** Mở thử vài file JSON ngẫu nhiên trong thư mục `output/grouped_products/` để tự xác minh cấu trúc chia flavor và nội dung.
5. **Bước 5:** Khi dữ liệu ổn định, chúng ta sẽ chuyển dữ liệu này sang Mapper của Phase 4.

## 15. Phase tiếp theo là gì? (Phase 4)
Ở Phase 4 sắp tới, tool sẽ **KHÔNG scrape dữ liệu nữa**.
Mọi dữ liệu thô đã nằm sẵn trong `output/grouped_products/chewy_grouped_by_flavor_<id>.json`.

Nhiệm vụ của Phase 4 là đọc các JSON này và chuẩn bị:
- Tạo Shopify-ready product payload (GraphQL hoặc REST API object).
- Tạo Metafields payload từ `metafields_plan` và `content_sections`.
- Lập kế hoạch upload hình ảnh (image/media mapping plan) để gán đúng hình cho đúng Variant.
- Xây dựng GraphQL **dry-run payload** để mô phỏng và bắt lỗi API trước khi thực thi việc push thật lên Shopify.
