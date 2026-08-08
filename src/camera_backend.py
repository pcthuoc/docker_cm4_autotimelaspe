#!/usr/bin/env python3
"""
AutoTimelapse CM4 Agent - Module Camera Backend
------------------------------------------------------------------
Quản lý giao tiếp trực tiếp với Máy ảnh qua USB (python-gphoto2).
Tích hợp tự động Reset cổng USB khi gặp lỗi kẹt device (-60 / -1),
Khởi động lại nguồn GPIO 16 (Hard Cycle Power) nếu kẹt nặng,
và Fallback sang giả lập ảnh bằng PIL nếu không cắm phần cứng.
"""

import io
import time
import logging
import threading
from datetime import datetime
from PIL import Image, ImageDraw

from config import SETTING_SPECS, MAX_CAMERA_RETRIES, FORCE_REAL_ONLY
from power_manager import CameraPowerManager
from usb_utils import reset_all_camera_usb_devices

log = logging.getLogger("cm4_camera_backend")

GPHOTO2_AVAILABLE = False
try:
    import gphoto2 as gp
    GPHOTO2_AVAILABLE = True
except ImportError:
    gp = None


class HybridCameraBackend:
    """Quản lý kết nối máy ảnh thật qua USB gphoto2 kết hợp Fallback Giả lập PIL."""

    def __init__(self, power_manager: CameraPowerManager):
        self._lock = threading.Lock()
        self._camera = None
        self.use_real_hardware = False
        self.power_manager = power_manager

        self._sim_applied = {
            "iso": "100", "aperture": "f/4", "shutter_speed": "1/200",
            "exposure_compensation": "0.0", "white_balance": "Auto",
            "image_format": "JPEG Fine", "image_size": "6000x4000",
            "focus_mode": "AF-S", "autofocus": "On", "capture_mode": "Single Shot",
            "capture_target": "Memory Card", "high_iso_nr": "Off",
            "long_exp_nr": "Off", "liveview_af": "Normal Area",
            "exposure_mode": "Manual", "focus_switch": "AF",
        }

    def _try_init_real_camera(self):
        """
        Kết nối máy ảnh thật qua USB gphoto2.
        Phân biệt 2 loại lỗi:
          - [-105] Unknown model: Máy ảnh đang boot, USB đã detect nhưng chưa xong → CHỜ THÊM (không reset USB)
          - [-60] I/O error / [-52] Not found: Lỗi USB thật → Reset USB ioctl → Hard Power Cycle
        """
        with self._lock:
            if self._camera is not None:
                return True

            if not GPHOTO2_AVAILABLE:
                self.use_real_hardware = False
                return False

            # Với lỗi -105 (timing): tối đa 10 lần × 2s = 20s polling sau warmup
            # Tổng: WARMUP_DELAY_SEC (10s) + 20s polling = tối đa 30s, đảm bảo Nikon boot xong
            max_attempts = max(MAX_CAMERA_RETRIES, 10)

            for attempt in range(1, max_attempts + 1):
                try:
                    cam = gp.Camera()
                    cam.init()
                    self._camera = cam
                    self.use_real_hardware = True

                    try:
                        config = cam.get_config()
                        try:
                            q = config.get_child_by_name("imagequality")
                            q.set_value("JPEG Fine")
                            cam.set_config(config)
                        except Exception:
                            pass
                    except Exception:
                        pass

                    summary = cam.get_summary()
                    first_line = str(summary).split('\n')[0] if summary else "gphoto2 USB Device"
                    log.info("📷 [USB SUCCESS] Kết nối MÁY ẢNH THẬT thành công sau %d lần thử! (%s)",
                             attempt, first_line)
                    return True

                except Exception as e:
                    err_str = str(e)
                    self._camera = None
                    self.use_real_hardware = False

                    # Phân biệt loại lỗi để xử lý đúng
                    is_timing_error = "[-105]" in err_str or "Unknown model" in err_str
                    is_io_error = any(x in err_str for x in ["[-7]", "[-60]", "[-52]", "I/O", "not found", "Not found"])

                    if is_timing_error:
                        # Máy ảnh đang boot, USB detect rồi nhưng chưa enum xong — CHỜ THÊM
                        log.info("⏳ [CAMERA BOOT] Máy ảnh đang khởi động (lần %d/%d), chờ thêm 2s...",
                                 attempt, max_attempts)
                        time.sleep(2.0)

                    elif is_io_error:
                        # Lỗi USB thật (device bị lock/treo) → reset USB
                        log.warning("⚠️ [USB ERROR] Lỗi cổng USB (lần %d/%d): %s — Reset USB...",
                                    attempt, MAX_CAMERA_RETRIES, e)
                        if attempt < MAX_CAMERA_RETRIES:
                            reset_all_camera_usb_devices()
                            time.sleep(1.5)
                        if attempt == MAX_CAMERA_RETRIES:
                            log.warning("🔌 USB kẹt nặng → Hard Power Cycle GPIO 16...")
                            self.power_manager.hard_cycle_power()
                            time.sleep(2.0)

                    else:
                        # Lỗi khác không xác định
                        log.warning("⚠️ Lỗi khởi tạo gphoto2 (lần %d/%d): %s",
                                    attempt, max_attempts, e)
                        time.sleep(1.5)

                    if attempt >= max_attempts:
                        break

            log.info("ℹ️ Không thể kết nối máy ảnh USB gphoto2 — Tự động chuyển sang Chế độ Giả Lập Ảnh (PIL).")
            return False

    def _wait_until_camera_ready(self, timeout=15.0, poll_interval=0.8) -> bool:
        """
        Poll liên tục kiểm tra máy ảnh đã thực sự sẵn sàng chụp chưa:
        - Đọc được config (máy ảnh nhận tín hiệu)
        - Đọc được storage/battery (thẻ nhớ đã mount xong)
        Trả về True nếu sẵn sàng, False nếu timeout.
        """
        if not GPHOTO2_AVAILABLE or self._camera is None:
            return False

        deadline = time.monotonic() + timeout
        attempt = 0
        while time.monotonic() < deadline:
            attempt += 1
            try:
                # Kiểm tra 1: Đọc được config cơ bản (máy ảnh phản hồi USB)
                config = self._camera.get_config()

                # Kiểm tra 2: Đọc được battery level - chắc chắn máy ảnh đã boot xong
                try:
                    battery = config.get_child_by_name("batterylevel")
                    bat_val = battery.get_value()
                    log.info("✅ [CAMERA READY] Máy ảnh sẵn sàng sau %.1fs (poll lần %d) | Battery: %s",
                             timeout - (deadline - time.monotonic()), attempt, bat_val)
                except Exception:
                    log.info("✅ [CAMERA READY] Máy ảnh sẵn sàng sau %.1fs (poll lần %d)",
                             timeout - (deadline - time.monotonic()), attempt)

                # Máy ảnh đã phản hồi config và battery -> sẵn sàng chụp ngay lập tức
                return True

            except Exception as e:
                log.debug("⏳ [CAMERA POLL %d] Chưa sẵn sàng: %s", attempt, e)
                time.sleep(poll_interval)

        log.warning("⚠️ [CAMERA READY] Máy ảnh KHÔNG sẵn sàng sau %.1fs timeout!", timeout)
        return False


    def disconnect_real_camera(self):
        with self._lock:
            if self._camera is not None:
                try:
                    self._camera.exit()
                except Exception:
                    pass
                self._camera = None
                self.use_real_hardware = False
                log.info("🔌 Đã đóng kết nối máy ảnh USB gphoto2.")

                # Force reset USB bus để Linux kernel quét lại thiết bị
                # sau khi rơ-le tắt/bật nguồn, tránh lỗi [-105] Unknown model
                try:
                    time.sleep(0.5)
                    reset_all_camera_usb_devices()
                    log.info("🔄 USB reset sau disconnect — sẵn sàng cho lần bật nguồn tiếp theo.")
                except Exception as e:
                    log.debug("USB reset không cần thiết: %s", e)

    def get_settings(self):
        if GPHOTO2_AVAILABLE and not self.use_real_hardware:
            self._try_init_real_camera()

        if self.use_real_hardware:
            with self._lock:
                try:
                    config = self._camera.get_config()
                    applied = {}
                    capabilities = {}
                    for field, (widget_name, settable) in SETTING_SPECS.items():
                        try:
                            widget = config.get_child_by_name(widget_name)
                            val = str(widget.get_value())
                            applied[field] = val
                            wtype = widget.get_type()
                            choices = [str(widget.get_choice(i)) for i in range(widget.count_choices())] if wtype in (5, 6) else []
                            capabilities[field] = {
                                "writable": settable and not bool(widget.get_readonly()),
                                "current": val,
                                "choices": choices,
                            }
                        except Exception:
                            pass
                    return applied, capabilities
                except Exception as e:
                    log.warning("Lỗi đọc cấu hình máy ảnh thật (%s) — Tái kết nối...", e)
                    self.disconnect_real_camera()

        capabilities = {
            k: {
                "writable": v[1],
                "current": self._sim_applied[k],
                "choices": [self._sim_applied[k], "Option1", "Option2"] if v[1] else [],
            }
            for k, v in SETTING_SPECS.items()
        }
        capabilities["iso"]["choices"] = ["100", "200", "400", "800", "1600", "3200", "6400"]
        capabilities["aperture"]["choices"] = ["f/2.8", "f/4", "f/5.6", "f/8", "f/11", "f/16"]
        capabilities["shutter_speed"]["choices"] = ["1/4000", "1/2000", "1/1000", "1/500", "1/200", "1/100"]
        capabilities["white_balance"]["choices"] = ["Auto", "Daylight", "Cloudy", "Shade", "Tungsten"]
        return dict(self._sim_applied), capabilities

    def set_settings(self, requested):
        if self.use_real_hardware:
            with self._lock:
                try:
                    config = self._camera.get_config()
                    for field, val in requested.items():
                        if field in SETTING_SPECS and SETTING_SPECS[field][1]:
                            widget_name = SETTING_SPECS[field][0]
                            try:
                                widget = config.get_child_by_name(widget_name)
                                if not widget.get_readonly():
                                    widget.set_value(str(val))
                            except Exception:
                                pass
                    self._camera.set_config(config)
                except Exception as e:
                    log.warning("Không thể ghi cấu hình lên máy ảnh thật: %s", e)

        settable = {f for f, (_, ok) in SETTING_SPECS.items() if ok}
        for field, val in requested.items():
            if field in settable:
                self._sim_applied[field] = str(val)

        applied, capabilities = self.get_settings()
        mismatches = {k: {"requested": v, "applied": applied.get(k)} for k, v in requested.items() if applied.get(k) != str(v)}
        return applied, capabilities, mismatches

    def capture(self, camera_code="CAM-CM4"):
        """Chụp ảnh từ máy ảnh thật (USB gphoto2) với cơ chế fallback trigger_capture cho Nikon/Canon/Sony."""
        if GPHOTO2_AVAILABLE and not self.use_real_hardware:
            self._try_init_real_camera()

        if self.use_real_hardware:
            with self._lock:
                try:
                    # ✅ BƯỚC 0: Kiểm tra máy ảnh thực sự sẵn sàng trước khi bấm màn trập
                    if not self._wait_until_camera_ready(timeout=20.0):
                        log.warning("⚠️ Máy ảnh chưa sẵn sàng sau 20s — Chuyển sang giả lập.")
                        self.disconnect_real_camera()
                        # Rơi xuống simulated bên dưới
                        raise RuntimeError("Camera not ready")

                    log.info("📸 [REAL CAMERA] Phát lệnh màn trập chụp ảnh...")

                    # 1. Tắt viewfinder (live view) trước khi chụp nếu đang bật
                    try:
                        config = self._camera.get_config()
                        vf = config.get_child_by_name("viewfinder")
                        if int(vf.get_value()) != 0:
                            vf.set_value(0)
                            self._camera.set_config(config)
                            time.sleep(0.5)
                    except Exception:
                        pass

                    # 2. Xóa các event tồn đọng
                    try:
                        while True:
                            ev_type, _ = self._camera.wait_for_event(100)
                            if ev_type == gp.GP_EVENT_TIMEOUT:
                                break
                    except Exception:
                        pass

                    # 3. Thử chụp bằng capture(GP_CAPTURE_IMAGE)
                    first_path = None
                    try:
                        first_path = self._camera.capture(gp.GP_CAPTURE_IMAGE)
                    except Exception as e_cap:
                        log.warning("Lỗi capture() (%s) — Thử trigger_capture()...", e_cap)
                        try:
                            self._camera.trigger_capture()
                        except Exception as e_trig:
                            log.error("Lỗi trigger_capture(): %s", e_trig)

                    paths = {}
                    if first_path:
                        paths[(first_path.folder, first_path.name)] = first_path

                    # 4. Chờ file mới được ghi vào thẻ nhớ/RAM máy ảnh
                    deadline = time.monotonic() + 6
                    while time.monotonic() < deadline:
                        try:
                            event_type, event_data = self._camera.wait_for_event(400)
                            if event_type == gp.GP_EVENT_FILE_ADDED:
                                paths[(event_data.folder, event_data.name)] = event_data
                            elif event_type == gp.GP_EVENT_CAPTURE_COMPLETE:
                                if paths:
                                    break
                        except Exception:
                            break

                    files = []
                    for path in list(paths.values()):
                        ext = path.name.lower().split('.')[-1]
                        if ext in ("thm", "tif", "tiff") and len(paths) > 1:
                            continue

                        try:
                            camera_file = self._camera.file_get(path.folder, path.name, gp.GP_FILE_TYPE_NORMAL)
                            data = bytes(camera_file.get_data_and_size())

                            preview_data = None
                            try:
                                preview_file = self._camera.file_get(path.folder, path.name, gp.GP_FILE_TYPE_PREVIEW)
                                preview_data = bytes(preview_file.get_data_and_size())
                            except Exception:
                                pass

                            try:
                                self._camera.file_delete(path.folder, path.name)
                            except Exception:
                                pass

                            files.append((path.name, data, preview_data))
                        except Exception as e_file:
                            log.warning("Lỗi tải file %s: %s", path.name, e_file)

                    if files:
                        log.info("✅ [REAL CAMERA] Chụp ảnh thật thành công (%d file, %d bytes)", len(files), len(files[0][1]))
                        return files
                except Exception as e:
                    log.error("Lỗi chụp trên máy ảnh thật: %s — Đóng kết nối & Chuyển sang Giả lập...", e)
                    self.disconnect_real_camera()

        if FORCE_REAL_ONLY:
            log.warning("🚫 [FORCE_REAL_ONLY] Không có máy ảnh thật — bỏ qua, không giả lập PIL.")
            return []

        log.info("📸 [SIMULATED CAMERA] Đang tạo khung hình giả lập JPEG bằng PIL...")
        time.sleep(0.5)
        img_bytes = self._generate_simulated_image(camera_code=camera_code)
        filename = f"CM4_CAM_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
        return [(filename, img_bytes, None)]

    def preview(self):
        if GPHOTO2_AVAILABLE and not self.use_real_hardware:
            self._try_init_real_camera()

        if self.use_real_hardware:
            with self._lock:
                try:
                    camera_file = self._camera.capture_preview()
                    return bytes(camera_file.get_data_and_size())
                except Exception:
                    pass
        if FORCE_REAL_ONLY:
            return None
        return self._generate_simulated_image(width=640, height=424, title="CM4 Live View Stream")

    def _generate_simulated_image(self, width=1920, height=1080, title="AutoTimelapse CM4 Camera", camera_code="CAM-CM4"):
        img = Image.new("RGB", (width, height), color=(20, 24, 33))
        draw = ImageDraw.Draw(img)

        for y in range(0, height, 4):
            r = int(20 + (y / height) * 35)
            g = int(24 + (y / height) * 45)
            b = int(33 + (y / height) * 65)
            draw.rectangle([(0, y), (width, y + 4)], fill=(r, g, b))

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        draw.rectangle([40, 40, width - 40, height - 40], outline=(0, 210, 255), width=3)
        draw.rectangle([60, 60, width - 60, 140], fill=(10, 15, 25))

        hw_type = "REAL HARDWARE (USB)" if self.use_real_hardware else "SIMULATED (PIL)"
        pwr_type = f"GPIO {self.power_manager.pin} {'ON' if self.power_manager.is_powered else 'OFF'}"
        draw.text((80, 75), f"📷 {title} - {camera_code} [{hw_type}]", fill=(0, 230, 255))
        draw.text((80, 105), f"🕒 Timestamp: {now_str} UTC | Power: {pwr_type}", fill=(200, 220, 240))

        draw.rectangle([100, 200, 400, height - 100], fill=(45, 55, 72), outline=(100, 116, 139), width=2)
        draw.rectangle([450, 300, 800, height - 100], fill=(30, 41, 59), outline=(100, 116, 139), width=2)
        draw.polygon([(450, 300), (625, 180), (800, 300)], fill=(71, 85, 105))

        iso = self._sim_applied.get("iso", "100")
        aperture = self._sim_applied.get("aperture", "f/4")
        shutter = self._sim_applied.get("shutter_speed", "1/200")
        wb = self._sim_applied.get("white_balance", "Auto")
        info_text = f"ISO: {iso} | Aperture: {aperture} | Shutter: {shutter} | WB: {wb}"
        draw.text((80, height - 80), f"⚙️ {info_text}", fill=(160, 255, 160))

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return buf.getvalue()
