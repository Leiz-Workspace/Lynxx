// Lynxx: general guide of the gender image model (Settings → Model nhận diện giới tính → Hướng dẫn, and the warning shown
// before the model is turned on). Data only: app.js renders it with textContent (`code` and **bold** spans, no HTML).
// Blocks: ['p', text] paragraph, ['ul', [text...]] list, ['code', text] command (copy button), ['note', text] highlighted.
'use strict';
window.LYNXX_GUIDE = {
  title: 'Hướng dẫn chung · Model nhận diện giới tính',
  sections: [
    {
      id: 'about',
      title: 'Model làm gì',
      blocks: [
        ['p', 'Model là **WD ViT Tagger v3** (SmilingWolf, giấy phép Apache-2.0): một mạng ViT xem ảnh 448×448 rồi đoán tag kiểu danbooru như `1girl`, `1boy`, `no_humans`.'],
        ['ul', [
          'Chỉ dùng cho post có tag chưa đủ để biết giới tính. Post đủ tag được lọc theo tag như bình thường, không gửi ảnh.',
          'App suy giới tính từ tag model đoán bằng **cùng luật** với lọc theo tag. Tag gợi ý trẻ vị thành niên mà model thấy được dùng để chặn thêm.',
          'Dành cho tranh vẽ / 3D. Ảnh chụp người thật được để "Không rõ".',
          'Kết quả được nhớ theo từng post và từng model: lần sau không gửi lại ảnh.',
        ]],
      ],
    },
    {
      id: 'hardware',
      title: 'Phần cứng cần',
      blocks: [
        ['ul', [
          '**GPU có VRAM riêng** (NVIDIA, ≥ 1,5 GB VRAM trống) và onnxruntime bản GPU: mỗi ảnh khoảng 20 ms (đo trên RTX 4070 Ti SUPER, model chiếm ~1,3 GB VRAM), gần như không tốn CPU.',
          '**Không có GPU**: model chạy bằng CPU, mỗi ảnh khoảng 0,3–1 giây (chậm hơn GPU khoảng 20 lần), chiếm khoảng 550 MB RAM khi đang nạp và tối đa 4 luồng CPU.',
          'GPU tích hợp (Intel, AMD APU) dùng chung RAM với máy nên không tính là VRAM.',
        ]],
        ['note', 'Nên để tắt khi máy không có VRAM mà lại yếu, còn ít RAM trống (dưới 2 GB), đang chạy pin, hoặc bạn không cần lọc giới tính cho post thiếu tag. Lọc theo tag vẫn hoạt động khi model tắt.'],
      ],
    },
    {
      id: 'onoff',
      title: 'Bật / tắt model',
      blocks: [
        ['ul', [
          '**Bật**: Cài đặt → Model nhận diện giới tính → **Dùng model**. App hỏi server về GPU và VRAM trước. Có GPU đủ VRAM thì model được nạp ngay; không có thì app cảnh báo và bạn chọn để tắt hoặc vẫn bật (chạy bằng CPU).',
          'Sau khi bật, ở bộ lọc giới tính chọn chế độ **Tag + model (ảnh)**.',
          '**Tắt**: job và preview chỉ lọc theo tag. Server trên máy này giải phóng bộ nhớ của model ngay.',
          'Server cũng tự giải phóng model sau 15 phút không nhận ảnh nào và chỉ nạp lại khi cần, nên để server chạy nền hầu như không tốn gì.',
        ]],
        ['p', 'Dừng hẳn server chạy nền (Linux, systemd):'],
        ['code', 'systemctl --user disable --now lynxx-model-server'],
      ],
    },
    {
      id: 'server',
      title: 'Chạy model server',
      blocks: [
        ['p', 'App không kèm model: model chạy trong một **model server** riêng, app chỉ gửi ảnh tới địa chỉ API trong Cài đặt (mặc định `http://127.0.0.1:8765`). Lần đầu chạy, server tự tải model (~380 MB, có kiểm sha256).'],
        ['p', '**Bản chạy độc lập** (không cần Python): giải nén `lynxx-model-server-<hệ điều hành>-<kiến trúc>-cpu_v<phiên bản>` (tải ở trang Releases) vào một thư mục rồi chạy file `lynxx-model-server` trong đó. Cho chạy nền mỗi khi đăng nhập:'],
        ['ul', [
          'Linux: chép thư mục vào `~/.local/share/lynxx-model-server`, rồi dùng file `lynxx-model-server-bundle.service` đi kèm (lệnh ở đầu file).',
          'macOS: trong thư mục đó chạy `sh install-model-server.sh`; gỡ bằng `sh install-model-server.sh --uninstall`.',
          'Windows: trong thư mục đó chạy `powershell -ExecutionPolicy Bypass -File install-model-server.ps1`; gỡ bằng thêm `-Uninstall`.',
          'Bản này chạy bằng CPU (Windows có thêm bản DirectML cho GPU). GPU NVIDIA: dùng môi trường Python hoặc image Docker GPU (mục Dùng GPU).',
        ]],
        ['p', 'Trên máy này, từ thư mục mã nguồn Lynxx:'],
        ['code', 'pip install -e ".[model]"'],
        ['code', 'python -m lynxx.model_server'],
        ['p', 'Chạy nền mỗi khi đăng nhập (Linux, systemd user):'],
        ['code', 'cp tools/systemd/lynxx-model-server.service ~/.config/systemd/user/'],
        ['code', 'systemctl --user daemon-reload && systemctl --user enable --now lynxx-model-server'],
        ['p', 'Bằng Docker (chỉ CPU), có token:'],
        ['code', 'docker build -f tools/model_server/Dockerfile -t lynxx-model-server .'],
        ['code', 'docker run -d --name lynxx-model -e LYNXX_GENDER_MODEL_TOKEN=<token> -p 127.0.0.1:8765:8765 -v lynxx-model:/models lynxx-model-server'],
        ['p', 'Trên máy khác trong mạng nội bộ (ví dụ máy có GPU): chạy server với token, rồi nhập `http://<ip-máy-đó>:8765` và token vào app.'],
        ['code', 'LYNXX_GENDER_MODEL_TOKEN=<token> python -m lynxx.model_server --host 0.0.0.0'],
        ['note', 'Server từ chối chạy ở địa chỉ khác máy này nếu không có token. Ra ngoài mạng nội bộ thì phải dùng https (reverse proxy), app không gửi ảnh và token qua http ra internet.'],
      ],
    },
    {
      id: 'gpu',
      title: 'Dùng GPU (VRAM)',
      blocks: [
        ['p', 'Server chạy model trên GPU khi onnxruntime của nó là bản GPU; bản `onnxruntime` thường chỉ chạy CPU. Với GPU NVIDIA (driver hỗ trợ CUDA 13, tức bản 580 trở lên; lệnh `nvidia-smi` chạy được), trong môi trường Python của server:'],
        ['code', 'pip uninstall -y onnxruntime'],
        ['code', 'pip install "onnxruntime-gpu[cuda,cudnn]"'],
        ['p', 'Gói này kèm thư viện CUDA và cuDNN (khoảng 2,4 GB). Khởi động lại server rồi bấm **Kiểm tra**: dòng Phần cứng ghi onnxruntime "có GPU (CUDA)", và khi bật model app báo "chạy bằng GPU (CUDA)".'],
        ['ul', [
          '`--device cpu`: luôn chạy CPU, kể cả khi có GPU.',
          '`--device gpu`: chỉ chạy GPU; server báo lỗi thay vì lặng lẽ chạy bằng CPU.',
          'Windows với GPU bất kỳ: `onnxruntime-directml`. AMD trên Linux: onnxruntime bản ROCm. Hai cách này chưa được thử với Lynxx.',
          'Docker có GPU NVIDIA (cần NVIDIA Container Toolkit): build image với `--build-arg ORT_PACKAGE=\'onnxruntime-gpu[cuda,cudnn]\'` rồi chạy với `--gpus all` (lệnh đầy đủ ở đầu `tools/model_server/Dockerfile`).',
        ]],
      ],
    },
    {
      id: 'privacy',
      title: 'Quyền riêng tư',
      blocks: [
        ['ul', [
          'Chỉ ảnh của post thiếu tag được gửi tới server. Server giải mã ảnh trong bộ nhớ, không lưu, không ghi ảnh vào log, và không biết bạn tìm gì trên site nào.',
          'http chỉ được dùng cho máy này và mạng nội bộ; ngoài đó phải là https.',
          'Token được mã hoá bằng kho khoá của hệ điều hành và không hiện lại trong app.',
        ]],
      ],
    },
    {
      id: 'trouble',
      title: 'Sự cố thường gặp',
      blocks: [
        ['ul', [
          '**Không kết nối được**: server chưa chạy hoặc sai cổng. Xem trạng thái dịch vụ, hoặc chạy server trong terminal để thấy lỗi.',
          '**wrong or missing token**: token trong app khác `LYNXX_GENDER_MODEL_TOKEN` của server.',
          '**Có GPU nhưng vẫn chạy CPU**: chưa cài onnxruntime-gpu, hoặc thiếu thư viện CUDA / cuDNN. Log của server có dòng `hardware:` và `model loaded on …` cho biết model chạy ở đâu.',
          '**Job dừng với lỗi model**: server tắt hoặc hết VRAM giữa chừng. Bật lại server rồi chạy lại job; post đã nhận diện được giữ trong cache.',
        ]],
        ['code', 'systemctl --user status lynxx-model-server'],
        ['code', 'journalctl --user -u lynxx-model-server -f'],
      ],
    },
  ],
};
