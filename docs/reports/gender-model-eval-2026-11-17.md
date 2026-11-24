# Đánh giá model giới tính: silver trên Danbooru `rating:g` — 2026-11-17

> **Đây là đánh giá silver, không phải tập vàng.** Chưa có nhãn của người. Tham chiếu là nhãn tầng 0 ở mức `high` (tag mạnh của Danbooru), model (WD tagger v3, phương án A0) chỉ nhận ảnh, tag bị ẩn. Các con số đo **mức model đồng ý với tag mạnh**, không phải precision thật: tag sai thì model bị tính là sai, và ngược lại. Số đo theo plan §7 (AC-G07, AC-G09, AC-G10) cần tập vàng do hai người gán (mục "Việc còn lại").

Plan: [gender-filter-model.md](../plans/gender-filter-model.md) §3.2, §3.7. Công cụ: `lynxx/gender_eval.py`, cách chạy ở [operations.md](../operations.md) mục 3g. Số liệu đầy đủ: [gender-model-eval-2026-11-17.json](gender-model-eval-2026-11-17.json). Manifest (id, md5, URL, tag; không có ảnh): [tools/gender/manifest-db-rating-g-2026-11-17.json](../../tools/gender/manifest-db-rating-g-2026-11-17.json).

## Tóm tắt

Điểm vận hành: độ tin cậy ≥ medium (mặc định của app). n = 311 post có tham chiếu (5 post bị safety gate của model loại, 12 post không có tag mạnh).

| Đầu vào | Độ phủ | Khớp tham chiếu (gộp) | only_female | only_male | both | none | Macro-F1 | ECE | Mức high: phủ / khớp |
|---|---|---|---|---|---|---|---|---|---|
| Sample (~850 px) | 90,0 % | 0,954 | 0,921 (82/89) | 0,966 (57/59) | 0,957 (45/47) | 0,977 (83/85) | 0,894 | 0,055 | 74,0 % / 0,978 |
| Thumbnail (180 px) | 84,2 % | 0,954 | 0,929 (79/85) | 0,944 (51/54) | 1,000 (33/33) | 0,967 (87/90) | 0,848 | 0,068 | 70,4 % / 0,973 |

- **Sample tốt hơn thumbnail ở độ phủ, không ở độ khớp.** Hai đầu vào cùng khớp 0,954 trên các nhãn đưa ra, nhưng với thumbnail model bỏ trống nhiều hơn, nhất là lớp `both` (recall 0,69 → 0,51: nhân vật thứ hai nhỏ hoặc ở rìa ảnh). Với thumbnail, lỗi ở xác suất rất cao nhiều hơn: ở ngưỡng ≥ 0,98 rủi ro là 3,8 % so với 0 % với sample. Hai đầu vào cho cùng nhãn ở 85,2 % post.
- **Slice "thiếu tag"** (≤ 8 tag general; 75 post vẫn có tag giới mạnh nên có tham chiếu): khớp 0,985, độ phủ 90,7 % (sample). Post không có tag giới, miền model thực sự nhắm tới, gần như không có trong silver (4 post, không có tham chiếu).
- **Lớp `only_female` khớp thấp nhất** (0,921 với sample). Các post lệch (danh sách đầy đủ ở dưới), mô tả theo tag của post, chưa có người xem lại:
  - tham chiếu `none` nhưng model thấy nữ: `doll` / `character_doll`, `pokemon_(creature)`, `text_focus`;
  - tham chiếu `both` nhưng model thấy một giới: `chibi`, `out_of_frame`, nhóm đông người;
  - tham chiếu `only_male` nhưng model ra `none`: `robot` / `mecha` (post có cả `6+boys` lẫn `no_humans`);
  - một post `trap` + `crossdressing` bị model gán `only_female`. Trong sản phẩm, `combine()` không cho model đổi post có tag trap thành nữ, nhưng trap không được tag thì vẫn là rủi ro (plan §1.2, §3.7).
