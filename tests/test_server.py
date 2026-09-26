"""HTTP 接口：端到端走通放行、变更、阻断、日志同步与审计。"""
import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from src.server import make_handler
from tests.helpers import build_plan, build_service


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.service = build_service()
        self.plan = build_plan(self.service)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.service))
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)

    def call(self, method, path, body=None, headers=None):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            method=method,
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8"))

    def test_health(self):
        status, payload = self.call("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ok")

    def test_context(self):
        status, payload = self.call("GET", "/context")
        self.assertEqual(status, 200)
        self.assertTrue(payload["project"])

    def test_clearance_endpoint_returns_explainable_conclusion(self):
        status, payload = self.call(
            "POST", f"/api/plans/{self.plan['plan_id']}/clearance", {"decided_by": "nurse-1"}
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["allowed"])
        self.assertEqual(len(payload["checks"]), 7)
        self.assertIn("放行", payload["conclusion"])

    def test_plan_view_requires_role(self):
        status, _ = self.call("GET", f"/api/plans/{self.plan['plan_id']}")
        self.assertEqual(status, 403)

    def test_engineer_view_excludes_patient_info(self):
        from urllib.parse import quote

        status, payload = self.call(
            "GET",
            f"/api/plans/{self.plan['plan_id']}",
            headers={"X-Role": "engineer", "X-Manufacturer": quote("某医疗科技")},
        )
        self.assertEqual(status, 200)
        self.assertNotIn("case_ref", payload)
        self.assertIn("device_snapshot", payload)

    def test_batch_change_and_adverse_flow(self):
        status, payload = self.call(
            "POST",
            "/api/batches/BAT-1/changes",
            {"changes": {"firmware_version": "F1.1"}, "reason": "升级"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["impacts"][0]["action"], "补充告知")

        status, payload = self.call(
            "POST",
            "/api/adverse-events",
            {"plan_id": self.plan["plan_id"], "severity": "严重", "description": "抽搐"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["blocked"])

        status, payload = self.call(
            "POST", f"/api/plans/{self.plan['plan_id']}/clearance", {}
        )
        self.assertFalse(payload["allowed"])

    def test_log_sync_endpoint(self):
        entries = [
            {
                "entry_id": "WARD-9-000001",
                "terminal_id": "WARD-9",
                "seq": 1,
                "actor": "nurse-1",
                "action": "treatment_step",
                "payload": {"plan_id": self.plan["plan_id"]},
                "ts": "2026-09-26T09:00:00+00:00",
                "prev_hash": "GENESIS",
            }
        ]
        from src.admission.oplog import entry_hash

        entries[0]["hash"] = entry_hash(entries[0])
        status, payload = self.call(
            "POST", "/api/logs/sync", {"terminal_id": "WARD-9", "entries": entries}
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["accepted"], 1)

    def test_audit_reconstruct_endpoint(self):
        _, decision = self.call(
            "POST", f"/api/plans/{self.plan['plan_id']}/clearance", {}
        )
        status, payload = self.call(
            "GET", f"/api/audit/decisions/{decision['decision_id']}"
        )
        self.assertEqual(status, 200)
        self.assertIn("standard", payload)
        self.assertIn("device_state", payload)
        self.assertIn("manual_decisions", payload)

    def test_unknown_route_404(self):
        status, _ = self.call("GET", "/api/nope")
        self.assertEqual(status, 404)

    def test_missing_plan_404(self):
        status, _ = self.call("POST", "/api/plans/PLAN-9999/clearance", {})
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
