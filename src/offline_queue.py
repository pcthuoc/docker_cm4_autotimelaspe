#!/usr/bin/env python3
"""
AutoTimelapse CM4 Agent - Module Hàng Đợi Offline
------------------------------------------------------------------
Lưu trữ ảnh đệm dưới đĩa (/app/offline_queue) khi mất mạng/lỗi server
và tự động thử upload lại khi kết nối internet/server được khôi phục.
"""

import os
import json
import logging
import threading
from datetime import datetime

log = logging.getLogger("cm4_offline_queue")


class OfflineQueueManager:
    """Quản lý hàng đợi ảnh lưu tạm dưới đĩa khi mất mạng hoặc server lỗi."""

    def __init__(self, queue_dir="/app/offline_queue"):
        self.queue_dir = queue_dir
        self._lock = threading.Lock()
        os.makedirs(self.queue_dir, exist_ok=True)
        log.info("📁 [OFFLINE QUEUE] Đã khởi tạo thư mục lưu trữ đệm: %s", self.queue_dir)

    def save_pending_capture(self, image_bytes, thumb_bytes, metadata):
        """Lưu ảnh và metadata xuống đĩa khi không thể gửi server."""
        with self._lock:
            ts_str = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            base_name = f"pending_{ts_str}"
            img_path = os.path.join(self.queue_dir, f"{base_name}.jpg")
            json_path = os.path.join(self.queue_dir, f"{base_name}.json")
            thumb_path = os.path.join(self.queue_dir, f"{base_name}_thumb.jpg") if thumb_bytes else None

            try:
                with open(img_path, "wb") as f:
                    f.write(image_bytes)

                if thumb_bytes and thumb_path:
                    with open(thumb_path, "wb") as f:
                        f.write(thumb_bytes)
                    metadata["thumb_filename"] = f"{base_name}_thumb.jpg"

                metadata["image_filename"] = f"{base_name}.jpg"

                with open(json_path, "w", encoding="utf-8") as f:
                    json.dump(metadata, f, indent=2)

                log.warning("💾 [OFFLINE QUEUE] Đã lưu tạm 1 ảnh lỗi/mất mạng vào %s", json_path)
            except Exception as e:
                log.error("❌ Không thể lưu ảnh vào hàng đợi offline: %s", e)

    def process_pending_queue(self, upload_fn):
        """Duyệt các ảnh pending và thử gửi lại server."""
        with self._lock:
            try:
                json_files = sorted([f for f in os.listdir(self.queue_dir) if f.endswith(".json") and f.startswith("pending_")])
            except Exception as e:
                log.error("Lỗi đọc thư mục offline queue: %s", e)
                return

            if not json_files:
                return

            log.info("🔄 [OFFLINE QUEUE] Phát hiện %d ảnh chờ upload lại...", len(json_files))
            for json_file in json_files:
                json_path = os.path.join(self.queue_dir, json_file)
                try:
                    with open(json_path, "r", encoding="utf-8") as f:
                        meta = json.load(f)

                    img_filename = meta.get("image_filename")
                    img_path = os.path.join(self.queue_dir, img_filename) if img_filename else None
                    if not img_path or not os.path.exists(img_path):
                        log.warning("Xóa metadata mồ côi: %s", json_file)
                        os.remove(json_path)
                        continue

                    with open(img_path, "rb") as f:
                        image_bytes = f.read()

                    thumb_bytes = None
                    thumb_filename = meta.get("thumb_filename")
                    if thumb_filename:
                        t_path = os.path.join(self.queue_dir, thumb_filename)
                        if os.path.exists(t_path):
                            with open(t_path, "rb") as f:
                                thumb_bytes = f.read()

                    # Thử upload lại
                    ok, media_id = upload_fn(image_bytes, thumb_bytes, meta)
                    if ok:
                        log.info("🎉 [OFFLINE QUEUE SUCCESS] Đã gửi lại ảnh offline thành công! media_id=%s", media_id)
                        try:
                            os.remove(json_path)
                            os.remove(img_path)
                            if thumb_filename:
                                t_path = os.path.join(self.queue_dir, thumb_filename)
                                if os.path.exists(t_path):
                                    os.remove(t_path)
                        except Exception as e:
                            log.warning("Lỗi dọn dẹp file offline: %s", e)
                    else:
                        log.warning("⚠️ Upload lại ảnh offline %s chưa thành công. Sẽ thử lại lần sau...", json_file)
                        break
                except Exception as e:
                    log.error("Lỗi xử lý file offline %s: %s", json_file, e)
