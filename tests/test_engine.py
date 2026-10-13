"""
Unit Tests for VoiceHUD Core Engine
"""
import unittest
import math
import os
import sys

# เพิ่ม root directory ใน sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from src.engine import (
        GatewayProjection,
        PlayerMovementState,
        MovementTracker,
        PackClusterManager,
        NavigationEngine
    )
except ImportError:
    from engine import (
        GatewayProjection,
        PlayerMovementState,
        MovementTracker,
        PackClusterManager,
        NavigationEngine
    )


class TestVoiceHUDEngine(unittest.TestCase):

    def test_gateway_projection_roundtrip(self):
        # ทดสอบการแปลงพิกัดไปและกลับ
        test_x, test_y = -150230.0, 284100.0
        svg_x, svg_y = GatewayProjection.project_coordinates(test_x, test_y)
        self.assertTrue(0 <= svg_x <= GatewayProjection.SVG_WIDTH)
        self.assertTrue(0 <= svg_y <= GatewayProjection.SVG_HEIGHT)

        recovered_x, recovered_y = GatewayProjection.unproject_coordinates(svg_x, svg_y)
        self.assertAlmostEqual(test_x, recovered_x, delta=0.1)
        self.assertAlmostEqual(test_y, recovered_y, delta=0.1)

    def test_movement_deadzone_and_forward_arrow(self):
        tracker = MovementTracker(max_footprints=5, min_step_dist=10.0, cull_dist=50.0)
        player = PlayerMovementState("p1", "Tester", 100.0, 100.0)

        # ขยับเล็กน้อยกว่า Deadzone (1.0 px) -> มุมต้องไม่เปลี่ยน
        tracker.update_player(player, 100.5, 100.5)
        self.assertEqual(player.heading_angle, 0.0)

        # ขยับชัดเจนไปทางขวา (+10 px x)
        tracker.update_player(player, 110.0, 100.0)
        self.assertAlmostEqual(player.heading_angle, 0.0, places=4)

        # ขยับลง (+10 px y)
        tracker.update_player(player, 110.0, 110.0)
        self.assertAlmostEqual(player.heading_angle, math.pi / 2, places=4)

    def test_footprint_stacking_and_ring_buffer(self):
        tracker = MovementTracker(max_footprints=3, min_step_dist=10.0)
        player = PlayerMovementState("p1", "Tester", 0.0, 0.0)

        # ก้าวที่ 1 (เกิน min_step_dist)
        tracker.update_player(player, 15.0, 0.0)
        self.assertEqual(len(player.footprints), 1)

        # ก้าวที่ 2 ไม่ถึง min_step_dist (ขยับแค่ 2 px) -> ไม่ต้องปั๊มรอยเท้า
        tracker.update_player(player, 17.0, 0.0)
        self.assertEqual(len(player.footprints), 1)

        # เพิ่มเรื่อยๆ จนเกินความจุ Ring buffer (max 3)
        tracker.update_player(player, 30.0, 0.0)
        tracker.update_player(player, 45.0, 0.0)
        tracker.update_player(player, 60.0, 0.0)
        self.assertEqual(len(player.footprints), 3)
        self.assertEqual(player.footprints[0].x, 30.0)  # จุดเก่าถูก pop ออก

    def test_proximity_culling(self):
        tracker = MovementTracker(cull_dist=50.0)
        local_pos = {"x": 100.0, "y": 100.0}

        # รอยเท้าตัวเองต้องแสดงเสมอ
        self.assertTrue(tracker.should_draw_footprints(True, {"x": 100.0, "y": 100.0}, local_pos))

        # เพื่อนอยู่ใกล้ (distance = 20) -> ซ่อนรอยเท้า
        self.assertFalse(tracker.should_draw_footprints(False, {"x": 120.0, "y": 100.0}, local_pos))

        # เพื่อนอยู่ไกล (distance = 80) -> แสดงรอยเท้า
        self.assertTrue(tracker.should_draw_footprints(False, {"x": 180.0, "y": 100.0}, local_pos))

    def test_pack_cluster_manager(self):
        cluster_mgr = PackClusterManager(cluster_radius=30.0)
        p1 = PlayerMovementState("p1", "Player 1", 100.0, 100.0)
        p2 = PlayerMovementState("p2", "Player 2", 105.0, 102.0)  # ใกล้ p1
        p3 = PlayerMovementState("p3", "Player 3", 300.0, 300.0)  # ไกล

        clusters = cluster_mgr.cluster_players([p1, p2, p3])
        self.assertEqual(len(clusters), 2)

        # ตรวจสอบว่ามี 1 Cluster และ 1 Single Marker
        cluster_item = next(c for c in clusters if c["is_cluster"])
        single_item = next(c for c in clusters if not c["is_cluster"])

        self.assertEqual(cluster_item["count"], 2)
        self.assertEqual(single_item["player"].name, "Player 3")

    def test_navigation_vector(self):
        nav = NavigationEngine.calculate_tracking_vector(0.0, 0.0, 30.0, 40.0)
        self.assertAlmostEqual(nav["distance"], 50.0)
        self.assertAlmostEqual(nav["midpoint"]["x"], 15.0)
        self.assertAlmostEqual(nav["midpoint"]["y"], 20.0)


if __name__ == "__main__":
    unittest.main()
