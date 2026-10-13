"""
Gateway Extra Layers Manager
จัดการข้อมูล Zones (Sanctuary, Migration, Patrol), Items (Salt, Gastro, Clam), Food Categories,
และ Landmark/Region Labels พร้อมระบบ Hover Tooltip สำหรับ Zone
"""

import os
import json
import math
from typing import Dict, List, Optional, Tuple, Set
from PyQt5 import QtCore, QtGui
from PyQt5.QtCore import QPointF, QRectF
from PyQt5.QtGui import QPainter, QColor, QPen, QBrush, QPolygonF, QFont, QPainterPath

try:
    from src.engine import GatewayProjection
except ImportError:
    from engine import GatewayProjection


FOOD_CATEGORY_COLORS = {
    "animal": QColor(255, 82, 82, 220),       # แดงส้ม (สัตว์/เนื้อ)
    "fruit": QColor(255, 179, 0, 220),        # ส้มทอง (ผลไม้)
    "vegetable": QColor(139, 195, 74, 220),   # เขียวสด (ผัก)
    "plant": QColor(0, 230, 118, 220),        # เขียวนีออน (พืชทั่วไป)
    "flower": QColor(240, 98, 146, 220),      # ชมพู (ดอกไม้)
    "mushroom": QColor(171, 71, 188, 220),    # ม่วง (เห็ด)
    "nut": QColor(141, 110, 99, 220)          # น้ำตาล (ถั่ว/เมล็ดพืช)
}


