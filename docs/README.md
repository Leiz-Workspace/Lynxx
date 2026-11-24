# Tài liệu Lynxx

Giới thiệu, tải về và cách build ngắn gọn (tiếng Anh): [README của repo](../README.md). Các tài liệu dưới đây là bản đầy đủ, viết bằng tiếng Việt, trừ hai tài liệu kế thừa từ bản gốc (tiếng Anh).

## Sản phẩm
| Tài liệu | Nội dung |
|---|---|
| [specs.md](specs.md) | Yêu cầu chức năng (F-01…F-21) và phi chức năng, CLI, giao thức sidecar & job, API model server, manifest extension, định dạng tag list, settings, thư mục dữ liệu |
| [components.md](components.md) | Kiến trúc, thành phần Python / Electron, hook & capability, extension, model server, công cụ build và release |
| [user-stories.md](user-stories.md) | User stories (US-01…US-22) |
| [use-cases.md](use-cases.md) | Use cases (UC-01…UC-11) |
| [acceptance-criteria.md](acceptance-criteria.md) | Acceptance criteria (AC-01…AC-20) và cách kiểm chứng |
| [tag-syntax.md](tag-syntax.md) | Cú pháp truy vấn tag của lõi: wildcard, nhóm OR / AND, meta tag, tag đặc biệt theo site (tiếng Anh, từ README gốc) |
| [legacy-gui.md](legacy-gui.md) | Giao diện Tk cũ, dùng từ dòng lệnh, build file exe kiểu cũ (tiếng Anh, từ README gốc) |

## Vận hành và phát hành
| Tài liệu | Nội dung |
|---|---|
| [operations.md](operations.md) | Cài đặt, chạy app, cronjob cập nhật tag, index, model server (máy này, chạy nền, Docker, GPU, bản chạy độc lập), catalog có chữ ký, danh sách chặn tên miền, eval, đánh giá model, test, build, release, nhánh git |
| [releases/](releases/) | Ghi chú phát hành theo phiên bản (`v<version>.md`, workflow release đọc để tạo trang release) |
| [dev_log.md](dev_log.md) | Nhật ký phát triển, mục 8 là việc tiếp theo |
| [schemas/extension-manifest.schema.json](schemas/extension-manifest.schema.json) | JSON Schema cho `manifest.json` của extension |

## Plan và báo cáo
| Tài liệu | Nội dung |
|---|---|
| [plans/search-engine.md](plans/search-engine.md) | Plan cải thiện search engine (giai đoạn 1–2 đã làm, index các lần tải) |
| [plans/site-extension-skill-and-ai-generator.md](plans/site-extension-skill-and-ai-generator.md) | Plan skill và AI tạo extension cho site mới |
| [plans/gender-filter-model.md](plans/gender-filter-model.md) | Plan lọc giới tính: tầng tag, model nhận diện ảnh, video, đánh giá |
| [reports/audit-2026-11-16.md](reports/audit-2026-11-16.md) | Rà soát toàn bộ và các đợt sửa A–D |
| [reports/gender-model-eval-2026-11-17.md](reports/gender-model-eval-2026-11-17.md) | Đánh giá silver của model giới tính trên Danbooru `rating:g` |
| `reports/gender-coverage-*.json` | Độ phủ của lọc giới tính theo tag (db, rx) |

Ảnh chụp giao diện trong README nằm ở [images/](images/) (chụp từ `app/renderer/mock.js`, thumbnail là SVG trung tính).
