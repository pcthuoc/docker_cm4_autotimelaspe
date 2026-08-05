FROM python:3.11-slim-bookworm

# Thiết lập biến môi trường
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src

# Cài đặt các gói hệ thống cần thiết cho gphoto2, USB và GPIO trên Raspberry Pi CM4
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    make \
    pkg-config \
    python3-dev \
    libgphoto2-dev \
    libgphoto2-6 \
    udev \
    usbutils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Khởi tạo thư mục hàng đợi offline persistent
RUN mkdir -p /app/offline_queue

# Copy và cài đặt thư viện Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy tất cả các file mã nguồn từ thư mục src
COPY src/ /app/src/
COPY src/ /app/

# Lệnh chạy chính khởi chạy Orchestrator main.py
CMD ["python3", "-u", "/app/src/main.py"]
