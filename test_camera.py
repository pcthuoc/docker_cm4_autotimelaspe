#!/usr/bin/env python3
"""
Test Script: Phát lệnh chụp ảnh màn trập thực tế trên máy ảnh USB (Nikon/Canon/Sony) qua gphoto2.
Chạy trực tiếp trên CM4:
    python3 test_camera.py
"""

import sys
import os
import time
import logging

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("test_camera")

try:
    import gphoto2 as gp
except ImportError:
    log.error("❌ Chưa cài đặt thư viện python-gphoto2")
    sys.exit(1)

from usb_utils import reset_all_camera_usb_devices


def test_real_camera_capture():
    log.info("==========================================================")
    log.info("📸 BẮT ĐẦU TEST CHỤP ẢNH MÀN TRẬP MÁY ẢNH THẬT (GPHOTO2)")
    log.info("==========================================================")

    # 1. Reset USB cổng máy ảnh trước khi thử
    log.info("1️⃣ Đang kiểm tra & reset cổng USB máy ảnh...")
    reset_all_camera_usb_devices()
    time.sleep(1.0)

    # 2. Thử kết nối gphoto2 (retry tối đa 5 lần)
    cam = None
    log.info("2️⃣ Đang kết nối tới máy ảnh qua USB...")
    for attempt in range(1, 6):
        try:
            cam = gp.Camera()
            cam.init()
            summary = cam.get_summary()
            first_line = str(summary).split('\n')[0] if summary else "gphoto2 Camera"
            log.info("✅ KHỞI TẠO MÁY ẢNH THÀNH CÔNG! (%s)", first_line)
            break
        except Exception as e:
            log.warning("⏳ [Lần %d/5] Chưa nhận diện máy ảnh (%s), chờ 2s...", attempt, e)
            time.sleep(2.0)

    if not cam:
        log.error("❌ KHÔNG THỂ KẾT NỐI MÁY ẢNH qua USB. Hãy kiểm tra dây cáp USB & nguồn rơ-le GPIO 16!")
        return

    try:
        # 3. Flush events cũ
        log.info("3️⃣ Đang xóa các event tồn đọng...")
        try:
            while True:
                ev_type, _ = cam.wait_for_event(100)
                if ev_type == gp.GP_EVENT_TIMEOUT:
                    break
        except Exception:
            pass

        # 4. Phát lệnh màn trập
        log.info("4️⃣ 📸 BẤM MÀN TRẬP CHỤP ẢNH...")
        log.info("👉 Quan sát đèn xanh trên máy ảnh nháy sáng!")
        
        file_path = None
        try:
            file_path = cam.capture(gp.GP_CAPTURE_IMAGE)
            log.info("🎉 [CAPTURE SUCCESS] Nháy màn trập thành công! File: %s/%s", file_path.folder, file_path.name)
        except Exception as e_cap:
            log.warning("⚠️ Lỗi capture(): %s. Thử trigger_capture()...", e_cap)
            try:
                cam.trigger_capture()
                log.info("🎉 [TRIGGER SUCCESS] Gửi lệnh trigger_capture() thành công!")
            except Exception as e_trig:
                log.error("❌ trigger_capture() cũng lỗi: %s", e_trig)
                return

        # 5. Chờ file ảnh mới
        log.info("5️⃣ Đang chờ máy ảnh lưu file và nạp dữ liệu qua USB...")
        deadline = time.monotonic() + 10
        files_saved = []

        if file_path:
            files_saved.append(file_path)

        while time.monotonic() < deadline:
            try:
                ev_type, ev_data = cam.wait_for_event(500)
                if ev_type == gp.GP_EVENT_FILE_ADDED:
                    log.info("📥 Phát hiện file ảnh mới: %s/%s", ev_data.folder, ev_data.name)
                    files_saved.append(ev_data)
                    break
                elif ev_type == gp.GP_EVENT_CAPTURE_COMPLETE:
                    log.info("✅ Quá trình chụp hoàn tất (CAPTURE_COMPLETE).")
                    if files_saved:
                        break
            except Exception:
                break

        # 6. Tải file ảnh về máy
        if files_saved:
            target = files_saved[0]
            log.info("6️⃣ 💾 Đang tải file %s/%s từ máy ảnh về đĩa cứng...", target.folder, target.name)
            camera_file = cam.file_get(target.folder, target.name, gp.GP_FILE_TYPE_NORMAL)
            data = bytes(camera_file.get_data_and_size())

            output_file = f"TEST_PHOTO_{target.name}"
            with open(output_file, "wb") as f:
                f.write(data)

            log.info("🎉 THÀNH CÔNG RỰC RỠ! Đã lưu ảnh '%s' (%d bytes, %.2f MB)", output_file, len(data), len(data) / (1024 * 1024))
        else:
            log.warning("⚠️ Không tìm thấy file ảnh mới sau khi phát lệnh chụp.")

    finally:
        try:
            cam.exit()
            log.info("🔌 Đã đóng kết nối máy ảnh an toàn.")
        except Exception:
            pass

    log.info("==========================================================")
    log.info("🏁 KẾT THÚC TEST MÁY ẢNH")
    log.info("==========================================================")


if __name__ == "__main__":
    test_real_camera_capture()
