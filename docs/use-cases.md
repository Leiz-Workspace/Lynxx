# Lynxx — Use Cases (UC)

> 2.0 r1 · 2026-11-16 · Tác nhân: **ND** = người dùng, **Hệ thống** = Electron main + sidecar + CLI.

## UC-01 Nhập tag với autocomplete
- **Tiền điều kiện:** sidecar sẵn sàng; module đang chọn có danh sách tag.
- **Luồng chính:**
  1. ND gõ ít nhất 2 ký tự vào ô tag.
  2. Hệ thống gọi `tags.autocomplete` và hiện tối đa 12 gợi ý, xếp theo số post, có chấm màu theo loại tag.
  3. ND chọn bằng ↑/↓ rồi Tab/Enter, hoặc bấm chuột. Hệ thống thêm chip.
  4. Hệ thống kiểm tra cú pháp (`tags.validate`) và hiển thị truy vấn đầy đủ dưới thanh tìm kiếm.
- **Luồng phụ:**
  - 1a. Gõ `-` ở đầu → gợi ý được chèn ở dạng loại trừ.
  - 3a. Shift+Enter → thêm dạng loại trừ.
  - 2a. Không có gợi ý → hiện gợi ý cập nhật danh sách tag.
  - 4a. Cú pháp sai → viền đỏ kèm hướng dẫn dùng `%tag%`.

## UC-02 Chọn tag theo A–Z
1. ND bấm **A–Z** trên thanh tìm kiếm, hoặc mở trang **Tags A–Z**.
2. ND chọn chữ cái hoặc `#`; tuỳ chọn thêm: tiền tố, loại tag, số post tối thiểu, cách sắp xếp.
3. Hệ thống gọi `tags.browse` và hiện 150 tag mỗi lần, cuộn xuống để tải thêm.
4. ND bấm **+** để thêm hoặc **−** để loại trừ. Chip xuất hiện ngay trong truy vấn.

## UC-03 Gộp tag phức hợp
1. ND bấm để chọn nhiều chip (chip được chọn có viền màu accent).
2. Thanh hành động hiện các lựa chọn: **Gộp OR**, **Nhóm loại trừ**, **Đảo bao gồm/loại trừ**, **Tách nhóm**, **Xoá**, **Bỏ chọn**.
3. Hệ thống thay các chip đã chọn bằng chip nhóm `(a~b)` hoặc `-(a,b)`, hoặc tách nhóm trở lại thành chip đơn.
- Bấm đúp vào chip → đưa chip về ô nhập để sửa.

## UC-04 Đếm kết quả
1. ND bấm **Đếm**.
2. Hệ thống gọi `search.count` (kèm proxy, API key, ngày) và hiện "≈ N kết quả". Nếu có tag được kiểm tra cục bộ, hệ thống ghi chú rằng số này là trước khi lọc cục bộ.

## UC-05 Preview và chọn post
1. ND bấm **Preview** (hoặc Enter khi ô nhập trống).
2. Hệ thống gọi `search.preview` với trang hiện tại: thumbnail tải qua `lynxx-thumb://` (có cache), post bị lọc cục bộ được ẩn (bật "Hiện post bị lọc" để xem).
3. ND bấm để chọn post; dùng Chọn hết, Bỏ chọn, Đảo; chuyển trang bằng ‹ ›.
4. ND bấm đúp → lightbox hiện ảnh sample và danh sách tag, có nút + và − cho từng tag, và liên kết mở post trên website.
5. ND bấm **Tải đã chọn (N)** → Hệ thống tạo job với `id:1,2,3` (Danbooru) hoặc `(id:1~id:2…)` (module khác, tối đa 200). Job tải đúng các post đã chọn: số post tối đa, khoảng ngày và bộ lọc giới tính của bảng bộ lọc không áp dụng (loại media và tuỳ chọn file vẫn áp dụng).
- **Ngoại lệ:** truy vấn, khoảng ngày hay bộ lọc đã đổi sau lần Preview, hoặc Preview vừa lỗi → lưới bị làm mờ kèm ghi chú cho tới lần Preview sau. Site không tìm theo ngày (như Rule34; Danbooru thì tìm được): Preview chỉ ẩn post ngoài khoảng ngày trên trang đang xem, Đếm ghi "chưa trừ post ngoài khoảng ngày".

## UC-06 Tải xuống
1. ND bấm **Tải xuống** (hoặc Ctrl+Enter). Nếu đang chọn post trên Preview còn khớp truy vấn và bộ lọc, Hệ thống hỏi: **Tải N post đã chọn** (như UC-05 bước 5) hay **Tải cả truy vấn**.
2. Nếu bật cảnh báo và thư mục đích không trống, Hệ thống hỏi xác nhận.
3. Hệ thống đưa job vào hàng đợi (tối đa `maxConcurrentJobs` job chạy cùng lúc) và spawn CLI ở chế độ IPC.
4. Trang **Tải xuống** cập nhật trạng thái, thanh tiến độ, ✓/✗, log.
5. ND có thể **Huỷ** (gửi `cancel`), **Mở thư mục**, **Xoá job đã xong**.
- **Ngoại lệ:**
  - website trả bot challenge → job dừng, hiển thị "Bị bot challenge chặn";
  - lỗi proxy/SSL vượt ngưỡng → dừng kèm thông báo.

