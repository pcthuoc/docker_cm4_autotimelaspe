#!/usr/bin/env python3
"""
AutoTimelapse CM4 Agent - Module Watchdog & Thread Manager
------------------------------------------------------------------
Giám sát toàn bộ các luồng (threads) quan trọng của Agent.
Tự động phát hiện và khởi động lại thread bị chết / crash.
Ghi log cảnh báo khi thread bị treo quá lâu (heartbeat timeout).
"""

import threading
import time
import logging
from typing import Callable, Dict

log = logging.getLogger("cm4_watchdog")


class ManagedThread:
    """Metadata của một thread được quản lý bởi Watchdog."""

    def __init__(self, name: str, target_fn: Callable, daemon: bool = True,
                 restart_on_crash: bool = True, heartbeat_timeout: int = 0):
        self.name = name
        self.target_fn = target_fn
        self.daemon = daemon
        self.restart_on_crash = restart_on_crash
        self.heartbeat_timeout = heartbeat_timeout  # giây, 0 = không check
        self.last_heartbeat = time.time()
        self.thread: threading.Thread | None = None
        self.crash_count = 0
        self.start_time = 0.0

    def touch_heartbeat(self):
        """Cập nhật heartbeat, gọi từ bên trong thread đang chạy."""
        self.last_heartbeat = time.time()

    def is_alive(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def is_heartbeat_ok(self) -> bool:
        if self.heartbeat_timeout <= 0 or not self.is_alive():
            return True
        return (time.time() - self.last_heartbeat) <= self.heartbeat_timeout

    def spawn(self, running_flag_fn: Callable[[], bool]):
        """Khởi động thread mới với wrapper tự động heartbeat và crash logging."""
        def _wrapper():
            log.info("🧵 Thread [%s] đã KHỞI ĐỘNG.", self.name)
            self.start_time = time.time()
            self.last_heartbeat = time.time()
            try:
                self.target_fn()
            except Exception as e:
                self.crash_count += 1
                log.error("💥 Thread [%s] CRASH! Lần %d. Lỗi: %s",
                          self.name, self.crash_count, e, exc_info=True)

        self.thread = threading.Thread(
            target=_wrapper, name=self.name, daemon=self.daemon
        )
        self.thread.start()


class ThreadWatchdog:
    """
    Watchdog giám sát và tự động khởi động lại các thread quan trọng.
    Chạy ngầm trong một thread riêng, kiểm tra mỗi CHECK_INTERVAL giây.
    """

    CHECK_INTERVAL = 5      # giây mỗi lần kiểm tra
    MIN_RESTART_AGE = 10    # giây tối thiểu sau khi thread mới start trước khi restart lại

    def __init__(self):
        self._managed: Dict[str, ManagedThread] = {}
        self._running = False
        self._lock = threading.Lock()
        self._running_flag_fn: Callable[[], bool] = lambda: True

    def register(self, managed: ManagedThread):
        """Đăng ký một ManagedThread để Watchdog giám sát."""
        with self._lock:
            self._managed[managed.name] = managed

    def touch(self, name: str):
        """Cập nhật timestamp heartbeat cho thread `name`."""
        with self._lock:
            if name in self._managed:
                self._managed[name].touch_heartbeat()

    def start(self, running_flag_fn: Callable[[], bool]):
        """Khởi động Watchdog daemon thread."""
        self._running = True
        self._running_flag_fn = running_flag_fn

        # Spawn tất cả các thread đã đăng ký lần đầu
        with self._lock:
            for mt in self._managed.values():
                if not mt.is_alive():
                    mt.spawn(running_flag_fn)

        # Bắt đầu vòng lặp giám sát
        t = threading.Thread(target=self._watch_loop, name="watchdog", daemon=True)
        t.start()
        log.info("🐕 Watchdog đã khởi động, giám sát %d threads.", len(self._managed))

    def _watch_loop(self):
        while self._running and self._running_flag_fn():
            time.sleep(self.CHECK_INTERVAL)
            self._check_all()

    def _check_all(self):
        with self._lock:
            for name, mt in self._managed.items():
                # 1. Kiểm tra thread còn sống không
                if not mt.is_alive():
                    if mt.restart_on_crash and self._running_flag_fn():
                        age = time.time() - mt.start_time
                        if age > self.MIN_RESTART_AGE or mt.crash_count == 0:
                            log.warning("⚠️ [WATCHDOG] Thread [%s] đã dừng (crash #%d). Đang khởi động lại...",
                                        name, mt.crash_count)
                            mt.spawn(self._running_flag_fn)
                    continue

                # 2. Kiểm tra heartbeat timeout (thread treo)
                if not mt.is_heartbeat_ok():
                    elapsed = int(time.time() - mt.last_heartbeat)
                    log.warning("⚠️ [WATCHDOG] Thread [%s] KHÔNG CÓ HEARTBEAT trong %ds (timeout=%ds)! Cảnh báo treo.",
                                name, elapsed, mt.heartbeat_timeout)

    def stop(self):
        self._running = False

    def status_report(self) -> dict:
        """Trả về báo cáo trạng thái toàn bộ threads để gắn vào Telemetry."""
        report = {}
        with self._lock:
            for name, mt in self._managed.items():
                last_hb = int(time.time() - mt.last_heartbeat)
                report[name] = {
                    "alive": mt.is_alive(),
                    "crash_count": mt.crash_count,
                    "heartbeat_age_s": last_hb,
                    "hb_ok": mt.is_heartbeat_ok(),
                }
        return report
