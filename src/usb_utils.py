#!/usr/bin/env python3
"""
AutoTimelapse CM4 Agent - Module USB Utilities
------------------------------------------------------------------
Xử lý reset cổng USB phần cứng trên Linux/CM4 khi máy ảnh tắt/bật GPIO
bị kẹt port hoặc lỗi gphoto2 lock device (-60 / -1).
"""

import os
import sys
import fcntl
import logging

log = logging.getLogger("cm4_usb_utils")

USBDEVFS_RESET = 21780

def reset_usb_device_path(dev_path):
    """Gửi tín hiệu USBDEVFS_RESET ioctl tới một USB device node (vd: /dev/bus/usb/001/002)."""
    if not os.path.exists(dev_path):
        return False
    try:
        log.info("🔄 [USB RESET] Đang reset cổng USB phần cứng: %s...", dev_path)
        with open(dev_path, 'w', os.O_WRONLY) as f:
            fcntl.ioctl(f, USBDEVFS_RESET, 0)
        log.info("✅ [USB RESET] Reset cổng USB %s thành công!", dev_path)
        return True
    except Exception as e:
        log.warning("⚠️ Không thể reset USB device %s: %s", dev_path, e)
        return False

def reset_all_camera_usb_devices():
    """Tự động tìm tất cả các thiết bị máy ảnh USB cắm trên CM4 và gửi tín hiệu reset."""
    if not sys.platform.startswith("linux"):
        return False

    usb_bus_dir = "/dev/bus/usb"
    if not os.path.exists(usb_bus_dir):
        return False

    reset_count = 0
    try:
        for bus in os.listdir(usb_bus_dir):
            bus_path = os.path.join(usb_bus_dir, bus)
            if not os.path.isdir(bus_path):
                continue
            for dev in os.listdir(bus_path):
                dev_path = os.path.join(bus_path, dev)
                if dev == "001":
                    continue
                if reset_usb_device_path(dev_path):
                    reset_count += 1
    except Exception as e:
        log.warning("Lỗi quét cổng USB: %s", e)

    return reset_count > 0
