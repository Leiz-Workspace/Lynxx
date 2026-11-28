# Lynxx — Implementation status

Đặc tả chi tiết: [specs.md](specs.md).

## Đã hoàn tất

### Nền tảng (các đợt trước)
- Repository độc lập, remote SSH `git@github.com:Leiz-Workspace/Lynxx.git`; nhánh `develop`, `staging`, `production`.
- Electron shell, preload boundary, Python sidecar, settings/secrets đổi namespace Lynxx; bộ icon cyan/monochrome.
- Domain model `Collection → Chapter → Page → Artifact`, SQLite WAL, policy gate fail-closed, adapter contract.

### Đợt 2026-11-28
- **Sửa lỗi kế thừa**: `lynxx/server.py` mất dòng `def m_app_info` và `def m_reader_progress_get` (sidecar sẽ NameError
  khi khởi động); `LibraryDB.create_page` dính đoạn ghi `policy_decisions` với biến không tồn tại; reader cũ ẩn luôn
  khung PDF.
- **Extension định dạng vBook** (`app/plugin/`): parser HTML + selector Jsoup tự viết, runtime vBook (fetch/Http/Html/
  Response/load/CryptoJS/localStorage/localCookie/cacheStorage/Engine.newBrowser), sandbox Chromium ẩn với cầu nối đồng
  bộ, HTTP có cookie jar, chặn IP nội bộ, nhịp theo host, Retry-After; cài từ thư mục / zip / link / kho; cấu hình
  `config`; mở trang nguồn để vượt kiểm tra thủ công. Đã chạy thử extension TruyenQQ thật của vBook (offline).
- **Job tải truyện (mục 1)**: detail → toc → chương còn thiếu → trang vào staging (`.part`, Range resume, kiểm tra ảnh,
  fallback, Retry-After, huỷ) → sidecar ghi Page/sha256 vào thư viện; cập nhật chương mới không tải lại chương cũ;
  tiến độ trên trang Tải xuống; thông báo hệ thống.
- **Thư viện collection (mục 4)**: trang "Thư viện" (bìa, nguồn, số chương, đang đọc), chi tiết truyện, tải chương
  chọn, cập nhật, xoá; schema SQLite v2 (migration tự động).
- **Reader (mục 3)**: ảnh cuộn dọc / trượt ngang / từng trang, LTR/RTL, văn bản cỡ chữ chỉnh được, PDF; mục lục,
  chương trước/sau, phím ←/→ `[` `]` F, toàn màn hình, lưu & khôi phục vị trí đọc theo chương và chương đọc gần nhất.
- **Xuất (mục 11)**: EPUB 2 / EPUB 3 / KEPUB / CBZ / PDF với tuỳ chọn như ảnh tham chiếu (phạm vi, giới thiệu, mục lục có
  link, tên chương, chương chưa tải, tự cập nhật); cấu trúc EPUB giống file vBook mẫu; nhập lại file vBook có chương
  chỉ chứa link ảnh → tải bổ sung → xuất lại đầy đủ.
- **PDF ảnh (mục 2)**: JPEG (gray/RGB/CMYK), PNG (nhúng nguyên hoặc giải mã alpha → SMask), WebP/GIF/AVIF/BMP chuyển
  JPEG bằng Chromium; outline theo chương.
- **Nhập ~30 định dạng (mục 9)**: epub, txt, mobi, prc, azw, azw3, fb2, fbz, umd, pdf, cbz, zip, html, htm, xhtml, md,
  markdown, chm, doc, dot, wps, docx, docm, dotx, dotm, odt, ott, fodt, rtf, hwp, hwpx, hwpml — thuần Python, không mạng,
  sanitize XHTML, chặn zip bomb/traversal/entity; 137 test (kèm fuzz trong quá trình viết).
- **Skill `new-site-extension` (mục 10, 12)**: chế độ mặc định tạo extension vBook (template comic/novel, tham chiếu
  API, checklist, quy tắc V1–V8), thử bằng `node app/plugin/cli.js`; chế độ booru cũ chuyển sang `reference/booru.md`.
  Skill nằm trong `.claude/` (bị `.gitignore`), đã cập nhật ở repo chính.
- **Dọn booru (mục 5, một phần)**: README, `docs/specs.md`, mô tả `app/package.json` / `pyproject.toml`, CI chạy test
  Lynxx. README và đặc tả booru cũ chuyển thành `docs/legacy-booru-*.md`.

## Chưa làm / còn lại

- **Gỡ hẳn hệ booru**: thư mục `extensions/` (16 extension booru) vẫn còn vì `tools/build_core.py`, `lynxx/aigen` và
  nhiều test cũ (`test_search_kinds`, `test_catalog_trust`, `test_batch_d_app`, `test_*_ext`) phụ thuộc vào nó; các
  trang Booru vẫn hoạt động trong app. Nên làm thành một đợt riêng, có môi trường chạy đủ test cũ.
- **Kiểm chứng trong Electron thật**: máy dev chưa cài `node_modules` (Electron); giao diện đã kiểm bằng mock trong
  trình duyệt, runtime extension và Python đã có test, nhưng sandbox/IPC/printToPDF/transcode chưa chạy trong app.
- **nhentai live (mục 6)**: vẫn `expired_review` trong policy adapter dựng sẵn. Extension vBook do người dùng cài đi
  theo đường extension (người dùng xác nhận khi cài, chịu trách nhiệm về quyền tải).

## Cố ý không làm

- **cuutruyen (mục 7)**: không giải `drm_data`, không unscramble ảnh, không gọi image host bị robots chặn. Runtime
  Lynxx không cung cấp `Graphics` nên extension vBook dùng canvas để unscramble cũng không chạy.
- **Model uncen (mục 8)**: không xây model khôi phục vùng bị che trong nội dung tình dục; `lynxx/uncen.py` chỉ là
  safety contract.
- **Extension mã hoá vBook** (`metadata.encrypt`): khoá giải mã không công khai, Lynxx không cố phá.
- Không vượt CAPTCHA/anti-bot, không mở khoá chương VIP/trả phí.
