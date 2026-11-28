# Lynxx — Vận hành, build và phát hành

## Chạy từ mã nguồn

Lõi Python (`lynxx/`) chỉ dùng thư viện chuẩn (Python ≥ 3.11), nên không cần cài gói nào:

```bash
npm --prefix app install
npm --prefix app start
```

App tự chạy sidecar `python3 -m lynxx.server`. Muốn dùng Python khác: biến môi trường `LYNXX_PYTHON`, hoặc Cài đặt → Nâng
cao. Dữ liệu nằm ở thư mục userData của Electron (`~/.config/Lynxx`, `%APPDATA%\Lynxx`, `~/Library/Application Support/Lynxx`):
`library/` (truyện, sách), `index/library.sqlite`, `plugins/` (extension), `settings.json`. `LYNXX_USER_DATA` đổi thư mục
này (thử nghiệm trên hồ sơ riêng).

## Test

```bash
python3 -m unittest discover -s lynxx/tests -t .
npm --prefix app run check          # cú pháp + test Node: icon, runtime extension, job tải
# cần màn hình (hoặc xvfb-run -a):
npx --prefix app electron app/test/electron-smoke.js      # sandbox, cầu nối đồng bộ, HTTP thật, printToPDF, WebP → JPEG
npx --prefix app electron app/test/electron-e2e.js        # sidecar thật + site giả: cài extension → tải → thư viện → reader → xuất
```

Test đụng tới thư mục dữ liệu thì đặt `LYNXX_DATA_DIR` và `XDG_CONFIG_HOME` vào thư mục tạm.

## Build bản cài

1. Frozen core (PyInstaller one-dir, có smoke test: sidecar khởi động và nhập thử 8 định dạng):

   ```bash
   python3 -m pip install 'pyinstaller>=6.21'
   python3 tools/build_core.py          # → app/build/core/lynxx-core
   ```

2. Gói Electron (electron-builder, cấu hình trong `app/package.json` → `build`):

   ```bash
   cd app && npm ci && npx electron-builder --linux AppImage deb --x64 --publish never
   ```

   Tên file: `lynxx-<os>-<arch>_v<version>.<ext>`, bản cài Windows `lynxx-setup-<arch>_v<version>.exe`. Hook
   `app/build/after-pack.js` bỏ quyền ghi của group/other trong thư mục app trước khi đóng gói.

3. Self-test của bản đã đóng gói (không cần mạng): sidecar, RPC renderer được phép / bị chặn, các trang và theme, nhập
   file → đọc chương → xuất EPUB, chạy extension trong sandbox đóng gói, hỏi lại khi thoát lúc còn lượt tải:

   ```bash
   LYNXX_SELFTEST_QUICK=1 LYNXX_SELFTEST_DIR=/tmp/st LYNXX_USER_DATA=/tmp/st-profile xvfb-run -a app/dist/linux-unpacked/lynxx --no-sandbox
   python3 tools/check_selftest.py /tmp/st/report.json
   ```

   Bỏ `LYNXX_SELFTEST_QUICK` để có thêm ảnh chụp từng trang trong thư mục report.

## Phát hành (giống Lustra)

Nhánh: `develop` (làm việc), `staging` (bản thử), `production` (bản chính thức).

1. Tăng phiên bản ở cả `app/package.json`, `app/package-lock.json` (2 chỗ), `pyproject.toml`, `lynxx/vcs/version.py`;
   viết ghi chú `docs/releases/v<version>.md` (workflow đọc file này làm nội dung trang release).
2. Đẩy `develop`, rồi cho `staging` trỏ tới cùng commit và đẩy lên: workflow **Release** build 6 nền tảng (Linux x64 /
   arm64, Windows x64 / arm64, macOS Apple Silicon / Intel), chạy self-test trên Linux, tạo pre-release
   `v<version>-rc.<run>` kèm `SHA256SUMS.txt` và `build-info.json`.
3. Thử bản rc, rồi cho `production` trỏ tới cùng commit và đẩy lên: workflow lấy lại file của bản rc khớp cây mã nguồn
   (kiểm SHA-256), không build lại, và tạo release `v<version>` (latest).

GitHub không tạo sự kiện push cho repo này từ 25/11/2026 (Lustra cùng tổ chức vẫn bình thường), nên push có thể không
khởi động workflow nào. Khi đó chạy tay với tuỳ chọn `publish`, kết quả giống hệt push:

```bash
gh workflow run release.yml --repo Leiz-Workspace/Lynxx --ref staging -f publish=true       # bản rc
gh workflow run release.yml --repo Leiz-Workspace/Lynxx --ref production -f publish=true    # bản chính thức
```

Workflow **Tests** chạy test Python (3.11 → 3.15, Linux / Windows / macOS) và test Node ở mỗi lần đẩy. Workflow **Builds**
(chạy tay) build app cho hệ điều hành được chọn mà không phát hành.

Forgejo (máy chủ riêng, mirror kéo từ GitHub) có `.forgejo/workflows/ci.yml` (test) và `forgejo-release.yml` (khi tag
`v*` tới qua mirror: build lại bản Linux x64, ký `SHA256SUMS-linux` bằng minisign, đẩy vào registry Debian / generic).
