"""
Private Party Room & Peer-to-Peer / Local Network Synchronizer
ระบบเชื่อมต่อปาร์ตี้ส่วนตัวผ่าน Party Code / PIN:
- รองรับโหมด Host (สร้างห้อง) และ Join (เข้าร่วมห้อง)
- ส่งพิกัดและทิศทางผ่าน UDP Multicast/Broadcast หรือ Direct IP
- มีห้อง PIN Protection ตรวจสอบความถูกต้องของข้อมูล
"""

import time
import json
import socket
import select
import threading
import uuid
from typing import Callable, Optional, Dict, List


class PartySyncNode:
    """
    โหนดเชื่อมต่อปาร์ตี้ส่วนตัว (Party Node)
    ทำงานผ่าน UDP Broadcast / LAN หรือ Direct Peer IP
    """
    DEFAULT_PORT = 39820
    MAGIC_HEADER = "VOICEHUD_PARTY_V1"

    def __init__(
        self,
        player_id: str,
        player_name: str,
        party_code: str = "ISLE123",
        port: int = DEFAULT_PORT,
        is_host: bool = False,
        remote_hosts: Optional[List[str]] = None
    ):
        self.player_id = player_id
        self.player_name = player_name
        self.party_code = party_code.strip().upper()
        self.port = port
        self.is_host = is_host
        self.remote_hosts = remote_hosts or []

        self.is_running = False
        self._thread: Optional[threading.Thread] = None
        self._sock: Optional[socket.socket] = None

        # Callbacks
        self.on_party_data_callback: Optional[Callable[[Dict], None]] = None
        self.on_status_callback: Optional[Callable[[str, str, int], None]] = None

        # สถิติและสถานะ
        self.known_peers: Dict[str, Dict] = {}  # pid -> {name, x, y, angle, color, last_seen}
        self.peer_timeout_sec = 8.0
        self.last_latency_ms = 0
        self.status = "idle"  # "connected", "hosting", "joining", "offline", "idle"
        self.status_msg = "ยังไม่ได้เชื่อมต่อห้อง"

    def configure(
        self,
        party_code: Optional[str] = None,
        is_host: Optional[bool] = None,
        remote_hosts: Optional[List[str]] = None,
        port: Optional[int] = None,
        player_name: Optional[str] = None
    ):
        if party_code is not None:
            self.party_code = party_code.strip().upper()
        if is_host is not None:
            self.is_host = is_host
        if remote_hosts is not None:
            self.remote_hosts = remote_hosts
        if port is not None:
            self.port = port
        if player_name is not None:
            self.player_name = player_name

    def start(self, on_data: Callable[[Dict], None], on_status: Optional[Callable[[str, str, int], None]] = None):
        self.on_party_data_callback = on_data
        self.on_status_callback = on_status

        if self.is_running:
            return

        self.is_running = True
        self._thread = threading.Thread(target=self._network_worker, daemon=True)
        self._thread.start()

    def stop(self):
        self.is_running = False
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

    def broadcast_my_position(self, gx: float, gy: float, heading: float, color: str = "#00FF66"):
        """ส่งพิกัดของตัวเองออกไปยังเครือข่ายปาร์ตี้"""
        if not self.is_running or not self._sock or not self.party_code:
            return

        payload = {
            "magic": self.MAGIC_HEADER,
            "party_code": self.party_code,
            "sender_id": self.player_id,
            "name": self.player_name,
            "x": gx,
            "y": gy,
            "heading": heading,
            "color": color,
            "timestamp": time.time()
        }

        try:
            msg_bytes = json.dumps(payload).encode("utf-8")
            # 1. ส่งผ่าน Global Broadcast
            try:
                self._sock.sendto(msg_bytes, ("<broadcast>", self.port))
            except Exception:
                pass

            # 2. ส่งผ่าน Subnet Broadcast ของวงเครือข่ายเสมือน (ZeroTier / VPN)
            for b_ip in ("172.24.255.255", "192.168.1.255", "255.255.255.255"):
                try:
                    self._sock.sendto(msg_bytes, (b_ip, self.port))
                except Exception:
                    pass

            # 3. ส่งตรงไปยัง Peer IPs ที่ระบุเพิ่มเติม (ZeroTier / Tailscale / Radmin VPN / Direct IP)
            for host in self.remote_hosts:
                h = host.strip()
                if h:
                    try:
                        self._sock.sendto(msg_bytes, (h, self.port))
                    except Exception:
                        pass
        except Exception:
            pass

    def _network_worker(self):
        # สร้าง UDP Socket ที่รองรับ Broadcast & Port Reuse
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            # บน Linux/Mac รองรับ SO_REUSEPORT
            if hasattr(socket, "SO_REUSEPORT"):
                try:
                    self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                except Exception:
                    pass

            self._sock.bind(("", self.port))
            self.status = "connected"
            mode_str = "โฮสต์" if self.is_host else "ร่วมปาร์ตี้"
            self.status_msg = f"ห้อง #{self.party_code} ({mode_str})"
            self._notify_status()
        except Exception as e:
            self.status = "offline"
            self.status_msg = f"ไม่สามารถเปิดพอร์ต: {e}"
            self._notify_status()
            self.is_running = False
            return

        last_cleanup = time.time()

        while self.is_running:
            try:
                r, _, _ = select.select([self._sock], [], [], 0.5)
                if r and self._sock:
                    data, addr = self._sock.recvfrom(4096)
                    self._handle_packet(data, addr)

                now = time.time()
                # ทุกๆ 1 วินาที ทำการตรวจสอบเพื่อนที่หลุดการเชื่อมต่อ (Peer Timeout)
                if now - last_cleanup >= 1.0:
                    last_cleanup = now
                    self._prune_inactive_peers(now)

            except Exception:
                time.sleep(0.1)

    def _handle_packet(self, data: bytes, addr):
        try:
            pkt = json.loads(data.decode("utf-8"))
            if pkt.get("magic") != self.MAGIC_HEADER:
                return

            # ตรวจสอบ Party PIN / Room Code
            pkt_code = str(pkt.get("party_code", "")).strip().upper()
            if pkt_code != self.party_code:
                # Debug log แจ้งเตือนเมื่อมีเพื่อนส่งมาแต่รหัสห้องไม่ตรงกัน
                sender_name = pkt.get("name", "Unknown")
                print(f"[PartySync] Received packet from {addr[0]} ({sender_name}) with mismatched code '{pkt_code}' (local is '{self.party_code}')")
                return

            sender_id = pkt.get("sender_id")
            # ถ้าเป็นแพ็กเก็ตของตัวเอง ให้ข้าม
            if not sender_id or sender_id == self.player_id:
                return

            now = time.time()
            self.known_peers[sender_id] = {
                "id": sender_id,
                "name": pkt.get("name", "Party Member"),
                "x": float(pkt.get("x", 0)),
                "y": float(pkt.get("y", 0)),
                "heading": float(pkt.get("heading", 0)),
                "color": pkt.get("color", "#00E5FF"),
                "last_seen": now
            }

            # คำนวณ Latency คร่าวๆ
            pkt_time = pkt.get("timestamp", now)
            self.last_latency_ms = max(1, int((now - pkt_time) * 1000))

            # แจ้ง UI อัปเดตรายชื่อและพิกัดเพื่อนในห้อง
            self._dispatch_party_update()

        except Exception:
            pass

    def _prune_inactive_peers(self, now: float):
        expired = [
            pid for pid, peer in self.known_peers.items()
            if now - peer.get("last_seen", 0) > self.peer_timeout_sec
        ]
        if expired:
            for pid in expired:
                del self.known_peers[pid]
            self._dispatch_party_update()

    def _dispatch_party_update(self):
        peer_list = list(self.known_peers.values())
        count = len(peer_list)
        self.status = "connected"
        self.status_msg = f"ห้อง #{self.party_code} ({count} คนในตี้)"
        self._notify_status()

        if self.on_party_data_callback:
            # รูปแบบข้อมูลให้อยู่ในฟอร์แมต dict ที่ main.py คุ้นเคย {"players": [...]}
            self.on_party_data_callback({"players": peer_list})

    def _notify_status(self):
        if self.on_status_callback:
            try:
                self.on_status_callback(self.status, self.status_msg, self.last_latency_ms)
            except Exception:
                pass
