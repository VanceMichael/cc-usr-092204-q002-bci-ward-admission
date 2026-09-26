"""交换契约：样例与运行结果符合 contracts/ 中的结构约定。"""
import json
import unittest
from pathlib import Path

from src.admission.contracts import validate
from src.catalog import load_context
from tests.helpers import build_plan, build_service

CONTRACTS = Path(__file__).resolve().parents[1] / "contracts"


def load_schema(name):
    return json.loads((CONTRACTS / name).read_text(encoding="utf-8"))


class ContractTest(unittest.TestCase):
    def test_context_fixture_matches_schema(self):
        errors = validate(load_context(), load_schema("context.schema.json"))
        self.assertEqual(errors, [])

    def test_clearance_decision_matches_schema(self):
        service = build_service()
        plan = build_plan(service)
        decision = service.evaluate(plan["plan_id"])
        errors = validate(decision, load_schema("clearance.schema.json"))
        self.assertEqual(errors, [])

    def test_plan_matches_schema(self):
        service = build_service()
        plan = build_plan(service)
        errors = validate(plan, load_schema("plan.schema.json"))
        self.assertEqual(errors, [])

    def test_schema_rejects_out_of_scope_indication(self):
        service = build_service()
        plan = build_plan(service)
        plan["indication"] = "抑郁症"
        errors = validate(plan, load_schema("plan.schema.json"))
        self.assertTrue(any("枚举" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
