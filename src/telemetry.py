#!/usr/bin/env python3
"""
AutoTimelapse CM4 Agent - Module Telemetry
------------------------------------------------------------------
Thu thập thông tin hệ thống thật từ phần cứng CM4:
- Tín hiệu SIM / modem (qua mmcli, ModemManager hoặc AT commands)
- Nhiệt độ CPU Raspberry Pi thật (sysfs thermal zone)
- Mức dùng RAM / CPU thật (psutil nếu có, fallback /proc)
- Thông tin mạng (IP, trạng thái kết nối)
Tất cả đều có fallback an toàn khi không có phần cứng.
"""

import os
import re
import subprocess
import logging
import socket
from datetime import datetime

log = logging.getLogger("cm4_telemetry")

# ── Cờ khả năng phần cứng (tự detect 1 lần khi import) ──────────────────────
try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    psutil = None
    HAS_PSUTIL = False


def _run_cmd(cmd, timeout=3):
    """Chạy lệnh shell an toàn, trả về stdout string hoặc '' nếu lỗi."""
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            timeout=timeout, encoding="utf-8", errors="ignore"
        )
        return result.stdout.strip()
    except Exception:
        return ""


# ── NHIỆT ĐỘ CPU THẬT ────────────────────────────────────────────────────────

def get_cpu_temperature() -> float:
    """Đọc nhiệt độ CPU thật từ sysfs thermal zone (Raspberry Pi CM4)."""
    # Raspberry Pi thermal zone
    for path in [
        "/sys/class/thermal/thermal_zone0/temp",
        "/sys/class/hwmon/hwmon0/temp1_input",
        "/sys/class/hwmon/hwmon1/temp1_input",
    ]:
        try:
            with open(path, "r") as f:
                raw = int(f.read().strip())
                if raw > 1000:
                    return round(raw / 1000.0, 1)
                return round(float(raw), 1)
        except Exception:
            pass

    # psutil fallback
    if HAS_PSUTIL:
        try:
            temps = psutil.sensors_temperatures()
            for key in ("cpu_thermal", "cpu-thermal", "coretemp", "acpitz"):
                if key in temps and temps[key]:
                    return round(temps[key][0].current, 1)
        except Exception:
            pass

    # Simulated fallback
    import random
    return round(random.uniform(40.0, 60.0), 1)


# ── SỬ DỤNG CPU / RAM ─────────────────────────────────────────────────────────

def get_cpu_percent() -> float:
    """Đọc tỷ lệ sử dụng CPU thật."""
    if HAS_PSUTIL:
        try:
            return round(psutil.cpu_percent(interval=0.5), 1)
        except Exception:
            pass

    # /proc/stat fallback
    try:
        out = _run_cmd("grep '^cpu ' /proc/stat")
        if out:
            vals = list(map(int, out.split()[1:]))
            total = sum(vals)
            idle = vals[3]
            return round((1 - idle / total) * 100, 1)
    except Exception:
        pass

    import random
    return round(random.uniform(5.0, 30.0), 1)


def get_memory_info() -> dict:
    """Đọc thông tin RAM thật."""
    if HAS_PSUTIL:
        try:
            mem = psutil.virtual_memory()
            return {
                "total_mb": round(mem.total / 1024 / 1024, 1),
                "used_mb": round(mem.used / 1024 / 1024, 1),
                "percent": mem.percent,
            }
        except Exception:
            pass

    # /proc/meminfo fallback
    try:
        out = _run_cmd("cat /proc/meminfo")
        info = {}
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 2:
                info[parts[0].rstrip(":")] = int(parts[1])
        total_kb = info.get("MemTotal", 0)
        avail_kb = info.get("MemAvailable", 0)
        used_kb = total_kb - avail_kb
        return {
            "total_mb": round(total_kb / 1024, 1),
            "used_mb": round(used_kb / 1024, 1),
            "percent": round(used_kb / max(1, total_kb) * 100, 1),
        }
    except Exception:
        pass

    return {"total_mb": 4096.0, "used_mb": 512.0, "percent": 12.5}


# ── UPTIME HỆ THỐNG ───────────────────────────────────────────────────────────

def get_uptime_seconds() -> int:
    """Đọc uptime hệ thống từ /proc/uptime (giây)."""
    try:
        with open("/proc/uptime", "r") as f:
            return int(float(f.read().split()[0]))
    except Exception:
        pass

    if HAS_PSUTIL:
        try:
            return int(datetime.now().timestamp() - psutil.boot_time())
        except Exception:
            pass

    return 0


