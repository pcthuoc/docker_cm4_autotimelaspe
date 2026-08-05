#!/usr/bin/env python3
"""
AutoTimelapse CM4 Agent - Module USB Utilities
------------------------------------------------------------------
Xử lý reset cổng USB phần cứng trên Linux/CM4 CHỈ CHO MÁY ẢNH (Nikon/Canon/Sony).
Tuyệt đối BẢO VỆ và KHÔNG reset Card mạng USB / 4G Modem / Tailscale interfaces.
"""

import os
import sys
import fcntl
import logging

log = logging.getLogger("cm4_usb_utils")

USBDEVFS_RESET = 21780

# Danh sách Vendor ID USB của các hãng máy ảnh chính
CAMERA_VENDOR_IDS = {
    "04b0",  # Nikon Corporation
    "04a9",  # Canon Inc.
    "054c",  # Sony Corp.
    "04cb",  # Fujifilm Corp.
    "07b4",  # Olympus Corp.
    "04da",  # Panasonic Corp.
}


def is_camera_usb_device(dev_path: str) -> bool:
    """
    Kiểm tra xem thiết bị USB tại dev_path (vd: /dev/bus/usb/001/002) có phải MÁY ẢNH hay không.
    Bảo vệ 100% card mạng USB (4G Modem, Ethernet USB, USB WiFi) không bao giờ bị reset nhầm.
    """
    try:
        parts = dev_path.split("/")
        if len(parts) >= 2:
            bus_num = int(parts[-2])
            dev_num = int(parts[-1])

            sys_usb_dir = "/sys/bus/usb/devices"
            if os.path.exists(sys_usb_dir):
                for dev_name in os.listdir(sys_usb_dir):
                    dev_sys_path = os.path.join(sys_usb_dir, dev_name)
                    vendor_file = os.path.join(dev_sys_path, "idVendor")
                    busnum_file = os.path.join(dev_sys_path, "busnum")
                    devnum_file = os.path.join(dev_sys_path, "devnum")

                    if os.path.exists(vendor_file) and os.path.exists(busnum_file) and os.path.exists(devnum_file):
                        try:
                            with open(busnum_file, "r") as f:
                                b = int(f.read().strip())
                            with open(devnum_file, "r") as f:
                                d = int(f.read().strip())
                            if b == bus_num and d == dev_num:
                                with open(vendor_file, "r") as f:
                                    vendor_id = f.read().strip().lower()
                                if vendor_id in CAMERA_VENDOR_IDS:
                                    return True
                                else:
                                    log.debug("🛡️ Bảo vệ thiết bị mạng/USB khác (VendorID: %s, bus: %d, dev: %d)", vendor_id, b, d)
                                    return False
                        except Exception:
                            pass
    except Exception:
        pass

    # Fallback: Đọc 18-byte USB Device Descriptor trực tiếp từ file node
    try:
        with open(dev_path, "rb") as f:
            desc = f.read(18)
            if len(desc) >= 18:
                vendor_id = f"{desc[9]:02x}{desc[8]:02x}".lower()
                if vendor_id in CAMERA_VENDOR_IDS:
                    return True
    except Exception:
        pass

    return False


def reset_usb_device_path(dev_path: str) -> bool:
    """Gửi tín hiệu USBDEVFS_RESET ioctl nếu thiết bị đúng là MÁY ẢNH."""
    if not os.path.exists(dev_path):
        return False

    if not is_camera_usb_device(dev_path):
        return False

    try:
        log.info("🔄 [USB RESET] Reset cổng USB máy ảnh phần cứng: %s...", dev_path)
        with open(dev_path, 'w', os.O_WRONLY) as f:
            fcntl.ioctl(f, USBDEVFS_RESET, 0)
        log.info("✅ [USB RESET] Reset máy ảnh %s thành công!", dev_path)
        return True
    except Exception as e:
        log.warning("⚠️ Không thể reset USB máy ảnh %s: %s", dev_path, e)
        return False


def reset_all_camera_usb_devices() -> bool:
    """Tự động tìm và chỉ reset các thiết bị MÁY ẢNH USB cắm trên CM4."""
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
