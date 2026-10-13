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
    def __init__(self, target_window_title: str = "Voice Island v.*", ptt_key: str = "v"):
        self.target_window_title = target_window_title
        self.ptt_key = ptt_key.lower()
        self._cached_wids: list = []
        self._last_wid_search: float = 0.0
        self._is_pressed: bool = False
        self._lock = threading.Lock()

    def get_window_ids(self) -> list:
        now = time.time()
        # Cache window IDs ไว้ 2.5 วินาที เพื่อประสิทธิภาพสูงสุด
        if self._cached_wids and (now - self._last_wid_search < 2.5):
            return self._cached_wids

        self._last_wid_search = now
        found_wids = []

        # ลำดับ patterns ที่จะค้นหาให้ตรงกับชื่อหน้าต่าง Voice Island v* หรือ Voice Island v0.2.5
        search_patterns = [
            "Voice Island v",
            "Voice Island v0.2.5",
            self.target_window_title,
            "Voice Island"
        ]

        for pat in search_patterns:
            try:
                out = subprocess.check_output(
                    ["xdotool", "search", "--name", pat],
                    stderr=subprocess.DEVNULL
                ).decode().strip()
                wids = [w.strip() for w in out.splitlines() if w.strip()]
                if wids:
                    found_wids = wids
                    break
            except Exception:
                continue

        self._cached_wids = found_wids
        return self._cached_wids

    def get_window_id(self) -> Optional[str]:
        wids = self.get_window_ids()
        return wids[-1] if wids else None

    def send_key_down(self):
        with self._lock:
            if self._is_pressed:
                return
            self._is_pressed = True

        wids = self.get_window_ids()
        if not wids:
            return

        for wid in wids:
            try:
                # ส่ง keydown ไปยังหน้าต่าง Voice Island โดยไม่ดึง Focus จากเกม
                subprocess.Popen(
                    ["xdotool", "keydown", "--window", wid, self.ptt_key],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )
                print(f"[Input Bridge] PTT Key '{self.ptt_key.upper()}' DOWN -> Forwarded to Voice Island (WID: {wid})")
            except Exception as e:
                print(f"[Input Bridge] Error sending keydown to WID {wid}: {e}")

    def send_key_up(self):
        with self._lock:
            if not self._is_pressed:
                return
            self._is_pressed = False

        wids = self.get_window_ids()
        if not wids:
            return

        for wid in wids:
            try:
                # ส่ง keyup ไปยังหน้าต่าง Voice Island
                subprocess.Popen(
                    ["xdotool", "keyup", "--window", wid, self.ptt_key],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )
                print(f"[Input Bridge] PTT Key '{self.ptt_key.upper()}' UP -> Forwarded to Voice Island (WID: {wid})")
            except Exception as e:
                print(f"[Input Bridge] Error sending keyup to WID {wid}: {e}")