class GatewayLayersManager:
    def __init__(self, json_path: Optional[str] = None):
        if json_path:
            self.json_path = json_path
        else:
            # ค้นหาใน data/gateway-overlay.json หรือเทียบเท่า (รองรับทั้ง source, _MEIPASS และ standalone)
            import sys
            candidates = []
            if getattr(sys, 'frozen', False):
                if hasattr(sys, '_MEIPASS'):
                    candidates.append(os.path.join(sys._MEIPASS, "data", "gateway-overlay.json"))
                    candidates.append(os.path.join(sys._MEIPASS, "gateway-overlay.json"))
                exe_dir = os.path.dirname(os.path.abspath(sys.executable))
                candidates.append(os.path.join(exe_dir, "data", "gateway-overlay.json"))
                candidates.append(os.path.join(exe_dir, "gateway-overlay.json"))

            root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            candidates.append(os.path.join(root_dir, "data", "gateway-overlay.json"))
            candidates.append(os.path.join(root_dir, "gateway-overlay.json"))
            candidates.append(os.path.join(os.path.dirname(__file__), "gateway-overlay.json"))

            self.json_path = candidates[0]
            for c in candidates:
                if os.path.exists(c):
                    self.json_path = c
                    break
        self.data: Dict = {}
        self.sanctuary_zones: List = []
        self.migration_zones: List = []
        self.patrol_zones: List = []
        self.items: List = []
        self.foods: List = []
        self.labels: List = []

        # Layer Visibility Toggles
        self.show_zones = True
        self.show_items = True          # Salt rocks, Gastro, Clam
        self.show_labels_region = True  # ชื่อ Region หลัก (Delta, Highlands, East Lake ฯลฯ)
        self.show_labels_all = True     # ชื่อจุดย่อย (Ponds, Bridges, Ports ฯลฯ)

        # Zone Category Toggles (เลือกเปิดเฉพาะหมวดโซนที่สนใจได้: sanctuary, migration, patrol)
        self.active_zone_categories: Set[str] = {"sanctuary", "migration", "patrol"}

        # Food Category Toggles (เลือกเปิดเฉพาะหมวดที่สนใจได้)
        self.show_food = False
        self.active_food_categories: Set[str] = {"animal", "fruit", "vegetable", "plant", "mushroom"}

        # Hovered Zone & Entity State
        self.hovered_zone_info: Optional[Dict] = None
        self.hovered_entity_info: Optional[Dict] = None

        self.load_data()

    def load_data(self):
        if not os.path.exists(self.json_path):
            return
        try:
            with open(self.json_path, "r", encoding="utf-8") as f:
                self.data = json.load(f)

            zones = self.data.get("zones", {})
            self.sanctuary_zones = zones.get("sanctuary", [])
            self.migration_zones = zones.get("migration", [])
            self.patrol_zones = zones.get("patrol", [])
            self.items = self.data.get("items", [])
            self.foods = self.data.get("food", [])
            self.labels = self.data.get("labels", [])
        except Exception as e:
            print(f"[Error loading gateway overlay]: {e}")

    @staticmethod
    def project_data_coord(data_x: float, data_y: float) -> Tuple[float, float]:
        svg_x = ((data_y - GatewayProjection.MIN_Y) / (GatewayProjection.MAX_Y - GatewayProjection.MIN_Y)) * GatewayProjection.SVG_WIDTH
        svg_y = ((data_x - GatewayProjection.MIN_X) / (GatewayProjection.MAX_X - GatewayProjection.MIN_X)) * GatewayProjection.SVG_HEIGHT
        return svg_x, svg_y

    def check_zone_hover(self, svg_x: float, svg_y: float) -> Optional[Dict]:
        """
        ตรวจสอบว่าพิกัดเคอร์เซอร์ (ใน SVG Space) อยู่ภายในโซนใดหรือไม่
        คืนค่า dict ข้อมูลของโซนที่ hover อยู่
        """
        if not self.show_zones:
            return None

        scale_x = GatewayProjection.SVG_WIDTH / (GatewayProjection.MAX_Y - GatewayProjection.MIN_Y)
        scale_y = GatewayProjection.SVG_HEIGHT / (GatewayProjection.MAX_X - GatewayProjection.MIN_X)

        all_zones = []
        if "sanctuary" in self.active_zone_categories:
            all_zones.append(("Sanctuary", "#00E676", self.sanctuary_zones))
        if "migration" in self.active_zone_categories:
            all_zones.append(("Migration (MMZ)", "#FF9100", self.migration_zones))
        if "patrol" in self.active_zone_categories:
            all_zones.append(("Patrol Zone", "#AA00FF", self.patrol_zones))

        pt = QPointF(svg_x, svg_y)

        matched_zones = []

        for ztype, color, zlist in all_zones:
            for zone in zlist:
                name = zone.get("name", "Zone")
                shapes = zone.get("shapes", [])
                is_inside_zone = False
                for shape in shapes:
                    kind = shape.get("kind")
                    if kind == "polygon":
                        ring = shape.get("ring", [])
                        if ring:
                            poly = QPolygonF([QPointF(*self.project_data_coord(p[0], p[1])) for p in ring])
                            if poly.containsPoint(pt, QtCore.Qt.OddEvenFill):
                                is_inside_zone = True
                                break
                    elif kind == "ellipse":
                        c = shape.get("c")
                        r = shape.get("r", [10, 10])
                        if c:
                            cx, cy = self.project_data_coord(c[0], c[1])
                            rx = r[1] * scale_x
                            ry = r[0] * scale_y
                            dx = svg_x - cx
                            dy = svg_y - cy
                            if (dx / max(rx, 1e-3))**2 + (dy / max(ry, 1e-3))**2 <= 1.0:
                                is_inside_zone = True
                                break
                if is_inside_zone:
                    matched_zones.append({"type": ztype, "name": name, "color": color, "x": svg_x, "y": svg_y})

        return matched_zones if matched_zones else None

    def check_entity_hover(self, svg_x: float, svg_y: float, threshold_svg: float = 6.0) -> Optional[Dict]:
        """
        ตรวจสอบว่าเคอร์เซอร์ชี้ใกล้กับ Item (Salt, Gastro, Clam) หรือ Food หรือไม่
        """
        # 1. ตรวจสอบ Items (SaltRock, Gastro, ClamRock)
        if self.show_items:
            for it in self.items:
                pos = it.get("pos")
                if not pos:
                    continue
                sx, sy = self.project_data_coord(pos[0], pos[1])
                if math.hypot(svg_x - sx, svg_y - sy) <= threshold_svg:
                    iclass = it.get("class", "Item")
                    icon_map = {"SaltRock": "🧂", "Gastro": "🌿", "ClamRock": "🐚"}
                    color_map = {"SaltRock": "#FFFFFF", "Gastro": "#32DC32", "ClamRock": "#00D2FF"}
                    name_map = {"SaltRock": "Salt Rock (ก้อนเกลือ)", "Gastro": "Gastrolith (ก้อนกรวด)", "ClamRock": "Clam Rock (หินหอย)"}
                    return {
                        "kind": "item",
                        "title": f"{icon_map.get(iclass, '📦')} {name_map.get(iclass, iclass)}",
                        "color": color_map.get(iclass, "#FFFFFF"),
                        "x": sx,
                        "y": sy
                    }

        # 2. ตรวจสอบ Foods
        if self.show_food:
            for f in self.foods:
                cat = f.get("category", "plant")
                if cat not in self.active_food_categories:
                    continue
                pos = f.get("pos")
                if not pos:
                    continue
                sx, sy = self.project_data_coord(pos[0], pos[1])
                if math.hypot(svg_x - sx, svg_y - sy) <= threshold_svg:
                    fclass = f.get("class", "Food")
                    icon_map = {
                        "animal": "🥩", "fruit": "🍎", "vegetable": "🥕",
                        "plant": "🌿", "mushroom": "🍄", "nut": "🌰", "flower": "🌸"
                    }
                    color = FOOD_CATEGORY_COLORS.get(cat, QColor(255, 215, 0)).name()
                    return {
                        "kind": "food",
                        "title": f"{icon_map.get(cat, '🍖')} {fclass} ({cat.capitalize()})",
                        "color": color,
                        "x": sx,
                        "y": sy
                    }

        return None

    def draw_layers(self, painter: QPainter, is_minimap: bool = False, entity_scale: float = 1.0):
        # 1. วาด Zones (Sanctuary / Migration / Patrol)
        if self.show_zones:
            self._draw_all_zones(painter, is_minimap)

        # 2. วาด Items (Salt Rocks / Gastro / Clam)
        if self.show_items:
            self._draw_items(painter, is_minimap, entity_scale)

        # 3. วาด Food ตามหมวดหมู่ (Animal, Fruit, Vegetable, Mushroom, etc.)
        if self.show_food and not is_minimap:
            self._draw_categorized_food(painter, entity_scale)

        # 4. วาด Region & Landmark Labels
        if not is_minimap:
            self._draw_labels(painter)

    def _draw_all_zones(self, painter: QPainter, is_minimap: bool):
        if "sanctuary" in self.active_zone_categories:
            s_pen = QPen(QColor(0, 230, 118, 180), 1.5 if not is_minimap else 1.0, QtCore.Qt.SolidLine)
            s_brush = QBrush(QColor(0, 230, 118, 45 if not is_minimap else 60))
            for zone in self.sanctuary_zones:
                self._draw_zone_shapes(painter, zone, s_pen, s_brush)

        if "migration" in self.active_zone_categories:
            m_pen = QPen(QColor(255, 145, 0, 170), 1.5 if not is_minimap else 1.0, QtCore.Qt.DashLine)
            m_brush = QBrush(QColor(255, 145, 0, 35 if not is_minimap else 50))
            for zone in self.migration_zones:
                self._draw_zone_shapes(painter, zone, m_pen, m_brush)

        if "patrol" in self.active_zone_categories:
            p_pen = QPen(QColor(170, 0, 255, 160), 1.2 if not is_minimap else 0.8, QtCore.Qt.DotLine)
            p_brush = QBrush(QColor(170, 0, 255, 25 if not is_minimap else 35))
            for zone in self.patrol_zones:
                self._draw_zone_shapes(painter, zone, p_pen, p_brush)

    def _draw_zone_shapes(self, painter: QPainter, zone_data: Dict, pen: QPen, brush: QBrush):
        shapes = zone_data.get("shapes", [])
        painter.setPen(pen)
        painter.setBrush(brush)

        scale_x = GatewayProjection.SVG_WIDTH / (GatewayProjection.MAX_Y - GatewayProjection.MIN_Y)
        scale_y = GatewayProjection.SVG_HEIGHT / (GatewayProjection.MAX_X - GatewayProjection.MIN_X)

        for shape in shapes:
            kind = shape.get("kind")
            if kind == "polygon":
                ring = shape.get("ring", [])
                if ring:
                    poly_pts = [QPointF(*self.project_data_coord(pt[0], pt[1])) for pt in ring]
                    painter.drawPolygon(QPolygonF(poly_pts))
            elif kind == "ellipse":
                c = shape.get("c")
                r = shape.get("r", [10, 10])
                rot = shape.get("rot", 0)
                if c:
                    cx, cy = self.project_data_coord(c[0], c[1])
                    radius_x = r[1] * scale_x
                    radius_y = r[0] * scale_y

                    painter.save()
                    painter.translate(cx, cy)
                    if rot:
                        painter.rotate(rot)
                    painter.drawEllipse(QPointF(0, 0), radius_x, radius_y)
                    painter.restore()

    def _draw_items(self, painter: QPainter, is_minimap: bool, entity_scale: float = 1.0):
        colors = {
            "Gastro": QColor(50, 220, 50, 230),
            "SaltRock": QColor(255, 255, 255, 240),
            "ClamRock": QColor(0, 210, 255, 230)
        }
        base_size = 2.2 if is_minimap else 3.2
        pt_size = base_size * entity_scale

        for it in self.items:
            pos = it.get("pos")
            if not pos:
                continue
            sx, sy = self.project_data_coord(pos[0], pos[1])
            iclass = it.get("class", "Item")
            color = colors.get(iclass, QColor(255, 255, 255, 200))

            painter.setPen(QtCore.Qt.NoPen)
            painter.setBrush(QBrush(color))
            painter.drawEllipse(QPointF(sx, sy), pt_size, pt_size)

        # วาด Outline Highlight รอบ Entity ที่กำลัง Hover หรือถูกเลือกอยู่
        if not is_minimap and self.hovered_entity_info:
            ent = self.hovered_entity_info.get("entity")
            if ent and ent.get("kind") == "item":
                ex, ey = ent["x"], ent["y"]
                ecolor = QColor(ent.get("color", "#FFFFFF"))
                painter.setBrush(QtCore.Qt.NoBrush)
                painter.setPen(QPen(ecolor, 2.0 * entity_scale))
                painter.drawEllipse(QPointF(ex, ey), pt_size + 4.0 * entity_scale, pt_size + 4.0 * entity_scale)

    def _draw_categorized_food(self, painter: QPainter, entity_scale: float = 1.0):
        painter.setPen(QtCore.Qt.NoPen)
        pt_size = 2.4 * entity_scale
        for f in self.foods:
            cat = f.get("category", "plant")
            if cat not in self.active_food_categories:
                continue
            pos = f.get("pos")
            if not pos:
                continue
            sx, sy = self.project_data_coord(pos[0], pos[1])
            color = FOOD_CATEGORY_COLORS.get(cat, QColor(255, 215, 0, 170))
            painter.setBrush(QBrush(color))
            painter.drawEllipse(QPointF(sx, sy), pt_size, pt_size)

        # วาด Outline Highlight รอบ Food ที่กำลัง Hover หรือถูกเลือกอยู่
        if self.hovered_entity_info:
            ent = self.hovered_entity_info.get("entity")
            if ent and ent.get("kind") == "food":
                ex, ey = ent["x"], ent["y"]
                ecolor = QColor(ent.get("color", "#FFD700"))
                painter.setBrush(QtCore.Qt.NoBrush)
                painter.setPen(QPen(ecolor, 2.0 * entity_scale))
                painter.drawEllipse(QPointF(ex, ey), 6.5 * entity_scale, 6.5 * entity_scale)

    def _draw_labels(self, painter: QPainter):
        """
        วาด Label แยกประเภทอย่างชัดเจน:
        - Region Name (Delta, Highlands, East Lake): ตัวหนาสีขาว คมชัด สไตล์แผนที่
        - Minor Landmarks (Ponds, Bridges, Water Treatment): ตัวเล็กสีนวลตา
        """
        for lbl in self.labels:
            pos = lbl.get("pos")
            name = lbl.get("name")
            if not pos or not name:
                continue
            tier = lbl.get("tier", "place")
            category = lbl.get("category", "land")

            # เช็ค Toggle การแสดงผล
            if tier == "region" and not self.show_labels_region:
                continue
            if tier != "region" and not self.show_labels_all:
                continue

            sx, sy = self.project_data_coord(pos[0], pos[1])
            clean_name = name.split("\n")[0]

            if tier == "region":
                # ป้าย Region หลัก (เด่นชัด สไตล์หัวข้อภูมิภาค)
                painter.setFont(QFont("SansSerif", 8, QFont.Bold))
                painter.setPen(QColor(255, 255, 255, 230))
                # เงาตัวอักษรเพื่อให้อ่านง่ายบนพื้นหลังแผนที่
                painter.setPen(QColor(0, 0, 0, 200))
                painter.drawText(int(sx - 24), int(sy + 5), clean_name)
                painter.setPen(QColor(255, 255, 255, 240))
                painter.drawText(int(sx - 25), int(sy + 4), clean_name)
            elif category == "water":
                # แหล่งน้ำ (บึง, บ่อน้ำ, ทะเลสาบ)
                painter.setFont(QFont("SansSerif", 5, QFont.Bold))
                painter.setPen(QColor(100, 210, 255, 180))
                painter.drawText(int(sx - 15), int(sy + 3), f"💧 {clean_name}")
            else:
                # แลนด์มาร์กทั่วไป (สะพาน, ประตู, โครงสร้าง)
                painter.setFont(QFont("SansSerif", 5))
                painter.setPen(QColor(220, 210, 190, 160))
                painter.drawText(int(sx - 15), int(sy + 3), clean_name)
