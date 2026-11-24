# Vận hành Lynxx

## 1. Cài đặt môi trường (dev)
```bash
cd ~/Workspace/Lynxx
uv venv .venv --python 3.13 --python-preference only-managed   # bản Python của uv có sẵn tkinter (GUI Tk cũ)
uv pip install --python .venv/bin/python -r requirements.txt ruff
cd app && npm install && node node_modules/electron/install.js   # tải binary Electron 45.2.1
```

## 2. Chạy
| Mục đích | Lệnh |
|---|---|
| App desktop | `cd app && npm start` (`npm run start:debug` mở DevTools) |
| CLI | `.venv/bin/python -m lynxx -module db -path ~/Downloads/Lynxx cat_ears rating:g` |
| GUI Tk cũ | `.venv/bin/python -m lynxx` |
| Sidecar (debug) | `.venv/bin/python -m lynxx.server`, rồi gõ `{"jsonrpc":"2.0","id":1,"method":"app.info"}` |
| UI mock trên trình duyệt | `python3 -m http.server 5178 --directory app`, mở `http://localhost:5178/renderer/index.html` |
| Tạo extension (CLI / skill) | `.venv/bin/python -m lynxx.extgen survey https://site` → `new https://site --out .extgen-drafts` → `record .extgen-drafts/<id> --query "<tag>"` → `check .extgen-drafts/<id>` → `pack .extgen-drafts/<id> --out <file>.zip` |
| Công cụ index các lần tải | `.venv/bin/python -m lynxx.dlindex_tools clean --dry-run`, `import ~/Pictures/booru --module db`, `retag ~/Downloads/Lynxx --source index`, `trash list`, `trash purge --days 30` (mục 3b) |
| Worker tạo extension (debug) | `(echo '{"cmd":"start","url":"https://site","mode":"template"}'; sleep 120) \| .venv/bin/python -m lynxx.aigen.worker` |

Biến môi trường:
- `LYNXX_PYTHON`: đường dẫn python cho app;
- `LYNXX_DATA_DIR`: thư mục dữ liệu;
- `LYNXX_USER_DATA`: userData của Electron;
- `LYNXX_TAGS_DIR`: thư mục tag list ưu tiên;
- `LYNXX_CATALOG_DIR`, `LYNXX_CATALOG_URL`: catalog extension;
- `LYNXX_CATALOG_KEYS_FILE`: **chỉ cho nhà phát triển**. File khoá công khai thêm vào danh sách tin cậy của catalog từ xa (cùng định dạng `lynxx/safety_data/catalog_keys.json`), dùng để thử catalog tự host (mục 3d);
- `LYNXX_CATALOG_ALLOW_UNSIGNED=1`: **chỉ cho nhà phát triển**. Nhận catalog từ xa không có file `.sig`; thẻ hiện "chưa ký", hộp xác nhận cảnh báo. Chữ ký có mà sai thì vẫn bị từ chối. Không bao giờ đặt biến này cho người dùng;
- `LYNXX_IPC=1`: CLI chạy ở chế độ báo tiến độ;
- `LYNXX_CATALOG_AUTOLOAD=0`: chỉ dùng extension builtin và đã cài (app đặt cho sidecar và job). Mặc định CLI và GUI Tk vẫn nạp extension trong catalog khi cần;
- `LYNXX_CORE`: đường dẫn lõi đóng băng (`lynxx-core`) cho app dev;
- `LYNXX_STREAMING=0`: tắt pipeline streaming (quay lại kiểu quét hết rồi mới tải, dùng để so sánh / gỡ lỗi);
- `LYNXX_DLINDEX=0`: không ghi file tải xong vào index các lần tải (mục 3b). Index vẫn đọc được, `-skip_known` vẫn chạy. Các test đặt sẵn biến này (`lynxx/tests/__init__.py`);
- `LYNXX_TAGS_LEARN=0`: không học danh sách tag từ post đã xem / đã tải (mục 3). Các test đặt sẵn biến này, `test_tag_sources` tự bật lại;
- `LYNXX_GENDER_MODEL_URL`, `LYNXX_GENDER_MODEL_TOKEN`: địa chỉ và token model server cho CLI (mục 3c; app dùng Cài đặt). Model server đọc `LYNXX_GENDER_MODEL_TOKEN` làm token của nó;
- `LYNXX_GENDER_MODEL_DIR`: thư mục model (model server hoặc model trong tiến trình) thay cho `<data>/models/wd-vit-tagger-v3`. Đặt biến này khi chạy test thì có thêm test với model thật;
- `LYNXX_GENDER_MODEL_DEVICE`: `auto` (mặc định: GPU nếu onnxruntime có provider GPU, nếu không thì CPU), `cpu`, `gpu` (không có GPU thì báo lỗi thay vì chạy CPU); model server có `--device` tương ứng;
- `LYNXX_API_KEY_<MODULE>` (ví dụ `LYNXX_API_KEY_RX=KEY,USER_ID`): khoá API cho CLI và cron khi không có `-api_key`. App luôn tự truyền `-api_key` từ khoá đã lưu. **Rule34 bắt buộc khoá riêng của người dùng**: Lynxx không có khoá dùng chung (khoá của tác giả bản gốc đã bị bỏ ngày 2026-11-18), thiếu khoá thì lõi dừng với lỗi `module 'RX' needs your own API key`;
- `LYNXX_SECRET_ARGS`: danh sách JSON các tham số bí mật (`["-api_key", "KEY,USER"]`) do app truyền cho CLI; CLI đọc xong thì xoá khỏi môi trường;
- `LYNXX_SECRET_MODULE_ARGS`: object JSON `{"db": ["-api_key", "KEY,LOGIN"]}`, khoá API theo module cho job nhập thư viện (`dlindex_tools import`); đọc xong thì xoá khỏi môi trường;
- `LYNXX_SELFTEST_SECURITY=1`: thêm kiểm tra bảo mật vào chế độ tự kiểm thử;
- `LYNXX_SELFTEST_DIR`: chạy tự kiểm thử Electron. Bước `generator` kiểm: khoá chỉ ghi (không lộ ra renderer, không có dạng chữ thường), phiên bị từ chối (R10), phiên cho site trong danh sách chặn tên miền bị từ chối (R7, lời nhắn tiếng Việt), cài từ catalog từ xa không nhận URL / sha256 từ renderer, không cài được bản nháp chưa đạt, cài cần xác nhận, dọn dẹp.
  Bước `indexTools` kiểm: `index.clean` / `index.cleanCancel` không gọi trực tiếp được qua `rpc`, dọn cần xác nhận (từ chối thì không chạy, chạy thử thì không hỏi), nhập thư viện chỉ với thư mục chọn từ hộp thoại và module hợp lệ, một job nhập chạy thử thật (lõi `index import --dry-run`) và thẻ job của nó, nhóm tải nhiều site (2–8 module, ép `-skip_known` / `-prefix`, site sau chờ site trước, renderer không tự tạo nhóm), thùng rác (`index.trash` / `index.restore` / `index.purgeTrash` / `index.forget` không gọi được qua `rpc`, đường dẫn không hợp lệ hoặc không có trong index bị từ chối, xoá → khôi phục → xoá vĩnh viễn với hộp xác nhận thay bằng hàm giả: từ chối thì không xoá gì, `settings.library.trashDays` chỉ nhận số nguyên 0–365), giao diện (Kiểm tra / Dọn index ở Cài đặt, hộp nhập thư viện, chọn site thêm, khung nhóm trong hàng đợi, chọn thẻ ở trang Đã tải → Xoá → Hoàn tác, Thùng rác; ảnh `20-…` tới `27-…`). Bước này cần Python của mã nguồn để ghi index mẫu (lõi đóng băng: phần thùng rác bỏ qua) và một hồ sơ tạm (`LYNXX_USER_DATA`): phần thùng rác dọn sạch thùng rác của hồ sơ nó chạy trên, nên với hồ sơ thật thì bỏ qua; số ngày tự dọn được đặt lại như cũ.
  Bước `contract.searchKinds` (cả với `LYNXX_SELFTEST_QUICK`, không gửi request tới site) kiểm kiểu tìm: `autoIdFilter` của main (`id:>N` chỉ khi module tìm được theo id, truy vấn không có kiểu listing, metatag dùng `:`); `autoHeld` (lịch "chỉ post mới từ bây giờ" không chạy khi truy vấn không còn lọc được theo id, `fromNow` suy ra cho lịch cũ); `tags.types` / `tags.autocomplete` theo `types` và `token` của lõi thật (danh sách db có trong `2tags/` khi chạy từ mã nguồn; lõi đóng băng không mang danh sách thì chỉ kiểm dạng kết quả); trong renderer, với bốn module giả (không request): không còn ô chọn Tìm theo (không có `#kind-picker` / `#kind-select`, không `select` nào trong thanh tìm, placeholder của ô nhập tag không đổi theo module, không còn khoá `lynxx.searchKind`), metatag theo module, token gõ tay thành chip đúng như đã gõ (`user:Some.Name`, `%artist:Ab.c%`, dấu cách kết thúc tag), chip "Người đăng: …", chip listing không có nút ±, bấm đúp sửa lại cả tag, `tags.validate` của lõi thật nhận `user:Some.Name` / `favorited_by:1` / `pool:5` trên db, nút ± của chip kiểu (dạng `%-…%`, Yêu thích / Pool của db viết thường và được lõi thật nhận, giá trị `id` phải là số, listing không đảo được), app không tự thêm listing ở dạng loại trừ, ô ngày bị tắt rồi bật lại (cả khi site thứ hai của "Tải thêm từ site khác" lấy kiểu đó theo thứ tự của site), chặn tải nhiều site, gợi ý Preview "chưa lọc cục bộ", `idSearch` false ("Theo tên file", "Tải đã chọn", tự tải lại), yêu cầu autocomplete (qua `rpc` giả): chữ bắt đầu bằng token của một kiểu gửi `types` và `token` của kiểu đó (cả `-artist:…`), kiểu `text` / `id` và giá trị dưới 2 ký tự không gợi ý, chữ khác không gửi `types` / `token`, dòng mang token (`artist:abc`) thành chip của kiểu; lỗi `tags.validate` tiếng Việt. Mọi thứ trên màn hình được trả lại như cũ.

Chế độ AI của trình tạo extension cần gói `anthropic`: `uv pip install --python .venv/bin/python 'anthropic>=2.9'`, hoặc `pip install "lynxx[ai]"`. Khoá Claude API nhập trong app (Cài đặt → AI tạo extension). Khoá được mã hoá bằng keyring của hệ điều hành; không có keyring thì app không lưu. Chế độ "Không dùng AI" không cần gói hay khoá này. Bản nháp nằm ở `<data>/extgen/drafts/`.

Exit code của CLI: `0` thành công, `1` lỗi, `2` sai tham số, `3` một số file lỗi, `130` bị huỷ. Script hoặc cron có thể dựa vào mã này.