- **Safety gate của model** loại 5 / 328 post `rating:g` mà tag của chúng không bị danh sách chặn bắt (model đoán `aged_down` 2, `child` 2, `randoseru` 1). Các post này không có trong số đo nào và **đã bị xoá khỏi manifest** (§3.2 / §4: không giữ): manifest còn 323 post, danh sách `dropped` của manifest chỉ ghi khoá và tag của gate. Tag của chúng mà danh sách chặn chưa bắt (để xem lại `lynxx/safety.py`) nằm trong `posts.modelGateDropped` của file JSON. Ngưỡng gate 0,15 thấp có chủ ý, nên có thể có chặn nhầm.
- **Luật thiên lệch** (§3.7): chỉ 4 slice có n ≥ 150 (`site=db`, `style=2d_anime`, `density=full`, `media=image`), không slice nào bị cờ. Các slice còn lại có n từ 2 đến 75 (3D 5, furry 5, đơn sắc 34, trap 2, POV 5, đông người 7, thiếu tag 75, video 2), chưa đủ để kết luận; mẫu không có post futa.
- **Độ trễ** (RTX 4070 Ti SUPER, CUDA, qua HTTP tới model server trên máy này, không tính thời gian tải ảnh): sample trung bình 29,9 ms (p95 38,7 ms), thumbnail 18,8 ms. 654 request ảnh, cách nhau 1,2 s, không bị bot challenge.

## Thiết lập

- Lấy mẫu (`sample`, 7 truy vấn × 1 trang × 100 post, `random:` để không lệch về post mới): `random:100 rating:g`, `1girl random:100 rating:g`, `1boy random:100 rating:g`, `male_focus random:100 rating:g`, `hetero random:100 rating:g`, `no_humans random:100 rating:g`, `gentags:<=8 random:100 rating:g`. Quota: 60 post cho mỗi nhãn tầng 0 mức high, 80 "thiếu tag", 20 nhãn yếu (chỉ có 8). Seed 17.
- Loại theo §3.2: 700 post → 26 bị danh sách chặn (gồm cả false positive đã chấp nhận như `kid_icarus`, `eustass_kid`, `baby_animal`), 52 thuộc danh sách loại bổ sung (`school_uniform` 50), 4 ảnh thật, 0 trùng md5, 29 không có file → 589 → chọn 328 theo quota. Safety gate của model loại thêm 5 lúc đánh giá; `evaluate` xoá 5 post này khỏi manifest (còn 323; `strata` trong manifest vẫn ghi số lúc chọn mẫu). Số đo trong báo cáo không đổi vì 5 post này vốn đã bị loại khỏi mọi số đo.
- Model: `wd-vit-tagger-v3` (revision `7f6b584d`), model server `http://127.0.0.1:8765` chạy trên CUDA; tag đoán ra đi qua bộ luật `db` (`classify`), điểm vận hành `medium`.
- Lệnh:
  ```bash
  LYNXX_DATA_DIR=$(mktemp -d) .venv/bin/python -m lynxx.gender_eval sample --module db --pages 1 --query "random:100 rating:g" --query "1girl random:100 rating:g" --query "1boy random:100 rating:g" --query "male_focus random:100 rating:g" --query "hetero random:100 rating:g" --query "no_humans random:100 rating:g" --query "gentags:<=8 random:100 rating:g" --quota only_female=60,only_male=60,both=60,none=60,sparse=80,weak=20 --seed 17 --out eval/manifest.json
  .venv/bin/python -m lynxx.gender_eval evaluate --manifest eval/manifest.json --silver --inputs sample,thumb --model-url http://127.0.0.1:8765 --image-interval 1.2 --out-dir eval/report
  ```
  `random:` cho mỗi lần chạy một tập post khác; dùng lại manifest trong `tools/gender/` để có đúng các post của báo cáo này (trừ 5 post safety gate đã loại). `evaluate` ghi lại file manifest khi gate loại post.

## Giới hạn

1. **Silver không thay được tập vàng.** Tham chiếu là tag của Danbooru, không phải nhãn của người theo quy tắc §1. Không đo được precision của tầng 0 (tham chiếu chính là tầng 0) và không đo được tầng 0 + model.
2. **Có thể lạc quan.** WD tagger v3 được huấn luyện trên ảnh và tag của Danbooru; nhiều post trong mẫu có thể đã nằm trong dữ liệu huấn luyện của nó. Trên rule34, e621, Xbooru, ảnh 3D, furry và western, kết quả có thể thấp hơn.
3. **Mẫu phân tầng, chỉ `rating:g`.** Tỉ lệ các lớp không giống phân bố thật (60 / 60 / 60 / 60 cộng "thiếu tag"), nên precision gộp phụ thuộc cách chia. Nội dung `rating:g` hầu như không có POV hay bộ phận cơ thể, là những ca §1.2 khó nhất.
4. **Slice nhỏ.** Ngoài 4 slice lớn, mọi slice có n < 150 nên luật thiên lệch chưa áp dụng được.
5. **ECE không phải của xác suất đã calibrate.** Xác suất dùng là xác suất yếu nhất trong các tag bằng chứng (cách `classify` tính độ tin cậy). Model A0 chưa được calibrate (§3.4 cần tập calib).
6. **Chưa đo**: video nhiều khung (P4), site khác Danbooru, post không có tag giới.

