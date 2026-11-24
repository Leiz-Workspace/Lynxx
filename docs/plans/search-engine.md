# Plan — Cải thiện search engine của Lynxx

> 2026-11-16 · Trạng thái (cập nhật 2026-11-17): Giai đoạn 1 **đã làm** trong 2.0 r1; Giai đoạn 2 **đã làm** (đợt D, [dev log 7j](../dev_log.md), audit D7; mục 1 thêm lịch tự tải ở 7r), trừ bước chuẩn hoá alias trong bộ lọc nhóm loại trừ cục bộ; Giai đoạn 3: bộ lọc theo model và index các lần tải đã làm (xem từng mục).
> Lynxx không có index tìm kiếm của site: "search engine" là **bộ biên dịch truy vấn + bộ lọc cục bộ** đặt trước search engine của từng website. Index cục bộ duy nhất là index các file đã tải (`lynxx/dlindex.py`). Cải thiện vì vậy tập trung vào bốn hướng:
> - nhập truy vấn dễ hơn;
> - gửi lên server phần chọn lọc nhất của truy vấn;
> - lọc cục bộ phần còn lại;
> - dữ liệu tag luôn mới.

## 1. Hiện trạng sau 2.0 r1 (đã làm)

| Hạng mục | Chi tiết | Nơi trong code |
|---|---|---|
| Autocomplete đúng thứ tự | Sửa lỗi xếp theo loại tag; giờ xếp theo số post. Danh sách khoá được sắp xếp lại khi nạp (file gốc có vài cặp sai thứ tự làm tìm kiếm nhị phân bỏ sót) | `tagsdb.py` |
| Gợi ý ở dạng loại trừ | Gõ `-pre…` sẽ gợi ý `-tag`; Shift+Enter thêm dạng loại trừ | `server.m_tags_autocomplete`, `app.js` |
| Chọn tag A–Z | Chữ cái, `#`, tiền tố, loại tag, số post tối thiểu, sắp theo phổ biến hoặc A→Z, cuộn vô hạn | `TagsDB.browse`, `server.m_tags_browse`, `createAZ()` |
| Chọn phức hợp | Chip có màu theo loại (tag, meta, OR, nhóm loại trừ, phủ định); gộp OR, gộp nhóm loại trừ, đảo, tách, sửa | `app.js` (chips) |
| Giới hạn tag thông minh | Metatag miễn phí không bị tính; tag dương thừa thì gửi **tag hiếm nhất** lên server, phần còn lại lọc cục bộ; tag phủ định thừa thành nhóm loại trừ cục bộ | `task.py` (`FREE_META_PREFIXES`, `LOCAL_POSITIVE_OVERFLOW`, `_pick_local_required_tags`), `download_base._filter_items_missing_required_tags` |
| Lọc ngày phía server | Danbooru chuyển khoảng ngày thành `date:a..b` (không tốn lượt tag), bỏ bước chia đôi tìm trang theo ngày | `download_db._consume_custom_module_tags`, `_supports_native_date_filter` |
| Preview | Trang kết quả có thumbnail, đánh dấu post bị lọc cục bộ, chọn post để tải, lightbox thêm/loại trừ tag | `server.m_search_preview`, `app.js` |
| Dữ liệu tag mới | Updater cho rx, db, xb, bb, en; cron/systemd; cập nhật trong app; chọn file mới nhất cho mỗi module | `tags_updater.py`, `tools/` |
| Đếm nhanh | Danbooru dùng `counts/posts.json` (1 request) | `download_db._get_items_query_size_or_html` |

## 2. Giai đoạn 2 — đề xuất (ưu tiên cao → thấp)

1. **Lịch sử và truy vấn đã lưu**: lưu 50 truy vấn gần nhất theo module, cho phép ghim. Truy vấn đã lưu có thể đặt lịch tải lại định kỳ, chỉ tải post mới nhờ `id:>last_max_id`. Lõi đã có `-get_maxid`. *Đã làm (2026-11-17, dev log 7r): ⟳ trong ngăn "Đã lưu", main chạy khi đến hạn, `-skip_known` + `id:>maxId`.*
2. **Tag alias & implication**: dùng `all_tag_aliases.json` và API alias (Danbooru `tag_aliases.json`) để chuẩn hoá chip; cảnh báo khi tag đã bị đổi tên. Bộ lọc nhóm loại trừ cục bộ hiện **không** áp alias (giới hạn có từ bản gốc) — cần ánh xạ alias trước khi so khớp. *Đã làm phần gợi ý (đợt D): `tags.resolve` cho chip số post, tag lạ và alias của tag đã đổi tên. Chưa làm: chuẩn hoá alias trong bộ lọc nhóm loại trừ cục bộ.*
3. **Gợi ý tag liên quan**: sau khi Preview, đếm tần suất tag trong các post trả về và hiện "Tag hay đi kèm" (top 20) với nút + và −. Không tốn thêm request vì preview đã có tags. *Đã làm (đợt D): `search.related`, nút "Tag liên quan".*
4. **Fuzzy autocomplete**: khi không có kết quả theo tiền tố, thử khớp chuỗi con hoặc khoảng cách sửa ≤ 1 trên các tag phổ biến (top 200k), trigram index được dựng lazy. *Đã làm (đợt D): `tags.fuzzy`, sai tối đa 2 ký tự, ~5 ms trên 766k tag; "Có phải bạn muốn: …".*
5. **Đếm theo từng tag và gợi ý tách truy vấn**: hiện số post của từng chip (lấy từ TagsDB) để người dùng biết tag nào đang thu hẹp kết quả; cảnh báo khi vượt giới hạn độ sâu (Danbooru 1000 trang, Rule34 200k) và đề xuất thêm `id:` để chia đoạn. *Đã làm phần đếm (đợt D): chip hiện số post. Cảnh báo độ sâu không còn cần cho Danbooru / Gelbooru / JSON API nhờ con trỏ id (mục 6).*
6. **Phân trang Danbooru vượt 1000 trang**: dùng `page=b<id>` thay vì số trang (cần một hook phân trang theo con trỏ trong `Downloader._fetch_task_items`). *Đã làm (đợt C, dev log 7h–7i): con trỏ id `_supports_id_cursor` / `_form_cursor_page_address`, lùi về offset khi cần.*
7. **Preview nhiều task**: với truy vấn có nhóm OR được tách task, hiện tab theo từng task hoặc gộp có khử trùng. *Đã làm (đợt D): preview gộp các task OR, bỏ trùng, sắp theo id, `hasMore`.*

