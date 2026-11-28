# Lynxx — Đặc tả (truyện, sách, extension vBook)

Lynxx bắt nguồn từ Lustra (một app tải booru); từ 2.1.0 phần booru đã được gỡ hẳn, Lynxx chỉ còn truyện và sách.

## 1. Phạm vi

Lynxx tải, đọc và xuất truyện tranh, truyện chữ và sách:

- nguồn mạng qua extension định dạng **vBook** (plugin.json + src/*.js);
- file trên máy (khoảng 30 định dạng, chuyển đổi offline);
- thư viện dạng collection → chapter → page/nội dung, có tiến độ đọc;
- xuất EPUB 2 / EPUB 3 / KEPUB / CBZ / PDF và cập nhật file đã xuất khi có chương mới.

## 2. Kiến trúc

| Tầng | File | Vai trò |
|---|---|---|
| Renderer | `app/renderer/app.js` | Khung app: điều hướng, theme / icon / hình nền, Cài đặt, Nhật ký, trạng thái lõi |
| Renderer | `app/renderer/books.js`, `index.html`, `styles.css` | Trang Thư viện, Nguồn truyện, Đọc truyện, job tải, hộp thoại Xuất |
| Preload | `app/preload.js` | `window.lynxx.settings / plugins / books / shelf / reader / app`, `rpc` chỉ cho `app.info`, `formats.supported` |
| Main | `app/main.js` | Settings, khởi động sidecar (lõi đóng gói `lynxx-core` hoặc `python -m lynxx.server`), ảnh bìa `lynxx-thumb://`, proxy, icon / launcher, thoát app khi còn job, self-test |
| Main | `app/plugin/ipc.js` | IPC, protocol `lynxx-lib://`, luồng xuất (transcode ảnh, printToPDF) |
| Main | `app/plugin/host.js` | Extension đã cài, pool sandbox, cầu nối đồng bộ |
| Main | `app/plugin/http.js` | HTTP cho extension và tải trang (cookie, chặn IP nội bộ, nhịp theo host, Retry-After, resume) |
| Main | `app/plugin/downloader.js` | Job tải truyện (`BookJobs`) |
| Sandbox | `app/plugin/sandbox.html`, `sandbox.js`, `sandbox-preload.js` | Renderer ẩn chạy script extension |
| Runtime | `app/plugin/html.js`, `runtime.js`, `cryptojs.js`, `service.js` | Parser HTML + selector Jsoup, API vBook, CryptoJS, chuẩn hoá kết quả |
| Sidecar | `lynxx/server.py` | JSON-RPC: thư viện, đọc, xuất, giải nén extension (chỉ thư viện chuẩn của Python) |
| Thư viện | `lynxx/library/{db,books,importer}.py` | SQLite v2, collection/chapter/page, nhập file |
| Định dạng | `lynxx/formats/*.py` | Bộ chuyển đổi → `Document` |
| Xuất | `lynxx/export/*.py` | EPUB/KEPUB, CBZ, PDF ảnh, HTML để in PDF |

## 3. Extension vBook

### 3.1 Gói và manifest

`plugin.json` (`metadata`: name, author, version, source, regexp, description, locale, type, nsfw, encrypt; `script`:
home, genre, search, detail, page, toc, chap…; `config`: hằng số người dùng chỉnh được). `src/*.js` ở cấp đầu của `src/`.
id = slug(name) + 8 hex của sha256(name + source): cập nhật cùng extension giữ id, storage và collection.

Không chạy: `metadata.encrypt = true` (khoá của vBook không công khai), loại `tts`, `translate`, `audio`, `video`,
`news`, `forum`, `ai`.

### 3.2 Cài đặt

Từ thư mục, `plugin.zip`, link, hoặc kho (JSON `{metadata, data: [{name, version, type, path, icon, …}]}`). Mọi cách đều
qua hộp thoại xác nhận của main. File zip do sidecar giải nén (`plugins.unpack`): chỉ nhận `plugin.json`, `icon.png`,
`src/*.js`, chặn traversal, symlink, file mã hoá, ≤ 10 MB/file, ≤ 60 MB tổng. Gói được kiểm tra rồi chép vào
`<data>/plugins/installed/<id>/` bằng thư mục staging + rename.

### 3.3 Sandbox và cầu nối

- Mỗi lần chạy: một `BrowserWindow` ẩn, `sandbox: true`, context isolation, không Node, partition riêng không có mạng
  (webRequest huỷ mọi request không phải `file://`), CSP `default-src 'none'; script-src 'self' 'unsafe-eval'`.
- Pool tối đa 3 cửa sổ, tái tạo sau 200 lần chạy; mỗi lần chạy tối đa 120 giây (quá hạn: huỷ cả renderer).
- Script gọi API đồng bộ → preload `ipcRenderer.sendSync('plugin-sandbox:sync', op, payload)` → main kiểm tra sender
  thuộc pool rồi xử lý: `request`, `sleep` (≤ 30 s), `storage` (JSON theo extension, ≤ 2 MB), `cookie`, `browser`
  (`Engine.newBrowser`: cửa sổ ẩn trong partition extension, tối đa 2 mỗi lần chạy), `crypto` (Node crypto).
- HTTP: chỉ http(s); mọi bước redirect qua `assertPublicHost` (chặn loopback, mạng nội bộ, link-local); cookie của
  partition `persist:lynxx-plugins` gửi/lưu thủ công; ≥ 250 ms giữa hai request cùng host; 429/503 có `Retry-After`
  ≤ 60 s thì chờ và thử lại tối đa 2 lần; thân ≤ 32 MB; timeout mặc định 120 s.

### 3.4 Ngữ nghĩa runtime (khớp vBook)

`fetch` không ném lỗi (lỗi mạng = status 504); `first()/last()/get(i)` trả phần tử rỗng; `attr()` trả `""`;
`[attr~=regex]` là regex; `:contains` không phân biệt hoa thường; parser tạo `html/head/body` và `tbody` như Jsoup;
`load('x.js')` là thay thế văn bản (cả trong comment), `crypto.js` luôn là CryptoJS dựng sẵn; hằng `config` là tham số
của hàm ngoài nên `let DOMAIN` trong script vẫn hợp lệ; `Http.post(url).params()` không có body → gửi form.

### 3.5 Chuẩn hoá kết quả (`service.js`)

Link tương đối nối với `host` hoặc `source`, `//cdn` → `https:`; item không có `name` bị bỏ; trùng link bị bỏ;
description/detail HTML → text; `page.js` + `toc.js` ghép, bỏ trùng, tối đa 20 000 chương; ảnh comic: chuỗi hoặc
`{link, fallback, referrerpolicy, script}`; fragment `#referrerpolicy=…&script=…` của vBook được tách ra.

## 4. Job tải truyện

1. `detail` → `toc` → sidecar `library.book.upsert` (collection theo `plugin:<id>` + URL truyện; chương theo URL;
   chương mới thêm vào, chương đã tải giữ nguyên file, chương site bỏ đi xếp cuối).
2. Ảnh bìa (một lần, hoặc lại khi lần trước lỗi).
3. Mỗi chương chưa `ready` và không khoá: `chap` → comic: tải trang vào `library/staging/<collection>/<chapter>/`
   (3 luồng, `.part` + Range, thử lại 3 lần, fallback URL, kiểm tra magic bytes) → `library.chapter.commitImages`
   (chuyển file vào thư viện, ghi Page có sha256/kích thước/kiểu); novel: tải ảnh trong chương (best effort) →
   `library.chapter.commitText` (sanitize XHTML, đổi src sang file cục bộ).
4. Chương lỗi được đánh dấu `failed` và không chặn chương sau; huỷ giữ lại file `.part` để lần sau tiếp tục.
5. Xong: làm mới các file đã xuất có bật "Tự cập nhật".

Biến thể `startRemote`: chương của file EPUB đã nhập chỉ có link ảnh (vBook xuất chương chưa tải theo cách này) được
tải về như trên, không cần extension.

## 5. Thư viện (SQLite v2)

`collections` thêm `kind` (comic/novel/document), `source_url`, `cover_path`; `chapters` thêm `source_url`, `position`,
`content_path`, `page_count`, `locked`, `error`; `reader_progress` thêm `direction`; bảng mới `exports`
(collection, format, path, options, danh sách chương, sha256, auto_update) và `collection_progress` (chương đọc gần nhất).
Migration v1 → v2 tự động, idempotent.

Bố cục file: `collections/<c>/cover.*`, `collections/<c>/res/*`, `collections/<c>/chapters/<ch>/pages/0001.jpg`,
`…/chapter.xhtml`, `…/res/*`, `…/original.<ext>`; `staging/<c>/<ch>/…` khi đang tải. Mọi đường dẫn từ app được kiểm
tra nằm trong thư mục mong đợi. Renderer đọc file qua `lynxx-lib://lib/<đường dẫn trong thư viện>` (chỉ ảnh, có
kiểm tra realpath).

## 6. Nhập file

`lynxx/formats/__init__.py`: `sniff_format` theo magic bytes trước phần mở rộng (doc thực chất là RTF/DOCX/HTML/text,
OLE có `FileHeader`+`BodyText` → HWP…). PDF giữ nguyên (trình xem PDF); định dạng khác → `Document` (chương XHTML đã
sanitize + tài nguyên ảnh) → ghi vào thư viện, giữ file gốc. Giới hạn: ≤ 512 MB đầu vào, zip bomb / traversal / symlink
bị chặn, giải nén có trần, XML có `<!ENTITY` bị từ chối, không mạng, không subprocess. DRM → báo lỗi.

| Nhóm | Module | Ghi chú |
|---|---|---|
| txt, md, html/htm/xhtml, rtf, fb2/fbz | text, markdown, htmldoc, rtf, fb2 | tách chương theo tiêu đề; ảnh cục bộ chỉ trong thư mục của file |
| docx/docm/dotx/dotm, odt/ott/fodt, hwpx/hwpml | ooxml, odf, hwpx | heading → chương, list, bảng, ảnh, chú thích |
| doc/dot/wps, hwp | cfb, msdoc, hwp | đọc OLE2 tự viết; file có mật khẩu bị từ chối |
| mobi/prc/azw/azw3, umd, chm | mobi, umd, chm | PalmDOC, HUFF/CDIC, KF8 (SKEL/FRAG), LZX |
| epub, cbz/zip | epub, comic | EPUB chỉ ảnh → truyện tranh; bỏ trang mục lục; trang Introduction → mô tả; chương chỉ có link ảnh → chương chờ tải |

## 7. Xuất

Cấu trúc EPUB giống bản xuất của vBook: `OEBPS/content.opf`, `toc.ncx`, `Styles/stylesheet.css`, `Images/cover.*`,
`Images/img_00001.*`, `Text/cover.xhtml`, `introduction.xhtml`, `toc.xhtml` (link tới từng chương),
`chapter_N.xhtml`; `mimetype` đứng đầu, không nén. EPUB 3 thêm `nav.xhtml` và `dcterms:modified`; KEPUB thêm
`koboSpan` và đuôi `.kepub.epub`. CBZ: ảnh theo thứ tự + `ComicInfo.xml` (Title, Series, Summary, Writer, Genre, Web,
LanguageISO, PageCount, `Pages/Page Bookmark` cho từng chương). PDF truyện tranh: mỗi ảnh một trang đúng kích thước,
JPEG nhúng nguyên, PNG nhúng nguyên hoặc giải mã (alpha → SMask), WebP/GIF/AVIF/BMP → JPEG qua Chromium, outline theo
chương. PDF truyện chữ: HTML một file → `printToPDF` (A5, outline từ heading, link mục lục). Tên mặc định
`<tên truyện>_<nguồn>.<đuôi>`. Mỗi lần xuất ghi vào bảng `exports`; "Cập nhật" tạo lại file với tuỳ chọn cũ.

## 8. RPC sidecar

`app.info`, `app.shutdown`, `source.list`, `source.policy`, `library.import`, `formats.supported`, `library.collections`, `library.collection`, `library.collection.delete`,
`library.collection.remote`, `library.book.upsert`, `library.book.cover`, `library.chapter.commitImages`,
`library.chapter.commitText`, `library.chapter.state`, `reader.chapter`, `reader.progress.get/set` (thêm `direction`),
`export.suggest`, `export.build` (lỗi -32010 = cần in HTML, -32011 = cần chuyển ảnh), `export.bundle`, `export.record`,
`export.autoTargets`, `export.forget`, `plugins.unpack`.

## 9. Không làm

- Giải mã/unscramble ảnh có DRM, vượt CAPTCHA hay anti-bot (extension dùng `Graphics` của vBook không chạy được).
- Mở khoá chương VIP/trả phí (chương `lock`/`pay` chỉ được liệt kê).
- Model khôi phục vùng bị che trong nội dung tình dục (`lynxx/uncen.py` chỉ có contract an toàn).
