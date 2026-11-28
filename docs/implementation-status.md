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
- **Extension** (`app/plugin/`): parser HTML + selector Jsoup tự viết, runtime (fetch/Http/Html/
  Response/load/CryptoJS/localStorage/localCookie/cacheStorage/Engine.newBrowser), sandbox Chromium ẩn với cầu nối đồng
  bộ, HTTP có cookie jar, chặn IP nội bộ, nhịp theo host, Retry-After; cài từ thư mục / zip / link / kho; cấu hình
  `config`; mở trang nguồn để vượt kiểm tra thủ công. Đã chạy thử extension TruyenQQ thật (offline).
- **Job tải truyện (mục 1)**: detail → toc → chương còn thiếu → trang vào staging (`.part`, Range resume, kiểm tra ảnh,
  fallback, Retry-After, huỷ) → sidecar ghi Page/sha256 vào thư viện; cập nhật chương mới không tải lại chương cũ;
  tiến độ trên trang Tải xuống; thông báo hệ thống.
- **Thư viện collection (mục 4)**: trang "Thư viện" (bìa, nguồn, số chương, đang đọc), chi tiết truyện, tải chương
  chọn, cập nhật, xoá; schema SQLite v2 (migration tự động).
- **Reader (mục 3)**: ảnh cuộn dọc / trượt ngang / từng trang, LTR/RTL, văn bản cỡ chữ chỉnh được, PDF; mục lục,
  chương trước/sau, phím ←/→ `[` `]` F, toàn màn hình, lưu & khôi phục vị trí đọc theo chương và chương đọc gần nhất.
- **Xuất (mục 11)**: EPUB 2 / EPUB 3 / KEPUB / CBZ / PDF với tuỳ chọn như ảnh tham chiếu (phạm vi, giới thiệu, mục lục có
  link, tên chương, chương chưa tải, tự cập nhật); cấu trúc EPUB giống file mẫu; nhập lại file EPUB có chương
  chỉ chứa link ảnh → tải bổ sung → xuất lại đầy đủ.
- **PDF ảnh (mục 2)**: JPEG (gray/RGB/CMYK), PNG (nhúng nguyên hoặc giải mã alpha → SMask), WebP/GIF/AVIF/BMP chuyển
  JPEG bằng Chromium; outline theo chương.
- **Nhập ~30 định dạng (mục 9)**: epub, txt, mobi, prc, azw, azw3, fb2, fbz, umd, pdf, cbz, zip, html, htm, xhtml, md,
  markdown, chm, doc, dot, wps, docx, docm, dotx, dotm, odt, ott, fodt, rtf, hwp, hwpx, hwpml — thuần Python, không mạng,
  sanitize XHTML, chặn zip bomb/traversal/entity; 137 test (kèm fuzz trong quá trình viết).
- **Skill `new-site-extension` (mục 10, 12)**: chế độ mặc định tạo extension (template comic/novel, tham chiếu
  API, checklist, quy tắc V1–V8), thử bằng `node app/plugin/cli.js`; chế độ booru cũ chuyển sang `reference/booru.md`.
  Skill nằm trong `.claude/` (bị `.gitignore`), đã cập nhật ở repo chính.
- **Kiểm chứng trong Electron 45.2.1**: `app/test/electron-smoke.js` (sandbox + cầu nối đồng bộ, HTTP thật, timeout,
  printToPDF, WebP→JPEG, `lynxx-lib://`) và `app/test/electron-e2e.js` (sidecar Python thật, site giả: cài extension,
  job tải, thư viện, reader, xuất EPUB/KEPUB/CBZ/PDF, cập nhật chương mới) đều pass; app đầy đủ khởi động, mở được EPUB
  mẫu và PDF trong Reader, thoát đúng khi đóng cửa sổ. Một review độc lập phần main process đã được sửa hết
  (thoát app khi còn cửa sổ sandbox, timeout không ảnh hưởng script kế tiếp, renderer không tự chọn đường dẫn xuất,
  trình duyệt ẩn chỉ do script mở nó điều khiển, proxy cho session extension, ảnh webtoon dài khi xuất PDF…).
- **Dọn booru (mục 5, một phần)**: README, `docs/specs.md`, mô tả `app/package.json` / `pyproject.toml`, CI chạy test
  Lynxx.

### Bản 1.0.0 (2026-11-28, bản phát hành đầu tiên)
- **Gỡ hẳn hệ booru (mục 5)**: trang Tìm kiếm / Đã tải / Tags A–Z / Extensions booru và các mục Cài đặt booru; 16
  extension trong `extensions/`, danh sách tag `2tags/`, lõi tải booru (CLI, downloader, tag DB, lọc giới tính, model
  server, index tải, catalog có chữ ký, trình tạo extension AI); main.js còn ~1.100 dòng, app.js ~420 dòng. Lõi Python chỉ
  còn thư viện chuẩn: frozen core 33,5 MiB (trước 78,5 MiB). Settings cũ được dọn khi mở (settings v4). Skill
  `new-site-extension` bỏ chế độ booru.
- **Màu theo icon**: theme mặc định Abyss (navy + cyan), Paper / OLED nhấn cyan; theme cam cũ tự chuyển sang Abyss.
- **Release như Lustra**: workflow Release build 6 nền tảng (staging → rc, production → release dùng lại file của rc);
  bỏ model server và image Docker. Sửa lỗi chặn build: `app/build/after-pack.js` bị sót khi fork; icon `*-64.png` thực ra
  1024 px.
- **Sửa lỗi**: nhiều luồng sidecar cùng mở database thư viện mới (hoặc v1) → `duplicate column` / `database is locked`;
  renderer có thể gọi hook của books.js trước khi books.js chạy (thư viện trống lúc mở app).
- **Self-test mới cho bản đóng gói**: sidecar, RPC được phép / bị chặn, các trang và theme, nhập → đọc → xuất EPUB,
  extension trong sandbox đóng gói, hỏi lại khi thoát lúc còn lượt tải.

## Chưa làm / còn lại

- **nhentai live (mục 6)**: vẫn `expired_review` trong policy adapter dựng sẵn. Extension do người dùng cài đi
  theo đường extension (người dùng xác nhận khi cài, chịu trách nhiệm về quyền tải).

## Cố ý không làm

- **cuutruyen (mục 7)**: không giải `drm_data`, không unscramble ảnh, không gọi image host bị robots chặn. Runtime
  Lynxx không cung cấp `Graphics` nên extension dùng canvas để unscramble cũng không chạy.
- **Model uncen (mục 8)**: không xây model khôi phục vùng bị che trong nội dung tình dục; `lynxx/uncen.py` chỉ là
  safety contract.
- **Extension mã hoá** (`metadata.encrypt`): khoá giải mã không công khai, Lynxx không cố phá.
- Không vượt CAPTCHA/anti-bot, không mở khoá chương VIP/trả phí.
