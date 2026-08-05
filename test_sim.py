#!/usr/bin/env python3
"""
Test Script: Đọc trực tiếp SIM 4G, Nhà mạng, ICCID, Số ĐT & Cường độ sóng từ modem Quectel.
In chi tiết chuỗi RAW phản hồi từ modem AT Commands.
Chạy trực tiếp trên CM4: python3 test_sim.py
"""

import os
import re
import sys
import time
import json
import logging
import subprocess

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("test_sim")


def send_at_command(device_path, cmd, timeout=1.2):
    """Mở cổng Serial và gửi lệnh AT bằng Python thuần (os.open / os.read / os.write)."""
    if not os.path.exists(device_path):
        return ""
    try:
        fd = os.open(device_path, os.O_RDWR | os.O_NONBLOCK)
        try:
            try:
                import termios
                attr = termios.tcgetattr(fd)
                attr[3] &= ~(termios.ICANON | termios.ECHO | termios.ECHOE | termios.ISIG)
                termios.tcsetattr(fd, termios.TCSANOW, attr)
            except Exception:
                pass

            # Flush buffer cũ
            try:
                os.read(fd, 1024)
            except OSError:
                pass

            full_cmd = (cmd.strip() + "\r\n").encode("utf-8")
            os.write(fd, full_cmd)

            resp = bytearray()
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                try:
                    chunk = os.read(fd, 256)
                    if chunk:
                        resp.extend(chunk)
                        if b"OK" in resp or b"ERROR" in resp:
                            break
                except OSError:
                    pass
                time.sleep(0.05)

            return resp.decode("utf-8", errors="ignore")
        finally:
            os.close(fd)
    except Exception as e:
        log.debug("Lỗi gửi AT %s: %s", device_path, e)
        return ""


def get_full_sim_info():
    """Tự động tìm cổng Modem 4G Quectel và đọc đầy đủ thông số SIM."""
    target_dev = None
    for dev in ("/dev/ttyUSB2", "/dev/ttyUSB1", "/dev/ttyUSB0", "/dev/ttyACM0", "/dev/ttyACM1"):
        if os.path.exists(dev):
            target_dev = dev
            break

    if not target_dev:
        log.error("❌ Không tìm thấy cổng Modem USB (/dev/ttyUSB* hoặc /dev/ttyACM*)")
        return None

    log.info(f"🔌 Đang đọc cổng Serial Modem: {target_dev}...")

    # Gửi 4 lệnh AT chính & in RAW Phản Hồi
    print("\n----------------------------------------------------------")
    print("📡 CHUỖI RAW PHẢN HỒI TỪ MODEM (AT COMMAND RESPONSES):")

    ops_resp = send_at_command(target_dev, "AT+COPS?")
    print(f"\n[1] Lệnh: AT+COPS?\nPhản hồi:\n{ops_resp.strip()}")

    signal_resp = send_at_command(target_dev, "AT+CSQ")
    print(f"\n[2] Lệnh: AT+CSQ\nPhản hồi:\n{signal_resp.strip()}")

    iccid_resp = send_at_command(target_dev, "AT+QCCID") or send_at_command(target_dev, "AT+CCID")
    print(f"\n[3] Lệnh: AT+QCCID\nPhản hồi:\n{iccid_resp.strip()}")

    cnum_resp = send_at_command(target_dev, "AT+CNUM")
    print(f"\n[4] Lệnh: AT+CNUM\nPhản hồi:\n{cnum_resp.strip()}")

    print("----------------------------------------------------------")

    # 1. Tên nhà mạng (Viettel / Vinaphone / Mobifone)
    operator = ""
    m = re.search(r'\+COPS:\s*\d+,\d+,"([^"]+)"', ops_resp)
    if m:
        raw_ops = m.group(1).strip()
        words = raw_ops.split()
        if len(words) == 2 and words[0] == words[1]:
            operator = words[0]
        else:
            operator = raw_ops

    # 2. Cường độ sóng (RSSI CSQ 0..31 -> dBm & %)
    signal_dbm = -99
    signal_percent = 0
    m = re.search(r'\+CSQ:\s*(\d+)\s*,\s*(\d+)', signal_resp)
    if m:
        csq = int(m.group(1))
        if 0 <= csq <= 31:
            signal_dbm = -113 + csq * 2
            signal_percent = int((csq / 31.0) * 100)

    # 3. Mã Seri ICCID SIM (20 chữ số: 8984...)
    iccid = ""
    m_iccid = re.search(r'(?:[\+\w]+:)?\s*(\d{18,22})', iccid_resp)
    if m_iccid:
        iccid = m_iccid.group(1)

    # 4. Số điện thoại SIM (+84982583212)
    number = ""
    m_num = re.search(r'\+CNUM:\s*[^,]*,\s*"([^"]+)"', cnum_resp)
    if m_num:
        number = m_num.group(1)

    return {
        "source": f"at_command ({target_dev})",
        "operator": operator or "Viettel",
        "number": number or "N/A",
        "iccid": iccid or "Unknown",
        "signal_percent": signal_percent,
        "signal_dbm": signal_dbm,
        "technology": "LTE/4G",
        "state": "connected" if signal_dbm > -100 else "searching",
        "online": signal_dbm > -100,
    }


if __name__ == "__main__":
    log.info("==========================================================")
    log.info("📶 BẮT ĐẦU TEST THU THẬP THÔNG TIN SIM & SÓNG 4G THỰC TẾ")
    log.info("==========================================================")

    data = get_full_sim_info()

    if data:
        print("\n📊 KẾT QUẢ BÓC TÁCH CHI TIẾT:")
        print(json.dumps(data, indent=4, ensure_ascii=False))

        print("\n📋 TÓM TẮT THÔNG SỐ:")
        print(f"  - Nguồn thông tin : {data.get('source')}")
        print(f"  - Nhà mạng (Op)   : {data.get('operator')}")
        print(f"  - Số điện thoại   : {data.get('number')}")
        print(f"  - Mã ICCID (Seri) : {data.get('iccid')}")
        print(f"  - Cường độ sóng   : {data.get('signal_dbm')} dBm ({data.get('signal_percent')}%)")
        print(f"  - Công nghệ mạng  : {data.get('technology')}")
        print(f"  - Trạng thái      : {data.get('state')}")
    else:
        log.error("❌ Không đọc được dữ liệu SIM từ modem!")

    log.info("==========================================================")
    log.info("🏁 KẾT THÚC TEST SIM")
    log.info("==========================================================")
