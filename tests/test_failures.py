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

    def test_version_conflict_and_blocking_opinion(self):
        self.service.register_operator({"operator_user_id": "op-a", "organization": "Org-A"}, "c", "coordinator")
        item = self.service.create_item(self.payload, "a", "analyst")
        item = self.service.act(item["id"], "assess", {"hours_to_tca": 2}, "a", "analyst", item["version"])
        item = self.service.act(item["id"], "record_opinion", {"operator": "Org-A", "opinion": "reject", "reason": "unsafe"}, "op-a", "operator")
        with self.assertRaises(DomainError) as context:
            self.service.act(item["id"], "approve", {"fuel_cost_m_s": 1, "maneuver_window": "w"}, "c", "coordinator", item["version"])
        self.assertEqual(context.exception.code, "signoff_blocked")
        self.assertIn("Org-A", str(context.exception))
        with self.assertRaises(ConflictError):
            self.service.act(item["id"], "record_opinion", {"operator": "Org-A", "opinion": "approve"}, "op-a", "operator", item["version"] - 1)


if __name__ == "__main__":
    unittest.main()