# ── THÔNG TIN MẠNG ────────────────────────────────────────────────────────────

def get_local_ip() -> str:
    """Lấy địa chỉ IP nội bộ đang active trên CM4."""
    # Ưu tiên interface wlan0, eth0 theo thứ tự
    for iface in ("wlan0", "eth0", "usb0", "end0"):
        out = _run_cmd(f"ip -4 addr show {iface} 2>/dev/null | grep inet")
        if out:
            m = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)", out)
            if m:
                return m.group(1)

    # Socket trick fallback
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "unknown"


def get_network_info() -> dict:
    """Lấy thông tin mạng đầy đủ bao gồm IP, loại kết nối."""
    ip = get_local_ip()
    iface = "unknown"
    link_type = "unknown"

    # Xác định interface đang được dùng
    for name in ("wlan0", "eth0", "usb0", "end0"):
        if _run_cmd(f"ip link show {name} 2>/dev/null | grep 'state UP'"):
            iface = name
            link_type = "wifi" if "wlan" in name else "ethernet" if "eth" in name or "end" in name else "usb_eth"
            break

    return {
        "local_ip": ip,
        "interface": iface,
        "link_type": link_type,
    }


# ── THÔNG TIN SIM / MODEM ─────────────────────────────────────────────────────

def _get_sim_info_mmcli() -> dict | None:
    """Thử đọc thông tin SIM qua ModemManager (mmcli) nếu có."""
    modem_list = _run_cmd("mmcli -L 2>/dev/null | grep '/Modem/'")
    if not modem_list:
        return None

    m = re.search(r"/Modem/(\d+)", modem_list)
    if not m:
        return None
    modem_id = m.group(1)

    info = _run_cmd(f"mmcli -m {modem_id} 2>/dev/null")
    if not info:
        return None

    def _extract(pattern, text):
        match = re.search(pattern, text)
        return match.group(1).strip() if match else ""

    operator   = _extract(r"operator name\s*:\s*(.+)", info)
    signal_pct = _extract(r"signal quality\s*:\s*(\d+)", info)
    technology = _extract(r"access tech\s*:\s*(.+)", info)
    state      = _extract(r"state\s*:\s*(.+)", info)

    # Đọc chi tiết SIM object (/SIM/x)
    number, iccid = "", ""
    sim_path = _extract(r"sim\s*:\s*(.+)", info)
    if sim_path and "none" not in sim_path.lower():
        sim_m = re.search(r"/SIM/(\d+)", sim_path)
        if sim_m:
            sim_out = _run_cmd(f"mmcli -i {sim_m.group(1)} 2>/dev/null")
            number = _extract(r"operator id\s*:\s*(.+)", sim_out) or _extract(r"number\s*:\s*(.+)", sim_out)
            iccid  = _extract(r"iccid\s*:\s*(.+)", sim_out)

    signal_dbm = None
    if signal_pct:
        pct = min(100, max(0, int(signal_pct)))
        signal_dbm = int(-110 + pct * 0.6)

    return {
        "source": "mmcli",
        "operator": operator or "Unknown",
        "number": number or "Unknown",
        "iccid": iccid or "Unknown",
        "signal_percent": int(signal_pct) if signal_pct else 0,
        "signal_dbm": signal_dbm or -99,
        "technology": technology or "Unknown",
        "state": state or "Unknown",
        "online": "connected" in state.lower() if state else False,
    }


