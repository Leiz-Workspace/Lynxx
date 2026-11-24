# Lynxx — Acceptance Criteria (AC)

> 2.0 r1 · 2026-11-16 · Định dạng Given/When/Then.
> Cột **Kiểm chứng**:
> - `T:` test tự động (`lynxx/tests/test_lynxx.py` hoặc `tests.py`);
> - `E2E:` chế độ tự kiểm thử Electron (`LYNXX_SELFTEST_DIR`);
> - `M:` kiểm tra thủ công.

| AC | Story | Tiêu chí | Kiểm chứng |
|---|---|---|---|
| AC-01 | US-01 | **Given** danh sách tag rx đã nạp **When** gõ `big` **Then** gợi ý đầu tiên là tag có nhiều post nhất (`big_breasts` 3,75 triệu), không phải tag metadata ít post; tối đa 12 gợi ý; Tab thêm chip | T: `TagsDBTests.test_autocomplete_ranks_by_posts_count`; M |
| AC-02 | US-02 | **Given** module rx **When** chọn chữ `b`, sắp xếp Phổ biến **Then** danh sách bắt đầu bằng `breasts`, `blush`, `big_breasts`; lọc loại tag và số post tối thiểu thu hẹp kết quả; cuộn xuống tải thêm 150 tag | T: `test_browse_prefix_and_filters`; E2E `04-az-drawer` |
| AC-03 | US-03 | **Given** các chip `sky`, `cloud` đã chọn **When** bấm Gộp OR **Then** truy vấn chứa `(sky~cloud)`; Tách nhóm trả lại 2 chip; Nhóm loại trừ tạo `-(a,b)`; ± đảo `tag` ↔ `-tag` | M (mock UI): truy vấn `scenery cat_ears (sky~cloud) -touhou rating:g` |
| AC-04 | US-04 | **Given** truy vấn hợp lệ **When** bấm Đếm **Then** hiện "≈ N kết quả" trong vòng 10 s; lỗi API được hiện bằng toast | E2E: `≈ 37.542 kết quả` |
| AC-05 | US-05 | **Given** Preview đã có kết quả **When** chọn 2 post và bấm Tải đã chọn **Then** một job được tạo, với tag `id:a,b` (db) hoặc `(id:a~id:b)` (module khác) | M (mock); E2E preview 200 post |
| AC-06 | US-06 | **Given** lightbox đang mở **When** bấm + hoặc − cạnh một tag **Then** chip tương ứng (bao gồm hoặc loại trừ) được thêm, không trùng lặp | M |
| AC-07 | US-07 | **Given** job đang chạy **When** bấm Huỷ **Then** trạng thái chuyển sang Đang huỷ, rồi Đã huỷ; file tải dở bị xoá; tiến trình bị kill nếu sau 15 s chưa thoát | M |
| AC-08 | US-08 | **Given** cài đặt mới **When** mở menu module **Then** chỉ có Rule34 và Danbooru (mặc định); trang Extensions liệt kê 6 site khác ở trạng thái chưa cài; builtin không gỡ được | T: `test_catalog_lists_optional_sites`, `test_builtin_cannot_be_replaced_or_removed`; E2E `05-extensions` |
| AC-09 | US-09 | **Given** module db **When** tìm `a b c d rating:g` **Then** server chỉ nhận 2 tag dương cùng metatag miễn phí; `c`, `d` được kiểm tra cục bộ; `a b -c` chuyển `-c` thành nhóm loại trừ cục bộ; không có `TagLimitError` | T: `DanbooruTagLimitTests` (4 test); E2E: "76 bị lọc cục bộ · tag kiểm tra cục bộ: cloud" |
| AC-10 | US-10 | **Given** nền chế độ Icon **Then** icon đang chọn hiển thị phía sau, được làm mờ theo thanh trượt; **When** chọn Ảnh tuỳ chọn **Then** ảnh được copy vào userData, hiển thị **không** làm mờ, và vẫn còn sau khi khởi động lại | E2E `06/07`; M |
| AC-11 | US-11 | **Given** phiên Wayland **When** đổi icon **Then** logo và nền đổi ngay, hiện thông báo kèm nút Cập nhật launcher và Khởi động lại; trên X11/Windows/macOS icon cửa sổ/dock đổi ngay | M |
| AC-12 | US-12 | **Given** bật Làm mờ thumbnail **Then** thumbnail bị làm mờ cho tới khi rê chuột vào | M |
| AC-13 | US-13 | **Given** proxy `socks5h://127.0.0.1:1080` **Then** được chấp nhận; host là tên miền hoặc `[::1]` hợp lệ; `ftp://` bị từ chối. **Given** bật "Tải file không qua proxy" **Then** session tải file không dùng proxy, còn session API vẫn dùng | T: `ValidatorsTests.test_proxy`, `NetworkHelpersTests.test_download_without_proxy` |
| AC-14 | US-14 | **Given** đã lưu API key **Then** settings.json chỉ chứa bản mã hoá (khi có keyring); renderer không nhận được key; log hiển thị `api_key=<REDACTED>`, `login=<REDACTED>` | M; code: `logger.trace`, `main.js` |
| AC-15 | US-15 | **Given** cron đã cài **When** tới lịch **Then** `lynxx-update-tags.sh` cập nhật `rx`, `db` vào `2tags/` (atomic), ghi log, không chạy chồng (flock); lần lấy một phần hoặc < 50% sẽ không ghi đè | M: chạy thật db → 1.109.118 tag (lọc còn 766.918 với ≥ 2 post) |
| AC-16 | US-16 | **Given** response 403/503 có `cf-mitigated: challenge` **Then** không retry; trả lỗi kèm hướng dẫn; job/preview đánh dấu `cfChallenge` | T: `test_cloudflare_challenge` |
| AC-17 | US-17 | **Given** proxy không kết nối được **Then** dừng sau `retries × 3` lỗi proxy/SSL thay vì lặp vô hạn | code: `network.wrap_request`, `download_file` |
| AC-18 | US-18 | **Given** một thư mục có manifest hợp lệ và downloader.py **When** cài đặt **Then** module nhận id ≥ 100, có trong `DOWNLOADERS_BY_PROC_MODULE` và các bảng cú pháp; tạo được downloader; gỡ được | T: `test_install_dir_registers_module`, `ManifestTests` |
| AC-19 | US-19 | **Given** file zip **When** SHA-256 sai **Then** từ chối; **When** zip chứa đường dẫn `../` **Then** từ chối; **When** đúng **Then** cài thành công | T: `test_archive_checksum_and_zip_slip` |
| AC-21 | US-21 | Bộ luật tag (AC-G01–G03 trong plan): `1girl 1boy hetero` → both/high; rx `1girls female_only` → only_female/high, `1girls solo` → medium; e621 `male solo anthro` → only_male/high + anthro; futanari được giữ ở only_female, bị loại khi strict; `trap 1boy` → only_male | T: `GenderRulesTests` (bảng 86 ca) |
| AC-22 | US-21 | **Given** `-gender only_female` **Then** post khác nhãn bị loại, có trong `filtered_out_ids_cache`, trace đếm theo nhãn; task parent không bị lọc giới tính. **Given** không có `-gender` **Then** kết quả giống hệt trước (AC-G04, AC-G05) | T: `GenderPipelineTests` |
| AC-23 | US-21 | Danh sách chặn (AC-S01–S03): mọi tag trong danh sách bị chặn (không phân biệt hoa thường, theo token); preview có `-safety_blacklist` không trả post bị chặn, kể cả URL thumbnail, chỉ tăng `blockedCount`; danh sách chặn chỉ chạy với `-safety_blacklist`, bộ lọc giới tính một mình không bật nó, và nó chạy được khi không lọc giới tính (quyết định 2026-11-18, thay quyết định 2026-11-16); chốt an toàn của model theo cùng tuỳ chọn; extension chỉ thêm được tag | T: `SafetyTests`, `test_safety_blacklist_is_its_own_option`, `test_preview_never_returns_blocked_posts`, `test_model_in_the_download_filter`, `ExtensionGenderManifestTests` |
| AC-24 | US-21 | Độ phủ (≥ medium) tầng 0 (AC-G07): db ≥ 90 %, rx ≥ 75 % | Đo 2026-11-16: db 95,9 % (1000 post mới nhất), 99,5 % (200 ngẫu nhiên); rx 85,8 % (1000 mới nhất), 94,6 % (1000 ngẫu nhiên): `docs/reports/gender-coverage-*.json`. E2E: preview `smile rating:g` + Chỉ nữ: 193 post · 49 loại · 5 không rõ · 7 bị chặn |
| AC-20 | — | Hồi quy: các test offline gốc của bản gốc đều pass sau refactor (DataStructureIntegrity, FileCheck, ArgParse, TagParse, Logger, DownloaderBase, ItemFilter) | T: 33 test pass |