## 3. Giai đoạn 3 — dài hạn

- **Bộ lọc nội dung theo model** (xem [gender-filter-model.md](gender-filter-model.md)) cắm vào pipeline như một filter mới. *Đã làm (dev log 7t–7w): `-gender_mode model`, model server.*
- **Index cục bộ các lần tải** — *đã làm phần lõi (2026-11-17, [dev log 7o](../dev_log.md))*:
  - **Đã có:** `lynxx/dlindex.py`, SQLite `<data>/index/downloads.sqlite` (WAL, nhiều job CLI ghi cùng lúc an toàn). Mỗi file tải xong được ghi qua hàng đợi + thread nền theo lô: module, id, md5, đường dẫn, tags, score, rating, ngày post, thời điểm tải. Chỉ metadata, không đọc file. `-skip_known` (mặc định tắt) bỏ qua post trùng module + id hoặc trùng md5 (mọi module) khi file còn trên đĩa. RPC `index.search` (tag / module / khoảng ngày, phân trang) và `index.stats`. App: ô "Bỏ qua file đã tải", số liệu ở Cài đặt.
  - **Để sau:**
    1. *(Đã làm, dev log 7r: trang "Đã tải")* Trang tìm offline đầy đủ trong app: ô tag (dùng lại chip), lọc module / ngày / rating, lưới kết quả từ file trên đĩa (thumbnail cục bộ, không request), mở file / thư mục, đánh dấu file đã mất.
    2. *(Đã làm, 2026-11-17)* Dọn index: xoá dòng có file không còn (chạy nền, theo lô, có nút ở Cài đặt). `DownloadIndex.clean_missing`, RPC `index.clean` (main hỏi xác nhận), CLI `python -m lynxx.dlindex_tools clean`; dòng có cả thư mục đã mất được giữ trừ khi được yêu cầu.
    3. *(Đã làm, 2026-11-17)* Nhập thư viện có sẵn: quét thư mục, nhận id từ tên file (`files.identify`) rồi lấy metadata theo lô, tôn trọng rate limit. Job `dlindex_tools import` trong hàng đợi; Danbooru 100 post / request, site khác 1 post / request, ≥ 0,5 s giữa hai request; tên không nhận ra được liệt kê.
    4. *(Đã làm, dev log 7r)* Preview đánh dấu post đã tải (tra index theo id / md5 của trang kết quả, không thêm request).
    5. *(Đã làm, 2026-11-17)* Tool re-tag (`file_tagger`) đọc tag từ index thay vì file info JSON (JSON làm dự phòng). GUI Tk và CLI `dlindex_tools retag`; app desktop không có công cụ này. Re-tag / un-tag / sắp xếp file cập nhật đường dẫn trong index.
    6. *(Đã làm, 2026-11-17)* Truy vấn đa site (mục dưới) khử trùng bằng md5 trong index.
- **Truy vấn đa site** — *đã làm (2026-11-17)*: một truy vấn chạy trên nhiều module, khử trùng theo md5. App: "Tải thêm từ site khác"; main tạo một job mỗi module, chạy lần lượt, ép `-skip_known` + `-prefix`; hàng đợi gộp thành một nhóm; Preview vẫn một module. Giới hạn: chuỗi tag gửi nguyên văn (cú pháp, alias khác nhau giữa các site, UI cảnh báo); chỉ khử trùng được khi site báo md5 và file không bị nén / sửa lại. Chưa làm: ánh xạ tag / alias giữa các site.

## 4. Rủi ro
- Tag list lớn (db ~0,77 triệu tag, rx ~0,61 triệu) tốn khoảng 150–250 MB RAM mỗi module khi nạp → cân nhắc chuyển sang SQLite FTS cho autocomplete và A–Z.
- Lọc cục bộ làm số "≈ kết quả" lớn hơn số file thực tải → UI đã ghi chú; cần ước lượng tỉ lệ lọc dựa trên preview.
- Gợi ý tag không được làm tăng số request tới website (tôn trọng rate limit).
