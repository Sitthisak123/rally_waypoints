"""
Private Party Room & Peer-to-Peer / Local Network Synchronizer
ระบบเชื่อมต่อปาร์ตี้ส่วนตัวผ่าน ZeroTier / LAN / VPN:
- รองรับโหมด Host (สร้างห้อง) และ Join (เข้าร่วมห้อง)
- ส่งพิกัดผู้เล่น (Playerpoint) และ Waypoints ของปาร์ตี้
- รองรับ Dynamic Network Detection (Auto-detect ZeroTier & LAN Broadcast)
- มีห้อง PIN / Party Code ตรวจสอบความถูกต้อง
"""

import time
import json
import socket
import select
import threading
import subprocess
import platform
import re
from typing import Callable, Optional, Dict, List, Any


def detect_network_interfaces() -> List[Dict[str, str]]:
    """
    ตรวจจับ IPv4 และ Broadcast Address ของ Network Adapters อัตโนมัติ
    ครอบคลุมทั้ง ZeroTier, Wi-Fi, Ethernet, VPN
    """
    res = []
    sys_type = platform.system()
    if sys_type == "Linux":
        try:
            out = subprocess.check_output(['ip', '-4', 'addr', 'show'], stderr=subprocess.DEVNULL).decode()
            curr_iface = "unknown"
            for line in out.splitlines():
                m_if = re.match(r'^\d+:\s+([^:]+):', line)
                if m_if:
                    curr_iface = m_if.group(1).strip()
                elif 'inet ' in line:
                    tokens = line.strip().split()
                    ip = tokens[1].split('/')[0]
                    brd = None
                    if 'brd' in tokens:
                        brd = tokens[tokens.index('brd') + 1]
                    if not ip.startswith('127.'):
                        is_zt = curr_iface.startswith('zt') or 'zerotier' in curr_iface.lower()
                        res.append({
                            'interface': curr_iface,
                            'ip': ip,
                            'broadcast': brd or '255.255.255.255',
                            'is_zerotier': is_zt
                        })
        except Exception:
            pass
    elif sys_type == "Windows":
        try:
            out = subprocess.check_output(['ipconfig', '/all'], stderr=subprocess.DEVNULL).decode('cp437', errors='ignore')
            curr_adapter = "Unknown Adapter"
            for line in out.splitlines():
                if 'adapter' in line.lower():
                    curr_adapter = line.strip().rstrip(':')
                elif 'IPv4' in line or 'IP Address' in line:
                    parts = line.split(':')
                    if len(parts) > 1:
                        ip = parts[1].strip().split('(')[0].strip()
                        if ip and not ip.startswith('127.'):
                            is_zt = 'zerotier' in curr_adapter.lower() or 'zt' in curr_adapter.lower()
                            res.append({
                                'interface': curr_adapter,
                                'ip': ip,
                                'broadcast': '255.255.255.255',
                                'is_zerotier': is_zt
                            })
        except Exception:
            pass

    return res