## 3. Cập nhật danh sách tag (autocomplete, Tags A–Z)
**Nguồn danh sách** (`lynxx/tags_updater.py`, lệnh `lynxx-core tags` / `python -m lynxx.tags_updater`):
- 5 module đã duyệt: Rule34 (`rx`, cần khoá của bạn), Danbooru (`db`, dùng khoá nếu có), Xbooru (`xb`), Bbooru (`bb`), e621 (`en`).
- Extension mang job cập nhật của riêng nó (manifest `"tagUpdate": {"entry": "tags.py:read_tags"}`, `"minSdkVersion": "1.1"`; plan [ext-tag-jobs](plans/ext-tag-jobs.md), chốt với chủ repo ngày 2026-11-19):
  - Extension mang sẵn danh sách (`tagList`, file cạnh `manifest.json`). Job chỉ chạy khi người dùng yêu cầu: nút "Cập nhật" ở Tags A–Z, hoặc `lynxx-core tags --modules <abbr>`. Lịch hệ thống (`auto`), việc tự cập nhật khi mở app và việc tải ngay sau khi cài không chạy job.
  - `read_tags(job)` nhận `lynxx.sdk.TagJob` (`site`, `api_key`, `min_posts`, `get_text`, `get_json`, `log`) và yield `(tên, số post, loại)`. Request của job đi qua cùng `PoliteClient` với các nguồn khác: chỉ https tới site của extension, nhịp tối thiểu 1 giây, robots.txt áp chặt, Retry-After, giới hạn của khoá, ngân sách request, không cookie.
  - Lõi đổi khoảng trắng trong tên thành `_`, bỏ dòng sai, đưa loại lạ về `general`, lọc theo `--min-posts`. Job không cho tag nào thì không ghi gì.
  - Dòng không có loại (Lynxx 2.0.12 trở đi): job yield `(tên, số post)`, hoặc loại là `None`, `''` hay `unknown`. Lõi lấy cho dòng đó loại mà danh sách extension mang theo (`tagList`, file cạnh `manifest.json` đã cài) ghi cho đúng tên ấy, khi đó là loại cụ thể (`artist`, `character`, `copyright`, `metadata`, `species`, `lore`, `invalid1`). Nhờ vậy loại mà người bảo trì gán sẵn trong danh sách mang theo (`tools/type_tag_list.py`) không mất sau lần bấm "Cập nhật" đầu tiên. Ghi như trước (`general`; dòng `unknown` vẫn là `unknown`) khi danh sách mang theo không có tên đó, chỉ ghi `general` / `unknown` / `tag` hoặc không ghi loại, hoặc khi không có file, không đọc được file. Loại do job ghi, kể cả `general`, được giữ nguyên; loại lạ vẫn về `general`. Tên được so đúng từng ký tự, sau khi đổi khoảng trắng thành `_`. Danh sách mang theo chỉ được đọc một lần mỗi lần cập nhật, và chỉ khi có dòng không có loại; không bao giờ đọc cho bộ đọc theo engine hay 5 module đã duyệt. Với job gộp (`merge`), danh sách đang dùng vẫn được hỏi trước như cũ và loại cụ thể của nó được giữ; danh sách mang theo chỉ bổ sung cho dòng mà danh sách đang dùng không nói gì cụ thể (không có tên, `general` / `unknown`, dòng không ghi loại), và không đọc lại khi chính nó là danh sách đang dùng. Khi có dòng lấy loại như vậy, thông báo kết quả ghi thêm `N types taken from the list the extension ships`. Lõi cũ hơn ghi `general` (`unknown`) cho các dòng này, nên job của site không có loại tag có thể yield `(tên, số post)` cho mọi bản lõi.
  - Job thay cho bộ đọc theo engine; extension chưa có `tagUpdate` vẫn dùng bộ đọc theo engine (bảng dưới). Trình tạo extension không chạy `tags.py` của bản nháp (code bản nháp chỉ chạy trong sandbox), nhưng kiểm nó bằng luật tĩnh R10 và đóng gói nó vào zip.
  - Job chỉ đọc được các tag dùng nhiều thì khai báo thêm `"merge": {"minPosts": N}` (Paheal từ 1.0.3, N = 100; chỉ Lynxx 2.0.10 trở đi nhận khoá này). Lõi hỏi job với `job.min_posts` ≥ N rồi gộp lần đọc vào danh sách đang dùng: tag đọc được lấy số post mới, tag mới được thêm, mọi tag khác giữ nguyên. Dòng không ghi loại vẫn không ghi loại; loại `general` / `unknown` của lần đọc không đè loại cụ thể của danh sách. Danh sách học từ post không bao giờ là nền. Không ghi khi lần đọc rỗng, thiếu quá nửa số tag có từ N post của danh sách đang dùng, không có danh sách nào để gộp vào mà extension có khai báo `tagList`, hoặc danh sách đang dùng có dòng không đọc được (`--force` để ghi). Lần gộp ghi kích thước và giờ của file vào `<thư mục ra>/.meta/<abbr>_tags.json`, cùng sha256 của danh sách mà các lần gộp bắt đầu từ đó (lần gộp sau giữ nguyên giá trị này). Ở `<thư mục dữ liệu>/tags/`, file chỉ thay danh sách đi kèm extension khi có dấu này và sha256 khớp danh sách extension đang mang, không so giờ file: cài lại cùng danh sách (kể cả từ zip, file mang giờ lúc cài) vẫn dùng file đã gộp; bản mang danh sách khác thì danh sách đó được dùng và thành nền của lần gộp sau, kể cả khi giờ file cũ hơn. Trình tạo extension (`python -m lynxx.extgen tags`) báo `failed` cho bản nháp có job gộp mà chưa có danh sách cạnh manifest: app không bao giờ làm ra được danh sách đầu tiên bằng job đó.
- Mọi extension tạo từ một engine có danh sách tag. Updater đọc `$generator.engine` và `site` trong manifest; trình tạo extension (`lynxx.extgen`) ghi sẵn hai trường này, kèm `tagList: "<abbr>_tags.json"`.

| Engine | Cách đọc danh sách | Khoá |
|---|---|---|
| `gelbooru_dapi` | `index.php?page=dapi&s=tag&q=index`, phân trang theo số trang `pid` (danh sách của site không theo thứ tự id) | bắt buộc nếu manifest có `auth.apiKey` (site trả "Missing authentication"), nếu không thì dùng khi có (`API_KEY,USER_ID`) |
| `danbooru_json` | `tags.json`, con trỏ id `page=a<id>` | dùng khi có (`API_KEY,LOGIN`) |
| `moebooru_json` | `tag.json?order=name`, theo trang | không |
| `e621_json` | bản export hằng ngày: trang `db_exports` liệt kê `tags.csv.gz` và SHA-256 của nó; tải file (host con của site, ví dụ `static1.e621.net`), kiểm SHA-256 (sai thì không đọc). Trang không có link thì thử `db_export/tags-<ngày>.csv.gz` cũ (4 ngày gần nhất) | không |
| `philomena_json` | `api/v1/json/search/tags`: 10.000 tag dùng nhiều nhất (giới hạn cửa sổ tìm kiếm của site); dấu cách trong tên tag thành `_` | không |
| `szurubooru_json` | `api/tags/`, theo offset | không |
| `shimmie_api` | `api/danbooru/find_tags`: cả danh sách trong một câu trả lời | không |

- Engine `shimmie` (không có API) và `custom` không có nguồn cập nhật: danh sách của chúng được học dần (xem dưới).

**Loại tag suy theo tên** (`tools/type_tag_list.py`, offline, cho người bảo trì): site không ghi loại tag (Paheal: không trang nào, kể cả post, API và autocomplete, dev log 7bf; Rule34Hentai: trả bot challenge) thì danh sách đi kèm không có loại, chip artist / character / copyright của Tags A–Z không có gì. Công cụ chép loại của tag cùng tên từ các danh sách có loại:
```bash
python tools/type_tag_list.py --list <danh sách chưa gắn> --ref <danh sách rx có loại artist> --ref 2tags/db_tags.json --ref 2tags/rx_tags.json --conflicts skip --out <file ra>
python tools/type_tag_list.py --evaluate <danh sách có loại> --ref … # đo độ chính xác trên một danh sách có loại thật
```
- Chỉ dòng không có loại (hay `unknown`) được gắn; mọi dòng khác, số post, thứ tự giữ nguyên từng byte. Mặc định chỉ gắn `artist`, `character`, `copyright` (`general` sai ở nơi danh sách tham chiếu đầu gọi nghệ sĩ là `general`, `metadata` chỉ đúng 5–65 %).
- Danh sách tham chiếu đầu tiên trả lời được thì thắng; với `--conflicts skip`, tên mà hai danh sách tham chiếu gắn loại khác nhau thì không gắn (`chi-chi` là nghệ sĩ ở Rule34, nhân vật ở danh sách khác). Dòng tham chiếu loại khác, dưới `--min-ref-posts`, hay ít hơn số post của dòng cần gắn quá `--max-count-ratio` lần (mặc định 10.000) không trả lời, để danh sách sau còn trả lời được.
- Đo ngày 2026-11-21 (tag ≥ 20 post, tham chiếu rx có loại artist rồi db): Xbooru đúng 95,2 %, tìm được 85,1 %; Rule34.xyz 100 % / 98,7 %. Bảng đầy đủ ở docstring của công cụ.
- Paheal 1.0.4 và Rule34Hentai 1.0.3 mang `2tags/rp_tags.json` / `2tags/rn_tags.json` đã gắn loại theo cách này (dev log 7bf). Tag gộp thêm từ trang của Paheal và tag học từ post vẫn không có loại (`unknown`).
- Đăng catalog có hai bản này: đừng để `rp_tags.json` / `rn_tags.json` cũ (lấy từ zip của catalog trước 7bf, hay từ một lần gộp trên danh sách cũ) trong thư mục `--lists`: `build_catalog.py` ưu tiên file ở đó hơn `2tags/` và sẽ đóng danh sách không loại vào zip. Bỏ hai file đó (build lấy `2tags/`), hoặc gộp lại rp lên danh sách mới trước.
- `--retype general` (chỉ với danh sách tham chiếu là danh sách mới hơn của **cùng site**): dòng `general` cũng nhận loại khác trong `--types` mà danh sách đó ghi; các dòng khác như trên. Danh sách mà nguồn ghi nghệ sĩ là `general` lấy lại loại `artist` theo cách này:
  ```bash
  python tools/type_tag_list.py --list 2tags/rx_tags.json --ref <danh sách rx do updater ghi> --types artist --retype general --out 2tags/rx_tags.json
  ```
  `2tags/rx_tags.json` và `2tags/xb_tags.json` được gắn như vậy ngày 2026-11-21, mỗi danh sách với danh sách của chính site đó do updater ghi từ API làm tham chiếu duy nhất: 186.454 và 53.988 dòng `general` thành `artist`; số post, thứ tự, mọi dòng khác giữ nguyên (đổi lại `general` thì được đúng file cũ). Chỉ gắn nghệ sĩ: hai danh sách mới còn đổi dòng `general` thành nhân vật / series / metadata (5.753 dòng ở Rule34, 217 ở Xbooru, như `girlfriend` thành series, `side_view` thành metadata; e621 và Xbooru phần lớn không đồng ý), và gọi 1.796 nhân vật cũ là nghệ sĩ (`night_fury`, `chi-chi`; e621 gọi 76 % số đó là nhân vật), nên dòng `character` / `copyright` / `metadata` giữ loại cũ. Lệnh gắn rp / rn ở trên (`2tags/rx_tags.json` là tham chiếu thứ ba) cho đúng từng byte cùng kết quả với file cũ hay mới.
  - Chạy từ mã nguồn sau khi kéo commit này: app dùng file mới nhất của mỗi module giữa `<thư mục dữ liệu>/tags/` và `2tags/`, nên `2tags/rx_tags.json` / `xb_tags.json` vừa kéo về (giờ của lúc kéo) che danh sách rx / xb bạn đã cập nhật, tới lần bấm "Cập nhật" rx / xb sau ở Tags A–Z.
- Danh sách được ghi vào `<thư mục dữ liệu>/tags/<abbr>_tags.json`. App tìm danh sách theo tên file (file mới nhất của mỗi module), nên autocomplete và Tags A–Z dùng được ngay sau khi cập nhật.

**Extension mới có danh sách tag ngay** (yêu cầu của chủ repo, 2026-11-18):
- Engine có API danh sách tag và robots.txt cho phép: trình tạo tải luôn danh sách lúc bản nháp đạt (`lynxx/extgen/taglist.py`), cả chế độ AI, chế độ template và skill (`python -m lynxx.extgen new` / `tags`).
  - File `<abbr>_tags.json` nằm cạnh `manifest.json` (trường `tagList`), được đóng vào file .zip và nằm lại trong thư mục extension khi cài. Lõi dùng nó ngay sau khi cài.
  - Giới hạn cho bản nháp: 30 request, 90 giây, 8 MB. Danh sách lớn hơn, hoặc site cần khoá của người dùng, thì app tải ngay sau khi cài (`tags.update`), bằng khoá đã lưu nếu có.
  - Đọc danh sách cũng theo đúng luật lịch sự ở dưới (robots.txt nghiêm ngặt, `Crawl-delay`, `Retry-After`). Báo cáo phiên ghi kết quả ở `tagList` (`bundled`, `later`, `learned`, `failed`).
  - Eval harness của trình tạo (`python -m lynxx.aigen.eval`) không tải danh sách tag.
- Site không có API danh sách tag (HTML tuỳ biến, Shimmie không có API) hoặc robots.txt cấm: app **học dần** danh sách (`lynxx/tags_learned.py`), không gửi thêm request nào.
  - Nguồn: tag của các post hiện trong Preview (`search.preview`) và của các file đã tải (index các lần tải; app gọi `tags.learn` sau mỗi lượt tải xong, hoặc nút "Học từ file đã tải" ở Tags A–Z).
  - Mỗi post chỉ đếm một lần. File ghi dạng `"tag": "N posts (unknown)"`, với N là số post đã thấy ở đây, không phải số trên site; loại tag không biết.
  - Trạng thái ở `<thư mục dữ liệu>/tags/learned/<abbr>.sqlite`.
  - Chỉ học khi module chưa có danh sách nào từ nguồn (trừ hai trường hợp dưới: danh sách đi kèm không có nguồn, danh sách của job gộp). Khi updater, extension có nguồn tag hoặc `2tags/` cho module một danh sách thật, việc học dừng và không ghi đè danh sách đó. Danh sách thật được dùng kể cả khi file của nó cũ hơn danh sách đã học.
  - Danh sách đi kèm extension không có nguồn tag (Rule34Hentai, Rule34.us từ v1.0.2; Paheal ở 1.0.2) không bao giờ được cập nhật, nên việc học vẫn chạy trên nó: `<thư mục dữ liệu>/tags/learned/<abbr>_added.json` chỉ giữ các tag học được mà danh sách đi kèm chưa có, autocomplete và Tags A–Z dùng danh sách đi kèm cộng các tag đó (số post của danh sách đi kèm giữ nguyên). File này không mang tên `<abbr>_tags.json` nên không bao giờ thay chỗ danh sách đi kèm, kể cả khi trạng thái học bị mất (thoát app lúc đang ghi, chép hồ sơ, xoá thư mục `learned/`).
    - Danh sách đi kèm không bị ghi lại hay đọc lại sau mỗi Preview (Rule34.us: 18,9 MB, khoảng 544 nghìn tag): chỉ tag của các post mới được tra trong nó. File học thêm chỉ được ghi lại (và app chỉ được báo) khi có tag mới mà danh sách đi kèm chưa có, hoặc số post của một tag học thêm tăng; trang Preview chỉ có tag mà danh sách đã có thì không ghi gì.
    - Cập nhật extension: tag mà danh sách mới đã có bị bỏ khỏi phần học thêm. Gỡ extension: danh sách học được (đủ mọi tag đã học) lại là danh sách của module, trừ khi `<thư mục dữ liệu>/tags/` có danh sách khác của module (ví dụ danh sách rp đã gộp): danh sách ấy được dùng, việc học dừng tới khi cài lại. Cài lại Paheal mang cùng danh sách thì danh sách đã gộp vẫn được dùng, tag học thêm lại nằm trên nó.
  - Module có job gộp (Paheal từ 1.0.3) cũng được học thêm: trên danh sách đi kèm trước lần "Cập nhật" đầu, trên file đã gộp sau đó. Sau mỗi lần cập nhật, tag học thêm nào danh sách nay đã có bị bỏ khỏi file học thêm và lấy số post của site.