## Việc còn lại: tập vàng (cần hai người gán)

AC-G08: ≥ 3.000 post (1.000 db, 1.000 rx, 1.000 en, cộng 300 rp / xb), ≥ 30 % thuộc slice thiếu tag, hai người gán độc lập theo §1, κ ≥ 0,8, trọng tài cho các bất đồng. Mỗi site một manifest, ví dụ với Danbooru (người dùng chọn rating và truy vấn; lặp lại một truy vấn `random:` để có thêm post):

```bash
.venv/bin/python -m lynxx.gender_eval sample --module db --query "random:100" --query "random:100" --query "gentags:<=8 random:100" --query "gentags:<=8 random:100" --quota only_female=250,only_male=200,both=200,none=100,sparse=300,weak=all --out gold/db.json
.venv/bin/python -m lynxx.gender_eval label --manifest gold/db.json --annotator nguoi1 --open
.venv/bin/python -m lynxx.gender_eval label --manifest gold/db.json --annotator nguoi2 --open --port 8791
.venv/bin/python -m lynxx.gender_eval agree gold/labels-nguoi1.json gold/labels-nguoi2.json --manifest gold/db.json --out gold/agree-db.json
.venv/bin/python -m lynxx.gender_eval label --manifest gold/db.json --annotator trongtai --only gold/agree-db.json
.venv/bin/python -m lynxx.gender_eval agree gold/labels-nguoi1.json gold/labels-nguoi2.json --arbiter gold/labels-trongtai.json --manifest gold/db.json --out gold/agree-db.json --gold gold/gold-db.json
.venv/bin/python -m lynxx.gender_eval evaluate --manifest gold/db.json --gold gold/gold-db.json --out-dir gold/report-db
```

- Trang gán nhãn cần model server đang chạy (safety gate trước khi người xem ảnh); hai người gán chạy hai cổng khác nhau hoặc lần lượt. Người gán không thấy tag hay nhãn tầng 0.
- Post bị đánh dấu "không an toàn" bị xoá khỏi manifest; `agree` liệt kê các tag của chúng mà danh sách chặn chưa bắt (`unsafeTagsNotBlocked`) để bổ sung vào `lynxx/safety.py` (§4.1).
- Với tập vàng, `evaluate` đo cả tầng 0, model và tầng 0 + model, nên trả lời được AC-G07 (precision tầng 0), AC-G09 và AC-G10.

---

# Báo cáo sinh tự động (`report.md`)

## Đánh giá model giới tính (silver) — 2026-11-17

> **Silver, không phải tập vàng.** Tham chiếu là nhãn tầng 0 ở mức `high` (tag mạnh), model chỉ nhận ảnh (tag bị ẩn). Số đo cho biết model đồng ý với tag mạnh tới đâu, **không** phải precision thật: tag sai thì model "sai" theo, và post thiếu tag (miền model nhắm tới) không có tham chiếu. Chưa có nhãn của người.

- Model: `wd-vit-tagger-v3`, thiết bị `cuda`, server `http://127.0.0.1:8765`; điểm vận hành: độ tin cậy ≥ `medium`.
- Manifest: 328 post, tạo lúc 2026-11-17T14:56:07; truy vấn: `random:100 rating:g`, `1girl random:100 rating:g`, `1boy random:100 rating:g`, `male_focus random:100 rating:g`, `hetero random:100 rating:g`, `no_humans random:100 rating:g`, `gentags:<=8 random:100 rating:g`.
- Post: 328 ứng viên, 328 đã xử lý, 323 được đánh giá, 311 có tham chiếu. Loại khi đánh giá: {"modelGate": 5}; không lấy được ảnh: {}.
- Safety gate của model loại 5 post (tag: `aged_down` 2, `randoseru` 1, `child` 2); các post này không có trong mọi số đo.
- 5 post đó đã bị xoá khỏi manifest (ghi trong `dropped` của manifest, chỉ kèm tag của gate, không có URL hay tag của post): db:9019708 (`aged_down`), db:10453505 (`aged_down`), db:7804235 (`randoseru`), db:8263106 (`child`), db:201251 (`child`).

