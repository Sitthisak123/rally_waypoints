"""
Voice Island Input Bridge
ส่ง Key Events (Push-to-Talk) จาก Global Kernel Listener (evdev) ตรงเข้าไปยังหน้าต่าง Voice Island
แม้ว่าเกม (The Isle) กำลัง Active หรือ Fullscreen อยู่ก็ตาม
"""

import subprocess
import time
import threading
from typing import Optional


class VoiceIslandInputBridge:
    def __init__(self, target_window_title: str = "Voice Island", ptt_key: str = "v"):
        self.target_window_title = target_window_title
        self.ptt_key = ptt_key.lower()
        self._cached_wid: Optional[str] = None
        self._last_wid_search: float = 0.0
        self._is_pressed: bool = False
        self._lock = threading.Lock()

    def get_window_id(self) -> Optional[str]:
        now = time.time()
        # Cache window ID ไว้ 3 วินาที เพื่อประสิทธิภาพสูงสุด ไม่หน่วงระบบ
        if self._cached_wid and (now - self._last_wid_search < 3.0):
            return self._cached_wid

        self._last_wid_search = now
        try:
            # ค้นหา Window ID ด้วย xdotool
            out = subprocess.check_output(
                ["xdotool", "search", "--name", self.target_window_title],
                stderr=subprocess.DEVNULL
            ).decode().strip()
            wids = [w for w in out.splitlines() if w.strip()]
            if wids:
                # เลือกหน้าต่างล่าสุด
                self._cached_wid = wids[-1]
                return self._cached_wid
        except Exception:
            pass

        self._cached_wid = None
        return None

    def send_key_down(self):
        with self._lock:
            if self._is_pressed:
                return
            self._is_pressed = True

        wid = self.get_window_id()
        if not wid:
            return

        try:
            # ส่ง keydown ไปยังหน้าต่าง Voice Island โดยไม่ดึง Focus จากเกม
            subprocess.Popen(
                ["xdotool", "keydown", "--window", wid, self.ptt_key],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            print(f"[Input Bridge] PTT Key '{self.ptt_key.upper()}' DOWN -> Forwarded to Voice Island (WID: {wid})")
        except Exception as e:
            print(f"[Input Bridge] Error sending keydown: {e}")

    def send_key_up(self):
        with self._lock:
            if not self._is_pressed:
                return
            self._is_pressed = False

        wid = self.get_window_id()
        if not wid:
            return

        try:
            # ส่ง keyup ไปยังหน้าต่าง Voice Island
            subprocess.Popen(
                ["xdotool", "keyup", "--window", wid, self.ptt_key],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            print(f"[Input Bridge] PTT Key '{self.ptt_key.upper()}' UP -> Forwarded to Voice Island (WID: {wid})")
        except Exception as e:
            print(f"[Input Bridge] Error sending keyup: {e}")
