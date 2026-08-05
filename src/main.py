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
from datetime import datetime, timezone

from PIL import Image

from config import (
    CAMERA_CODE, MQTT_PASSWORD, MQTT_BROKER, MQTT_PORT, SERVER_BASE,
    POWER_GPIO_PIN, POWER_ACTIVE_HIGH, WARMUP_DELAY_SEC, ALWAYS_KEEP_POWER,
    TELEMETRY_INTERVAL, OFFLINE_QUEUE_DIR, OFFLINE_RETRY_INTERVAL,
    MAX_UPLOAD_RETRIES, UPLOAD_RETRY_DELAY, SIM_INFO_TELEMETRY
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


class CameraAgent:
    """Quản lý luồng hoạt động chính của Camera Agent trên CM4."""

    def __init__(self, code, password, broker, port, server_base,
                 power_pin=16, power_active_high=True, warmup_delay=3.0,
                 always_keep_power=False, telemetry_interval=30,
                 offline_dir="/app/offline_queue", offline_retry_interval=60):
        self.code = code
        self.password = password
        self.broker = broker
        self.port = port
        self.server_base = server_base.rstrip("/")
        self.always_keep_power = always_keep_power
        self.telemetry_interval = telemetry_interval
        self.offline_retry_interval = offline_retry_interval

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

        self.t_cmd    = f"camera/{self.code}/cmd"
        self.t_ack    = f"camera/{self.code}/ack"
        self.t_data   = f"camera/{self.code}/data"
        self.t_status = f"camera/{self.code}/status"

    # ── HTTP Helpers ──────────────────────────────────────────────────────────

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
        with urllib.request.urlopen(req, timeout=15) as r:
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
        req = urllib.request.Request(url, data=data, method="PUT",
                                     headers={
                                         "Content-Type": content_type,
                                         "User-Agent": USER_AGENT,
                                     })
        with urllib.request.urlopen(req, timeout=30) as r:
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

            self._http_put(pre["url"], final_bytes, content_type)
            if thumb_bytes and "thumb_url" in pre:
                self._http_put(pre["thumb_url"], thumb_bytes, "image/jpeg")

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

    def upload_capture(self):
        """Thực hiện chu trình chụp đầy đủ:
        GPIO ON → Capture → Retry 3x upload → Offline Queue → Power Management.
        """
        self.power_manager.power_on()

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

        if not self.live_session_id and not self.always_keep_power:
            if self.capture_interval_sec == 0 or self.capture_interval_sec > 15:
                self.power_manager.power_off()

        return media_ids

    def _normalize_image_bytes(self, raw_bytes, preview_bytes=None):
        if preview_bytes:
            try:
                with Image.open(io.BytesIO(preview_bytes)) as im:
                    pw, ph = im.size
                    if im.format == "JPEG" and pw >= 600 and ph >= 400:
                        return preview_bytes, pw, ph
            except Exception:
                pass
        try:
            with Image.open(io.BytesIO(raw_bytes)) as im:
                w, h = im.size
                if im.format == "JPEG" and w >= 600 and h >= 400:
                    return raw_bytes, w, h
                buf = io.BytesIO()
                rgb = im.convert("RGB")
                rgb.save(buf, "JPEG", quality=90)
                return buf.getvalue(), rgb.size[0], rgb.size[1]
        except Exception as e:
            log.warning("Không decode được ảnh (%s), giữ nguyên.", e)
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
            )
            payload["threads"] = self.watchdog.status_report()

            self.mqtt_client.publish(self.t_data, json.dumps(payload), qos=1)
            log.info("📡 Telemetry [CM4]: %.1f°C CPU%.0f%% RAM%.0f%% Signal:%ddBm[%s] CamPwr:%s Mode:%s",
                     payload["temperature_c"], payload["cpu_percent"],
                     payload["memory_percent"], payload["sim_signal_dbm"],
                     payload["sim_source"],
                     payload["camera_gpio_power"],
                     payload["camera_hw_mode"])
        except Exception as e:
            log.warning("Lỗi publish Telemetry: %s", e)

    def process_command(self, req):
        cmd     = req.get("command", "")
        rid     = req.get("request_id", "")
        payload = req.get("payload") or {}
        log.info("📥 Nhận lệnh MQTT: %s (req_id=%s)", cmd, rid)

        try:
            if cmd in ("power_on_cm4", "power_on"):
                self.power_manager.power_on()
                resp = {"type": cmd, "request_id": rid, "status": "ok",
                        "data": {"cm4_power_state": "running", "camera_power": "on"}}

            elif cmd in ("power_off_camera", "power_off"):
                self.power_manager.power_off()
                resp = {"type": cmd, "request_id": rid, "status": "ok",
                        "data": {"camera_power": "off"}}

            elif cmd == "set_settings":
                if not self.power_manager.is_powered:
                    self.power_manager.power_on()
                applied, caps, mismatches = self.backend.set_settings(payload)
                resp = {"type": cmd, "request_id": rid, "status": "ok",
                        "data": {"requested": payload, "applied": applied,
                                 "capabilities": caps, "mismatches": mismatches}}

            elif cmd in ("get_settings", "get_capabilities", "get_status"):
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
                media_ids = self.upload_capture()
                if media_ids:
                    resp = {"type": cmd, "request_id": rid, "status": "ok",
                            "data": {"media_id": media_ids[0], "media_ids": media_ids}}
                else:
                    resp = {"type": cmd, "request_id": rid, "status": "ok",
                            "data": {"note": "Ảnh đã lưu vào Offline Queue"}}

            elif cmd == "set_interval":
                val = max(0, int(payload.get("capture_interval_sec", self.capture_interval_sec)))
                self.capture_interval_sec = val
                log.info("⏱ Chu kỳ chụp: %d giây", val)
                resp = {"type": cmd, "request_id": rid, "status": "ok",
                        "data": {"capture_interval_sec": val}}

            elif cmd == "start_live_view":
                self.power_manager.power_on()
                self.live_session_id = payload.get("session_id") or "lv-cm4"
                self.live_fps = max(1, min(2, int(payload.get("fps") or 1)))
                self.live_seq = 0
                resp = {"type": cmd, "request_id": rid, "status": "ok",
                        "data": {"live_view": True, "session_id": self.live_session_id,
                                 "fps": self.live_fps}}

            elif cmd == "stop_live_view":
                self.live_session_id = None
                if not self.always_keep_power and self.capture_interval_sec == 0:
                    self.power_manager.power_off()
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
            if not self.live_session_id:
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
            wait = self.capture_interval_sec
            if wait <= 0:
                time.sleep(1)
                continue

            log.info("⏱ Chụp kế tiếp sau %d giây", wait)
            elapsed = 0
            while elapsed < wait and self.running:
                self.watchdog.touch("capture_loop")
                time.sleep(1)
                elapsed += 1
                if self.capture_interval_sec != wait:
                    break

            if self.running and self.capture_interval_sec == wait:
                try:
                    log.info("🔔 Bắt đầu chu kỳ chụp tự động...")
                    self.upload_capture()
                except Exception:
                    log.exception("Lỗi chu kỳ chụp tự động")

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
                client.publish(self.t_status, json.dumps({"online": True}), qos=1, retain=True)
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
                raw = json.loads(msg.payload.decode())
                self.cmd_queue.put(raw)
            except Exception as exc:
                log.error("Lỗi giải mã MQTT message: %s", exc)

        def on_disconnect(client, userdata, flags, rc, props=None):
            log.warning("⚠️ MQTT mất kết nối (rc=%s). Paho sẽ tự reconnect...", rc)

        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=self.code)
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
            restart_on_crash=True, heartbeat_timeout=120
        ))

        self.watchdog.start(lambda: self.running)
        self._setup_mqtt()

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