### Loại trừ khi lấy mẫu (§3.2)

| Bước | Lý do | Bị loại | Còn lại | Tag (số post) |
|---|---|---|---|---|
| 1 | §4.1 danh sách chặn cứng (tag và alias) | 26 | 674 | `aged_down` 8, `child` 4, `baby_animal` 3, `father_and_child` 1, `seishou_elementary_school_uniform` 1, `tacho_baby` 1, `mother_and_child` 1, `von_lycaon_(young)` 1 |
| 2 | §4.2 danh sách loại bổ sung | 52 | 622 | `school_uniform` 50, `petite` 1, `flat_chest` 1 |
| 3 | §4.3 ảnh / người thật | 4 | 618 | `photo_(medium)` 3, `cosplay_photo` 1 |
| 4 | §4.2 safety gate của model (cần ảnh: áp ở label / evaluate) | 5 (lúc đánh giá) | 618 |  |
| 5 | trùng md5 | 0 | 618 |  |
| 6 | đã xoá / không có file | 29 | 589 |  |

Phân tầng: both 60/129; none 60/89; only_female 60/121; only_male 60/151; sparse 80/91; weak 8/8. Tỉ lệ "thiếu tag" trong manifest: 24,4 %.

### Model (chỉ ảnh) — ảnh sample (~850 px)

| n | Độ phủ | Precision (gộp) | only_female | only_male | both | none | Macro-F1 | Macro-F1 (khi trả lời) | ECE |
|---|---|---|---|---|---|---|---|---|---|
| 311 | 90,0 % | 0,954 | 0,921 (82/89) | 0,966 (57/59) | 0,957 (45/47) | 0,977 (83/85) | 0,894 | 0,933 (độ phủ 99,7 %) | 0,055 (n 310) |

Ma trận nhầm lẫn (hàng: tham chiếu, cột: dự đoán ở điểm vận hành):

| | only_female | only_male | both | none | unknown |
|---|---|---|---|---|---|
| **only_female** (90) | 82 | 1 | 1 | 1 | 5 |
| **only_male** (68) | 2 | 57 | 1 | 1 | 7 |
| **both** (65) | 2 | 1 | 45 | 0 | 17 |
| **none** (88) | 3 | 0 | 0 | 83 | 2 |

Risk–coverage (ngưỡng trên xác suất mà nhãn dựa vào): ≥ 0,35: phủ 99,7 %, rủi ro 6,5 %; ≥ 0,50: phủ 93,9 %, rủi ro 4,8 %; ≥ 0,60: phủ 90,0 %, rủi ro 4,6 %; ≥ 0,70: phủ 86,2 %, rủi ro 3,7 %; ≥ 0,80: phủ 80,7 %, rủi ro 3,6 %; ≥ 0,85: phủ 74,3 %, rủi ro 2,2 %; ≥ 0,90: phủ 66,2 %, rủi ro 1,5 %; ≥ 0,95: phủ 52,7 %, rủi ro 1,2 %; ≥ 0,98: phủ 26,7 %, rủi ro 0,0 %.

Theo độ tin cậy: ≥ high: phủ 74,0 %, precision 0,978; ≥ medium: phủ 90,0 %, precision 0,954; ≥ low: phủ 99,7 %, precision 0,935.

