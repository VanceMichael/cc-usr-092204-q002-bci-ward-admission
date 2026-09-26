"""床旁离线日志：断网不丢、防篡改、幂等同步。"""
import json
import tempfile
import unittest

from src.admission.oplog import BedsideLog
from tests.helpers import build_service


class OfflineLogTest(unittest.TestCase):
    def setUp(self):
        self.service = build_service()
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.terminal = BedsideLog("WARD-01", self.dir.name)

    def _append_three(self):
        for index in range(3):
            self.terminal.append(
                "nurse-1", "treatment_step", {"plan_id": "PLAN-0001", "step": index}, "2026-09-26T09:00:00+00:00"
            )

    def test_offline_entries_survive_and_sync(self):
        self._append_three()
        # 模拟断网期间终端重启：从磁盘恢复
        recovered = BedsideLog("WARD-01", self.dir.name)
        self.assertEqual(len(recovered.pending()), 3)
        result = self.service.sync_logs("WARD-01", recovered.pending())
        self.assertEqual(result["accepted"], 3)
        self.assertEqual(result["rejected"], [])
        actions = [e["action"] for e in self.service.store.audit_log if e.get("terminal_id") == "WARD-01"]
        self.assertEqual(actions, ["treatment_step"] * 3)

    def test_resync_is_idempotent(self):
        self._append_three()
        first = self.service.sync_logs("WARD-01", self.terminal.pending())
        self.assertEqual(first["accepted"], 3)
        second = self.service.sync_logs("WARD-01", self.terminal.pending())
        self.assertEqual(second["accepted"], 0)
        self.assertEqual(second["duplicates"], 3)
        count = len([e for e in self.service.store.audit_log if e.get("terminal_id") == "WARD-01"])
        self.assertEqual(count, 3)

    def test_tampered_entry_rejected(self):
        self._append_three()
        entries = self.terminal.pending()
        entries[1]["payload"]["step"] = 99  # 篡改内容
        result = self.service.sync_logs("WARD-01", entries)
        self.assertEqual(result["accepted"], 1)
        self.assertEqual(len(result["rejected"]), 2)
        self.assertEqual(result["rejected"][0]["reason"], "哈希校验失败")

    def test_sequence_gap_rejected(self):
        self._append_three()
        entries = self.terminal.pending()
        result = self.service.sync_logs("WARD-01", [entries[0], entries[2]])
        self.assertEqual(result["accepted"], 1)
        self.assertEqual(result["rejected"][0]["reason"], "序号不连续")

    def test_mark_synced_persists_cursor(self):
        self._append_three()
        self.terminal.mark_synced(2)
        recovered = BedsideLog("WARD-01", self.dir.name)
        self.assertEqual(len(recovered.pending()), 1)

    def test_spool_file_is_jsonl(self):
        self._append_three()
        lines = self.terminal.spool.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 3)
        for line in lines:
            self.assertIn("hash", json.loads(line))


if __name__ == "__main__":
    unittest.main()
