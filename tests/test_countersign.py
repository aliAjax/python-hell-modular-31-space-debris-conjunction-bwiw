import os
import sys
import tempfile
import threading
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
        self.service.register_operator({"operator_user_id": "op-a", "organization": "Org-A"}, "c", "coordinator")
        self.service.register_operator({"operator_user_id": "op-b", "organization": "Org-B"}, "c", "coordinator")

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _item(self):
        item = self.service.create_item({
            "primary_object_id": "SAT-1",
            "secondary_object_id": "SAT-2",
            "tca": "2026-09-30T12:00:00+00:00",
            "miss_distance_m": 80,
            "covariance_m": 100,
            "fuel_budget_m_s": 5,
            "track_age_hours": 1,
            "operating_organizations": ["Org-A", "Org-B"],
            "object_operators": {"SAT-1": "Org-A", "SAT-2": "Org-B"},
        }, "analyst-1", "analyst")
        return self.service.act(item["id"], "assess", {"hours_to_tca": 10}, "analyst-1", "analyst", item["version"])

    def _all_approve(self, item):
        item = self.service.act(item["id"], "record_opinion",
                                {"operator": "Org-A", "opinion": "approve"}, "op-a", "operator")
        item = self.service.act(item["id"], "record_opinion",
                                {"operator": "Org-B", "opinion": "approve"}, "op-b", "operator")
        return item

    def test_missing_and_blocking_opinions_name_the_org(self):
        item = self._item()
        item = self.service.act(item["id"], "record_opinion",
                                {"operator": "Org-A", "opinion": "approve"}, "op-a", "operator")
        with self.assertRaises(DomainError) as ctx:
            self.service.act(item["id"], "approve",
                             {"fuel_cost_m_s": 1, "maneuver_window": "w"}, "c", "coordinator", item["version"])
        self.assertEqual(ctx.exception.code, "signoff_incomplete")
        self.assertIn("Org-B", str(ctx.exception))

        item = self.service.act(item["id"], "record_opinion",
                                {"operator": "Org-B", "opinion": "reject", "reason": "unsafe"}, "op-b", "operator")
        with self.assertRaises(DomainError) as ctx:
            self.service.act(item["id"], "approve",
                             {"fuel_cost_m_s": 1, "maneuver_window": "w"}, "c", "coordinator", item["version"])
        self.assertEqual(ctx.exception.code, "signoff_blocked")
        self.assertIn("Org-B:reject", str(ctx.exception))

        item = self.service.act(item["id"], "record_opinion",
                                {"operator": "Org-B", "opinion": "request_review"}, "op-b", "operator")
        with self.assertRaises(DomainError) as ctx:
            self.service.act(item["id"], "approve",
                             {"fuel_cost_m_s": 1, "maneuver_window": "w"}, "c", "coordinator", item["version"])
        self.assertEqual(ctx.exception.code, "signoff_blocked")
        self.assertIn("Org-B:request_review", str(ctx.exception))

    def test_changed_opinion_voids_approval_and_returns_pending_command(self):
        item = self._all_approve(self._item())
        item = self.service.act(item["id"], "approve",
                                {"fuel_cost_m_s": 1, "maneuver_window": "w"}, "c", "coordinator", item["version"])
        self.assertEqual(item["status"], "coordinating")
        item = self.service.act(item["id"], "prepare_command",
                                {"command_ref": "CMD-1"}, "op-a", "operator")
        self.assertEqual(item["payload"]["pending_command"]["command_ref"], "CMD-1")

        # 后到的反对意见：原批准连带失效，配好未发的指令退回重议
        item = self.service.act(item["id"], "record_opinion",
                                {"operator": "Org-B", "opinion": "reject", "reason": "new data"}, "op-b", "operator")
        self.assertEqual(item["status"], "review")
        self.assertIsNone(item["payload"]["countersign"])
        self.assertIsNone(item["payload"]["pending_command"])
        returned = item["payload"]["returned_commands"]
        self.assertEqual(len(returned), 1)
        self.assertEqual(returned[0]["command_ref"], "CMD-1")
        self.assertEqual(returned[0]["returned_reason"], "operator_opinion_changed")
        self.assertEqual(item["payload"]["countersign_history"][0]["voided_reason"], "operator_opinion_changed")

        # 退回状态下指令发不出去
        with self.assertRaises(DomainError):
            self.service.act(item["id"], "execute", {"command_ref": "CMD-1"}, "op-a", "operator", item["version"])
        # 反对未撤回也不能重新批准
        with self.assertRaises(DomainError) as ctx:
            self.service.act(item["id"], "approve",
                             {"fuel_cost_m_s": 1, "maneuver_window": "w"}, "c", "coordinator", item["version"])
        self.assertEqual(ctx.exception.code, "signoff_blocked")

        # 反对改回同意 -> 新一轮会签可以批准
        item = self.service.act(item["id"], "record_opinion",
                                {"operator": "Org-B", "opinion": "approve"}, "op-b", "operator")
        item = self.service.act(item["id"], "approve",
                                {"fuel_cost_m_s": 1, "maneuver_window": "w"}, "c", "coordinator", item["version"])
        self.assertEqual(item["status"], "coordinating")
        self.assertEqual(item["payload"]["countersign"]["round"], 2)
        # 退回记录仍保留
        self.assertEqual(len(item["payload"]["returned_commands"]), 1)

    def test_record_locked_after_command_issued(self):
        item = self._all_approve(self._item())
        item = self.service.act(item["id"], "approve",
                                {"fuel_cost_m_s": 1, "maneuver_window": "w"}, "c", "coordinator", item["version"])
        item = self.service.act(item["id"], "execute", {"command_ref": "CMD-2"}, "op-a", "operator", item["version"])
        self.assertEqual(item["status"], "executing")
        self.assertEqual(item["payload"]["command_ref"], "CMD-2")
        self.assertEqual(item["payload"]["issued_round"], 1)
        with self.assertRaises(DomainError) as ctx:
            self.service.act(item["id"], "record_opinion",
                             {"operator": "Org-B", "opinion": "reject"}, "op-b", "operator")
        self.assertEqual(ctx.exception.code, "record_locked")
        # 原会签记录保留
        fresh = self.service.get_item(item["id"])
        self.assertEqual(fresh["payload"]["countersign"]["round"], 1)
        latest = {row["operator"]: row for row in fresh["signoff"]["signatories"]}
        self.assertEqual(latest["Org-B"]["opinion"], "approve")

    def test_restating_same_opinion_keeps_approval(self):
        item = self._all_approve(self._item())
        item = self.service.act(item["id"], "approve",
                                {"fuel_cost_m_s": 1, "maneuver_window": "w"}, "c", "coordinator", item["version"])
        # 晚到但内容相同的意见只是重申，不翻结论
        item = self.service.act(item["id"], "record_opinion",
                                {"operator": "Org-A", "opinion": "approve"}, "op-a", "operator")
        self.assertEqual(item["status"], "coordinating")
        self.assertIsNotNone(item["payload"]["countersign"])

    def test_late_entry_wins_by_seq(self):
        item = self._all_approve(self._item())
        item = self.service.act(item["id"], "approve",
                                {"fuel_cost_m_s": 1, "maneuver_window": "w"}, "c", "coordinator", item["version"])
        # Org-A 同时提交两条：一条重申同意、一条改反对，序号大的生效
        item_a = self.service.act(item["id"], "record_opinion",
                                  {"operator": "Org-A", "opinion": "approve"}, "op-a", "operator")
        item_b = self.service.act(item["id"], "record_opinion",
                                  {"operator": "Org-A", "opinion": "reject"}, "op-a", "operator")
        latest = max(item_b["payload"]["opinions"], key=lambda e: e["seq"])
        self.assertEqual(latest["opinion"], "reject")
        self.assertEqual(item_b["status"], "review")
        self.assertEqual(item_b["signoff"]["blockers"], ["Org-A:reject"])
        self.assertGreaterEqual(len(item_a["payload"]["opinions"]), 2)

    def test_overreach_is_denied_and_audited(self):
        item = self._item()
        # Org-A 的用户不能替 Org-B 表态
        with self.assertRaises(DomainError) as ctx:
            self.service.act(item["id"], "record_opinion",
                             {"operator": "Org-B", "opinion": "approve"}, "op-a", "operator")
        self.assertEqual(ctx.exception.status, 403)
        self.assertEqual(ctx.exception.code, "operator_overreach")
        # 未登记的 operator 用户不能表态
        with self.assertRaises(DomainError) as ctx:
            self.service.act(item["id"], "record_opinion",
                             {"operator": "Org-A", "opinion": "approve"}, "stranger", "operator")
        self.assertEqual(ctx.exception.code, "operator_overreach")
        # 事件之外的运营方不能表态
        with self.assertRaises(DomainError) as ctx:
            self.service.act(item["id"], "record_opinion",
                             {"operator": "Org-C", "opinion": "approve"}, "op-a", "operator")
        self.assertEqual(ctx.exception.code, "operator_overreach")
        # 被拒操作不改状态，但留下三条越权审计
        fresh = self.service.get_item(item["id"])
        self.assertEqual(fresh["status"], "assessed")
        denials = [event for event in fresh["audit"] if event["event_type"] == "action_denied"]
        self.assertEqual(len(denials), 3)
        reasons = {(event["payload"]["reason"], event["payload"]["claimed_operator"]) for event in denials}
        self.assertEqual(reasons, {
            ("operator_overreach", "Org-B"),
            ("operator_overreach", "Org-A"),
            ("operator_overreach", "Org-C"),
        })
        self.assertEqual(fresh["payload"]["opinions"], [])

    def test_concurrent_opinions_both_land_and_later_commits_decide(self):
        item = self._all_approve(self._item())
        item = self.service.act(item["id"], "approve",
                                {"fuel_cost_m_s": 1, "maneuver_window": "w"}, "c", "coordinator", item["version"])
        barrier = threading.Barrier(2)
        errors = []

        def submit(user, opinion):
            barrier.wait()
            try:
                self.service.act(item["id"], "record_opinion",
                                 {"operator": "Org-A", "opinion": opinion}, user, "operator")
            except Exception as exc:  # noqa: BLE001 - 记录到主线程断言
                errors.append(exc)

        t1 = threading.Thread(target=submit, args=("op-a", "approve"))
        t2 = threading.Thread(target=submit, args=("op-a", "reject"))
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        self.assertEqual(errors, [])

        fresh = self.service.get_item(item["id"])
        opinions = fresh["payload"]["opinions"]
        seqs = [entry["seq"] for entry in opinions]
        self.assertEqual(len(seqs), 4)  # 最初两条 + 并发两条
        self.assertEqual(sorted(seqs), list(range(1, 5)))
        self.assertEqual(len({entry["submitted_at"] for entry in opinions[-2:]}), 2)
        latest = opinions[-1]
        # 两条并发意见都落库，晚提交（序号最大）的那条决定最终卡点
        expected_blockers = ["Org-A:reject"] if latest["opinion"] == "reject" else []
        self.assertEqual(fresh["signoff"]["blockers"], expected_blockers)
        # reject 一旦落库即退回 review；重申 approve 不会自动重新批准，需协调员重新会签
        self.assertEqual(fresh["status"], "review")

    def test_single_org_speaks_for_both_objects(self):
        item = self.service.create_item({
            "primary_object_id": "SAT-9",
            "secondary_object_id": "DEB-9",
            "tca": "2026-10-01T12:00:00+00:00",
            "miss_distance_m": 80,
            "covariance_m": 100,
            "fuel_budget_m_s": 5,
            "track_age_hours": 1,
            "operating_organizations": ["Org-A"],
        }, "analyst-1", "analyst")
        item = self.service.act(item["id"], "assess", {"hours_to_tca": 10}, "analyst-1", "analyst", item["version"])
        item = self.service.act(item["id"], "record_opinion",
                                {"operator": "Org-A", "opinion": "approve"}, "op-a", "operator")
        item = self.service.act(item["id"], "approve",
                                {"fuel_cost_m_s": 1, "maneuver_window": "w"}, "c", "coordinator", item["version"])
        self.assertEqual(item["status"], "coordinating")


if __name__ == "__main__":
    unittest.main()
