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

    # Lấy modem index đầu tiên
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

    operator  = _extract(r"operator name\s*:\s*(.+)", info)
    signal_pct = _extract(r"signal quality\s*:\s*(\d+)", info)
    technology = _extract(r"access tech\s*:\s*(.+)", info)
    state      = _extract(r"state\s*:\s*(.+)", info)

    # Đọc thêm SIM info
    sim_info = _run_cmd(f"mmcli -m {modem_id} --sim 2>/dev/null | head -5")
    number    = _extract(r"number\s*:\s*(.+)", sim_info)
    iccid     = _extract(r"iccid\s*:\s*(.+)", sim_info)

    # Chuyển signal % thành dBm gần đúng (0% ~ -110dBm, 100% ~ -50dBm)
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
    """Thử lấy thông tin mạng qua AT commands trực tiếp trên serial port (non-blocking)."""
    target_dev = None
    for dev in ("/dev/ttyUSB2", "/dev/ttyUSB1", "/dev/ttyUSB0", "/dev/ttyACM0", "/dev/ttyACM1"):
        if os.path.exists(dev):
            target_dev = dev
            break

    if not target_dev:
        return None

    def at_cmd(cmd: str, timeout=1.2) -> str:
        """Gửi lệnh AT an toàn không dùng bash job control (%1) gây treo thread."""
        try:
            cmd_line = f"exec 3<>{target_dev}; stty -F {target_dev} 115200 raw -echo 2>/dev/null; echo -e '{cmd}\\r' >&3; timeout {timeout} cat <&3 2>/dev/null; exec 3>&-"
            return _run_cmd(cmd_line, timeout=timeout + 1.0)
        except Exception:
            return ""

    try:
        ops_resp = at_cmd("AT+COPS?")
        signal_resp = at_cmd("AT+CSQ")

        operator = ""
        m = re.search(r'\+COPS:\s*\d+,\d+,"([^"]+)"', ops_resp)
        if m:
            operator = m.group(1)

        signal_dbm = -99
        m = re.search(r'\+CSQ:\s*(\d+)', signal_resp)
        if m:
            csq = int(m.group(1))
            if csq != 99:
                signal_dbm = -113 + csq * 2

        if not operator and signal_dbm == -99:
            return None

        return {
            "source": "at_command",
            "operator": operator or "Unknown",
            "number": "Unknown",
            "iccid": "Unknown",
            "signal_percent": max(0, min(100, int((signal_dbm + 110) / 0.6))) if signal_dbm != -99 else 0,
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


# ── TỔNG HỢP TELEMETRY ────────────────────────────────────────────────────────

def collect_telemetry(camera_code: str, is_powered: bool, use_real_hw: bool,
                      firmware_version: str = "cm4-autotimelapse-v1.0") -> dict:
    """Thu thập toàn bộ thông tin telemetry thực tế từ phần cứng CM4."""
    sim = get_sim_info()
    net = get_network_info()
    mem = get_memory_info()
    cpu_temp = get_cpu_temperature()
    cpu_pct = get_cpu_percent()
    uptime_s = get_uptime_seconds()

    return {
        # Camera status
        "node": "cm4",
        "camera_code": camera_code,
        "cm4_power_state": "running",
        "camera_gpio_power": "ON" if is_powered else "OFF",
        "camera_hw_mode": "gphoto2_usb" if use_real_hw else "simulated_pil",

        # System resources
        "temperature_c": cpu_temp,
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

        # Placeholder solar/battery (populated by separate sensor if connected)
        "battery_percent": 100,
        "battery_voltage": 12.6,
        "is_charging": True,
        "solar_voltage": 0.0,
        "solar_percent": 100,

        # Metadata
        "firmware_version": firmware_version,
        "ts": datetime.utcnow().isoformat() + "Z",
    }
