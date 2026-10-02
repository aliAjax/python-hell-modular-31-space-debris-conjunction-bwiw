import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service
from src.domain import ConflictError, DomainError


class CountersignTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)
        self.payload = {
            "primary_object_id": "SAT-1",
            "secondary_object_id": "DEB-9",
            "tca": "2026-09-28T12:00:00+00:00",
            "miss_distance_m": 120,
            "covariance_m": 100,
            "fuel_budget_m_s": 5,
            "track_age_hours": 1,
            "operating_organizations": ["Org-A", "Org-B"],
        }

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _setup(self):
        item = self.service.create_item(self.payload, "analyst-1", "analyst")
        item = self.service.act(item["id"], "assess", {"hours_to_tca": 18}, "analyst-1", "analyst", item["version"])
        self.service.register_operator_member("Org-A", "operator-a", "coord-1", "coordinator")
        self.service.register_operator_member("Org-B", "operator-b", "coord-1", "coordinator")
        return item

    def _opine(self, item, operator, opinion, actor, version):
        return self.service.act(item["id"], "record_opinion", {
            "operator": operator, "opinion": opinion,
        }, actor, "operator", version)

    def test_unanimous_approval_required(self):
        item = self._setup()
        # 只有 Org-A 同意、Org-B 未表态 → 不能批准
        item = self._opine(item, "Org-A", "approve", "operator-a", item["version"])
        with self.assertRaises(DomainError) as context:
            self.service.act(item["id"], "approve", {"fuel_cost_m_s": 2, "maneuver_window": "w"}, "coord-1", "coordinator", item["version"])
        self.assertEqual(context.exception.code, "opinion_pending")
        self.assertIn("Org-B", str(context.exception))
        # Org-B 也同意 → 批准
        item = self._opine(item, "Org-B", "approve", "operator-b", item["version"])
        item = self.service.act(item["id"], "approve", {"fuel_cost_m_s": 2, "maneuver_window": "w"}, "coord-1", "coordinator", item["version"])
        self.assertEqual(item["status"], "coordinating")
        self.assertEqual(len(item["payload"]["approved_maneuver"]["countersigned_by"]), 2)

    def test_reject_or_review_blocks_and_names_operator(self):
        item = self._setup()
        item = self._opine(item, "Org-A", "approve", "operator-a", item["version"])
        item = self._opine(item, "Org-B", "request_review", "operator-b", item["version"])
        with self.assertRaises(DomainError) as context:
            self.service.act(item["id"], "approve", {"fuel_cost_m_s": 2, "maneuver_window": "w"}, "coord-1", "coordinator", item["version"])
        self.assertEqual(context.exception.code, "unresolved_conflict")
        self.assertIn("Org-B", str(context.exception))
        # 改成反对同样退回
        item = self._opine(item, "Org-B", "reject", "operator-b", item["version"])
        with self.assertRaises(DomainError) as context:
            self.service.act(item["id"], "approve", {"fuel_cost_m_s": 2, "maneuver_window": "w"}, "coord-1", "coordinator", item["version"])
        self.assertIn("Org-B", str(context.exception))

    def test_late_concurrent_opinion_decides_outcome(self):
        item = self._setup()
        # 两家同时读取同一版本，各自提交意见
        v = item["version"]
        item_a = self._opine(item, "Org-A", "approve", "operator-a", v)
        # Org-B 用旧版本提交 → 版本冲突，重试后（晚到的一条）按提交时间决定结论
        with self.assertRaises(ConflictError):
            self._opine(item, "Org-B", "reject", "operator-b", v)
        item_b = self._opine(item, "Org-B", "reject", "operator-b", item_a["version"])
        self.assertTrue(item_b["payload"]["conflict"])
        with self.assertRaises(DomainError) as context:
            self.service.act(item["id"], "approve", {"fuel_cost_m_s": 2, "maneuver_window": "w"}, "coord-1", "coordinator", item_b["version"])
        self.assertEqual(context.exception.code, "unresolved_conflict")
        self.assertIn("Org-B", str(context.exception))

    def test_opinion_change_after_approval_invalidates_and_returns_command(self):
        item = self._setup()
        item = self._opine(item, "Org-A", "approve", "operator-a", item["version"])
        item = self._opine(item, "Org-B", "approve", "operator-b", item["version"])
        item = self.service.act(item["id"], "approve", {"fuel_cost_m_s": 2, "maneuver_window": "w"}, "coord-1", "coordinator", item["version"])
        self.assertEqual(item["status"], "coordinating")
        # 批准后 Org-A 改动意见 → 批准连带失效，退回重议
        item = self._opine(item, "Org-A", "reject", "operator-a", item["version"])
        self.assertEqual(item["status"], "assessed")
        self.assertNotIn("approved_maneuver", item["payload"])
        self.assertTrue(any(e["event_type"] == "record_opinion" and e["payload"].get("approval_invalidated") for e in item["audit"]))
        # 指令尚未发出，执行被拦住
        with self.assertRaises(DomainError) as context:
            self.service.act(item["id"], "execute", {"command_ref": "CMD-9"}, "operator-a", "operator", item["version"])
        self.assertEqual(context.exception.code, "invalid_state")
        # 重新议决：Org-A 改回同意，再次批准
        item = self._opine(item, "Org-A", "approve", "operator-a", item["version"])
        item = self.service.act(item["id"], "approve", {"fuel_cost_m_s": 2, "maneuver_window": "w"}, "coord-1", "coordinator", item["version"])
        self.assertEqual(item["status"], "coordinating")

    def test_opinion_change_after_execution_keeps_records(self):
        item = self._setup()
        item = self._opine(item, "Org-A", "approve", "operator-a", item["version"])
        item = self._opine(item, "Org-B", "approve", "operator-b", item["version"])
        item = self.service.act(item["id"], "approve", {"fuel_cost_m_s": 2, "maneuver_window": "w"}, "coord-1", "coordinator", item["version"])
        item = self.service.act(item["id"], "execute", {"command_ref": "CMD-7"}, "operator-a", "operator", item["version"])
        self.assertEqual(item["status"], "executing")
        # 指令发出后再改意见 → 保留原记录，指令与批准不被撤销
        item = self._opine(item, "Org-A", "reject", "operator-a", item["version"])
        self.assertEqual(item["status"], "executing")
        self.assertEqual(item["payload"]["command_ref"], "CMD-7")
        self.assertIn("approved_maneuver", item["payload"])


if __name__ == "__main__":
    unittest.main()
