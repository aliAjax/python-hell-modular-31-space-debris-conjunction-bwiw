import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _two_operator_item(self):
        self.service.register_operator({"operator_user_id": "op-a", "organization": "Org-A"}, "c", "coordinator")
        self.service.register_operator({"operator_user_id": "op-b", "organization": "Org-B"}, "c", "coordinator")
        return self.service.create_item({
            "primary_object_id": "SAT-1",
            "secondary_object_id": "DEB-9",
            "tca": "2026-09-28T12:00:00+00:00",
            "miss_distance_m": 120,
            "covariance_m": 100,
            "fuel_budget_m_s": 5,
            "track_age_hours": 1,
            "operating_organizations": ["Org-A", "Org-B"],
            "object_operators": {"SAT-1": "Org-A", "DEB-9": "Org-B"},
        }, "analyst-1", "analyst")

    def test_complete_conjunction_workflow(self):
        item = self._two_operator_item()
        item = self.service.act(item["id"], "assess", {"hours_to_tca": 18}, "analyst-1", "analyst", item["version"])
        self.assertEqual(item["status"], "assessed")
        self.assertEqual(item["payload"]["assessment"]["level"], "high")

        # 两家运营方各自表态同意之前，协调员不能批准
        with self.assertRaises(Exception) as ctx:
            self.service.act(item["id"], "approve", {
                "fuel_cost_m_s": 2.5,
                "maneuver_window": "2026-09-28T08:00:00Z/2026-09-28T09:00:00Z",
            }, "coordinator-1", "coordinator", item["version"])
        self.assertEqual(ctx.exception.code, "signoff_incomplete")

        item = self.service.act(item["id"], "record_opinion",
                                {"operator": "Org-A", "opinion": "approve"}, "op-a", "operator")
        item = self.service.act(item["id"], "record_opinion",
                                {"operator": "Org-B", "opinion": "approve"}, "op-b", "operator")
        self.assertEqual(item["signoff"]["missing"], [])
        self.assertEqual(item["signoff"]["blockers"], [])

        item = self.service.act(item["id"], "approve", {
            "fuel_cost_m_s": 2.5,
            "maneuver_window": "2026-09-28T08:00:00Z/2026-09-28T09:00:00Z",
        }, "coordinator-1", "coordinator", item["version"])
        self.assertEqual(item["status"], "coordinating")
        self.assertEqual(item["payload"]["countersign"]["round"], 1)

        item = self.service.act(item["id"], "execute", {"command_ref": "CMD-7"}, "op-a", "operator", item["version"])
        self.assertEqual(item["status"], "executing")
        item = self.service.act(item["id"], "resolve", {"report_ref": "RPT-7"}, "coordinator-1", "coordinator", item["version"])
        self.assertEqual(item["status"], "resolved")
        self.assertGreaterEqual(len(item["audit"]), 7)


if __name__ == "__main__":
    unittest.main()
