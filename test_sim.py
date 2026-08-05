#!/usr/bin/env python3
"""
Test Script: Kiểm tra và in toàn bộ thông tin SIM, Sóng 4G, Nhà mạng, ICCID & Số ĐT.
Chạy trực tiếp trên CM4: python3 test_sim.py
"""

import sys
import os
import json
import logging

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("test_sim")

from telemetry import get_sim_info

log.info("==========================================================")
log.info("📶 BẮT ĐẦU TEST THU THẬP THÔNG TIN SIM & SÓNG 4G THỰC TẾ")
log.info("==========================================================")

sim_data = get_sim_info(force=True)

print("\n📊 KẾT QUẢ BÓC TÁCH THÔNG TIN SIM:")
print(json.dumps(sim_data, indent=4, ensure_ascii=False))

print("\n📋 TÓM TẮT CHI TIẾT:")
print(f"  - Nguồn thông tin : {sim_data.get('source')}")
print(f"  - Nhà mạng (Op)   : {sim_data.get('operator')}")
print(f"  - Số điện thoại   : {sim_data.get('number')}")
print(f"  - Mã ICCID (Seri) : {sim_data.get('iccid')}")
print(f"  - Cường độ sóng   : {sim_data.get('signal_dbm')} dBm ({sim_data.get('signal_percent')}%)")
print(f"  - Công nghệ mạng  : {sim_data.get('technology')}")
print(f"  - Trạng thái      : {sim_data.get('state')}")

log.info("==========================================================")
log.info("🏁 KẾT THÚC TEST SIM")
log.info("==========================================================")
