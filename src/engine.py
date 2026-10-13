"""
VoiceHUD - PackPoint: Core Math & Movement Tracking Engine
รองรับทั้ง Windows 11 และ Linux (X11 / Cinnamon)
"""

import math
import time
from typing import List, Dict, Tuple, Optional


class GatewayProjection:
    """
    ตัวแปลงพิกัดเกม The Isle Evrima (Gateway Map) เป็นพิกัด SVG/Canvas 2D
    อ้างอิงจากการ Reverse-engineering voice-island.com
    """
    MIN_X = -607.0
    MAX_X = 509.0
    MIN_Y = -505.0
    MAX_Y = 607.0
    SVG_WIDTH = 731.25
    SVG_HEIGHT = 732.75

    @classmethod
    def project_coordinates(cls, game_x: float, game_y: float) -> Tuple[float, float]:
        """
        แปลงพิกัดจาก In-game (หาร 1000 แล้วสลับแกนตาม Cartographic space)
        dataX = game_y / 1000.0
        dataY = game_x / 1000.0
        """
        data_x = game_y / 1000.0
        data_y = game_x / 1000.0

        svg_x = ((data_y - cls.MIN_Y) / (cls.MAX_Y - cls.MIN_Y)) * cls.SVG_WIDTH
        svg_y = ((data_x - cls.MIN_X) / (cls.MAX_X - cls.MIN_X)) * cls.SVG_HEIGHT
        return svg_x, svg_y

    @classmethod
    def unproject_coordinates(cls, svg_x: float, svg_y: float) -> Tuple[float, float]:
        """แปลงพิกัดกลับจาก SVG เป็นพิกัด in-game"""
        data_y = (svg_x / cls.SVG_WIDTH) * (cls.MAX_Y - cls.MIN_Y) + cls.MIN_Y
        data_x = (svg_y / cls.SVG_HEIGHT) * (cls.MAX_X - cls.MIN_X) + cls.MIN_X

        game_x = data_y * 1000.0
        game_y = data_x * 1000.0
        return game_x, game_y


class TrailPoint:
    def __init__(self, x: float, y: float, timestamp: Optional[float] = None):
        self.x = x
        self.y = y
        self.timestamp = timestamp or time.time()

    def to_dict(self) -> Dict:
        return {"x": self.x, "y": self.y, "timestamp": self.timestamp}


class PlayerMovementState:
    def __init__(self, player_id: str, name: str, initial_x: float, initial_y: float, color: str = "#00FF66"):
        self.player_id = player_id
        self.name = name
        self.current_pos = {"x": initial_x, "y": initial_y}
        self.prev_pos = {"x": initial_x, "y": initial_y}
        self.heading_angle = 0.0  # เรเดียน
        self.footprints: List[TrailPoint] = []
        self.color = color

    @property
    def id(self) -> str:
        return self.player_id


