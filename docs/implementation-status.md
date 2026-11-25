# Lynxx — Implementation status

## Đã hoàn tất

- Repository độc lập và remote SSH: `git@github.com:Leiz-Workspace/Lynxx.git`.
- Nhánh `develop`, `staging`, `production` được push và đồng bộ.
- Electron shell, preload boundary, Python sidecar và settings/secrets kế thừa đã đổi namespace Lynxx.
- Bộ icon Lynxx cyan/monochrome từ `Lynxx_Icon_Pack_8_Files_1024` đã tích hợp.
- Domain model `Collection → Chapter → Page → Artifact`.
- SQLite library metadata, WAL, migration version và reader progress.
- Import PDF/EPUB qua native dialog → sidecar validation → managed library copy.
- PDF artifact assembly từ JPEG pages với atomic publish.
- EPUB artifact assembly, validation chống traversal/symlink và page extraction sandboxed.
- Reader PDF sandboxed và EPUB image reader với hai mode: cuộn dọc/trượt ngang.
- Policy gate fail-closed, source registry, policy decision RPC.
- Adapter contract collection-centric.
- Model uncen local-only contract: consent, provenance, original immutable, network disabled.
- Test foundation, library import, EPUB reader, policy, source adapter và Electron checks.

## Cố ý không bật

### nhentai.net

Adapter chỉ ở trạng thái `probe-only`/`expired_review`. Chưa bật request live vì hồ sơ ToS, robots, quyền lưu và quyền phân phối chưa được phê duyệt trong Lynxx.

### cuutruyen.net

Adapter ở trạng thái `blocked`. Không gọi image host bị robots từ chối, không giải mã `drm_data`, không unscramble để vượt access control và không bypass CAPTCHA/anti-bot.

### Model uncen

Lynxx chưa chứa model inference để khôi phục vùng che trong nội dung tình dục. Hiện chỉ phát hành safety contract local-only, không upload dữ liệu, không ghi đè bản gốc và yêu cầu xác nhận quyền. Đây là giới hạn cố ý về an toàn/pháp lý, không phải lỗi chưa wiring.

## Điều kiện để mở khóa source

Chỉ chuyển source sang `permitted` sau khi có hồ sơ versioned gồm ToS, robots snapshot, endpoint/capability allowlist, rate limit, quyền lưu cục bộ, quyền tạo PDF/EPUB và quy trình takedown. Nếu không đạt, trạng thái `blocked`/`expired_review` là trạng thái phát hành đúng.
