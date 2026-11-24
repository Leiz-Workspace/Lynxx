# Plan — Gán nhãn & lọc theo giới tính nhân vật (`only_female` / `only_male` / `both`)

> 2026-11-16 · Trạng thái (cập nhật 2026-11-17): tầng 0, tầng 1 phương án A0 và P4 (video nhiều khung, trừ đánh giá 300 clip) **đã làm**; công cụ đánh giá P1 đã có, tập vàng chưa được gán. Xem [Trạng thái triển khai](#trạng-thái-triển-khai). User story: US-21 ([user-stories.md](../user-stories.md)). Liên quan: [search-engine.md](search-engine.md) (Giai đoạn 3).
> Mục tiêu: người dùng chọn "chỉ nữ", "chỉ nam" hoặc "cả hai" và Lynxx tự loại post không khớp **trước khi tải**.
> Hướng đi gồm hai tầng:
> - **Tầng 0** suy nhãn từ tag sẵn có. Không cần ML, không tải ảnh, ra mắt được ngay.
> - **Tầng 1** là model thị giác chạy cục bộ, chỉ dùng cho post thiếu tag hoặc tag nhiễu.
>
> Bên cạnh hai tầng có một **blacklist an toàn cứng** (§4). **Quyết định 2026-11-18** (thay quyết định 2026-11-16, khi blacklist tự chạy cùng bộ lọc giới tính): blacklist là **bộ lọc riêng, mặc định tắt** (`-safety_blacklist`, ô riêng trong app), độc lập với bộ lọc giới tính; người dùng tự bật. Khi tắt, app giữ hành vi như bản gốc.

## Mục lục

0. [Tóm tắt & quyết định chính](#0-tóm-tắt--quyết-định-chính)
1. [Định nghĩa nhãn](#1-định-nghĩa-nhãn)
2. [Tầng 0 — lọc theo tag](#2-tầng-0--lọc-theo-tag-không-cần-ml)
3. [Tầng 1 — model thị giác](#3-tầng-1--model-thị-giác-cho-post-thiếunhiễu-tag)
4. [An toàn dữ liệu (bắt buộc)](#4-an-toàn-dữ-liệu-bắt-buộc)
5. [Tích hợp sản phẩm](#5-tích-hợp-sản-phẩm)
6. [Lộ trình, ước lượng, rủi ro](#6-lộ-trình-ước-lượng-rủi-ro)
7. [Acceptance criteria](#7-acceptance-criteria)
8. [Câu hỏi mở](#8-câu-hỏi-mở)

---

## Trạng thái triển khai

- **Bằng chứng nữ đối xứng với nam (2026-11-20)**: chủ app báo "chỉ nam" + `amputee` trên rx vẫn ra post có nữ. Nguyên nhân: bảng `rx` thiếu `1girl` (~1,3M post rx) và `female_focus`, còn tag bộ phận cơ thể nữ không phải bằng chứng trong khi `penis` tạo M. Đã sửa:
  - `rx`, `bb`, `gelbooru_generic`: F (H) thêm `1girl`, `female_focus`; `rx` thêm M (H) `male_focus` cho cân.
  - Khoá mới `femaleBodypart`, đối xứng với `maleBodypart` (§1.2): có bộ phận nữ thì F=1 (high), nên post có cả bằng chứng nam thành `both`, không còn là `only_male`. Cờ `futa` / `futaMale` vẫn chặn cả hai chiều: bộ phận đó có thể là của nhân vật futa / cuntboy.
  - Cờ futa thêm `gynomorph`, `herm` (rx, bb, generic); cờ futaMale thêm `andromorph`, `maleherm` và `male_with_breasts` (db thêm `cuntboy`, `male_with_breasts`), để bộ phận nữ của các nhân vật này không bị đếm là một người nữ.
  - Bộ phận nữ chỉ vào `evidence` khi chính nó làm F thành high. Vì vậy một tag thừa không hạ độ tin cậy của model (độ tin cậy lấy theo tag bằng chứng yếu nhất).
  - Hệ quả: post chỉ có tag cơ thể nữ (`breasts`, `pussy`) có nhãn `only_female` (medium trên rx), như `penis` một mình vẫn cho `only_male`. Các số độ phủ đo bên dưới là của bảng cũ; chưa đo lại (đo cần request tới site).
- **P1, công cụ đánh giá (2026-11-17, dev log "Công cụ đánh giá model giới tính")**: `lynxx/gender_eval.py` (`python -m lynxx.gender_eval`, cách chạy ở operations.md mục 3g).
  - `sample`: lấy post qua downloader (chỉ metadata), loại theo thứ tự §3.2 có đếm từng bước, nhãn tầng 0 + slice §3.7, phân tầng (nhãn high, "thiếu tag", nhãn yếu) → manifest JSON. `label`: trang gán nhãn cục bộ (127.0.0.1, phím tắt, người gán không thấy tag), ảnh chỉ nằm trong RAM, safety gate của model chạy trước khi người xem, nút "không an toàn" xoá post khỏi manifest và báo tag. `agree`: Cohen's κ, danh sách cho trọng tài, tập vàng. `evaluate`: tầng 0 / model / tầng 0 + model với tập vàng, hoặc silver; ra `report.json` + `report.md` theo §3.7.
  - Đã chạy **silver** trên Danbooru `rating:g`: [docs/reports/gender-model-eval-2026-11-17.md](../reports/gender-model-eval-2026-11-17.md). Silver đo mức đồng ý của model với tag mạnh, **không** thay cho tập vàng.
  - **Khác plan**: không tính embedding và không có `meta.parquet` (A0 không train head; manifest JSON thay thế, version ở `tools/gender/`); chưa khử trùng theo pHash (cần giữ ảnh); split theo artist chưa làm (chỉ cần khi train A1 / B).
  - **Còn cần người**: tập vàng (AC-G08: ≥ 3.000 post, ≥ 30 % thiếu tag, hai người gán độc lập, κ ≥ 0,8, trọng tài), sau đó mới đo được precision của tầng 0 (AC-G07) và model (AC-G09, AC-G10). Lệnh ở operations.md mục 3g.
- **Model server (2026-11-17, dev log 7u)**: model chạy ở `lynxx/model_server.py` (HTTP API, Docker, systemd), app và CLI chỉ gọi API; bản app không kèm onnxruntime / numpy / Pillow / model (lõi đóng băng 51,4 MiB). Khác §3.6 / §4.4 / §5.4: suy luận không bắt buộc trên máy người dùng, ảnh được gửi tới model server mà người dùng cấu hình (mặc định 127.0.0.1); http chỉ cho máy này / mạng nội bộ, ngoài ra phải https + token. Bộ luật, safety gate và cache vẫn ở client. Nút tải / xoá model trong app được bỏ (server tự tải).
- **Bật / tắt và phần cứng (2026-11-17, dev log 7v)**: app có công tắc Dùng model (mặc định tắt), kiểm tra GPU / VRAM của server trước khi bật và cảnh báo khi model sẽ chạy bằng CPU, kèm hướng dẫn chung. Server chạy model trên GPU khi onnxruntime có provider GPU, nạp lười và tự giải phóng sau 15 phút không dùng.
- **Tầng 1, phương án A0 đã làm (2026-11-17, dev log 7t)**: `lynxx/gender_model.py`, `-gender_mode model`, RPC `gender.modelStatus` / `gender.modelInstall` / `gender.modelRemove`, Cài đặt → Model nhận diện giới tính, lựa chọn "Tag + model (ảnh)" trong bộ lọc. Đo thử: 20/20 ảnh mẫu khớp nhãn tag (sample), 18/20 với thumbnail (2 ca lệch đều ở độ tin cậy low, nên thành unknown với ngưỡng mặc định medium); ~0,4 s/ảnh trên CPU; Xbooru `scenery` chỉ nữ / cả hai: model xem 4/42 post thiếu tag, gán nhãn cả 4. **Khác plan**:
  - Không train gì: dùng thẳng WD tagger v3 ViT (fp32, ~361 MB, revision ghim), đầu ra đi qua bộ luật `db`. Chưa có tập vàng nên chưa đo precision theo §7; ngưỡng §3.4 lấy theo xác suất yếu nhất trong bằng chứng (≥ 0,85 high, ≥ 0,6 medium).
  - Extra là `lynxx[model]` (numpy, onnxruntime, pillow), không phải `lynxx[gender]`. Chỉ `CPUExecutionProvider`, session không giải phóng khi rảnh.
  - Thư mục `data_dir()/models/wd-vit-tagger-v3/` (biến `LYNXX_GENDER_MODEL_DIR` để trỏ chỗ khác), tải thẳng từ Hugging Face, không có catalog model (§5.4) và chưa có rollback.
  - Ảnh: `sample_url` khi là ảnh (~850 px, tốt hơn nhiều so với thumbnail 150–180 px), nếu không thì thumbnail; không đọc cache thumbnail của Electron. Cache kết quả `cache/gender_model.sqlite`, khoá `<module>:<post id>` + id model.
  - RPC đặt tên `gender.model*`; không có `gender.classify` chạy nền: preview gọi model đồng bộ cho tối đa 40 post / trang.
  - Video: trước khi tải chỉ ảnh poster (độ tin cậy tối đa medium); xem nhiều khung sau khi tải là P4, xem mục P4 bên dưới.
- **P4 video nhiều khung đã làm (2026-11-17, dev log mục "Xem lại video / ảnh động sau khi tải")**: `-gender_verify_media` (app: ô "Kiểm tra video sau khi tải" trong bộ lọc, `download.gender.verifyMedia`), `POST /v1/frames` của model server, `lynxx/media_frames.py` (ffmpeg / PyAV cho video, Pillow cho GIF / APNG / WebP), `classify_frames` gộp theo §3.5. File không khớp bộ lọc được chuyển vào `_gender_mismatch/`, không xoá (AC-G16: test tự động, chạy thật trên media tổng hợp với model thật). **Khác plan**:
  - Khung được đọc ở model server (hoặc model trong tiến trình), không ở client: lõi của app không có Pillow / ffmpeg. Client chỉ nhận ra video / ảnh động qua các byte đầu rồi gửi cả file (≤ 200 MB).
  - Chưa có tập vàng nên ngưỡng §3.5 không chọn theo precision: t_hi = 0,35 (ngưỡng tag của một ảnh), t_lo = 0,2. Thuộc tính là từng tag của bộ luật `db` (không phải hai đầu ra F / M): nhãn chỉ giữ khi mọi tổ hợp tag luật chưa rõ cho cùng nhãn, nên một khung có nam (hay một tag nam ở vùng 0,2–0,35) đủ làm clip thành `unknown`.
  - Chỉ file của post mà tag chưa đủ kết luận (`needs_model`) mới được xem lại: tag đủ tin cậy vẫn thắng như §3.4. File đã có trước lượt tải và post của task parent không bị đụng tới.
  - Safety gate trên một khung cũng chuyển file vào `_gender_mismatch/` (không xoá: file nằm trong thư mục người dùng, gate có ngưỡng thấp nên có chặn nhầm).
  - **Chưa làm**: đánh giá trên 300 clip gán tay (AC-G17), cần người gán nhãn; badge riêng cho kết quả nhiều khung trong app (card job chỉ có số file đã chuyển).
- Còn lại của tầng 1: gán tập vàng rồi đo precision / thiên lệch (P1, §3.7; công cụ đã có, cần hai người gán), head A1 / phương án B, int8, catalog model, DirectML / CoreML, đánh giá video (AC-G17).
- **Bổ sung 2026-11-17 (dev log 7s)**: chế độ loại bỏ: `-gender_exclude` ở lõi / CLI, nút gạt Giữ / Loại bỏ trong app.
- **P0 đã làm (2026-11-16)**: `lynxx/gender.py`, `lynxx/gender_rules/{db,rx,en,gelbooru_generic}.json`, `lynxx/safety.py`, hai filter trong `_process_tags`, CLI `-gender/-gender_conf/-gender_strict/-gender_mode`, RPC `search.preview` (`gender`, `excludedBy`, `genderStats`, `blockedCount`), IPC (`genderFiltered`, `blocked`, `genderStats`), UI (chip, badge, thống kê, lightbox, card job), manifest `genderRules`/`safetyTags`, `tools/gender_coverage.py`, test `lynxx/tests/test_gender.py`.
- **Khác plan**: không chuẩn hoá qua `all_tag_aliases.json`. File đó là alias dùng để đặt tên file (ví dụ `male` → `1+boys`), dùng để suy nhãn sẽ sai. Mỗi bộ luật mang khoá `aliases` riêng (rx: `hetero`, `hetero_sex` → `straight`). Chưa có `-dump_gender` (câu hỏi mở §8.5).
- **Độ phủ đo được** (≥ medium): db 95,9 % (mới nhất) / 99,5 % (ngẫu nhiên, `random:200` vì `order:random` timeout trên toàn DB); rx 85,8 % / 94,6 %. Xem `docs/reports/`.
- Chưa làm (tầng 0): gán tập vàng và đo precision (P1, công cụ `lynxx/gender_eval.py` đã có), đo độ phủ e621.

## 0. Tóm tắt & quyết định chính

| # | Quyết định | Lý do |
|---|---|---|
| D1 | Nhãn được suy ra từ **hai thuộc tính nhị phân** `has_female`, `has_male` cộng một số cờ phụ (`futa`, `trap`, `other`…). Ba lớp `only_female` / `only_male` / `both` cùng `none` / `unknown` đều là hàm của hai thuộc tính này. | Khớp với cách booru gắn count tag (`1girl`, `1boy`). Model chỉ cần hai đầu ra sigmoid. Tầng 0 và Tầng 1 dùng chung một không gian nhãn. |
| D2 | Theo **quy ước đếm của booru**: futanari tính là nữ, otoko_no_ko/trap tính là nam, nhân vật mơ hồ không tính. Có cờ `-gender_strict` để loại các ca biên này. | Nhất quán với dữ liệu site, nên nhãn yếu ít mâu thuẫn. |
| D3 | **Ưu tiên precision**: `unknown` bị loại theo mặc định. Ngưỡng tin cậy mặc định là `medium`. | Người bật bộ lọc muốn ít lọt sai, chấp nhận thiếu vài post. |
| D4 | Tầng 0 là **filter cục bộ ở cuối chuỗi** trong `Downloader._process_tags`, không viết lại truy vấn gửi lên site. Ruleset theo site ở dạng JSON; extension tự mang ruleset của mình. | Không tốn lượt tag (Danbooru giới hạn 2 tag), ngữ nghĩa giống nhau trên mọi site, model chỉ chạy trên số post ít nhất. |
| D5 | **Blacklist an toàn cứng** nằm trong code (`lynxx/safety.py`). Là **bộ lọc riêng, mặc định tắt** (`-safety_blacklist`) (quyết định 2026-11-18, thay quyết định 2026-11-16); khi bật, nó chạy cho **mọi task, kể cả task parent**, trước mọi suy luận, và bật cả chốt an toàn của model. Preview không trả URL thumbnail của post bị chặn. Extension chỉ được thêm tag vào blacklist, không được bớt. | Danh sách cố định, không có đường nào để bớt tag. |
| D6 | Tầng 1 dùng **backbone đóng băng + head 2-sigmoid**. Baseline là zero-shot từ WD tagger v3. Ưu tiên WD v3 (đúng miền booru, có sẵn đầu ra làm safety gate) trừ khi SigLIP thắng rõ. Chỉ fine-tune toàn phần khi slice 3D/furry không đạt. | Rẻ, nhanh, dễ calibrate. Không phải giữ ảnh lâu. |
| D7 | Dataset **chỉ lưu embedding + metadata**; ảnh bị xoá ngay sau khi tính embedding. Split theo artist. Tập vàng gồm 3.000 post do 2 người gán độc lập. | Giảm lưu trữ nội dung nhạy cảm và tránh rò rỉ giữa train/test. |
| D8 | Model là gói **tải về tuỳ chọn** (≤ 120 MB int8), chạy bằng ONNX Runtime CPU trong sidecar/CLI. Ảnh lấy từ cache thumbnail sẵn có của Electron. **Không gửi gì ra ngoài.** | Riêng tư, không đội dung lượng bộ cài. |
| D9 | Video: trước khi tải chỉ suy luận trên 1 khung (thumbnail), độ tin cậy tối đa `medium`. Kiểm tra nhiều khung sau khi tải là tuỳ chọn ở P4. | Trước khi tải chỉ có thumbnail tĩnh. |

---

## 1. Định nghĩa nhãn

### 1.1 Không gian nhãn

Mỗi post có hai thuộc tính:
- `F = has_female`: có ít nhất một nhân vật được vẽ/được đếm là nữ.
- `M = has_male`: tương tự, cho nam.

| Nhãn | Điều kiện | Ví dụ tag |
|---|---|---|
| `only_female` | F=1, M=0 | db `1girl solo`; rx `female_only`; en `female solo` |
| `only_male` | F=0, M=1 | db `1boy male_focus`; rx `male_only`; en `male solo` |
| `both` | F=1, M=1 | db `1girl 1boy hetero`; rx `straight`; en `male/female` |
| `none` | Chắc chắn F=0 và M=0: không có nhân vật, hoặc chỉ có nhân vật mơ hồ | db `no_humans scenery` |
| `unknown` | Không đủ bằng chứng, hoặc độ tin cậy dưới ngưỡng | paheal `Character_Name` |

- **Độ tin cậy**: `high`, `medium` hoặc `low`. Tầng 1 ánh xạ xác suất đã calibrate về ba mức này (§3.4).
- **Cờ phụ**: `futa`, `trap`, `other` (nhân vật mơ hồ), `pov`, `bodypart`, `crowd`, `anthro`, `real`. Cờ dùng cho chế độ strict, cho tooltip giải thích và cho slice đánh giá.
- **Phạm vi**: nhãn mô tả *cách nhân vật hư cấu được vẽ và được site đếm*. Nó không phải là nhận định về giới của người thật. Ảnh người thật nằm ngoài phạm vi của model (§4.3).

### 1.2 Quy tắc biên

| Trường hợp | Quy tắc | Ghi chú |
|---|---|---|
| Futanari, dickgirl, gynomorph, herm, intersex (thể hiện nữ) | Đặt **F=1**, gắn cờ `futa`. Khi có cờ `futa`, tag `penis` **không** tạo M. | Danbooru đếm futanari là `1girl`. Với `-gender_strict`, post có cờ này bị loại khỏi `only_female`. |
| Andromorph, maleherm, cuntboy, male_with_breasts (thể hiện nam) | Đặt **M=1**, gắn cờ `futa` (`futaMale`). Khi có cờ này, tag bộ phận nữ (`pussy`, `breasts`) **không** tạo F. | e621 và rx có những tag này. |
| otoko_no_ko, trap, femboy, girly (rx) | Đặt **M=1**, gắn cờ `trap`. | Đây là ca khó nhất với model vì ngoại hình giống nữ. Model không được tự kết luận `only_female` cho slice này (xem §3.7). Strict loại post khỏi `only_male`. |
| reverse_trap (db) | Đặt **F=1**, gắn cờ `trap`. | |
| genderswap, rule_63, crossgender | Không phải bằng chứng. Count tag của ảnh hiện tại quyết định. | |
| 1other, androgynous, ambiguous_gender | Không cộng vào F hay M; gắn cờ `other`. Nếu chỉ có nhân vật mơ hồ → `none` (medium). Nếu có F hoặc M kèm nhân vật mơ hồ → nhãn `only_*` bị hạ tối đa xuống `medium`. | Strict loại mọi post có cờ `other` khỏi `only_*`. |
| Anthro, furry, feral | Đếm như người, theo tag giới của site. e621 áp `male`/`female` cho mọi nhân vật; db dùng `furry_female`/`furry_male`. Feral không có tag giới → `other`. | Gắn cờ `anthro`. Có slice đánh giá riêng. |
| Nhóm đông người (`6+girls`, `group`, `gangbang`, `orgy`) | Theo count tag. `crowd`/`everyone` mà không có count tag → `unknown`. Nhân vật nền không được tag thì không tính. | Gắn cờ `crowd`. Model chỉ được kết luận nếu độ tin cậy là `high`. |
| Chỉ một bộ phận cơ thể, POV, close-up | Bộ phận nam nhìn thấy được (`penis` khi không có cờ futa, `disembodied_penis`, `faceless_male`, `male_pov`) → **M=1**, tức post sẽ là `both` nếu có nữ. Đối xứng (từ 2026-11-20): bộ phận nữ (`pussy`, `vagina`, `breasts` và các cỡ, `faceless_female`, `female_pov`; khi không có cờ futa) → **F=1**, tức post sẽ là `both` nếu có nam. Close-up không có count tag và không có bộ phận phân biệt → `unknown`. Đồ chơi tình dục không tính. | Người chọn "chỉ nữ" thường không muốn POV nam, người chọn "chỉ nam" thường không muốn thấy nữ, nên tính F / M là lựa chọn bảo thủ. e621 chỉ dùng `female_pov`, `faceless_female`: ở đó nhân vật intersex nhiều, tag cơ thể không cho biết giới. |
| `no_humans`, scenery không có count tag | `none` | `no_humans` đi kèm `1girl` (robot, pokémon cái) thì vẫn theo count tag. |
| Ảnh thật, cosplay, `real_person` | Tầng 0 vẫn suy nhãn từ tag. **Tầng 1 không chạy.** | Gắn cờ `real` (§4.3). |
| Tag mâu thuẫn (`female_only` + `1boy`) | Count tag thắng marker loại trừ; độ tin cậy `low`. | Ghi log khi `-verbose`. |

---

## 2. Tầng 0 — lọc theo tag (không cần ML)

### 2.1 Bảng ánh xạ tag theo site

Mức bằng chứng: **H** (mạnh), **m** (vừa). Khi so khớp, tag được chuyển về chữ thường và chuẩn hoá qua `all_tag_aliases.json` (ví dụ `hetero` → `straight`, `futa_*` → `futanari`). Bộ lọc nhóm loại trừ hiện có chưa làm bước chuẩn hoá alias này.

**Danbooru (`db`)**, completeness prior 0,95:

| Bằng chứng | Tag |
|---|---|
| F (H) | `1girl`, `2girls`, `3girls`, `4girls`, `5girls`, `6+girls`, `multiple_girls`, `furry_female` |
| M (H) | `1boy` … `6+boys`, `multiple_boys`, `male_focus`, `furry_male` |
| both (H) | `hetero` |
| F / M (m) | `yuri` / `yaoi` |
| other | `1other`, `2others`, `multiple_others`, `6+others`, `androgynous` |
| none | `no_humans` |
| cờ futa / trap | `futanari`, `futa_with_female`, `futa_with_male`, `futa_with_futa` / `trap`, `reverse_trap`. Snapshot không có `otoko_no_ko`. `cuntboy`, `male_with_breasts` → M + futa |
| M từ bộ phận cơ thể | `penis` (khi không có futa), `disembodied_penis`, `faceless_male` |
| F từ bộ phận cơ thể | `pussy`, `breasts`, `large_breasts`, `huge_breasts`, `medium_breasts`, `small_breasts`, `gigantic_breasts`, `faceless_female`, `female_pov` (khi không có futa) |
| real | `photo_(medium)`, `cosplay_photo` |

**Rule34.xxx (`rx`)**, completeness prior 0,85:
- `bb` (Bbooru) có bảng riêng `bb.json` từ 2026-11-19 (dev log 7av): bảng này cộng thêm các tag đếm kiểu Danbooru (xem mục Bbooru bên dưới). Trước đó `bb` dùng chung bảng `rx`, vì `2tags/bb_tags.json` từng là bản sao của `rx_tags.json`.
- `rs` (rule34.us) và `xb` (xbooru) dùng bản `gelbooru_generic`, prior lần lượt 0,80 và 0,75. Bản này thêm `hetero` (và có `1girl`, `female_focus`, `male_focus` như `rx`).

| Bằng chứng | Tag |
|---|---|
| exclusive | `female_only` → `only_female`; `male_only` → `only_male`; `futa_only` → `only_female` + cờ futa; `solo_female` / `solo_male` |
| both (H) | `straight` (gồm cả alias `hetero`, `hetero_sex`) |
| F (H) | `1girls`, `2girls`, `3girls`, `multiple_girls`, `female`, `1girl` (~1,3M post kiểu Danbooru), `female_focus` |
| M (H) | `1boy`, `2boys`, `3boys`, `multiple_boys`, `male`, `male_focus` |
| F / M (m) | `yuri`, `lesbian` / `yaoi`, `gay` |
| cờ futa | `futanari`, `1futa`, `multiple_futa`, `dickgirl`, `intersex`, `gynomorph`, `herm`; `cuntboy`, `andromorph`, `maleherm`, `male_with_breasts` → M + futa |
| cờ trap | `femboy`, `trap`, `otoko_no_ko`, `girly` |
| M từ bộ phận cơ thể | `penis` (khi không có futa), `disembodied_penis`, `faceless_male`, `male_pov` |
| F từ bộ phận cơ thể | `pussy`, `vagina`, `vulva`, `disembodied_pussy`, `faceless_female`, `female_pov`, `breasts` và các cỡ (`large_`, `big_`, `huge_`, `medium_`, `small_`, `hyper_`, `gigantic_breasts`) (khi không có futa) |
| other | `androgynous`, `ambiguous_gender` |
| real | `real`, `photo`, `real_person` (tranh vẽ người thật có thể nhận diện; loại khỏi dataset) |

Ghi chú cho rx: `female` (10,4M post) và `male` (6,5M) được gắn rất rộng. Chúng tốt để chứng minh *có mặt*, nhưng *vắng mặt* thì kém tin cậy hơn. Vì vậy một nhãn `only_*` dựa trên count tag mà không có marker exclusive chỉ đạt **medium** trên rx.

**Bbooru (`bb`, extension)**, completeness prior 0,85 (như rx): bảng `rx` cộng các tag đếm của Danbooru.
- Bbooru gom post của nhiều site, và tự gắn thêm tag kiểu Rule34 cho post kiểu Danbooru: trong mẫu đo, chỉ 3/6.902 post có `1girl` mà thiếu cả `1girls` lẫn `female`.
- Thêm so với `rx` (từ 2026-11-20 `rx` cũng có `1girl`, `male_focus`):
  - F (H): `4girls`, `5girls`, `6+girls`, `furry_female`;
  - M (H): `4boys`, `5boys`, `6+boys`, `furry_male`;
  - other: `1other`, `2others`, `multiple_others`, `6+others`;
  - cờ futa: `futa_with_female`, `futa_with_male`, `futa_with_futa`; cờ trap nữ: `reverse_trap`;
  - real: `photo_(medium)`, `cosplay_photo`.
- **Đo 2026-11-19** (`gender_eval sample --module bb`, chỉ metadata, 14 request):
  - Mẫu: 4.095 post mới (8 truy vấn: `solo`, `breasts`, `penis`, `1girl`, `1girls`, `anthro`, `highres`, `male`) và 2.807 post cũ (`id:<3000000`, `id:<10000000`, `id:<20000000`), sau khi loại theo danh sách chặn và ảnh người thật.
  - Độ phủ ≥ medium: post mới 96,2 % với `rx`, 96,4 % với `bb`; post cũ 85,8 % và 86,0 %.
  - `bb` đổi nhãn 19 post (0,3 %), mọi trường hợp đều đi về nhãn đúng hơn: `1girl penis` từ only_male thành both, `furry_female` từ unknown thành only_female, `1other` từ unknown thành none. `gelbooru_generic` kém hơn `rx`, vì thiếu `solo_female` / `solo_male`.
  - Luật `solo` của e621 không được thêm: nó nâng 17–25 % post từ medium lên high, nhưng chưa có tập vàng để kiểm.
  - Post còn `unknown` (3 % post mới, 14 % post cũ) phần lớn chỉ có tag cơ thể (`breasts`, `pussy`) hoặc `ai_generated`, không có tag đếm. Đây là phần việc của tầng 1. Từ 2026-11-20 tag cơ thể nữ là bằng chứng F, nên phần post chỉ có tag cơ thể nay có nhãn; số trên là của bảng cũ, chưa đo lại.
  - Đây là độ phủ và độ đồng ý giữa các bảng, chưa phải precision: chưa có nhãn của người.

**e621 (`en`, extension)**, completeness prior 0,92:

| Bằng chứng | Tag |
|---|---|
| F / M (H) | `female` / `male` |
| cặp | Tag dạng `a/b` (`male/female`, `male/male`, `female/female`, `intersex/male`, `gynomorph/male`…) được tách thành hai vế; mỗi vế đóng góp bằng chứng theo giới. |
| số lượng | `solo`, `duo`, `trio`, `group`. `solo` + đúng một giới → `only_*` **H**. |
| futa | `intersex`, `gynomorph`, `herm` → F + futa; `andromorph`, `maleherm` → M + futa |
| trap / other | `femboy` → M + trap; `ambiguous_gender` → other |
| M từ bộ phận cơ thể | `male_pov`, `faceless_male` |
| F từ bộ phận cơ thể | `female_pov`, `faceless_female` |
| real | `photography_(artwork)`, `real` |

**Paheal (`rp`), rule34hentai (`rn`)**: tag tự do, viết hoa, chủ yếu là tên nhân vật/series. Gần như không có count tag: rp có `yuri` 2.573 post và `1girls` 13 post. Ruleset tối thiểu (so khớp không phân biệt hoa thường) sẽ cho phần lớn post nhãn `unknown`, nên các site này phụ thuộc Tầng 1. UI cảnh báo khi người dùng bật bộ lọc ở chế độ "chỉ tag" trên các site này.

### 2.2 Luật suy nhãn

```python
def infer_from_tags(tags: Iterable[str], rules: GenderRules) -> GenderResult:
    t = rules.normalize(tags)                          # lower() + all_tag_aliases.json
    futa = bool(t & rules.flags['futa'])
    F = rules.strongest(t, 'female')                   # None | MEDIUM | HIGH
    M = rules.strongest(t, 'male')
    if t & rules.male_bodypart and not futa: M = HIGH
    if t & rules.female_bodypart and not futa: F = HIGH  # 2026-11-20, đối xứng với bộ phận nam
    if futa and F is None and not (t & rules.futa_male_presenting): F = HIGH
    other, conflict = bool(t & rules.other), False
    if excl := rules.exclusive_label(t):              # female_only / male_only / straight / solo+giới
        if excl.consistent_with(F, M): return GenderResult(excl, HIGH, evidence=...)
        conflict = True                                # count tag thắng, độ tin cậy LOW ở dưới
    if F and M: label, conf = BOTH, min(F, M)
    elif F or M:
        label, conf = (ONLY_FEMALE if F else ONLY_MALE), (F or M)
        if rules.completeness < 0.9: conf = min(conf, MEDIUM)   # suy "vắng mặt" trên site gắn tag lỏng
        if other: conf = min(conf, MEDIUM)
    elif t & rules.none or other: label, conf = NONE, (HIGH if rules.completeness >= 0.9 and not other else MEDIUM)
    else: return GenderResult(UNKNOWN, None)
    if conflict: conf = LOW
    return GenderResult(label, conf, flags=..., evidence=...)
```

Quyết định cuối cho từng post khi chạy `-gender S -gender_conf C [-gender_strict]`:
- Nếu `conf < C`, nhãn trở thành `unknown`.
- Nếu bật strict, post có cờ `futa`, `trap` hoặc `other` không thuộc `only_*`.
- **Giữ** post khi nhãn thuộc S.

### 2.3 Tích hợp vào pipeline

| File | Thay đổi |
|---|---|
| `lynxx/gender.py` (mới) | `GenderLabel` (StrEnum), `GenderConf` (IntEnum), `GenderResult` (dataclass, slots), `GenderRules.load(abbr)`, `infer_from_tags`. Thuần Python, không phụ thuộc mới. |
| `lynxx/gender_rules/{db,rx,bb,en,gelbooru_generic}.json` (mới; `bb` từ 2026-11-19) | Gồm `schema`, `completeness`, `female`/`male` {high, medium}, `exclusive`, `other`, `none`, `flags`, `maleBodypart`, `femaleBodypart` (từ 2026-11-20). Extension khai báo `"genderRules": "gender_rules.json"` trong manifest (trường tuỳ chọn, `apiVersion` vẫn là 1). Module không có ruleset dùng `gelbooru_generic` với completeness 0,5. |
| `lynxx/safety.py` (mới) | Blacklist cứng (§4.1). |
| `lynxx/defines.py` | Thêm `ModuleCaps.GENDER_TAGS = 'gender_tags'` (module có ruleset đã được đo). Thêm vào `CAPABILITIES` của `DownloaderDb` và `DownloaderRx`. Extension có `genderRules` được thêm tự động. |
| `lynxx/download_base.py` | Thuộc tính `gender_select: frozenset[GenderLabel]`, `gender_mode`, `gender_min_conf`, `gender_strict`, `gender_results: dict[str, GenderResult]`. Kết quả để ở dict riêng, keyed theo `idstring` giống `item_info_dict_per_task`, **không** thêm slot vào `ItemInfo`, nhờ vậy định dạng dump/merge không đổi. Thêm `_filter_items_safety(parents)` và `_filter_items_by_gender(parents)`. Cách xoá item giống `_filter_items_missing_required_tags`: xoá khỏi `item_info_dict_per_task` và `items_raw_per_task`, thêm vào `filtered_out_ids_cache`, gọi `parents.discard(item_id)`. |
| `lynxx/download.py` | Gắn vào `_process_tags` (xem dưới); đọc tham số trong `_parse_args`; kiểm tra xung đột trong `_solve_argument_conflicts`. |

Vị trí trong `Downloader._process_tags`:

```python
        task_parents: set[str] = set()
        self._extract_cur_task_infos(task_parents)
        # MỚI (1): an toàn — chạy cho MỌI task, kể cả task parent do -include_parchi sinh ra
        self._apply_filter(DownloaderStates.FILTERING_ITEMS4, self._filter_items_safety, task_parents)

        if self.current_task_num <= self.orig_tasks_count:
            self._apply_filter(..., self._filter_items_matching_negative_and_groups, task_parents)
            self._apply_filter(..., self._filter_items_missing_required_tags, task_parents)
            self._apply_filter(..., self._filter_items_by_module_filters, task_parents)
            # MỚI (2): cuối chuỗi — chạy trên ít item nhất; Tầng 1 (nếu bật) chỉ chạy cho post còn unknown
            self._apply_filter(DownloaderStates.FILTERING_ITEMS4, self._filter_items_by_gender, task_parents)
```

Lý do chọn vị trí này:
- Cần `ItemInfo.tags`, mà tag chỉ có sau `_extract_cur_task_infos`. Riêng `rs` (`_can_extract_item_info_without_fetch() == False`) đã lấy tag từng post ở bước đó.
- Các filter rẻ chạy trước, nên số post phải xử lý ở bước cuối là ít nhất.
- Bộ lọc giới tính **không** áp cho task parent, giống quy ước hiện tại của nhóm loại trừ và tag bắt buộc: post cha/con là ngữ cảnh người dùng chủ động yêu cầu.
- Bộ lọc an toàn thì áp cho tất cả task.

Trace in ra: `Filtered out N / T items by gender (only_male: a, both: b, unknown: c)`.

Với `-gender` trên site `rx`, có thể đẩy luật tag lên server (ví dụ thêm `-male -1boy`) để giảm số trang phải tải. Việc này để sau P3 và chỉ bật theo lựa chọn (§8).

### 2.4 Tham số CLI

Đặt tên theo kiểu sẵn có (`-skip_img`, `-dump_tags`). Hằng số đặt trong `gui_defines.py`, help trong `help.py`, validator `valid_gender_list` trong `validators.py`.

| Tham số | Giá trị | Mặc định | Ý nghĩa |
|---|---|---|---|
| `-gender` | Danh sách phân cách bằng phẩy, lấy từ `only_female,only_male,both,none,unknown` | tắt | Giữ post có nhãn thuộc danh sách |
| `-gender_mode` | `tags` \| `model` | `tags` | `model` = dùng tag, rồi chạy model cho post `unknown` hoặc có độ tin cậy dưới ngưỡng |
| `-gender_conf` | `low` \| `medium` \| `high` | `medium` | Độ tin cậy tối thiểu để nhãn được coi là đã quyết định |
| `-gender_strict` | flag | tắt | Loại post có cờ `futa`, `trap`, `other` khỏi `only_*` |

Ví dụ: `python -m lynxx -module db kagamine_rin -gender only_female -path out/`

Xung đột:
- `-gender_mode model` khi chưa cài model hoặc thiếu `onnxruntime` → lỗi fatal kèm hướng dẫn.
- `-gender` trên module không có `GENDER_TAGS` ở chế độ `tags` → cảnh báo "phần lớn post sẽ là unknown".

Tk GUI cũ (`gui.py`) không được cập nhật.

### 2.5 RPC sidecar & IPC

- **`search.preview`** nhận thêm tham số `gender: {select: [...], mode, conf, strict}`.
  - `server.py` chuyển tham số này thành CLI args qua helper `_gender_args(p)`; `main.js` chỉ việc truyền `q.gender`.
  - Mỗi item có thêm `gender: {label, conf, source: 'tags'|'model'|'tags+model', pF?, pM?, flags[], evidence[]}` và `excludedBy: ['neg'|'required'|'gender']`. `excluded` hiện có được tính lại để gồm cả lý do giới tính.
  - Cấp trang có thêm `genderStats: {only_female: n, …}` và `blockedCount`.
  - Post bị chặn bởi bộ lọc an toàn **không có trong `items`**.
- **Method mới**:

| Method | Mô tả |
|---|---|
| `gender.status` | Model đã cài chưa, phiên bản, dung lượng, backend ORT, `onnxruntime` có sẵn không |
| `gender.install` / `gender.remove` | Tải hoặc gỡ gói model; tiến độ qua `notify('gender.installProgress')` |
| `gender.classify` | Nhận `{module, page, ids[]}` và trả `{jobId}`. Sidecar dùng lại tag/URL của trang preview gần nhất (cache trong RAM), không tin URL do client gửi lên. Kết quả về dần qua `notify('gender.result', {jobId, results})`. Chạy trên `ThreadPoolExecutor` riêng, **không** giữ `_core_lock`. |
| `gender.cancel` | Huỷ job classify |

- **IPC tiến độ** (`lynxx_cmd._start_ipc_threads`): thêm `genderFiltered`, `genderUnknown`, `blocked`, `modelProcessed`. Dòng `DONE` có thêm `genderStats`.
- `search.count` vẫn trả số **trước** khi lọc giới tính. UI ghi "≈ N (trước lọc giới tính)".

### 2.6 Ước lượng độ phủ

Snapshot `2tags/*_tags.json` (số post của tag):

| Site | Tag nữ | Tag nam | Cả hai / khác |
|---|---|---|---|
| db | `1girl` 8,66M · `multiple_girls` 2,18M | `1boy` 2,20M · `male_focus` 1,17M | `hetero` 846k · `no_humans` 243k · `1other` 135k · `futanari` 54k |
| rx | `female` 10,4M · `1girls` 5,38M · `female_only` 2,33M | `male` 6,50M · `1boy` 3,16M · `male_only` 1,38M | `straight` 1,16M · `futanari` 1,24M · `femboy` 487k |
| en | `female` 3,30M | `male` 3,31M | `male/female` 877k · `ambiguous_gender` 429k · `intersex` 332k |
| xb | `1girl` 334k · `female` 182k | `1boy` 95k · `male` 90k | — |
| rp | `yuri` 2,6k | — | gần như không có |

Giả thuyết độ phủ (tỉ lệ post có nhãn khác `unknown` ở mức ≥ medium), **cần đo ở P0**:

| Site | Độ phủ |
|---|---|
| db | 92–97 % |
| en | 85–93 % |
| rx/bb | 80–90 % (bb đo 2026-11-19: 86–96 %, xem §2.1) |
| rs/xb | 70–85 % |
| rp/rn | < 15 % |

Cách đo: script `tools/gender_coverage.py --module db --n 2000`.
- Mẫu: 2.000 post mới nhất cộng 2.000 post ngẫu nhiên (`order:random` / `sort:random`), lấy qua downloader hiện có.
- Đầu ra: tỉ lệ theo nhãn × độ tin cậy, tỉ lệ `unknown`, tỉ lệ mâu thuẫn, tỉ lệ bị chặn.
- Kết quả là JSON, **không chứa ảnh**.
- Precision của Tầng 0 được đo trên tập vàng (§3.2).

---

## 3. Tầng 1 — model thị giác cho post thiếu/nhiễu tag

### 3.1 Khi nào model chạy

Model chỉ chạy khi **tất cả** điều kiện sau đúng:
- `-gender_mode model`;
- post không bị chặn;
- Tầng 0 ra `unknown` hoặc độ tin cậy dưới `-gender_conf`;
- post không có cờ `real`;
- module có `ModuleCaps.PREVIEW` (có URL thumbnail).

### 3.2 Dataset từ nhãn yếu

- **Nguồn**: db, en, rx (completeness cao). xb và rs dùng để bổ sung slice. Metadata lấy qua chính các downloader của Lynxx, tôn trọng rate limit (e621 2 request/s) và ToS.
- **Nhãn yếu**: kết quả Tầng 0 **chỉ ở mức `high`**, ánh xạ về mục tiêu (F, M); `none` ứng với (0, 0).
- **Thứ tự loại trừ** (bắt buộc, có log đếm):
  1. Blacklist cứng §4.1, áp lên tag và alias.
  2. Danh sách loại bổ sung §4.2.
  3. Tag ảnh thật / người thật (§4.3).
  4. Safety gate của model (§4.2).
  5. Khử trùng theo md5 và pHash.
  6. Post đã xoá hoặc không có file.
- **Cân bằng**: khoảng 200k post train (~50k mỗi lớp `only_female` / `only_male` / `both` / `none`), stratify theo site (db/rx/en mỗi site ~⅓) và phong cách. Thêm val 20k và calib 10k.
- **Split theo artist**: lấy hash của tag artist, tránh rò rỉ phong cách giữa các tập. Post không có artist được gom theo md5 prefix.
- **Hai góc nhìn mỗi post**: thumbnail (`preview_url`, 150–180 px) và sample (~850 px). Tính embedding cho cả hai để model chịu được đầu vào thumbnail.
- **Lưu trữ**:
  - Ảnh chỉ nằm trong thư mục tạm và bị **xoá ngay sau khi tính embedding**.
  - Chỉ giữ `embeddings.f16.npy` và `meta.parquet` (site, id, md5, nhãn, cờ, slice, hash artist).
  - Thí nghiệm fine-tune toàn phần (nếu cần) giữ ảnh ở thư mục cục bộ không đồng bộ cloud và xoá khi thí nghiệm kết thúc.
  - Manifest dữ liệu (post id + md5, không có ảnh) được version trong `tools/gender/` để tái lập.
- **Tập vàng gán tay**:
  - 3.000 post: 1.000 db, 1.000 rx, 1.000 en, cộng 300 rp/xb.
  - Stratify theo nhãn Tầng 0 và phong cách. **≥ 30 % thuộc slice "thiếu tag"** (≤ 8 tag general hoặc không có tag giới); đây chính là miền mà model nhắm tới, và vì không có nhãn yếu nên bắt buộc phải gán tay.
  - Hai người gán độc lập theo §1, yêu cầu Cohen's κ ≥ 0,8; bất đồng được trọng tài quyết định.
  - Người gán thấy nội dung nghi là trẻ vị thành niên → loại ngay, không lưu, và bổ sung tag liên quan vào §4.1.
  - Tập vàng cũng dùng để đo precision của Tầng 0.

### 3.3 Kiến trúc

| Phương án | Backbone | Input | ONNX fp32 → int8 (ước lượng) | Ưu | Nhược |
|---|---|---|---|---|---|
| **A0** zero-shot | WD tagger v3 (ViT-B/16, SmilingWolf) | 448 | ~380 → ~100 MB | Không cần train. Đầu ra đã có `1girl`, `1boy`, `multiple_*` nên **dùng lại được ruleset db**. Có sẵn đầu ra làm safety gate. | 448 px nặng (~150–250 ms/ảnh CPU); thiên về anime 2D |
| **A1** head | Embedding pooled từ WD v3 + MLP | 448 | head < 1 MB | Đúng miền booru; calibrate được theo site | Độ trễ như A0 |
| **B** head | SigLIP-B/16 (224/256) + MLP | 224 | ~370 → ~95 MB | Nhanh hơn ~3–4×; tổng quát hơn cho 3D/western | Kém tinh tế với anime; không có safety gate |
| **C** head | CLIP ViT-B/32 (OpenAI) | 224 | ~350 → ~90 MB | Nhanh nhất | Yếu nhất |
| **D** fine-tune toàn phần | ConvNeXt-T / ViT-S, hoặc A/B mở khoá | 224–384 | 30–110 MB | Trần chất lượng cao nhất | Cần GPU; phải giữ ảnh lâu; dễ học theo nhiễu nhãn; rủi ro dữ liệu cao hơn |

- **Head**: MLP `d → 512 → 2` (GELU, dropout 0,2), hai đầu ra sigmoid `has_female` và `has_male`.
  - Loss: BCE với label smoothing 0,05 (vì nhãn yếu), sampler cân bằng lớp.
  - Huấn luyện: AdamW, lr 1e-3, ≤ 20 epoch, early stopping theo macro-F1 trên val. Vì embedding đã được cache, mỗi lần train chỉ mất vài phút.
  - Ablation: head softmax 4 lớp.
- **Thứ tự thử nghiệm**: A0 → A1 và B → chọn theo §7 và độ trễ. D chỉ làm khi slice 3D hoặc furry trượt tiêu chí.
- **Export**: gộp backbone và head thành một graph ONNX (opset 17). Lượng tử hoá bằng `quantize_dynamic` int8; chấp nhận nếu macro-F1 giảm ≤ 1 điểm. Nếu chọn WD, giữ đầu ra safety gate trong graph dưới tên `safety_score`.

### 3.4 Ngưỡng, calibration, suy ra 3 lớp

- **Calibration**: temperature scaling cho từng đầu ra × từng site (site luôn biết lúc suy luận), fit trên tập calib. Báo cáo ECE (15 bin) trước và sau; nếu ECE > 0,05 thì thử isotonic.
- **Dải quyết định**: với k ∈ {F, M}:

  | Điều kiện | Kết luận |
  |---|---|
  | p_k ≥ t_hi_k | có mặt |
  | p_k ≤ t_lo_k | vắng mặt |
  | còn lại | không chắc |

  Chỉ suy ra lớp khi **cả hai** đầu ra đã quyết định; ngược lại nhãn là `unknown`. Ngưỡng được chọn trên val sao cho **precision ≥ 0,92 cho mỗi lớp** `only_*`/`both` và độ phủ lớn nhất. Ngưỡng ghi vào manifest model theo từng site.
- **Ánh xạ độ tin cậy**: P(lớp) tính theo p_F·(1−p_M), (1−p_F)·p_M, p_F·p_M hoặc (1−p_F)·(1−p_M).

  | P(lớp) | Mức |
  |---|---|
  | ≥ 0,95 | high |
  | ≥ 0,85 | medium |
  | thấp hơn | low |

- **Kết hợp với Tầng 0**:

  | Tầng 0 | Model | Kết quả |
  |---|---|---|
  | Độ tin cậy ≥ `-gender_conf` | không chạy | Theo tag |
  | Dưới ngưỡng nhưng có nhãn | Đồng ý | Nhãn đó, độ tin cậy lấy mức cao hơn, `source=tags+model` |
  | Dưới ngưỡng nhưng có nhãn | Bất đồng | `unknown` (bảo thủ) |
  | `unknown` | Quyết định | Theo model, `source=model` |
  | `unknown` | Không chắc | `unknown` |
  | Cờ `trap` | Bất kỳ | Model không được đổi M thành F |

### 3.5 Video & GIF

- **Trước khi tải**: chỉ có thumbnail tĩnh của video, nên suy luận 1 khung và độ tin cậy tối đa là `medium`. Với mặc định `-gender_conf medium`, kết quả này vẫn dùng được.
- **Sau khi tải** (P4, tuỳ chọn `-gender_verify_media`):
  - Video: lấy 8 khung đều nhau, bỏ 5 % đầu và cuối. Dùng `ffmpeg` có sẵn trên máy hoặc PyAV.
  - GIF, APNG, WebP động: dùng Pillow `ImageSequence`, tối đa 8 khung.
  - Gộp theo từng thuộc tính: *có mặt* nếu ≥ 2 khung có p ≥ t_hi (clip < 3 khung thì chỉ cần 1); *vắng mặt* nếu mọi khung có p ≤ t_lo; còn lại là `unknown`.
  - File không khớp được **chuyển** vào thư mục con `_gender_mismatch/`, không xoá.
- **Đánh giá**: 300 video/GIF gán tay theo toàn bộ clip.

### 3.6 Chạy cục bộ (ONNX Runtime)

- **Module** `lynxx/gender_model.py`:
  - Import lazy `onnxruntime`, `numpy`, `Pillow`. Đây là extras tuỳ chọn `lynxx[gender]`; `requirements.txt` của lõi không đổi.
  - Mặc định dùng `CPUExecutionProvider`. Nếu có thì dùng thêm DirectML (Windows) hoặc CoreML (macOS).
  - `intra_op_num_threads = min(4, cpu_count // 2)`, batch 16.
  - Session là singleton, giải phóng sau 5 phút rảnh.
  - CLI và sidecar dùng chung module này.
- **Nguồn ảnh**:
  - Đọc trước từ cache thumbnail của Electron, `data_dir()/cache/thumbs/<sha1(url)><ext>`. Khoá này giống `registerThumbProtocol` trong `app/main.js`, nên ảnh đã hiển thị trên lưới không bị tải lại.
  - Thiếu thì tải bằng session mạng của downloader: cùng proxy, header, cookie, cùng giới hạn luồng, và chỉ tới host/CDN của site đang truy vấn.
- **Độ phân giải**:
  - Mặc định dùng thumbnail.
  - Tuỳ chọn "Độ chính xác cao" dùng `sample_url` khi cạnh ngắn của thumbnail < 200 px (thumbnail rx ~150 px). Tốn thêm băng thông.
  - Có bật mặc định hay không sẽ quyết định theo số đo accuracy theo độ phân giải ở P2.
- **Cache kết quả**:
  - SQLite `data_dir()/cache/gender.sqlite`, khoá (site, post_id, md5, model_version) → pF, pM, safety_score.
  - **Không lưu ảnh hay embedding.** Xoá được trong Settings.

### 3.7 Metric & đánh giá thiên lệch

- **Metric chính**, đo trên tập vàng ở điểm vận hành:
  - precision theo lớp (`only_female`, `only_male`, `both`);
  - độ phủ (tỉ lệ khác `unknown`);
  - macro-F1 trên 4 lớp (gồm `none`) khi không abstain;
  - confusion matrix 4×4 kèm cột `unknown`;
  - ECE;
  - đường risk–coverage.
- **Slice**:
  - Site: db, rx, en, xb, rp.
  - Phong cách: 2D anime, 3D render, furry/anthro, western/cartoon, `ai-generated`, monochrome/sketch.
  - Ca biên: futa, trap/femboy, POV/bộ phận cơ thể, đông người ≥ 6, `muscular_female`, `dark_skin`.
  - Đầu vào: thumbnail vs sample. Mật độ tag: thiếu tag vs đủ tag. Phương tiện: ảnh vs thumbnail video.
- **Luật thiên lệch**:
  - Slice có n ≥ 150 và precision thấp hơn mức toàn cục > 5 điểm:
    - nếu là slice site: nâng ngưỡng riêng của site đó;
    - nếu là slice phong cách: ghi là giới hạn đã biết, hiện trong tooltip của badge "·AI", và chặn phát hành nếu precision < 0,85.
  - Slice trap/femboy được kỳ vọng kém; luật ở §3.4 bảo đảm model không đẩy các post này sang `only_female`.
- **Báo cáo** mỗi phiên bản model: bảng metric, các slice, ngưỡng, ECE, độ trễ. Báo cáo đi kèm manifest model.

---

## 4. An toàn dữ liệu (bắt buộc)

### 4.1 Blacklist cứng: nội dung gợi ý trẻ vị thành niên

**Nguyên tắc**:
1. Danh sách là `frozenset` cộng regex **hard-code trong `lynxx/safety.py`**. Không đọc từ settings, CLI, biến môi trường, `2tags/` hay manifest.
2. **Không có cờ tắt.** Extension chỉ được **thêm** tag qua trường manifest `safetyTags`, không được bớt.
3. Danh sách cho phép (allowlist) cho false positive chỉ được sửa trong code, qua review.

**Phạm vi áp dụng**:
- (a) `_filter_items_safety` cho mọi task, kể cả task parent, mỗi khi bật `-gender` ở bất kỳ chế độ nào.
- (b) `search.preview` có tham số `gender`: post bị chặn bị **bỏ khỏi `items`**, không trả `preview`/`sample` URL, nên renderer không tải thumbnail kể cả khi bật "Hiện post bị lọc". Chỉ trả `blockedCount`.
- (c) Công cụ dựng dataset và công cụ gán tập vàng: luôn áp, không phụ thuộc tham số.
- (d) Model **không bao giờ** chạy trên post bị chặn.
- (e) Bỏ mọi entry tương ứng khỏi cache.

Mở rộng ra toàn app: **đã chốt là không** (2026-11-16). Từ 2026-11-18 blacklist là bộ lọc riêng, mặc định tắt, không đi kèm bộ lọc giới tính, xem §8.1.

**Cách so khớp**: tag được chuyển về chữ thường và chuẩn hoá alias, rồi so khớp đúng (exact) **hoặc** khớp regex token:

```
(^|_)(loli|lolicon|shota|shotacon|toddler|toddlercon|child|children|kid|kids|infant|baby|newborn|
      preteen|underage|underaged|minor|cub|cubs|young|younger|juvenile|tween|kindergarten|elementary)(_|$)
```

Chấp nhận false positive như `baby_5`, `kid_icarus`, `young_justice` vì an toàn quan trọng hơn.

**Tag cụ thể theo site** (ngoài phần regex đã bắt; số post theo snapshot):

| Site | Tag |
|---|---|
| db | `loli` 216k, `shota` 32k, `child` 83k, `aged_down` 92k, `randoseru` 20k, `oppai_loli` 11k, `baby` 9k, `kindergarten_uniform` 6k, `mesugaki` 5k, `onee-shota` 18k, `onee-loli` 5k, `age_regression`, `toddlercon`, `toddler`, `lolidom`, `shotadom`, `diaper` |
| rx / bb | `young` 113k, `teenager` 160k, `younger_female` 74k, `younger_male` 55k, `young_female` 22k, `cub` 19k, `teen` 15k, `aged_down`, `age_regression`, `randoseru`, `mesugaki`, `oppai_loli`, `shotacon`, `very_young`, `juvenile`, `tween`, `young_(lore)`, `diaper`. `loli`/`shota` gần như không tồn tại vì site cấm tag này, nên **phải** bắt qua `young*` và `teen*`. |
| en | `young` 307k, `child` 44k, `loli` 38k, `shota` 42k, `young_male` 94k, `young_female` 71k, `diaper` 42k, `toddler` 4k, `child_on_child`, `aged_down`, `age_regression`, `kodomo`, `young_(lore)`. `cub` đã bị alias thành `young`. |
| rs | Như rx, thêm `lolicon`, `underage`, `jailbait`, `pedophilia`, `futa_loli`, `straight_shota`, `infant`, `kids` |
| xb | Như rx, thêm `preteen`, `underage`, `underaged`, `pedophilia`, `straight_shota`, `toddlercon`, `infant` |
| rp / rn | `kid`, `baby`, `cub`, `child`, `shotacon`, `kindergarten`. Tag tự do nên so khớp không phân biệt hoa thường và luôn qua regex. |

`teen`/`teenager` bị chặn dù có thể gồm người 18–19 tuổi, vì không kiểm chứng được tuổi. `diaper` bị chặn theo hướng bảo thủ.

Unit test bắt buộc:
- mọi tag ở bảng trên đều bị chặn;
- không có API hay tham số nào làm `safety.is_blocked()` trả `False` cho các tag đó;
- extension không gỡ được tag nào.

### 4.2 Lớp loại bổ sung (dataset) và safety gate

- **Chỉ loại khỏi dataset, không chặn kết quả**: `petite`, `flat_chest`, `shortstack`, `school_uniform`, `age_difference`, `father_and_daughter`, `mother_and_son`, `aunt_and_nephew`. Các tag này không chứng minh nhân vật là trẻ vị thành niên, nhưng loại đi giúp model không học từ vùng biên.
- **Safety gate từ model**: nếu backbone là WD v3, dùng đầu ra **có sẵn** `loli`/`shota` làm `safety_score`. Điểm ≥ 0,15 (ngưỡng thấp, bảo thủ) thì loại khỏi dataset và chặn kết quả ở chế độ model.
  - Ta **không** thu thập tập dương tính và **không** tự huấn luyện bộ phát hiện loại nội dung này. Vì vậy recall của gate không được đo nội bộ; ta chỉ đo tỉ lệ chặn nhầm trên tập vàng sạch.
  - Gate chỉ là lớp phòng thủ bổ sung. **Blacklist tag là kiểm soát chính.** Rủi ro còn lại (post không có tag và gate bỏ sót) được ghi rõ trong tài liệu người dùng.

### 4.3 Không dùng ảnh người thật

- **Dataset** chỉ gồm tranh vẽ 2D và CG 3D. Loại `photo_(medium)`, `cosplay_photo`, `photo`, `real`, `photography_(artwork)`, và cả `real_person` (tranh vẽ người thật có thể nhận diện).
- **Lúc suy luận**: post có cờ `real` không đi qua model. Ảnh thật nhưng không được tag `real` vẫn có thể lọt vào model; UI ghi rõ "Model chỉ dành cho tranh vẽ/3D; kết quả trên ảnh thật không có ý nghĩa". Lynxx không dùng nhãn này để suy đoán giới tính của người thật.

### 4.4 Suy luận on-device

- Không gửi ảnh, embedding, nhãn hay tag ra ngoài. Không có telemetry.
- Chỉ có hai loại request mạng:
  - tới site booru và CDN của nó (giống preview hiện nay);
  - tới catalog model, **chỉ khi người dùng bấm** Cài hoặc Kiểm tra cập nhật, và chỉ để tải về.
- Test: chạy preview ở chế độ model qua một proxy có ghi log, rồi assert tập host gọi tới.

### 4.5 Giấy phép

Mọi giấy phép phải xác minh lại tại nguồn trước khi đóng gói.

| Thành phần | Giấy phép | Ghi chú |
|---|---|---|
| WD tagger v3 | Apache-2.0 (theo model card) | Kèm LICENSE/NOTICE. Dữ liệu huấn luyện của nó (Danbooru) nằm ngoài tầm kiểm soát của ta; ghi rõ trong model card của Lynxx. |
| SigLIP | Apache-2.0 | Dữ liệu WebLI không công khai |
| CLIP (OpenAI) | MIT | — |
| Checkpoint OpenCLIP/LAION | — | **Không dùng** checkpoint huấn luyện trên LAION-5B bản gốc (đã bị rút năm 2023 sau khi phát hiện CSAM). Nếu cần thì chỉ dùng bản Re-LAION. |
| onnxruntime | MIT | Wheel CPU ~15–20 MB |
| numpy / Pillow / PyAV | BSD-3 / MIT-CMU / BSD-3 | Wheel PyAV kèm FFmpeg (LGPL/GPL), nên ưu tiên `ffmpeg` có sẵn trên hệ thống |
| Ảnh huấn luyện | Bản quyền thuộc tác giả; tuân ToS từng site | Chỉ dùng nội bộ để train. **Không** phân phối ảnh, dataset hay embedding; chỉ phân phối trọng số. Phân phối trọng số train trên tranh có bản quyền là vùng xám pháp lý; cần ghi trong câu hỏi mở. |

---

## 5. Tích hợp sản phẩm

### 5.1 UI (Electron)

- **Bộ lọc** (`#filters` trong `index.html`), thêm field "Giới tính nhân vật":
  - Nhóm chip chọn nhiều: **Chỉ nữ**, **Chỉ nam**, **Cả hai**, **Không rõ / không có**. Không chọn chip nào nghĩa là tắt lọc.
  - Select "Cách xác định": *Chỉ tag* hoặc *Tag + model*. Nếu chưa cài model, lựa chọn thứ hai bị vô hiệu và có link "Cài model (~100 MB)".
  - Checkbox "Nghiêm ngặt (loại futa/trap/mơ hồ)".
  - Lưu vào `settings.download.gender`. `downloadArgs()` và `searchArgs()` trong `main.js` sinh ra `-gender…`.
- **Badge trên card preview** (`renderGrid` → `.badges`):
  - Nội dung: `Nữ` / `Nam` / `Nam+Nữ` / `Không có` / `?`.
  - Kiểu: nền đặc = high, viền = medium, viền gạch = low. Thêm hậu tố `·AI` khi nhãn từ model, và spinner nhỏ khi đang chờ `gender.result`.
  - Tooltip: bằng chứng (ví dụ "1girl, solo → chỉ nữ · cao") hoặc "pF 0,97 · pM 0,03".
  - Post bị loại vì giới tính dùng lớp `.excluded` có sẵn. Badge và trạng thái loại được cập nhật trực tiếp khi model trả kết quả.
- **`#results-info`**: "… · 14 loại theo giới tính · 5 không rõ · 2 bị chặn (an toàn)". Post bị chặn chỉ hiện dưới dạng số đếm.
- **Lightbox**: hiện pF, pM và các cờ.
- **Settings → "Model giới tính"**:
  - trạng thái, phiên bản, dung lượng, backend;
  - Cài / Gỡ / Kiểm tra cập nhật;
  - "Dùng ảnh sample khi thumbnail quá nhỏ";
  - "Xoá cache kết quả".
- **Trang Tải xuống**: card job hiện "đã lọc theo giới tính: n".
- `mock.js` mô phỏng `gender.*` để phát triển UI mà không cần sidecar.

### 5.2 Chế độ "chỉ tag" vs "tag + model"

| | Chỉ tag (mặc định) | Tag + model |
|---|---|---|
| Cài thêm | Không | Gói model ≤ 120 MB + `onnxruntime` |
| Tải thêm ảnh | Không | Thumbnail (hoặc sample) cho post `unknown`; ưu tiên lấy từ cache |
| Độ phủ | Theo site (cao ở db/en/rx, thấp ở rp/rn) | Cao hơn, nhất là ở rp/rn và post thiếu tag |
| Độ trễ | ≈ 0 | Thêm thời gian cho mỗi post `unknown` (§5.3) |
| Áp dụng | Mọi module; có `GENDER_TAGS` thì tốt nhất | Module có `ModuleCaps.PREVIEW` |

### 5.3 Hiệu năng & kích thước mục tiêu

Máy tham chiếu: CPU 4 nhân AVX2, ví dụ i5-8250U hoặc Ryzen 5 3500U.

| Chỉ số | Mục tiêu |
|---|---|
| Tầng 0 | ≤ 20 µs/post; 10.000 post < 0,3 s; không thêm request mạng |
| Tầng 1, backbone 224 | p50 ≤ 40 ms, p95 ≤ 80 ms mỗi ảnh (int8, batch 16, ảnh đã cache) |
| Tầng 1, backbone 448 (WD) | p50 ≤ 150 ms mỗi ảnh. Chấp nhận được vì model chỉ chạy cho post `unknown`. |
| Một trang preview | ≤ 100 post, ~20 % `unknown`: đủ badge model trong ≤ 3 s sau khi lưới hiện (ảnh đã cache) |
| Khởi động lạnh | Nạp model ≤ 1,5 s |
| Bộ nhớ | RAM thêm ≤ 400 MB; giải phóng sau 5 phút rảnh |
| Gói model | ≤ 120 MB (int8), trần 150 MB. Không đóng vào bộ cài. |

### 5.4 Phân phối & cập nhật model

- **Thư mục**: `data_dir()/models/gender/<version>/`, gồm `model.onnx`, `manifest.json` và `LICENSES/`.
  - Manifest có: `schema`, `version`, `labelSchema`, `backbone`, `inputSize`, `mean`/`std`, `outputs`, `thresholds[site]`, `temperature[site]`, `sha256`, `size`, `minAppVersion`.
- **Catalog**: một file JSON ở `settings.advanced.modelCatalogUrl`, mặc định cùng host với catalog extension.
  - Chỉ kiểm tra khi người dùng bấm, hoặc tối đa 1 lần/tuần nếu người dùng bật tự kiểm tra.
  - Tải về thư mục tạm, kiểm SHA-256, rồi đổi tên nguyên tử. Giữ phiên bản trước để rollback.
  - Kiểm SHA-256 và chống zip-slip giống `ext.install` (AC-19).
- **Phiên bản**:
  - Đổi `labelSchema` là thay đổi major: cache bị vô hiệu và quy tắc kết hợp phải kiểm lại.
  - Đổi ngưỡng hoặc calibration là thay đổi minor.

---

## 6. Lộ trình, ước lượng, rủi ro

### 6.1 Các giai đoạn

| Giai đoạn | Nội dung | Sản phẩm | Công (ngày) | Phụ thuộc |
|---|---|---|---|---|
| **P0** Luật tag | `gender.py`, ruleset db/rx/en/generic, `safety.py`, hai filter trong `_process_tags`, CLI, RPC `search.preview`, IPC, UI chip + badge (nguồn tag), unit test dạng bảng, `tools/gender_coverage.py` và báo cáo độ phủ | Bộ lọc dùng được ngay ở chế độ "chỉ tag" | 8–10 | — |
| **P1** Dataset | Công cụ dựng dataset (metadata qua downloader, loại trừ §4, embedding, xoá ảnh), split theo artist, công cụ gán nhãn tập vàng, 3.000 nhãn × 2 người | `meta.parquet`, embedding, tập vàng, báo cáo precision Tầng 0 | 10–12 (gồm ~4 ngày gán nhãn mỗi người) | `safety.py` từ P0 |
| **P2** Train / eval | Baseline A0, head A1 và B (C tuỳ chọn), calibration, ngưỡng theo site, báo cáo slice, export ONNX int8, memo chọn phương án | Gói model v1 kèm model card | 8–10 | P1 |
| **P3** Tích hợp | `gender_model.py`, `-gender_mode model`, `gender.*` RPC + streaming, cache SQLite, quản lý và cập nhật model, Settings, đo hiệu năng trên Linux/Windows/macOS, test mạng (§4.4) | Chế độ "Tag + model" | 10–12 | P2 |
| **P4** Video | Badge thumbnail video, `-gender_verify_media`, lấy mẫu khung, 300 clip gán tay | Kiểm tra sau tải | 5–7 | P3 |
| | | **Tổng** | **41–51 ngày công** (~9–10 tuần cho một người) | |

P0 phát hành được độc lập. P1 bắt đầu được ngay khi `safety.py` đã merge.

### 6.2 Rủi ro

| Rủi ro | Khả năng | Ảnh hưởng | Giảm thiểu |
|---|---|---|---|
| Tag rx gắn lỏng: post `only_female` thực ra có POV nam không được tag | Cao | Lọt sai | Trên rx, `only_*` không có marker exclusive chỉ đạt medium; `male_pov`/`penis` tính là M; đo trên tập vàng |
| Nội dung trẻ vị thành niên không có tag lọt qua | Thấp–vừa | Nghiêm trọng | Blacklist rộng kèm regex; safety gate; model không chạy trên post bị chặn; ghi rõ rủi ro còn lại |
| Thumbnail quá nhỏ (rx ~150 px) làm model kém | Vừa | Độ phủ thấp | Train với góc nhìn thumbnail; tuỳ chọn dùng sample; ngưỡng theo site |
| Request thumbnail bổ sung bị rate limit hoặc dính Cloudflare | Vừa | Preview chậm, `cfChallenge` | Ưu tiên cache Electron; chỉ tải cho post `unknown`; giới hạn luồng; tôn trọng `cfChallenge` hiện có |
| Lệch miền (3D, furry, western) | Vừa | Precision slice thấp | Slice riêng; ngưỡng theo site; phương án D nếu cần |
| Giấy phép backbone hoặc dữ liệu thay đổi | Thấp | Phải thay backbone | Head tách rời; có phương án B và C; manifest ghi rõ nguồn |
| Model làm bộ cài hoặc RAM nặng | Vừa | Trải nghiệm kém trên máy yếu | Tải tuỳ chọn; mặc định "chỉ tag"; giải phóng khi rảnh |
| Nhãn gây tranh cãi (futa, trap) | Vừa | Người dùng không hài lòng | Quy ước rõ ở §1; tooltip giải thích; `-gender_strict` |

---

## 7. Acceptance criteria

Định dạng giống [acceptance-criteria.md](../acceptance-criteria.md). Ký hiệu: `T:` test tự động, `E2E:` tự kiểm Electron, `M:` thủ công.

| AC | Giai đoạn | Tiêu chí | Kiểm chứng |
|---|---|---|---|
| AC-G01 | P0 | **Given** post db có tag `1girl 1boy hetero` **When** chạy `infer_from_tags` **Then** nhãn là `both`, độ tin cậy `high`, evidence chứa `1girl` và `1boy` | T: `GenderRulesTests` (bảng ≥ 40 ca/site) |
| AC-G02 | P0 | **Given** post rx `1girls female_only` **Then** `only_female`/`high`. **Given** rx `1girls solo` (không có marker) **Then** `only_female`/`medium`. **Given** e621 `male solo anthro` **Then** `only_male`/`high` kèm cờ `anthro` | T |
| AC-G03 | P0 | **Given** `-gender only_female` và post `1girl futanari solo` **Then** post được giữ (cờ `futa`). **When** thêm `-gender_strict` **Then** post bị loại. **Given** post `trap 1boy` **Then** nhãn là `only_male` | T |
| AC-G04 | P0 | **Given** `-gender only_female` **When** tải một truy vấn db **Then** mọi post bị loại có trong `filtered_out_ids_cache`; trace in số lượng theo nhãn; task parent không bị lọc giới tính | T: `ItemFilter` mở rộng với downloader giả |
| AC-G05 | P0 | **Given** không có `-gender` **When** chạy bất kỳ test hồi quy nào **Then** hành vi và kết quả giống hệt trước khi có tính năng; AC-20 vẫn pass | T |
| AC-G06 | P0 | **Given** Preview với bộ lọc "Chỉ nam" **Then** card có badge nhãn (nguồn tag); post bị loại bị ẩn trừ khi bật "Hiện post bị lọc"; `#results-info` hiện số bị lọc theo giới tính | E2E; M |
| AC-G07 | P0 | **Given** `tools/gender_coverage.py` chạy 2.000 post/site **Then** độ phủ (≥ medium) là db ≥ 90 %, en ≥ 85 %, rx ≥ 75 %. **And** trên tập vàng (sau P1): precision Tầng 0 ≥ 0,97 ở mức high và ≥ 0,92 ở mức medium | M + báo cáo JSON |
| AC-S01 | P0 | **Given** post có tag bất kỳ trong bảng §4.1 (kể cả qua alias hoặc regex, ví dụ `younger_female`, `Young`) **When** bật `-gender` ở mọi chế độ, kể cả task parent **Then** post bị loại với lý do `safety` | T: `SafetyTests` (bảng toàn bộ tag) |
| AC-S02 | P0 | **Given** `search.preview` có `gender` và trang có post bị chặn **Then** post đó không có trong `items`, response không chứa URL thumbnail của nó, chỉ tăng `blockedCount`; bật "Hiện post bị lọc" vẫn không hiện | T (server); E2E |
| AC-S03 | P0 | **Given** extension khai báo `safetyTags` **Then** tag của nó được thêm vào blacklist. **Given** settings, CLI hoặc manifest cố gắng gỡ một tag **Then** không có tác dụng (không tồn tại API gỡ) | T |
| AC-S04 | P1 | **Given** công cụ dựng dataset **When** chạy trên mẫu **Then** log đếm số post bị loại ở từng bước §3.2; `meta.parquet` có 0 post chứa tag bị chặn; thư mục ảnh tạm rỗng sau khi chạy xong | T + M |
| AC-G08 | P1 | **Given** tập vàng **Then** có ≥ 3.000 post, ≥ 30 % thuộc slice thiếu tag, κ ≥ 0,8 giữa hai người gán | M |
| AC-G09 | P2 | **Given** model ứng viên trên tập vàng (slice thiếu tag) **Then** precision ≥ 0,92 cho mỗi lớp `only_female`/`only_male`/`both` ở điểm vận hành, độ phủ ≥ 70 %, macro-F1 4 lớp ≥ 0,85, ECE ≤ 0,05 sau calibration | Báo cáo eval |
| AC-G10 | P2 | **Given** báo cáo slice **Then** không có slice site/phong cách nào (n ≥ 150) có precision thấp hơn toàn cục > 5 điểm mà chưa được xử lý (nâng ngưỡng hoặc ghi giới hạn); slice trap/femboy không có post nào bị gán `only_female` | Báo cáo eval |
| AC-G11 | P2 | **Given** model int8 **Then** macro-F1 giảm ≤ 1 điểm so với fp32; gói model ≤ 120 MB | Báo cáo eval |
| AC-G12 | P3 | **Given** máy tham chiếu và một trang preview 100 post đã cache **When** chọn "Tag + model" **Then** mọi badge `·AI` xuất hiện trong ≤ 3 s; p95 mỗi ảnh đạt mục tiêu §5.3; RAM thêm ≤ 400 MB | M (đo trên 3 OS) |
| AC-G13 | P3 | **Given** preview ở chế độ model chạy qua proxy ghi log **Then** chỉ có request tới host/CDN của site đang truy vấn; không có request nào khác ngoài lúc người dùng bấm Cài/Cập nhật | T/E2E (proxy giả) |
| AC-G14 | P3 | **Given** gói model tải về có SHA-256 sai **When** cài **Then** cài đặt thất bại, phiên bản cũ vẫn chạy. **Given** gỡ model **Then** chế độ "Tag + model" bị vô hiệu và CLI `-gender_mode model` báo lỗi kèm hướng dẫn | T |
| AC-G15 | P3 | **Given** post có cờ `real` hoặc bị chặn **Then** model không được gọi (bộ đếm `modelProcessed` không tăng) | T |
| AC-G16 | P4 | **Given** video chỉ có thumbnail **Then** độ tin cậy tối đa là `medium`. **Given** `-gender_verify_media` và một file không khớp **Then** file được chuyển vào `_gender_mismatch/`, không bị xoá | T + M |
| AC-G17 | P4 | **Given** 300 clip gán tay **Then** precision theo lớp ≥ 0,90 với độ phủ ≥ 60 % | Báo cáo eval |

---

## 8. Câu hỏi mở

1. ~~**Blacklist an toàn toàn cục**~~: **đã chốt 2026-11-16, chỉ khi lọc; đổi 2026-11-18 thành bộ lọc riêng.** `_filter_items_safety` chạy khi có `-safety_blacklist` (CLI, RPC, ô riêng trong app), cho mọi task kể cả parent, độc lập với `-gender`; mặc định tắt, tức là giữ hành vi như bản gốc.
2. **Đẩy luật tag lên server** cho site không giới hạn tag (rx): có làm ở P3+ như một tuỳ chọn để giảm số trang phải tải?
3. **Host catalog model** và quy trình ký gói (SHA-256 trong catalog có đủ, hay cần chữ ký)?
4. **Phân phối trọng số** train trên tranh có bản quyền: chỉ phát hành head và để người dùng tự tải backbone từ nguồn gốc, hay đóng gói cả hai?
5. **`-dump_gender`**: có ghi nhãn kèm `-dump_tags` (file riêng, không đổi định dạng `ItemInfo`) không?
6. Có cần thêm nhãn **`only_futa`** riêng (rx có `futa_only` 434k post) thay vì chỉ dùng cờ cộng strict không?