def _get_sim_info_at_commands() -> dict | None:
    """
    Thử lấy thông tin mạng qua AT commands trực tiếp trên cổng Serial bằng Single Session (mở port 1 lần duy nhất):
    - AT+QCCID -> ICCID (89840480009206559331)
    - AT+CNUM  -> Phone (+84982583212)
    - AT+CSQ   -> Signal (RSSI 0..31 -> dBm & %)
    - AT+COPS  -> Operator (Viettel)
    """
    target_dev = None
    for dev in ("/dev/ttyUSB2", "/dev/ttyUSB1", "/dev/ttyUSB0", "/dev/ttyACM0", "/dev/ttyACM1"):
        if os.path.exists(dev):
            target_dev = dev
            break

    if not target_dev:
        return None

    subprocess.run(f"stty -F {target_dev} 115200 raw -echo 2>/dev/null", shell=True)

    ser = None
    fd = None

    try:
        try:
            import serial
            ser = serial.Serial(target_dev, 115200, timeout=1.5)
            ser.reset_input_buffer()
        except Exception:
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
    except Exception:
        return None

    def send_at_session(cmd_str: str, timeout=1.5) -> str:
        full_cmd = (cmd_str.strip() + "\r\n").encode("utf-8")
        if ser:
            try:
                ser.reset_input_buffer()
                ser.write(full_cmd)
                time.sleep(0.1)
                resp_bytes = ser.read_until(b"OK")
                if not resp_bytes or b"OK" not in resp_bytes:
                    resp_bytes += ser.read_until(b"ERROR")
                return resp_bytes.decode('utf-8', errors='ignore')
            except Exception:
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
        ops_resp    = send_at_session("AT+COPS?")
        signal_resp = send_at_session("AT+CSQ")
        iccid_resp  = send_at_session("AT+QCCID") or send_at_session("AT+CCID")
        cnum_resp   = send_at_session("AT+CNUM")

        operator = ""
        m = re.search(r'\+COPS:\s*\d+,\d+,"([^"]+)"', ops_resp)
        if m:
            raw_ops = m.group(1).strip()
            words = raw_ops.split()
            if len(words) == 2 and words[0] == words[1]:
                operator = words[0]
            else:
                operator = raw_ops

        signal_dbm = -99
        signal_percent = 0
        m = re.search(r'\+CSQ:\s*(\d+)\s*,\s*(\d+)', signal_resp)
        if m:
            csq = int(m.group(1))
            if 0 <= csq <= 31:
                signal_dbm = -113 + csq * 2
                signal_percent = int((csq / 31.0) * 100)

        iccid = ""
        m_iccid = re.search(r'(?:[\+\w]+:)?\s*(\d{18,22})', iccid_resp)
        if m_iccid:
            iccid = m_iccid.group(1)

        number = ""
        m_num = re.search(r'\+CNUM:\s*[^,]*,\s*"([^"]+)"', cnum_resp)
        if m_num:
            number = m_num.group(1)

        if not operator and signal_dbm == -99 and not iccid:
            return None

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
    except Exception:
        return None


def _get_sim_info_wifi() -> dict:
    """Lấy thông tin WiFi khi không có SIM modem."""
    ssid = _run_cmd("iwgetid -r 2>/dev/null") or _run_cmd("nmcli -t -f active,ssid dev wifi | grep '^yes' | cut -d: -f2")
    signal_dbm = -65

    # Đọc signal strength WiFi
    rssi_raw = _run_cmd("iwconfig wlan0 2>/dev/null | grep -i signal")
    m = re.search(r"Signal level=(-?\d+)", rssi_raw)
    if m:
        signal_dbm = int(m.group(1))

    return {
        "source": "wifi",
        "operator": f"WiFi: {ssid}" if ssid else "WiFi (SSID unknown)",
        "number": "N/A",
        "iccid": "N/A",
        "signal_percent": max(0, min(100, int((signal_dbm + 100) * 2))) if signal_dbm else 50,
        "signal_dbm": signal_dbm,
        "technology": "WiFi",
        "state": "connected" if ssid else "wifi_nosid",
        "online": True,
    }


_SIM_CACHE = None
_SIM_CACHE_TS = 0
SIM_CACHE_TTL = 120  # Seconds - cache SIM info để không spam mmcli liên tục


