"""
HTTP Telemetry Poller & Live Synchronizer for Voice-Island
ดึงพิกัดผู้เล่นจาก https://api.voice-island.com/api/players พร้อมรองรับ Cookie, Bearer Token,
และรายงานสถานะการเชื่อมต่อ (Connected, Unauthorized, Offline, Latency)
"""

import time
import json
import threading
import urllib.request
import urllib.error
from typing import Callable, Optional, Dict, Tuple


class VoiceIslandPoller:
    """
    Poller สำหรับดึงข้อมูลพิกัดผู้เล่นแบบ HTTP Polling พร้อมระบบจัดการ Session Auth
    """
    DEFAULT_API_URL = "https://api.voice-island.com/api/players"

    def __init__(
        self,
        api_url: Optional[str] = None,
        interval_sec: float = 2.0,
        cookie_header: Optional[str] = None,
        auth_token: Optional[str] = None
    ):
        self.api_url = api_url or self.DEFAULT_API_URL
        self.interval_sec = interval_sec
        self.cookie_header = cookie_header or ""
        self.auth_token = auth_token or ""
        self.is_running = False
        self._thread: Optional[threading.Thread] = None

        # Callbacks
        self.on_data_callback: Optional[Callable[[Dict], None]] = None
        self.on_status_callback: Optional[Callable[[str, str, int], None]] = None

        # Live State
        self.status = "idle"  # "connected", "unauthorized", "offline", "idle"
        self.status_msg = "ยังไม่ได้เชื่อมต่อ"
        self.last_latency_ms = 0
        self.last_sync_time = 0.0
        self.last_player_count = 0

    def configure(
        self,
        api_url: Optional[str] = None,
        cookie_header: Optional[str] = None,
        auth_token: Optional[str] = None,
        interval_sec: Optional[float] = None
    ):
        """อัปเดตการตั้งค่าการเชื่อมต่อ"""
        if api_url is not None:
            self.api_url = api_url.strip() or self.DEFAULT_API_URL
        if cookie_header is not None:
            self.cookie_header = cookie_header.strip()
        if auth_token is not None:
            self.auth_token = auth_token.strip()
        if interval_sec is not None:
            self.interval_sec = max(1.0, float(interval_sec))

    def start(
        self,
        on_data: Callable[[Dict], None],
        on_status: Optional[Callable[[str, str, int], None]] = None
    ):
        """เริ่ม Polling ทำงานเบื้องหลัง"""
        self.on_data_callback = on_data
        self.on_status_callback = on_status
        self.is_running = True
        if not self._thread or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._poll_loop, daemon=True)
            self._thread.start()

    def stop(self):
        """หยุดการทำงานของ Poller"""
        self.is_running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        self.status = "idle"
        self.status_msg = "หยุดทำงานแล้ว"
        self._notify_status()

    def fetch_once(self) -> Tuple[Optional[Dict], str, str, int]:
        """
        ดึงข้อมูล Telemetry 1 ครั้ง
        คืนค่า: (data, status, message, latency_ms)
        """
        req = urllib.request.Request(self.api_url)
        req.add_header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) VoiceHUD/1.2")
        req.add_header("Accept", "application/json")

        if self.cookie_header:
            req.add_header("Cookie", self.cookie_header)

        if self.auth_token:
            token = self.auth_token
            if not token.lower().startswith("bearer "):
                token = f"Bearer {token}"
            req.add_header("Authorization", token)

        start_t = time.time()
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                latency = int((time.time() - start_t) * 1000)
                if response.status == 200:
                    raw = response.read().decode("utf-8")
                    data = json.loads(raw)
                    p_list = data.get("players", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
                    p_count = len(p_list)
                    self.last_player_count = p_count
                    self.last_sync_time = time.time()
                    return data, "connected", f"ออนไลน์ ({p_count} คน)", latency
                return None, "offline", f"HTTP {response.status}", latency
        except urllib.error.HTTPError as e:
            latency = int((time.time() - start_t) * 1000)
            if e.code in (401, 403):
                return None, "unauthorized", f"Session หมดอายุ / ต้อง Login (HTTP {e.code})", latency
            return None, "offline", f"HTTP Error {e.code}", latency
        except urllib.error.URLError as e:
            latency = int((time.time() - start_t) * 1000)
            return None, "offline", "ไม่สามารถติดต่อ Server ได้", latency
        except Exception as e:
            latency = int((time.time() - start_t) * 1000)
            return None, "offline", f"Error: {e}", latency

    def _notify_status(self):
        if self.on_status_callback:
            try:
                self.on_status_callback(self.status, self.status_msg, self.last_latency_ms)
            except Exception as e:
                print(f"[Error in Poller Status Callback]: {e}")

    def _poll_loop(self):
        while self.is_running:
            data, status, msg, latency = self.fetch_once()
            self.status = status
            self.status_msg = msg
            self.last_latency_ms = latency
            self._notify_status()

            if data and self.on_data_callback:
                try:
                    self.on_data_callback(data)
                except Exception as e:
                    print(f"[Error in Poller Callback]: {e}")

            time.sleep(self.interval_sec)
