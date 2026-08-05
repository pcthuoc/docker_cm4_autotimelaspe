#!/usr/bin/env python3
"""
AutoTimelapse CM4 Agent - Module Cấu Hình (Config)
------------------------------------------------------------------
Đọc biến môi trường (Environment Variables) và cấu hình hệ thống.
"""

import os

# Cấu hình Camera & MQTT Server
CAMERA_CODE        = os.getenv("CAMERA_CODE", "CAM-4YZ8X6")
MQTT_PASSWORD      = os.getenv("MQTT_PASSWORD", os.getenv("DEVICE_SECRET", "o2hs_IojnvqSXlF1b9M-sg"))
MQTT_BROKER        = os.getenv("MQTT_BROKER", "mqtt.congnghetimelapse.com")
MQTT_PORT          = int(os.getenv("MQTT_PORT", "1883"))
SERVER_BASE        = os.getenv("SERVER_BASE", "https://cloud.congnghetimelapse.com")

# Cấu hình Quản lý Nguồn GPIO trên CM4
POWER_GPIO_PIN     = int(os.getenv("POWER_GPIO_PIN", "16"))
POWER_ACTIVE_HIGH  = os.getenv("POWER_ACTIVE_HIGH", "true").lower() in ("true", "1", "yes")
WARMUP_DELAY_SEC   = float(os.getenv("WARMUP_DELAY_SEC", "5.0"))
ALWAYS_KEEP_POWER  = os.getenv("ALWAYS_KEEP_POWER", "false").lower() in ("true", "1", "yes")

# Cấu hình Số lần Thử lại (Retry Rules)
MAX_CAMERA_RETRIES = int(os.getenv("MAX_CAMERA_RETRIES", "3"))
MAX_UPLOAD_RETRIES = int(os.getenv("MAX_UPLOAD_RETRIES", "3"))
UPLOAD_RETRY_DELAY = float(os.getenv("UPLOAD_RETRY_DELAY", "2.0"))

# Cấu hình Telemetry & Hàng Đợi Offline
TELEMETRY_INTERVAL     = int(os.getenv("TELEMETRY_INTERVAL", "30"))
OFFLINE_QUEUE_DIR      = os.getenv("OFFLINE_QUEUE_DIR", "/app/offline_queue")
OFFLINE_RETRY_INTERVAL = int(os.getenv("OFFLINE_RETRY_INTERVAL", "60"))

# Danh sách thông số máy ảnh hỗ trợ
SETTING_SPECS = {
    "iso":                   ("iso",                 True),
    "aperture":              ("f-number",            True),
    "shutter_speed":         ("shutterspeed2",       True),
    "exposure_compensation": ("exposurecompensation",True),
    "white_balance":         ("whitebalance",        True),
    "image_format":          ("imagequality",        True),
    "image_size":            ("imagesize",           True),
    "focus_mode":            ("focusmode2",          True),
    "autofocus":             ("autofocus",           True),
    "capture_mode":          ("capturemode",         True),
    "capture_target":        ("capturetarget",       True),
    "high_iso_nr":           ("highisonr",           True),
    "long_exp_nr":           ("longexpnr",           True),
    "liveview_af":           ("liveviewaffocus",     True),
    "exposure_mode":         ("expprogram",          False),
    "focus_switch":          ("focusmode",           False),
}

SIM_INFO_TELEMETRY = {
    "operator": "CM4 4G/WiFi Gateway",
    "number": "+84987654321",
    "iccid": "8984047123456789012",
}
