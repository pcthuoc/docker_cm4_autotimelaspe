#!/usr/bin/env python3
"""
AutoTimelapse CM4 Agent - Module Quản Lý Nguồn GPIO
------------------------------------------------------------------
Điều khiển Bật/Tắt nguồn máy ảnh qua chân GPIO (mặc định GPIO 16) trên Raspberry Pi CM4.
Hỗ trợ Reset nguồn cứng (Hard Power Cycle) khi thiết bị USB bị kẹt.
"""

import time
import logging
import threading

log = logging.getLogger("cm4_power_manager")

HAS_GPIO = False
try:
    import RPi.GPIO as GPIO
    HAS_GPIO = True
except (ImportError, RuntimeError):
    GPIO = None


class CameraPowerManager:
    """Quản lý nguồn cấp điện cho máy ảnh qua GPIO 16 trên Raspberry Pi CM4."""

    def __init__(self, pin=16, active_high=True, warmup_delay=3.0):
        self.pin = pin
        self.active_high = active_high
        self.warmup_delay = warmup_delay
        self.is_powered = False
        self.has_hardware_gpio = HAS_GPIO
        self._lock = threading.Lock()
        self.last_power_off_time = 0.0

        self._init_gpio()

    def _init_gpio(self):
        if self.has_hardware_gpio:
            try:
                GPIO.setmode(GPIO.BCM)
                GPIO.setwarnings(False)
                GPIO.setup(self.pin, GPIO.OUT)
                off_state = GPIO.HIGH if not self.active_high else GPIO.LOW
                GPIO.output(self.pin, off_state)
                log.info("⚡ [GPIO] Đã khởi tạo thành công GPIO Pin %d điều khiển nguồn Máy ảnh", self.pin)
            except Exception as e:
                log.warning("⚠️ Không thể cấu hình GPIO Pin %d: %s. Chuyển sang Giả lập GPIO.", self.pin, e)
                self.has_hardware_gpio = False
        else:
            log.info("ℹ️ Không tìm thấy phần cứng RPi.GPIO. Quản lý nguồn chạy ở chế độ GIẢ LẬP (Simulated GPIO %d).", self.pin)

    MIN_POWER_OFF_DURATION = 30.0  # Chống nháy nguồn: máy ảnh phải tắt ít nhất 30s trước khi bật lại

    def power_on(self):
        """Bật nguồn máy ảnh và chờ phần cứng khởi động (warmup delay), có chống nháy relay (30s)."""
        with self._lock:
            if not self.is_powered:
                # Chống nháy relay: Nếu vừa tắt nguồn dưới 30s, chờ đủ 30s để tụ xả sạch tránh làm treo máy ảnh
                elapsed = time.monotonic() - self.last_power_off_time
                if elapsed < self.MIN_POWER_OFF_DURATION:
                    wait_sec = self.MIN_POWER_OFF_DURATION - elapsed
                    log.info("🛡️ [ANTI-BOUNCE] Máy ảnh vừa tắt %.1fs trước. Chờ %.1fs (đủ 30s) để tụ xả sạch trước khi bật lại...", elapsed, wait_sec)
                    time.sleep(wait_sec)

                log.info("🔌 [POWER ON] Đang BẬT NGUỒN máy ảnh qua GPIO %d...", self.pin)
                if self.has_hardware_gpio:
                    on_state = GPIO.HIGH if self.active_high else GPIO.LOW
                    GPIO.output(self.pin, on_state)
                self.is_powered = True

                if self.warmup_delay > 0:
                    log.info("⏳ Chờ %.1f giây để máy ảnh khởi động & nhận USB...", self.warmup_delay)
                    time.sleep(self.warmup_delay)
                return True
            else:
                log.debug("🔌 Nguồn máy ảnh hiện đã đang BẬT.")
                return False

    def power_off(self):
        """Tắt nguồn máy ảnh để tiết kiệm điện trên CM4."""
        with self._lock:
            if self.is_powered:
                log.info("🔌 [POWER OFF] Đang TẮT NGUỒN máy ảnh qua GPIO %d...", self.pin)
                if self.has_hardware_gpio:
                    off_state = GPIO.LOW if self.active_high else GPIO.HIGH
                    GPIO.output(self.pin, off_state)
                self.is_powered = False
                self.last_power_off_time = time.monotonic()
                return True
            return False

    def hard_cycle_power(self, power_off_delay=30.0):
        """Tắt nguồn GPIO 16, chờ delay (ít nhất 30s) rồi bật lại để khởi động lại máy ảnh khi USB kẹt."""
        delay = max(self.MIN_POWER_OFF_DURATION, power_off_delay)
        log.warning("🔄 [HARD POWER CYCLE] Tiến hành tắt nguồn máy ảnh, chờ %.1fs trước khi bật lại...", delay)
        self.power_off()
        time.sleep(delay)
        self.power_on()

    def cleanup(self):
        if self.has_hardware_gpio:
            try:
                self.power_off()
                GPIO.cleanup()
                log.info("🧹 Giải phóng tài nguyên GPIO hoàn tất.")
            except Exception as e:
                log.warning("Lỗi cleanup GPIO: %s", e)
