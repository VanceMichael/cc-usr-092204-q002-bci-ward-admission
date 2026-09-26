"""团体标准：版本管理与"升级不追溯放宽旧病例"规则。"""
import unittest

from src.admission.standards import current_standard, get_standard, is_looser
from tests.helpers import STANDARD_V1, build_plan, build_service


def looser_v2():
    update = dict(STANDARD_V1)
    update.update(version="2.0.0", age_max=80, change_notes="放宽年龄上限")
    return update


def stricter_v2():
    update = dict(STANDARD_V1)
    update.update(version="2.0.0", min_ward_level="三级", change_notes="提高病房等级")
    return update


class StandardTest(unittest.TestCase):
    def test_publish_supersedes_previous_version(self):
        service = build_service()
        service.publish_standard(**looser_v2())
        self.assertEqual(current_standard(service.store, "T/BCI-WARD").version, "2.0.0")
        old = get_standard(service.store, "T/BCI-WARD", "1.0.0")
        self.assertEqual(old.status, "已替代")

    def test_plan_keeps_pinned_version_after_upgrade(self):
        service = build_service()
        plan = build_plan(service)
        service.publish_standard(**looser_v2())
        stored = service.store.get("plans", plan["plan_id"])
        self.assertEqual(stored.standard_version, "1.0.0")
        decision = service.evaluate(plan["plan_id"])
        self.assertEqual(decision["snapshot"]["standard"]["version"], "1.0.0")

    def test_repin_to_looser_version_rejected(self):
        service = build_service()
        plan = build_plan(service)
        service.publish_standard(**looser_v2())
        with self.assertRaises(ValueError) as ctx:
            service.repin_standard(plan["plan_id"], "2.0.0", actor="auditor")
        self.assertIn("不得追溯放宽", str(ctx.exception))
        stored = service.store.get("plans", plan["plan_id"])
        self.assertEqual(stored.standard_version, "1.0.0")

    def test_repin_to_stricter_version_allowed(self):
        service = build_service()
        plan = build_plan(service)
        service.publish_standard(**stricter_v2())
        service.repin_standard(plan["plan_id"], "2.0.0", actor="auditor")
        stored = service.store.get("plans", plan["plan_id"])
        self.assertEqual(stored.standard_version, "2.0.0")

    def test_is_looser_dimensions(self):
        service = build_service()
        old = get_standard(service.store, "T/BCI-WARD", "1.0.0")
        service.publish_standard(**dict(STANDARD_V1, version="2", age_max=80))
        new = get_standard(service.store, "T/BCI-WARD", "2")
        self.assertTrue(is_looser(new, old))
        self.assertFalse(is_looser(old, new))


if __name__ == "__main__":
    unittest.main()
