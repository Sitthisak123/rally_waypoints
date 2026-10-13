"""
Unit Tests for PartySyncNode (Private Party Room & PIN Code)
"""
import unittest
import time
from src.party_sync import PartySyncNode


class TestPartySync(unittest.TestCase):

    def test_party_node_initialization(self):
        node = PartySyncNode(
            player_id="tester_1",
            player_name="Tester",
            party_code="ISLE99",
            port=39825,
            is_host=True
        )
        self.assertEqual(node.party_code, "ISLE99")
        self.assertTrue(node.is_host)
        self.assertEqual(node.player_id, "tester_1")

    def test_party_packet_filtering(self):
        node = PartySyncNode(
            player_id="local_p",
            player_name="Me",
            party_code="ROOM1",
            port=39826
        )

        # 1. ทดสอบแพ็กเก็ตที่เป็นของห้องอื่น -> ต้องถูกปฏิเสธ
        other_room_pkt = (
            b'{"magic": "VOICEHUD_PARTY_V1", "party_code": "WRONG_ROOM", '
            b'"sender_id": "friend_1", "name": "Friend", "x": 100, "y": 200}'
        )
        node._handle_packet(other_room_pkt, ("127.0.0.1", 39826))
        self.assertNotIn("friend_1", node.known_peers)

        # 2. ทดสอบแพ็กเก็ตห้องเดียวกัน -> ต้องถูกบันทึก
        correct_room_pkt = (
            b'{"magic": "VOICEHUD_PARTY_V1", "party_code": "ROOM1", '
            b'"sender_id": "friend_1", "name": "Friend", "x": 100, "y": 200, "heading": 1.57}'
        )
        node._handle_packet(correct_room_pkt, ("127.0.0.1", 39826))
        self.assertIn("friend_1", node.known_peers)
        self.assertEqual(node.known_peers["friend_1"]["name"], "Friend")
        self.assertEqual(node.known_peers["friend_1"]["x"], 100)

        # 3. ทดสอบแพ็กเก็ตของตัวเอง -> ต้องข้าม ไม่นับซ้ำ
        self_pkt = (
            b'{"magic": "VOICEHUD_PARTY_V1", "party_code": "ROOM1", '
            b'"sender_id": "local_p", "name": "Me", "x": 50, "y": 50}'
        )
        node._handle_packet(self_pkt, ("127.0.0.1", 39826))
        self.assertNotIn("local_p", node.known_peers)

    def test_peer_timeout_pruning(self):
        node = PartySyncNode(
            player_id="local_p",
            player_name="Me",
            party_code="ROOM1"
        )
        node.known_peers["old_peer"] = {
            "id": "old_peer",
            "name": "Inactive Friend",
            "x": 0, "y": 0,
            "last_seen": time.time() - 15.0  # เกิน 8 วินาที
        }
        node.known_peers["active_peer"] = {
            "id": "active_peer",
            "name": "Active Friend",
            "x": 0, "y": 0,
            "last_seen": time.time()
        }

        node._prune_inactive_peers(time.time())
        self.assertNotIn("old_peer", node.known_peers)
        self.assertIn("active_peer", node.known_peers)


if __name__ == "__main__":
    unittest.main()
