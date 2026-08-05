# Stage 1: Builder - Biên dịch các gói C/C++ & Python wheels
FROM python:3.11-slim-bookworm AS builder

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    make \
    pkg-config \
    python3-dev \
    libgphoto2-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# Stage 2: Final Minimal Runtime Image - Chỉ giữ lại thư viện chạy thực tế
FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src

# Chỉ cài các thư viện runtime siêu nhẹ (KHÔNG cài gcc/g++/make/dev)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgphoto2-6 \
    udev \
    usbutils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
RUN mkdir -p /app/offline_queue

# Copy các thư viện Python đã biên dịch từ Stage 1
COPY --from=builder /install /usr/local

# Copy mã nguồn dự án
COPY src/ /app/src/
COPY src/ /app/

# Khởi chạy Orchestrator main.py
CMD ["python3", "-u", "/app/src/main.py"]
