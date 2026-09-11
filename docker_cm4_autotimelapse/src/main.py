#!/usr/bin/env python3
"""
AutoTimelapse CM4 Camera Agent - Main Orchestrator
------------------------------------------------------------------
Luồng xử lý chính kết hợp:
  - MQTT (auto-reconnect, QoS)
  - Điều khiển nguồn GPIO 16
  - Quản lý USB Máy ảnh (gphoto2 + USB reset + hard power cycle)
  - Hàng đợi Upload Offline (3x retry → local disk → auto retry)
  - Telemetry thật (SIM/modem/WiFi, CPU temp, RAM, Network)
  - Watchdog giám sát & tự động khởi động lại mọi thread bị crash
  - User-Agent header chuẩn cho HTTP requests bypass WAF/Cloudflare
"""

import sys
import os

# Ưu tiên load toàn bộ module từ thư mục src/ hiện tại (tránh dính file cũ trong /app của image)
SRC_DIR = os.path.dirname(os.path.abspath(__file__))
if SRC_DIR in sys.path:
    sys.path.remove(SRC_DIR)
sys.path.insert(0, SRC_DIR)

import io
import json
import time
import logging
import argparse
import signal
import threading
import queue as _queue
import urllib.request
import urllib.error
import socket
from datetime import datetime, timezone



from PIL import Image

from config import (
    CAMERA_CODE, MQTT_PASSWORD, MQTT_BROKER, MQTT_PORT, SERVER_BASE,
    POWER_GPIO_PIN, POWER_ACTIVE_HIGH, WARMUP_DELAY_SEC, ALWAYS_KEEP_POWER,
    TELEMETRY_INTERVAL, OFFLINE_QUEUE_DIR, OFFLINE_RETRY_INTERVAL,
    MAX_UPLOAD_RETRIES, UPLOAD_RETRY_DELAY, SIM_INFO_TELEMETRY,
    AUTO_SHUTDOWN_AFTER_CAPTURE, AUTO_CAPTURE_ON_BOOT, SHUTDOWN_DELAY_SEC,
    EC25_STATE_FILE
)
from power_manager import CameraPowerManager
from offline_queue import OfflineQueueManager
from camera_backend import HybridCameraBackend
from telemetry import collect_telemetry, get_sim_info
from watchdog import ThreadWatchdog, ManagedThread

try:
    import paho.mqtt.client as mqtt
except ImportError:
    print("❌ Lỗi: Chưa cài đặt paho-mqtt. Hãy chạy: pip install paho-mqtt pillow")
    sys.exit(1)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(threadName)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("cm4_main_agent")

FIRMWARE_VERSION = "cm4-autotimelapse-v2.0"
USER_AGENT = "AutoTimelapse-CM4-Agent/2.0 (RaspberryPi CM4)"


def optimize_network_mtu():
    """No-op: ip/iptables không có trong container Debian-slim.
    MSS/SNDBUF được patch qua socket.connect monkey-patch ở đầu file.
    """
    pass


