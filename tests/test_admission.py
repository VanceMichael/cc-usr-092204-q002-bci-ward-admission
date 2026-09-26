"""准入后台测试：事件链、冻结/升级、变更影响、SAE 阻断、RBAC、断网补传、时点还原。"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from src import access, models as M, seed
from src.bedside import BedsideSpool
from src.eventstore import EventStore, TamperError
from src.service import AdmissionService, Registry


class ServiceTestBase(unittest.TestCase):
    clock = "2025-06-01T09:00:00Z"

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.store = EventStore(os.path.join(self.tmp, "log.jsonl"))
        self.svc = AdmissionService(self.store, clock=lambda: self.clock)
        seed.seed(self.svc)

    def fresh_case(self, **kw):
        return seed.seed_case(self.svc, **kw)

    def verdict(self, case_id, actor=None, **kw):
        return self.svc.evaluate_clearance(
            case_id=case_id, actor=actor or seed.PHYSICIAN, persist=False, **kw)["verdict"]

    def failed_codes(self, case_id, actor=None):
        d = self.svc.evaluate_clearance(case_id=case_id, actor=actor or seed.PHYSICIAN,
                                        persist=False)
        return [r["code"] for r in d["rules"] if not r["passed"]]


# --------------------------------------------------------------------------- #
# 事件存储
# --------------------------------------------------------------------------- #
class EventStoreTest(unittest.TestCase):
    def test_chain_and_reload(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "log.jsonl")
        s = EventStore(path)
        s.append("a", {"x": 1})
        s.append("b", {"y": 2})
        self.assertTrue(s.verify_chain()["ok"])
        s2 = EventStore(path)
        self.assertEqual([e.event_type for e in s2.events()], ["a", "b"])
        self.assertTrue(s2.verify_chain()["ok"])

    def test_tamper_detected(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "log.jsonl")
        EventStore(path).append("a", {"x": 1})
        lines = Path(path).read_text(encoding="utf-8").splitlines()
        rec = json.loads(lines[0])
        rec["data"]["x"] = 999  # 篡改内容
        Path(path).write_text(json.dumps(rec, ensure_ascii=False) + "\n", encoding="utf-8")
        with self.assertRaises(TamperError):
            EventStore(path)

    def test_idempotent_ingest(self):
        s = EventStore()
        e1 = s.append("t", {"v": 1}, event_id="ID-1")
        e2 = s.append("t", {"v": 2}, event_id="ID-1")  # 同一 event_id
        self.assertIs(e1, e2)
        self.assertEqual(len(s.events()), 1)


# --------------------------------------------------------------------------- #
# 冻结与放行
# --------------------------------------------------------------------------- #
class ClearanceTest(ServiceTestBase):
    def test_go_when_all_six_elements_valid(self):
        c = self.fresh_case()
        d = self.svc.evaluate_clearance(case_id=c["case_id"], actor=seed.PHYSICIAN,
                                        persist=False)
        self.assertEqual(d["verdict"], M.VERDICT_GO)
        codes = {r["code"] for r in d["rules"]}
        # 六类准入要素均有规则覆盖
        for expected in ("STD_SCOPE", "WARD_CERT", "ETHICS_OK", "CONSENT_OK",
                         "DEVICE_FROZEN_MATCH", "OPERATOR_OK"):
            self.assertIn(expected, codes)
        # 放行结论自带冻结快照与规范证据
        self.assertEqual(d["frozen_plan"]["standard_version"], "2025.1")
        self.assertTrue(d["explanation"][0].startswith("结论：放行"))

    def test_indication_frozen_to_one_plan_scope(self):
        # 癫痫仅允许住院：门诊冻结必须被拒绝
        with self.assertRaises(M.DomainError) as cm:
            self.fresh_case(case_id="C-EPI", indication=M.INDICATION_EPILEPSY,
                            setting="outpatient", ethics_no="EC-E1", consent_id="IC-E1")
        self.assertEqual(cm.exception.code, "STD_SCOPE")
        # 住院可冻结
        c = self.fresh_case(case_id="C-EPI2", indication=M.INDICATION_EPILEPSY,
                            setting="inpatient", ethics_no="EC-E2", consent_id="IC-E2")
        self.assertEqual(self.verdict(c["case_id"]), M.VERDICT_GO)
        # 即便门诊是同一患者的后续想法，也不能换场所放行（范围冻结到这一次计划）
        self.assertEqual(self.verdict(c["case_id"], setting="outpatient"), M.VERDICT_NO_GO)

    def test_standard_upgrade_does_not_retroactively_relax_or_tighten(self):
        c = self.fresh_case()
        self.svc.issue_standard(seed.ADMIN, {
            "code": seed.STD_CODE, "version": "2026.1", "effective_at": "2026-01-01",
            "title": "新版", "consent_template_version": "IC-NI-2026.1",
            "content_hash": "h2026",
            # 新版本把脑卒中康复限缩为仅住院（旧病例原为住院，验证不被追溯扰动）
            "scope": {M.INDICATION_STROKE_REHAB: {"settings": ["inpatient"], "max_plan_sessions": 40},
                      M.INDICATION_HEMIPLEGIA: {"settings": ["inpatient"], "max_plan_sessions": 30},
                      M.INDICATION_EPILEPSY: {"settings": ["inpatient"], "max_plan_sessions": 20}}})
        d = self.svc.evaluate_clearance(case_id=c["case_id"], actor=seed.PHYSICIAN,
                                        persist=False)
        self.assertEqual(d["verdict"], M.VERDICT_GO)  # 仍按 2025.1
        self.assertEqual(d["frozen_plan"]["standard_version"], "2025.1")
        rule = next(r for r in d["rules"] if r["code"] == "STD_VERSION_PINNED")
        self.assertIn("2026.1", rule["detail"])


class EthicsConsentTest(ServiceTestBase):
    def _case_with(self):
        return self.fresh_case(case_id="C-X", ethics_no="EC-X", consent_id="IC-X")

    def test_ethics_revoked_blocks(self):
        c = self._case_with()
        self.svc.revoke_ethics(seed.ETHICS, "EC-X", "监督检查发现问题")
        self.assertIn("ETHICS_REVOKED", self.failed_codes(c["case_id"]))
        self.assertEqual(self.verdict(c["case_id"]), M.VERDICT_NO_GO)

    def test_consent_withdrawn_blocks_and_flags_case(self):
        c = self._case_with()
        self.svc.withdraw_consent(seed.PHYSICIAN, "IC-X", "患者撤回")
        self.assertEqual(self.svc.reg.cases[c["case_id"]]["status"], M.CASE_SUSPENDED)
        self.assertIn("CONSENT_WITHDRAWN", self.failed_codes(c["case_id"]))

    def test_consent_template_version_mismatch_blocks_freeze(self):
        self.svc.grant_ethics(seed.ETHICS, {
            "approval_no": "EC-Y", "hospital_id": "H-001", "standard_code": seed.STD_CODE,
            "standard_version": "2025.1", "valid_from": "2025-03-10", "valid_until": "2026-12-31",
            "scope": {"indications": [M.INDICATION_HEMIPLEGIA]}})
        self.svc.open_case(seed.PHYSICIAN, {"case_id": "C-Y", "hospital_id": "H-001",
                                            "patient_ref": "P", "indication": M.INDICATION_HEMIPLEGIA})
        self.svc.grant_consent(seed.PHYSICIAN, {"consent_id": "IC-Y", "case_id": "C-Y",
                                                "template_version": "IC-OLD-999",
                                                "signed_at": "2025-03-11"})
        with self.assertRaises(M.DomainError) as cm:
            self.svc.freeze_plan(seed.PHYSICIAN, case_id="C-Y", ethics_approval_no="EC-Y",
                                 consent_id="IC-Y", device_id="D-1001", operator_id="op-zhao",
                                 ward_id="W-001", setting="inpatient",
                                 observation_protocol_id="OBS-P-NI-1.0")
        self.assertEqual(cm.exception.code, "CONSENT_VERSION")


# --------------------------------------------------------------------------- #
# 设备变更影响分析
# --------------------------------------------------------------------------- #
class DeviceChangeTest(ServiceTestBase):
    def test_firmware_change_suspends_affected_cases_only(self):
        a = self.fresh_case(case_id="C-A")
        b = self.fresh_case(case_id="C-B")
        # C-B 用另一台同批设备不受影响：先登记 D-1002
        self.svc.register_device(seed.ENGINEER, {
            "device_id": "D-1002", "batch_id": "B-NS-2025-0301",
            "firmware_version": "fw-3.1.0", "algorithm_version": "alg-2.4.0",
            "calibration_id": "CAL-2025-0302-A"})
        self.svc.grant_ethics(seed.ETHICS, {
            "approval_no": "EC-B2", "hospital_id": "H-001", "standard_code": seed.STD_CODE,
            "standard_version": "2025.1", "valid_from": "2025-03-10", "valid_until": "2026-12-31",
            "scope": {"indications": [M.INDICATION_STROKE_REHAB]}})
        # C-B 重建为使用 D-1002 的计划
        self.svc.grant_consent(seed.PHYSICIAN, {"consent_id": "IC-B2", "case_id": "C-B",
                                                "template_version": "IC-NI-2025.1", "signed_at": "2025-03-11"})
        self.svc.freeze_plan(seed.PHYSICIAN, case_id="C-B", ethics_approval_no="EC-B2",
                             consent_id="IC-B2", device_id="D-1002", operator_id="op-zhao",
                             ward_id="W-001", setting="inpatient",
                             observation_protocol_id="OBS-P-NI-1.0")
        evt, affected, rc = self.svc.report_device_change(
            seed.ENGINEER, device_id="D-1001", change_type=M.CHANGE_FIRMWARE,
            old="fw-3.1.0", new="fw-3.2.0")
        self.assertEqual(rc, "requalify")
        self.assertEqual(affected, ["C-A"])
        self.assertEqual(self.svc.reg.cases["C-A"]["status"], M.CASE_SUSPENDED)
        self.assertEqual(self.svc.reg.cases["C-B"]["status"], M.CASE_APPROVED)

    def test_requalify_then_accept_resumes_without_rewriting_snapshot(self):
        c = self.fresh_case()
        pid = self.svc.reg.cases[c["case_id"]]["plan_id"]
        before = self.svc.reg.plans[pid]["firmware_version"]
        evt, _, _ = self.svc.report_device_change(
            seed.ENGINEER, device_id="D-1001", change_type=M.CHANGE_FIRMWARE,
            old="fw-3.1.0", new="fw-3.2.0")
        self.assertEqual(self.verdict(c["case_id"]), M.VERDICT_NO_GO)
        # 未核验先接受 → 拒绝
        with self.assertRaises(M.DomainError) as cm:
            self.svc.accept_device_change(seed.DIRECTOR, case_id=c["case_id"], plan_id=pid,
                                          change_id=evt.data["change_id"], evidence_id="EV-1",
                                          equivalence_note="等价")
        self.assertEqual(cm.exception.code, "DEVICE_NOT_REQUALIFIED")
        self.svc.requalify_device(seed.ENGINEER, "D-1001", evidence_id="EV-1")
        self.svc.accept_device_change(seed.DIRECTOR, case_id=c["case_id"], plan_id=pid,
                                      change_id=evt.data["change_id"], evidence_id="EV-1",
                                      equivalence_note="回归测试等价")
        self.assertEqual(self.verdict(c["case_id"]), M.VERDICT_GO)
        # 冻结快照未被改写
        self.assertEqual(self.svc.reg.plans[pid]["firmware_version"], before)
        # 再次变更（无新接受）→ 旧接受自动失效
        evt2, _, _ = self.svc.report_device_change(
            seed.ENGINEER, device_id="D-1001", change_type=M.CHANGE_FIRMWARE,
            old="fw-3.2.0", new="fw-3.3.0")
        self.assertEqual(self.verdict(c["case_id"]), M.VERDICT_NO_GO)

    def test_algorithm_change_requires_reapproval_and_cannot_be_accepted(self):
        c = self.fresh_case()
        pid = self.svc.reg.cases[c["case_id"]]["plan_id"]
        evt, _, rc = self.svc.report_device_change(
            seed.ENGINEER, device_id="D-1001", change_type=M.CHANGE_ALGORITHM,
            old="alg-2.4.0", new="alg-3.0.0")
        self.assertEqual(rc, "reapprove")
        self.assertEqual(self.svc.reg.cases[c["case_id"]]["status"], M.CASE_REAPPROVAL)
        self.assertEqual(self.verdict(c["case_id"]), M.VERDICT_NO_GO)
        with self.assertRaises(M.DomainError) as cm:
            self.svc.accept_device_change(seed.DIRECTOR, case_id=c["case_id"], plan_id=pid,
                                          change_id=evt.data["change_id"], evidence_id="EV",
                                          equivalence_note="试", basis="requalify")
        self.assertEqual(cm.exception.code, "FLAG_NOT_OPEN")


# --------------------------------------------------------------------------- #
# SAE 批次阻断
# --------------------------------------------------------------------------- #
class SaeTest(ServiceTestBase):
    def test_sae_quarantines_batch_and_blocks_all_active_cases(self):
        a = self.fresh_case(case_id="C-A")
        # 第二台同批设备与第二个病例
        self.svc.register_device(seed.ENGINEER, {
            "device_id": "D-1002", "batch_id": "B-NS-2025-0301",
            "firmware_version": "fw-3.1.0", "algorithm_version": "alg-2.4.0",
            "calibration_id": "CAL-2025-0302-A"})
        for cid, ec, ic in [("C-B", "EC-B", "IC-B")]:
            self.svc.grant_ethics(seed.ETHICS, {
                "approval_no": ec, "hospital_id": "H-001", "standard_code": seed.STD_CODE,
                "standard_version": "2025.1", "valid_from": "2025-03-10", "valid_until": "2026-12-31",
                "scope": {"indications": [M.INDICATION_STROKE_REHAB]}})
            self.svc.open_case(seed.PHYSICIAN, {"case_id": cid, "hospital_id": "H-001",
                                                "patient_ref": "PB", "indication": M.INDICATION_STROKE_REHAB})
            self.svc.grant_consent(seed.PHYSICIAN, {"consent_id": ic, "case_id": cid,
                                                    "template_version": "IC-NI-2025.1", "signed_at": "2025-03-11"})
            self.svc.freeze_plan(seed.PHYSICIAN, case_id=cid, ethics_approval_no=ec, consent_id=ic,
                                 device_id="D-1002", operator_id="op-zhao", ward_id="W-001",
                                 setting="inpatient", observation_protocol_id="OBS-P-NI-1.0")
        self.svc.report_sae(seed.NURSE, case_id="C-A", device_id="D-1001",
                            description="治疗后新发持续震颤")
        self.assertEqual(self.svc.reg.batches["B-NS-2025-0301"]["status"], M.BATCH_QUARANTINED)
        for cid in ("C-A", "C-B"):
            self.assertEqual(self.svc.reg.cases[cid]["status"], M.CASE_SUSPENDED)
            self.assertIn("BATCH_SAE_HOLD", self.failed_codes(cid))
        # SAE 硬阻断不得人工背书
        with self.assertRaises(M.DomainError) as cm:
            self.svc.add_override(seed.DIRECTOR,
                                  plan_id=self.svc.reg.cases["C-A"]["plan_id"],
                                  rule_code="BATCH_SAE_HOLD", rationale="急用",
                                  expires_at="2026-12-31")
        self.assertEqual(cm.exception.code, "OVERRIDE_FORBIDDEN")
        # 批次解除后病例仍需逐例人工恢复
        self.svc.release_batch(seed.DIRECTOR, "B-NS-2025-0301", "INV-09",
                               "调查结论：设备无关，系个体反应")
        self.assertEqual(self.verdict("C-A"), M.VERDICT_NO_GO)  # 标记仍在
        self.svc.resume_case(seed.DIRECTOR, "C-A", "batch_sae", note="逐例评估可恢复")
        self.assertEqual(self.verdict("C-A"), M.VERDICT_GO)


# --------------------------------------------------------------------------- #
# 操作者资质 & 临床观察
# --------------------------------------------------------------------------- #
class OperatorObservationTest(ServiceTestBase):
    def test_expired_operator_credential_blocks(self):
        c = self.fresh_case()
        self.svc.register_operator(seed.ADMIN, {
            "operator_id": "op-new", "credential_id": "CR-NEW", "valid_from": "2024-01-01",
            "valid_until": "2025-01-01",  # 已过期
            "scope": {"indications": list(M.INDICATIONS), "device_models": ["NeuroSync-Cap-X2"]}})
        self.assertIn("OPERATOR_CREDENTIAL_EXPIRED",
                      self._codes_with_operator(c["case_id"], "op-new"))

    def _codes_with_operator(self, case_id, operator_id):
        d = self.svc.evaluate_clearance(case_id=case_id, operator_id=operator_id,
                                        actor=seed.PHYSICIAN, persist=False)
        return [r["code"] for r in d["rules"] if not r["passed"]]

    def test_observation_missing_hold_then_override(self):
        c = self.fresh_case()
        pid = self.svc.reg.cases[c["case_id"]]["plan_id"]
        self.svc.log_bedside_operation(seed.NURSE, {
            "plan_id": pid, "action": "session_end", "session_seq": 1})
        d = self.svc.evaluate_clearance(case_id=c["case_id"], actor=seed.PHYSICIAN,
                                        persist=False)
        self.assertEqual(d["verdict"], M.VERDICT_HOLD)
        self.assertIn("OBS_PRIOR_MISSING", [r["code"] for r in d["rules"] if not r["passed"]])
        self.svc.add_override(seed.DIRECTOR, plan_id=pid, rule_code="OBS_PRIOR_MISSING",
                              rationale="观察单纸质已签，24h内补录", expires_at="2025-06-03")
        d2 = self.svc.evaluate_clearance(case_id=c["case_id"], actor=seed.PHYSICIAN,
                                         persist=False)
        self.assertEqual(d2["verdict"], M.VERDICT_GO_OVERRIDE)
        # 补录观察后无需背书即为普通放行（此处保留背书也标注 go_with_override；补录本身可核）
        self.svc.record_observation(seed.NURSE, plan_id=pid, session_seq=1,
                                    findings="治疗后无异常", normal=True)
        d3 = self.svc.evaluate_clearance(case_id=c["case_id"], actor=seed.PHYSICIAN,
                                         persist=False)
        self.assertIn(d3["verdict"], (M.VERDICT_GO, M.VERDICT_GO_OVERRIDE))


# --------------------------------------------------------------------------- #
# 权限与最小可见
# --------------------------------------------------------------------------- #
class AccessTest(ServiceTestBase):
    def test_engineer_sees_tech_but_not_clinical_and_vendor_scoped(self):
        c = self.fresh_case()
        d = self.svc.evaluate_clearance(case_id=c["case_id"], actor=seed.ENGINEER,
                                        persist=False)
        v = access.redact_decision(d, seed.ENGINEER)
        self.assertEqual(v["evidence_bundle"]["device"]["firmware_version"], "fw-3.1.0")
        self.assertEqual(v["frozen_plan"]["indication"], "[redacted:clinical]")
        self.assertNotIn("放行", str(v.get("verdict_label", "")))
        # 其他厂商工程师被行级拒绝
        other = {"id": "eng-x", "role": M.ROLE_ENGINEER, "vendor": "RivalCorp"}
        pid = self.svc.reg.cases[c["case_id"]]["plan_id"]
        with self.assertRaises(access.AccessDenied):
            access.assert_case_scope(other, self.svc.reg.cases[c["case_id"]],
                                     self.svc.reg.plans[pid], self.svc.reg)

    def test_consultant_requires_case_grant(self):
        c = self.fresh_case()
        pid = self.svc.reg.cases[c["case_id"]]["plan_id"]
        with self.assertRaises(access.AccessDenied):
            access.assert_case_scope(seed.CONSULTANT, self.svc.reg.cases[c["case_id"]],
                                     self.svc.reg.plans[pid], self.svc.reg)
        self.svc.grant_access(seed.ADMIN, {"subject_id": seed.CONSULTANT["id"],
                                           "role": "consultant", "case_id": c["case_id"],
                                           "scopes": ["clinical"], "granted_by": "admin"})
        access.assert_case_scope(seed.CONSULTANT, self.svc.reg.cases[c["case_id"]],
                                 self.svc.reg.plans[pid], self.svc.reg)  # 不抛异常
        view = access.redact_timeline(self.svc.case_timeline(c["case_id"]), seed.CONSULTANT)
        # 只见临床，不见身份与设备技术细节
        self.assertEqual(view["case"]["patient_ref"], "[redacted:patient_identity]")

    def test_engineer_cannot_register_other_vendor_device(self):
        other = {"id": "eng-x", "role": M.ROLE_ENGINEER, "vendor": "RivalCorp"}
        with self.assertRaises(M.DomainError) as cm:
            self.svc.register_device(other, {
                "device_id": "D-9999", "batch_id": "B-NS-2025-0301",
                "firmware_version": "fw", "algorithm_version": "alg",
                "calibration_id": "CAL"})
        self.assertEqual(cm.exception.code, "ACCESS_DENIED")


# --------------------------------------------------------------------------- #
# 床旁断网日志
# --------------------------------------------------------------------------- #
class BedsideTest(ServiceTestBase):
    def test_offline_logs_survive_and_flush_idempotent(self):
        c = self.fresh_case()
        pid = self.svc.reg.cases[c["case_id"]]["plan_id"]
        spool_dir = os.path.join(self.tmp, "spool")
        spool = BedsideSpool(spool_dir, bedside_id="BS-01")
        spool.record("bedside.operation",
                     {"plan_id": pid, "action": "session_start", "session_seq": 1},
                     actor="nurse-sun")
        spool.record("bedside.operation",
                     {"plan_id": pid, "action": "abnormal_handled",
                      "abnormal_code": "EEG_ARTIFACT", "handling": "暂停刺激并复测"},
                     actor="nurse-sun")
        spool.record("bedside.operation",
                     {"plan_id": pid, "action": "session_end", "session_seq": 1},
                     actor="nurse-sun")
        self.assertTrue(spool.verify_local_chain()["ok"])
        self.assertEqual(len(spool.pending()), 3)

        # 模拟“补传后进程崩溃前已删除部分文件”——重新 flush 剩余，不重复
        first = spool.flush(self.svc.ingest_bedside)
        self.assertEqual(first, {"flushed": 3, "remaining": 0})
        second = spool.flush(self.svc.ingest_bedside)
        self.assertEqual(second, {"flushed": 0, "remaining": 0})
        self.assertEqual(self.svc.reg.sessions[pid], [1])

        # 重建缓冲对象（终端重启），本地链仍可校验
        spool2 = BedsideSpool(spool_dir, bedside_id="BS-01")
        self.assertTrue(spool2.verify_local_chain()["ok"])

    def test_ingest_whitelist_rejects_privilege_escalation(self):
        evil = {"event_type": "batch.released", "data": {"batch_id": "B-NS-2025-0301"}}
        with self.assertRaises(M.DomainError) as cm:
            self.svc.ingest_bedside(evil)
        self.assertEqual(cm.exception.code, "INGEST_FORBIDDEN")


# --------------------------------------------------------------------------- #
# 审计还原
# --------------------------------------------------------------------------- #
class AuditTest(ServiceTestBase):
    def _advancing_service(self):
        state = {"t": "2025-06-01T08:00:00Z"}
        def clock():
            cur = state["t"]; H = int(cur[11:13]); MM = int(cur[14:16]) + 10
            if MM >= 60: H += 1; MM -= 60
            state["t"] = f"2025-06-01T{H:02d}:{MM:02d}:00Z"
            return cur
        store = EventStore(os.path.join(self.tmp, "log2.jsonl"))
        return AdmissionService(store, clock=clock), store

    def test_as_of_replay_reconstructs_then_state(self):
        svc, store = self._advancing_service()
        seed.seed(svc)
        c = seed.seed_case(svc)
        frozen_evt = next(e for e in store.events() if e.event_type == "plan.frozen")
        svc.report_device_change(seed.ENGINEER, device_id="D-1001",
                                 change_type=M.CHANGE_FIRMWARE, old="fw-3.1.0", new="fw-3.2.0")
        before = svc.as_of_view(frozen_evt.ts, c["case_id"])
        self.assertEqual(before["verdict"], M.VERDICT_GO)
        self.assertEqual(before["evidence_bundle"]["device"]["firmware_version"], "fw-3.1.0")
        after = svc.as_of_view("2025-06-02T00:00:00Z", c["case_id"])
        self.assertEqual(after["verdict"], M.VERDICT_NO_GO)
        self.assertEqual(after["evidence_bundle"]["device"]["firmware_version"], "fw-3.2.0")

    def test_timeline_contains_snapshot_decisions_and_manual_actions(self):
        c = self.fresh_case()
        pid = self.svc.reg.cases[c["case_id"]]["plan_id"]
        self.svc.evaluate_clearance(case_id=c["case_id"], actor=seed.PHYSICIAN)
        tl = self.svc.case_timeline(c["case_id"])
        self.assertTrue(tl["plan_snapshots"])
        self.assertTrue(tl["plan_snapshots"][0]["snapshot_hash"])
        self.assertTrue(tl["decisions"], "应保留放行决定")
        types = {e["event_type"] for e in tl["events"]}
        self.assertIn("plan.frozen", types)
        self.assertIn("clearance.decided", types)

    def test_28_hospitals_present(self):
        self.assertEqual(len(self.svc.reg.hospitals), 28)
        fixtures = json.loads(
            (Path(__file__).resolve().parents[1] / "fixtures" / "hospitals.json")
            .read_text(encoding="utf-8"))
        self.assertEqual(len(fixtures), 28)


if __name__ == "__main__":
    unittest.main()