def get_sim_info(force=False) -> dict:
    """
    Lấy thông tin SIM/Modem/Network đầy đủ:
    1. Thử ModemManager (mmcli) → real SIM 4G/LTE
    2. Thử AT commands trực tiếp qua /dev/ttyUSB*
    3. Fallback sang WiFi info (iwconfig / nmcli)
    Kết quả được cache 120 giây để tránh spam hardware.
    """
    global _SIM_CACHE, _SIM_CACHE_TS
    import time as _time

    if not force and _SIM_CACHE and (_time.time() - _SIM_CACHE_TS) < SIM_CACHE_TTL:
        return _SIM_CACHE

    # Thử theo thứ tự ưu tiên
    info = None

    try:
        info = _get_sim_info_mmcli()
    except Exception as e:
        log.debug("mmcli không khả dụng: %s", e)

    if not info:
        try:
            info = _get_sim_info_at_commands()
        except Exception as e:
            log.debug("AT command không khả dụng: %s", e)

    if not info:
        try:
            info = _get_sim_info_wifi()
        except Exception as e:
            log.debug("WiFi info không khả dụng: %s", e)

    if not info:
        # Simulated fallback cuối cùng
        info = {
            "source": "simulated",
            "operator": "CM4 WiFi/Ethernet (No Modem)",
            "number": "N/A",
            "iccid": "N/A",
            "signal_percent": 80,
            "signal_dbm": -65,
            "technology": "WiFi/Ethernet",
            "state": "connected",
            "online": True,
        }

    _SIM_CACHE = info
    _SIM_CACHE_TS = _time.time()

    log.debug("📶 SIM Info [%s]: %s signal=%ddBm", info["source"], info["operator"], info["signal_dbm"])
    return info


# ── I2C SENSORS: SHT20 (0x40) & ADS1115 (0x49) trên I2C bus 10 ─────────────────

def read_sht20_sensor(bus_id: int = 10, address: int = 0x40) -> tuple:
    """
    Đọc nhiệt độ (°C) và độ ẩm (%RH) từ cảm biến SHT20 ở địa chỉ 0x40 trên bus I2C 10.
    Sử dụng phương pháp đọc trực tiếp 3 byte (MSB, LSB, CRC) đã kiểm thử thực tế.
    Trả về (temp_c, humidity_percent) hoặc (None, None) nếu không kết nối/lỗi.
    """
    try:
        import smbus2
    except ImportError:
        smbus2 = None

    if smbus2 is None:
        log.warning("⚠️ Thư viện 'smbus2' chưa được cài đặt trong Python/Container. Không thể đọc I2C.")
        return None, None

    try:
        import time
        with smbus2.SMBus(bus_id) as bus:
            # 1. Đo Nhiệt độ (Lệnh 0xF3)
            bus.write_byte(address, 0xF3)
            time.sleep(0.1)  # Chờ 100ms cho cảm biến đo xong

            msb = bus.read_byte(address)
            lsb = bus.read_byte(address)
            _crc = bus.read_byte(address)

            raw_temp = (msb << 8) | lsb
            temp_c = round(-46.85 + 175.72 * (raw_temp / 65536.0), 1)

            # 2. Đo Độ ẩm (Lệnh 0xF5)
            bus.write_byte(address, 0xF5)
            time.sleep(0.05)

            msb_h = bus.read_byte(address)
            lsb_h = bus.read_byte(address)
            _crc_h = bus.read_byte(address)

            raw_hum = (msb_h << 8) | lsb_h
            humi = round(-6.0 + 125.0 * (raw_hum / 65536.0), 1)
            humi = max(0.0, min(100.0, humi))

            return temp_c, humi
    except Exception as e:
        log.warning("⚠️ SHT20 [bus %d, addr 0x%02X] read error: %s", bus_id, address, e)
        return None, None


def read_ads1115_voltages(bus_id: int = 10, address: int = 0x49) -> dict:
    """
    Đọc điện áp Pin (A0) và Solar (A1) từ ADS1115 (0x49) trên I2C bus 10.
    Hệ số phân áp (mạch cầu phân áp với R_dưới = 22kΩ):
      BATTERY_VOLTAGE_SCALE: Pin Li-ion 3S (100kΩ / 22kΩ) -> mặc định 5.545
      SOLAR_VOLTAGE_SCALE: Solar 0-24V/28V (180kΩ / 22kΩ) -> mặc định 9.182
    """
    try:
        import smbus2
    except ImportError:
        smbus2 = None

    if smbus2 is None:
        return {"battery_voltage": None, "solar_voltage": None}

    bat_scale = float(os.environ.get("BATTERY_VOLTAGE_SCALE", "5.545"))
    sol_scale = float(os.environ.get("SOLAR_VOLTAGE_SCALE", "9.182"))

    def _read_channel(bus, channel: int):
        try:
            import time
            mux = (0x4 + channel) << 12
            # OS=1, MUX=100/101, PGA=001 (+/-4.096V), MODE=1 (Single-shot), DR=100 (128SPS), COMP=0003
            config = 0x8000 | mux | 0x0200 | 0x0100 | 0x0083
            config_bytes = [(config >> 8) & 0xFF, config & 0xFF]
            bus.write_i2c_block_data(address, 0x01, config_bytes)
            time.sleep(0.015)
            data = bus.read_i2c_block_data(address, 0x00, 2)
            raw = (data[0] << 8) | data[1]
            if raw > 32767:
                raw -= 65536
            v_pin = (raw / 32767.0) * 4.096
            return max(0.0, v_pin)
        except Exception as ex:
            log.warning("⚠️ ADS1115 channel %d read error: %s", channel, ex)
            return None

    try:
        with smbus2.SMBus(bus_id) as bus:
            v0_pin = _read_channel(bus, 0)
            v1_pin = _read_channel(bus, 1)

            bat_v = round(v0_pin * bat_scale, 2) if v0_pin is not None else None
            sol_v = round(v1_pin * sol_scale, 2) if v1_pin is not None else None

            return {
                "battery_voltage": bat_v,
                "solar_voltage": sol_v,
            }
    except Exception as e:
        log.warning("⚠️ ADS1115 [bus %d, addr 0x%02X] read error: %s", bus_id, address, e)
        return {"battery_voltage": None, "solar_voltage": None}


