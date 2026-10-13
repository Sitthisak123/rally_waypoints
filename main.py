"""
VoiceHUD - PackPoint Complete Implementation
Optimize for Maximum Performance & Smoothness on Linux & Windows
- Opacity ปรับแล้วมีผลจริง (Alpha blending บน Minimap Background & Texture)
- Interactive Map รองรับ Scroll Mouse เพื่อซูมเข้า/ออกอย่างอิสระ
- ลดภาระ Render Timer จาก 33ms เป็น 50ms (20 FPS เพียงพอสำหรับ Map Telemetry 3s)
- ใช้ gateway-minimap.png ขนาด 638KB แทนไฟล์ใหญ่ เรนเดอร์ลื่นไหล ไม่กิน CPU/GPU
"""

import sys
import os
import math
import time
import json
import uuid
from typing import Dict, List, Optional, Tuple

from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtCore import Qt, QPointF, QRectF, QTimer, pyqtSignal
from PyQt5.QtGui import QPainter, QColor, QPen, QBrush, QFont, QPolygonF, QPixmap

try:
    from src.engine import (
        GatewayProjection,
        PlayerMovementState,
        MovementTracker,
        PackClusterManager,
        NavigationEngine
    )
    from src.window_manager import CrossPlatformWindowManager
    from src.poller import VoiceIslandPoller
    from src.hotkeys import GlobalHotkeyListener
    from src.layers_manager import GatewayLayersManager
    from src.party_sync import PartySyncNode
    from src.voice_island_bridge import VoiceIslandInputBridge
except ImportError:
    from engine import (
        GatewayProjection,
        PlayerMovementState,
        MovementTracker,
        PackClusterManager,
        NavigationEngine
    )
    from window_manager import CrossPlatformWindowManager
    from poller import VoiceIslandPoller
    from hotkeys import GlobalHotkeyListener
    from layers_manager import GatewayLayersManager
    from party_sync import PartySyncNode
    from voice_island_bridge import VoiceIslandInputBridge


def get_base_dir() -> str:
    """คืนค่า path โฟลเดอร์ของแอพ รองรับทั้งรันตรงจาก source และ PyInstaller frozen bundle"""
    if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))


def get_writable_dir() -> str:
    """คืนค่าไดเรกทอรีสำหรับบันทึก user config/waypoints (อยู่ข้าง .exe หรือโฟลเดอร์โปรเจกต์)"""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def get_resource_path(*rel_path: str) -> str:
    """คืนค่า path ของ asset/data ที่อ่านได้ ทั้งใน _MEIPASS หรือข้างนอก"""
    # ตรวจสอบว่ามีอยู่ใน writable_dir หรือไม่ก่อน (เพื่อให้ override ได้)
    p_writable = os.path.join(get_writable_dir(), *rel_path)
    if os.path.exists(p_writable):
        return p_writable
    # ตรวจสอบใน bundle หรือ source tree
    p_base = os.path.join(get_base_dir(), *rel_path)
    if os.path.exists(p_base):
        return p_base
    return p_writable


PARTY_PALETTE = [
    "#FF7A00",  # ส้ม
    "#FF007A",  # ชมพู
    "#00E5FF",  # ฟ้า
    "#9E00FF",  # ม่วง
    "#FFD600",  # เหลือง
    "#00FFC2",  # เขียวมินต์
    "#FF3366",  # แดงคอรัล
    "#3399FF"   # น้ำเงินสว่าง
]


class Waypoint:
    def __init__(self, name: str, svg_x: float, svg_y: float, icon: str = "camp", color: str = "#FFD600", wid: Optional[str] = None):
        self.id = wid or str(uuid.uuid4())[:8]
        self.name = name
        self.x = svg_x
        self.y = svg_y
        self.icon = icon  # "camp", "danger", "water", "food"
        self.color = color

    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "name": self.name,
            "x": self.x,
            "y": self.y,
            "icon": self.icon,
            "color": self.color
        }


