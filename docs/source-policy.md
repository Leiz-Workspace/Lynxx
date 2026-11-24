# Lynxx — Policy và đặc tả nguồn ban đầu

> Trạng thái: `policy-gated` · 2026-11-24

Lynxx không gửi request tải nội dung tới nguồn nào khi hồ sơ policy chưa được phê duyệt. Mọi adapter phải có snapshot `robots.txt`, điều khoản sử dụng, capability được phép, giới hạn request và quyền lưu cục bộ trước khi chuyển sang `permitted`.

## nhentai.net

- Trạng thái hiện tại: `manual-review` / chưa bật tải.
- Không dùng HTML scraping, không đoán CDN path, không bulk crawl.
- Chỉ xem xét API chính thức sau khi xác minh live endpoint, ToS, robots, rate limit và quyền sử dụng.
- Rủi ro bản quyền/DMCA và khả năng endpoint thay đổi phải được ghi vào policy decision trước khi bật.

## cuutruyen.net

- Trạng thái hiện tại: `blocked`.
- Không giải mã `drm_data`, không unscramble ảnh để vượt cơ chế bảo vệ.
- Không gọi image host khi `robots.txt` từ chối route tương ứng.
- Chỉ xem xét lại nếu có API/export chính thức, quyền lưu cục bộ rõ ràng và policy cho phép.

## Quy tắc bất biến

- Không bypass CAPTCHA, Cloudflare, paywall, DRM hoặc access control.
- Policy không rõ hoặc hết hạn đánh giá thì fail-closed.
- Fixture CI phải offline và không chứa cookie, token hay nội dung nhạy cảm.
- Artifact dẫn xuất phải giữ bản gốc bất biến và ghi provenance.
