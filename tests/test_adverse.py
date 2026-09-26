"""严重不良事件：立即阻断同批设备，且人工放行不得绕过。"""
import unittest

from tests.helpers import build_plan, build_service


class AdverseEventTest(unittest.TestCase):
    def test_sae_blocks_batch_and_suspends_plans(self):
        service = build_service()
        plan = build_plan(service)
        result = service.report_adverse(plan["plan_id"], "严重", "治疗中出现抽搐")
        self.assertTrue(result["blocked"])
        self.assertIn(plan["plan_id"], result["affected_plans"])
        batch = service.store.get("batches", "BAT-1")
        self.assertEqual(batch.status, "阻断")
        stored = service.store.get("plans", plan["plan_id"])
        self.assertEqual(stored.status, "已暂停")
        decision = service.evaluate(plan["plan_id"])
        self.assertFalse(decision["allowed"])

    def test_sae_suspends_all_plans_on_same_batch(self):
        service = build_service()
        plan_a = build_plan(service)
        service.add_case(
            case_ref="CASE-2", institution_id="INST-A", age=45, sex="男", conditions=[]
        )
        service.add_consent(
            consent_id="CON-2",
            case_ref="CASE-2",
            scope=["治疗"],
            device_snapshot={
                "firmware_version": "F1.0",
                "calibration_version": "C1.0",
                "algorithm_version": "A1.0",
            },
        )
        plan_b = build_plan(service, case_ref="CASE-2", consent_id="CON-2")
        result = service.report_adverse(plan_a["plan_id"], "严重", "皮肤灼伤")
        self.assertEqual(
            sorted(result["affected_plans"]), sorted([plan_a["plan_id"], plan_b["plan_id"]])
        )

    def test_general_event_does_not_block(self):
        service = build_service()
        plan = build_plan(service)
        result = service.report_adverse(plan["plan_id"], "一般", "轻微头晕")
        self.assertFalse(result["blocked"])
        self.assertEqual(service.store.get("batches", "BAT-1").status, "在用")

    def test_manual_release_forbidden_during_block(self):
        service = build_service()
        plan = build_plan(service)
        service.report_adverse(plan["plan_id"], "严重", "抽搐")
        with self.assertRaises(PermissionError):
            service.manual_release(plan["plan_id"], "manager-1", "尝试放行")

    def test_reinstate_allows_resume(self):
        service = build_service()
        plan = build_plan(service)
        service.report_adverse(plan["plan_id"], "严重", "抽搐")
        service.reinstate_batch("BAT-1", "调查结案：个案因素", "auditor-1")
        service.resume_plan(plan["plan_id"], "auditor-1")
        self.assertEqual(service.store.get("plans", plan["plan_id"]).status, "进行中")

    def test_resume_forbidden_while_blocked(self):
        service = build_service()
        plan = build_plan(service)
        service.report_adverse(plan["plan_id"], "严重", "抽搐")
        with self.assertRaises(ValueError):
            service.resume_plan(plan["plan_id"], "nurse-1")


if __name__ == "__main__":
    unittest.main()