**Lịch sự** (mọi nguồn):
- **Nhịp request:** khoảng cách giữa hai request là `--delay` (mặc định 0,5 giây). Bộ cập nhật dùng giá trị lớn nhất trong các mức sau, tối đa 60 giây:
  - site của extension: ít nhất 1 giây;
  - giới hạn của khoá API: Rule34 công bố 60 request mỗi 60 giây cho mỗi khoá, nên 1,05 giây (xem "Giới hạn theo khoá" bên dưới);
  - `Crawl-delay` trong robots.txt.
- **Gặp 429 / 503:**
  - chờ theo `Retry-After`; nếu site không gửi thì chờ 5, 15, 30, 60, 120, 180 giây (tổng khoảng 7 phút), rồi mới dừng danh sách;
  - phần còn lại của lượt tải chạy chậm hơn: theo nhịp site ghi trong phản hồi ("limit of N requests every M second(s)"), không thì chậm 1,5 lần;
  - lỗi mạng thử lại tối đa 5 lần;
  - HTTP 502 / 504 (proxy hay gateway trước site lỗi một lần) cũng được thử lại như lỗi mạng: chờ 5, 10, 15, 20 giây, tối đa 5 lần mỗi request, rồi mới dừng danh sách. Trước 2.0.11 một lần 502 làm dừng cả danh sách (rule34video.com trả một lần 502 giữa chừng ngày 2026-11-21, lần sau trang đó trả bình thường);
  - mỗi lần chờ hoặc thử lại đều hiện trong tiến độ (app và log), ví dụ `api.rule34.xxx asks to wait (HTTP 429): waiting 5 s, then one request every 1.05 s`.
- **Giới hạn theo khoá** (Rule34, và extension khai báo `auth.rateLimit` trong manifest):
  - Mặc định là giới hạn site công bố (`RATE_LIMIT_RX` trong `lynxx/defines.py`, `auth.rateLimit` trong manifest).
  - Admin của site cấp thêm cho khoá của bạn thì nhập số mới ở **Cài đặt → API key theo module**, ô "Giới hạn của khoá" ngay dưới khoá (request mỗi phút; trống hoặc 0 = giới hạn site công bố).
  - App ghi số đó vào `<thư mục dữ liệu>/ratelimit/limits.json` (`{"rx": 120}`). Mọi tiến trình lõi đọc file này nên dùng số mới ngay, kể cả sidecar đang chạy. Dòng lệnh / cron: biến `LYNXX_RATE_LIMIT_RX=120` thắng file.
  - Giới hạn tính theo khoá, nên mọi tiến trình dùng chung khoá giữ chung một nhịp qua file khoá `<thư mục dữ liệu>/ratelimit/<băm>.ts`: các job tải, Preview, cập nhật danh sách tag. Tải ảnh trong lúc cập nhật tag không còn làm vượt giới hạn. Trang yêu thích / pool của Rule34 là trang HTML của site chính, không tính vào giới hạn API.
  - Nhập số cao hơn mức site thật sự cho thì site trả 429: lõi tự chậm lại theo câu báo của site, chờ rồi tải tiếp.
- **Rule34 mất lâu:** danh sách đầy đủ khoảng 1.620 trang (1.000 tag mỗi trang, site không cho trang lớn hơn), khoảng 1,6 triệu tag.
  - Site trả tag theo thứ tự lưu trong database: tag vừa đổi số post bị dời xuống cuối, nên các tag phổ biến (`female`, `breasts`, `1girls`…) chỉ có ở **vài trang cuối**. Danh sách dở thiếu đúng những tag hay dùng nhất, vì vậy không bao giờ được dùng.
  - Bộ đọc chỉ dừng ở trang rỗng: trang có ít hơn 1.000 tag (ví dụ có tag tên rỗng) không phải trang cuối.
  - Trang càng sâu site trả càng chậm (trang 5: 0,5 giây, trang 1.200: 4 giây, trang 1.600: 6–7 giây). Một lần cập nhật đầy đủ mất khoảng 1,5–2 giờ. Giới hạn của khoá chỉ là nút thắt ở khoảng 100 trang đầu (site trả nhanh hơn 1 giây), nên khoá được cấp thêm request gần như không làm việc này nhanh hơn; nó giúp nhiều hơn cho việc tải ảnh.
  - Đóng app giữa chừng không sao: lần sau tải tiếp từ trang đã dừng.
- **Rule34: chỉ đọc phần cuối danh sách** (bản sau 2.0.6; `TagSource.tail_updates`):
  - Các lần cập nhật sau một lần đọc đầy đủ chỉ đọc **phần cuối danh sách**. Lý do: tag vừa đổi số post bị dời xuống cuối và nằm liền thành một khối. Đo ngày 2026-11-19, 4 giờ sau một lần đọc đầy đủ: 8.867 trong 8.870 tag đổi số post nằm ở 10 trang cuối, mỗi trang khoảng 960/1.000 dòng đổi; các trang trước đó gần như không đổi.
  - **Đọc lùi:** bắt đầu từ trang cuối của lần đọc trước, dừng khi gặp 3 trang liền "yên" (tối đa 1% tag đổi số post). Ít nhất 5 trang, nhiều nhất 800 trang.
  - **Đọc tiếp phần mới:** sau đó đọc từ trang cuối cũ tới trang rỗng, để lấy các dòng mới thêm, kể cả các dòng bị dời xuống cuối trong lúc đang đọc.
  - **Gộp:** số post đọc được thay số cũ; tag mới được thêm; tag không thấy giữ nguyên tới lần đọc đầy đủ sau.
  - **Đọc đầy đủ** khi: chưa từng đọc đầy đủ (cần `<thư mục tags>/.meta/rx_tags.json`), lần đọc đầy đủ đã quá 30 ngày, có một lần đọc đầy đủ đang dở, hoặc khi người dùng yêu cầu. Yêu cầu từ app: Tags A–Z → **Đọc lại toàn bộ**; từ dòng lệnh: `--full`.
  - **Chỉ lần đọc đầy đủ thấy được:**
    - tag mới tạo giữa danh sách (đo được 12 tag mới trong 4 giờ, có cái không nằm ở cuối);
    - tag mà lần đọc đầy đủ trước đã sót. Đọc hơn 1 giờ thì các dòng bị dời chỗ trong lúc đọc, nên lần đọc ngày 18/11 sót khoảng 1 tag mỗi trang;
    - tag giảm về 0 post.
- **Tải tiếp sau khi dừng giữa chừng** (Gelbooru: theo số trang `pid`; Danbooru: theo con trỏ id):
  - tiến độ được lưu ở `<thư mục tags>/.partial/<module>_tags.json` sau mỗi 20 trang và ngay khi dừng (429, mạng, đóng app);
  - lần cập nhật sau, trong vòng 3 ngày, tải tiếp từ con trỏ đó; xong thì xoá file tiến độ;
  - danh sách dở không bao giờ được dùng làm danh sách thật.
- Mỗi danh sách có ngân sách request `--max-requests` (mặc định 5000, tính cả robots.txt và các lần thử lại). Hết ngân sách thì dừng và không ghi danh sách dở.
- Không gửi cookie. Gặp bot challenge (`cf-mitigated: challenge`) thì dừng.
- Chỉ dùng https, chỉ gọi host của site (và tên miền cha), chỉ địa chỉ công khai, không gọi site trong danh sách chặn tên miền.
- Chỉ dùng khoá của chính bạn: `--api-key`, biến `LYNXX_API_KEY_<MODULE>`, hoặc khoá đã lưu trong app. Lynxx không có khoá dùng chung. Khoá được che (`<REDACTED>`) trong log và thông báo lỗi.
- robots.txt được đọc theo RFC 9309 (`lynxx/robots.py`): luật dài nhất thắng, hiểu `*` và `$`, nhóm của `Lynxx` được ưu tiên hơn nhóm `*`.
  - Site của extension: **tuân thủ nghiêm ngặt.** Nếu robots.txt chặn endpoint danh sách tag thì không cập nhật và báo lỗi rõ. Nếu không đọc được robots.txt (5xx, lỗi mạng) thì coi như bị chặn hết.
  - **Quyết định 2026-11-18 (chủ repo, phương án a — ngoại lệ cho 5 site cũ):** Rule34, Danbooru, Xbooru, Bbooru, e621 là ngoại lệ có ghi rõ. Chúng dùng API chính thức của site, nên các dòng `Disallow` không được áp dụng; `Crawl-delay`, `Retry-After` và ngân sách request vẫn được tôn trọng. Lý do: robots.txt của Danbooru có `Disallow: /` và `Disallow: /*.json` cho mọi user-agent, nếu tuân thủ nghiêm thì `db` không cập nhật được nữa.
- Sanity check giữ nguyên: nếu danh sách mới ít hơn một nửa danh sách đang dùng thì không ghi (dùng `--force` để ghi). Job gộp có luật riêng (xem trên).