class CameraAgent:
    """Quản lý luồng hoạt động chính của Camera Agent trên CM4."""

    def __init__(self, code, password, broker, port, server_base,
                 power_pin=16, power_active_high=True, warmup_delay=3.0,
                 always_keep_power=False, telemetry_interval=30,
                 offline_dir="/app/offline_queue", offline_retry_interval=60,
                 auto_shutdown=AUTO_SHUTDOWN_AFTER_CAPTURE,
                 auto_capture_boot=AUTO_CAPTURE_ON_BOOT,
                 shutdown_delay=SHUTDOWN_DELAY_SEC):
        optimize_network_mtu()
        self.code = code
        self.password = password
        self.broker = broker
        self.port = port
        self.server_base = server_base.rstrip("/")
        self.always_keep_power = always_keep_power
        self.telemetry_interval = telemetry_interval
        self._is_capturing = False           # Cờ khóa Live View khi đang chụp ảnh thật tránh xung đột USB
        self.offline_retry_interval = offline_retry_interval
        self.auto_shutdown_after_capture = auto_shutdown
        self.auto_capture_on_boot = auto_capture_boot
        self.shutdown_delay_sec = shutdown_delay

        self.power_manager = CameraPowerManager(
            pin=power_pin, active_high=power_active_high, warmup_delay=warmup_delay
        )
        self.offline_queue = OfflineQueueManager(queue_dir=offline_dir)
        self.backend = HybridCameraBackend(self.power_manager)
        self.watchdog = ThreadWatchdog()

        self.running = False
        self.capture_interval_sec = 0
        self.live_session_id = None
        self.live_fps = 1
        self.live_seq = 0
        self.cmd_queue = _queue.SimpleQueue()
        self.mqtt_client = None

        self.schedule_enabled = False
        self.work_start_time = "06:00"
        self.work_end_time = "18:00"
        self.schedule_rules = []

        # ── Trạng thái đồng bộ Cưỡng Bức vs. Chu Kỳ ──────────────────────────
        # missed_capture_flag: True khi đã đến mốc chụp nhưng CM4 chưa chụp được
        # (ví dụ: boot chậm, qua mốc, hoặc giữa các chu kỳ sleep).
        # Được persist ra disk để sống sót qua reboot.
        self.missed_capture_flag = False
        self._load_ec25_state()

        self.t_cmd    = f"camera/{self.code}/cmd"
        self.t_ack    = f"camera/{self.code}/ack"
        self.t_data   = f"camera/{self.code}/data"
        self.t_status = f"camera/{self.code}/status"

        self._power_off_timer = None
        self._power_timer_lock = threading.Lock()

        self.load_schedule_config()

    # ── EC25 State File (đồng bộ Cưỡng Bức vs. Chu Kỳ) ──────────────────────

    def _load_ec25_state(self):
        """Đọc trạng thái EC25 từ disk (missed_capture_flag, last_capture_ts)."""
        try:
            if os.path.exists(EC25_STATE_FILE):
                with open(EC25_STATE_FILE, "r", encoding="utf-8") as f:
                    state = json.load(f)
                self.missed_capture_flag = bool(state.get("missed_capture_flag", False))
                log.info("📂 [EC25 STATE] Đã đọc state từ disk: missed_capture=%s",
                         self.missed_capture_flag)
        except Exception as e:
            log.warning("⚠️ Không đọc được ec25_state.json: %s", e)
            self.missed_capture_flag = False

    def _save_ec25_state(self, **kwargs):
        """Lưu trạng thái EC25 xuống disk, merge với state hiện có."""
        try:
            # Đọc state cũ nếu có để merge (EC25 cũng ghi vào file này)
            state = {}
            if os.path.exists(EC25_STATE_FILE):
                try:
                    with open(EC25_STATE_FILE, "r", encoding="utf-8") as f:
                        state = json.load(f)
                except Exception:
                    pass

            # Cập nhật các key được truyền vào
            state.update(kwargs)

            # Luôn đồng bộ missed_capture_flag hiện tại
            state["missed_capture_flag"] = self.missed_capture_flag
            state["last_updated_by"] = "cm4_agent"
            state["last_updated_ts"] = datetime.now(timezone.utc).isoformat()

            os.makedirs(os.path.dirname(EC25_STATE_FILE), exist_ok=True)
            with open(EC25_STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            log.warning("⚠️ Không lưu được ec25_state.json: %s", e)

    def _set_missed_capture_flag(self, value: bool, reason: str = ""):
        """Set missed_capture_flag và đồng bộ ngay xuống disk."""
        self.missed_capture_flag = value
        flag_str = "SET" if value else "CLEAR"
        log.info("🏳 [FLAG:%s] missed_capture_flag=%s%s",
                 flag_str, value, f" ({reason})" if reason else "")
        self._save_ec25_state()

    def load_schedule_config(self):
        """Đọc cấu hình lịch chụp từ ổ đĩa đệm (/app/offline_queue/schedules.json)."""
        filepath = os.path.join(self.offline_queue.queue_dir, "schedules.json")
        if os.path.exists(filepath):
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.schedule_rules = data.get("schedules") or []
                    if "capture_interval_sec" in data:
                        self.capture_interval_sec = int(data["capture_interval_sec"])
                    self.schedule_enabled = bool(data.get("schedule_enabled", False))
                    self.work_start_time = str(data.get("work_start_time", "06:00"))
                    self.work_end_time = str(data.get("work_end_time", "18:00"))
                    log.info("📁 [SCHEDULE DISK] Đã tải %d khung giờ chụp từ đĩa", len(self.schedule_rules))
            except Exception as e:
                log.warning("Không đọc được schedules.json: %s", e)

    def save_schedule_config(self):
        """Lưu cấu hình lịch chụp xuống ổ đĩa đệm (/app/offline_queue/schedules.json) để dùng khi offline."""
        filepath = os.path.join(self.offline_queue.queue_dir, "schedules.json")
        try:
            data = {
                "schedules": self.schedule_rules,
                "capture_interval_sec": self.capture_interval_sec,
                "schedule_enabled": self.schedule_enabled,
                "work_start_time": self.work_start_time,
                "work_end_time": self.work_end_time,
            }
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            log.info("💾 [SCHEDULE DISK] Đã lưu %d lịch chụp xuống đĩa", len(self.schedule_rules))
        except Exception as e:
            log.warning("Không lưu được schedules.json: %s", e)

    def get_active_schedule_slot(self):
        """Trả về (is_active, interval_sec, slot_name) cho thời gian hiện tại theo MÚI GIỜ VIỆT NAM (UTC+7)."""
        from datetime import datetime, timezone as dt_tz, timedelta, time as time_obj
        vn_tz = dt_tz(timedelta(hours=7))
        now_dt = datetime.now(vn_tz)
        weekday = now_dt.isoweekday()  # 1=Mon ... 7=Sun
        now_time = now_dt.time()

        # 1. Ưu tiên kiểm tra danh sách multi-slot schedules
        if self.schedule_rules:
            active_slots = []
            for slot in self.schedule_rules:
                if not slot.get("is_enabled", True):
                    continue
                days = slot.get("days_of_week") or [1, 2, 3, 4, 5, 6, 7]
                if days and weekday not in days:
                    continue
                try:
                    sh, sm = map(int, str(slot.get("start_time", "00:00")).split(":"))
                    eh, em = map(int, str(slot.get("end_time", "23:59")).split(":"))
                    st, et = time_obj(sh, sm), time_obj(eh, em)
                    matched = (st <= now_time <= et) if (st <= et) else (now_time >= st or now_time <= et)
                    if matched:
                        active_slots.append(slot)
                except Exception:
                    pass
            if active_slots:
                min_interval = min(int(s.get("interval_sec", 300)) for s in active_slots)
                slot_names = ", ".join(s.get("name", "Ca") for s in active_slots)
                return True, max(30, min_interval), slot_names
            return False, 0, None

        # 2. Fallback về đơn khung giờ
        if not self.schedule_enabled:
            return True, self.capture_interval_sec, "Mặc định"
        try:
            sh, sm = map(int, self.work_start_time.split(":"))
            eh, em = map(int, self.work_end_time.split(":"))
            st, et = time_obj(sh, sm), time_obj(eh, em)
            matched = (st <= now_time <= et) if (st <= et) else (now_time >= st or now_time <= et)
            if matched:
                return True, self.capture_interval_sec, f"Khung giờ {self.work_start_time}–{self.work_end_time}"
        except Exception:
            return True, self.capture_interval_sec, "Mặc định"
        return False, 0, None

    def get_seconds_to_next_aligned_slot(self, interval_sec):
        """Tính số giây cần chờ đến mốc chụp chẵn kế tiếp tính từ phút 00 của mỗi giờ (theo UTC+7).
        Ví dụ:
          - interval = 300s (5 phút)  -> các mốc :00, :05, :10, :15, :20, :25, :30...
          - interval = 600s (10 phút) -> :00, :10, :20, :30, :40, :50
          - interval = 900s (15 phút) -> :00, :15, :30, :45
          - interval = 1200s (20 phút) -> :00, :20, :40
          - interval = 1800s (30 phút) -> :00, :30
          - interval = 3600s (60 phút) -> :00
        """
        if interval_sec <= 0:
            return 300
        from datetime import datetime, timezone as dt_tz, timedelta
        vn_tz = dt_tz(timedelta(hours=7))
        now = datetime.now(vn_tz)
        current_second_in_hour = now.minute * 60 + now.second + (now.microsecond / 1000000.0)

        if interval_sec <= 3600:
            rem = current_second_in_hour % interval_sec
            wait_sec = interval_sec - rem
            if wait_sec < 1.0:
                wait_sec += interval_sec
            return max(1.0, wait_sec)
        else:
            current_sec_in_day = now.hour * 3600 + now.minute * 60 + now.second
            rem = current_sec_in_day % interval_sec
            wait_sec = interval_sec - rem
            if wait_sec < 1.0:
                wait_sec += interval_sec
            return max(1.0, wait_sec)

    def is_on_aligned_slot(self, interval_sec, tolerance_sec=180):
        """Kiểm tra thời điểm hiện tại có nằm trong cửa sổ mốc chu kỳ chẵn hay không."""
        if interval_sec <= 0:
            return True
        from datetime import datetime, timezone as dt_tz, timedelta
        vn_tz = dt_tz(timedelta(hours=7))
        now = datetime.now(vn_tz)
        if interval_sec <= 3600:
            sec_in_period = now.minute * 60 + now.second
        else:
            sec_in_period = now.hour * 3600 + now.minute * 60 + now.second

        rem = sec_in_period % interval_sec
        return rem <= tolerance_sec or (interval_sec - rem) <= 15

    # ── HTTP Helpers ──────────────────────────────────────────────────────────

    def _http_get_json(self, path):
        req = urllib.request.Request(
            self.server_base + path, method="GET",
            headers={
                "X-Device-Key": self.code,
                "X-Device-Secret": self.password,
                "User-Agent": USER_AGENT,
            },
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read().decode() or "{}")

    def pull_server_config(self):
        """Pulls latest configuration, schedules, and force_power_on status from Backend."""
        try:
            st, data = self._http_get_json("/api/device/config/")
            if st == 200 and data.get("ok"):
                if "capture_interval_sec" in data:
                    self.capture_interval_sec = int(data["capture_interval_sec"])
                if "schedule_enabled" in data:
                    self.schedule_enabled = bool(data["schedule_enabled"])
                if "work_start_time" in data:
                    self.work_start_time = str(data["work_start_time"])
                if "work_end_time" in data:
                    self.work_end_time = str(data["work_end_time"])
                if "schedules" in data:
                    self.schedule_rules = data["schedules"]

                # Đồng bộ ngưỡng nhiệt độ quạt từ Server nếu Backend có trả về
                if "fan_temp_off" in data:
                    os.environ["FAN_TEMP_OFF"] = str(data["fan_temp_off"])
                if "fan_temp_mid" in data:
                    os.environ["FAN_TEMP_MID"] = str(data["fan_temp_mid"])
                if "fan_temp_high" in data:
                    os.environ["FAN_TEMP_HIGH"] = str(data["fan_temp_high"])

                try:
                    from telemetry import get_fan_controller
                    fc = get_fan_controller()
                    if fc:
                        if "fan_temp_off" in data:
                            fc.temp_off = float(data["fan_temp_off"])
                        if "fan_temp_mid" in data:
                            fc.temp_mid = float(data["fan_temp_mid"])
                        if "fan_temp_high" in data:
                            fc.temp_high = float(data["fan_temp_high"])
                except Exception:
                    pass

                self.save_schedule_config()
                force_on = bool(data.get("force_power_on", False))
                log.info("📥 [CONFIG SYNC] Kéo thành công từ Server: interval=%ds, force_power_on=%s, %d lịch",
                         self.capture_interval_sec, force_on, len(self.schedule_rules))
                return True, force_on
        except Exception as e:
            log.warning("⚠️ Không thể kéo cấu hình từ Server (%s), dùng cấu hình local.", e)
        return False, False

    def _http_post_json(self, path, obj):
        body = json.dumps(obj).encode()
        req = urllib.request.Request(
            self.server_base + path, data=body, method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Device-Key": self.code,
                "X-Device-Secret": self.password,
                "User-Agent": USER_AGENT,
            },
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode() or "{}")

    def _http_post_frame(self, session_id, seq, frame_bytes):
        req = urllib.request.Request(
            self.server_base + "/api/device/live/frame/",
            data=frame_bytes, method="POST",
            headers={
                "Content-Type": "image/jpeg",
                "X-Device-Key": self.code,
                "X-Device-Secret": self.password,
                "X-Live-Session": session_id,
                "X-Frame-Seq": str(seq),
                "User-Agent": USER_AGENT,
            },
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode() or "{}")

    def _http_put(self, url, data, content_type):
        """Upload dữ liệu ảnh lên presigned URL (timeout 300s)."""
        log.info("📤 [HTTP PUT] Đang upload %.2f MB lên storage...", len(data) / (1024 * 1024))
        req = urllib.request.Request(
            url, data=data, method="PUT",
            headers={
                "Content-Type": content_type,
                "User-Agent": USER_AGENT,
            }
        )
        with urllib.request.urlopen(req, timeout=300) as r:
            log.info("✅ [HTTP PUT] Hoàn tất PUT status=%s", r.status)
            return r.status

    # ── Upload ────────────────────────────────────────────────────────────────

    def _do_upload_to_server(self, final_bytes, thumb_bytes, metadata):
        """Upload S3 Presigned URL workflow (Presign → PUT → Complete)."""
        try:
            content_type = metadata.get("content_type", "image/jpeg")
            taken_at = metadata.get("taken_at") or datetime.now(timezone.utc).isoformat()

            st, pre = self._http_post_json("/api/device/upload/presign/", {
                "content_type": content_type,
                "taken_at": taken_at,
                "with_thumb": thumb_bytes is not None,
            })
            if st != 200:
                log.error("Lỗi xin Presigned URL: status=%s resp=%s", st, pre)
                return False, None

            st_put = self._http_put(pre["url"], final_bytes, content_type)
            if st_put not in (200, 204):
                log.error("Lỗi PUT ảnh chính: status=%s url=%s", st_put, pre.get("url"))
                return False, None

            if thumb_bytes and "thumb_url" in pre:
                st_thumb = self._http_put(pre["thumb_url"], thumb_bytes, "image/jpeg")
                if st_thumb not in (200, 204):
                    log.error("Lỗi PUT thumb: status=%s", st_thumb)
                    return False, None

            st, done = self._http_post_json("/api/device/upload/complete/", {
                "media_id": pre["media_id"],
                "key": pre["key"],
                "thumb_key": pre.get("thumb_key"),
                "taken_at": taken_at,
                "width": metadata.get("width", 1920),
                "height": metadata.get("height", 1080),
                "content_type": content_type,
                "source_name": metadata.get("source_name", "CM4_CAM.jpg"),
                "size_bytes": len(final_bytes),
            })

            if st == 200 and done.get("ok"):
                return True, done["media_id"]
            log.error("Lỗi Complete Upload: status=%s resp=%s", st, done)
            return False, None
        except Exception as exc:
            log.warning("Upload thất bại: %s", exc)
            return False, None

    def upload_capture(self, triggered_by: str = "schedule"):
        """Thực hiện chu trình chụp đầy đủ:
        GPIO ON → Capture → Retry 3x upload → Offline Queue → Power Management.

        Args:
            triggered_by: "schedule" (chu kỳ EC25) hoặc "force_on" (cưỡng bức) hoặc "mqtt" (lệnh thủ công).
        """
        self._is_capturing = True
        try:
            self._ensure_camera_ready()

            taken_at = datetime.now(timezone.utc).isoformat()
            captured_files = self.backend.capture(camera_code=self.code)
            media_ids = []

            for filename, image_bytes, thumb in captured_files:
                final_bytes, width, height = self._normalize_image_bytes(image_bytes, thumb)

                if thumb is None:
                    try:
                        with Image.open(io.BytesIO(final_bytes)) as im:
                            t = im.copy()
                            t.thumbnail((480, 320))
                            buf = io.BytesIO()
                            t.save(buf, "JPEG", quality=82)
                            thumb = buf.getvalue()
                    except Exception as e:
                        log.warning("Lỗi sinh thumbnail: %s", e)

                metadata = {
                    "content_type": "image/jpeg",
                    "taken_at": taken_at,
                    "source_name": filename,
                    "width": width,
                    "height": height,
                    "camera_code": self.code,
                }

                ok, media_id = False, None
                for attempt in range(1, MAX_UPLOAD_RETRIES + 1):
                    ok, media_id = self._do_upload_to_server(final_bytes, thumb, metadata)
                    if ok:
                        break
                    log.warning("⚠️ Upload lần %d/%d thất bại.%s",
                                attempt, MAX_UPLOAD_RETRIES,
                                f" Thử lại sau {UPLOAD_RETRY_DELAY}s..." if attempt < MAX_UPLOAD_RETRIES else " → Offline Queue.")
                    if attempt < MAX_UPLOAD_RETRIES:
                        time.sleep(UPLOAD_RETRY_DELAY)

                if ok and media_id:
                    log.info("🎉 Upload OK! media_id=%s file=%s (%d bytes, %dx%d)",
                             media_id, filename, len(final_bytes), width, height)
                    media_ids.append(media_id)
                else:
                    self.offline_queue.save_pending_capture(final_bytes, thumb, metadata)

            # ── Sau khi chụp xong: xoá missed_capture_flag và thông báo cho EC25 ──
            if self.missed_capture_flag:
                self._set_missed_capture_flag(False, f"chụp xong (triggered_by={triggered_by})")

            # Lưu timestamp chụp gần nhất và trạng thái để EC25 đồng bộ
            self._save_ec25_state(
                last_capture_ts=taken_at,
                last_capture_triggered_by=triggered_by,
                capture_success=bool(captured_files),
            )

            # Publish MQTT thông báo EC25 biết CM4 đã hoàn thành chu kỳ chụp
            if triggered_by == "schedule" and self.mqtt_client and self.mqtt_client.is_connected():
                try:
                    self.mqtt_client.publish(
                        self.t_status,
                        json.dumps({
                            "online": True,
                            "node": "cm4",
                            "event": "cycle_capture_done",
                            "triggered_by": triggered_by,
                            "taken_at": taken_at,
                            "media_count": len(media_ids),
                        }),
                        qos=1,
                        retain=False,
                    )
                    log.info("📡 [CYCLE-EC25] Đã publish cycle_capture_done → EC25 nhận tín hiệu.")
                except Exception as e:
                    log.warning("Không publish cycle_capture_done: %s", e)

            # Trì hoãn 20s sau khi chụp xong rồi mới tắt nguồn để gphoto2 giải phóng hoàn toàn và máy ảnh ổn định
            if not self.live_session_id and not self.always_keep_power:
                if self.capture_interval_sec == 0 or self.capture_interval_sec > 15:
                    self._schedule_delayed_camera_off(delay=20.0)

            return media_ids
        finally:
            self._is_capturing = False

    def _normalize_image_bytes(self, raw_bytes, preview_bytes=None):
        """Đảm bảo dữ liệu ảnh là JPEG hợp lệ với độ phân giải đầy đủ."""
        # 1. Thử decode ảnh gốc (full resolution)
        if raw_bytes:
            try:
                with Image.open(io.BytesIO(raw_bytes)) as im:
                    w, h = im.size
                    if im.format == "JPEG" and w >= 600 and h >= 400:
                        return raw_bytes, w, h
                    buf = io.BytesIO()
                    rgb = im.convert("RGB")
                    rgb.save(buf, "JPEG", quality=92)
                    return buf.getvalue(), rgb.size[0], rgb.size[1]
            except Exception as e_raw:
                log.debug("Ảnh raw_bytes không decode trực tiếp được (%s), thử preview_bytes...", e_raw)

        # 2. Fallback sang preview_bytes nếu raw_bytes là file định dạng RAW không decode được
        if preview_bytes:
            try:
                with Image.open(io.BytesIO(preview_bytes)) as im:
                    pw, ph = im.size
                    if im.format == "JPEG" and pw >= 600 and ph >= 400:
                        return preview_bytes, pw, ph
                    buf = io.BytesIO()
                    rgb = im.convert("RGB")
                    rgb.save(buf, "JPEG", quality=90)
                    return buf.getvalue(), rgb.size[0], rgb.size[1]
            except Exception as e_pv:
                log.debug("Lỗi decode preview_bytes: %s", e_pv)

        return raw_bytes, 1920, 1080

    def publish_telemetry(self):
        if not self.mqtt_client or not self.mqtt_client.is_connected():
            return
        try:
            payload = collect_telemetry(
                camera_code=self.code,
                is_powered=self.power_manager.is_powered,
                use_real_hw=self.backend.use_real_hardware,
                firmware_version=FIRMWARE_VERSION,
                camera_info=self.backend._detected_camera_info,
            )
            payload["threads"] = self.watchdog.status_report()

            self.mqtt_client.publish(self.t_data, json.dumps(payload), qos=1)
            hum_str = f" Hum:{payload['humidity_percent']}%" if payload.get("humidity_percent") is not None else ""
            bat_str = f" Bat:{payload['battery_voltage']}V({payload.get('battery_percent', 0)}%)" if payload.get("battery_voltage") is not None else ""
            sol_str = f" Sol:{payload['solar_voltage']}V" if payload.get("solar_voltage") is not None else ""
            chg_str = "⚡" if payload.get("is_charging") else ""

            log.info("📡 Telemetry [CM4]: %.1f°C%s CPU%.0f%% RAM%.0f%%%s%s%s Signal:%ddBm[%s] CamPwr:%s Mode:%s",
                     payload["temperature_c"], hum_str, payload["cpu_percent"],
                     payload["memory_percent"], bat_str, sol_str, chg_str,
                     payload["sim_signal_dbm"], payload["sim_source"],
                     payload["camera_gpio_power"], payload["camera_hw_mode"])
        except Exception as e:
            log.warning("Lỗi publish Telemetry: %s", e)

    def shutdown_host_cm4(self, delay=None):
        """Tắt nguồn toàn bộ hệ điều hành Host CM4 an toàn từ bên trong Docker Container."""
        delay = delay if delay is not None else self.shutdown_delay_sec
        log.info("🔌 [HOST POWEROFF] Đang chuẩn bị tắt nguồn hệ điều hành CM4 sau %.1fs...", delay)

        def _do_shutdown():
            time.sleep(delay)
            log.info("🔌 [HOST POWEROFF] Đang thực hiện tắt máy an toàn (sync + SysRq poweroff)...")
            try:
                if os.path.exists("/proc/sysrq-trigger"):
                    with open("/proc/sysrq-trigger", "w") as f:
                        f.write("s")
                    time.sleep(0.5)
                    with open("/proc/sysrq-trigger", "w") as f:
                        f.write("u")
                    time.sleep(0.5)
                    with open("/proc/sysrq-trigger", "w") as f:
                        f.write("o")
            except Exception as e:
                log.warning("SysRq trigger error: %s", e)

            os.system('dbus-send --system --print-reply --dest=org.freedesktop.login1 /org/freedesktop/login1 "org.freedesktop.login1.Manager.PowerOff" boolean:true 2>/dev/null')
            os.system("poweroff 2>/dev/null || shutdown -h now 2>/dev/null")

        threading.Thread(target=_do_shutdown, name="host_shutdown_thread", daemon=True).start()

    def _cancel_delayed_camera_off(self):
        """Hủy bộ hẹn giờ tắt nguồn nếu có lệnh chụp hoặc hoạt động mới."""
        with self._power_timer_lock:
            if self._power_off_timer and self._power_off_timer.is_alive():
                self._power_off_timer.cancel()
                self._power_off_timer = None
                log.info("⏹️ [CAMERA POWER] Đã hủy hẹn giờ tắt nguồn máy ảnh do có hoạt động mới.")

    def _schedule_delayed_camera_off(self, delay=20.0):
        """Lên lịch tắt nguồn máy ảnh sau `delay` giây (mặc định 20s), có thể hủy nếu có chụp mới hoặc liveview."""
        with self._power_timer_lock:
            if self._power_off_timer and self._power_off_timer.is_alive():
                self._power_off_timer.cancel()

            def _do_off():
                with self._power_timer_lock:
                    if self._is_capturing or self.live_session_id or self.always_keep_power:
                        log.info("🛑 [DELAYED-OFF] Bỏ qua tắt nguồn (đang chụp / liveview / always_keep_power)")
                        return
                    log.info("🔌 [DELAYED-OFF] Đã qua %.0fs sau chụp -> Ngắt kết nối USB & Tắt nguồn máy ảnh", delay)
                    self.backend.disconnect_real_camera()
                    self.power_manager.power_off()

            self._power_off_timer = threading.Timer(delay, _do_off)
            self._power_off_timer.name = "delayed_camera_off_timer"
            self._power_off_timer.daemon = True
            self._power_off_timer.start()
            log.info("⏱️ [CAMERA POWER] Sẽ tắt nguồn máy ảnh sau %.0f giây nếu không có lệnh chụp mới...", delay)

    def _ensure_camera_ready(self):
        """Tự động BẬT NGUỒN GPIO 16 và kết nối USB máy ảnh thật khi người dùng tương tác từ Web UI."""
        self._cancel_delayed_camera_off()
        was_off = not self.power_manager.is_powered
        if was_off:
            log.info("🔌 Máy ảnh đang TẮT — Tự động BẬT NGUỒN (GPIO 16) để thực hiện lệnh...")
            self.power_manager.power_on()
            time.sleep(self.power_manager.warmup_delay)

        if not self.backend.use_real_hardware:
            log.info("🔍 Thử khởi tạo lại kết nối máy ảnh thật qua gphoto2 (USB)...")
            self.backend._try_init_real_camera()

    def process_command(self, req):
        cmd     = req.get("command", "")
        rid     = req.get("request_id", "")
        payload = req.get("payload") or {}
        log.info("📥 Nhận lệnh MQTT: %s (req_id=%s)", cmd, rid)

        try:
            if cmd in ("power_on_cm4", "power_on", "set_interactive_mode"):
                self.operating_mode = "interactive"
                self.power_manager.power_on()
                if not self.backend.use_real_hardware:
                    time.sleep(self.power_manager.warmup_delay)
                    self.backend._try_init_real_camera()
                resp = {"type": cmd, "request_id": rid, "status": "ok",
                        "data": {"cm4_power_state": "running", "mode": "interactive", "camera_power": "on", "message": "CM4 is online and running in interactive mode"}}
                log.info("🎮 [MODE] Nhận lệnh %s từ Web UI -> Kích hoạt Chế độ Tương Tác (Bỏ qua Auto-Shutdown)", cmd)
                self.publish_telemetry()

            elif cmd in ("power_off_cm4", "power_off", "shutdown_cm4", "shutdown_host"):
                self.power_manager.power_off()
                resp = {"type": cmd, "request_id": rid, "status": "ok",
                        "data": {"cm4_power_state": "shutting_down", "camera_power": "off", "message": "CM4 shutdown in progress"}}
                log.info("🔴 [SHUTDOWN] Nhận lệnh tắt nguồn từ Web UI -> Đang tắt hệ điều hành CM4...")
                self.shutdown_host_cm4(delay=2.0)

            elif cmd == "power_off_camera":
                self._cancel_delayed_camera_off()
                self.backend.disconnect_real_camera()
                self.power_manager.power_off()
                resp = {"type": cmd, "request_id": rid, "status": "ok",
                        "data": {"camera_power": "off", "message": "Camera powered off"}}

            elif cmd == "set_settings":
                self._ensure_camera_ready()
                applied, caps, mismatches = self.backend.set_settings(payload)
                resp = {"type": cmd, "request_id": rid, "status": "ok",
                        "data": {"requested": payload, "applied": applied,
                                 "capabilities": caps, "mismatches": mismatches,
                                 "camera_power": "on" if self.power_manager.is_powered else "off"}}

            elif cmd in ("get_settings", "get_capabilities", "get_status"):
                if cmd in ("get_settings", "get_capabilities"):
                    self._ensure_camera_ready()

                applied, caps = self.backend.get_settings()
                resp = {"type": cmd, "request_id": rid, "status": "ok",
                        "data": {"online": True, "applied": applied, "capabilities": caps,
                                 "live_view": bool(self.live_session_id),
                                 "camera_power": "on" if self.power_manager.is_powered else "off",
                                 "threads": self.watchdog.status_report()}}

            elif cmd == "get_sim_info":
                sim = get_sim_info(force=True)
                resp = {"type": cmd, "request_id": rid, "status": "ok",
                        "data": {"sim": sim}}

            elif cmd in ("capture_now", "capture"):
                media_ids = self.upload_capture(triggered_by="mqtt")
                if media_ids:
                    resp = {"type": cmd, "request_id": rid, "status": "ok",
                            "data": {"media_id": media_ids[0], "media_ids": media_ids}}
                else:
                    resp = {"type": cmd, "request_id": rid, "status": "ok",
                            "data": {"note": "Ảnh đã lưu vào Offline Queue"}}

            elif cmd == "set_interval":
                val = max(0, int(payload.get("capture_interval_sec", self.capture_interval_sec)))
                self.capture_interval_sec = val
                if "schedules" in payload:
                    self.schedule_rules = payload["schedules"]
                self.save_schedule_config()
                log.info("⏱ Chu kỳ chụp: %d giây", val)
                resp = {"type": cmd, "request_id": rid, "status": "ok",
                        "data": {"capture_interval_sec": val}}

            elif cmd == "set_schedules":
                schedules = payload.get("schedules") or []
                self.schedule_rules = schedules
                self.save_schedule_config()
                log.info("🗓 [SCHEDULE] Đã nhận %d khung giờ chụp từ server", len(schedules))
                resp = {"type": cmd, "request_id": rid, "status": "ok",
                        "data": {"count": len(schedules)}}

            elif cmd == "start_live_view":
                self._ensure_camera_ready()
                self.live_session_id = payload.get("session_id") or "lv-cm4"
                self.live_fps = max(1, min(2, int(payload.get("fps") or 1)))
                self.live_seq = 0
                resp = {"type": cmd, "request_id": rid, "status": "ok",
                        "data": {"live_view": True, "session_id": self.live_session_id,
                                 "fps": self.live_fps}}

            elif cmd == "stop_live_view":
                log.info("🛑 [LIVE VIEW] Dừng luồng Live View...")
                self.live_session_id = None
                if self.backend:
                    self.backend.end_live_view()
                if not self.always_keep_power and (self.capture_interval_sec == 0 or self.capture_interval_sec > 15):
                    self._schedule_delayed_camera_off(delay=20.0)
                resp = {"type": cmd, "request_id": rid, "status": "ok",
                        "data": {"live_view": False}}

            elif cmd == "get_watchdog_status":
                resp = {"type": cmd, "request_id": rid, "status": "ok",
                        "data": {"threads": self.watchdog.status_report()}}

            else:
                resp = {"type": cmd, "request_id": rid, "status": "error",
                        "data": {"note": f"Lệnh chưa hỗ trợ: {cmd}"}}

        except Exception as exc:
            log.exception("Lỗi xử lý lệnh %s: %s", cmd, exc)
            resp = {"type": cmd, "request_id": rid, "status": "error",
                    "data": {"note": str(exc)}}

        if self.mqtt_client and self.mqtt_client.is_connected():
            self.mqtt_client.publish(self.t_ack, json.dumps(resp), qos=1)

    # ── Worker Thread Functions (đăng ký với Watchdog) ────────────────────────

    def _fn_live_view(self):
        while self.running:
            self.watchdog.touch("liveview")
            if not self.live_session_id or getattr(self, "_is_capturing", False):
                time.sleep(0.5)
                continue
            self.live_seq += 1
            try:
                frame = self.backend.preview()
                st, resp = self._http_post_frame(self.live_session_id, self.live_seq, frame)
                if st == 200 and resp.get("ok"):
                    log.debug("Live frame seq=%d (%d bytes)", self.live_seq, len(frame))
            except Exception as e:
                log.warning("Lỗi stream live view: %s", e)
            time.sleep(max(0.5, 1.0 / max(1, self.live_fps)))

    def _fn_capture_loop(self):
        while self.running:
            self.watchdog.touch("capture_loop")
            is_active, interval_sec, slot_name = self.get_active_schedule_slot()
            if not is_active or interval_sec <= 0:
                for _ in range(15):
                    if not self.running:
                        break
                    self.watchdog.touch("capture_loop")
                    time.sleep(1)
                continue

            wait_sec = self.get_seconds_to_next_aligned_slot(interval_sec)
            target_epoch = time.time() + wait_sec
            log.info("⏱ [%s] Chụp kế tiếp sau %.1f giây (chu kỳ %ds, căn chuẩn mốc phút chẵn)", slot_name, wait_sec, interval_sec)

            # ── Set missed_capture_flag trước khi bắt đầu chờ ────────────────
            self._set_missed_capture_flag(True, f"bắt đầu chờ mốc {slot_name} (wait={wait_sec:.0f}s)")

            while self.running:
                remaining = target_epoch - time.time()
                if remaining <= 0:
                    break
                self.watchdog.touch("capture_loop")
                time.sleep(min(1.0, max(0.05, remaining)))
                curr_active, curr_interval, _ = self.get_active_schedule_slot()
                if not curr_active or curr_interval != interval_sec:
                    break

            curr_active, _, _ = self.get_active_schedule_slot()
            if self.running and curr_active:
                try:
                    log.info("🔔 [%s] Bắt đầu chu kỳ chụp tự động (đúng mốc phút chẵn)...", slot_name)
                    self.upload_capture(triggered_by="schedule")
                    if self.auto_shutdown_after_capture and self.operating_mode != "interactive":
                        self.publish_telemetry()
                        self.shutdown_host_cm4()
                except Exception:
                    log.exception("Lỗi chu kỳ chụp tự động")
                    # Nếu lỗi, giữ nguyên flag=True để lần boot sau biết bị missed

    def _fn_offline_retry(self):
        while self.running:
            self.watchdog.touch("offline_retry")
            time.sleep(self.offline_retry_interval)
            if self.running:
                try:
                    self.offline_queue.process_pending_queue(self._do_upload_to_server)
                except Exception as e:
                    log.error("Lỗi offline retry: %s", e)

    def _fn_cmd_worker(self):
        while self.running:
            self.watchdog.touch("cmd_worker")
            try:
                raw = self.cmd_queue.get(timeout=1)
                self.watchdog.touch("cmd_worker")
                self.process_command(raw)
            except _queue.Empty:
                continue
            except Exception as exc:
                log.error("cmd_worker error: %s", exc)

    # ── MQTT Setup ────────────────────────────────────────────────────────────

    def _setup_mqtt(self):
        def on_connect(client, userdata, flags, rc, props=None):
            if rc == 0:
                log.info("✅ MQTT Kết nối OK!")
                client.subscribe(self.t_cmd, qos=1)
                client.publish(self.t_status, json.dumps({"online": True, "node": "cm4", "cm4_power_state": "running"}), qos=1, retain=True)
                self.publish_telemetry()
                threading.Thread(
                    target=self.offline_queue.process_pending_queue,
                    args=(self._do_upload_to_server,),
                    daemon=True
                ).start()
            else:
                log.error("❌ MQTT kết nối thất bại rc=%s", rc)

        def on_message(client, userdata, msg):
            try:
                payload_str = msg.payload.decode()
                log.info("📩 [MQTT RECV] Nhận packet trên topic '%s': %s", msg.topic, payload_str[:120])
                raw = json.loads(payload_str)
                self.cmd_queue.put(raw)
            except Exception as exc:
                log.error("Lỗi giải mã MQTT message: %s", exc)

        def on_disconnect(client, userdata, flags, rc, props=None):
            log.warning("⚠️ MQTT mất kết nối (rc=%s). Paho sẽ tự reconnect...", rc)

        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"{self.code}_agent")
        client.username_pw_set(self.code, self.password)
        client.will_set(self.t_status, json.dumps({"online": False}), qos=1, retain=True)
        client.reconnect_delay_set(min_delay=2, max_delay=30)
        client.on_connect    = on_connect
        client.on_message    = on_message
        client.on_disconnect = on_disconnect

        self.mqtt_client = client

        while self.running:
            try:
                client.connect(self.broker, self.port, keepalive=60)
                client.loop_start()
                return
            except Exception as e:
                log.error("Chưa thể kết nối MQTT %s:%d (%s). Thử lại sau 5s...",
                          self.broker, self.port, e)
                time.sleep(5)

    # ── Start / Stop ──────────────────────────────────────────────────────────

    def start(self):
        self.running = True
        log.info("=" * 60)
        log.info("🚀 AUTOTIMELAPSE CM4 AGENT [%s]", FIRMWARE_VERSION)
        log.info("📷 Camera: %s | MQTT: %s:%d", self.code, self.broker, self.port)
        log.info("🌐 Server: %s", self.server_base)
        log.info("⚡ GPIO Power Pin %d | Warmup %.1fs | Keep=%s",
                 self.power_manager.pin, self.power_manager.warmup_delay,
                 self.always_keep_power)
        log.info("🔁 Upload Retry %dx delay %.1fs | Offline Retry %ds",
                 MAX_UPLOAD_RETRIES, UPLOAD_RETRY_DELAY, self.offline_retry_interval)
        log.info("📁 Offline Queue: %s", self.offline_queue.queue_dir)
        log.info("🔋 Power Save Mode: Auto-Boot-Capture=%s | Auto-Shutdown=%s (delay=%.1fs)",
                 self.auto_capture_on_boot, self.auto_shutdown_after_capture, self.shutdown_delay_sec)
        log.info("=" * 60)

        self.watchdog.register(ManagedThread(
            name="liveview", target_fn=self._fn_live_view,
            restart_on_crash=True, heartbeat_timeout=0
        ))
        self.watchdog.register(ManagedThread(
            name="capture_loop", target_fn=self._fn_capture_loop,
            restart_on_crash=True, heartbeat_timeout=0
        ))
        self.watchdog.register(ManagedThread(
            name="offline_retry", target_fn=self._fn_offline_retry,
            restart_on_crash=True, heartbeat_timeout=0
        ))
        self.watchdog.register(ManagedThread(
            name="cmd_worker", target_fn=self._fn_cmd_worker,
            restart_on_crash=True, heartbeat_timeout=0   # lệnh capture/upload có thể mất 30-60s, không dùng heartbeat timeout
        ))

        self.watchdog.start(lambda: self.running)
        self._setup_mqtt()

        # ── Smart Boot Manager (Tự động nhận diện Chụp Định Kỳ EC25 vs Cưỡng Bức Bật Web UI) ──
        self.operating_mode = "pending"  # "pending" | "interactive" | "auto_schedule"

        def _smart_boot_task():
            log.info("⏳ [SMART BOOT] CM4 khởi động: Đang đồng bộ cấu hình từ Server & kiểm tra trạng thái...")

            # ── BƯỚC 1: Kéo cấu hình và trạng thái force_power_on từ Server ──
            sync_ok, force_on = self.pull_server_config()

            # ── BƯỚC 2: Phát hiện chế độ CƯỠNG BỨC BẬT ─────────────────────
            # Ưu tiên cao nhất: server nói force_on=True → giữ CM4 online hoàn toàn
            # (LƯU Ý: Nguồn máy ảnh GPIO 16 vẫn giữ TẮT, chỉ BẬT khi chụp, liveview, hoặc chỉnh thông số)
            if force_on:
                self.operating_mode = "interactive"
                log.info("🎮 [FORCE-ON] Phát hiện CƯỠNG BỨC BẬT từ Server (force_power_on=True) → CM4 GIỮ ONLINE LIÊN TỤC (Máy ảnh giữ TẮT chờ lệnh).")
                # Xoá missed_capture_flag vì chế độ interactive do người dùng trực tiếp điều khiển
                if self.missed_capture_flag:
                    self._set_missed_capture_flag(False, "chế độ cưỡng bức, không áp dụng chu kỳ")
                # Cập nhật EC25 state: force_on=True để EC25 biết không được cắt nguồn
                self._save_ec25_state(force_power_on=True)
                self.publish_telemetry()
                return

            # Server xác nhận KHÔNG cưỡng bức → cập nhật EC25 state
            self._save_ec25_state(force_power_on=False)

            # ── BƯỚC 3: Chờ ngắn để nhận lệnh MQTT tức thì nếu có ────────────
            # (3 giây – đủ nhận lệnh power_on_cm4 từ Web UI gửi ngay khi detect CM4 online)
            for _ in range(30):
                if not self.running:
                    return
                if self.operating_mode == "interactive":
                    log.info("🎮 [FORCE-ON] Nhận lệnh tương tác qua MQTT khi khởi động → CM4 GIỮ ONLINE.")
                    # Cập nhật EC25 state: force_on=True
                    self._save_ec25_state(force_power_on=True)
                    return
                time.sleep(0.1)

            # ── BƯỚC 4: Xử lý Chế độ Chu Kỳ EC25 (Auto-Schedule) ─────────────
            self.operating_mode = "auto_schedule"
            is_active, interval_sec, slot_name = self.get_active_schedule_slot()

            log.info("⏰ [CYCLE-EC25] Khởi động theo chu kỳ. Lịch hiện tại: [%s] active=%s, interval=%ds (capture_loop sẽ chụp đúng mốc phút chẵn)",
                     slot_name, is_active, interval_sec)
            self.publish_telemetry()

        threading.Thread(target=_smart_boot_task, name="smart_boot_manager", daemon=True).start()

        last_telemetry = time.time()
        try:
            while self.running:
                time.sleep(1)
                if time.time() - last_telemetry >= self.telemetry_interval:
                    self.publish_telemetry()
                    last_telemetry = time.time()
        except KeyboardInterrupt:
            log.info("⏹ Nhận KeyboardInterrupt. Đang dừng...")
        finally:
            self.stop()

    def stop(self):
        if not self.running:
            return
        log.info("🛑 Đang dừng Agent...")
        self.running = False
        self.watchdog.stop()

        if self.mqtt_client:
            try:
                self.mqtt_client.publish(self.t_status,
                                         json.dumps({"online": False}), qos=1, retain=True)
                self.mqtt_client.loop_stop()
                self.mqtt_client.disconnect()
            except Exception:
                pass

        self.power_manager.cleanup()
        try:
            from solar_fan_controller import get_solar_fan_controller
            get_solar_fan_controller().cleanup()
        except Exception:
            pass
        log.info("👋 Agent đã dừng hoàn toàn.")


