# Plan: tag list và job cập nhật tag của extension

> 2026-11-19 · Trạng thái: **đã chốt với người dùng; phần lõi và app đã làm** (nhánh `feat/ext-tag-jobs`, dev log 7at); danh sách của ext trong catalog nằm ngoài repo (dev log 7ay). Extension có job do các session tạo extension làm · Nhánh tham chiếu: `develop`
> Yêu cầu gốc: extension mang sẵn tag list và job cập nhật tag list của riêng nó. Lynxx chỉ việc lấy danh sách đó, và chạy job khi người dùng bấm nút. Job không chạy nền liên tục, chỉ chạy khi được yêu cầu.

## Quyết định (người dùng chọn, 2026-11-19)

| Câu hỏi | Chọn | Không chọn |
|---|---|---|
| Job chạy ở đâu | **Trong app, trên máy người dùng**, khi bấm "Cập nhật" ở Tags A–Z, bằng khoá của chính họ | Job theo lịch trên server (Forgejo) ký và đăng danh sách, app chỉ tải về |
| Bộ dò tag theo engine của lõi (Gelbooru, Danbooru, Moebooru, e621, Philomena, Szurubooru, Shimmie) | **Giữ làm dự phòng** cho ext chưa có `tagUpdate`; ext đã cài không bị hỏng | Bỏ khỏi lõi, chỉ giữ rx / db |
| Cách tự chạy đang có: lịch hệ thống (crontab / Task Scheduler), tự cập nhật khi mở app, tải ngay sau khi cài | **Không bao giờ chạy job của ext**; các tuỳ chọn này giữ nguyên cho rx / db và ext cũ | Bỏ hẳn mọi cách tự chạy |
| Danh sách đi kèm của ext trong catalog nằm ở đâu (chọn ngày 2026-11-19, sau PR #37: 9 danh sách, 33,8 MB, 897 nghìn dòng) | **Ngoài repo**: `tools/build_catalog.py --lists <thư mục>` gói từng danh sách vào zip của ext trong catalog chính thức | Trong repo, cạnh `manifest.json`: repo nặng thêm, và app cũng nặng thêm vì `tools/build_core.py` chép cả `extensions/` vào lõi đóng gói |
| Nguồn chỉ đọc được các tag dùng nhiều (rp, 2026-11-20, dev log 7bd) | **Gộp** vào danh sách đang dùng (`tagUpdate.merge`) | Hạ sanity check; thay cả danh sách bằng 26 nghìn tag |

## Hợp đồng cho extension

`manifest.json`:
```json
{
  "tagList": "<abbr>_tags.json",
  "tagUpdate": {"entry": "tags.py:read_tags"},
  "minSdkVersion": "1.1"
}
```
- `tagList`: file danh sách nằm cạnh `manifest.json`, cùng định dạng các danh sách hiện có (`{"tag": "N posts (type)", ...}`, mỗi dòng một tag, đã sắp xếp). Lynxx dùng file này ngay khi ext được cài.
- `tagUpdate.entry`: `<file>.py:<hàm>`. File phải nằm ở thư mục gốc của ext. Ngoài `entry` chỉ có `merge` (tuỳ chọn, từ Lynxx 2.0.10): `{"minPosts": N}` khi job chỉ đọc được các tag có từ N post; lõi gộp lần đọc vào danh sách đang dùng thay vì thay nó. Lõi cũ hơn 2.0.10 từ chối manifest có khoá này.
- `minSdkVersion: "1.1"`: ext dùng `TagJob` (SDK 1.1). Lynxx 2.0.6 trở về trước đọc manifest trước khi kiểm SDK, nên từ chối với thông báo `unknown field(s): tagUpdate`, không phải thông báo về SDK.

`tags.py`:
```python
def read_tags(job):  # job: lynxx.sdk.TagJob
    page = 0
    while rows := job.get_json(f'{job.site}api/tags?page={page}'):
        for r in rows:
            yield r['name'], r['count'], r.get('type', 'general')
        page += 1
```
- `job.site` (`https://host/` của manifest), `job.api_key` (khoá của người dùng, hoặc `''`), `job.min_posts`.
- `job.get_text(url, accept='')`, `job.get_json(url)`: chỉ dùng URL https của site đó hoặc tên miền con. Mọi request đi qua `PoliteClient` của lõi: nhịp tối thiểu 1 giây, `Crawl-delay`, robots.txt (áp chặt cho ext), Retry-After, giới hạn của khoá (`auth.rateLimit`), ngân sách request, không cookie, dừng khi gặp bot challenge.
- `job.log(text)`: một dòng tiến độ, hiện trong app.
- Mỗi tag là `(name, posts)` hoặc `(name, posts, type)`. Lõi đổi khoảng trắng trong tên thành `_`, bỏ dòng sai định dạng, đưa type lạ về `general`, lọc theo `min_posts`, rồi ghi `<data dir>/tags/<abbr>_tags.json`. Danh sách mới nhất thắng danh sách đi kèm ext. Ghi đè chỉ khi danh sách mới không ít hơn một nửa danh sách đang dùng.
- Job gộp (`merge`): `job.min_posts` ≥ `minPosts`, job phải cho đủ mọi tag có từ chừng đó post, tên đúng dạng của danh sách (viết thường nếu site tìm không phân biệt hoa thường, `_` thay khoảng trắng): lõi so tên chính xác. Lõi gộp lần đọc vào danh sách đang dùng (tag đọc được lấy số post mới, tag mới được thêm, mọi tag khác giữ nguyên). Ghi chỉ khi lần đọc có ít nhất một nửa số tag có từ `minPosts` post của danh sách đang dùng, so theo tên. Danh sách học từ post không bao giờ là nền; việc học vẫn thêm tag lên danh sách đã gộp. Danh sách đã gộp chỉ thay danh sách đi kèm ext khi các lần gộp bắt đầu từ đúng danh sách ext đang mang (sha256 ở `.meta`), không so giờ file; ext mang danh sách khác thì danh sách đó là nền lại. Bản nháp có job gộp phải mang sẵn danh sách: `python -m lynxx.extgen tags` báo `failed` khi thiếu.
- `tags.py` chịu cùng luật tĩnh R10 như `downloader.py`, chỉ khác là không cần lớp Downloader. Không được import `requests` (trừ `exceptions`), `urllib.request`, `socket`... Trình tạo extension kiểm cả file này và đóng gói nó vào zip.

## Ai chạy job, khi nào

| Đường chạy | Job của ext (`tagUpdate`) | Ext cũ / rx / db |
|---|---|---|
| Nút "Cập nhật" ở Tags A–Z | chạy | chạy |
| `lynxx-core tags --modules <abbr>` (nêu tên module) | chạy | chạy |
| `lynxx-core tags --modules auto` (lịch hệ thống của app) | **không** | chạy (module không cần khoá) |
| Tự cập nhật khi mở app (tuỳ chọn, mặc định tắt) | **không** | rx / db |
| Tải ngay sau khi cài ext | **không**: ext đã mang sẵn danh sách | chạy khi ext không mang danh sách |
| Trình tạo extension (extgen / aigen) với bản nháp | **không**: code của bản nháp chỉ chạy trong sandbox | bộ dò theo engine |

### Danh sách đi kèm của ext trong catalog

Danh sách tag là dữ liệu, không phải code, nên **không nằm trong repo**: `extensions/<id>/` chỉ commit `manifest.json` (vẫn khai báo `tagList`), `downloader.py`, `tags.py`, `specs.md`. `.gitignore` bỏ qua `extensions/*/*_tags.json`, nên người bảo trì cứ để danh sách cạnh `manifest.json` trên máy mà không lo commit nhầm. Test `test_repository_extensions_carry_no_tag_list` từ chối file `*_tags.json` hoặc file trên 1 MB được commit trong `extensions/`.

Danh sách đi vào zip của ext trong catalog chính thức (bản phát hành `catalog` trên GitHub), lúc người bảo trì build catalog:
```bash
lynxx-core tags --modules <abbr> --out <thư mục danh sách>     # job của chính ext (ext nằm trong extensions/ của repo)
python tools/build_catalog.py --out <thư mục catalog> --lists <thư mục danh sách>
```
- Danh sách để sẵn cạnh `manifest.json` (bị `.gitignore` bỏ qua) được dùng trước; `--lists` cho các danh sách nằm ở thư mục khác.
- Dùng lại danh sách của catalog đang phát hành, thay vì đọc lại từ site: `unzip -j <id>-<version>.zip '<abbr>_tags.json' -d <thư mục danh sách>` cho từng zip của bản phát hành `catalog`.
- Ext thiếu danh sách trong `--lists` vẫn được build, kèm một dòng báo. App tạo danh sách bằng job của ext khi người dùng bấm "Cập nhật".
- Ext có job gộp (`merge`, hiện là Paheal) thì khác: job không làm ra được cả danh sách, nên giống ext không có nguồn, zip và catalog đi kèm app mang danh sách `2tags/<abbr>_tags.json` làm nền; thiếu thì `build_catalog.py` dừng. Danh sách trong `--lists` ít hơn một nửa danh sách `2tags/` cũng làm build dừng.
- `build_catalog.py` từ chối zip vượt giới hạn của trình cài (10 MB nén, 50 MB giải nén, 2.000 file).

| Đường cài | Danh sách tag |
|---|---|
| Zip (bản nháp của trình tạo, file tự cài) | có trong zip: dùng được ngay |
| Catalog chính thức (zip tải về, đã ký) | có trong zip (`--lists`): dùng được ngay |
| Catalog đi kèm app (`extensions/` của repo, đóng gói trong lõi) | không có: bấm "Cập nhật" ở Tags A–Z (job của ext; ví dụ Safebooru khoảng 1.000 request, khoảng 21 phút) |

## Phân công giữa các session

- **Lõi và app** (session "Repository overview and components", nhánh `feat/ext-tag-jobs`): `lynxx/tags_updater.py` (`TagJob`, `EXTENSION_JOB`, `TagSource.on_demand`), `lynxx/modspec.py` (`tagUpdate`), `lynxx/sdk.py` (SDK 1.1), `lynxx/extgen/policy.py`, `sandbox.py`, `taglist.py`, `app/main.js`, `app/renderer/`, schema manifest, tài liệu.
- **Extension và skill** (các session tạo ext): `tags.py`, manifest, danh sách đi kèm của từng ext (trong zip bản nháp; với ext của catalog thì qua `--lists`, không commit vào `extensions/`); skill `new-site-extension`. Không sửa các file của lõi và app ở trên.
- Bộ đọc riêng cho một site (không phải engine) nằm trong `tags.py` của ext đó, không nằm trong lõi.