## UC-07 Quản lý extension
1. ND mở **Extensions**: thẻ builtin (Luôn bật), đã cài (Gỡ), chưa cài (Cài đặt).
2. **Cài đặt** từ catalog cục bộ → copy vào `<data>/extensions/<id>` → module xuất hiện trong menu module.
3. **Cài từ file .zip** → ND nhập SHA-256 → Hệ thống kiểm checksum và zip-slip rồi mới cài.
4. **Catalog từ xa** → ND nhập URL index.json → danh sách → **Cài** (URL + SHA-256).
5. **Gỡ** → xoá thư mục; code đã import vẫn còn tới khi khởi động lại.

## UC-08 Tuỳ biến giao diện
1. ND mở **Cài đặt → Giao diện**.
2. Chọn theme → áp dụng ngay.
3. Chọn icon → logo, nền và icon cửa sổ đổi ngay. Trên Wayland, Hệ thống hiện thông báo kèm nút **Cập nhật launcher** và **Khởi động lại**.
4. Chọn nền:
   - **Icon (làm mờ)**: chỉnh độ mờ;
   - **Ảnh tuỳ chọn**: chọn file, app copy vào userData, ảnh không bị làm mờ;
   - **Không**.
   Với mọi chế độ, có thể chỉnh độ hiện của nền và lớp phủ tối.

## UC-09 Cấu hình kết nối & API key
1. ND nhập proxy (`socks5h://…`), các tuỳ chọn, headers/cookies JSON → **Lưu kết nối**. Hệ thống kiểm định dạng và áp proxy cho cả session thumbnail.
2. ND nhập API key cho từng module → **Lưu**. Key được mã hoá bằng `safeStorage` và được truyền vào CLI/sidecar; renderer không đọc được key.

## UC-10 Cập nhật danh sách tag
- **Trong app:** trang **Tags A–Z** → **Cập nhật** cho từng module → tiến độ cập nhật trực tiếp → `tags.updateDone`, sau đó TagsDB nạp lại.
- **Tự động:** bật "Tự cập nhật mỗi N ngày" → kiểm tra khi mở app.
- **Cron:** cài `tools/cron/crontab.example` hoặc systemd timer → `lynxx-update-tags.sh` ghi `2tags/`, log ở `~/.local/state/lynxx/update-tags.log`.

## UC-11 Thêm site mới (người phát triển)
1. Tạo `extensions/<id>/manifest.json` (theo schema) và `downloader.py` (class kế thừa `Downloader`, `DownloaderGelbooru` hoặc mẫu `DownloaderDb`).
2. Viết test theo mẫu `ExtensionInstallTests` và `DanbooruParsingTests`.
3. Chạy `python -m unittest lynxx.tests.test_lynxx` và cài thử bằng `ext.install {path}`.
4. (Tương lai) skill `new-site-extension` hoặc AI generator → xem plan.

## UC-12 Lọc theo giới tính nhân vật (tầng 0)
1. Trong **Bộ lọc & tuỳ chọn tải**, ND bật một hoặc nhiều chip: **Chỉ nữ**, **Chỉ nam**, **Cả hai**, **Không rõ / không có**. Chọn thêm độ tin cậy tối thiểu và có/không **Nghiêm ngặt**.
2. Hệ thống lưu lựa chọn (`settings.download.gender`). Nếu module có ít tag giới tính, hiện thêm cảnh báo phần lớn post sẽ là "Không rõ". Danh sách chặn nội dung gợi ý trẻ vị thành niên là ô riêng "Chặn nội dung gợi ý trẻ vị thành niên", mặc định tắt, không đi kèm bộ lọc giới tính (quyết định 2026-11-18, thay quyết định 2026-11-16).
3. **Preview**: mỗi card có badge Nữ / Nam / Nam+Nữ / Không có / ?. Nền đặc nghĩa là tin cậy cao, viền liền là vừa, viền gạch là thấp; tooltip ghi bằng chứng. Post không khớp được đánh dấu "lọc giới tính" và bị ẩn trừ khi bật "Hiện post bị lọc". Post bị chặn không hiện, chỉ được đếm.
4. **Tải xuống**: CLI nhận `-gender …`; card job hiện "Lọc giới tính: n · Chặn (an toàn): m".
- Bỏ hết chip → tắt lọc; khi đó danh sách chặn cũng không chạy, hành vi như bản gốc.