class MovementTracker:
    """
    ระบบคำนวณ ForwardArrow และประวัติรอยเท้า (Footprints) พร้อมระบบ Optimization:
    - Deadzone Filter: ป้องกันลูกศรหมุนส่ายไปมาขณะยืนนิ่ง
    - Stacking Filter: ไม่ปั๊มรอยเท้าซ้ำถ้ายังขยับไม่ถึงระยะขั้นต่ำ
    - Ring Buffer: จำกัดจำนวนรอยเท้าต่อคน
    - Proximity Culling: ซ่อนรอยเท้าของเพื่อนเมื่ออยู่ใกล้ตัวเรา
    """
    def __init__(self, max_footprints: int = 12, min_step_dist: float = 12.0, cull_dist: float = 60.0):
        self.max_footprints = max_footprints
        self.min_step_dist = min_step_dist
        self.cull_dist = cull_dist

    def update_player(self, state: PlayerMovementState, new_x: float, new_y: float) -> None:
        dx = new_x - state.current_pos["x"]
        dy = new_y - state.current_pos["y"]
        dist_moved = math.hypot(dx, dy)

        # 1. ForwardArrow: คำนวณมุมเมื่อเคลื่อนที่จริงเกิน Deadzone (2 px)
        if dist_moved >= 2.0:
            state.heading_angle = math.atan2(dy, dx)
            state.prev_pos = dict(state.current_pos)
            state.current_pos = {"x": new_x, "y": new_y}

        # 2. Footprint History & Stacking Filter
        if not state.footprints:
            dist_from_last = float("inf")
        else:
            last_fp = state.footprints[-1]
            dist_from_last = math.hypot(new_x - last_fp.x, new_y - last_fp.y)

        if dist_from_last >= self.min_step_dist:
            state.footprints.append(TrailPoint(new_x, new_y))
            if len(state.footprints) > self.max_footprints:
                state.footprints.pop(0)

    def should_draw_footprints(
        self,
        is_local: bool,
        party_pos: Dict[str, float],
        local_pos: Dict[str, float]
    ) -> bool:
        """ตรวจสอบ Proximity Culling (ซ่อนรอยเท้าเพื่อนเมื่ออยู่ใกล้ตัวเรา)"""
        if is_local:
            return True
        dist_to_local = math.hypot(party_pos["x"] - local_pos["x"], party_pos["y"] - local_pos["y"])
        return dist_to_local >= self.cull_dist


class PackClusterManager:
    """
    ระบบรวมกลุ่มหมุด (Pack Points Clustering)
    ยุบหมุดเพื่อนที่ยืนชิดกันให้เป็นหมุดเดียวเพื่อไม่ให้ชื่อและไอคอนซ้อนทับกัน
    """
    def __init__(self, cluster_radius: float = 35.0):
        self.cluster_radius = cluster_radius

    def cluster_players(self, players: List[PlayerMovementState]) -> List[Dict]:
        """
        จัดกลุ่มผู้เล่นที่ระยะห่าง < cluster_radius
        คืนค่ารายการหมุดทั้งที่เป็น Single Marker และ Cluster Marker
        """
        clusters: List[Dict] = []
        assigned = [False] * len(players)

        for i in range(len(players)):
            if assigned[i]:
                continue

            current_group = [players[i]]
            assigned[i] = True

            for j in range(i + 1, len(players)):
                if assigned[j]:
                    continue
                d = math.hypot(
                    players[i].current_pos["x"] - players[j].current_pos["x"],
                    players[i].current_pos["y"] - players[j].current_pos["y"]
                )
                if d <= self.cluster_radius:
                    current_group.append(players[j])
                    assigned[j] = True

            if len(current_group) == 1:
                p = current_group[0]
                clusters.append({
                    "is_cluster": False,
                    "x": p.current_pos["x"],
                    "y": p.current_pos["y"],
                    "player": p
                })
            else:
                avg_x = sum(p.current_pos["x"] for p in current_group) / len(current_group)
                avg_y = sum(p.current_pos["y"] for p in current_group) / len(current_group)
                clusters.append({
                    "is_cluster": True,
                    "count": len(current_group),
                    "x": avg_x,
                    "y": avg_y,
                    "members": current_group
                })

        return clusters


class NavigationEngine:
    """
    ระบบนำทางและคำนวณเวกเตอร์ระยะทาง (Tracking Vector Line & Off-screen indicator)
    """
    @staticmethod
    def calculate_tracking_vector(
        origin_x: float,
        origin_y: float,
        target_x: float,
        target_y: float
    ) -> Dict:
        dx = target_x - origin_x
        dy = target_y - origin_y
        dist = math.hypot(dx, dy)
        angle = math.atan2(dy, dx)
        mid_x = (origin_x + target_x) / 2.0
        mid_y = (origin_y + target_y) / 2.0

        return {
            "origin": {"x": origin_x, "y": origin_y},
            "target": {"x": target_x, "y": target_y},
            "distance": dist,
            "angle": angle,
            "midpoint": {"x": mid_x, "y": mid_y}
        }
