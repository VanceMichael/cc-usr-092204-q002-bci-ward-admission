"""事后还原：规范、证据、人工决定与设备状态。"""
import unittest

from tests.helpers import build_plan, build_service


class AuditTest(unittest.TestCase):
    def test_reconstruct_decision_context(self):
        service = build_service()
        plan = build_plan(service)
        decision = service.evaluate(plan["plan_id"], decided_by="nurse-1")
        restored = service.reconstruct(decision["decision_id"])

        self.assertEqual(restored["decision"]["decision_id"], decision["decision_id"])
        # 当时采用的规范
        self.assertEqual(restored["standard"]["version"], "1.0.0")
        self.assertEqual(restored["standard"]["min_ward_level"], "二级")
        # 当时的证据
        evidence = restored["evidence"]
        self.assertEqual(evidence["ethics"]["approval_id"], "ETH-1")
        self.assertEqual(evidence["consent"]["consent_id"], "CON-1")
        self.assertEqual(evidence["operator"]["operator_id"], "OP-1")
        self.assertEqual(len(evidence["observations"]), 1)
        # 当时的设备状态
        self.assertEqual(restored["device_state"]["firmware_version"], "F1.0")
        self.assertEqual(restored["device_state"]["status"], "在用")

    def test_manual_decision_appears_in_trail(self):
        service = build_service()
        plan = build_plan(service)
        decision = service.manual_release(plan["plan_id"], "manager-1", "复核无误")
        self.assertTrue(decision["manual"])
        restored = service.reconstruct(decision["decision_id"])
        self.assertEqual(len(restored["manual_decisions"]), 1)
        entry = restored["manual_decisions"][0]
        self.assertEqual(entry["actor"], "manager-1")
        self.assertEqual(entry["details"]["reason"], "复核无误")

    def test_manual_release_cannot_bypass_failed_gates(self):
        service = build_service()
        plan = build_plan(service)
        ethics = service.store.get("ethics", "ETH-1")
        ethics.valid_to = "2026-06-30"
        with self.assertRaises(ValueError):
            service.manual_release(plan["plan_id"], "manager-1", "尝试绕过")

    def test_plan_trail_covers_lifecycle(self):
        service = build_service()
        plan = build_plan(service)
        service.evaluate(plan["plan_id"])
        service.change_batch_versions("BAT-1", {"firmware_version": "F1.1"}, "升级")
        service.resolve_supplement(plan["plan_id"], "已补充告知", "nurse-1")
        trail = service.plan_trail(plan["plan_id"])
        actions = [entry["action"] for entry in trail]
        self.assertIn("plan_created", actions)
        self.assertIn("clearance_evaluated", actions)
        self.assertIn("batch_version_changed", actions)
        self.assertIn("supplement_resolved", actions)

    def test_reconstruct_unknown_decision_404(self):
        service = build_service()
        with self.assertRaises(KeyError):
            service.reconstruct("DEC-999999")


if __name__ == "__main__":
    unittest.main()