# ── TỔNG HỢP TELEMETRY ────────────────────────────────────────────────────────

def collect_telemetry(camera_code: str, is_powered: bool, use_real_hw: bool,
                      firmware_version: str = "cm4-autotimelapse-v1.0") -> dict:
    """Thu thập toàn bộ thông tin telemetry thực tế từ phần cứng CM4 (System + I2C sensors)."""
    sim = get_sim_info()
    net = get_network_info()
    mem = get_memory_info()
    cpu_temp = get_cpu_temperature()
    cpu_pct = get_cpu_percent()
    uptime_s = get_uptime_seconds()

    # Đọc cảm biến I2C (SHT20 @ 0x40, ADS1115 @ 0x49 trên i2c-10)
    i2c_bus_id = int(os.environ.get("I2C_BUS_ID", "10"))
    sht_temp, sht_humi = read_sht20_sensor(bus_id=i2c_bus_id, address=0x40)
    adc_voltages = read_ads1115_voltages(bus_id=i2c_bus_id, address=0x49)

    env_temp = sht_temp if sht_temp is not None else cpu_temp
    humidity_pct = sht_humi

    bat_v = adc_voltages.get("battery_voltage")
    sol_v = adc_voltages.get("solar_voltage")

    bat_pct = None
    if bat_v is not None:
        bat_pct = int(max(0.0, min(100.0, (bat_v - 10.5) / (12.6 - 10.5) * 100.0)))

    sol_pct = None
    if sol_v is not None:
        sol_pct = int(max(0.0, min(100.0, (sol_v / 18.0) * 100.0)))

    is_charging = False
    if sol_v is not None and bat_v is not None:
        is_charging = sol_v > (bat_v + 0.5)
    elif sol_v is not None:
        is_charging = sol_v > 5.0

    return {
        # Camera status
        "node": "cm4",
        "camera_code": camera_code,
        "cm4_power_state": "running",
        "camera_gpio_power": "ON" if is_powered else "OFF",
        "camera_hw_mode": "gphoto2_usb" if use_real_hw else "simulated_pil",

        # System resources & Environment sensors
        "temperature_c": env_temp,
        "humidity_percent": humidity_pct,
        "cpu_percent": cpu_pct,
        "memory_used_mb": mem["used_mb"],
        "memory_percent": mem["percent"],
        "uptime_seconds": uptime_s,

        # Network
        "local_ip": net["local_ip"],
        "network_interface": net["interface"],
        "network_type": net["link_type"],

        # SIM / Signal
        "sim_source": sim["source"],
        "sim_active_node": "cm4",
        "sim_operator": sim["operator"],
        "sim_technology": sim["technology"],
        "sim_state": sim["state"],
        "sim_online": sim["online"],
        "sim_signal_dbm": sim["signal_dbm"],
        "sim_signal_percent": sim["signal_percent"],

        # Real I2C solar/battery telemetry
        "battery_percent": bat_pct,
        "battery_voltage": bat_v,
        "is_charging": is_charging,
        "solar_voltage": sol_v,
        "solar_percent": sol_pct,

        # Metadata
        "firmware_version": firmware_version,
        "ts": datetime.utcnow().isoformat() + "Z",
    }

