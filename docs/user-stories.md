# Lynxx — User Stories (US)

> 2.0 r1 · 2026-11-16 · Mỗi story liên kết tới use case (UC) và acceptance criteria (AC) tương ứng.
> Vai trò: **Người dùng** (tải nội dung qua app), **Người dùng nâng cao** (CLI/cron/proxy/API key), **Người phát triển extension**.

| ID | Vai trò | Story | Ưu tiên | UC | AC | Trạng thái |
|---|---|---|---|---|---|---|
| US-01 | Người dùng | Tôi muốn gõ tag và nhận gợi ý theo độ phổ biến để không phải nhớ chính xác tên tag | Cao | UC-01 | AC-01 | Xong |
| US-02 | Người dùng | Tôi muốn chọn tag theo bảng chữ cái A–Z khi không muốn gõ | Cao | UC-02 | AC-02 | Xong |
| US-03 | Người dùng | Tôi muốn gộp nhiều tag thành nhóm OR hoặc nhóm loại trừ ngay trên giao diện mà không cần nhớ cú pháp | Cao | UC-03 | AC-03 | Xong |
| US-04 | Người dùng | Tôi muốn biết truy vấn có bao nhiêu kết quả trước khi tải | Trung bình | UC-04 | AC-04 | Xong |
| US-05 | Người dùng | Tôi muốn xem trước thumbnail, chọn từng post và chỉ tải những post đã chọn | Cao | UC-05 | AC-05 | Xong |
| US-06 | Người dùng | Tôi muốn bấm vào tag của một post trong preview để thêm hoặc loại trừ tag đó khỏi truy vấn | Trung bình | UC-05 | AC-06 | Xong |
| US-07 | Người dùng | Tôi muốn tải nền, xem tiến độ, huỷ được và mở thư mục kết quả | Cao | UC-06 | AC-07 | Xong |
| US-08 | Người dùng | Tôi muốn Rule34 và Danbooru có sẵn, các site khác cài thêm khi cần | Cao | UC-07 | AC-08 | Xong |
| US-09 | Người dùng | Tôi muốn tìm Danbooru với nhiều hơn 2 tag mà không bị lỗi giới hạn | Cao | UC-05, UC-06 | AC-09 | Xong |
| US-10 | Người dùng | Tôi muốn chọn theme và icon; nền mặc định là icon làm mờ ở phía sau, hoặc ảnh của tôi (không làm mờ) | Trung bình | UC-08 | AC-10 | Xong |
| US-11 | Người dùng | Khi đổi icon, tôi muốn biết vì sao taskbar chưa đổi và có cách cập nhật nhanh nhất | Thấp | UC-08 | AC-11 | Xong |
| US-12 | Người dùng | Tôi muốn làm mờ thumbnail để riêng tư khi dùng ở nơi công cộng | Thấp | UC-05 | AC-12 | Xong |
| US-13 | Người dùng nâng cao | Tôi muốn dùng proxy SOCKS5 tự phân giải DNS (`socks5h`) và tải file không qua proxy | Trung bình | UC-09 | AC-13 | Xong |
| US-14 | Người dùng nâng cao | Tôi muốn lưu API key an toàn, không hiện trong log | Trung bình | UC-09 | AC-14 | Xong |
| US-15 | Người dùng nâng cao | Tôi muốn danh sách tag tự cập nhật định kỳ (cronjob hoặc ngay trong app) | Cao | UC-10 | AC-15 | Xong |
| US-16 | Người dùng | Khi website chặn bằng bot challenge, tôi muốn app dừng ngay và chỉ cách xử lý, thay vì treo hoặc retry mãi | Cao | UC-06 | AC-16 | Xong |
| US-17 | Người dùng nâng cao | Khi proxy chết, tôi muốn app báo lỗi sau một số lần thử, không lặp vô hạn | Cao | UC-09 | AC-17 | Xong |
| US-18 | Người phát triển extension | Tôi muốn thêm site mới bằng một thư mục `manifest.json` + `downloader.py` mà không phải sửa lõi | Cao | UC-11 | AC-18 | Xong (SDK v1) |
| US-19 | Người dùng | Tôi muốn cài extension từ file zip hoặc catalog từ xa một cách an toàn (kiểm SHA-256) | Trung bình | UC-07 | AC-19 | Xong |
| US-20 | Người phát triển extension | Tôi muốn một skill sinh specs/components và khung code khi thêm site mới; AI có thể tạo downloader rồi đưa vào hệ thống như extension | Trung bình | — | — | Plan: [plans/site-extension-skill-and-ai-generator.md](plans/site-extension-skill-and-ai-generator.md) |
| US-21 | Người dùng | Tôi muốn lọc nội dung theo only-female, only-male hoặc both | Trung bình | UC-12 | AC-21…AC-24 | **P0 xong** (lọc theo tag); model (P1–P4) theo [plan](plans/gender-filter-model.md) |
| US-22 | Người dùng | Tôi muốn tìm kiếm thông minh hơn: lưu truy vấn, lịch sử, gợi ý tag liên quan, tag alias | Trung bình | — | — | Plan: [plans/search-engine.md](plans/search-engine.md) |
