"""放行评估：七道闸门与可解释结论。"""
import unittest

from src.admission.service import ScopeFrozenError
from tests.helpers import build_plan, build_service


def gate(decision, name):
    return next(c for c in decision["checks"] if c["gate"] == name)


class ClearanceTest(unittest.TestCase):
    def test_all_gates_pass(self):
        service = build_service()
        plan = build_plan(service)
        decision = service.evaluate(plan["plan_id"])
        self.assertTrue(decision["allowed"])
        self.assertEqual(len(decision["checks"]), 7)
        self.assertIn("放行", decision["conclusion"])
        for check in decision["checks"]:
            self.assertTrue(check["passed"], check)
            self.assertTrue(check["detail"])

    def test_scope_frozen_to_single_plan(self):
        service = build_service()
        plan = build_plan(service)
        with self.assertRaises(ScopeFrozenError):
            service.revise_plan_scope(plan["plan_id"], indication="癫痫")

    def test_indication_limited_to_non_invasive_scope(self):
        service = build_service()
        with self.assertRaises(ValueError):
            build_plan(service, indication="抑郁症")

    def test_expired_ethics_blocks_release(self):
        service = build_service()
        ethics = service.store.get("ethics", "ETH-1")
        ethics.valid_to = "2026-06-30"
        plan = build_plan(service)
        decision = service.evaluate(plan["plan_id"])
        self.assertFalse(decision["allowed"])
        self.assertIn("伦理批件", decision["conclusion"])
        self.assertIn("有效期", gate(decision, "伦理批件")["detail"])

    def test_consent_version_mismatch_blocks_release(self):
        service = build_service()
        plan = build_plan(service)
        consent = service.store.get("consents", "CON-1")
        consent.device_snapshot["firmware_version"] = "F0.9"
        decision = service.evaluate(plan["plan_id"])
        self.assertFalse(decision["allowed"])
        self.assertIn("补充告知", gate(decision, "患者授权")["detail"])

    def test_blocked_batch_blocks_release(self):
        service = build_service()
        plan = build_plan(service)
        batch = service.store.get("batches", "BAT-1")
        batch.status = "阻断"
        decision = service.evaluate(plan["plan_id"])
        self.assertFalse(decision["allowed"])
        self.assertIn("阻断", gate(decision, "设备批次")["detail"])

    def test_unqualified_operator_blocks_release(self):
        service = build_service()
        service.add_operator(
            operator_id="OP-2",
            name="李某",
            institution_id="INST-A",
            qualifications=[
                {
                    "device_model": "BCI-X1",
                    "indication": "偏瘫",
                    "level": "初级",
                    "valid_to": "2027-01-01",
                }
            ],
        )
        plan = build_plan(service, operator_id="OP-2")
        decision = service.evaluate(plan["plan_id"])
        self.assertFalse(decision["allowed"])
        self.assertIn("等级", gate(decision, "操作者资质")["detail"])

    def test_missing_baseline_observation_blocks_release(self):
        service = build_service()
        plan = service.create_plan(
            case_ref="CASE-1",
            institution_id="INST-A",
            indication="偏瘫",
            ethics_approval_id="ETH-1",
            consent_id="CON-1",
            batch_id="BAT-1",
            operator_id="OP-1",
        )
        decision = service.evaluate(plan["plan_id"])
        self.assertFalse(decision["allowed"])
        self.assertIn("基线评估", gate(decision, "临床观察")["detail"])

    def test_excluded_condition_blocks_release(self):
        service = build_service()
        service.add_case(
            case_ref="CASE-2",
            institution_id="INST-A",
            age=50,
            sex="男",
            conditions=["颅内金属植入"],
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
        plan = build_plan(service, case_ref="CASE-2", consent_id="CON-2")
        decision = service.evaluate(plan["plan_id"])
        self.assertFalse(decision["allowed"])
        self.assertIn("排除情形", gate(decision, "团体标准条件")["detail"])

    def test_snapshot_supports_reconstruction(self):
        service = build_service()
        plan = build_plan(service)
        decision = service.evaluate(plan["plan_id"])
        snapshot = decision["snapshot"]
        self.assertEqual(snapshot["standard"]["version"], "1.0.0")
        self.assertEqual(snapshot["batch"]["batch_id"], "BAT-1")
        self.assertEqual(snapshot["ethics"]["approval_id"], "ETH-1")
        self.assertEqual(snapshot["consent"]["consent_id"], "CON-1")
        self.assertEqual(snapshot["operator"]["operator_id"], "OP-1")
        self.assertEqual(snapshot["case"]["case_ref"], "CASE-1")


if __name__ == "__main__":
    unittest.main()