| Slice | n | Độ phủ | Precision | only_female | only_male | both | none |
|---|---|---|---|---|---|---|---|
| site=db | 311 | 90,0 % | 0,954 | 0,921 (89) | 0,966 (59) | 0,957 (47) | 0,977 (85) |
| style=2d_anime | 301 | 90,7 % | 0,952 | 0,920 (88) | 0,965 (57) | 0,957 (47) | 0,975 (81) |
| style=monochrome | 34 | 79,4 % | 0,926 | 0,800 (10) | 1,000 (4) | 1,000 (6) | 1,000 (7) |
| style=3d | 5 | 80,0 % | 1,000 | – (0) | – (0) | – (0) | 1,000 (4) |
| style=furry | 5 | 60,0 % | 1,000 | 1,000 (1) | 1,000 (2) | – (0) | – (0) |
| edge=crowd6 | 7 | 57,1 % | 0,750 | 0,500 (2) | 1,000 (2) | – (0) | – (0) |
| edge=dark_skin | 5 | 100,0 % | 1,000 | 1,000 (1) | 1,000 (2) | 1,000 (1) | 1,000 (1) |
| edge=pov_bodypart | 5 | 80,0 % | 0,750 | 0,000 (1) | 1,000 (2) | – (0) | 1,000 (1) |
| edge=trap | 2 | 100,0 % | 0,500 | 0,000 (1) | 1,000 (1) | – (0) | – (0) |
| density=full | 236 | 89,8 % | 0,943 | 0,903 (62) | 0,961 (51) | 0,951 (41) | 0,966 (58) |
| density=sparse | 75 | 90,7 % | 0,985 | 0,963 (27) | 1,000 (8) | 1,000 (6) | 1,000 (27) |
| media=image | 309 | 90,0 % | 0,953 | 0,921 (89) | 0,966 (59) | 0,957 (46) | 0,976 (84) |
| media=video | 2 | 100,0 % | 1,000 | – (0) | – (0) | 1,000 (1) | 1,000 (1) |

Cờ thiên lệch: không có.
Slice có n < 150 (chưa áp luật thiên lệch): style=monochrome, style=3d, style=furry, edge=crowd6, edge=dark_skin, edge=pov_bodypart, edge=trap, density=sparse, media=video.
Slice trap: 2 post, 1 bị gán only_female (db:10277933).
Post có nhãn khác tham chiếu (13, tham chiếu → dự đoán): db:11488194 (both → only_female), db:9475092 (both → only_female), db:12374115 (both → only_male), db:10559953 (none → only_female), db:10596362 (none → only_female), db:6171202 (none → only_female), db:9174511 (only_female → both), db:7886469 (only_female → none), db:10726951 (only_female → only_male), db:8273114 (only_male → both), db:10806148 (only_male → none), db:10277933 (only_male → only_female), db:12534156 (only_male → only_female).

### Model (chỉ ảnh) — thumbnail (150–180 px)

| n | Độ phủ | Precision (gộp) | only_female | only_male | both | none | Macro-F1 | Macro-F1 (khi trả lời) | ECE |
|---|---|---|---|---|---|---|---|---|---|
| 311 | 84,2 % | 0,954 | 0,929 (79/85) | 0,944 (51/54) | 1,000 (33/33) | 0,967 (87/90) | 0,848 | 0,914 (độ phủ 99,7 %) | 0,068 (n 310) |

Ma trận nhầm lẫn (hàng: tham chiếu, cột: dự đoán ở điểm vận hành):

| | only_female | only_male | both | none | unknown |
|---|---|---|---|---|---|
| **only_female** (90) | 79 | 0 | 0 | 1 | 10 |
| **only_male** (68) | 3 | 51 | 0 | 2 | 12 |
| **both** (65) | 2 | 3 | 33 | 0 | 27 |
| **none** (88) | 1 | 0 | 0 | 87 | 0 |

Risk–coverage (ngưỡng trên xác suất mà nhãn dựa vào): ≥ 0,35: phủ 99,7 %, rủi ro 8,1 %; ≥ 0,50: phủ 90,7 %, rủi ro 6,0 %; ≥ 0,60: phủ 84,2 %, rủi ro 4,6 %; ≥ 0,70: phủ 81,0 %, rủi ro 4,0 %; ≥ 0,80: phủ 75,2 %, rủi ro 3,4 %; ≥ 0,85: phủ 70,7 %, rủi ro 2,7 %; ≥ 0,90: phủ 61,7 %, rủi ro 2,6 %; ≥ 0,95: phủ 46,0 %, rủi ro 2,8 %; ≥ 0,98: phủ 25,4 %, rủi ro 3,8 %.

Theo độ tin cậy: ≥ high: phủ 70,4 %, precision 0,973; ≥ medium: phủ 84,2 %, precision 0,954; ≥ low: phủ 99,7 %, precision 0,919.

