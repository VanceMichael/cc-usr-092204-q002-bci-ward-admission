"""版本变更影响分析：固件→补充告知、校准→暂停、算法→重新审批。"""
import unittest

from tests.helpers import build_plan, build_service


class ImpactTest(unittest.TestCase):
    def test_firmware_change_requires_supplement(self):
        service = build_service()
        plan = build_plan(service)
        report = service.change_batch_versions(
            "BAT-1", {"firmware_version": "F1.1"}, "例行固件升级"
        )
        self.assertEqual(report["impacts"][0]["action"], "补充告知")
        stored = service.store.get("plans", plan["plan_id"])
        self.assertEqual(stored.status, "待补充告知")
        consent = service.store.get("consents", "CON-1")
        self.assertEqual(consent.status, "需补充告知")
        decision = service.evaluate(plan["plan_id"])
        self.assertFalse(decision["allowed"])

    def test_supplement_resolution_restores_plan(self):
        service = build_service()
        plan = build_plan(service)
        service.change_batch_versions("BAT-1", {"firmware_version": "F1.1"}, "升级")
        service.resolve_supplement(plan["plan_id"], "已向患者补充告知固件变更", "nurse-1")
        stored = service.store.get("plans", plan["plan_id"])
        self.assertEqual(stored.status, "进行中")
        consent = service.store.get("consents", "CON-1")
        self.assertEqual(consent.status, "有效")
        self.assertEqual(consent.device_snapshot["firmware_version"], "F1.1")
        self.assertEqual(len(consent.supplements), 1)
        self.assertTrue(service.evaluate(plan["plan_id"])["allowed"])

    def test_calibration_change_suspends_plan(self):
        service = build_service()
        plan = build_plan(service)
        report = service.change_batch_versions(
            "BAT-1", {"calibration_version": "C1.1"}, "传感器重新校准"
        )
        self.assertEqual(report["impacts"][0]["action"], "暂停使用")
        stored = service.store.get("plans", plan["plan_id"])
        self.assertEqual(stored.status, "已暂停")
        service.resume_plan(plan["plan_id"], "engineer-1")
        self.assertEqual(service.store.get("plans", plan["plan_id"]).status, "进行中")

    def test_algorithm_change_requires_reapproval(self):
        service = build_service()
        plan = build_plan(service)
        report = service.change_batch_versions(
            "BAT-1", {"algorithm_version": "A2.0"}, "算法模型更新"
        )
        self.assertEqual(report["impacts"][0]["action"], "重新审批")
        stored = service.store.get("plans", plan["plan_id"])
        self.assertEqual(stored.status, "待重新审批")
        service.resolve_reapproval(plan["plan_id"], "伦理委员会复审通过", "committee")
        self.assertEqual(service.store.get("plans", plan["plan_id"]).status, "进行中")

    def test_combined_change_takes_severest_action(self):
        service = build_service()
        plan = build_plan(service)
        report = service.change_batch_versions(
            "BAT-1",
            {"firmware_version": "F1.1", "algorithm_version": "A2.0"},
            "联合升级",
        )
        self.assertEqual(report["impacts"][0]["action"], "重新审批")
        consent = service.store.get("consents", "CON-1")
        self.assertEqual(consent.status, "需补充告知")

    def test_completed_plan_not_affected(self):
        service = build_service()
        plan = build_plan(service)
        stored = service.store.get("plans", plan["plan_id"])
        stored.status = "已完成"
        report = service.change_batch_versions(
            "BAT-1", {"firmware_version": "F1.1"}, "升级"
        )
        self.assertEqual(report["impacts"], [])

    def test_unknown_component_rejected(self):
        service = build_service()
        build_plan(service)
        with self.assertRaises(ValueError):
            service.change_batch_versions("BAT-1", {"shell_version": "S2"}, "外壳")

    def test_noop_change_reports_nothing(self):
        service = build_service()
        build_plan(service)
        report = service.change_batch_versions(
            "BAT-1", {"firmware_version": "F1.0"}, "版本未变"
        )
        self.assertEqual(report["impacts"], [])


if __name__ == "__main__":
    unittest.main()
