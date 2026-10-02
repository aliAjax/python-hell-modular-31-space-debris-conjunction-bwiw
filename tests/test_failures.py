import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service
from src.domain import ConflictError, DomainError


class FailureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)
        self.payload = {
            "primary_object_id": "SAT-2",
            "secondary_object_id": "DEB-3",
            "tca": "2026-09-29T12:00:00+00:00",
            "miss_distance_m": 50,
            "covariance_m": 100,
            "fuel_budget_m_s": 4,
            "track_age_hours": 1,
            "operating_organizations": ["Org-A"],
        }

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_duplicate_and_permission_failures(self):
        item = self.service.create_item(self.payload, "a", "analyst")
        with self.assertRaises(ConflictError):
            self.service.create_item(self.payload, "a", "analyst")
        with self.assertRaises(DomainError) as context:
            self.service.act(item["id"], "assess", {"hours_to_tca": 2}, "x", "operator", item["version"])
        self.assertEqual(context.exception.status, 403)

    def test_version_conflict_and_conflicting_opinion(self):
        item = self.service.create_item(self.payload, "a", "analyst")
        item = self.service.act(item["id"], "assess", {"hours_to_tca": 2}, "a", "analyst", item["version"])
        self.service.register_operator_member("Org-A", "operator-1", "c", "coordinator")
        item = self.service.act(item["id"], "record_opinion", {"operator": "Org-A", "opinion": "reject", "reason": "unsafe"}, "operator-1", "operator", item["version"])
        with self.assertRaises(DomainError) as context:
            self.service.act(item["id"], "approve", {"fuel_cost_m_s": 1, "maneuver_window": "w"}, "c", "coordinator", item["version"])
        self.assertEqual(context.exception.code, "unresolved_conflict")
        self.assertIn("Org-A", str(context.exception))
        with self.assertRaises(ConflictError):
            self.service.act(item["id"], "record_opinion", {"operator": "Org-A", "opinion": "approve"}, "operator-1", "operator", item["version"] - 1)

    def test_unauthorized_opinion_is_rejected_and_audited(self):
        item = self.service.create_item(self.payload, "a", "analyst")
        item = self.service.act(item["id"], "assess", {"hours_to_tca": 2}, "a", "analyst", item["version"])
        self.service.register_operator_member("Org-A", "operator-1", "c", "coordinator")
        self.service.register_operator_member("Org-C", "operator-2", "c", "coordinator")
        # operator-1 隶属于 Org-A，却替 Org-B 表态 → 越权拒绝并留审计
        with self.assertRaises(DomainError) as context:
            self.service.act(item["id"], "record_opinion", {"operator": "Org-B", "opinion": "approve"}, "operator-1", "operator", item["version"])
        self.assertEqual(context.exception.code, "operator_mismatch")
        self.assertEqual(context.exception.status, 403)
        item = self.service.get_item(item["id"])
        self.assertTrue(any(e["event_type"] == "opinion_rejected" for e in item["audit"]))
        # operator-2 隶属于 Org-C，但 Org-C 未参与本次接近事件 → 同样拒绝并留审计
        with self.assertRaises(DomainError) as context:
            self.service.act(item["id"], "record_opinion", {"operator": "Org-C", "opinion": "approve"}, "operator-2", "operator", item["version"])
        self.assertEqual(context.exception.code, "operator_mismatch")
        self.assertEqual(context.exception.status, 403)
        # 未隶属任何运营方的用户不能表态
        with self.assertRaises(DomainError) as context:
            self.service.act(item["id"], "record_opinion", {"operator": "Org-A", "opinion": "approve"}, "nobody", "operator", item["version"])
        self.assertEqual(context.exception.code, "operator_mismatch")


if __name__ == "__main__":
    unittest.main()