| Slice | n | Độ phủ | Precision | only_female | only_male | both | none |
|---|---|---|---|---|---|---|---|
| site=db | 311 | 84,2 % | 0,954 | 0,929 (85) | 0,944 (54) | 1,000 (33) | 0,967 (90) |
| style=2d_anime | 301 | 84,7 % | 0,953 | 0,929 (84) | 0,942 (52) | 1,000 (33) | 0,965 (86) |
| style=monochrome | 34 | 70,6 % | 0,917 | 0,800 (10) | 1,000 (3) | 1,000 (4) | 1,000 (7) |
| style=3d | 5 | 100,0 % | 1,000 | – (0) | 1,000 (1) | – (0) | 1,000 (4) |
| style=furry | 5 | 40,0 % | 1,000 | 1,000 (1) | 1,000 (1) | – (0) | – (0) |
| edge=crowd6 | 7 | 42,9 % | 0,333 | 0,500 (2) | – (0) | – (0) | 0,000 (1) |
| edge=dark_skin | 5 | 80,0 % | 1,000 | 1,000 (1) | 1,000 (1) | 1,000 (1) | 1,000 (1) |
| edge=pov_bodypart | 5 | 80,0 % | 0,750 | 0,000 (1) | 1,000 (2) | – (0) | 1,000 (1) |
| edge=trap | 2 | 50,0 % | 0,000 | 0,000 (1) | – (0) | – (0) | – (0) |
| density=full | 236 | 83,0 % | 0,944 | 0,915 (59) | 0,938 (48) | 1,000 (27) | 0,952 (62) |
| density=sparse | 75 | 88,0 % | 0,985 | 0,962 (26) | 1,000 (6) | 1,000 (6) | 1,000 (28) |
| media=image | 309 | 84,1 % | 0,954 | 0,929 (85) | 0,944 (54) | 1,000 (32) | 0,966 (89) |
| media=video | 2 | 100,0 % | 1,000 | – (0) | – (0) | 1,000 (1) | 1,000 (1) |

Cờ thiên lệch: không có.
Slice có n < 150 (chưa áp luật thiên lệch): style=monochrome, style=3d, style=furry, edge=crowd6, edge=dark_skin, edge=pov_bodypart, edge=trap, density=sparse, media=video.
Slice trap: 2 post, 1 bị gán only_female (db:10277933).
Post có nhãn khác tham chiếu (12, tham chiếu → dự đoán): db:11488194 (both → only_female), db:9475092 (both → only_female), db:6770421 (both → only_male), db:8687937 (both → only_male), db:8864506 (both → only_male), db:6171202 (none → only_female), db:7886469 (only_female → none), db:10806148 (only_male → none), db:4271089 (only_male → none), db:10277933 (only_male → only_female), db:1359066 (only_male → only_female), db:7979824 (only_male → only_female).

### Thumbnail và sample

Model cho cùng nhãn (ở điểm vận hành) với hai đầu vào ở 265/311 post (85,2 %).

### Post không có tham chiếu (silver)

Tag không đủ mạnh (không có nhãn tầng 0 ở mức high): không đo được độ đúng, chỉ biết model gán nhãn bao nhiêu post.

| Đầu vào | Mật độ tag | n | only_female | only_male | both | none | unknown |
|---|---|---|---|---|---|---|---|
| ảnh sample (~850 px) | sparse | 4 | 1 | 0 | 0 | 1 | 2 |
| ảnh sample (~850 px) | full | 8 | 2 | 5 | 0 | 0 | 1 |
| thumbnail (150–180 px) | sparse | 4 | 1 | 0 | 0 | 2 | 1 |
| thumbnail (150–180 px) | full | 8 | 2 | 4 | 0 | 0 | 2 |

### Cách đọc

- Độ phủ: tỉ lệ post có nhãn khác `unknown` ở điểm vận hành. Precision (gộp): tỉ lệ nhãn đúng trong các nhãn đã đưa ra. Precision theo lớp kèm (đúng/số dự đoán).
- Macro-F1: trung bình F1 của 4 lớp, `unknown` tính là bỏ sót. Macro-F1 (khi trả lời): chỉ trên post mà hệ thống đưa ra nhãn ở bất kỳ độ tin cậy nào.
- ECE (15 bin) và risk–coverage dùng xác suất mà nhãn dựa vào: xác suất yếu nhất trong các tag bằng chứng của model (cách `classify` tính độ tin cậy). Model A0 chưa được calibrate.
- Slice: một post có thể thuộc nhiều slice (phong cách, ca biên). "thiếu tag" (`density=sparse`) = ≤ 8 tag general hoặc không có tag giới.
