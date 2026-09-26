"""角色权限：会诊专家与企业工程师仅见职责内信息。"""
import unittest

from tests.helpers import build_plan, build_service


class RbacTest(unittest.TestCase):
    def setUp(self):
        self.service = build_service()
        self.plan = build_plan(self.service)

    def test_engineer_sees_device_fields_only(self):
        view = self.service.plan_view(
            self.plan["plan_id"], "engineer", manufacturer="某医疗科技"
        )
        self.assertIsNotNone(view)
        self.assertIn("batch_id", view)
        self.assertIn("device_snapshot", view)
        self.assertNotIn("case_ref", view)
        self.assertNotIn("indication", view)
        self.assertNotIn("institution_id", view)

    def test_engineer_cannot_see_case(self):
        view = self.service.case_view("CASE-1", "engineer", manufacturer="某医疗科技")
        self.assertIsNone(view)

    def test_engineer_limited_to_own_products(self):
        view = self.service.batch_view("BAT-1", "engineer", manufacturer="其他厂商")
        self.assertIsNone(view)
        view = self.service.batch_view("BAT-1", "engineer", manufacturer="某医疗科技")
        self.assertEqual(view["batch_id"], "BAT-1")

    def test_consultant_sees_flagged_plan_summary(self):
        view = self.service.plan_view(self.plan["plan_id"], "consultant")
        self.assertIsNotNone(view)
        self.assertIn("indication", view)
        self.assertNotIn("device_snapshot", view)
        self.assertNotIn("consent_id", view)

    def test_consultant_cannot_see_unflagged_plan(self):
        plan = self.service.create_plan(
            case_ref="CASE-1",
            institution_id="INST-A",
            indication="偏瘫",
            ethics_approval_id="ETH-1",
            consent_id="CON-1",
            batch_id="BAT-1",
            operator_id="OP-1",
            consult_flag=False,
        )
        self.assertIsNone(self.service.plan_view(plan["plan_id"], "consultant"))

    def test_consultant_cannot_see_device_batch(self):
        self.assertIsNone(self.service.batch_view("BAT-1", "consultant"))

    def test_clinician_limited_to_own_institution(self):
        view = self.service.plan_view(
            self.plan["plan_id"], "clinician", institution_id="INST-B"
        )
        self.assertIsNone(view)
        view = self.service.plan_view(
            self.plan["plan_id"], "clinician", institution_id="INST-A"
        )
        self.assertEqual(view["plan_id"], self.plan["plan_id"])
        self.assertIn("consent_id", view)

    def test_auditor_sees_everything(self):
        view = self.service.plan_view(self.plan["plan_id"], "auditor")
        self.assertIn("device_snapshot", view)
        self.assertIn("consent_id", view)

    def test_unknown_role_rejected(self):
        with self.assertRaises(PermissionError):
            self.service.plan_view(self.plan["plan_id"], "visitor")


if __name__ == "__main__":
    unittest.main()