# ── MAIN ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AutoTimelapse CM4 Camera Agent")
    parser.add_argument("--code",        default=CAMERA_CODE,
                        help=f"Mã Camera [Mặc định: {CAMERA_CODE}]")
    parser.add_argument("--secret",      default=MQTT_PASSWORD,
                        help="Mật khẩu thiết bị / MQTT Password")
    parser.add_argument("--broker",      default=MQTT_BROKER,
                        help=f"MQTT Broker host [Mặc định: {MQTT_BROKER}]")
    parser.add_argument("--port",        type=int, default=MQTT_PORT,
                        help=f"MQTT Broker port [Mặc định: {MQTT_PORT}]")
    parser.add_argument("--server",      default=SERVER_BASE,
                        help=f"Server Base URL [Mặc định: {SERVER_BASE}]")
    parser.add_argument("--power-gpio",  type=int, default=POWER_GPIO_PIN,
                        help=f"GPIO Pin điều khiển nguồn [Mặc định: {POWER_GPIO_PIN}]")
    parser.add_argument("--warmup",      type=float, default=WARMUP_DELAY_SEC,
                        help=f"Delay warmup máy ảnh (s) [Mặc định: {WARMUP_DELAY_SEC}]")
    parser.add_argument("--offline-dir", default=OFFLINE_QUEUE_DIR,
                        help=f"Thư mục offline queue [Mặc định: {OFFLINE_QUEUE_DIR}]")

    args = parser.parse_args()

    agent = CameraAgent(
        code=args.code,
        password=args.secret,
        broker=args.broker,
        port=args.port,
        server_base=args.server,
        power_pin=args.power_gpio,
        power_active_high=POWER_ACTIVE_HIGH,
        warmup_delay=args.warmup,
        always_keep_power=ALWAYS_KEEP_POWER,
        telemetry_interval=TELEMETRY_INTERVAL,
        offline_dir=args.offline_dir,
        offline_retry_interval=OFFLINE_RETRY_INTERVAL,
    )

    def _sig_handler(sig, frame):
        agent.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, _sig_handler)
    signal.signal(signal.SIGTERM, _sig_handler)

    agent.start()