class PartySyncNode:
    """
    โหนดเชื่อมต่อปาร์ตี้ส่วนตัว (Party Node)
    ทำงานผ่าน UDP Broadcast / ZeroTier Subnet / Direct Peer IP
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
        remote_hosts: Optional[List[str]] = None,
        sync_waypoints: bool = True,
        sync_playerpoint: bool = True
    ):
        self.player_id = player_id
        self.player_name = player_name
        self.party_code = party_code.strip().upper()
        self.port = port
        self.is_host = is_host
        self.remote_hosts = remote_hosts or []
        self.sync_waypoints = sync_waypoints
        self.sync_playerpoint = sync_playerpoint

        self.is_running = False
        self._thread: Optional[threading.Thread] = None
        self._sock: Optional[socket.socket] = None

        # Callbacks
        self.on_party_data_callback: Optional[Callable[[Dict], None]] = None
        self.on_waypoint_sync_callback: Optional[Callable[[str, Dict], None]] = None  # action, wp_dict
        self.on_status_callback: Optional[Callable[[str, str, int], None]] = None

        # สถิติและสถานะ
        self.known_peers: Dict[str, Dict] = {}  # pid -> {name, x, y, angle, color, last_seen, ip}
        self.peer_timeout_sec = 8.0
        self.last_latency_ms = 0
        self.status = "idle"  # "connected", "hosting", "joining", "offline", "idle"
        self.status_msg = "ยังไม่ได้เชื่อมต่อห้อง"

        # Cached broadcasts
        self._broadcast_targets: List[str] = ["<broadcast>", "255.255.255.255"]
        self._detected_interfaces: List[Dict[str, str]] = []
        self._last_iface_refresh: float = 0.0

    def refresh_network_interfaces(self):
        """ค้นหา IP และ Broadcast Address ทั้งหมดในเครื่อง (รวมถึง ZeroTier)"""
        now = time.time()
        if now - self._last_iface_refresh < 5.0 and self._detected_interfaces:
            return
        self._last_iface_refresh = now
        self._detected_interfaces = detect_network_interfaces()
        targets = {"<broadcast>", "255.255.255.255"}
        for item in self._detected_interfaces:
            brd = item.get("broadcast")
            if brd:
                targets.add(brd)
        self._broadcast_targets = list(targets)

    def get_detected_interfaces(self) -> List[Dict[str, str]]:
        self.refresh_network_interfaces()
        return self._detected_interfaces

    def configure(
        self,
        party_code: Optional[str] = None,
        is_host: Optional[bool] = None,
        remote_hosts: Optional[List[str]] = None,
        port: Optional[int] = None,
        player_name: Optional[str] = None,
        sync_waypoints: Optional[bool] = None,
        sync_playerpoint: Optional[bool] = None
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
        if sync_waypoints is not None:
            self.sync_waypoints = sync_waypoints
        if sync_playerpoint is not None:
            self.sync_playerpoint = sync_playerpoint

    def start(
        self,
        on_data: Callable[[Dict], None],
        on_status: Optional[Callable[[str, str, int], None]] = None,
        on_waypoint_sync: Optional[Callable[[str, Dict], None]] = None
    ):
        self.on_party_data_callback = on_data
        self.on_status_callback = on_status
        self.on_waypoint_sync_callback = on_waypoint_sync

        if self.is_running:
            return

        self.is_running = True
        self.refresh_network_interfaces()
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

    def _send_packet(self, payload: Dict):
        """ส่งแพ็กเก็ตไปยังเป้าหมายทั้งหมด (Broadcasts + Subnets + Remote Peers)"""
        if not self._sock or not self.is_running:
            return
        try:
            msg_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        except Exception:
            return

        self.refresh_network_interfaces()

        # 1. ส่งผ่าน Broadcast targets ทั้งหมด
        for b_target in self._broadcast_targets:
            try:
                self._sock.sendto(msg_bytes, (b_target, self.port))
            except Exception:
                pass

        # 2. ส่งตรงไปยัง Direct Remote Peers (เช่น ZeroTier IP เพื่อน)
        for host in self.remote_hosts:
            h = host.strip()
            if h:
                try:
                    self._sock.sendto(msg_bytes, (h, self.port))
                except Exception:
                    pass

    def broadcast_my_position(self, gx: float, gy: float, heading: float, color: str = "#00FF66"):
        """ส่งพิกัดของผู้เล่น (Playerpoint) ออกไปยังปาร์ตี้"""
        if not self.sync_playerpoint or not self.is_running or not self.party_code:
            return

        payload = {
            "magic": self.MAGIC_HEADER,
            "type": "position",
            "party_code": self.party_code,
            "sender_id": self.player_id,
            "name": self.player_name,
            "x": gx,
            "y": gy,
            "heading": heading,
            "color": color,
            "timestamp": time.time()
        }
        self._send_packet(payload)

    def broadcast_waypoint_event(self, action: str, waypoint_data: Dict):
        """
        ส่ง Event เกี่ยวกับ Waypoint (action: "add", "delete", "sync_all")
        """
        if not self.sync_waypoints or not self.is_running or not self.party_code:
            return

        payload = {
            "magic": self.MAGIC_HEADER,
            "type": "waypoint",
            "action": action,
            "party_code": self.party_code,
            "sender_id": self.player_id,
            "name": self.player_name,
            "waypoint": waypoint_data,
            "timestamp": time.time()
        }
        self._send_packet(payload)

    def _network_worker(self):
        # สร้าง UDP Socket ที่รองรับ Broadcast & Port Reuse
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
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
            self.status_msg = f"ไม่สามารถเปิดพอร์ต {self.port}: {e}"
            self._notify_status()
            self.is_running = False
            return

        last_cleanup = time.time()

        while self.is_running:
            try:
                r, _, _ = select.select([self._sock], [], [], 0.5)
                if r and self._sock:
                    data, addr = self._sock.recvfrom(8192)
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
                sender_name = pkt.get("name", "Unknown")
                print(f"[PartySync] Packet from {addr[0]} ({sender_name}) mismatched code '{pkt_code}' (local is '{self.party_code}')")
                return

            sender_id = pkt.get("sender_id")
            if not sender_id or sender_id == self.player_id:
                return

            pkt_type = pkt.get("type", "position")
            now = time.time()

            # 1. พิกัดผู้เล่น (Playerpoint)
            if pkt_type == "position":
                if not self.sync_playerpoint:
                    return

                self.known_peers[sender_id] = {
                    "id": sender_id,
                    "name": pkt.get("name", "Party Member"),
                    "x": float(pkt.get("x", 0)),
                    "y": float(pkt.get("y", 0)),
                    "heading": float(pkt.get("heading", 0)),
                    "color": pkt.get("color", "#00E5FF"),
                    "last_seen": now,
                    "peer_ip": addr[0]
                }

                pkt_time = pkt.get("timestamp", now)
                self.last_latency_ms = max(1, int((now - pkt_time) * 1000))
                self._dispatch_party_update()

            # 2. การซิงค์ Waypoint (เพิ่ม / ลบ / ซิงค์ทั้งหมด)
            elif pkt_type == "waypoint":
                if not self.sync_waypoints:
                    return

                action = pkt.get("action", "")
                wp_data = pkt.get("waypoint", {})
                if self.on_waypoint_sync_callback and action and wp_data:
                    self.on_waypoint_sync_callback(action, wp_data)

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
        mode_str = "โฮสต์" if self.is_host else "ร่วมปาร์ตี้"
        self.status_msg = f"ห้อง #{self.party_code} ({count} คนในตี้)"
        self._notify_status()

        if self.on_party_data_callback:
            self.on_party_data_callback({"players": peer_list})

    def _notify_status(self):
        if self.on_status_callback:
            try:
                self.on_status_callback(self.status, self.status_msg, self.last_latency_ms)
            except Exception:
                pass
