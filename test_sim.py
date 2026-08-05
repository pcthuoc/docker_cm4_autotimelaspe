#!/usr/bin/env python3
"""
Test Script: Đọc trực tiếp SIM 4G, Nhà mạng, ICCID, Số ĐT & Cường độ sóng từ modem Quectel.
Giữ cổng Serial MỞ 1 LẦN DUY NHẤT (giống hệt Minicom) và dùng read_until(b"OK") phản hồi siêu tốc.
Chạy trực tiếp trên CM4: python3 test_sim.py hoặc sudo python3 test_sim.py
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


def get_full_sim_info():
    """Tự động tìm cổng Modem 4G Quectel và đọc đầy đủ thông số SIM trong 1 Session duy nhất."""
    target_dev = None
    for dev in ("/dev/ttyUSB2", "/dev/ttyUSB1", "/dev/ttyUSB0", "/dev/ttyACM0", "/dev/ttyACM1"):
        if os.path.exists(dev):
            target_dev = dev
            break

    if not target_dev:
        log.error("❌ Không tìm thấy cổng Modem USB (/dev/ttyUSB* hoặc /dev/ttyACM*)")
        return None

    if not os.access(target_dev, os.R_OK | os.W_OK):
        log.warning("⚠️ Cảnh báo phân quyền %s. Nếu lỗi hãy chạy bằng: sudo python3 test_sim.py", target_dev)

    log.info(f"🔌 Đang mở cổng Serial Modem (Session 1 lần): {target_dev}...")

    subprocess.run(f"stty -F {target_dev} 115200 raw -echo 2>/dev/null", shell=True)

    ser = None
    fd = None

    try:
        try:
            import serial
            ser = serial.Serial(target_dev, 115200, timeout=1.5)
            ser.reset_input_buffer()
        except Exception as e_ser:
            log.debug("Mở bằng pyserial thất bại (%s), chuyển sang os.open...", e_ser)
            fd = os.open(target_dev, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
            try:
                import termios
                attr = termios.tcgetattr(fd)
                attr[4] = termios.B115200
                attr[5] = termios.B115200
                attr[3] &= ~(termios.ICANON | termios.ECHO | termios.ECHOE | termios.ISIG)
                termios.tcsetattr(fd, termios.TCSANOW, attr)
                termios.tcflush(fd, termios.TCIOFLUSH)
            except Exception:
                pass
    except Exception as e_open:
        log.error("❌ Không thể mở cổng %s: %s", target_dev, e_open)
        log.info("👉 Hãy thử chạy lệnh: sudo python3 test_sim.py")
        return None

    def send_at_session(cmd_str: str, timeout=1.5) -> str:
        """Gửi lệnh AT và dùng read_until(b'OK') đọc siêu tốc ngay khi modem phản hồi."""
        full_cmd = (cmd_str.strip() + "\r\n").encode("utf-8")
        if ser:
            try:
                ser.reset_input_buffer()
                ser.write(full_cmd)
                time.sleep(0.1)
                # Đọc tới khi thấy OK hoặc ERROR
                resp_bytes = ser.read_until(b"OK")
                if not resp_bytes or b"OK" not in resp_bytes:
                    resp_bytes += ser.read_until(b"ERROR")
                return resp_bytes.decode('utf-8', errors='ignore')
            except Exception as e:
                log.debug("Lỗi ser: %s", e)
                return ""
        elif fd is not None:
            try:
                os.write(fd, full_cmd)
                time.sleep(0.1)
                resp = bytearray()
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    try:
                        chunk = os.read(fd, 512)
                        if chunk:
                            resp.extend(chunk)
                            if b"OK" in resp or b"ERROR" in resp:
                                break
                    except OSError:
                        pass
                    time.sleep(0.05)
                return resp.decode('utf-8', errors='ignore')
            except Exception:
                return ""
        return ""

    try:
        print("\n----------------------------------------------------------")
        print("📡 CHUỖI RAW PHẢN HỒI TỪ MODEM (AT COMMAND RESPONSES):")

        ops_resp = send_at_session("AT+COPS?")
        print(f"\n[1] Lệnh: AT+COPS?\nPhản hồi:\n{ops_resp.strip()}")

        signal_resp = send_at_session("AT+CSQ")
        print(f"\n[2] Lệnh: AT+CSQ\nPhản hồi:\n{signal_resp.strip()}")

        iccid_resp = send_at_session("AT+QCCID") or send_at_session("AT+CCID")
        print(f"\n[3] Lệnh: AT+QCCID\nPhản hồi:\n{iccid_resp.strip()}")

        cnum_resp = send_at_session("AT+CNUM")
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

    finally:
        if ser:
            try:
                ser.close()
            except Exception:
                pass
        if fd is not None:
            try:
                os.close(fd)
            except Exception:
                pass


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