**Trong app** (Tags A–Z → Danh sách tag):
- Bảng liệt kê mọi extension đã cài, có cột "Nguồn" (rê chuột để xem site, cách đọc, có tuân thủ robots.txt không).
  - Module có nguồn: có nút "Cập nhật". Với extension có job riêng, nút này là cách duy nhất để chạy job.
  - Khi đang cập nhật (bấm ở đây, tự cập nhật khi mở app, hoặc extension vừa cài), dòng đó ghi "đang cập nhật… N tag" (khoảng 2 giây cập nhật một lần) và nút thành "Đang cập nhật…" (tắt) tới khi xong, kể cả khi chuyển trang hay tải lại cửa sổ: bảng hỏi lõi (`tags.sources`, `running`).
  - Module bắt buộc khoá mà chưa có khoá: ghi "cần API key" (nhập ở Cài đặt → API key).
  - Module không có nguồn: cột "Nguồn" ghi "học từ post đã xem / đã tải", có nút "Học từ file đã tải"; cột dung lượng ghi "(đã học)" khi danh sách là danh sách học được. Chưa học được tag nào (thường là extension tự tạo cho site không có danh sách tag dùng được, ví dụ robots.txt cấm API tag của site) thì cột "Cập nhật" ghi "chưa học tag nào": xem Preview hoặc tải vài post của site để Lynxx học.
  - Module có danh sách đi kèm extension nhưng không có nguồn (Rule34Hentai, Rule34.us; Paheal tới 1.0.2): cột "Nguồn" ghi "đi kèm extension", cột "Cập nhật" ghi "theo extension v<phiên bản>" (không ghi giờ: file giữ giờ của lúc build extension, không phải lúc cài hay ngày của dữ liệu), cột "File" thêm "+N tag học thêm" khi đã học được tag mà danh sách chưa có; có nút "Học từ file đã tải". Module không có nguồn mà danh sách nằm ở chỗ khác (ví dụ `2tags/` của repo khi chạy từ mã nguồn) ghi "không có nguồn".
    - Nút "Học từ file đã tải" báo rõ lý do khi không học thêm được: index chưa có post nào của site ("Chưa có post nào của … trong index: tải vài post (hoặc xem Preview) để Lynxx học tag"), module đã có danh sách từ nguồn (không cần học), hoặc không có post đã tải mới.
    - Rule34Hentai, Rule34.us (Paheal tới 1.0.2): catalog đi kèm app có sẵn danh sách của chúng (từ v1.0.2). Bản 1.0.1 đã cài thấy "có bản v1.0.2" ở trang Extensions; cập nhật là có danh sách.
  - Paheal 1.0.3: cột "Nguồn" ghi "Rule34 Paheal" (rê chuột: job chỉ đọc các tag có từ 100 post rồi gộp). Có nút "Cập nhật": 2 request, cách nhau 3 giây theo `Crawl-delay` của site. Trước lần bấm đầu, cột "Cập nhật" ghi "theo extension v1.0.3"; sau đó ghi giờ của lần gộp (rê chuột: tag site liệt kê ở mức từ 100 post mang số post lúc đó, tag khác giữ số của danh sách trước; chú thích này chỉ có khi lõi báo danh sách là của lần gộp, `tags.status` `merged`). Chạy từ mã nguồn thì catalog của repo không có danh sách cạnh manifest (`.gitignore`), danh sách đang dùng là `2tags/rp_tags.json`: tới lần bấm đầu, cột ghi giờ của file đó, không kèm chú thích. Cột "File" vẫn có "+N tag học thêm". Không có nút "Học từ file đã tải": việc học tự chạy sau mỗi lượt tải và khi xem Preview. Thông báo khi xong là dòng của lõi: `written <N> tags (was <M>, 2 requests, the tags with 100+ posts merged into the list in use: <đọc> read, <mới> new, <đổi số> updated, <không còn trong trang> no longer listed kept)`.
  - Module có nguồn mà danh sách đang dùng vẫn là danh sách đi kèm extension (extension có job riêng, ví dụ các extension của PR #37, hoặc extension của trình tạo; chưa bấm "Cập nhật" ở máy này, `tags.status` `shipped`): cột "Cập nhật" cũng ghi "theo extension v<phiên bản>", không ghi giờ của file; nút "Cập nhật" vẫn còn. Sau lần cập nhật đầu tiên, danh sách đang dùng là file ghi ở `<thư mục dữ liệu>/tags/` và cột ghi giờ của lần cập nhật đó.
  - Cài một extension: nếu chưa có danh sách đi kèm mà extension có nguồn, app bắt đầu tải ngay. Extension có job riêng thì không: job chỉ chạy khi bấm nút.
- "Tự cập nhật mỗi N ngày khi mở app": Rule34 và Danbooru, bằng khoá đã lưu; Rule34 bị bỏ qua khi chưa có khoá.
- "Cập nhật theo lịch của hệ thống, kể cả khi app đóng" (mỗi tuần vào thứ Hai, hoặc mỗi ngày, lúc khoảng 4 giờ sáng):
  - Linux và macOS: app thêm hoặc xoá một khối có đánh dấu `# BEGIN lynxx-tags` … `# END lynxx-tags` trong crontab của người dùng, không đụng các dòng khác.
  - Windows: tác vụ `Lynxx tag lists` của Task Scheduler.
  - Bản AppImage không hỗ trợ, vì lõi chỉ tồn tại khi app chạy.
- Lịch chạy `lynxx-core tags --modules auto --out <thư mục dữ liệu>/tags --min-posts 2 --log <thư mục dữ liệu>/logs/tags-schedule.log`; bản phát triển chạy `python -m lynxx.tags_updater`.
  - `auto` là mọi module đã cài có nguồn tag và không bắt buộc khoá: db, xb, bb, en (nếu đã cài) và các extension tạo từ engine. Extension có job riêng (`tagUpdate`) không nằm trong `auto`.
  - Danh sách module được tính lúc lịch chạy, nên extension cài sau cũng được cập nhật mà không cần ghi lại mục lịch.
- Lịch không đọc được khoá đã mã hoá của app, nên module bắt buộc khoá (Rule34, extension Gelbooru cần khoá) không chạy theo lịch.
- App ghi lại mục lịch mỗi lần mở, nên đường dẫn lõi luôn đúng sau khi cập nhật app.

**Dòng lệnh** (bản checkout repo):
```bash
.venv/bin/python -m lynxx.tags_updater --modules auto            # như lịch của app
.venv/bin/python -m lynxx.tags_updater --modules fq --delay 1    # một extension, theo abbr
.venv/bin/python -m lynxx.extgen tags <thư mục bản nháp>         # tải danh sách tag vào một bản nháp
```

**Không qua app** (bản checkout repo, danh sách ghi vào `2tags/` của repo):
```bash
chmod +x tools/cron/lynxx-update-tags.sh
crontab -l 2>/dev/null | cat - tools/cron/crontab.example | crontab -    # thứ Hai 04:17
```
Hoặc dùng systemd user timer:
```bash
mkdir -p ~/.config/systemd/user && cp tools/systemd/lynxx-update-tags.* ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now lynxx-update-tags.timer
```
Biến môi trường của script:
- `LYNXX_TAG_MODULES` (mặc định `rx,db`);
- `LYNXX_TAG_MIN_POSTS` (mặc định 2);
- `LYNXX_API_KEY_RX` (`KEY,USER_ID`): bắt buộc cho Rule34, Lynxx không có khoá dùng chung;
- `LYNXX_API_KEY_DB` (`API_KEY,LOGIN`);
- `LYNXX_TAGS_OUT`.

Log ghi ở `~/.local/state/lynxx/update-tags.log`. Chạy tay: `.venv/bin/python tools/update_tags.py --modules db --delay 0.5`.

**Kiểu tìm** (2.0.11; ô chọn "Tìm theo" giữa nút module và ô nhập tag chỉ có ở 2.0.11, từ 2.0.12 không còn): kiểu tìm do manifest khai báo (khoá `search`), gõ thẳng bằng token trong ô nhập tag hoặc thêm bằng nút + của Tags A–Z. Rule34 và Danbooru có Người đăng (`user:`, giữ chữ hoa và dấu chấm), Yêu thích của người dùng (`favorited_by:`; Rule34 nhập id người dùng, Danbooru nhập tên bằng chữ thường) và Pool (`pool:`). Extension có trang danh sách riêng (nghệ sĩ, chuyên mục, tìm theo từ khoá) khai báo kiểu listing (`artist:`, `category:`, `search:`): tag khác trong truy vấn được lọc cục bộ, kết quả theo thứ tự của site, ô ngày bị tắt, không dùng nhóm OR, mỗi truy vấn một listing, không loại trừ được. Gõ token của một kiểu nhận tên tag rồi ít nhất hai ký tự (`artist:se`) thì gợi ý chỉ lấy giá trị của kiểu đó; chip hiện nhãn của kiểu ("Nghệ sĩ: …"). Tag gõ tay được thêm đúng như đã gõ (app không tự đổi chữ thường hay bọc `%…%`). Nghệ sĩ / nhân vật / series ở các site khác là tag thường: gõ như mọi tag, hoặc lọc ở Tags A–Z bằng chip loại (đoạn dưới); danh sách tag không ghi loại (học từ post; Paheal tới 1.0.3, Rule34Hentai tới 1.0.2) thì các chip đó bị tắt. App không còn gọi `tags.kinds`; lựa chọn cũ của ô chọn (`localStorage`) được xoá khi app khởi động. Paheal 1.0.4 và Rule34Hentai 1.0.3 mang danh sách đã gắn loại (xem "Loại tag suy theo tên" ở mục 3). Danh sách `2tags/` của Rule34 và Xbooru (khi chạy từ mã nguồn) có loại `artist` từ 2026-11-21 (`--retype general`, xem "Loại tag suy theo tên" ở mục 3); trước đó chúng ghi nghệ sĩ là `general`.

**Chip loại tag ở Tags A–Z** (2.0.11, `tags.types`): chip của loại mà danh sách tag đang dùng không có tag nào bị tắt (mờ, viền đứt; rê chuột: "Danh sách tag đang dùng của <ABBR> không có tag loại …"), chip khác ghi số tag khi rê chuột. Danh sách có loại khác (species, lore của e621) có thêm chip cho loại đó; danh sách có dòng không ghi loại có chip "chưa rõ loại". Đổi module thì chip đang chọn mà danh sách mới không có được bỏ chọn. Không có tag nào: chân trang ghi rõ loại, tiền tố và số post đang lọc (ví dụ "Không có tag loại artist bắt đầu bằng “x”"); "Chưa có danh sách tag cho module này" chỉ khi module thật sự chưa có danh sách (Gelbooru: bấm "Cập nhật" với khoá của bạn). Dòng `artist:<tên>` / `category:<tên>` của site video (rule34video, rule34gen 1.1.0) nằm dưới chip artist / copyright, số post là 1 vì trang của site không ghi số; nút + thêm đúng kiểu tìm đó.

## 3b. Index các lần tải
- File: `<data dir>/index/downloads.sqlite` (kèm `-wal`, `-shm` khi đang có tiến trình mở). Mỗi file tải xong ở chế độ `full` được ghi một dòng (module, id, md5, đường dẫn, tags, score, rating, ngày post, thời điểm tải). Không đọc nội dung file, không gửi dữ liệu ra ngoài.
- Bỏ qua post đã tải: CLI `-skip_known`; app: ô "Bỏ qua file đã tải" trong tuỳ chọn tải, mặc định ở Cài đặt → Index các lần tải. Post chỉ bị bỏ qua khi file đã ghi trong index vẫn còn trên đĩa và không nằm trong thùng rác, nên xoá file (trên đĩa hoặc ở trang Đã tải) là cách tải lại.
  ```bash
  .venv/bin/python -m lynxx -module db -skip_known -path ~/Downloads/Lynxx -- cat_ears rating:g
  ```
- Nhiều job ghi cùng lúc an toàn (WAL, chờ khoá tối đa 10 s, mỗi lô thử lại 3 lần). Nếu một lô vẫn không ghi được, log có `Warning (W2): download index: …` và lượt tải không bị ảnh hưởng.
- Xem nhanh bằng `sqlite3`: `sqlite3 ~/.config/Lynxx/index/downloads.sqlite "SELECT module, COUNT(*) FROM files GROUP BY module"`. Sidecar: `{"jsonrpc":"2.0","id":1,"method":"index.stats"}`, `{"jsonrpc":"2.0","id":2,"method":"index.search","params":{"tags":"cat_ears -rating:e","pageSize":20}}`.
- Sao lưu: copy file khi không có job nào chạy, hoặc `sqlite3 <file> ".backup <đích>"`. Làm lại từ đầu: đóng app và mọi job rồi xoá cả ba file `downloads.sqlite*`; index được tạo lại ở lần tải sau.
- **Dọn index** (bỏ dòng có file đã mất, không đụng tới file): app: Cài đặt → Index các lần tải → **Kiểm tra** (chỉ đếm) rồi **Dọn index…** (hộp xác nhận; ô "Xoá cả dòng có thư mục không còn" mặc định tắt: cắm ổ ngoài / kết nối thư mục mạng trước khi dọn). Chạy nền, dừng được, job tải vẫn chạy bình thường. CLI:
  ```bash
  .venv/bin/python -m lynxx.dlindex_tools clean --dry-run
  ```
  Bỏ `--dry-run` để xoá; thêm `--missing-folders` để xoá cả dòng có thư mục đã mất; `--json` in kết quả dạng JSON.
- **Nhập thư viện có sẵn**: app: Cài đặt → Index → **Nhập thư mục…** (hoặc trang Đã tải → **Nhập thư viện…**), chọn thư mục bằng hộp thoại, rồi theo dõi job ở trang Tải xuống; nút **Danh sách** trên thẻ job liệt kê tên không nhận ra, post không có trên site, request lỗi. CLI (mỗi lần chạy chỉ tra những file chưa có trong index, nên chạy lại để thử tiếp phần lỗi):
  ```bash
  .venv/bin/python -m lynxx.dlindex_tools import ~/Pictures/booru --module db --dry-run
  ```
  `--module`: module cho tên không có tiền tố (`12345.jpg`, `<md5>.jpg`), chỉ dùng khi cả thư mục là file của một site; `--no-recursive`; `--verify-md5` (đọc từng file, chỉ nhập file trùng md5 với file gốc trên site); `--dry-run` (chỉ nhận tên, không request); `--json`. Tốc độ: Danbooru 100 post mỗi request, site khác 1 post mỗi request, tối đa 2 request mỗi giây (10 000 file rx ≈ 1,5 giờ). Khoá API của module lấy từ `-api_key` trong `LYNXX_SECRET_MODULE_ARGS` (app tự truyền).
- **Re-tag từ index**: GUI Tk (Tools → Re-tag files…) hoặc CLI:
  ```bash
  .venv/bin/python -m lynxx.dlindex_tools retag ~/Downloads/Lynxx --source index
  ```
  `--source both` (mặc định: index trước, file info JSON sau), `--types artist,character` (theo loại tag, cần danh sách tag), `--module` (bộ lọc tag của module khi `--types auto`). Exit code `3` khi có file không tìm được info. Re-tag / un-tag / sắp xếp file trong GUI Tk tự cập nhật đường dẫn trong index.
- **Xoá file, thùng rác** (trang Đã tải): bấm thẻ để chọn (Shift + bấm: chọn một dải; **Chọn cả trang**), bấm đúp để xem. **Xoá (N)** hoặc phím Delete (Mac: phím delete) chuyển file vào thùng rác, thông báo có nút **Hoàn tác**. **Thùng rác (N)** liệt kê file đã xoá (vị trí cũ, "còn X ngày"): **Khôi phục** đưa file về chỗ cũ (thư mục đã mất thì tạo lại; tên đã có file khác thì thêm `_1`, `_2`…), **Xoá vĩnh viễn** và **Dọn sạch thùng rác…** xoá file khỏi đĩa sau hộp xác nhận. Thẻ "đã mất" (file không còn trên đĩa): **Xoá** chỉ bỏ dòng khỏi index, cũng có xác nhận.
  - Thùng rác do Lynxx quản lý, không phải thùng rác của hệ điều hành. File chỉ được đổi tên thành `<id dòng>_<tên>`, không bao giờ chép sang ổ khác: vào `<data dir>/trash/` nếu cùng ổ với file; nếu không, vào `.lynxx-trash` ở gốc thư mục tải (Cài đặt → Tải xuống mặc định → Thư mục) nếu file nằm trong đó, cùng ổ; nếu không nữa thì `.lynxx-trash` cạnh file. Nhập thư viện bỏ qua thư mục ẩn nên không nhập lại file trong thùng rác.
  - Trong index, dòng của file trong thùng rác giữ nguyên metadata: `path` là chỗ file đang nằm, `orig_path` là chỗ cũ, `trashed_at` là lúc xoá. File trong thùng rác không còn tính là đã tải (`-skip_known`, tìm ở trang Đã tải, số liệu, tag học từ file đã tải); tải lại post đó thì có một dòng mới. Dọn index bỏ cả dòng thùng rác có file đã bị xoá bằng tay.
  - Tự dọn: Cài đặt → Index các lần tải → **Tự dọn thùng rác sau** 7 / 14 / 30 (mặc định) / 60 / 90 ngày hoặc Không tự xoá (`settings.library.trashDays`, 0 = không tự xoá). App xoá file quá hạn 60 giây sau khi lõi sẵn sàng, sau đó tối đa 12 giờ một lần, và ngay khi số ngày được giảm. File cài đặt bị hỏng, hoặc số ngày trong đó không hợp lệ (sửa tay): app chuyển sang Không tự xoá và báo, chứ không dùng mặc định 30 ngày, cho tới khi người dùng chọn lại. CLI:
    ```bash
    .venv/bin/python -m lynxx.dlindex_tools trash list
    .venv/bin/python -m lynxx.dlindex_tools trash purge --days 30
    ```
    `--days 0` hoặc `--all`: xoá mọi file trong thùng rác; `--json`. Exit code `3` khi có file không xoá được (đang mở). Sidecar: `index.trash`, `index.restore`, `index.purgeTrash`, `index.forget` (app chỉ gọi từ tiến trình chính, renderer không gọi trực tiếp được), `index.search` với `"trash": true`.
  - Schema index lên 3 (thêm cột `trashed_at`, `orig_path`, `moving_to`): index cũ được nâng cấp tại chỗ ở lần mở đầu tiên, dòng cũ giữ nguyên. Bản app cũ vẫn đọc được index mới, nhưng hiện file trong thùng rác như file thường.
  - Mỗi lượt chuyển (tối đa 200 file) ghi chỗ đến của từng file vào `moving_to` trước khi đổi tên, rồi đổi tên và cập nhật dòng trong cùng một giao dịch giữ khoá ghi (dọn, quên dòng, lượt chuyển khác chờ tới khi xong). App tắt hay lõi bị dừng giữa chừng: lần sau mở trang Đã tải (hoặc xoá, khôi phục, dọn thùng rác), dòng đi theo file đã tới chỗ mới, file chưa đi thì giữ nguyên.
  - Thư mục thùng rác không truy cập được (ổ ngoài đã rút, ổ mạng chưa gắn): dọn thùng rác không bỏ dòng của các file trong đó (báo "không xoá được"), cắm lại thì lần dọn sau xoá như thường; "Xoá khỏi thư viện" cũng không bỏ các dòng đó.
- **Tải nhiều site**: Tìm kiếm → Bộ lọc & tuỳ chọn tải → **Tải thêm từ site khác**, chọn thêm site rồi bấm **Tải xuống (N site)**. Mỗi site một job, chạy lần lượt, luôn bỏ qua file đã tải và đặt tiền tố module cho tên file; post trùng md5 với file site trước đã tải thì không tải lại. Thử truy vấn trên từng site bằng Preview trước: tên tag, metatag và alias khác nhau giữa các site. Truy vấn có tag của một kiểu tìm (ví dụ `artist:` của một site có trang người mẫu) mà site được thêm không có kiểu đó thì app từ chối và ghi lý do trong khung cảnh báo.

## 3c. Model nhận diện giới tính (`-gender_mode model`)
Model (~361 MB) chạy ở **model server** riêng; app và CLI chỉ gọi API nên bản app không kèm numpy / onnxruntime / Pillow / model. Lần đầu chạy server tự tải model từ Hugging Face `SmilingWolf/wd-vit-tagger-v3` (revision ghim, kiểm sha256). Mỗi ảnh ~0,4 s trên CPU (Ryzen, 4 luồng).

Server nạp model khi có ảnh đầu tiên (hoặc khi app bật model, `--preload`) và giải phóng sau 15 phút không dùng (`--idle-unload 900`, `0` = giữ mãi). Model chạy trong một tiến trình con; giải phóng là tiến trình con thoát, nên RAM và VRAM được trả hết. Đo trên máy dev: server chưa nạp ~30 MB; model trên CUDA ~530–850 MB RAM và ~1,3 GB VRAM; giải phóng xong VRAM về 0. Log khởi động có dòng `hardware:` (GPU, VRAM trống, onnxruntime có GPU hay không), mỗi lần nạp có `model loaded on <cpu|cuda> in … s`.

- **Trên máy này** (app mặc định gọi `http://127.0.0.1:8765`, không cần token):
  ```bash
  uv pip install --python .venv/bin/python 'numpy>=1.26' 'onnxruntime>=1.18' 'pillow>=10'
  ```
  ```bash
  .venv/bin/python -m lynxx.model_server
  ```
  Chạy nền theo user: `cp tools/systemd/lynxx-model-server.service ~/.config/systemd/user/ && systemctl --user enable --now lynxx-model-server`.
- **GPU (VRAM)**: model chạy trên GPU khi onnxruntime của server là bản GPU. NVIDIA: `onnxruntime-gpu` 1.32 dùng CUDA 13 nên cần driver 580 trở lên (`nvidia-smi` ghi "CUDA Version: 13.0"). Thay `onnxruntime` bằng `onnxruntime-gpu` kèm CUDA / cuDNN từ pip (`.venv` tăng từ 227 MB lên 2,6 GB; hoặc extra `.[model-gpu]` thay cho `.[model]`), rồi khởi động lại server:
  ```bash
  uv pip uninstall --python .venv/bin/python onnxruntime
  ```
  ```bash
  uv pip install --python .venv/bin/python 'onnxruntime-gpu[cuda,cudnn]'
  ```
  `GenderTagger` gọi `onnxruntime.preload_dlls()` để nạp CUDA / cuDNN từ các gói `nvidia-*`. Provider GPU không nạp được thì onnxruntime rơi về CPU: status báo `device: "cpu"`, app báo `gpu_fallback`; `--device gpu` biến trường hợp này thành lỗi. DirectML (Windows), ROCm / MIGraphX (AMD) được chọn nếu có, chưa thử; TensorRT (có trong danh sách provider của onnxruntime-gpu) không dùng. Image Docker mặc định chỉ có CPU; bản GPU: `docker build -f tools/model_server/Dockerfile --build-arg ORT_PACKAGE='onnxruntime-gpu[cuda,cudnn]' -t lynxx-model-server:gpu .` rồi chạy với `--gpus all` (cần NVIDIA Container Toolkit). Đo trên máy dev: image 4,41 GB, nạp trên CUDA, 18 ms / ảnh, token bắt buộc, giải phóng trả lại VRAM.
  Đo trên RTX 4070 Ti SUPER (driver 580.178, onnxruntime-gpu 1.32.0, CUDA 13.5 / cuDNN 9.28 từ pip): riêng model 8 ms / ảnh (CPU 323 ms), qua HTTP ~18 ms / ảnh, 2 luồng ~10 ms / ảnh; VRAM ~1,3 GB; nạp 0,7 s. Xác suất lệch với CPU < 0,001, nhãn như nhau. `--device cpu` vẫn chạy CPU (app báo `cpu_forced`).
- **Bản chạy độc lập** (cho người dùng app không có mã nguồn hay Python): `python tools/build_model_server.py [--variant cpu|directml]` tạo `dist-model-server/lynxx-model-server/` và file nén `lynxx-model-server-<os>-<arch>-<variant>_v<phiên bản>.tar.gz` (`.zip` trên Windows). Script tự dựng môi trường build riêng (uv, hoặc venv + pip) với numpy, onnxruntime bản CPU (hoặc `onnxruntime-directml` trên Windows), Pillow, requests, yarl, PyInstaller, nên không phụ thuộc onnxruntime-gpu của `.venv`. Bản đóng gói không kèm model (lần đầu chạy tự tải), có `LICENSE`, `THIRD_PARTY_NOTICES.md` và script chạy nền của hệ điều hành build ra. Smoke test: `--help`; có model trên máy thì chạy server, nhận diện một ảnh trắng, giải phóng; không có model thì kiểm việc báo thiếu model. Đo trên Linux x86_64: 162 MiB giải nén, 47 MiB nén; model nạp trên CPU trong 0,7 s. CI: job `model-server` trong `.github/workflows/build.yml` (Linux, macOS, Windows CPU, Windows DirectML).
  Chạy nền mỗi khi đăng nhập:
  - Linux: giải nén vào `~/.local/share/lynxx-model-server`, chép `lynxx-model-server-bundle.service` (trong bản đóng gói, hoặc `tools/systemd/`) thành `~/.config/systemd/user/lynxx-model-server.service`, rồi `systemctl --user daemon-reload && systemctl --user enable --now lynxx-model-server`. Đã thử bản đóng gói dưới `systemd-run --user`.
  - macOS: `sh install-model-server.sh` trong thư mục bản đóng gói (LaunchAgent `local.lynxx.model-server`, log `~/Library/Logs/lynxx-model-server.log`); gỡ: `--uninstall`. Mới thử phần sinh plist trên Linux với `launchctl` giả; chưa chạy trên macOS.
  - Windows: `powershell -ExecutionPolicy Bypass -File install-model-server.ps1` trong thư mục bản đóng gói (Scheduled Task "Lynxx model server" lúc đăng nhập, ẩn cửa sổ bằng `conhost.exe --headless`); gỡ: `-Uninstall`. Chưa chạy trên Windows.
- **Image đã phát hành** (CPU, amd64 + arm64): `ghcr.io/leiz-workspace/lynxx-model-server:<version>` và `:latest`, đăng bởi workflow release. Lắng nghe trong container là ngoài loopback nên cần token (nhập cùng token ở Cài đặt của app):
  ```bash
  docker run -d --name lynxx-model --restart unless-stopped -e LYNXX_GENDER_MODEL_TOKEN=<token> -p 127.0.0.1:8765:8765 -v lynxx-model:/models ghcr.io/leiz-workspace/lynxx-model-server:latest
  ```
- **Docker tự build** (máy này hoặc máy chủ; lắng nghe ngoài loopback bắt buộc có token):
  ```bash
  docker build -f tools/model_server/Dockerfile -t lynxx-model-server .
  ```
  ```bash
  docker run -d --name lynxx-model --restart unless-stopped -e LYNXX_GENDER_MODEL_TOKEN=<token> -p 127.0.0.1:8765:8765 -v lynxx-model:/models lynxx-model-server
  ```
  Model tải vào volume `/models` ở lần chạy đầu. Đã có model trên máy thì gắn thẳng: `-v ~/.config/Lynxx/models/wd-vit-tagger-v3:/models:ro`. Image ~1,05 GB (chưa gồm model; có ffmpeg để đọc video cho `-gender_verify_media`, không có ffmpeg ~420 MB). Cho máy khác dùng: `-p 8765:8765` và đặt server sau reverse proxy https (app từ chối http tới máy ngoài mạng nội bộ).
- **App**: Cài đặt → Model nhận diện giới tính: công tắc **Dùng model** (mặc định tắt), địa chỉ API, token (nếu server có), nút Kiểm tra, dòng **Phần cứng của server**, nút **Hướng dẫn** (hướng dẫn chung, `app/renderer/guide.js`). Bật: app hỏi server GPU / VRAM trước; không có GPU có VRAM riêng ≥ 1,5 GB, onnxruntime bản CPU, server bị đặt `--device cpu` hoặc VRAM trống không đủ thì hiện cảnh báo khuyên để tắt (Hướng dẫn / Vẫn bật (chạy bằng CPU) / Để tắt), rồi nạp model ngay và báo chạy bằng gì. Tắt: job chỉ lọc theo tag, server trên máy này giải phóng model. Sau khi bật: Bộ lọc giới tính → Nhận diện → "Tag + model (ảnh)". Server không phản hồi thì preview ghi lỗi, job dừng với `Error: '-gender_mode model': model server … is not reachable`.
- **CLI**:
  ```bash
  .venv/bin/python -m lynxx -module xb -gender only_female,both -gender_mode model -gender_model_url http://127.0.0.1:8765 -path ~/Downloads/Lynxx -- scenery
  ```
  Token: `-gender_model_token` hoặc `LYNXX_GENDER_MODEL_TOKEN`. Không có địa chỉ thì CLI chạy model trong tiến trình (cần `pip install "lynxx[model]"` và `python -m lynxx.gender_model install`). Log có `Gender model: looking at N / M item(s) …` và `N labeled, K still unknown`.
- **Xem lại video / ảnh động sau khi tải** (`-gender_verify_media`, F-21, plan §3.5 P4): file video / ảnh động vừa tải của post mà tag chưa đủ kết luận được gửi tới `POST /v1/frames`, server gắn tag tối đa 8 khung; file không khớp bộ lọc được **chuyển** vào `<thư mục lưu>/_gender_mismatch/` (không xoá).
  - Server cần `ffmpeg` trên PATH để đọc video (không có thì dùng PyAV nếu đã cài); ảnh động (GIF / APNG / WebP) chỉ cần Pillow. Dòng `media (/v1/frames): videos ffmpeg …, animated pictures pillow …` lúc khởi động server cho biết server đọc được gì. Image Docker đã có ffmpeg (gói Debian). Server không có ffmpeg / PyAV: job cảnh báo W2, video giữ nguyên, không xem lại.
  - CLI (thêm vào lệnh ở trên): `-gender_verify_media`. Log có `… does not match the gender filter (only_male on 8 frame(s)), moved to _gender_mismatch/`; với `-verbose` có thêm dòng cho file khớp. Dòng tiến độ / DONE có `genderVerified`, `genderMismatch`. App: bộ lọc giới tính → ô **Kiểm tra video sau khi tải** (hiện khi đã bật model, dùng được với "Tag + model (ảnh)"); card job hiện "Không khớp, đã chuyển: n / m file đã xem lại".
  - Đo trên máy dev: model trên GPU (RTX 4070 Ti SUPER, CUDA), mỗi video 4–5 s (512 px / 640 px) qua HTTP ~0,5 s (phần lớn là 8 lần chạy ffmpeg), GIF 10 khung ~0,12 s; image Docker (CPU) 2,7–4,2 s mỗi file. File tối đa 200 MB; ffmpeg mỗi lần tối đa 30 s, cả file tối đa 120 s. Image Docker có ffmpeg: ~1,05 GB trên đĩa (trước đó ~420 MB), vì gói `ffmpeg` của Debian kéo theo nhiều thư viện codec.
  - Kiểm tra tay: `python -m lynxx.media_frames clip.mp4 anim.gif` (khung nào được lấy, bộ giải mã; không cần model), `LYNXX_GENDER_MODEL_URL=http://127.0.0.1:8765 python -m lynxx.gender_model classify clip.mp4` (nhãn, bằng chứng, số khung).
- Công cụ: `python -m lynxx.gender_model status` (có `LYNXX_GENDER_MODEL_URL` thì kiểm server), `install`, `remove`, `classify ảnh.jpg …` (in nhãn, độ tin cậy, bằng chứng, safety tag; video / ảnh động thì xét trên nhiều khung; qua server nếu có biến địa chỉ).
- Kết quả theo post nằm ở `<data>/cache/gender_model.sqlite` của client; xoá file này để model xem lại. Đổi bộ luật `lynxx/gender_rules/db.json` (hoặc `CLASSIFY_VERSION`) thì kết quả cũ tự bị bỏ qua.

## 3d. Catalog extension từ xa: ký số
App và CLI chỉ tin catalog từ xa (`Settings → Advanced → Catalog URL`, `LYNXX_CATALOG_URL`) khi `index.json` có file chữ ký `index.json.sig` cạnh nó, ký bằng Ed25519 với khoá có trong `lynxx/safety_data/catalog_keys.json`. Lõi kiểm chữ ký trên đúng các byte của `index.json` trước khi đọc bất kỳ mục nào; mỗi mục ghi sha256 của archive nên chữ ký phủ luôn các archive. Kiểm bằng `lynxx/ed25519.py` (Python thuần theo RFC 8032, có test vector), không cần thư viện thêm.

**Catalog chính thức** (từ Lynxx 2.0.4): `https://github.com/Leiz-Workspace/Lynxx/releases/download/catalog/index.json`, là mặc định của ô "Catalog URL" (`settings.advanced.catalogUrl`; ô để trống cũng dùng URL này).
- Các file nằm trong release `catalog` của repo GitHub: `index.json`, `index.json.sig` và một zip cho mỗi extension trong `extensions/`. Tag `catalog` không khớp `v*`, nên không kích workflow phát hành nào.
- Chữ ký dùng khoá `b852144452eea5dc`; khoá công khai có trong `catalog_keys.json`. Bản Lynxx cũ hơn 2.0.4 không có khoá nên từ chối catalog này.
- Khoá bí mật nằm ngoài repo, trên máy của người bảo trì: `~/lynxx-keys/signing-key.pem` (thư mục 0700, file 0600). Phải có bản sao lưu offline: mất khoá thì phải phát hành khoá mới; lộ khoá thì phải bỏ khoá khỏi app ngay (xem "Lộ khoá" bên dưới).

Tạo khoá (đã làm một lần, ngày 2026-11-18):
```bash
python tools/sign_catalog.py genkey --out ~/lynxx-keys/signing-key.pem    # file 0600, không ghi đè; giữ offline, có bản sao lưu
python tools/sign_catalog.py pubkey --key ~/lynxx-keys/signing-key.pem    # in mục {keyId, publicKey, comment}
```
Dán mục in ra vào `"keys"` của `lynxx/safety_data/catalog_keys.json`, commit file đó (chỉ có khoá công khai) rồi phát hành bản Lynxx mới. Không bao giờ commit khoá bí mật (`.gitignore` bỏ qua `*.pem`, `*.key`, `signing-key*`); có thể tạo khoá bằng `openssl genpkey -algorithm ed25519 -out signing-key.pem`, cùng định dạng.

Cập nhật catalog chính thức (khi một extension trong `extensions/` đổi `version`, và trước ngày `expires`, hiện là 2027-05-21):
```bash
python tools/build_catalog.py --out /tmp/lynxx-catalog --lists ~/lynxx-tag-lists     # zip tất định cho mỗi extension + index.json
python tools/sign_catalog.py sign /tmp/lynxx-catalog/index.json --key ~/lynxx-keys/signing-key.pem --expires-days 180
python tools/sign_catalog.py verify /tmp/lynxx-catalog/index.json
gh release upload catalog /tmp/lynxx-catalog/* --repo Leiz-Workspace/Lynxx --clobber
```
Extension không đổi giữ nguyên sha256, vì zip tất định. Zip của bản cũ có thể giữ lại trong release cho tới khi người dùng đã cập nhật.

`build_catalog.py` báo trên stderr extension nào có khoá `tagUpdate` mà Lynxx cũ từ chối (hiện là Paheal 1.0.3: `tagUpdate.merge`, cần 2.0.10). Khi bản Lynxx đó chưa lên `production`, build từ một bản sao `extensions/` giữ phiên bản trước của extension ấy. Nếu không, người dùng bản cũ thấy "Cập nhật" mà cài không được, và `index.json` chỉ giữ một phiên bản mỗi extension nên họ cũng mất bản cũ trong catalog:
```bash
rm -rf /tmp/ext-catalog && cp -r extensions /tmp/ext-catalog && rm -rf /tmp/ext-catalog/paheal
git archive v2.0.9 extensions/paheal | tar -x --strip-components=1 -C /tmp/ext-catalog     # Paheal 1.0.2
python tools/build_catalog.py --extensions /tmp/ext-catalog --out /tmp/lynxx-catalog --lists ~/lynxx-tag-lists
```

`--lists`: thư mục chứa danh sách tag mà manifest khai báo (`"tagList": "<abbr>_tags.json"`). Danh sách không nằm trong repo: có thể để ngay cạnh `manifest.json` trên máy người bảo trì (`.gitignore` bỏ qua `extensions/*/*_tags.json`, `build_catalog.py` lấy file ở đó trước) hoặc trong thư mục `--lists`; mỗi file được gói vào zip của extension của nó, nên extension có danh sách ngay khi cài. Tạo danh sách bằng `lynxx-core tags --modules <abbr> --out ~/lynxx-tag-lists`, hoặc lấy lại từ zip của catalog đang phát hành (`unzip -j <zip> '<abbr>_tags.json' -d ~/lynxx-tag-lists`). Extension không có nguồn tag (không `tagUpdate`, không phải module đã duyệt, không engine có API danh sách: hiện là rn, rs) hoặc có job gộp (`tagUpdate.merge`, hiện là rp) không có danh sách ở hai chỗ trên thì lấy `2tags/<abbr>_tags.json` của repo, giống catalog đi kèm lõi; thiếu cả ở đó thì build dừng (cập nhật từ catalog sẽ làm module mất danh sách). Với job gộp, danh sách ở cạnh manifest hoặc trong `--lists` mà ít hơn một nửa danh sách `2tags/` thì build dừng: đó là danh sách job tự ghi một mình (chỉ các tag dùng nhiều). Muốn catalog mang danh sách rp mới hơn: `lynxx-core tags --modules rp --out ~/lynxx-tag-lists` (gộp vào danh sách đang dùng của máy đó; máy chưa cài Paheal thì gộp vào danh sách đi kèm Paheal trong catalog của lõi) rồi `--lists ~/lynxx-tag-lists`. Paheal 1.0.3 cần Lynxx 2.0.10: đăng catalog chính thức có bản này sau khi 2.0.10 lên `production`. Tương tự, extension có khoá `search` (kiểu tìm, F-03b) cần Lynxx 2.0.11: `build_catalog.py` báo `search (Lynxx 2.0.11)` trên stderr (`MANIFEST_KEYS_SINCE` trong `lynxx/modspec.py`), lõi cũ hơn từ chối cả manifest với `unknown field(s): search` và giữ bản đang cài. Extension khai báo `"minSdkVersion": "1.2"` cũng vậy, kể cả khi không có `search` (bản nháp mới của trình tạo ghi SDK hiện tại): `build_catalog.py` báo `minSdkVersion 1.2 (Lynxx 2.0.11)` (`SDK_VERSIONS_SINCE`; Lynxx 2.0.7 tới 2.0.10 có SDK 1.1 và từ chối khi cài, giữ bản đang có). Chỉ đăng catalog chính thức có bản đó sau khi 2.0.11 lên `production`; tới lúc đó build từ bản sao `extensions/` giữ phiên bản trước của extension ấy, như với Paheal ở trên. Extension chỉ sửa lỗi (không thêm `search`, không dùng tên của SDK 1.2, `build_catalog.py` không báo dòng nào) thì tăng PATCH và đăng được ngay khi chủ repo quyết. Các bản của PR kiểu tìm (7be): Realbooru, Rule34.xyz, Rule34.world, Zerochan 1.1.0 chờ 2.0.11; yande.re, Sakugabooru, Gelbooru, TBIB 1.0.1 và e621 1.0.2 chạy trên mọi lõi từ 2.0.7. Thử các bản này trong app trước khi đăng: chạy app từ mã nguồn (catalog của lõi là `extensions/` của repo) hoặc bản build 2.0.11 (catalog đi kèm lõi), trang Extensions → cài từ catalog đi kèm; `.extgen-drafts/<id>-1.0.0.zip` là bản 1.0.0 cũ của PR #37, không phải các bản này. Extension khác thiếu danh sách vẫn được build, kèm một dòng báo; zip vượt giới hạn của trình cài bị từ chối. Xem plan [ext-tag-jobs](plans/ext-tag-jobs.md).

Mỗi lần sửa catalog:
```bash
python tools/sign_catalog.py sign index.json --key ~/lynxx-keys/signing-key.pem --expires-days 180   # ghi index.json.sig
python tools/sign_catalog.py verify index.json     # kiểm như app, với khoá đi kèm bản Lynxx đang chạy
```
Đăng cả `index.json` lẫn `index.json.sig` ở cùng thư mục (URL chữ ký = URL index + `.sig`, giữ query). `--expires-days` ghi trường `expires` vào `index.json` rồi mới ký: quá hạn đó app từ chối catalog, nên phải ký lại trước khi hết hạn (chống việc dùng mãi một catalog cũ). Định dạng mục: `{"id", "name", "abbr", "version", "url" (https), "sha256", "site", "description"}`; công cụ từ chối ký nếu có mục app sẽ bỏ qua. Ký dùng gói `cryptography` nếu có (thời gian hằng), không thì dùng `lynxx.ed25519`; chữ ký Ed25519 tất định nên hai cách ra cùng kết quả.

Đổi khoá: thêm khoá mới vào `catalog_keys.json` và phát hành, ký catalog bằng khoá mới khi phần lớn người dùng đã cập nhật, rồi bỏ khoá cũ ở bản sau. Lộ khoá: bỏ khoá đó khỏi `catalog_keys.json` và phát hành bản sửa ngay.

Thử catalog tự host khi phát triển: tạo khoá thử trong thư mục tạm, ký, rồi chạy app hoặc lõi với `LYNXX_CATALOG_KEYS_FILE=<file json có mục khoá thử>`; hoặc `LYNXX_CATALOG_ALLOW_UNSIGNED=1` cho catalog chưa ký.

## 3e. Danh sách chặn tên miền
`lynxx/safety_data/denied_domains.txt` liệt kê các tên miền không bao giờ được tạo hay cài extension: dịch vụ streaming thương mại / có DRM (R7), site đăng lại nội dung trả phí của người khác (R6), site có nội dung bất hợp pháp (R8). Mỗi dòng: `<tên miền> <R6|R7|R8> [lý do]`; một mục chặn tên miền đó và mọi tên miền con. Với R8, người bảo trì nên ghi dạng băm để file không in tên site:
```bash
python -c "import hashlib,sys; print('sha256:' + hashlib.sha256(sys.argv[1].encode()).hexdigest())" ten-mien.example
```
Chặn ở: chuẩn hoá link site của trình tạo (`survey`, `fetch`, `new`, phiên AI, trước mọi request và lời gọi model), mọi request của `SafeFetcher` (kể cả redirect và robots.txt), kiểm tra offline bản nháp (`domain:R7`…), mọi đường cài (thư mục, zip, catalog cục bộ, catalog từ xa) và lúc nạp extension. Host được so ở mọi dạng mà thư viện mạng có thể đi tới: chữ thường, bỏ dấu chấm cuối, giải mã `%xx`, ASCII / punycode của tên miền quốc tế (IDNA 2008 / UTS 46 như `requests`, và IDNA 2003: ký tự full-width, dấu chấm kiểu Nhật, ký tự rộng 0). URL có `\`, khoảng trắng hoặc ký tự điều khiển bị từ chối (các thư viện tách host khác nhau). Địa chỉ IP của một tên miền bị chặn không được dò ngược. Thêm dòng không cần sửa code; `lynxx.tests.test_extgen.DenylistTests` kiểm file và các cách lách.

## 3f. Đánh giá trình tạo extension (eval)
`python -m lynxx.aigen.eval` chạy trình tạo trên một bộ site (mặc định `lynxx/aigen/eval_sites.json`: kết quả mong đợi, engine, rule) và đo tỉ lệ đạt, chi phí (từ `usage` của API), số lượt model, số bước tool, số request tới site, thời gian, lỗi. Báo cáo `report.json` + `report.md` nằm ở `<data>/extgen/eval/<thời điểm>/` (hoặc `--out`). Bản nháp nằm trong thư mục báo cáo, không bao giờ được cài; mỗi site còn kiểm "không cài gì" và "khoá API không có trong file nào".
```bash
# 1. Ghi dữ liệu site thật một lần, chế độ không AI (miễn phí; lịch sự: robots.txt, 1 request/giây)
.venv/bin/python -m lynxx.aigen.eval run --mode template --live --record ~/lynxx-eval-fixtures
# 2. Chạy lại offline, không mạng: template, hoặc model giả lập (đo phần tính chi phí, không gọi API)
.venv/bin/python -m lynxx.aigen.eval run --mode template --fixtures ~/lynxx-eval-fixtures
.venv/bin/python -m lynxx.aigen.eval run --mode fake --fixtures ~/lynxx-eval-fixtures
# 3. Ước tính chi phí trước khi chạy với Claude thật (không gọi API)
.venv/bin/python -m lynxx.aigen.eval estimate --max-usd 2
# 4. Chạy với Claude thật: TỐN PHÍ, cần --confirm-cost, khoá từ ANTHROPIC_API_KEY hoặc `ant auth login`
ANTHROPIC_API_KEY=... .venv/bin/python -m lynxx.aigen.eval run --mode ai --live --confirm-cost --max-usd 2
```
- `--only a,b` chạy một số site; site có `"modes": ["ai"]` (site HTML tùy biến) chỉ chạy ở chế độ AI. Chạy offline với `--mode ai --fixtures DIR` cũng được, nhưng request nào model gọi mà chưa ghi sẽ nhận 404 và được liệt kê ở `missing`.
- Ước tính chi phí (`estimate`): (1) **trường hợp xấu nhất** = số site × trần mỗi phiên (`--max-usd`, phiên không bắt đầu lượt mới khi đã chạm trần; lượt cuối có thể vượt một ít); (2) **mô hình**: một phiên `--turns` lượt (mặc định 14), lượt đầu ghi cache tiền tố (system prompt + tool, đếm theo 3,5 ký tự / token), lượt sau đọc cache tiền tố + lịch sử và ghi phần mới (`--out-tokens` 1500 + `--result-tokens` 2500), giá theo `MODELS` trong `lynxx/aigen/session.py` (Opus 5.5: 4 / 20 USD mỗi triệu token vào / ra, đọc cache 0,20, ghi cache 5). Với mặc định: ~0,77 USD / phiên, 9 site ~7 USD, xấu nhất 18 USD; (3) **đo được**: trung bình `usd` các phiên AI cũ (báo cáo eval và `report.json` của bản nháp) nếu có. Khi chạy thật, lệnh in ước tính và dừng cả bộ khi tổng chi phí chạm tổng các trần.

## 3g. Đánh giá model giới tính (`python -m lynxx.gender_eval`, plan gender-filter-model P1)
Công cụ đo độ chính xác của tầng 0 (tag) và tầng 1 (model) theo plan §3.2, §3.7. **Không lưu ảnh**: ảnh được tải vào RAM bằng session của downloader (cùng header, proxy, giãn cách request), cho người xem hoặc gửi tới model rồi bỏ. File ra chỉ có id post, md5, URL, tag, nhãn và số đo. Nên chạy với `LYNXX_DATA_DIR` riêng nếu không muốn đụng thư mục dữ liệu thật (chỉ thư mục `ratelimit/` được dùng).

1. **Lấy mẫu** (chỉ metadata, qua downloader của module, cách nhau ≥ 1 s mỗi trang):
   ```bash
   .venv/bin/python -m lynxx.gender_eval sample --module db --query "random:100 rating:g" --query "1boy random:100 rating:g" --query "no_humans random:100 rating:g" --query "gentags:<=8 random:100 rating:g" --quota only_female=60,only_male=60,both=60,none=60,sparse=80,weak=20 --out eval/manifest.json
   ```
   - Mỗi `--query` là một truy vấn (không có nhóm OR), lấy `--pages` trang. Trên Danbooru, `random:N` chỉ trả một trang: muốn thêm post thì lặp lại cùng truy vấn; post trùng bị bỏ.
   - Thứ tự loại (log đếm từng bước, ghi vào `exclusions` của manifest): 1 danh sách chặn cứng §4.1 trên tag và alias (`gender_rules/*.json` + `all_tag_aliases.json`), 2 danh sách loại bổ sung §4.2 (`school_uniform`, `petite`…), 3 ảnh / người thật §4.3, 4 safety gate của model (cần ảnh nên áp ở `label` và `evaluate`), 5 trùng md5 (pHash chưa làm vì không giữ ảnh), 6 post đã xoá / không có file. Module mà trang tìm kiếm không có tag (rule34.us) bị từ chối vì không kiểm được danh sách chặn.
   - Mỗi post có nhãn tầng 0 + độ tin cậy, slice §3.7 (site, phong cách, ca biên futa / trap / POV / đông người ≥ 6 / `muscular_female` / da tối, mật độ tag, loại media) và tầng: nhãn tầng 0 mức high, `sparse` ("thiếu tag": ≤ 8 tag general hoặc không có tag giới) hoặc `weak` (tag giới mức medium / low). `--quota` đặt số post mỗi tầng (`all` = lấy hết).
2. **Gán nhãn** (mỗi người một file, độc lập, không thấy tag hay nhãn tầng 0):
   ```bash
   .venv/bin/python -m lynxx.gender_eval label --manifest eval/manifest.json --annotator an --open
   ```
   - Trang chỉ nghe trên `127.0.0.1` (mặc định cổng 8790), kiểm Host và token phiên. Phím: `1`/`F` chỉ nữ, `2`/`M` chỉ nam, `3`/`B` cả hai, `4`/`N` không có, `S`/Space bỏ qua, `T` đổi sample / thumbnail, `Z` hoàn tác, `X` không an toàn (loại).
   - Nhãn ghi vào `eval/labels-an.json` sau mỗi lần bấm. `X`: post bị xoá khỏi manifest ngay, tag của nó được in ra và lưu trong `dropped` (kèm danh sách tag mà blacklist chưa chặn, để bổ sung vào `lynxx/safety.py`).
   - Trước khi hiện ảnh, công cụ gửi ảnh tới model server (`--model-url`, mặc định `LYNXX_GENDER_MODEL_URL` hoặc `http://127.0.0.1:8765`) làm safety gate: tag cấm ≥ 0,15 thì post bị loại tự động, người gán không thấy ảnh. Server không chạy thì lệnh báo lỗi; `--no-model-gate` tắt bước này.
3. **Độ đồng thuận** và tập vàng:
   ```bash
   .venv/bin/python -m lynxx.gender_eval agree eval/labels-an.json eval/labels-binh.json --manifest eval/manifest.json --out eval/agree.json
   .venv/bin/python -m lynxx.gender_eval label --manifest eval/manifest.json --annotator trongtai --only eval/agree.json
   .venv/bin/python -m lynxx.gender_eval agree eval/labels-an.json eval/labels-binh.json --arbiter eval/labels-trongtai.json --manifest eval/manifest.json --out eval/agree.json --gold eval/gold.json
   ```
   `agree` tính Cohen's κ trên các post cả hai người đều gán một trong 4 nhãn (bỏ qua `skip`, post bị loại), in danh sách bất đồng cho trọng tài và kiểm AC-G08 (κ ≥ 0,8; ≥ 3.000 post; ≥ 30 % thiếu tag). `--gold`: nhãn hai người cùng chọn + quyết định của trọng tài.
4. **Đánh giá**:
   ```bash
   .venv/bin/python -m lynxx.gender_eval evaluate --manifest eval/manifest.json --gold eval/gold.json --out-dir eval/report
   .venv/bin/python -m lynxx.gender_eval evaluate --manifest eval/manifest.json --silver --out-dir eval/report-silver
   ```
   - Có tập vàng: đo tầng 0, model (chỉ ảnh) và tầng 0 + model (`combine`) so với nhãn của người. `--silver` (chưa có nhãn của người): tham chiếu là nhãn tầng 0 mức high, model chỉ nhận ảnh; đây là độ đồng ý với tag mạnh, không phải precision thật.
   - Đầu vào `--inputs sample,thumb` (mặc định cả hai), điểm vận hành `--min-conf medium`. Ảnh cách nhau `--image-interval` giây (mặc định 1). Site trả bot challenge thì lệnh dừng và vẫn ghi báo cáo (đánh dấu chưa chạy hết); tag đoán ra được cache ở `<out-dir>/predictions.jsonl` nên chạy lại sẽ tiếp tục, không tải lại ảnh đã xem. `--max-minutes` giới hạn thời gian.
   - Ra `report.json` và `report.md`: precision theo lớp, độ phủ, macro-F1 4 lớp, ma trận nhầm lẫn 4×4 + cột unknown, ECE (15 bin) trên xác suất mà nhãn dựa vào, các điểm risk–coverage, mọi slice kèm n, thumbnail so với sample, cờ thiên lệch (slice n ≥ 150 có precision thấp hơn toàn cục > 5 điểm), số post trap bị gán only_female.
   - Post mà safety gate của model chặn (tag cấm ≥ 0,15 trên ảnh sample hoặc thumbnail) bị loại khỏi mọi số đo và bị xoá khỏi file manifest như ở `label`: `dropped` của manifest chỉ ghi khoá và tag của gate; các tag mà danh sách chặn chưa bắt được in ra và ghi trong `posts.modelGateDropped` của `report.json` để xem lại `lynxx/safety.py`.

## 3h. Chặn nội dung gợi ý trẻ vị thành niên (`-safety_blacklist`)
- **Bộ lọc riêng, mặc định tắt**, độc lập với bộ lọc giới tính (quyết định 2026-11-18, thay quyết định 2026-11-16 khi nó tự chạy cùng `-gender`).
  - CLI: `-safety_blacklist`.
  - App: ô "Chặn nội dung gợi ý trẻ vị thành niên" trong Bộ lọc & tuỳ chọn tải. Lựa chọn được nhớ (`settings.download.safetyBlacklist`) và lưu theo từng truy vấn, nên "chạy lại" và tải theo lịch giữ nguyên.
- **Khi bật:**
  - bỏ mọi post có tag trong danh sách cố định của `lynxx/safety.py`, cho mọi task kể cả post cha;
  - ở Preview, post bị chặn không hiện (kể cả thumbnail), chỉ được đếm; post không có tag để kiểm bị ẩn;
  - với `-gender_mode model`, chốt an toàn của model (tag gợi ý trẻ vị thành niên mà model nhận ra) cũng chặn post, và với `-gender_verify_media` thì chặn cả file đã tải.
- Extension chỉ thêm được tag (`safetyTags`); không đường nào bớt được tag của danh sách.
```bash
.venv/bin/python -m lynxx -module db -safety_blacklist -gender only_female -path ~/Downloads/Lynxx -- scenery
```

## 4. Test
```bash
.venv/bin/python -m unittest lynxx.tests.test_lynxx
.venv/bin/python -m unittest lynxx.tests.tests.DataStructureIntegrityTests lynxx.tests.tests.FileCheckTests lynxx.tests.tests.ArgParseTests lynxx.tests.tests.TagParseTests lynxx.tests.tests.LoggerTests lynxx.tests.tests.DownloaderBaseTests lynxx.tests.tests.ItemFilterTests
.venv/bin/ruff check lynxx extensions tools
cd app && npm run check
LYNXX_USER_DATA=/tmp/lynxx-ud LYNXX_SELFTEST_DIR=/tmp/lynxx-shots xvfb-run -a ./node_modules/.bin/electron . --no-sandbox --ozone-platform=x11
```
Tự kiểm thử không cần mạng: thêm `LYNXX_SELFTEST_QUICK=1` rồi đọc kết quả bằng `python tools/check_selftest.py /tmp/lynxx-shots/report.json`. Đặt `TMPDIR` ngắn (Chromium dừng với `Socket path too long` khi đường dẫn socket trong `TMPDIR` dài quá ~100 ký tự). Chạy từ một worktree không có `node_modules`: gọi `electron` của bản checkout chính với đường dẫn tới thư mục `app` của worktree, và `LYNXX_PYTHON=<repo>/.venv/bin/python`.
`ConnTests` và `RealDownloadTests` gọi mạng thật. `test_gender.test_real_model_on_the_app_icon` chỉ chạy khi có `LYNXX_GENDER_MODEL_DIR` (thư mục đã tải model, mục 3c). `test_model_server` chạy server thật trên cổng ngẫu nhiên với model giả (và onnxruntime giả cho phần chọn GPU / CPU), không cần numpy / onnxruntime hay GPU. `test_media_verify` (`-gender_verify_media`, `/v1/frames`) cũng dùng model giả; phần đọc khung thật chỉ chạy khi có Pillow (ảnh động) và `ffmpeg` trên PATH (video), thiếu thì tự bỏ qua. `lynxx.tests.test_extgen` chạy offline (gồm `DenylistTests`: các cách lách danh sách chặn tên miền; `SdkTests`: `lynxx.sdk` và `minSdkVersion`); test nối SDK `anthropic` tự bỏ qua nếu chưa cài gói. `lynxx.tests.test_dlindex_tools` chạy offline (site giả, thư mục tạm). `lynxx.tests.test_catalog_trust`: test vector RFC 8032, chữ ký catalog (sửa 1 byte, khoá lạ, chưa ký, khoá giữ chỗ, hết hạn, biến môi trường cho nhà phát triển), cài từ catalog từ xa qua transport giả, `tools/sign_catalog.py` (và đối chiếu với `openssl` nếu máy có). `lynxx.tests.test_aigen_eval`: harness eval ghi / phát lại fixture (cả qua server HTTP cục bộ với `requests` thật), chế độ template, model giả lập, chế độ AI với client theo kịch bản, trần chi phí, kiểm khoá bị lộ; không gọi API trả phí. `test_gender_eval` chạy offline (trang tìm kiếm giả, ảnh giả, model giả, trang gán nhãn thật trên cổng ngẫu nhiên). `lynxx.tests.test_tag_sources` chạy offline: site giả trả danh sách tag của cả 7 engine qua transport giả; kiểm robots.txt (RFC 9309, ngoại lệ cho module đã duyệt), `Crawl-delay`, `Retry-After`, ngân sách request, bot challenge, redirect sang host khác, khoá bắt buộc; và luồng đầu-cuối: trình tạo làm extension Gelbooru → cài → `tags.update` → `tags.autocomplete` có tag của site. Cùng file: danh sách đi vào bản nháp và file .zip (và các trường hợp để app tải sau), danh sách đi kèm extension đã cài, và danh sách học từ post Preview / index. `lynxx.tests.test_search_kinds` chạy offline: khoá `search` của manifest và cổng 2.0.11 (cả mã 2.0.10 thật khi git có commit `fe0c695`, clone nông thì bỏ qua), kiểu tìm trong bộ đọc truy vấn, kiểu listing (lọc cục bộ, thứ tự của site, cỡ trang của kiểu, robots.txt lưu trong extension) qua một extension giả cài vào thư mục tạm và site giả, tag sắp xếp theo cú pháp của module, token / `metaNames` không bị chuyển sang lọc cục bộ, các method sidecar (`tags.autocomplete` / `tags.fuzzy` theo loại, `tags.kinds`, lỗi của `tags.validate`, `tags.resolve`, `search.related`, cờ của Preview, `files.identify`), `SafeFetcher` giãn cách cả request robots.txt (đồng hồ giả), `tags.types`. `lynxx.tests.test_type_tag_list` chạy offline: `tools/type_tag_list.py` (chỉ dòng không loại được gắn, dòng khác giữ từng byte, thứ tự tham chiếu, `--max-count-ratio`, `--evaluate`, CLI). `lynxx.tests.test_gelbooru_ext` chạy offline trên câu trả lời ghi từ gelbooru.com (`lynxx/tests/fixtures/gelbooru/`, không có khoá): post dạng thẻ con thành chuỗi lõi đọc, post dạng thuộc tính như cũ, giới hạn vị trí 20.000 và con trỏ id, job tag theo số post (sàn 20, trang lặp, tag deprecated, khoá, câu trả lời lạ được trích lại). `lynxx.tests.test_e621_ext` chạy offline trên câu trả lời ghi từ e621 ngày 2026-11-22 (`lynxx/tests/fixtures/e621/`): dạng mới và dạng cũ của `posts.json`, file bị ẩn, Preview và Đếm qua sidecar, lời báo bot challenge của Preview.

**Chạy test trên máy có app đã cài:** đặt `LYNXX_DATA_DIR` (và `XDG_CONFIG_HOME`) tới thư mục tạm. Không đặt thì vài test (`test_search_kinds.RealbooruRouteTests`, `RepoExtensionFixTests`) nạp extension đã cài trong `~/.config/Lynxx` thay cho bản của repo và hỏng, và lõi mở index của bạn (chỉ đọc).

Toàn bộ test offline như CI (`.github/workflows/run-tests.yml`), với thư mục dữ liệu tạm:
```bash
LYNXX_DATA_DIR=$(mktemp -d) .venv/bin/python -W ignore -m unittest lynxx.tests.test_lynxx lynxx.tests.test_gender lynxx.tests.test_security lynxx.tests.test_batch_b lynxx.tests.test_batch_c lynxx.tests.test_batch_c_tags lynxx.tests.test_batch_d_core lynxx.tests.test_batch_d_search lynxx.tests.test_batch_d_app lynxx.tests.test_extgen lynxx.tests.test_dlindex lynxx.tests.test_dlindex_tools lynxx.tests.test_model_server lynxx.tests.test_media_verify lynxx.tests.test_catalog_trust lynxx.tests.test_aigen_eval lynxx.tests.test_gender_eval lynxx.tests.test_tag_sources lynxx.tests.test_search_kinds
```

## 5. Build
- CLI exe: `python lynxx/release` (PyInstaller, đã kèm manifest builtin và hidden-import `download_rx`/`download_db`, icon `images/lynxx.ico`: icon mặt nạ cam của app ở 16–256 px). Thư mục `extensions/` cần được đặt cạnh file exe để làm catalog.
- App desktop:
  1. Build lõi Python đóng băng: `python -m pip install -r requirements.txt 'pyinstaller>=6.21' 'anthropic>=2.9' && python tools/build_core.py` → `app/build/core/lynxx-core/`, khoảng 40 MB (khoảng 51 MB khi kèm `anthropic`; bỏ gói này thì bản build chỉ có chế độ "Không dùng AI"). Mặc định không kèm danh sách tag trong `2tags/` của lõi; dùng `--tags all` hoặc `--tags rx,db` để kèm. Riêng catalog extension đi kèm thì extension nào có `tagList` mà không có nguồn cập nhật (không `tagUpdate`, không phải module đã duyệt, không engine có API danh sách: hiện là rn, rs) hoặc có job gộp (`tagUpdate.merge`, hiện là rp) được chép danh sách `2tags/` cạnh manifest (thêm khoảng 26 MB); danh sách mà người bảo trì để cạnh manifest trong `extensions/` không được chép vào lõi. Smoke test kiểm `server`, client model server (`gender.modelStatus`), `cli`, `index` (nhập thư viện chạy thử), `aigen`, danh sách chặn tên miền (phiên `https://www.youtube.com/` bị từ chối R7) và các file `lynxx/safety_data`. Đo ngày 2026-11-17 (có `lynxx.sdk`, `lynxx.trust`, danh sách chặn): 51,4 MiB khi kèm `anthropic`. numpy, onnxruntime, Pillow luôn bị loại khỏi bản build kể cả khi môi trường build có (model chạy ở model server, mục 3c): đo được 51,4 MiB khi kèm `anthropic`; trước đó kèm onnxruntime thì thêm ~110 MB.
  2. Build app: `cd app && npm ci && npm run dist` (Linux AppImage; Windows nsis/zip; macOS dmg/zip), hoặc `npm run dist:dir` để chỉ tạo thư mục chưa nén.
  3. Model server chạy độc lập (tuỳ chọn, phát hành riêng với app): `python tools/build_model_server.py`, xem mục 3c.
- **Release** (`.github/workflows/release.yml`, cùng cách làm với Mirrix). Tên file: `<app>-<os>-<arch>_v<version>.<ext>`.
  - Push lên `staging`: build mọi nền tảng rồi đăng pre-release `v<version>-rc.<lượt chạy>`. Push lên `production`: đăng release `v<version>` (latest) bằng chính các file của bản rc mới nhất có `build-info.json` khớp cây commit (kiểm lại `SHA256SUMS.txt`, không build lại); không có bản rc khớp thì build lại tất cả. Pull request và chạy tay: chỉ build và tự kiểm Linux x64, không đăng gì. Push chỉ đổi `docs/`, `.github/`, `.forgejo/`, `.claude/`, `lynxx/tests/`, `.gitignore`, `*.md` ở gốc, hoặc các công cụ không tạo ra file phát hành nào (`tools/build_catalog.py`, `tools/sign_catalog.py`, `tools/check_selftest.py`, `tools/type_tag_list.py`), hoặc danh sách `2tags/` mà app không đóng gói (`rx`, `db`, `xb`, `bb`, `en`: lõi đóng băng chỉ mang file alias, catalog đi kèm chỉ mang danh sách rn / rs / rp), sau khi `v<version>` đã ra thì không đăng gì (luật `NOT_SHIPPED`; `BuildCoreTests.test_files_the_release_rule_calls_not_shipped_are_not_in_the_build` kiểm luật với bản build). Trước 2026-11-21 hai mục cuối chưa có: PR #78 (develop → staging, chỉ mang `2tags/rx`, `2tags/xb` và `tools/type_tag_list.py` ngoài tài liệu) làm lượt Release của staging dừng với "v2.0.11 is already released. Bump the version".
  - App: Linux x64 / arm64 (`lynxx-linux-x86_64_v….AppImage`, `lynxx-linux-amd64_v….deb`), Windows x64 / arm64 (`lynxx-setup-<arch>_v….exe`), macOS arm64 / Intel (`lynxx-mac-<arch>_v….dmg`, `.zip`, chưa ký). Lõi Python là bản PyInstaller nên mỗi kiến trúc build trên máy của nó (`ubuntu-24.04-arm`, `windows-11-arm`, `macos-15-intel`), không build chéo. Bản Linux: chuẩn hoá quyền file (umask 0022) và kiểm quyền từng mục trong `.deb`.
  - Self-test mới gồm `contract.selectedDownload`, `contract.quality`, `contract.upstream` và `contract.siteVerify`; Node check chạy thêm `app/test/site-verify.test.js`. Test Python mới gồm `lynxx.tests.test_pick_and_quality` và `lynxx.tests.test_rule34xyz_ext`.

Mỗi bản app được tự kiểm bằng self-test của app (`LYNXX_SELFTEST_QUICK=1`, kết quả đọc bằng `tools/check_selftest.py`): bắt buộc đạt trên mọi nền tảng (Linux chạy dưới xvfb; giới hạn 10 phút bằng Python vì macOS không có lệnh `timeout`).
  - Model server chạy độc lập: Linux x64 / arm64, Windows x64, macOS arm64 (CPU), Windows x64 DirectML. Image Docker CPU (amd64 + arm64) lên `ghcr.io/<owner>/lynxx-model-server:<version>-rc.<n>`; release đổi tag bản rc thành `<version>` và `latest`. Image GPU không được đăng (thư viện NVIDIA có điều khoản riêng): người dùng tự build.
  - Package `lynxx-model-server` trên ghcr được tạo ở chế độ riêng tư lần đầu đăng: chủ tổ chức chuyển sang Public trong Package settings → Danger Zone → Change visibility (tổ chức phải cho phép package công khai); đã công khai từ `2.0.1`, các tag sau giữ nguyên chế độ đó. Kiểm tra từ máy không đăng nhập ghcr: `docker logout ghcr.io && docker pull ghcr.io/leiz-workspace/lynxx-model-server:<version>`.
  - Mọi release có `SHA256SUMS.txt` và `build-info.json` (version, cây commit, commit, lượt chạy, image). Ghi chú phát hành lấy từ `docs/releases/v<version>.md` nếu có, nếu không thì GitHub tự tạo.
  - Đưa code đi: `develop` → `staging` (bản rc) → `production` (bản chính thức). Version nằm ở `app/package.json` và `pyproject.toml` (bước plan báo lỗi khi hai nơi khác nhau); version đã phát hành thì phải tăng mới đăng được bản rc mới.
  - App đã đóng gói dùng `<resources>/core/lynxx-core`. Biến `LYNXX_CORE` trỏ app dev sang một lõi đóng băng.
  - Self-test chạy được trên file đã đóng gói với cùng các biến `LYNXX_SELFTEST_*`; thêm `LYNXX_SELFTEST_QUICK=1` để bỏ các bước cần mạng. `LYNXX_SELFTEST_ICONS=1` kiểm việc đồng bộ launcher khi đổi icon; nó **ghi vào `~/.local/share/applications`**, nên chỉ chạy kèm `HOME=<thư mục tạm>`.
  - CI: job `desktop` trong `.github/workflows/build.yml`.

## 6. Nhánh git, pull request và Forgejo
`production` (nhánh mặc định, phát hành) ← `staging` (bản rc) ← `develop` (tích hợp), cùng quy trình với các repo khác của `Leiz-Workspace` (hướng dẫn của nền tảng phân phối, repo `self-software-distribution-platform`, `docs/GUIDE.md`).

- **Mọi thay đổi đi qua pull request.** Thay đổi vào `develop`: nhánh riêng → PR vào `develop`. Đưa lên `staging` / `production`: đẩy đầu nhánh nguồn sang nhánh tạm rồi mở PR, merge bằng merge commit; không dùng chính `develop` hay `staging` làm nhánh nguồn của PR.
  ```bash
  git push origin origin/develop:refs/heads/release/promote-staging
  ```
  ```bash
  gh pr create --head release/promote-staging --base staging --title "Promote develop to staging: <nội dung>" --body "<thay đổi và kết quả CI>"
  ```
  ```bash
  gh pr merge <số PR> --merge --delete-branch
  ```
  Lặp lại với `release/promote-production` → `production`. Chủ repo quyết định merge vào `production`.
- **Ruleset "develop, staging, production: CI must pass"** trên GitHub: chỉ cập nhật ba nhánh qua PR, PR chỉ merge được khi check `Tests passed` (job gộp kết quả của `.github/workflows/run-tests.yml`) và `Package (linux)` (build và self-test bản Linux của `release.yml`) đạt; không xoá, không force-push. Workflow Tests chạy cho mọi PR, kể cả PR chỉ đổi tài liệu, để check bắt buộc luôn có kết quả.
- **Merge PR có file trong `.github/workflows/`:** GitHub không cho token OAuth thiếu quyền `workflow` merge những PR đó qua API; merge trong trang PR, hoặc thêm quyền: `gh auth refresh -h github.com -s workflow`.
- **Runner:** CI của Lynxx chạy trên runner của GitHub (miễn phí cho repo công khai). Runner tự host `aevra-home` của tổ chức không được dùng cho repo công khai: PR từ fork sẽ chạy được code tuỳ ý trên Home Server.
- **Forgejo (Home Server):** webhook của GitHub đồng bộ mirror `mirror/Lynxx` sau mỗi push. `.forgejo/workflows/ci.yml` chạy lint và test (Python 3.11, Debian) với mỗi push lên ba nhánh; `.forgejo/workflows/forgejo-release.yml` chạy với mỗi tag `v*` mà release trên GitHub tạo ra: build lại bản Linux x64 (`.deb`, `.AppImage`, model server chạy độc lập), self-test, ký `SHA256SUMS-linux` bằng minisign, đăng package generic `lynxx/<version>` và kho Debian (`stable`, component `main` cho bản chính thức, `rc` cho bản rc), tạo Release trên Forgejo; image model server lên registry của Forgejo, quét Trivy, ký cosign kèm SBOM. Windows, macOS và Linux arm64 chỉ có trên GitHub. Khoá công khai nằm trong `keys/` (khoá bí mật chỉ có trong Vault và secret của mirror): kiểm file Linux bằng `minisign -Vm SHA256SUMS-linux -p keys/minisign.pub` rồi `sha256sum -c SHA256SUMS-linux`, kiểm image bằng `cosign verify --key keys/cosign.pub <image>`. Repo công khai này không ghi tên miền hay tài khoản nội bộ của Forgejo; chúng nằm trong tài liệu của nền tảng.
