# Docker CM4 AutoTimelapse Agent

Thư mục này chứa toàn bộ mã nguồn, cấu hình Docker và script hoàn toàn độc lập dành cho **Raspberry Pi CM4** để điều khiển máy ảnh DSLR/Mirrorless chụp timelapse và đồng bộ dữ liệu về hệ thống **AutoTimelapse Cloud**.

---

## 📁 Cấu trúc thư mục

```text
docker_cm4_autotimelapse/
├── camera_wifi_agent.py   # Script Python chính (Tối ưu loop, GPIO 16 power control, Env variables)
├── Dockerfile             # Docker image cho RaspCM4 (ARM64) tích hợp gphoto2, USB & GPIO
├── docker-compose.yml     # File khởi chạy Docker Compose với privileged mode & USB mapping
├── requirements.txt       # Danh sách thư viện Python cần thiết
├── .env.example           # File mẫu biến môi trường
├── .env                   # Configuration thực tế
└── README.md              # Hướng dẫn chi tiết
```

---

## 🚀 Tính năng chính & Tối ưu hóa

1. **Đưa toàn bộ tham số ra Biến Môi Trường (Env Vars)**:
   - Tất cả tham số kết nối Server, MQTT Broker, mật khẩu thiết bị và cấu hình GPIO được lấy trực tiếp từ `.env` hoặc tham số truyền vào (`--code`, `--broker`, `--power-gpio`, v.v.).
2. **Quản lý Nguồn Máy Ảnh qua GPIO 16**:
   - Tích hợp lớp `CameraPowerManager` điều khiển **GPIO 16** trên Raspberry Pi CM4.
   - **Quy trình chụp**: Tự động bật nguồn máy ảnh qua GPIO 16 ➔ Chờ thời gian Warmup (mặc định 3s) để máy ảnh nhận USB ➔ Tiến hành Chụp & Upload S3 ➔ Tắt nguồn máy ảnh (nếu không chạy LiveView & chu kỳ > 15s) để tiết kiệm năng lượng.
   - Chạy an toàn với fallback giả lập nếu không có phần cứng GPIO (khi test trên máy tính cá nhân).
3. **Hybrid Camera Backend (gphoto2 USB + Fallback PIL)**:
   - Tự động nhận diện máy ảnh thật qua thư viện `gphoto2` (USB).
   - Tự động fallback sang giả lập ảnh bằng `PIL` nếu chưa cắm máy ảnh USB.
4. **Độc lập 100%**:
   - Không can thiệp hay sửa đổi bất kỳ mã nguồn Backend (Django) hay Frontend (React/Vite) nào của dự án chính.

---

## 🛠️ Hướng dẫn Chạy trên Raspberry Pi CM4

### Cách 1: Chạy bằng Docker Compose (Khuyên dùng)

1. **Chỉnh sửa file cấu hình `.env`**:
   ```bash
   cp .env.example .env
   nano .env
   ```

2. **Khởi chạy container**:
   ```bash
   docker compose up -d --build
   ```

3. **Xem log hoạt động**:
   ```bash
   docker compose logs -f
   ```

---

### Cách 2: Chạy trực tiếp qua Python 3 (Local Test)

1. **Cài đặt thư viện**:
   ```bash
   pip install -r requirements.txt
   ```

2. **Chạy agent**:
   ```bash
   python3 camera_wifi_agent.py --code CAM-4YZ8X6 --power-gpio 16
   ```
