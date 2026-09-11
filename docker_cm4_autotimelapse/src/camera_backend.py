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

from config import SETTING_SPECS, MAX_CAMERA_RETRIES, CANON_EOS_PROFILES, CANON_EOS_WARMUP_EXTRA_SEC, DEFAULT_IMAGE_FORMAT
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
        self._context = gp.Context() if GPHOTO2_AVAILABLE else None
        self.use_real_hardware = False
        self.power_manager = power_manager

        # Canon EOS đặc biệt: lưu profile và camera info sau khi detect thành công
        self._detected_camera_info = None   # Cache kết quả get_camera_info()
        self._canon_profile = None          # Canon EOS profile từ CANON_EOS_PROFILES
        self._canon_model_key = None        # Key match trong CANON_EOS_PROFILES (vd: "eos 6d")
        self._in_live_view = False          # Cờ theo dõi camera đang ở chế độ Live View (EVF/Mirror up)

        self._sim_applied = {
            "iso": "100", "aperture": "f/4", "shutter_speed": "1/200",
            "exposure_compensation": "0.0", "white_balance": "Auto",
            "image_format": "S1", "image_size": "2880x1920",
            "focus_mode": "AF-S", "autofocus": "On", "capture_mode": "Single Shot",
            "capture_target": "Memory Card", "high_iso_nr": "Off",
            "long_exp_nr": "Off", "liveview_af": "Normal Area",
            "exposure_mode": "Manual", "focus_switch": "AF", "aspect_ratio": "3:2",
            # Canon EOS specific defaults
            "drivemode": "Single", "mirror_lockup": "Off",
            "auto_power_off": "Off", "battery_level": "100%",
            "metering_mode": "Evaluative",
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
                    # Quét danh sách thiết bị USB để ưu tiên máy ảnh chuyên dụng (Canon, Nikon, Sony)
                    # Tránh trường hợp cắm điện thoại (vivo, samsung, v.v.) bị gphoto2 nhận nhầm thành máy ảnh
                    cam = gp.Camera()
                    try:
                        autodetected = list(gp.Camera.autodetect())
                    except Exception:
                        autodetected = []

                    target_model = None
                    target_port = None
                    if autodetected:
                        log.info("🔍 [USB AUTODETECT] Tìm thấy %d thiết bị: %s", len(autodetected), autodetected)
                        CAMERA_KEYWORDS = ["canon", "nikon", "sony", "fujifilm", "panasonic", "olympus"]
                        PHONE_KEYWORDS = ["vivo", "apple", "iphone", "samsung", "xiaomi", "oppo", "huawei", "pixel", "android"]

                        # 1. Ưu tiên máy ảnh DSLR / Mirrorless chuyên nghiệp
                        for m_name, p_addr in autodetected:
                            m_low = m_name.lower()
                            if any(k in m_low for k in CAMERA_KEYWORDS):
                                target_model = m_name
                                target_port = p_addr
                                break

                        # 2. Nếu không thấy keyword trên, chọn thiết bị không phải điện thoại
                        if not target_model:
                            for m_name, p_addr in autodetected:
                                m_low = m_name.lower()
                                if not any(k in m_low for k in PHONE_KEYWORDS) and m_low != "usb ptp class camera":
                                    target_model = m_name
                                    target_port = p_addr
                                    break

                    if target_model and target_port:
                        log.info("🎯 [CAMERA SELECT] Đã chọn đích danh máy ảnh: '%s' trên cổng '%s'", target_model, target_port)
                        try:
                            port_info_list = gp.PortInfoList()
                            port_info_list.load()
                            port_idx = port_info_list.lookup_path(target_port)
                            cam.set_port_info(port_info_list[port_idx])

                            abilities_list = gp.CameraAbilitiesList()
                            abilities_list.load()
                            model_idx = abilities_list.lookup_model(target_model)
                            cam.set_abilities(abilities_list[model_idx])
                        except Exception as e_bind:
                            log.debug("Lỗi bind port/abilities: %s", e_bind)

                    if self._context:
                        cam.init(self._context)
                    else:
                        cam.init()
                    self._camera = cam
                    self.use_real_hardware = True

                    try:
                        config = cam.get_config()
                        w_fmt, _ = self._find_widget(config, ["imagequality", "imageformat", "imageformatsd"])
                        if w_fmt and not w_fmt.get_readonly():
                            choices = [str(w_fmt.get_choice(i)) for i in range(w_fmt.count_choices())] if w_fmt.get_type() in (5, 6) else []
                            for preferred in [DEFAULT_IMAGE_FORMAT, "L", "Large Fine", "Large", "Large 1", "cL", "JPEG Fine", "Fine"]:
                                if preferred in choices:
                                    w_fmt.set_value(preferred)
                                    cam.set_config(config)
                                    break
                    except Exception:
                        pass

                    summary = cam.get_summary()
                    first_line = str(summary).split('\n')[0] if summary else "gphoto2 USB Device"
                    log.info("📷 [USB SUCCESS] Kết nối MÁY ẢNH THẬT thành công sau %d lần thử! (%s)",
                             attempt, first_line)

                    # ── Canon EOS auto-detect & auto-config ──
                    self._detect_and_apply_canon_defaults()

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
                        # Lỗi USB thật (device bị lock/treo/chưa nhận kịp) → reset USB sau một số lần thử
                        log.info("⏳ [CAMERA BOOT] Máy ảnh đang khởi động (lần %d/%d), chờ thêm 2s...",
                                 attempt, max_attempts)
                        time.sleep(2.0)
                        if attempt == 5:
                            reset_all_camera_usb_devices()
                        elif attempt == 8:
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
        - Canon EOS: tự động tăng timeout thêm CANON_EOS_WARMUP_EXTRA_SEC
        Trả về True nếu sẵn sàng, False nếu timeout.
        """
        if not GPHOTO2_AVAILABLE or self._camera is None:
            return False

        # Canon EOS cần thêm thời gian chờ
        if self._canon_profile:
            extra = self._canon_profile.get("extra_warmup", CANON_EOS_WARMUP_EXTRA_SEC)
            timeout = max(timeout, timeout + extra)
            log.debug("⏳ [CANON] Tăng camera ready timeout lên %.1fs (extra=%.1fs)", timeout, extra)

        deadline = time.monotonic() + timeout
        attempt = 0
        while time.monotonic() < deadline:
            attempt += 1
            try:
                # Kiểm tra 1: Đọc được config cơ bản (máy ảnh phản hồi USB)
                config = self._camera.get_config()

                # Kiểm tra 2: Đọc được battery level - chắc chắn máy ảnh đã boot xong
                # Canon EOS dùng "eosbatterylevel", Nikon/Sony dùng "batterylevel"
                bat_candidates = ["eosbatterylevel", "batterylevel"] if self._canon_profile else ["batterylevel"]
                try:
                    bat_widget, bat_name = self._find_widget(config, bat_candidates)
                    if bat_widget:
                        bat_val = bat_widget.get_value()
                        log.info("✅ [CAMERA READY] Máy ảnh sẵn sàng sau %.1fs (poll lần %d) | Battery[%s]: %s",
                                 timeout - (deadline - time.monotonic()), attempt, bat_name, bat_val)
                    else:
                        log.info("✅ [CAMERA READY] Máy ảnh sẵn sàng sau %.1fs (poll lần %d)",
                                 timeout - (deadline - time.monotonic()), attempt)
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

    def _detect_and_apply_canon_defaults(self):
        """
        Phát hiện model Canon EOS và tự động cấu hình tối ưu cho timelapse.
        Gọi sau khi _try_init_real_camera() thành công.
        """
        if self._camera is None:
            return

        # 1. Lấy camera info và cache lại
        self._detected_camera_info = self.get_camera_info()
        brand = self._detected_camera_info.get("brand", "")
        model = self._detected_camera_info.get("model", "")
        model_lower = model.lower()

        if brand != "Canon":
            log.info("📷 [DETECT] Máy ảnh: %s %s (Không phải Canon — bỏ qua Canon-specific config)", brand, model)
            return

        # 2. Tìm Canon EOS profile phù hợp
        matched_key = None
        matched_profile = None
        for key, profile in CANON_EOS_PROFILES.items():
            if key in model_lower:
                matched_key = key
                matched_profile = profile
                break

        self._canon_model_key = matched_key
        self._canon_profile = matched_profile

        if matched_profile:
            log.info("📷 [CANON EOS] Phát hiện %s → Profile: %s | Notes: %s",
                     model, matched_key.upper(), matched_profile.get("notes", ""))
        else:
            log.info("📷 [CANON EOS] Phát hiện %s — Không có profile đặc biệt, dùng Canon mặc định.", model)
            matched_profile = {"disable_autopoweroff": True, "mirror_lock_off": True}

        # 3. Tự động cấu hình Canon EOS
        self._apply_canon_eos_defaults(matched_profile)

    def _apply_canon_eos_defaults(self, profile: dict):
        """
        Cấu hình mặc định tối ưu cho Canon EOS khi detect thành công.
        - Tắt Auto Power Off (critical! Canon tự ngủ sau 30s-1min)
        - Tắt Mirror Lock-up (tránh mirror stuck)
        - Set Drive Mode = Single Shot
        """
        if self._camera is None:
            return

        try:
            config = self._camera.get_config()
            changes_made = []

            # ── 3a. Tắt Auto Power Off (CRITICAL cho timelapse) ──
            if profile.get("disable_autopoweroff", True):
                apo_widget, apo_name = self._find_widget(config, ["autopoweroff", "eosautopoweroff"])
                if apo_widget is not None and not apo_widget.get_readonly():
                    current_val = str(apo_widget.get_value())
                    # Canon Auto Power Off: "0" hoặc "None" = tắt
                    if current_val not in ("0", "None", "Off"):
                        try:
                            # Thử set "0" trước (Canon DSLR), nếu lỗi thử "None"
                            wtype = apo_widget.get_type()
                            if wtype in (5, 6):  # RADIO / MENU
                                choices = [str(apo_widget.get_choice(i)) for i in range(apo_widget.count_choices())]
                                for off_val in ["0", "None", "Off"]:
                                    if off_val in choices:
                                        apo_widget.set_value(off_val)
                                        changes_made.append(f"AutoPowerOff: {current_val} → {off_val}")
                                        break
                            else:
                                apo_widget.set_value("0")
                                changes_made.append(f"AutoPowerOff: {current_val} → 0")
                        except Exception as e:
                            log.debug("Không set được AutoPowerOff: %s", e)
                    else:
                        log.debug("AutoPowerOff đã TẮT (%s)", current_val)

            # ── 3b. Tắt Mirror Lock-up (DSLR only) ──
            if profile.get("mirror_lock_off", True):
                ml_widget, ml_name = self._find_widget(config, ["mirrorlock", "eosmirrorlock", "mirrorlockup"])
                if ml_widget is not None and not ml_widget.get_readonly():
                    current_val = str(ml_widget.get_value())
                    if current_val not in ("0", "Off", "Disable"):
                        try:
                            wtype = ml_widget.get_type()
                            if wtype in (5, 6):
                                choices = [str(ml_widget.get_choice(i)) for i in range(ml_widget.count_choices())]
                                for off_val in ["0", "Off", "Disable"]:
                                    if off_val in choices:
                                        ml_widget.set_value(off_val)
                                        changes_made.append(f"MirrorLock: {current_val} → {off_val}")
                                        break
                            else:
                                ml_widget.set_value("0")
                                changes_made.append(f"MirrorLock: {current_val} → 0")
                        except Exception as e:
                            log.debug("Không set được MirrorLock: %s", e)

            # ── 3c. Set Drive Mode = Single ──
            dm_widget, dm_name = self._find_widget(config, ["drivemode"])
            if dm_widget is not None and not dm_widget.get_readonly():
                current_val = str(dm_widget.get_value())
                if "continuous" in current_val.lower() or "timer" in current_val.lower():
                    try:
                        wtype = dm_widget.get_type()
                        if wtype in (5, 6):
                            choices = [str(dm_widget.get_choice(i)) for i in range(dm_widget.count_choices())]
                            for single_val in ["Single", "Single shooting", "Single shot"]:
                                if single_val in choices:
                                    dm_widget.set_value(single_val)
                                    changes_made.append(f"DriveMode: {current_val} → {single_val}")
                                    break
                    except Exception as e:
                        log.debug("Không set được DriveMode: %s", e)

            # ── 3d. Set Capture Target (Hỗ trợ cả Internal RAM và Memory card) ──
            ct_widget, _ = self._find_widget(config, ["capturetarget"])
            if ct_widget is not None and not ct_widget.get_readonly():
                current_ct = str(ct_widget.get_value())
                # Cho phép giữ Internal RAM hoặc Memory card
                if current_ct not in ("Internal RAM", "Memory card", "Memory Card"):
                    try:
                        wtype = ct_widget.get_type()
                        choices = [str(ct_widget.get_choice(i)) for i in range(ct_widget.count_choices())] if wtype in (5, 6) else []
                        for target in ["Internal RAM", "Memory card"]:
                            if target in choices:
                                ct_widget.set_value(target)
                                changes_made.append(f"CaptureTarget: {current_ct} → {target}")
                                break
                    except Exception as e:
                        log.debug("Không set được CaptureTarget: %s", e)

            # ── 3e. Set Quick Review Time = 2 seconds hoặc None ──
            rt_widget, _ = self._find_widget(config, ["reviewtime"])
            if rt_widget is not None and not rt_widget.get_readonly():
                try:
                    wtype = rt_widget.get_type()
                    if wtype in (5, 6):
                        choices = [str(rt_widget.get_choice(i)) for i in range(rt_widget.count_choices())]
                        for pref in ["None", "2 seconds"]:
                            if pref in choices:
                                rt_widget.set_value(pref)
                                break
                except Exception:
                    pass

            # ── 3f. Set Image Format mặc định S1 (2880 x 1920, 5.5 MP, ~2-2.5MB) ──
            target_fmt = profile.get("default_image_format", DEFAULT_IMAGE_FORMAT)
            fmt_widget, _ = self._find_widget(config, ["imageformat", "imagequality", "imageformatsd"])
            if fmt_widget is not None and not fmt_widget.get_readonly():
                try:
                    current_fmt = str(fmt_widget.get_value())
                    wtype = fmt_widget.get_type()
                    choices = [str(fmt_widget.get_choice(i)) for i in range(fmt_widget.count_choices())] if wtype in (5, 6) else []
                    for pref in [target_fmt, "L", "Large Fine", "Large", "Large 1", "cL"]:
                        if pref in choices:
                            if current_fmt != pref:
                                fmt_widget.set_value(pref)
                                changes_made.append(f"ImageFormat: {current_fmt} → {pref} (2880x1920)")
                            break
                except Exception as e_fmt:
                    log.debug("Không set được ImageFormat: %s", e_fmt)

            # ── Apply tất cả thay đổi ──
            if changes_made:
                self._camera.set_config(config)
                log.info("⚙️ [CANON AUTO-CONFIG] Đã cấu hình Canon EOS: %s", " | ".join(changes_made))
            else:
                log.info("⚙️ [CANON AUTO-CONFIG] Canon EOS đã đúng cấu hình, không cần thay đổi.")

            # ── Log battery level Canon ──
            try:
                bat_widget, bat_name = self._find_widget(config, ["eosbatterylevel", "batterylevel"])
                if bat_widget is not None:
                    log.info("🔋 [CANON BATTERY] %s = %s", bat_name, bat_widget.get_value())
            except Exception:
                pass

        except Exception as e:
            log.warning("⚠️ [CANON AUTO-CONFIG] Lỗi cấu hình Canon EOS: %s (máy ảnh vẫn hoạt động)", e)


    def disconnect_real_camera(self):
        with self._lock:
            if self._camera is not None:
                try:
                    self._camera.exit()
                except Exception:
                    pass
                self._camera = None
                self.use_real_hardware = False
                self._detected_camera_info = None
                self._canon_profile = None
                self._canon_model_key = None
                log.info("🔌 Đã đóng kết nối máy ảnh USB gphoto2.")

                # Force reset USB bus để Linux kernel quét lại thiết bị
                # sau khi rơ-le tắt/bật nguồn, tránh lỗi [-105] Unknown model
                try:
                    time.sleep(0.5)
                    reset_all_camera_usb_devices()
                    log.info("🔄 USB reset sau disconnect — sẵn sàng cho lần bật nguồn tiếp theo.")
                except Exception as e:
                    log.debug("USB reset không cần thiết: %s", e)

    def _find_widget(self, config, candidate_names):
        """Tìm widget đầu tiên tồn tại trong danh sách tên ứng viên (scan 3 tầng cây gphoto2)."""
        if config is None:
            return None, None
        if isinstance(candidate_names, str):
            candidate_names = [candidate_names]
        targets = set(candidate_names)
        
        try:
            for i in range(config.count_children()):
                sec = config.get_child(i)
                if sec.get_name() in targets:
                    return sec, sec.get_name()
                for j in range(sec.count_children()):
                    w = sec.get_child(j)
                    if w.get_name() in targets:
                        return w, w.get_name()
                    for k in range(w.count_children()):
                        sub = w.get_child(k)
                        if sub.get_name() in targets:
                            return sub, sub.get_name()
        except Exception:
            pass

        return None, None

    def get_camera_info(self):
        """Lấy thông tin nhận diện model, brand, lens và serial của máy ảnh (Hỗ trợ Canon, Nikon, Sony)."""
        if not self.use_real_hardware or self._camera is None:
            return {
                "is_real": False,
                "brand": "Simulated",
                "model": "PIL Simulated Camera",
                "lens": "None",
                "serial": "SIM-001",
            }
        try:
            abilities = self._camera.get_abilities()
            model_name = getattr(abilities, "model", "USB Camera")
        except Exception:
            model_name = "USB Camera"

        brand = "Generic"
        model_lower = model_name.lower()
        if "canon" in model_lower:
            brand = "Canon"
        elif "nikon" in model_lower:
            brand = "Nikon"
        elif "sony" in model_lower:
            brand = "Sony"

        lens_name = "Unknown"
        serial_number = "Unknown"
        try:
            config = self._camera.get_config()
            lens_widget, _ = self._find_widget(config, ["lensname", "lensid", "eoslensname"])
            if lens_widget:
                lens_name = str(lens_widget.get_value())
            serial_widget, _ = self._find_widget(config, ["serialnumber", "eosserialnumber"])
            if serial_widget:
                serial_number = str(serial_widget.get_value())
        except Exception:
            pass

        return {
            "is_real": True,
            "brand": brand,
            "model": model_name,
            "lens": lens_name,
            "serial": serial_number,
        }

    def get_settings(self):
        if GPHOTO2_AVAILABLE and not self.use_real_hardware:
            self._try_init_real_camera()

        if self.use_real_hardware:
            with self._lock:
                try:
                    config = self._camera.get_config()
                    applied = {}
                    capabilities = {}
                    for field, (candidates, settable) in SETTING_SPECS.items():
                        widget, matched_name = self._find_widget(config, candidates)
                        if widget is not None:
                            try:
                                val = str(widget.get_value())
                                wtype = widget.get_type()
                                choices = [str(widget.get_choice(i)) for i in range(widget.count_choices())] if wtype in (5, 6) else []
                                if matched_name == "liveviewsize":
                                    rev_map = {"val 0": "Large", "val 1": "Medium", "val 2": "Small", "0": "Large", "1": "Medium", "2": "Small"}
                                    val = rev_map.get(val, val)
                                    choices = ["Large", "Medium", "Small"]
                                applied[field] = val
                                capabilities[field] = {
                                    "writable": settable and not bool(widget.get_readonly()),
                                    "current": val,
                                    "choices": choices,
                                    "widget_name": matched_name,
                                }
                            except Exception:
                                pass
                    log.info("=" * 60)
                    log.info("📋 [GET_SETTINGS] ĐÃ ĐỌC TOÀN BỘ THÔNG SỐ TỪ MÁY ẢNH THẬT:")
                    for f_key, f_val in applied.items():
                        c_list = capabilities.get(f_key, {}).get("choices", [])
                        c_info = f"({len(c_list)} choices)" if c_list else ""
                        log.info("  • %-16s = %-16s %s", f_key, f_val, c_info)
                    log.info("=" * 60)
                    capabilities["_camera_info"] = self.get_camera_info()
                    return applied, capabilities
                except Exception as e:
                    log.warning("Lỗi đọc cấu hình máy ảnh thật (%s) — Tái kết nối...", e)
                    self.disconnect_real_camera()

        capabilities = {
            k: {
                "writable": v[1],
                "current": self._sim_applied.get(k, "Auto"),
                "choices": [self._sim_applied.get(k, "Auto"), "Option1", "Option2"] if v[1] else [],
            }
            for k, v in SETTING_SPECS.items()
        }
        capabilities["iso"]["choices"] = ["100", "200", "400", "800", "1600", "3200", "6400"]
        capabilities["aperture"]["choices"] = ["f/2.8", "f/4", "f/5.6", "f/8", "f/11", "f/16"]
        capabilities["shutter_speed"]["choices"] = ["1/4000", "1/2000", "1/1000", "1/500", "1/200", "1/100"]
        capabilities["white_balance"]["choices"] = ["Auto", "Daylight", "Cloudy", "Shade", "Tungsten"]
        capabilities["capture_target"]["choices"] = ["Internal RAM", "Memory card"]
        capabilities["drivemode"]["choices"] = ["Single", "Continuous", "Single silent", "Timer 2 sec", "Timer 10 sec"]
        capabilities["auto_power_off"]["choices"] = ["0", "1 min", "2 min", "4 min", "8 min", "15 min", "30 min", "Off"]
        capabilities["_camera_info"] = self.get_camera_info()
        return dict(self._sim_applied), capabilities

    def set_settings(self, requested):
        # Thử kết nối camera thật nếu chưa có — giống get_settings()
        if GPHOTO2_AVAILABLE and not self.use_real_hardware:
            self._try_init_real_camera()

        if self.use_real_hardware:
            with self._lock:
                # 1. Xả sạch event buffer của camera trước khi đọc/ghi config
                if self._camera is not None:
                    try:
                        for _ in range(5):
                            ev_type, _ = self._camera.wait_for_event(10)
                            if ev_type == gp.GP_EVENT_TIMEOUT:
                                break
                    except Exception:
                        pass

                # 2. Thử ghi cấu hình với retry loop
                for retry in range(1, 4):
                    try:
                        config = self._camera.get_config()
                        failed_widgets = []
                        success_count = 0

                        for field, val in requested.items():
                            if field in SETTING_SPECS and SETTING_SPECS[field][1]:
                                candidates = SETTING_SPECS[field][0]
                                widget, matched_name = self._find_widget(config, candidates)
                                if widget is not None:
                                    try:
                                        if not widget.get_readonly():
                                            target_val = str(val)
                                            if matched_name == "liveviewsize":
                                                size_map = {
                                                    "large": "val 0", "0": "val 0", "val 0": "val 0",
                                                    "medium": "val 1", "1": "val 1", "val 1": "val 1",
                                                    "small": "val 2", "2": "val 2", "val 2": "val 2",
                                                }
                                                target_val = size_map.get(target_val.lower(), target_val)
                                            else:
                                                # Match choice nếu là RADIO / MENU
                                                wtype = widget.get_type()
                                                if wtype in (gp.GP_WIDGET_RADIO, gp.GP_WIDGET_MENU):
                                                    num_c = widget.count_choices()
                                                    choices = [str(widget.get_choice(i)) for i in range(num_c)]
                                                    if target_val not in choices:
                                                        # Thử case-insensitive match
                                                        for c in choices:
                                                            if c.lower() == target_val.lower():
                                                                target_val = c
                                                                break
                                            widget.set_value(target_val)
                                            success_count += 1
                                            # Nếu là lệnh trigger lấy nét Canon (Press Half) -> kích hoạt lấy nét rồi tự động nhả cò
                                            if matched_name == "eosremoterelease" and target_val in ("Press Half", "Press Half AF", "Press Full AF"):
                                                log.info("🎯 [CANON AF] Kích hoạt lấy nét tự động (Press Half)...")
                                        else:
                                            log.debug("Widget %s (%s) là read-only, bỏ qua", field, matched_name)
                                    except Exception as e:
                                        failed_widgets.append(f"{field}({matched_name}): {e}")
                                else:
                                    log.debug("Không tìm thấy widget tương thích cho %s trong %s", field, candidates)

                        self._camera.set_config(config)
                        # Nếu vừa kích hoạt Press Half, chờ 0.5s lấy nét xong rồi nhả cò về Release Half/None
                        if "autofocus" in requested and requested["autofocus"] in ("Press Half", "Press Half AF", "Auto"):
                            time.sleep(0.5)
                            try:
                                cfg_rel = self._camera.get_config()
                                w_rel, _ = self._find_widget(cfg_rel, ["eosremoterelease"])
                                if w_rel:
                                    w_rel.set_value("Release Half")
                                    self._camera.set_config(cfg_rel)
                                    log.info("🎯 [CANON AF] Đã khóa nét thành công & nhả cò (Release Half).")
                            except Exception:
                                pass
                        if failed_widgets:
                            log.warning("⚠️ set_settings (lần %d) — một số widget lỗi: %s", retry, "; ".join(failed_widgets))
                        else:
                            log.info("✅ set_settings — Đã ghi %d thông số lên máy ảnh thật (lần %d)", success_count, retry)
                        break
                    except Exception as e:
                        log.warning("⚠️ [SET_SETTINGS] Thử lần %d/3 thất bại (%s), chờ 0.5s thử lại...", retry, e)
                        time.sleep(0.5)
                        if retry == 3:
                            # Fallback: Thử ghi từng widget đơn lẻ để widget hợp lệ vẫn được áp dụng
                            log.info("🔄 [SET_SETTINGS FALLBACK] Đang thử ghi từng thông số riêng lẻ...")
                            for field, val in requested.items():
                                if field in SETTING_SPECS and SETTING_SPECS[field][1]:
                                    candidates = SETTING_SPECS[field][0]
                                    try:
                                        single_cfg = self._camera.get_config()
                                        single_w, w_name = self._find_widget(single_cfg, candidates)
                                        if single_w and not single_w.get_readonly():
                                            single_w.set_value(str(val))
                                            self._camera.set_config(single_cfg)
                                            log.info("  ✓ Đã ghi đơn lẻ: %s (%s) = %s", field, w_name, val)
                                    except Exception as ex_single:
                                        log.debug("  ✗ Không ghi được %s: %s", field, ex_single)

        settable = {f for f, (_, ok) in SETTING_SPECS.items() if ok}
        for field, val in requested.items():
            if field in settable:
                self._sim_applied[field] = str(val)

        applied, capabilities = self.get_settings()
        mismatches = {k: {"requested": v, "applied": applied.get(k)} for k, v in requested.items() if applied.get(k) != str(v)}
        return applied, capabilities, mismatches

    def end_live_view(self):
        """Đóng phiên Live View và hạ gương lật (mirror down) để sẵn sàng chụp ảnh."""
        with self._lock:
            if self.use_real_hardware and self._camera is not None and self._in_live_view:
                try:
                    self._camera.exit()
                    time.sleep(0.2)
                    cam = gp.Camera()
                    cam.init()
                    self._camera = cam
                    self._in_live_view = False
                    log.info("🛑 [LIVE VIEW] Đã kết thúc Live View & hạ gương lật (Mirror Down) sẵn sàng chụp ảnh.")
                except Exception as e:
                    log.debug("Lỗi kết thúc live view: %s", e)
                    self._in_live_view = False

    def capture(self, camera_code="CAM-CM4"):
        """Chụp ảnh từ máy ảnh thật (USB gphoto2) với cơ chế Canon EOS Remote Release + Multi-tier Fallback."""
        if GPHOTO2_AVAILABLE and not self.use_real_hardware:
            self._try_init_real_camera()

        if self.use_real_hardware:
            with self._lock:
                try:
                    # Nếu máy ảnh vừa chạy Live View, kết thúc phiên preview để hạ gương lật
                    if self._in_live_view:
                        try:
                            self._camera.exit()
                            time.sleep(0.2)
                            cam = gp.Camera()
                            cam.init()
                            self._camera = cam
                            self._in_live_view = False
                            log.info("🛑 [LIVE VIEW] Đã hạ gương lật trước khi chụp.")
                        except Exception as e_lv:
                            log.debug("Lỗi reset sau preview: %s", e_lv)
                            self._in_live_view = False

                    # ✅ BƯỚC 0: Kiểm tra máy ảnh thực sự sẵn sàng trước khi bấm màn trập
                    if not self._wait_until_camera_ready(timeout=20.0):
                        log.warning("⚠️ Máy ảnh chưa sẵn sàng sau 20s — Chuyển sang giả lập.")
                        self.disconnect_real_camera()
                        raise RuntimeError("Camera not ready")

                    log.info("📸 [REAL CAMERA] Phát lệnh màn trập chụp ảnh...")

                    # 1. Tắt viewfinder và evfmode (live view) trước khi chụp nếu đang bật
                    try:
                        config = self._camera.get_config()
                        evf_changed = False

                        w_evf, _ = self._find_widget(config, ["evfmode"])
                        if w_evf and str(w_evf.get_value()) != "0":
                            try:
                                w_evf.set_value("0")
                                evf_changed = True
                            except Exception:
                                pass

                        w_vf, _ = self._find_widget(config, ["viewfinder", "eosviewfinder"])
                        if w_vf:
                            try:
                                if int(w_vf.get_value()) != 0:
                                    w_vf.set_value(0)
                                    evf_changed = True
                            except Exception:
                                pass

                        if evf_changed:
                            self._camera.set_config(config)
                            time.sleep(0.3)
                    except Exception as e_evf:
                        log.debug("Lỗi tắt EVF/viewfinder: %s", e_evf)

                    # 2. Xóa các event tồn đọng
                    try:
                        while True:
                            ev_type, _ = self._camera.wait_for_event(50)
                            if ev_type == gp.GP_EVENT_TIMEOUT:
                                break
                    except Exception:
                        pass

                    paths = {}
                    config = self._camera.get_config(self._context) if self._context else self._camera.get_config()
                    w_rel, _ = self._find_widget(config, ["eosremoterelease"])
                    is_canon = (self._canon_profile is not None) or (self.get_camera_info().get("brand") == "Canon") or (w_rel is not None)

                    # ── 3. CANON EOS SHUTTER TRIGGER (eosremoterelease + Fallback) ──
                    if is_canon and w_rel is not None:
                        log.info("📸 [CANON SHUTTER] Kích hoạt chụp ảnh qua Canon EOS Remote Release...")
                        try:
                            if w_rel is not None:
                                choices = [str(w_rel.get_choice(i)) for i in range(w_rel.count_choices())] if w_rel.get_type() in (5, 6) else []
                                log.info("📷 [CANON REMOTE] eosremoterelease choices: %s", choices)

                                # Tìm lựa chọn release phù hợp
                                # Canon 5D Mark III dùng "Release", các máy khác dùng "Release Full", "None"
                                rel_choice = next((c for c in ["Release", "Release Full", "Release Half", "Release 2", "Release 1", "None"] if c in choices), "None" if "None" in choices else None)

                                triggered = False
                                # Ưu tiên 1: Lệnh "Immediate" (chuẩn nhất cho Canon EOS trên gphoto2)
                                if "Immediate" in choices:
                                    log.info("📸 [CANON SHUTTER] Phát lệnh 'Immediate'...")
                                    w_rel.set_value("Immediate")
                                    if self._context:
                                        self._camera.set_config(config, self._context)
                                    else:
                                        self._camera.set_config(config)
                                    time.sleep(0.4)
                                    triggered = True
                                else:
                                    # Ưu tiên 2: Press Full (AF hoặc MF) — Canon 5D Mark III dùng "Press Full AF"/"Press Full MF"
                                    press_full = next((c for c in ["Press Full AF", "Press Full MF", "Press Full", "Press 2", "Press 3"] if c in choices), None)
                                    press_half = next((c for c in ["Press Half AF", "Press Half MF", "Press Half", "Press 1"] if c in choices), None)
                                    if press_full:
                                        if press_half:
                                            try:
                                                w_rel.set_value(press_half)
                                                if self._context:
                                                    self._camera.set_config(config, self._context)
                                                else:
                                                    self._camera.set_config(config)
                                                time.sleep(0.2)
                                            except Exception:
                                                pass
                                        log.info("📸 [CANON SHUTTER] Phát lệnh '%s'...", press_full)
                                        w_rel.set_value(press_full)
                                        if self._context:
                                            self._camera.set_config(config, self._context)
                                        else:
                                            self._camera.set_config(config)
                                        time.sleep(0.4)
                                        triggered = True

                                # Luôn nhả cò (release) sau khi chụp để tránh PTP Device Busy
                                if rel_choice:
                                    try:
                                        w_rel.set_value(rel_choice)
                                        if self._context:
                                            self._camera.set_config(config, self._context)
                                        else:
                                            self._camera.set_config(config)
                                    except Exception as e_rel:
                                        log.debug("Lỗi nhả cò eosremoterelease: %s", e_rel)

                                if triggered:
                                    # Polling event chờ file ảnh nạp về từ thẻ nhớ
                                    deadline = time.monotonic() + 10.0
                                    while time.monotonic() < deadline:
                                        ev_type, ev_data = self._camera.wait_for_event(300, self._context) if self._context else self._camera.wait_for_event(300)
                                        if ev_type == gp.GP_EVENT_FILE_ADDED:
                                            log.info("🎉 [CANON EOS] Nhận file mới từ máy ảnh: %s/%s", ev_data.folder, ev_data.name)
                                            paths[(ev_data.folder, ev_data.name)] = ev_data
                                            break
                                        elif ev_type == gp.GP_EVENT_CAPTURE_COMPLETE:
                                            if paths:
                                                break
                            else:
                                log.warning("⚠️ Không tìm thấy widget eosremoterelease trên Canon EOS.")
                        except Exception as e_mf:
                            log.warning("⚠️ Lỗi Canon EOS Remote Release (%s) — Chuẩn bị thử fallback", e_mf)
                            # Đảm bảo reset cò nếu bị exception
                            try:
                                cfg_clean = self._camera.get_config()
                                w_c, _ = self._find_widget(cfg_clean, ["eosremoterelease"])
                                if w_c:
                                    ch_c = [str(w_c.get_choice(i)) for i in range(w_c.count_choices())] if w_c.get_type() in (5, 6) else []
                                    rel_c = next((c for c in ["Release Full", "Release 2", "Release 1", "Release", "None"] if c in ch_c), None)
                                    if rel_c:
                                        w_c.set_value(rel_c)
                                        self._camera.set_config(cfg_clean)
                            except Exception:
                                pass

                    # ── 4. STANDARD GPHOTO2 CAPTURE FALLBACK (Nếu chưa nhận được ảnh) ──
                    if not paths:
                        # Dọn sạch event buffer trước khi chụp fallback
                        try:
                            for _ in range(10):
                                ev_type, _ = self._camera.wait_for_event(30)
                                if ev_type == gp.GP_EVENT_TIMEOUT:
                                    break
                        except Exception:
                            pass

                        first_path = None
                        try:
                            log.info("📸 [STANDARD CAPTURE] Thử chụp bằng self._camera.capture(GP_CAPTURE_IMAGE)...")
                            first_path = self._camera.capture(gp.GP_CAPTURE_IMAGE)
                        except Exception as e_cap:
                            log.warning("Lỗi capture() (%s) — Thử trigger_capture()...", e_cap)
                            try:
                                self._camera.trigger_capture()
                            except Exception as e_trig:
                                log.error("Lỗi trigger_capture(): %s", e_trig)

                        if first_path:
                            paths[(first_path.folder, first_path.name)] = first_path

                        # Chờ file mới được ghi vào thẻ nhớ máy ảnh
                        deadline = time.monotonic() + 8.0
                        while time.monotonic() < deadline:
                            try:
                                event_type, event_data = self._camera.wait_for_event(400)
                                if event_type == gp.GP_EVENT_FILE_ADDED:
                                    log.info("🎉 [FALLBACK CAPTURE] Nhận file mới từ máy ảnh: %s/%s", event_data.folder, event_data.name)
                                    paths[(event_data.folder, event_data.name)] = event_data
                                    break
                                elif event_type == gp.GP_EVENT_CAPTURE_COMPLETE:
                                    if paths:
                                        break
                            except Exception:
                                break

                    # ── 5. TẢI FILE ẢNH VỀ CM4 ──
                    # Chờ camera hoàn tất ghi vào Internal RAM trước khi download
                    time.sleep(1.0)

                    files = []
                    for path in list(paths.values()):
                        ext = path.name.lower().split('.')[-1]
                        if ext in ("thm", "tif", "tiff") and len(paths) > 1:
                            continue

                        # Retry tải file khi gặp lỗi [-110] I/O in progress
                        data = None
                        for dl_attempt in range(1, 6):
                            try:
                                camera_file = self._camera.file_get(
                                    path.folder, path.name,
                                    gp.GP_FILE_TYPE_NORMAL
                                )
                                data = bytes(camera_file.get_data_and_size())
                                log.info("📥 [DOWNLOAD] Tải file %s thành công lần %d (%d bytes)",
                                         path.name, dl_attempt, len(data))
                                break
                            except Exception as e_dl:
                                log.warning("Lỗi tải file %s (lần %d): %s", path.name, dl_attempt, e_dl)
                                if dl_attempt < 5:
                                    time.sleep(1.5)  # Chờ camera xong I/O

                        if data is None:
                            log.error("❌ Không tải được file %s sau 5 lần", path.name)
                            continue

                        try:
                            preview_data = None
                            try:
                                preview_file = self._camera.file_get(
                                    path.folder, path.name,
                                    gp.GP_FILE_TYPE_PREVIEW
                                )
                                preview_data = bytes(preview_file.get_data_and_size())
                            except Exception:
                                pass

                            try:
                                self._camera.file_delete(path.folder, path.name)
                            except Exception:
                                pass

                            files.append((path.name, data, preview_data))
                        except Exception as e_file:
                            log.warning("Lỗi post-download %s: %s", path.name, e_file)

                    if files:
                        log.info("✅ [REAL CAMERA] Chụp ảnh thật thành công (%d file, %d bytes)", len(files), len(files[0][1]))
                        return files
                    else:
                        log.warning("⚠️ Không nhận được ảnh từ máy ảnh thật — Reset gphoto2 để tránh kẹt trạng thái...")
                        self.disconnect_real_camera()
                except Exception as e:
                    log.error("Lỗi chụp trên máy ảnh thật: %s — Đóng kết nối & Chuyển sang Giả lập...", e)
                    self.disconnect_real_camera()

        log.info("📸 [SIMULATED CAMERA] Đang tạo khung hình giả lập JPEG bằng PIL...")
        time.sleep(0.5)
        img_bytes = self._generate_simulated_image(camera_code=camera_code)
        filename = f"CM4_CAM_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
        return [(filename, img_bytes, None)]

    def preview(self):
        """Lấy 1 frame Live View JPEG từ máy ảnh thật qua gphoto2 capture_preview()."""
        if GPHOTO2_AVAILABLE and not self.use_real_hardware:
            self._try_init_real_camera()

        if self.use_real_hardware and self._camera is not None:
            with self._lock:
                try:
                    camera_file = self._camera.capture_preview()
                    self._in_live_view = True
                    return bytes(camera_file.get_data_and_size())
                except Exception as e:
                    log.debug("Lỗi capture_preview trên máy ảnh: %s", e)
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