class VoiceHUDOverlay(QtWidgets.QWidget):
    sig_toggle_interactive = pyqtSignal()
    sig_open_interactive = pyqtSignal()
    sig_close_interactive = pyqtSignal()
    sig_toggle_minimap = pyqtSignal()
    sig_zoom_in = pyqtSignal()
    sig_zoom_out = pyqtSignal()
    sig_exit = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setWindowTitle("VoiceHUD - PackPoint")

        self.setWindowFlags(
            Qt.FramelessWindowHint |
            Qt.WindowStaysOnTopHint |
            Qt.SubWindow |
            Qt.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setMouseTracking(True)

        # โหลดรูปแผนที่แบบ Lightweight เพื่อความลื่นไหลสูงสุด
        map_paths = [
            get_resource_path("assets", "gateway-minimap.png"),
            get_resource_path("assets", "gateway-base-optimized.png"),
            get_resource_path("assets", "gateway-base.png"),
            get_resource_path("gateway-minimap.png"),
            get_resource_path("gateway-base-optimized.png"),
            get_resource_path("gateway-base.png")
        ]
        self.map_pixmap = None
        for p in map_paths:
            if os.path.exists(p):
                self.map_pixmap = QPixmap(p)
                break

        # Cross-platform window manager
        self.wm = CrossPlatformWindowManager()
        self.is_interactive = False

        # Config & Styles
        self.self_color = "#00FF66"
        self.local_player_id = "local_player"
        self.players: Dict[str, PlayerMovementState] = {}
        self.movement_tracker = MovementTracker(max_footprints=10, min_step_dist=8.0, cull_dist=50.0)
        self.cluster_manager = PackClusterManager(cluster_radius=32.0)

        # Minimap Config
        self.minimap_visible = True
        self.minimap_scale = 1.0
        self.minimap_size_mode = "Medium"
        self.minimap_sizes = {"Small": 220, "Medium": 300, "Large": 380}
        self.minimap_size = self.minimap_sizes["Medium"]
        self.minimap_opacity = 0.85
        self.minimap_brightness = 0.8  # ความสว่าง Minimap (0.2 - 1.0)
        self.fullmap_brightness = 0.8   # ความสว่าง Full Map (0.2 - 1.0)
        self.entity_icon_scale = 1.0    # ขนาดสเกลไอคอนและเอนทิตี (0.5 - 2.0)
        self.dragging_slider: Optional[str] = None  # "minimap_br", "fullmap_br", or "icon_scale"
        self.show_config_menu = False

        # Live Session & Telemetry Config (Mode: "web" or "party_room")
        self.sync_mode = "party_room"  # "web" หรือ "party_room"
        self.party_code = "ISLE77"
        self.party_player_name = ".xda_7"
        self.party_is_host = True
        self.party_remote_hosts: List[str] = []
        self.telemetry_api_url = "https://api.voice-island.com/api/players"
        self.telemetry_cookie = ""
        self.telemetry_token = ""
        self.telemetry_interval = 2.0  # Polling interval (1.0s - 5.0s)
        self.telemetry_status = "idle"  # "connected", "unauthorized", "offline", "idle"
        self.telemetry_status_msg = "ยังไม่ได้เชื่อมต่อ"
        self.telemetry_latency = 0
        self.live_status_rect: Optional[QRectF] = None

        # Interactive Map Zoom & Pan (Hold Right-Click to Drag Map)
        self.interactive_map_scale = 1.0
        self.fullmap_pan_x = 0.0
        self.fullmap_pan_y = 0.0
        self.is_right_dragging = False
        self.drag_start_pos: Optional[QtCore.QPoint] = None
        self.drag_start_pan_x = 0.0
        self.drag_start_pan_y = 0.0

        # Gateway Extra Layers (Sanctuaries, Salt Rocks, Gastro, Labels)
        self.layers_mgr = GatewayLayersManager()

        # Load Saved Mini Config Settings (Mini config persistence)
        self._load_mini_config()

        # Shared Waypoints & Hover Menu
        self.waypoints: List[Waypoint] = []
        self._load_saved_waypoints()
        self.hovered_waypoint: Optional[Waypoint] = None
        self.hovered_wp_screen_pos: Optional[QPointF] = None
        self.hovered_wp_action_rects: Dict[str, QRectF] = {}
        self.hovered_entity_action_rects: Dict[str, QRectF] = {}

        # Player Hover & Selection
        self.hovered_player: Optional[PlayerMovementState] = None
        self.hovered_player_screen_pos: Optional[QPointF] = None
        self.hovered_player_action_rects: Dict[str, QRectF] = {}

        # Tracking System
        self.tracked_target: Optional[Dict] = None
        self.show_track_menu = False
        self.fullmap_untrack_rect: Optional[QRectF] = None

        # Universal Selection State (Lock hover menu open on click)
        self.selected_entity: Optional[Dict] = None
        self.selected_waypoint: Optional[Waypoint] = None
        self.selected_player: Optional[PlayerMovementState] = None

        # Signals
        self.sig_toggle_interactive.connect(self.toggle_interactive_mode)
        self.sig_open_interactive.connect(self.open_interactive_mode)
        self.sig_close_interactive.connect(self.close_interactive_mode)
        self.sig_toggle_minimap.connect(self.toggle_minimap)
        self.sig_zoom_in.connect(self.zoom_in)
        self.sig_zoom_out.connect(self.zoom_out)
        self.sig_exit.connect(self.exit_application)

        # Voice Island Input Bridge (ส่ง PTT Key 'V' ตรงไปยัง Voice Island แม้อยู่ในเกม)
        self.vi_bridge = VoiceIslandInputBridge(target_window_title="Voice Island", ptt_key="v")

        # Global Hotkey Listener (M: กดค้าง 1 วินาทีเพื่อเปิด, กด M หรือ ESC เพื่อปิด, 8: ออกจากโปรแกรม, V: Voice Island PTT)
        self.hotkeys = GlobalHotkeyListener(
            on_toggle_interactive=self.sig_toggle_interactive.emit,
            on_toggle_minimap=self.sig_toggle_minimap.emit,
            on_zoom_in=self.sig_zoom_in.emit,
            on_zoom_out=self.sig_zoom_out.emit,
            on_m_hold_open=self.sig_open_interactive.emit,
            on_m_quick_close=self.sig_close_interactive.emit,
            is_interactive_check=lambda: self.is_interactive,
            on_exit=self.sig_exit.emit,
            on_ptt_down=self.vi_bridge.send_key_down,
            on_ptt_up=self.vi_bridge.send_key_up
        )
        self.hotkeys.start()

        # Telemetry Poller (Web Mode)
        self.poller = VoiceIslandPoller(
            api_url=self.telemetry_api_url,
            interval_sec=self.telemetry_interval,
            cookie_header=self.telemetry_cookie,
            auth_token=self.telemetry_token
        )

        # Private Party Sync Node (LAN / P2P / VPN Mode)
        self.party_node = PartySyncNode(
            player_id=self.local_player_id,
            player_name=self.party_player_name,
            party_code=self.party_code,
            is_host=self.party_is_host,
            remote_hosts=self.party_remote_hosts
        )

        if self.sync_mode == "party_room":
            self.party_node.start(self.on_party_received, self.on_party_status)
        else:
            self.poller.start(self.on_telemetry_received, self.on_telemetry_status)

        # Party Broadcast Timer (ส่งพิกัดตัวเองให้เพื่อนทุก 100ms หรือ 10 FPS เมื่ออยู่ใน party_room mode)
        self.party_broadcast_timer = QTimer(self)
        self.party_broadcast_timer.timeout.connect(self._broadcast_local_position)
        self.party_broadcast_timer.start(100)

        # ปรับ Render Timer เป็น 50ms (20 FPS) ลดภาระ CPU ให้เบาและไม่กระตุก
        self.render_timer = QTimer(self)
        self.render_timer.timeout.connect(self.update)
        self.render_timer.start(50)

        screen = QtWidgets.QApplication.primaryScreen().geometry()
        self.setGeometry(screen)

    def _load_mini_config(self):
        """โหลดการตั้งค่า Minimap และ Layers จาก config.json"""
        cfg_file = get_resource_path("data", "config.json")
        if not os.path.exists(cfg_file):
            cfg_file = get_resource_path("config.json")
        if os.path.exists(cfg_file):
            try:
                with open(cfg_file, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                mode = cfg.get("minimap_size_mode", "Medium")
                if mode in self.minimap_sizes:
                    self.minimap_size_mode = mode
                    self.minimap_size = self.minimap_sizes[mode]
                self.minimap_opacity = float(cfg.get("minimap_opacity", 0.85))
                self.minimap_brightness = float(cfg.get("minimap_brightness", 0.8))
                self.fullmap_brightness = float(cfg.get("fullmap_brightness", 0.8))
                self.entity_icon_scale = float(cfg.get("entity_icon_scale", 1.0))

                layers = cfg.get("layers", {})
                self.layers_mgr.show_zones = bool(layers.get("show_zones", True))
                self.layers_mgr.show_items = bool(layers.get("show_items", True))
                self.layers_mgr.show_labels_region = bool(layers.get("show_labels_region", True))
                self.layers_mgr.show_food = bool(layers.get("show_food", False))

                cats = cfg.get("active_food_categories")
                if isinstance(cats, list):
                    self.layers_mgr.active_food_categories = set(cats)

                z_cats = cfg.get("active_zone_categories")
                if isinstance(z_cats, list):
                    self.layers_mgr.active_zone_categories = set(z_cats)

                # Telemetry & Session Auth Config
                telem = cfg.get("telemetry", {})
                self.telemetry_api_url = telem.get("api_url", "https://api.voice-island.com/api/players")
                self.telemetry_cookie = telem.get("cookie_header", "")
                self.telemetry_token = telem.get("auth_token", "")
                self.telemetry_interval = float(telem.get("interval_sec", 2.0))

                # Private Party Room Config
                party_cfg = cfg.get("party_room", {})
                self.sync_mode = cfg.get("sync_mode", "party_room")
                self.party_code = party_cfg.get("code", "ISLE77")
                self.party_player_name = party_cfg.get("player_name", ".xda_7")
                self.party_is_host = bool(party_cfg.get("is_host", True))
                self.party_remote_hosts = party_cfg.get("remote_hosts", [])
            except Exception as e:
                print(f"[Error loading mini config]: {e}")

    def _save_mini_config(self):
        """บันทึกการตั้งค่า Minimap และ Layers ลง config.json"""
        data_dir = os.path.join(get_writable_dir(), "data")
        os.makedirs(data_dir, exist_ok=True)
        cfg_file = os.path.join(data_dir, "config.json")
        try:
            cfg = {
                "sync_mode": self.sync_mode,
                "minimap_size_mode": self.minimap_size_mode,
                "minimap_opacity": self.minimap_opacity,
                "minimap_brightness": self.minimap_brightness,
                "fullmap_brightness": self.fullmap_brightness,
                "entity_icon_scale": self.entity_icon_scale,
                "party_room": {
                    "code": self.party_code,
                    "player_name": self.party_player_name,
                    "is_host": self.party_is_host,
                    "remote_hosts": self.party_remote_hosts
                },
                "telemetry": {
                    "api_url": self.telemetry_api_url,
                    "cookie_header": self.telemetry_cookie,
                    "auth_token": self.telemetry_token,
                    "interval_sec": self.telemetry_interval
                },
                "layers": {
                    "show_zones": self.layers_mgr.show_zones,
                    "show_items": self.layers_mgr.show_items,
                    "show_labels_region": self.layers_mgr.show_labels_region,
                    "show_food": self.layers_mgr.show_food
                },
                "active_food_categories": list(self.layers_mgr.active_food_categories),
                "active_zone_categories": list(self.layers_mgr.active_zone_categories)
            }
            with open(cfg_file, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"[Error saving mini config]: {e}")

    def _load_saved_waypoints(self):
        cfg_file = get_resource_path("data", "waypoints.json")
        if not os.path.exists(cfg_file):
            cfg_file = get_resource_path("waypoints.json")
        if os.path.exists(cfg_file):
            try:
                with open(cfg_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    for item in data:
                        self.waypoints.append(Waypoint(
                            item["name"], item["x"], item["y"], item.get("icon", "camp"), item.get("color", "#FFD600"), item.get("id")
                        ))
            except Exception:
                pass
        if not self.waypoints:
            self.waypoints.append(Waypoint("จุดรวมพล Gateway Center", 365.0, 366.0, "camp", "#00E5FF"))

    def _save_waypoints(self):
        data_dir = os.path.join(get_writable_dir(), "data")
        os.makedirs(data_dir, exist_ok=True)
        cfg_file = os.path.join(data_dir, "waypoints.json")
        try:
            with open(cfg_file, "w", encoding="utf-8") as f:
                json.dump([w.to_dict() for w in self.waypoints], f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    def showEvent(self, event):
        super().showEvent(event)
        xid_or_hwnd = int(self.winId())
        self.wm.set_click_through(xid_or_hwnd, not self.is_interactive)

    def exit_application(self):
        """ปิดโปรแกรมอย่างปลอดภัยเมื่อกดปุ่ม Hotkey (Top-row 8)"""
        print("[VoiceHUD] Exit hotkey triggered. Exiting application...")
        if hasattr(self, 'hotkeys') and self.hotkeys:
            self.hotkeys.stop()
        if hasattr(self, 'party_node') and self.party_node:
            self.party_node.stop()
        if hasattr(self, 'poller') and self.poller:
            self.poller.stop()
        QtWidgets.QApplication.quit()

    def open_interactive_mode(self):
        print(f"[Main Debug] open_interactive_mode called (current is_interactive={self.is_interactive})")
        if not self.is_interactive:
            self.toggle_interactive_mode()

    def close_interactive_mode(self):
        print(f"[Main Debug] close_interactive_mode called (current is_interactive={self.is_interactive})")
        if self.is_interactive:
            self.toggle_interactive_mode()

    def toggle_interactive_mode(self):
        self.is_interactive = not self.is_interactive
        print(f"[Main Debug] toggle_interactive_mode executed -> NEW STATE is_interactive={self.is_interactive}")
        xid_or_hwnd = int(self.winId())
        self.wm.set_click_through(xid_or_hwnd, not self.is_interactive)
        if self.is_interactive:
            self.activateWindow()
            self.setFocus()
        else:
            self.show_track_menu = False
            self.show_config_menu = False
            self.clearFocus()
            # ป้องกันไม่ให้ KDE Plasma / X11 ถือว่าหน้าต่างร้องขอความสนใจ (Demands Attention)
            QtWidgets.QApplication.setActiveWindow(None)
        self.update()

    def toggle_minimap(self):
        self.minimap_visible = not self.minimap_visible
        self.update()

    def zoom_in(self):
        if self.is_interactive:
            self.interactive_map_scale = min(self.interactive_map_scale * 1.15, 4.0)
        else:
            self.minimap_scale = min(self.minimap_scale * 1.15, 3.5)
        self.update()

    def zoom_out(self):
        if self.is_interactive:
            self.interactive_map_scale = max(self.interactive_map_scale / 1.15, 0.5)
        else:
            self.minimap_scale = max(self.minimap_scale / 1.15, 0.4)
        self.update()

    def wheelEvent(self, event: QtGui.QWheelEvent):
        """รองรับการใช้ Scroll Mouse ในการซูมเข้า/ออก"""
        if self.is_interactive:
            angle = event.angleDelta().y()
            if angle > 0:
                self.interactive_map_scale = min(self.interactive_map_scale * 1.12, 4.0)
            elif angle < 0:
                self.interactive_map_scale = max(self.interactive_map_scale / 1.12, 0.5)
            self.update()
        elif self.minimap_visible:
            # หากเคอร์เซอร์อยู่เหนือ Minimap ให้ซูม Minimap
            pos = event.pos()
            minimap_rect = QRectF(16, 16, self.minimap_size, self.minimap_size)
            if minimap_rect.contains(pos):
                angle = event.angleDelta().y()
                if angle > 0:
                    self.zoom_in()
                elif angle < 0:
                    self.zoom_out()

    def keyPressEvent(self, event: QtGui.QKeyEvent):
        key = event.key()
        # เมื่อแผนที่เปิดอยู่: กด M หรือ ESC เพื่อปิดแผนที่ได้ทันที
        if self.is_interactive and key in (Qt.Key_Escape, Qt.Key_M):
            if event.isAutoRepeat():
                # ละเว้นคีย์ซ้ำ (Auto-repeat) จากการกดค้าง
                return
            print(f"[Main Debug] keyPressEvent caught key={key} while is_interactive=True")
            if self.show_track_menu or self.show_config_menu:
                self.show_track_menu = False
                self.show_config_menu = False
            else:
                self.close_interactive_mode()
        else:
            super().keyPressEvent(event)

    def mousePressEvent(self, event: QtGui.QMouseEvent):
        if not self.is_interactive:
            return

        pos = event.pos()

        # คลิกปุ่ม Track Menu
        if QRectF(self.width() - 170, 20, 140, 36).contains(pos):
            self.show_track_menu = not self.show_track_menu
            self.show_config_menu = False
            self.update()
            return

        # คลิกปุ่ม Config Menu
        if QRectF(self.width() - 320, 20, 130, 36).contains(pos):
            self.show_config_menu = not self.show_config_menu
            self.show_track_menu = False
            self.update()
            return

        # คลิกใน Track Menu Drawer
        if self.show_track_menu:
            drawer_rect = QRectF(self.width() - 280, 70, 260, 400)
            if drawer_rect.contains(pos):
                self._handle_track_menu_click(pos, drawer_rect)
                return

        # คลิกใน Mini Config Drawer
        if self.show_config_menu:
            extra_h = (55 if self.layers_mgr.show_zones else 0) + (55 if self.layers_mgr.show_food else 0)
            drawer_h = 475 + extra_h
            cfg_rect = QRectF(self.width() - 350, 70, 330, drawer_h)
            if cfg_rect.contains(pos):
                self._handle_config_menu_click(pos, cfg_rect)
                return

        # คลิกบน Live Status Pill หรือปุ่ม Session Auth เพื่อเปิดหน้าต่างตั้งค่า Session
        if self.live_status_rect and self.live_status_rect.contains(pos):
            self.open_session_auth_dialog()
            return

        # คลิกปุ่ม Untrack บน Full Map HUD Header
        if self.fullmap_untrack_rect and self.fullmap_untrack_rect.contains(pos):
            self.tracked_target = None
            self.update()
            return

        # ตรวจสอบการคลิกบน Entity Hover Menu Actions (Track / Untrack This Item/Food)
        if self.hovered_entity_action_rects:
            e_track = self.hovered_entity_action_rects.get("track")
            if e_track and e_track.contains(pos):
                ent = self.hovered_entity_action_rects.get("entity", {})
                is_currently_tracked = (self.tracked_target and
                                        self.tracked_target.get("type") == "entity" and
                                        math.hypot(self.tracked_target.get("x", 0) - ent.get("x", 0),
                                                   self.tracked_target.get("y", 0) - ent.get("y", 0)) < 0.1)
                if is_currently_tracked:
                    self.tracked_target = None
                else:
                    self.tracked_target = {
                        "type": "entity",
                        "id": f"entity_{ent.get('kind', 'item')}_{ent.get('x')}_{ent.get('y')}",
                        "name": ent.get("title", "ไอเทม/อาหาร"),
                        "x": ent.get("x"),
                        "y": ent.get("y")
                    }
                self.update()
                return

        # ตรวจสอบการคลิกบน Waypoint Hover Menu Actions (Track / Untrack / Delete)
        if self.hovered_waypoint and self.hovered_wp_action_rects:
            track_rect = self.hovered_wp_action_rects.get("track")
            delete_rect = self.hovered_wp_action_rects.get("delete")

            if track_rect and track_rect.contains(pos):
                if self.tracked_target and self.tracked_target.get("id") == self.hovered_waypoint.id:
                    self.tracked_target = None
                else:
                    self.tracked_target = {"type": "waypoint", "id": self.hovered_waypoint.id, "name": self.hovered_waypoint.name}
                self.update()
                return

            if delete_rect and delete_rect.contains(pos):
                target_id = self.hovered_waypoint.id
                self.waypoints = [w for w in self.waypoints if w.id != target_id]
                if self.tracked_target and self.tracked_target.get("id") == target_id:
                    self.tracked_target = None
                self._save_waypoints()
                self.hovered_waypoint = None
                self.hovered_wp_action_rects = {}
                self.update()
                return

        # ตรวจสอบการคลิกบน Player Hover Menu Actions (Track / Untrack Party Member)
        if self.hovered_player and self.hovered_player_action_rects:
            p_track = self.hovered_player_action_rects.get("track")
            if p_track and p_track.contains(pos):
                if self.tracked_target and self.tracked_target.get("id") == self.hovered_player.id:
                    self.tracked_target = None
                else:
                    self.tracked_target = {
                        "type": "player",
                        "id": self.hovered_player.id,
                        "name": self.hovered_player.name
                    }
                self.update()
                return

        # คลิกขวา: เริ่มต้นการลากเลื่อนแผนที่ (Hold Right-Click to Drag Map)
        if event.button() == Qt.RightButton:
            side = min(self.width() - 140, self.height() - 120)
            map_rect = QRectF((self.width() - side) / 2, 60, side, side)
            if map_rect.contains(pos):
                self.is_right_dragging = True
                self.drag_start_pos = pos
                self.drag_start_pan_x = self.fullmap_pan_x
                self.drag_start_pan_y = self.fullmap_pan_y
                self.setCursor(Qt.ClosedHandCursor)
                return


        # คลิกซ้ายบนแผนที่: เลือก Waypoint, Player หรือ Entity เพื่อล็อก Hover Menu ให้อยู่ตลอด
        if event.button() == Qt.LeftButton:
            side = min(self.width() - 140, self.height() - 120)
            map_rect = QRectF((self.width() - side) / 2, 60, side, side)
            if map_rect.contains(pos):
                base_scale = (side / GatewayProjection.SVG_WIDTH) * self.interactive_map_scale
                svg_x = (pos.x() - map_rect.center().x() - self.fullmap_pan_x) / base_scale + (GatewayProjection.SVG_WIDTH / 2)
                svg_y = (pos.y() - map_rect.center().y() - self.fullmap_pan_y) / base_scale + (GatewayProjection.SVG_HEIGHT / 2)

                # 1. ตรวจสอบคลิกเลือก Waypoint
                clicked_wp = None
                for wp in self.waypoints:
                    if math.hypot(svg_x - wp.x, svg_y - wp.y) <= 15.0 / max(self.interactive_map_scale, 0.5):
                        clicked_wp = wp
                        break

                if clicked_wp:
                    self.selected_waypoint = clicked_wp
                    self.selected_player = None
                    self.selected_entity = None
                    self.hovered_waypoint = clicked_wp
                    self.hovered_wp_screen_pos = pos
                    self.update()
                    return

                # 2. ตรวจสอบคลิกเลือก Player (ปาร์ตี้ - ไม่รวมตัวเราเอง)
                clicked_player = None
                for pid, p in self.players.items():
                    if pid == self.local_player_id:
                        continue
                    if math.hypot(svg_x - p.current_pos["x"], svg_y - p.current_pos["y"]) <= 15.0 / max(self.interactive_map_scale, 0.5):
                        clicked_player = p
                        break

                if clicked_player:
                    self.selected_player = clicked_player
                    self.selected_waypoint = None
                    self.selected_entity = None
                    self.hovered_player = clicked_player
                    self.hovered_player_screen_pos = pos
                    self.update()
                    return

                # 3. ตรวจสอบคลิกเลือก Entity (Item/Food/Salt)
                clicked_entity = self.layers_mgr.check_entity_hover(svg_x, svg_y, threshold_svg=8.0 / max(self.interactive_map_scale, 0.5))
                if clicked_entity:
                    self.selected_entity = clicked_entity
                    self.selected_waypoint = None
                    self.selected_player = None
                    self.layers_mgr.hovered_entity_info = {
                        "entity": clicked_entity,
                        "screen_x": pos.x(),
                        "screen_y": pos.y()
                    }
                    self.update()
                    return
                else:
                    # ถ้าคลิกที่ว่างในแผนที่ ให้ปลดล็อก selection ทั้งหมด
                    has_selection = (self.selected_entity or self.selected_waypoint or self.selected_player)
                    if has_selection:
                        self.selected_entity = None
                        self.selected_waypoint = None
                        self.selected_player = None
                        self.hovered_waypoint = None
                        self.hovered_player = None
                        self.layers_mgr.hovered_entity_info = None
                        self.update()

    def mouseReleaseEvent(self, event: QtGui.QMouseEvent):
        if not self.is_interactive:
            return

        if self.dragging_slider:
            self.dragging_slider = None
            self._save_mini_config()
            self.update()

        if event.button() == Qt.RightButton and self.is_right_dragging:
            self.is_right_dragging = False
            self.setCursor(Qt.ArrowCursor)

            # หากระยะลากน้อยมาก (< 5px) ถือเป็นการคลิกขวาธรรมดา -> ให้เปิดหน้าต่างสร้าง Waypoint
            if self.drag_start_pos:
                dist_drag = math.hypot(event.pos().x() - self.drag_start_pos.x(), event.pos().y() - self.drag_start_pos.y())
                if dist_drag < 5.0:
                    self._handle_map_create_waypoint(event.pos())
            self.drag_start_pos = None

    def mouseMoveEvent(self, event: QtGui.QMouseEvent):
        if not self.is_interactive:
            return

        pos = event.pos()

        # กำลังลากตัวเลื่อน Slide Bar (Minimap / Full Map Brightness, Icon Scale)
        if self.dragging_slider and self.show_config_menu:
            extra_h = (55 if self.layers_mgr.show_zones else 0) + (55 if self.layers_mgr.show_food else 0)
            drawer_h = 525 + extra_h
            cfg_drawer = QRectF(self.width() - 350, 70, 330, drawer_h)
            slider_w = 280
            track_start_x = cfg_drawer.x() + 20
            ratio = max(0.0, min(1.0, (pos.x() - track_start_x) / slider_w))
            if self.dragging_slider == "minimap_br":
                self.minimap_brightness = round(0.2 + ratio * 0.8, 2)
            elif self.dragging_slider == "fullmap_br":
                self.fullmap_brightness = round(0.2 + ratio * 0.8, 2)
            elif self.dragging_slider == "icon_scale":
                self.entity_icon_scale = round(0.5 + ratio * 1.5, 2)
            self.update()
            return

        # กำลังกดคลิกขวาค้างเพื่อลากเลื่อนแผนที่ (Pan Map)
        if self.is_right_dragging and self.drag_start_pos:
            dx = pos.x() - self.drag_start_pos.x()
            dy = pos.y() - self.drag_start_pos.y()
            self.fullmap_pan_x = self.drag_start_pan_x + dx
            self.fullmap_pan_y = self.drag_start_pan_y + dy
            self.update()
            return

        side = min(self.width() - 140, self.height() - 120)
        map_rect = QRectF((self.width() - side) / 2, 60, side, side)

        # หากเมาส์กำลังชี้อยู่บน Hover Menu Actions เดิม ไม่ต้องรีเซ็ต
        if self.hovered_wp_action_rects:
            box_wp = self.hovered_wp_action_rects.get("box")
            if box_wp and isinstance(box_wp, QRectF) and box_wp.contains(pos):
                return

        if self.hovered_entity_action_rects:
            box_r = self.hovered_entity_action_rects.get("box")
            if box_r and isinstance(box_r, QRectF) and box_r.contains(pos):
                return

        if self.hovered_player_action_rects:
            box_p = self.hovered_player_action_rects.get("box")
            if box_p and isinstance(box_p, QRectF) and box_p.contains(pos):
                return

        has_selection = bool(self.selected_entity or self.selected_waypoint or self.selected_player)

        # 1. การจัดการ Waypoint Hover
        if self.selected_waypoint:
            base_scale = (side / GatewayProjection.SVG_WIDTH) * self.interactive_map_scale
            sx = map_rect.center().x() + self.fullmap_pan_x + (self.selected_waypoint.x - GatewayProjection.SVG_WIDTH / 2) * base_scale
            sy = map_rect.center().y() + self.fullmap_pan_y + (self.selected_waypoint.y - GatewayProjection.SVG_HEIGHT / 2) * base_scale
            self.hovered_waypoint = self.selected_waypoint
            self.hovered_wp_screen_pos = QPointF(sx, sy)
        elif not has_selection and map_rect.contains(pos):
            base_scale = (side / GatewayProjection.SVG_WIDTH) * self.interactive_map_scale
            found_wp = None
            found_screen_pos = None
            for wp in self.waypoints:
                screen_x = map_rect.center().x() + self.fullmap_pan_x + (wp.x - GatewayProjection.SVG_WIDTH / 2) * base_scale
                screen_y = map_rect.center().y() + self.fullmap_pan_y + (wp.y - GatewayProjection.SVG_HEIGHT / 2) * base_scale
                if math.hypot(pos.x() - screen_x, pos.y() - screen_y) <= 18:
                    found_wp = wp
                    found_screen_pos = QPointF(screen_x, screen_y)
                    break
            if found_wp != self.hovered_waypoint:
                self.hovered_waypoint = found_wp
                self.hovered_wp_screen_pos = found_screen_pos
                if not found_wp:
                    self.hovered_wp_action_rects = {}
                self.update()
        elif not self.selected_waypoint:
            if self.hovered_waypoint:
                self.hovered_waypoint = None
                self.hovered_wp_action_rects = {}
                self.update()

        # 2. การจัดการ Player Hover
        if self.selected_player:
            base_scale = (side / GatewayProjection.SVG_WIDTH) * self.interactive_map_scale
            sx = map_rect.center().x() + self.fullmap_pan_x + (self.selected_player.current_pos["x"] - GatewayProjection.SVG_WIDTH / 2) * base_scale
            sy = map_rect.center().y() + self.fullmap_pan_y + (self.selected_player.current_pos["y"] - GatewayProjection.SVG_HEIGHT / 2) * base_scale
            self.hovered_player = self.selected_player
            self.hovered_player_screen_pos = QPointF(sx, sy)
        elif not has_selection and map_rect.contains(pos) and not self.hovered_waypoint:
            base_scale = (side / GatewayProjection.SVG_WIDTH) * self.interactive_map_scale
            found_p = None
            found_p_pos = None
            for pid, p in self.players.items():
                if pid == self.local_player_id:
                    continue
                screen_x = map_rect.center().x() + self.fullmap_pan_x + (p.current_pos["x"] - GatewayProjection.SVG_WIDTH / 2) * base_scale
                screen_y = map_rect.center().y() + self.fullmap_pan_y + (p.current_pos["y"] - GatewayProjection.SVG_HEIGHT / 2) * base_scale
                if math.hypot(pos.x() - screen_x, pos.y() - screen_y) <= 16:
                    found_p = p
                    found_p_pos = QPointF(screen_x, screen_y)
                    break
            if found_p != self.hovered_player:
                self.hovered_player = found_p
                self.hovered_player_screen_pos = found_p_pos
                if not found_p:
                    self.hovered_player_action_rects = {}
                self.update()
        elif not self.selected_player:
            if self.hovered_player:
                self.hovered_player = None
                self.hovered_player_action_rects = {}
                self.update()

        # 3. ตรวจสอบ Zone และ Entity (Item/Salt/Food) Hover
        if map_rect.contains(pos):
            base_scale = (side / GatewayProjection.SVG_WIDTH) * self.interactive_map_scale
            svg_x = (pos.x() - map_rect.center().x() - self.fullmap_pan_x) / base_scale + (GatewayProjection.SVG_WIDTH / 2)
            svg_y = (pos.y() - map_rect.center().y() - self.fullmap_pan_y) / base_scale + (GatewayProjection.SVG_HEIGHT / 2)

            if self.selected_entity:
                sx = map_rect.center().x() + self.fullmap_pan_x + (self.selected_entity["x"] - GatewayProjection.SVG_WIDTH / 2) * base_scale
                sy = map_rect.center().y() + self.fullmap_pan_y + (self.selected_entity["y"] - GatewayProjection.SVG_HEIGHT / 2) * base_scale
                self.layers_mgr.hovered_entity_info = {
                    "entity": self.selected_entity,
                    "screen_x": sx,
                    "screen_y": sy
                }
                self.layers_mgr.hovered_zone_info = None
            elif not has_selection and not self.hovered_waypoint and not self.hovered_player:
                entity = self.layers_mgr.check_entity_hover(svg_x, svg_y, threshold_svg=7.0 / max(self.interactive_map_scale, 0.5))
                if entity:
                    self.layers_mgr.hovered_entity_info = {
                        "entity": entity,
                        "screen_x": pos.x(),
                        "screen_y": pos.y()
                    }
                else:
                    self.layers_mgr.hovered_entity_info = None

                zones = self.layers_mgr.check_zone_hover(svg_x, svg_y)
                if zones:
                    self.layers_mgr.hovered_zone_info = {
                        "zones": zones,
                        "screen_x": pos.x(),
                        "screen_y": pos.y()
                    }
                else:
                    self.layers_mgr.hovered_zone_info = None
            elif not self.selected_entity:
                self.layers_mgr.hovered_entity_info = None
                self.layers_mgr.hovered_zone_info = None
            self.update()
        else:
            if not self.selected_entity:
                self.layers_mgr.hovered_entity_info = None
            if self.layers_mgr.hovered_zone_info:
                self.layers_mgr.hovered_zone_info = None
            self.update()

    def _handle_track_menu_click(self, pos: QtCore.QPoint, rect: QRectF):
        y_offset = rect.y() + 45
        if QRectF(rect.x() + 10, y_offset, rect.width() - 20, 28).contains(pos):
            self.tracked_target = None
            self.update()
            return
        y_offset += 35

        for pid, p in self.players.items():
            item_rect = QRectF(rect.x() + 10, y_offset, rect.width() - 20, 28)
            if item_rect.contains(pos):
                self.tracked_target = {"type": "player", "id": pid, "name": p.name}
                self.update()
                return
            y_offset += 30

        for wp in self.waypoints:
            item_rect = QRectF(rect.x() + 10, y_offset, rect.width() - 20, 28)
            if item_rect.contains(pos):
                self.tracked_target = {"type": "waypoint", "id": wp.id, "name": wp.name}
                self.update()
                return
            y_offset += 30

    def _handle_config_menu_click(self, pos: QtCore.QPoint, rect: QRectF):
        # ปุ่มขนาด Minimap
        btn_y = rect.y() + 50
        btn_w = 80
        for i, mode in enumerate(["Small", "Medium", "Large"]):
            btn_rect = QRectF(rect.x() + 20 + i * (btn_w + 10), btn_y, btn_w, 30)
            if btn_rect.contains(pos):
                self.minimap_size_mode = mode
                self.minimap_size = self.minimap_sizes[mode]
                self._save_mini_config()
                self.update()
                return

        # ปุ่มความโปร่งแสง Opacity
        btn_y = rect.y() + 130
        for i, op in enumerate([0.3, 0.6, 0.85, 1.0]):
            btn_rect = QRectF(rect.x() + 20 + i * 65, btn_y, 55, 30)
            if btn_rect.contains(pos):
                self.minimap_opacity = op
                self._save_mini_config()
                self.update()
                return

        # สลับเปิด/ปิด Layer ต่างๆ (Zones, Items/Salt, Food, Labels)
        btn_y = rect.y() + 205
        btn_w = 68
        # Toggle Zones
        if QRectF(rect.x() + 15, btn_y, btn_w, 28).contains(pos):
            self.layers_mgr.show_zones = not self.layers_mgr.show_zones
            self._save_mini_config()
            self.update()
            return
        # Toggle Items (Salt/Gastro/Clam)
        if QRectF(rect.x() + 90, btn_y, btn_w, 28).contains(pos):
            self.layers_mgr.show_items = not self.layers_mgr.show_items
            self._save_mini_config()
            self.update()
            return
        # Toggle Labels
        if QRectF(rect.x() + 165, btn_y, btn_w, 28).contains(pos):
            self.layers_mgr.show_labels_region = not self.layers_mgr.show_labels_region
            self._save_mini_config()
            self.update()
            return
        # Toggle Food
        if QRectF(rect.x() + 240, btn_y, btn_w, 28).contains(pos):
            self.layers_mgr.show_food = not self.layers_mgr.show_food
            self._save_mini_config()
            self.update()
            return

        curr_y = rect.y() + 248

        # สลับเปิด/ปิด Zone Categories แต่ละตัว (เฉพาะเมื่อ show_zones เปิดอยู่)
        if self.layers_mgr.show_zones:
            curr_y += 13
            zone_cats = [
                ("sanctuary", 15, 95),
                ("migration", 115, 95),
                ("patrol", 215, 95)
            ]
            for cat_id, offset_x, bw in zone_cats:
                if QRectF(rect.x() + offset_x, curr_y, bw, 26).contains(pos):
                    if cat_id in self.layers_mgr.active_zone_categories:
                        self.layers_mgr.active_zone_categories.remove(cat_id)
                    else:
                        self.layers_mgr.active_zone_categories.add(cat_id)
                    self._save_mini_config()
                    self.update()
                    return
            curr_y += 38

        # สลับเปิด/ปิด Food Categories แต่ละตัว (เฉพาะเมื่อ show_food เปิดอยู่)
        if self.layers_mgr.show_food:
            curr_y += 13
            food_cats = [
                ("animal", 15),
                ("fruit", 75),
                ("vegetable", 135),
                ("plant", 195),
                ("mushroom", 255)
            ]
            for cat_id, offset_x in food_cats:
                if QRectF(rect.x() + offset_x, curr_y, 55, 26).contains(pos):
                    if cat_id in self.layers_mgr.active_food_categories:
                        self.layers_mgr.active_food_categories.remove(cat_id)
                    else:
                        self.layers_mgr.active_food_categories.add(cat_id)
                    self._save_mini_config()
                    self.update()
                    return
            curr_y += 38

        slider_w = 280
        # ตรวจสอบการคลิกบน Slide Bar: Minimap Brightness
        curr_y += 20
        track_rect_mini = QRectF(rect.x() + 20, curr_y - 6, slider_w, 20)
        if track_rect_mini.contains(pos):
            ratio = max(0.0, min(1.0, (pos.x() - (rect.x() + 20)) / slider_w))
            self.minimap_brightness = round(0.2 + ratio * 0.8, 2)
            self.dragging_slider = "minimap_br"
            self._save_mini_config()
            self.update()
            return
        curr_y += 28

        # ตรวจสอบการคลิกบน Slide Bar: Full Map Brightness
        curr_y += 20
        track_rect_full = QRectF(rect.x() + 20, curr_y - 6, slider_w, 20)
        if track_rect_full.contains(pos):
            ratio = max(0.0, min(1.0, (pos.x() - (rect.x() + 20)) / slider_w))
            self.fullmap_brightness = round(0.2 + ratio * 0.8, 2)
            self.dragging_slider = "fullmap_br"
            self._save_mini_config()
            self.update()
            return
        curr_y += 28

        # ตรวจสอบการคลิกบน Slide Bar: Entity / Icon Scale (0.5 - 2.0)
        curr_y += 20
        track_rect_scale = QRectF(rect.x() + 20, curr_y - 6, slider_w, 20)
        if track_rect_scale.contains(pos):
            ratio = max(0.0, min(1.0, (pos.x() - (rect.x() + 20)) / slider_w))
            self.entity_icon_scale = round(0.5 + ratio * 1.5, 2)
            self.dragging_slider = "icon_scale"
            self._save_mini_config()
            self.update()
            return
        curr_y += 32

        # ปุ่มตั้งค่า Session Auth / Live Map
        auth_rect = QRectF(rect.x() + 15, curr_y + 4, rect.width() - 30, 30)
        if auth_rect.contains(pos):
            self.open_session_auth_dialog()
            return
        curr_y += 38

        # ปุ่ม Reset All to Off (ปิดทุก Layer, Zone และ Food ทุกหมวด)
        reset_rect = QRectF(rect.x() + 15, curr_y + 4, rect.width() - 30, 30)
        if reset_rect.contains(pos):
            self.layers_mgr.show_zones = False
            self.layers_mgr.show_items = False
            self.layers_mgr.show_labels_region = False
            self.layers_mgr.show_labels_all = False
            self.layers_mgr.show_food = False
            self.layers_mgr.active_food_categories.clear()
            self.layers_mgr.active_zone_categories.clear()
            self._save_mini_config()
            self.update()
            return

    def open_session_auth_dialog(self):
        """เปิดหน้าต่างตั้งค่า Session Auth & ระบบปาร์ตี้ส่วนตัว (Party Room / PIN Code)"""
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("👥 ตั้งค่าปาร์ตี้ & Live Telemetry")
        dialog.setFixedWidth(500)
        dialog.setStyleSheet("""
            QDialog { background-color: #12100E; color: #FFFFFF; }
            QLabel { color: #F0E2C0; font-weight: bold; }
            QLineEdit { background-color: #1E1B18; color: #FFFFFF; border: 1px solid #00E5FF; border-radius: 4px; padding: 6px; }
            QPushButton { background-color: #00E5FF; color: #000000; font-weight: bold; border-radius: 4px; padding: 8px; }
            QPushButton:hover { background-color: #33EBFF; }
            QTabWidget::pane { border: 1px solid #332E27; background: #181512; border-radius: 6px; padding: 8px; }
            QTabBar::tab { background: #25211D; color: #CCCCCC; padding: 8px 14px; font-weight: bold; border-top-left-radius: 4px; border-top-right-radius: 4px; margin-right: 2px; }
            QTabBar::tab:selected { background: #00E5FF; color: #000000; }
            QRadioButton { color: #FFFFFF; font-weight: bold; }
        """)

        main_layout = QtWidgets.QVBoxLayout(dialog)

        # Tab Widget
        tabs = QtWidgets.QTabWidget()

        # ================= TAB 1: ปาร์ตี้ส่วนตัว (Party Room / PIN Code) =================
        tab_party = QtWidgets.QWidget()
        layout_party = QtWidgets.QVBoxLayout(tab_party)

        lbl_party_head = QtWidgets.QLabel("🔐 ห้องปาร์ตี้ส่วนตัว (Direct P2P / LAN / VPN)")
        lbl_party_head.setStyleSheet("font-size: 13px; color: #00FF66;")
        layout_party.addWidget(lbl_party_head)

        lbl_party_sub = QtWidgets.QLabel("เพื่อนทุกคนเพียงตั้ง Party Code / PIN เดียวกัน จะมองเห็นตำแหน่งและทิศทางของกันทันที โดยไม่ต้องง้อเว็บ Voice-Island:")
        lbl_party_sub.setWordWrap(True)
        lbl_party_sub.setStyleSheet("font-size: 10px; color: #CCCCCC; font-weight: normal; margin-bottom: 6px;")
        layout_party.addWidget(lbl_party_sub)

        # Party Code / PIN
        layout_party.addWidget(QtWidgets.QLabel("🏷️ Party Code / PIN (รหัสห้อง):"))
        txt_party_code = QtWidgets.QLineEdit(self.party_code)
        txt_party_code.setPlaceholderText("เช่น ISLE77, DINO99")
        layout_party.addWidget(txt_party_code)

        # Player Name
        layout_party.addWidget(QtWidgets.QLabel("👤 ชื่อของคุณในปาร์ตี้:"))
        txt_player_name = QtWidgets.QLineEdit(self.party_player_name)
        txt_player_name.setPlaceholderText("เช่น .xda_7, Hunter")
        layout_party.addWidget(txt_player_name)

        # Host / Client Mode
        role_box = QtWidgets.QHBoxLayout()
        rad_host = QtWidgets.QRadioButton("👑 สร้างห้อง (Host)")
        rad_join = QtWidgets.QRadioButton("👥 เข้าร่วมห้อง (Join)")
        if self.party_is_host:
            rad_host.setChecked(True)
        else:
            rad_join.setChecked(True)
        role_box.addWidget(rad_host)
        role_box.addWidget(rad_join)
        layout_party.addLayout(role_box)

        # Direct Peer IP (สำหรับเล่นข้ามวงเน็ต เช่น Tailscale / Radmin / IP เพื่อน)
        layout_party.addWidget(QtWidgets.QLabel("🌐 IP เพื่อนในตี้ (ถ้าอยู่คนละวงเน็ต / Tailscale / Radmin VPN):"))
        txt_remote_ips = QtWidgets.QLineEdit(", ".join(self.party_remote_hosts))
        txt_remote_ips.setPlaceholderText("เว้นว่างได้หากอยู่ใน LAN เดียวกัน (เช่น 100.64.1.2, 192.168.1.50)")
        layout_party.addWidget(txt_remote_ips)

        lbl_party_status = QtWidgets.QLabel(f"สถานะปัจจุบัน: {self.telemetry_status_msg}")
        lbl_party_status.setStyleSheet("color: #00FF66; font-size: 11px;")
        layout_party.addWidget(lbl_party_status)

        layout_party.addStretch()
        tabs.addTab(tab_party, "👥 ห้องปาร์ตี้ส่วนตัว (Party PIN)")

        # ================= TAB 2: Live Map Web Auth (Voice-Island) =================
        tab_web = QtWidgets.QWidget()
        layout_web = QtWidgets.QVBoxLayout(tab_web)

        lbl_info = QtWidgets.QLabel("🌐 เชื่อมต่อ Live Map: voice-island.com")
        lbl_info.setStyleSheet("font-size: 13px; color: #00E5FF;")
        layout_web.addWidget(lbl_info)

        lbl_desc = QtWidgets.QLabel("ระบุ Cookie หรือ Bearer Token ที่ได้จาก Browser เพื่อดึงข้อมูลตำแหน่งเพื่อนร่วมปาร์ตี้:")
        lbl_desc.setWordWrap(True)
        lbl_desc.setStyleSheet("font-size: 10px; color: #CCCCCC; font-weight: normal;")
        layout_web.addWidget(lbl_desc)

        # API URL
        layout_web.addWidget(QtWidgets.QLabel("API Endpoint URL:"))
        txt_url = QtWidgets.QLineEdit(self.telemetry_api_url)
        layout_web.addWidget(txt_url)

        # Cookie Header
        layout_web.addWidget(QtWidgets.QLabel("Cookie Header (connect.sid / session):"))
        txt_cookie = QtWidgets.QLineEdit(self.telemetry_cookie)
        txt_cookie.setPlaceholderText("เช่น connect.sid=s%3A... หรือ cookies ทั้งหมด")
        layout_web.addWidget(txt_cookie)

        # Auth Token
        layout_web.addWidget(QtWidgets.QLabel("Authorization Token (Bearer Token):"))
        txt_token = QtWidgets.QLineEdit(self.telemetry_token)
        txt_token.setPlaceholderText("เช่น eyJhbGciOi... หรือ Discord OAuth Token")
        layout_web.addWidget(txt_token)

        # Polling Rate Slider (1.0s - 5.0s)
        rate_box = QtWidgets.QHBoxLayout()
        lbl_rate_title = QtWidgets.QLabel("⏱️ ความถี่ดึงข้อมูล (Polling Rate):")
        lbl_rate_val = QtWidgets.QLabel(f"{self.telemetry_interval:.1f} วินาที")
        lbl_rate_val.setStyleSheet("color: #00E5FF; font-weight: bold;")
        rate_box.addWidget(lbl_rate_title)
        rate_box.addStretch()
        rate_box.addWidget(lbl_rate_val)
        layout_web.addLayout(rate_box)

        slider_rate = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        slider_rate.setMinimum(10)
        slider_rate.setMaximum(50)
        slider_rate.setValue(int(self.telemetry_interval * 10))
        slider_rate.setStyleSheet("""
            QSlider::groove:horizontal { height: 6px; background: #2A2622; border-radius: 3px; }
            QSlider::sub-page:horizontal { background: #00E5FF; border-radius: 3px; }
            QSlider::handle:horizontal { background: #FFFFFF; border: 2px solid #00E5FF; width: 14px; height: 14px; margin: -5px 0; border-radius: 7px; }
        """)
        def on_slider_change(v):
            sec = v / 10.0
            lbl_rate_val.setText(f"{sec:.1f} วินาที")
        slider_rate.valueChanged.connect(on_slider_change)
        layout_web.addWidget(slider_rate)

        # Test Status Label
        lbl_test = QtWidgets.QLabel(f"สถานะปัจจุบัน: {self.telemetry_status_msg}")
        lbl_test.setStyleSheet("color: #FFB400; font-size: 11px;")
        layout_web.addWidget(lbl_test)

        btn_test = QtWidgets.QPushButton("⚡ ทดสอบเชื่อมต่อ Web (Test)")
        btn_test.setStyleSheet("background-color: #FFB400; color: #000000;")
        def on_test():
            lbl_test.setText("⏳ กำลังทดสอบเชื่อมต่อ...")
            QtWidgets.QApplication.processEvents()
            self.poller.configure(
                api_url=txt_url.text().strip(),
                cookie_header=txt_cookie.text().strip(),
                auth_token=txt_token.text().strip(),
                interval_sec=slider_rate.value() / 10.0
            )
            data, status, msg, lat = self.poller.fetch_once()
            lbl_test.setText(f"ผลการทดสอบ: {msg} ({lat}ms)")
            if status == "connected":
                lbl_test.setStyleSheet("color: #00FF66; font-size: 11px;")
            elif status == "unauthorized":
                lbl_test.setStyleSheet("color: #FFB400; font-size: 11px;")
            else:
                lbl_test.setStyleSheet("color: #FF4444; font-size: 11px;")
        btn_test.clicked.connect(on_test)
        layout_web.addWidget(btn_test)

        layout_web.addStretch()
        tabs.addTab(tab_web, "🌐 เว็บ Voice-Island (Auth)")

        # เลือกแท็บเริ่มต้นตาม sync_mode
        if self.sync_mode == "party_room":
            tabs.setCurrentIndex(0)
        else:
            tabs.setCurrentIndex(1)

        main_layout.addWidget(tabs)

        # ปุ่มบันทึก & ยกเลิก
        btn_box = QtWidgets.QHBoxLayout()
        btn_save = QtWidgets.QPushButton("💾 บันทึก & เริ่มเชื่อมต่อ")
        def on_save():
            # ตรวจสอบแท็บที่เลือกเพื่อสลับโหมด
            is_party_tab = (tabs.currentIndex() == 0)
            self.sync_mode = "party_room" if is_party_tab else "web"

            # 1. บันทึกค่า Party Room
            self.party_code = txt_party_code.text().strip().upper() or "ISLE77"
            self.party_player_name = txt_player_name.text().strip() or ".xda_7"
            self.party_is_host = rad_host.isChecked()
            raw_ips = txt_remote_ips.text().split(",")
            self.party_remote_hosts = [ip.strip() for ip in raw_ips if ip.strip()]

            # 2. บันทึกค่า Web Telemetry
            self.telemetry_api_url = txt_url.text().strip()
            self.telemetry_cookie = txt_cookie.text().strip()
            self.telemetry_token = txt_token.text().strip()
            self.telemetry_interval = slider_rate.value() / 10.0

            # 3. จัดการสลับการทำงานของโหนด
            if self.sync_mode == "party_room":
                self.poller.stop()
                self.party_node.configure(
                    party_code=self.party_code,
                    player_name=self.party_player_name,
                    is_host=self.party_is_host,
                    remote_hosts=self.party_remote_hosts
                )
                self.party_node.start(self.on_party_received, self.on_party_status)
            else:
                self.party_node.stop()
                self.poller.configure(
                    api_url=self.telemetry_api_url,
                    cookie_header=self.telemetry_cookie,
                    auth_token=self.telemetry_token,
                    interval_sec=self.telemetry_interval
                )
                self.poller.start(self.on_telemetry_received, self.on_telemetry_status)

            self._save_mini_config()
            dialog.accept()
            self.update()

        btn_save.clicked.connect(on_save)
        btn_box.addWidget(btn_save)

        btn_cancel = QtWidgets.QPushButton("ยกเลิก")
        btn_cancel.setStyleSheet("background-color: #444444; color: #FFFFFF;")
        btn_cancel.clicked.connect(dialog.reject)
        btn_box.addWidget(btn_cancel)

        main_layout.addLayout(btn_box)
        dialog.exec_()

    def _handle_map_create_waypoint(self, screen_pos: QtCore.QPoint):
        side = min(self.width() - 140, self.height() - 120)
        map_rect = QRectF((self.width() - side) / 2, 60, side, side)
        if map_rect.contains(screen_pos):
            base_scale = (side / GatewayProjection.SVG_WIDTH) * self.interactive_map_scale
            # คำนวณพิกัด SVG ร่วมกับ Pan Offset
            svg_x = (screen_pos.x() - map_rect.center().x() - self.fullmap_pan_x) / base_scale + (GatewayProjection.SVG_WIDTH / 2)
            svg_y = (screen_pos.y() - map_rect.center().y() - self.fullmap_pan_y) / base_scale + (GatewayProjection.SVG_HEIGHT / 2)

            name, ok = QtWidgets.QInputDialog.getText(
                self, "สร้าง Waypoint ร่วมกัน", "ระบุชื่อ Waypoint:", QtWidgets.QLineEdit.Normal, "จุดพักผ่อน"
            )
            if ok and name.strip():
                icon_choice, ok2 = QtWidgets.QInputDialog.getItem(
                    self, "เลือกไอคอน", "ประเภท:", ["camp", "danger", "water", "food"], 0, False
                )
                icon = icon_choice if ok2 else "camp"
                color_map = {"camp": "#00E5FF", "danger": "#FF3366", "water": "#00A2FF", "food": "#FFD600"}
                new_wp = Waypoint(name.strip(), svg_x, svg_y, icon, color_map.get(icon, "#FFD600"))
                self.waypoints.append(new_wp)
                self._save_waypoints()
                self.update()

    def on_telemetry_status(self, status: str, msg: str, latency: int):
        if self.sync_mode == "web":
            self.telemetry_status = status
            self.telemetry_status_msg = msg
            self.telemetry_latency = latency

    def on_party_status(self, status: str, msg: str, latency: int):
        if self.sync_mode == "party_room":
            self.telemetry_status = status
            self.telemetry_status_msg = msg
            self.telemetry_latency = latency

    def _broadcast_local_position(self):
        """ส่งพิกัดของตัวเราให้เพื่อนในปาร์ตี้ผ่าน PartySyncNode"""
        if self.sync_mode != "party_room":
            return
        local_p = self.players.get(self.local_player_id)
        if local_p:
            svg_x = local_p.current_pos["x"]
            svg_y = local_p.current_pos["y"]
            gx, gy = GatewayProjection.unproject_coordinates(svg_x, svg_y)
            self.party_node.broadcast_my_position(gx, gy, local_p.heading_angle, self.self_color)

    def on_party_received(self, data: Dict):
        """เมื่อได้รับอัปเดตจาก Party Room Node"""
        if self.sync_mode != "party_room":
            return
        player_list = data.get("players", [])
        active_ids = {p["id"] for p in player_list}

        # ลบเพื่อนที่ออกจากห้องหรือหมดเวลา (ยกเว้นตัวเราเอง)
        for pid in list(self.players.keys()):
            if pid != self.local_player_id and pid not in active_ids:
                del self.players[pid]

        for p_item in player_list:
            pid = p_item.get("id")
            if not pid or pid == self.local_player_id:
                continue
            name = p_item.get("name", "Party Member")
            gx = float(p_item.get("x", 0))
            gy = float(p_item.get("y", 0))
            color = p_item.get("color") or PARTY_PALETTE[abs(hash(pid)) % len(PARTY_PALETTE)]
            svg_x, svg_y = GatewayProjection.project_coordinates(gx, gy)

            if pid not in self.players:
                self.players[pid] = PlayerMovementState(pid, name, svg_x, svg_y, color)
                self.players[pid].heading_angle = float(p_item.get("heading", 0.0))
            else:
                self.movement_tracker.update_player(self.players[pid], svg_x, svg_y)
                self.players[pid].heading_angle = float(p_item.get("heading", self.players[pid].heading_angle))

    def on_telemetry_received(self, data: Dict):
        if self.sync_mode != "web":
            return
        if isinstance(data, dict):
            player_list = data.get("players", [])
        elif isinstance(data, list):
            player_list = data
        else:
            player_list = []
        # หากยังไม่มี local_player_id หรือหาไม่เจอ ให้ลองหาจากชื่อ .xda_7 หรือตัวแรก
        if self.local_player_id not in self.players and player_list:
            for item in player_list:
                pname = item.get("name", "")
                pid = item.get("id") or pname
                if pname == ".xda_7" or pid == ".xda_7":
                    self.local_player_id = pid
                    break
            if self.local_player_id not in self.players and self.local_player_id == "local_player":
                # Fallback กำหนดตัวแรกเป็น local player
                first_p = player_list[0]
                self.local_player_id = first_p.get("id") or first_p.get("name")

        for idx, item in enumerate(player_list):
            pid = item.get("id") or item.get("name")
            name = item.get("name", "Player")
            gx = float(item.get("x", 0))
            gy = float(item.get("y", 0))
            svg_x, svg_y = GatewayProjection.project_coordinates(gx, gy)

            if pid not in self.players:
                if pid == self.local_player_id or name == ".xda_7":
                    color = self.self_color
                    self.local_player_id = pid
                else:
                    color = PARTY_PALETTE[abs(hash(pid)) % len(PARTY_PALETTE)]
                self.players[pid] = PlayerMovementState(pid, name, svg_x, svg_y, color)
            else:
                self.movement_tracker.update_player(self.players[pid], svg_x, svg_y)

    def paintEvent(self, event):
        painter = QPainter()
        if not painter.begin(self):
            return
        try:
            painter.setRenderHint(QPainter.Antialiasing)

            if self.is_interactive:
                self.draw_fullmap(painter)
                self.draw_interactive_overlays(painter)
            else:
                # เมื่ออยู่ในโหมดเล่นเกมปกติ (In-Game Mode) วาด HUD เข็มทิศและตัวชี้เป้าหมายรอบหน้าจอ
                self.draw_hud_tracking_indicator(painter)

            if self.minimap_visible:
                self.draw_minimap(painter)
        finally:
            painter.end()

    def draw_minimap(self, painter: QPainter):
        margin = 16
        size = self.minimap_size
        rect = QRectF(margin, margin, size, size)

        # 1. กรอบพื้นหลัง Minimap (ใช้อัลฟ่าของ self.minimap_opacity)
        bg_alpha = int(255 * self.minimap_opacity)
        bg_color = QColor(14, 10, 8, bg_alpha)
        border_pen = QPen(QColor(240, 226, 192, min(bg_alpha, 160)), 2)
        painter.setBrush(QBrush(bg_color))
        painter.setPen(border_pen)
        painter.drawRoundedRect(rect, 14, 14)

        # หัวข้อ Minimap & ซูม
        painter.setFont(QFont("Monospace", 8, QFont.Bold))
        painter.setPen(QColor(240, 226, 192, bg_alpha))
        painter.drawText(int(rect.x() + 12), int(rect.y() + 20), f"VOICE-HUD [{self.minimap_scale:.2f}x] Opacity:{int(self.minimap_opacity*100)}%")

        local_p = self.players.get(self.local_player_id)
        center_x = local_p.current_pos["x"] if local_p else GatewayProjection.SVG_WIDTH / 2
        center_y = local_p.current_pos["y"] if local_p else GatewayProjection.SVG_HEIGHT / 2

        # 2. เรนเดอร์ตัวแผนที่พร้อม Opacity
        painter.save()
        painter.setClipRect(rect.adjusted(2, 2, -2, -2))
        painter.setOpacity(self.minimap_opacity)  # ใช้ Opacity กับ Layer แผนที่ทั้งหมด

        # เลื่อนมุมมองให้ Player อยู่กึ่งกลาง
        painter.translate(rect.center().x(), rect.center().y())
        painter.scale(self.minimap_scale, self.minimap_scale)
        painter.translate(-center_x, -center_y)

        # วาดรูปแผนที่ Gateway
        if self.map_pixmap:
            painter.drawPixmap(
                QRectF(0, 0, GatewayProjection.SVG_WIDTH, GatewayProjection.SVG_HEIGHT),
                self.map_pixmap,
                QRectF(0, 0, self.map_pixmap.width(), self.map_pixmap.height())
            )
            # ปรับแต่งความสว่างของ Minimap ตาม self.minimap_brightness (0.4 - 1.0)
            if self.minimap_brightness < 0.99:
                dark_alpha = int(255 * (1.0 - self.minimap_brightness))
                painter.setBrush(QBrush(QColor(0, 0, 0, dark_alpha)))
                painter.setPen(Qt.NoPen)
                painter.drawRect(QRectF(0, 0, GatewayProjection.SVG_WIDTH, GatewayProjection.SVG_HEIGHT))

        # วาด Gateway Layers (Items, Salt, Gastro)
        minimap_icon_scale = (self.entity_icon_scale / max(self.minimap_scale, 0.05))
        self.layers_mgr.draw_layers(painter, is_minimap=True, entity_scale=minimap_icon_scale)

        # วาด Waypoints
        self.draw_waypoints(painter, is_minimap=True)

        # วาดผู้เล่น, Pack Points, รอยเท้า
        self.draw_game_elements(painter, is_minimap=True)

        painter.restore()

        # วาด Off-screen indicator บนขอบ Minimap
        self.draw_offscreen_indicator(painter, rect, center_x, center_y)

    def draw_offscreen_indicator(self, painter: QPainter, minimap_rect: QRectF, center_x: float, center_y: float):
        if not self.tracked_target:
            return

        tx, ty = self._get_tracked_position()
        if tx is None or ty is None:
            return

        dx = (tx - center_x) * self.minimap_scale
        dy = (ty - center_y) * self.minimap_scale
        dist = math.hypot(dx, dy)
        half_w = (minimap_rect.width() / 2) - 18
        half_h = (minimap_rect.height() / 2) - 18

        if abs(dx) > half_w or abs(dy) > half_h:
            angle = math.atan2(dy, dx)
            edge_x = minimap_rect.center().x() + math.cos(angle) * min(dist, half_w)
            edge_y = minimap_rect.center().y() + math.sin(angle) * min(dist, half_h)

            painter.save()
            painter.translate(edge_x, edge_y)
            painter.rotate(math.degrees(angle))

            arrow = QPolygonF([QPointF(10, 0), QPointF(-6, -6), QPointF(-2, 0), QPointF(-6, 6)])
            painter.setBrush(QBrush(QColor(0, 229, 255)))
            painter.setPen(QPen(QColor(255, 255, 255), 1))
            painter.drawPolygon(arrow)
            painter.restore()

            dist_m = int(dist * 1.37 / self.minimap_scale)
            painter.setFont(QFont("Monospace", 7, QFont.Bold))
            painter.setPen(QColor(0, 229, 255))
            painter.drawText(int(edge_x - 15), int(edge_y + 16), f"{dist_m}m")

    def draw_hud_tracking_indicator(self, painter: QPainter):
        """
        แสดง Off-screen Target Arrow และ Compass HUD ในโหมดเล่นเกมปกติ (In-Game HUD)
        """
        if not self.tracked_target:
            return

        tx, ty = self._get_tracked_position()
        if tx is None or ty is None:
            return

        local_p = self.players.get(self.local_player_id)
        if not local_p:
            return

        lx, ly = local_p.current_pos["x"], local_p.current_pos["y"]
        dx = tx - lx
        dy = ty - ly
        dist_svg = math.hypot(dx, dy)
        dist_m = int(dist_svg * 1.37)
        t_name = self.tracked_target.get("name", "Target")

        # 1. Compass Bar ด้านบนกึ่งกลางจอ (N, E, S, W พร้อมไอคอนเป้าหมาย)
        bar_w = 420
        bar_h = 32
        bar_x = (self.width() - bar_w) / 2
        bar_y = 18
        compass_rect = QRectF(bar_x, bar_y, bar_w, bar_h)

        # พื้นหลังเข็มทิศ
        painter.setBrush(QBrush(QColor(12, 10, 8, 190)))
        painter.setPen(QPen(QColor(240, 226, 192, 90), 1))
        painter.drawRoundedRect(compass_rect, 6, 6)

        # ขีดบอกองศาตรงกลาง
        painter.setFont(QFont("SansSerif", 8, QFont.Bold))
        painter.setPen(QColor(240, 226, 192, 180))
        painter.drawText(compass_rect, Qt.AlignCenter, f"▲ 🎯 {t_name} — {dist_m}m")

        # 2. Off-Screen Edge Arrow (ลูกศรชี้ตามขอบจอ)
        # คำนวณมุม relative กับตำแหน่งของผู้เล่น
        screen_cx = self.width() / 2
        screen_cy = self.height() / 2
        margin = 60
        max_rx = screen_cx - margin
        max_ry = screen_cy - margin

        angle = math.atan2(dy, dx)
        edge_x = screen_cx + math.cos(angle) * max_rx
        edge_y = screen_cy + math.sin(angle) * max_ry

        # วาดลูกศรนำทางขอบจอ
        painter.save()
        painter.translate(edge_x, edge_y)
        painter.rotate(math.degrees(angle))

        arrow = QPolygonF([QPointF(16, 0), QPointF(-10, -10), QPointF(-4, 0), QPointF(-10, 10)])
        painter.setBrush(QBrush(QColor(0, 229, 255, 230)))
        painter.setPen(QPen(QColor(255, 255, 255, 240), 1.5))
        painter.drawPolygon(arrow)
        painter.restore()

        # วาดข้อความบอกระยะทางกำกับที่ลูกศร
        tag_rect = QRectF(edge_x - 45, edge_y + 16, 90, 20)
        painter.setBrush(QBrush(QColor(10, 10, 10, 210)))
        painter.setPen(QPen(QColor(0, 229, 255, 180), 1))
        painter.drawRoundedRect(tag_rect, 4, 4)
        painter.setFont(QFont("Monospace", 7, QFont.Bold))
        painter.setPen(QColor(255, 255, 255))
        painter.drawText(tag_rect, Qt.AlignCenter, f"{dist_m}m")

    def _get_tracked_position(self) -> Tuple[Optional[float], Optional[float]]:
        if not self.tracked_target:
            return None, None
        ttype = self.tracked_target.get("type")
        tid = self.tracked_target.get("id")

        if ttype == "player" and tid in self.players:
            p = self.players[tid]
            return p.current_pos["x"], p.current_pos["y"]
        elif ttype == "waypoint":
            for wp in self.waypoints:
                if wp.id == tid:
                    return wp.x, wp.y
        elif "x" in self.tracked_target and "y" in self.tracked_target:
            return float(self.tracked_target["x"]), float(self.tracked_target["y"])
        return None, None

    def draw_fullmap(self, painter: QPainter):
        painter.setBrush(QBrush(QColor(10, 10, 14, 210)))
        painter.setPen(Qt.NoPen)
        painter.drawRect(self.rect())

        side = min(self.width() - 140, self.height() - 120)
        map_rect = QRectF((self.width() - side) / 2, 60, side, side)

        painter.save()
        painter.setBrush(QBrush(QColor(20, 20, 24, 240)))
        painter.setPen(QPen(QColor(240, 226, 192, 100), 2))
        painter.drawRoundedRect(map_rect, 10, 10)
        painter.setClipRect(map_rect)

        # ซูมและเลื่อนแผนที่ในโหมด Interactive Map (Hold Right-Click to Drag Map)
        base_scale = (side / GatewayProjection.SVG_WIDTH) * self.interactive_map_scale
        painter.translate(map_rect.center().x() + self.fullmap_pan_x, map_rect.center().y() + self.fullmap_pan_y)
        painter.scale(base_scale, base_scale)
        painter.translate(-GatewayProjection.SVG_WIDTH / 2, -GatewayProjection.SVG_HEIGHT / 2)

        if self.map_pixmap:
            painter.drawPixmap(
                QRectF(0, 0, GatewayProjection.SVG_WIDTH, GatewayProjection.SVG_HEIGHT),
                self.map_pixmap,
                QRectF(0, 0, self.map_pixmap.width(), self.map_pixmap.height())
            )
            # ปรับแต่งความสว่างของ Full Map ตาม self.fullmap_brightness (0.4 - 1.0)
            if self.fullmap_brightness < 0.99:
                dark_alpha = int(255 * (1.0 - self.fullmap_brightness))
                painter.setBrush(QBrush(QColor(0, 0, 0, dark_alpha)))
                painter.setPen(Qt.NoPen)
                painter.drawRect(QRectF(0, 0, GatewayProjection.SVG_WIDTH, GatewayProjection.SVG_HEIGHT))

        # วาด Gateway Layers (Sanctuaries, Patrol, Salt, Gastro, Landmark Labels)
        # ใช้ scale_factor คำนวณแบบ Fixed Screen Scale
        fullmap_icon_scale = (self.entity_icon_scale / max(self.interactive_map_scale, 0.05))
        self.layers_mgr.draw_layers(painter, is_minimap=False, entity_scale=fullmap_icon_scale)

        self.draw_waypoints(painter, is_minimap=False)
        self.draw_game_elements(painter, is_minimap=False)
        painter.restore()

    def draw_interactive_overlays(self, painter: QPainter):
        # Top Bar Info
        painter.setFont(QFont("SansSerif", 10, QFont.Bold))
        painter.setPen(QColor(255, 255, 255, 240))
        painter.drawText(
            40, 42,
            f"🗺️ VoiceHUD Interactive Mode [Zoom: {self.interactive_map_scale:.2f}x] — คลิกขวาค้างเพื่อลากเลื่อน | Scroll เพื่อซูม | [M] หรือ [ESC] ปิด"
        )

        # Live Telemetry Status Pill (มุมบนขวาถัดจากปุ่มควบคุม)
        status_color = QColor(100, 100, 100, 200)
        status_dot = "⚪"
        if self.telemetry_status == "connected":
            status_color = QColor(0, 230, 118, 220)
            status_dot = "🟢"
        elif self.telemetry_status == "unauthorized":
            status_color = QColor(255, 179, 0, 220)
            status_dot = "🟡"
        elif self.telemetry_status == "offline":
            status_color = QColor(255, 68, 68, 220)
            status_dot = "🔴"

        lat_text = f" ({self.telemetry_latency}ms)" if self.telemetry_latency > 0 else ""
        live_label = f"{status_dot} Live: {self.telemetry_status_msg}{lat_text}"
        font_live = QFont("SansSerif", 8, QFont.Bold)
        painter.setFont(font_live)
        fm_live = QtGui.QFontMetrics(font_live)
        live_w = fm_live.horizontalAdvance(live_label) + 20
        live_rect = QRectF(self.width() - 330 - live_w - 10, 20, live_w, 36)
        self.live_status_rect = live_rect

        painter.setBrush(QBrush(QColor(18, 16, 14, 245)))
        painter.setPen(QPen(status_color, 1.5))
        painter.drawRoundedRect(live_rect, 8, 8)
        painter.setPen(status_color)
        painter.drawText(live_rect, Qt.AlignCenter, live_label)

        # ปุ่ม Config Menu
        cfg_btn = QRectF(self.width() - 320, 20, 130, 36)
        painter.setBrush(QBrush(QColor(40, 44, 52, 230)))
        painter.setPen(QPen(QColor(240, 226, 192, 150), 1))
        painter.drawRoundedRect(cfg_btn, 8, 8)
        painter.setFont(QFont("SansSerif", 9, QFont.Bold))
        painter.setPen(QColor(240, 226, 192))
        painter.drawText(cfg_btn, Qt.AlignCenter, "⚙️ Mini Config")

        # ปุ่ม Track Menu
        track_btn = QRectF(self.width() - 170, 20, 140, 36)
        painter.setBrush(QBrush(QColor(0, 150, 255, 200) if self.tracked_target else QColor(40, 44, 52, 230)))
        painter.setPen(QPen(QColor(240, 226, 192, 150), 1))
        painter.drawRoundedRect(track_btn, 8, 8)
        painter.setFont(QFont("SansSerif", 9, QFont.Bold))
        painter.setPen(QColor(255, 255, 255))
        painter.drawText(track_btn, Qt.AlignCenter, "🧭 Track Menu")

        # Full Map Tracking Target HUD Card & Untrack Button
        if self.tracked_target:
            t_name = self.tracked_target.get("name", "เป้าหมาย")
            t_type = self.tracked_target.get("type", "target")
            tx, ty = self._get_tracked_position()
            dist_str = ""
            local_p = self.players.get(self.local_player_id)
            if local_p and tx is not None and ty is not None:
                d = math.hypot(tx - local_p.current_pos["x"], ty - local_p.current_pos["y"])
                dist_str = f" [{int(d * 1.37)}m]"

            hud_text = f"🎯 กำลังติดตาม: {t_name}{dist_str}"
            font_hud = QFont("SansSerif", 9, QFont.Bold)
            painter.setFont(font_hud)
            fm = QtGui.QFontMetrics(font_hud)
            t_width = fm.horizontalAdvance(hud_text)

            card_w = t_width + 115
            card_x = live_rect.x() - card_w - 12
            card_rect = QRectF(card_x, 20, card_w, 36)

            # Card background
            painter.setBrush(QBrush(QColor(18, 16, 14, 245)))
            painter.setPen(QPen(QColor(0, 229, 255, 180), 1.5))
            painter.drawRoundedRect(card_rect, 8, 8)

            painter.setPen(QColor(0, 229, 255))
            painter.drawText(int(card_rect.x() + 12), int(card_rect.y() + 23), hud_text)

            # Untrack button inside Card
            untrack_btn = QRectF(card_rect.right() - 85, card_rect.y() + 5, 75, 26)
            self.fullmap_untrack_rect = untrack_btn
            painter.setBrush(QBrush(QColor(220, 50, 50, 220)))
            painter.setPen(Qt.NoPen)
            painter.drawRoundedRect(untrack_btn, 5, 5)
            painter.setFont(QFont("SansSerif", 8, QFont.Bold))
            painter.setPen(QColor(255, 255, 255))
            painter.drawText(untrack_btn, Qt.AlignCenter, "✖ Untrack")
        else:
            self.fullmap_untrack_rect = None

        # Drawer: Config Menu
        if self.show_config_menu:
            extra_h = (55 if self.layers_mgr.show_zones else 0) + (55 if self.layers_mgr.show_food else 0)
            drawer_h = 535 + extra_h
            cfg_drawer = QRectF(self.width() - 350, 70, 330, drawer_h)
            painter.setBrush(QBrush(QColor(18, 16, 14, 245)))
            painter.setPen(QPen(QColor(240, 226, 192, 120), 1.5))
            painter.drawRoundedRect(cfg_drawer, 10, 10)

            painter.setFont(QFont("SansSerif", 10, QFont.Bold))
            painter.setPen(QColor(240, 226, 192))
            painter.drawText(int(cfg_drawer.x() + 15), int(cfg_drawer.y() + 30), "ขนาด Minimap:")

            btn_w = 80
            for i, mode in enumerate(["Small", "Medium", "Large"]):
                b_rect = QRectF(cfg_drawer.x() + 20 + i * (btn_w + 10), cfg_drawer.y() + 45, btn_w, 30)
                is_active = (self.minimap_size_mode == mode)
                painter.setBrush(QBrush(QColor(0, 200, 120, 220) if is_active else QColor(40, 44, 52, 200)))
                painter.drawRoundedRect(b_rect, 6, 6)
                painter.setFont(QFont("SansSerif", 8, QFont.Bold))
                painter.setPen(QColor(255, 255, 255))
                painter.drawText(b_rect, Qt.AlignCenter, mode)

            painter.setFont(QFont("SansSerif", 10, QFont.Bold))
            painter.setPen(QColor(240, 226, 192))
            painter.drawText(int(cfg_drawer.x() + 15), int(cfg_drawer.y() + 115), "ความโปร่งแสง (Opacity):")

            for i, op in enumerate([0.3, 0.6, 0.85, 1.0]):
                b_rect = QRectF(cfg_drawer.x() + 20 + i * 65, cfg_drawer.y() + 130, 55, 30)
                is_active = (abs(self.minimap_opacity - op) < 0.05)
                painter.setBrush(QBrush(QColor(0, 200, 120, 220) if is_active else QColor(40, 44, 52, 200)))
                painter.drawRoundedRect(b_rect, 6, 6)
                painter.setFont(QFont("SansSerif", 8, QFont.Bold))
                painter.setPen(QColor(255, 255, 255))
                painter.drawText(b_rect, Qt.AlignCenter, f"{int(op*100)}%")

            # สลับเปิด/ปิดเลเยอร์ (Layer Toggles)
            painter.setFont(QFont("SansSerif", 10, QFont.Bold))
            painter.setPen(QColor(240, 226, 192))
            painter.drawText(int(cfg_drawer.x() + 15), int(cfg_drawer.y() + 190), "แผนที่เลเยอร์ (Map Layers):")

            layer_configs = [
                ("Zones", self.layers_mgr.show_zones, 15),
                ("Salt/Item", self.layers_mgr.show_items, 90),
                ("Labels", self.layers_mgr.show_labels_region, 165),
                ("Food", self.layers_mgr.show_food, 240)
            ]
            for title, active, offset_x in layer_configs:
                l_rect = QRectF(cfg_drawer.x() + offset_x, cfg_drawer.y() + 205, 68, 28)
                painter.setBrush(QBrush(QColor(0, 200, 120, 220) if active else QColor(40, 44, 52, 200)))
                painter.drawRoundedRect(l_rect, 5, 5)
                painter.setFont(QFont("SansSerif", 7, QFont.Bold))
                painter.setPen(QColor(255, 255, 255))
                painter.drawText(l_rect, Qt.AlignCenter, title)

            curr_y = cfg_drawer.y() + 248

            # หมวดหมู่ Zone Categories (แสดงเมื่อเปิดเลเยอร์ Zones)
            if self.layers_mgr.show_zones:
                painter.setFont(QFont("SansSerif", 9, QFont.Bold))
                painter.setPen(QColor(240, 226, 192))
                painter.drawText(int(cfg_drawer.x() + 15), int(curr_y), "หมวดโซน (Zone Categories):")
                curr_y += 13

                zone_cats = [
                    ("sanctuary", "🛡️ Sanctuary", 15, 95, QColor(0, 230, 118, 220)),
                    ("migration", "🧭 Migration", 115, 95, QColor(255, 145, 0, 220)),
                    ("patrol", "⚔️ Patrol", 215, 95, QColor(170, 0, 255, 220))
                ]
                for cat_id, cat_title, offset_x, bw, active_c in zone_cats:
                    zc_rect = QRectF(cfg_drawer.x() + offset_x, curr_y, bw, 26)
                    is_on = cat_id in self.layers_mgr.active_zone_categories
                    painter.setBrush(QBrush(active_c if is_on else QColor(40, 44, 52, 200)))
                    painter.drawRoundedRect(zc_rect, 4, 4)
                    painter.setFont(QFont("SansSerif", 7, QFont.Bold))
                    painter.setPen(QColor(255, 255, 255))
                    painter.drawText(zc_rect, Qt.AlignCenter, cat_title)

                curr_y += 38

            # หมวดหมู่ Food Categories (จะแสดงเฉพาะเมื่อเปิดเลเยอร์ Food เท่านั้น)
            if self.layers_mgr.show_food:
                painter.setFont(QFont("SansSerif", 9, QFont.Bold))
                painter.setPen(QColor(240, 226, 192))
                painter.drawText(int(cfg_drawer.x() + 15), int(curr_y), "หมวดอาหาร (Food Categories):")
                curr_y += 13

                food_cats = [
                    ("animal", "🥩 Meat", 15),
                    ("fruit", "🍎 Fruit", 75),
                    ("vegetable", "🥕 Veg", 135),
                    ("plant", "🌿 Plant", 195),
                    ("mushroom", "🍄 Mush", 255)
                ]
                for cat_id, cat_title, offset_x in food_cats:
                    fc_rect = QRectF(cfg_drawer.x() + offset_x, curr_y, 55, 26)
                    is_on = cat_id in self.layers_mgr.active_food_categories
                    painter.setBrush(QBrush(QColor(255, 152, 0, 220) if is_on else QColor(40, 44, 52, 200)))
                    painter.drawRoundedRect(fc_rect, 4, 4)
                    painter.setFont(QFont("SansSerif", 6, QFont.Bold))
                    painter.setPen(QColor(255, 255, 255))
                    painter.drawText(fc_rect, Qt.AlignCenter, cat_title)

                curr_y += 38

            # Slide Bar: Minimap Brightness (0.2 - 1.0)
            painter.setFont(QFont("SansSerif", 9, QFont.Bold))
            painter.setPen(QColor(240, 226, 192))
            painter.drawText(int(cfg_drawer.x() + 15), int(curr_y + 12), "ความสว่าง Minimap:")
            painter.drawText(int(cfg_drawer.x() + 265), int(curr_y + 12), f"{int(self.minimap_brightness * 100)}%")
            curr_y += 20

            # ราง Slider Track
            slider_w = 280
            track_rect = QRectF(cfg_drawer.x() + 20, curr_y + 4, slider_w, 8)
            painter.setBrush(QBrush(QColor(35, 38, 46, 230)))
            painter.setPen(Qt.NoPen)
            painter.drawRoundedRect(track_rect, 4, 4)

            # เส้นแถบสี Active Fill
            mini_ratio = max(0.0, min(1.0, (self.minimap_brightness - 0.2) / 0.8))
            fill_rect = QRectF(track_rect.x(), track_rect.y(), slider_w * mini_ratio, 8)
            painter.setBrush(QBrush(QColor(255, 180, 0, 230)))
            painter.drawRoundedRect(fill_rect, 4, 4)

            # หัวจับ Slider Handle (Thumb)
            handle_cx = track_rect.x() + slider_w * mini_ratio
            handle_cy = track_rect.center().y()
            painter.setBrush(QBrush(QColor(255, 255, 255)))
            painter.setPen(QPen(QColor(255, 180, 0), 2))
            painter.drawEllipse(QPointF(handle_cx, handle_cy), 8, 8)

            curr_y += 28

            # Slide Bar: Full Map Brightness (0.2 - 1.0)
            painter.setFont(QFont("SansSerif", 9, QFont.Bold))
            painter.setPen(QColor(240, 226, 192))
            painter.drawText(int(cfg_drawer.x() + 15), int(curr_y + 12), "ความสว่าง Full Map:")
            painter.drawText(int(cfg_drawer.x() + 265), int(curr_y + 12), f"{int(self.fullmap_brightness * 100)}%")
            curr_y += 20

            track_rect_full = QRectF(cfg_drawer.x() + 20, curr_y + 4, slider_w, 8)
            painter.setBrush(QBrush(QColor(35, 38, 46, 230)))
            painter.setPen(Qt.NoPen)
            painter.drawRoundedRect(track_rect_full, 4, 4)

            full_ratio = max(0.0, min(1.0, (self.fullmap_brightness - 0.2) / 0.8))
            fill_rect_full = QRectF(track_rect_full.x(), track_rect_full.y(), slider_w * full_ratio, 8)
            painter.setBrush(QBrush(QColor(0, 200, 255, 230)))
            painter.drawRoundedRect(fill_rect_full, 4, 4)

            handle_cx_full = track_rect_full.x() + slider_w * full_ratio
            handle_cy_full = track_rect_full.center().y()
            painter.setBrush(QBrush(QColor(255, 255, 255)))
            painter.setPen(QPen(QColor(0, 200, 255), 2))
            painter.drawEllipse(QPointF(handle_cx_full, handle_cy_full), 8, 8)

            curr_y += 28

            # Slide Bar: Entity / Icon Scale (50% - 200%)
            painter.setFont(QFont("SansSerif", 9, QFont.Bold))
            painter.setPen(QColor(240, 226, 192))
            painter.drawText(int(cfg_drawer.x() + 15), int(curr_y + 12), "ขนาดไอคอน (Icon Scale):")
            painter.drawText(int(cfg_drawer.x() + 265), int(curr_y + 12), f"{int(self.entity_icon_scale * 100)}%")
            curr_y += 20

            track_rect_scale = QRectF(cfg_drawer.x() + 20, curr_y + 4, slider_w, 8)
            painter.setBrush(QBrush(QColor(35, 38, 46, 230)))
            painter.setPen(Qt.NoPen)
            painter.drawRoundedRect(track_rect_scale, 4, 4)

            scale_ratio = max(0.0, min(1.0, (self.entity_icon_scale - 0.5) / 1.5))
            fill_rect_scale = QRectF(track_rect_scale.x(), track_rect_scale.y(), slider_w * scale_ratio, 8)
            painter.setBrush(QBrush(QColor(0, 230, 118, 230)))
            painter.drawRoundedRect(fill_rect_scale, 4, 4)

            handle_cx_scale = track_rect_scale.x() + slider_w * scale_ratio
            handle_cy_scale = track_rect_scale.center().y()
            painter.setBrush(QBrush(QColor(255, 255, 255)))
            painter.setPen(QPen(QColor(0, 230, 118), 2))
            painter.drawEllipse(QPointF(handle_cx_scale, handle_cy_scale), 8, 8)

            curr_y += 32

            # ปุ่มเปิดหน้าต่างตั้งค่า Session Auth & Sync
            auth_rect = QRectF(cfg_drawer.x() + 15, curr_y + 4, cfg_drawer.width() - 30, 30)
            painter.setBrush(QBrush(QColor(0, 140, 220, 220)))
            painter.setPen(Qt.NoPen)
            painter.drawRoundedRect(auth_rect, 6, 6)
            painter.setFont(QFont("SansSerif", 8, QFont.Bold))
            painter.setPen(QColor(255, 255, 255))
            painter.drawText(auth_rect, Qt.AlignCenter, "👥 ปาร์ตี้ส่วนตัว (Party PIN) / Live Map")

            curr_y += 38

            # ปุ่ม Reset All to Off (ปิดทุก Layer, Zone และ Food ทั้งหมด)
            reset_rect = QRectF(cfg_drawer.x() + 15, curr_y + 4, cfg_drawer.width() - 30, 30)
            painter.setBrush(QBrush(QColor(210, 45, 45, 220)))
            painter.setPen(Qt.NoPen)
            painter.drawRoundedRect(reset_rect, 6, 6)
            painter.setFont(QFont("SansSerif", 8, QFont.Bold))
            painter.setPen(QColor(255, 255, 255))
            painter.drawText(reset_rect, Qt.AlignCenter, "🔄 Reset All to Off (ปิดทั้งหมด)")

        # Drawer: Track Menu
        if self.show_track_menu:
            track_drawer = QRectF(self.width() - 280, 70, 260, 400)
            painter.setBrush(QBrush(QColor(18, 16, 14, 245)))
            painter.setPen(QPen(QColor(0, 229, 255, 120), 1.5))
            painter.drawRoundedRect(track_drawer, 10, 10)

            painter.setFont(QFont("SansSerif", 10, QFont.Bold))
            painter.setPen(QColor(0, 229, 255))
            painter.drawText(int(track_drawer.x() + 15), int(track_drawer.y() + 30), "เลือกเป้าหมายที่ต้องการนำทาง:")

            stop_btn = QRectF(track_drawer.x() + 10, track_drawer.y() + 45, track_drawer.width() - 20, 26)
            painter.setBrush(QBrush(QColor(180, 40, 40, 200)))
            painter.drawRoundedRect(stop_btn, 6, 6)
            painter.setFont(QFont("SansSerif", 8, QFont.Bold))
            painter.setPen(QColor(255, 255, 255))
            painter.drawText(stop_btn, Qt.AlignCenter, "❌ ยกเลิกการนำทาง (Untrack)")

            y_offset = track_drawer.y() + 85
            painter.setFont(QFont("SansSerif", 8, QFont.Bold))
            painter.setPen(QColor(200, 200, 200))
            painter.drawText(int(track_drawer.x() + 15), int(y_offset), "สมาชิกในปาร์ตี้:")
            y_offset += 15

            for pid, p in self.players.items():
                p_btn = QRectF(track_drawer.x() + 10, y_offset, track_drawer.width() - 20, 26)
                is_tracking = self.tracked_target and self.tracked_target.get("id") == pid
                painter.setBrush(QBrush(QColor(0, 180, 120, 220) if is_tracking else QColor(35, 38, 45, 220)))
                painter.drawRoundedRect(p_btn, 6, 6)
                painter.setFont(QFont("SansSerif", 8))
                painter.setPen(QColor(255, 255, 255))
                painter.drawText(p_btn, Qt.AlignCenter, f"👤 {p.name}")
                y_offset += 30

            y_offset += 10
            painter.setFont(QFont("SansSerif", 8, QFont.Bold))
            painter.setPen(QColor(200, 200, 200))
            painter.drawText(int(track_drawer.x() + 15), int(y_offset), "จุด Waypoint:")
            y_offset += 15

            for wp in self.waypoints:
                w_btn = QRectF(track_drawer.x() + 10, y_offset, track_drawer.width() - 20, 26)
                is_tracking = self.tracked_target and self.tracked_target.get("id") == wp.id
                painter.setBrush(QBrush(QColor(0, 180, 120, 220) if is_tracking else QColor(35, 38, 45, 220)))
                painter.drawRoundedRect(w_btn, 6, 6)
                painter.setFont(QFont("SansSerif", 8))
                painter.setPen(QColor(255, 255, 255))
                icon_sym = {"camp": "⛺", "danger": "⚠️", "water": "💧", "food": "🍖"}.get(wp.icon, "📍")
                painter.drawText(w_btn, Qt.AlignCenter, f"{icon_sym} {wp.name}")
                y_offset += 30

        # Hover Popover Menu เหนือหมุด Waypoint (Feature: Waypoint Hover Menu)
        if self.hovered_waypoint and self.hovered_wp_screen_pos:
            self.draw_waypoint_hover_menu(painter)

        # Hover Popover Menu เหนือสมาชิก Player (Feature: Player Hover/Selection Menu)
        elif self.hovered_player and self.hovered_player_screen_pos:
            self.draw_player_hover_menu(painter)

        # Hover Tooltip สำหรับ Items / Salt / Food (Feature: item salt food hover label)
        elif self.layers_mgr.hovered_entity_info:
            self.draw_entity_hover_tooltip(painter)

        # Hover Tooltip สำหรับ Zone (Feature: Zone label appears when hover zone)
        elif self.layers_mgr.show_zones and self.layers_mgr.hovered_zone_info:
            self.draw_zone_hover_tooltip(painter)

    def draw_player_hover_menu(self, painter: QPainter):
        p = self.hovered_player
        pos = self.hovered_player_screen_pos
        if not p or not pos or p.id == self.local_player_id:
            self.hovered_player_action_rects = {}
            return

        is_selected = (self.selected_player and self.selected_player.id == p.id)
        menu_w = 190
        menu_h = 74
        menu_x = min(max(pos.x() - menu_w / 2, 20), self.width() - menu_w - 20)
        menu_y = pos.y() - menu_h - 14
        if menu_y < 50:
            menu_y = pos.y() + 18

        box_rect = QRectF(menu_x, menu_y, menu_w, menu_h)

        # Card Background Glassmorphism
        painter.setBrush(QBrush(QColor(18, 16, 14, 245)))
        painter.setPen(QPen(QColor(p.color), 2.0 if is_selected else 1.5))
        painter.drawRoundedRect(box_rect, 8, 8)

        # Title / Player Name
        tag_icon = "👑 " if p.id == self.local_player_id else "👤 "
        painter.setFont(QFont("SansSerif", 8, QFont.Bold))
        painter.setPen(QColor(255, 255, 255))
        painter.drawText(int(box_rect.x() + 10), int(box_rect.y() + 22), f"{tag_icon}{p.name}")

        # Action Button: Track / Untrack Party Member
        track_rect = QRectF(box_rect.x() + 10, box_rect.y() + 34, menu_w - 20, 28)
        is_tracking = self.tracked_target and self.tracked_target.get("id") == p.id
        painter.setBrush(QBrush(QColor(220, 50, 50, 230) if is_tracking else QColor(0, 140, 220, 220)))
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(track_rect, 5, 5)
        painter.setFont(QFont("SansSerif", 7, QFont.Bold))
        painter.setPen(QColor(255, 255, 255))
        painter.drawText(track_rect, Qt.AlignCenter, "❌ ยกเลิกนำทาง (Untrack)" if is_tracking else "🧭 นำทางหาเพื่อน (Track Player)")

        self.hovered_player_action_rects = {
            "track": track_rect,
            "box": box_rect,
            "player": p
        }

    def draw_entity_hover_tooltip(self, painter: QPainter):
        e_info = self.layers_mgr.hovered_entity_info
        if not e_info:
            self.hovered_entity_action_rects = {}
            return

        ent = e_info.get("entity", {})
        title = ent.get("title", "")
        color_str = ent.get("color", "#FFFFFF")
        sx = e_info.get("screen_x", 100)
        sy = e_info.get("screen_y", 100)

        font = QFont("SansSerif", 8, QFont.Bold)
        painter.setFont(font)
        fm = QtGui.QFontMetrics(font)

        text_w = fm.horizontalAdvance(title)
        box_w = max(text_w + 30, 180)
        box_h = 68
        box_x = min(max(sx - box_w / 2, 20), self.width() - box_w - 20)
        box_y = sy - box_h - 14
        if box_y < 60:
            box_y = sy + 18
        box_rect = QRectF(box_x, box_y, box_w, box_h)

        # Card Background Glassmorphism
        painter.setBrush(QBrush(QColor(18, 16, 14, 245)))
        painter.setPen(QPen(QColor(color_str), 1.5))
        painter.drawRoundedRect(box_rect, 8, 8)

        # Title Text
        painter.setPen(QColor(255, 255, 255))
        painter.drawText(int(box_rect.x() + 10), int(box_rect.y() + 22), title)

        # Action Button: Track / Untrack This Item/Food
        track_rect = QRectF(box_rect.x() + 10, box_rect.y() + 32, box_w - 20, 26)
        is_tracking = (self.tracked_target and
                       self.tracked_target.get("type") == "entity" and
                       math.hypot(self.tracked_target.get("x", 0) - ent.get("x", 0),
                                  self.tracked_target.get("y", 0) - ent.get("y", 0)) < 0.1)

        painter.setBrush(QBrush(QColor(220, 50, 50, 230) if is_tracking else QColor(0, 140, 220, 220)))
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(track_rect, 5, 5)
        painter.setFont(QFont("SansSerif", 7, QFont.Bold))
        painter.setPen(QColor(255, 255, 255))
        painter.drawText(track_rect, Qt.AlignCenter, "❌ ยกเลิกนำทาง (Untrack)" if is_tracking else "🧭 นำทางมาที่นี่ (Track This)")

        self.hovered_entity_action_rects = {
            "track": track_rect,
            "box": box_rect,
            "entity": ent,
            "is_tracking": is_tracking
        }

    def draw_zone_hover_tooltip(self, painter: QPainter):
        z_data = self.layers_mgr.hovered_zone_info
        if not z_data:
            return

        zones = z_data.get("zones", [])
        if not zones:
            return

        sx = z_data.get("screen_x", 100)
        sy = z_data.get("screen_y", 100)

        # คำนวณความกว้างและความสูงตามจำนวนโซนที่ซ้อนทับกัน
        font = QFont("SansSerif", 8, QFont.Bold)
        painter.setFont(font)
        fm = QtGui.QFontMetrics(font)

        line_height = 24
        header_height = 20
        total_h = header_height + (len(zones) * line_height) + 10

        max_text_w = fm.horizontalAdvance("📍 Overlapping Zones:")
        for z in zones:
            tw = fm.horizontalAdvance(f"• {z['name']} ({z['type']})")
            if tw > max_text_w:
                max_text_w = tw

        box_w = max_text_w + 30
        box_x = min(sx + 15, self.width() - box_w - 20)
        box_y = max(sy - total_h - 10, 60)
        box_rect = QRectF(box_x, box_y, box_w, total_h)

        # พื้นหลัง Glassmorphism พร้อมขอบทองอ่อน
        painter.setBrush(QBrush(QColor(15, 12, 10, 245)))
        painter.setPen(QPen(QColor(240, 226, 192, 150), 1.5))
        painter.drawRoundedRect(box_rect, 8, 8)

        # หัวข้อ
        painter.setFont(QFont("SansSerif", 7, QFont.Bold))
        painter.setPen(QColor(240, 226, 192, 200))
        painter.drawText(int(box_rect.x() + 10), int(box_rect.y() + 16), f"📍 พื้นที่ซ้อนทับ ({len(zones)} โซน):" if len(zones) > 1 else "📍 ข้อมูลพื้นที่:")

        # รายชื่อโซนแต่ละชั้น
        curr_y = box_rect.y() + header_height + 14
        painter.setFont(QFont("SansSerif", 8, QFont.Bold))

        for z in zones:
            # วาดจุดสีประจำโซน
            c = QColor(z["color"])
            painter.setBrush(QBrush(c))
            painter.setPen(Qt.NoPen)
            painter.drawEllipse(QPointF(box_rect.x() + 16, curr_y - 4), 4, 4)

            # ชื่อโซนและประเภท
            painter.setPen(QColor(255, 255, 255))
            painter.drawText(int(box_rect.x() + 26), int(curr_y), f"{z['name']} [{z['type']}]")
            curr_y += line_height

    def draw_waypoint_hover_menu(self, painter: QPainter):
        wp = self.hovered_waypoint
        pos = self.hovered_wp_screen_pos
        if not wp or not pos:
            return

        menu_w = 180
        menu_h = 75
        menu_x = min(max(pos.x() - menu_w / 2, 20), self.width() - menu_w - 20)
        menu_y = pos.y() - menu_h - 14
        if menu_y < 50:
            menu_y = pos.y() + 18

        box_rect = QRectF(menu_x, menu_y, menu_w, menu_h)

        # พื้นหลัง Card โปร่งแสง
        painter.setBrush(QBrush(QColor(18, 16, 14, 245)))
        painter.setPen(QPen(QColor(wp.color), 1.5))
        painter.drawRoundedRect(box_rect, 8, 8)

        # ชื่อและไอคอน Waypoint
        icon_sym = {"camp": "⛺", "danger": "⚠️", "water": "💧", "food": "🍖"}.get(wp.icon, "📍")
        painter.setFont(QFont("SansSerif", 8, QFont.Bold))
        painter.setPen(QColor(255, 255, 255))
        painter.drawText(int(box_rect.x() + 10), int(box_rect.y() + 22), f"{icon_sym} {wp.name}")

        # ปุ่ม Action: Track / Untrack (นำทาง / ยกเลิกนำทาง)
        track_rect = QRectF(box_rect.x() + 10, box_rect.y() + 34, 75, 28)
        is_tracking = self.tracked_target and self.tracked_target.get("id") == wp.id
        painter.setBrush(QBrush(QColor(220, 50, 50, 230) if is_tracking else QColor(0, 140, 220, 220)))
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(track_rect, 5, 5)
        painter.setFont(QFont("SansSerif", 7, QFont.Bold))
        painter.setPen(QColor(255, 255, 255))
        painter.drawText(track_rect, Qt.AlignCenter, "❌ Untrack" if is_tracking else "🧭 Track")

        # ปุ่ม Action: Delete (ลบหมุด)
        del_rect = QRectF(box_rect.x() + 95, box_rect.y() + 34, 75, 28)
        painter.setBrush(QBrush(QColor(200, 45, 45, 220)))
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(del_rect, 5, 5)
        painter.setFont(QFont("SansSerif", 7, QFont.Bold))
        painter.setPen(QColor(255, 255, 255))
        painter.drawText(del_rect, Qt.AlignCenter, "🗑️ ลบหมุด")

        # บันทึก Rect สำหรับดักจับการคลิก
        self.hovered_wp_action_rects = {
            "track": track_rect,
            "delete": del_rect,
            "box": box_rect
        }

    def draw_waypoints(self, painter: QPainter, is_minimap: bool = False):
        zoom = self.minimap_scale if is_minimap else self.interactive_map_scale
        scale_factor = self.entity_icon_scale / max(zoom, 0.05)
        r = 5.0 * scale_factor
        r_glow = 9.0 * scale_factor
        font_size = max(5, int(round(7 * scale_factor)))

        for wp in self.waypoints:
            is_hovered_or_selected = (
                (self.hovered_waypoint and self.hovered_waypoint.id == wp.id) or
                (self.selected_waypoint and self.selected_waypoint.id == wp.id)
            )

            if is_hovered_or_selected:
                painter.setBrush(QtCore.Qt.NoBrush)
                glow_c = QColor(wp.color)
                glow_c.setAlpha(220)
                painter.setPen(QPen(glow_c, 2.2 * scale_factor))
                painter.drawEllipse(QPointF(wp.x, wp.y), r_glow, r_glow)

            painter.setBrush(QBrush(QColor(wp.color)))
            painter.setPen(QPen(QColor(255, 255, 255), 1.5 * scale_factor))
            painter.drawEllipse(QPointF(wp.x, wp.y), r, r)

            icon_sym = {"camp": "⛺", "danger": "⚠️", "water": "💧", "food": "🍖"}.get(wp.icon, "📍")
            painter.setFont(QFont("SansSerif", font_size, QFont.Bold))
            painter.setPen(QColor(255, 255, 255, 220))
            painter.drawText(int(wp.x + 7 * scale_factor), int(wp.y + 4 * scale_factor), f"{icon_sym} {wp.name}")

    def draw_game_elements(self, painter: QPainter, is_minimap: bool = False):
        zoom = self.minimap_scale if is_minimap else self.interactive_map_scale
        scale_factor = self.entity_icon_scale / max(zoom, 0.05)

        local_p = self.players.get(self.local_player_id)
        local_pos = local_p.current_pos if local_p else {"x": 0.0, "y": 0.0}

        # 1. Footprints (รอยเท้า) พร้อม Stacking & Culling
        fp_size = 2.2 * scale_factor
        for pid, p in self.players.items():
            is_local = (pid == self.local_player_id)
            if self.movement_tracker.should_draw_footprints(is_local, p.current_pos, local_pos):
                for i, fp in enumerate(p.footprints):
                    alpha = int(40 + (i / max(len(p.footprints), 1)) * 160)
                    painter.setPen(Qt.NoPen)
                    c = QColor(p.color)
                    c.setAlpha(alpha)
                    painter.setBrush(QBrush(c))
                    painter.drawEllipse(QPointF(fp.x, fp.y), fp_size, fp_size)

        # 2. Pack Points Clustering (พร้อม Spiderfy)
        clusters = self.cluster_manager.cluster_players(list(self.players.values()))
        for c in clusters:
            if c["is_cluster"]:
                cx, cy = c["x"], c["y"]

                if (is_minimap and self.minimap_scale >= 2.0) or not is_minimap:
                    members = c["members"]
                    radius = 20.0 * scale_factor
                    for idx, m in enumerate(members):
                        angle = (2 * math.pi / len(members)) * idx
                        mx = cx + radius * math.cos(angle)
                        my = cy + radius * math.sin(angle)

                        painter.setPen(QPen(QColor(255, 255, 255, 100), 1.0 * scale_factor, Qt.DotLine))
                        painter.drawLine(QPointF(cx, cy), QPointF(mx, my))

                        painter.setBrush(QBrush(QColor(m.color)))
                        painter.setPen(QPen(QColor(255, 255, 255), 1.0 * scale_factor))
                        painter.drawEllipse(QPointF(mx, my), 5.0 * scale_factor, 5.0 * scale_factor)
                        self.draw_forward_arrow(painter, mx, my, m.heading_angle, m.color, scale_factor)
                        painter.setFont(QFont("SansSerif", max(5, int(round(6 * scale_factor))), QFont.Bold))
                        painter.setPen(QColor(255, 255, 255, 220))
                        painter.drawText(int(mx + 7 * scale_factor), int(my + 3 * scale_factor), m.name)

                    painter.setBrush(QBrush(QColor(255, 120, 0, 180)))
                    painter.setPen(Qt.NoPen)
                    painter.drawEllipse(QPointF(cx, cy), 6.0 * scale_factor, 6.0 * scale_factor)
                else:
                    painter.setBrush(QBrush(QColor(255, 120, 0, 230)))
                    painter.setPen(QPen(QColor(255, 255, 255), 1.5 * scale_factor))
                    r_cl = 12.0 * scale_factor
                    painter.drawEllipse(QPointF(cx, cy), r_cl, r_cl)

                    painter.setFont(QFont("SansSerif", max(5, int(round(7 * scale_factor))), QFont.Bold))
                    painter.setPen(QColor(255, 255, 255))
                    painter.drawText(QRectF(cx - r_cl, cy - r_cl, r_cl * 2, r_cl * 2), Qt.AlignCenter, f"[{c['count']}]")
            else:
                p = c["player"]
                px, py = p.current_pos["x"], p.current_pos["y"]

                is_player_active = (
                    (self.hovered_player and self.hovered_player.id == p.id) or
                    (self.selected_player and self.selected_player.id == p.id)
                )

                if is_player_active and not is_minimap:
                    painter.setBrush(QtCore.Qt.NoBrush)
                    p_glow = QColor(p.color)
                    p_glow.setAlpha(220)
                    painter.setPen(QPen(p_glow, 2.2 * scale_factor))
                    painter.drawEllipse(QPointF(px, py), 10.0 * scale_factor, 10.0 * scale_factor)

                painter.setBrush(QBrush(QColor(p.color)))
                painter.setPen(QPen(QColor(255, 255, 255), 1.0 * scale_factor))
                painter.drawEllipse(QPointF(px, py), 5.5 * scale_factor, 5.5 * scale_factor)

                self.draw_forward_arrow(painter, px, py, p.heading_angle, p.color, scale_factor)

                painter.setFont(QFont("SansSerif", max(5, int(round(7 * scale_factor))), QFont.Bold))
                painter.setPen(QColor(255, 255, 255, 220))
                painter.drawText(int(px + 8 * scale_factor), int(py + 4 * scale_factor), p.name)

        # 3. Tracking Vector Line
        if self.tracked_target and local_p:
            tx, ty = self._get_tracked_position()
            if tx is not None and ty is not None:
                vector = NavigationEngine.calculate_tracking_vector(
                    local_p.current_pos["x"], local_p.current_pos["y"], tx, ty
                )
                pen = QPen(QColor(0, 229, 255, 220), max(1.5, 2.0 * scale_factor), Qt.DashLine)
                painter.setPen(pen)
                painter.drawLine(
                    QPointF(vector["origin"]["x"], vector["origin"]["y"]),
                    QPointF(vector["target"]["x"], vector["target"]["y"])
                )

                mid = vector["midpoint"]
                dist_m = int(vector["distance"] * 1.37)
                painter.setFont(QFont("Monospace", max(6, int(round(8 * scale_factor))), QFont.Bold))
                painter.setPen(QColor(0, 229, 255))
                painter.drawText(int(mid["x"]), int(mid["y"] - 6 * scale_factor), f"{dist_m}m")

    def draw_forward_arrow(self, painter: QPainter, x: float, y: float, angle: float, color_str: str, scale_factor: float = 1.0):
        length = 10.0 * scale_factor
        p1 = QPointF(x + length * math.cos(angle), y + length * math.sin(angle))
        p2 = QPointF(
            x + (length * 0.4) * math.cos(angle + 2.4),
            y + (length * 0.4) * math.sin(angle + 2.4)
        )
        p3 = QPointF(
            x + (length * 0.4) * math.cos(angle - 2.4),
            y + (length * 0.4) * math.sin(angle - 2.4)
        )
        painter.setBrush(QBrush(QColor(color_str)))
        painter.setPen(QPen(QColor(255, 255, 255), 1.0 * scale_factor))
        painter.drawPolygon(QPolygonF([p1, p2, p3]))


def main():
    import signal
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    app = QtWidgets.QApplication(sys.argv)
    overlay = VoiceHUDOverlay()
    overlay.show()

    sig_timer = QTimer()
    sig_timer.start(200)
    sig_timer.timeout.connect(lambda: None)

    print("[VoiceHUD - PackPoint] Application started.")
    print("  * M (กดค้าง 1 วินาที): เปิด Fullscreen Interactive Map")
    print("  * M หรือ Esc (กดครั้งเดียว): ปิด Fullscreen Interactive Map")
    print("  * F7: ซ่อน / แสดง Minimap HUD")
    print("  * Scroll Mouse: ซูมเข้า/ออก แผนที่")
    print("  * +/-: ซูมเข้า / ซูมออก แผนที่")
    print("  * 8: ปิดและออกจากโปรแกรมทันที (Exit Hotkey)")
    print("  * Ctrl+C: ปิดโปรแกรม")
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
