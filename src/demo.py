"""端到端演示：一次治疗计划从冻结到放行，经历标准升级、设备变更、SAE、断网补传与审计还原。

运行：python3 -m src.demo
"""
from __future__ import annotations

import os
import tempfile

from src import models as M, seed
from src.bedside import BedsideSpool
from src.eventstore import EventStore
from src.service import AdmissionService


def line(title: str) -> None:
    print("\n" + "=" * 72 + f"\n{title}\n" + "-" * 72)


def main() -> None:
    workdir = tempfile.mkdtemp(prefix="bci_admission_")
    svc = AdmissionService(EventStore(os.path.join(workdir, "ledger.jsonl")),
                           clock=lambda: "2025-06-01T09:00:00Z")
    seed.seed(svc)
    case = seed.seed_case(svc)
    cid = case["case_id"]

    line("1. 治疗前放行：六类要素按冻结快照核对")
    d = svc.evaluate_clearance(case_id=cid, actor=seed.PHYSICIAN)
    print("\n".join(d["explanation"]))

    line("2. 团体标准升级到 2026.1（放宽了癫痫门诊）——旧病例不被追溯改变")
    svc.issue_standard(seed.ADMIN, {
        "code": seed.STD_CODE, "version": "2026.1", "effective_at": "2026-01-01",
        "title": "新版规范", "consent_template_version": "IC-NI-2026.1",
        "content_hash": "std2026-1",
        "scope": {M.INDICATION_HEMIPLEGIA: {"settings": ["inpatient", "outpatient"], "max_plan_sessions": 30},
                  M.INDICATION_EPILEPSY: {"settings": ["inpatient", "outpatient"], "max_plan_sessions": 20},
                  M.INDICATION_STROKE_REHAB: {"settings": ["inpatient", "outpatient"], "max_plan_sessions": 40}}})
    d = svc.evaluate_clearance(case_id=cid, actor=seed.PHYSICIAN, persist=False)
    print("结论：", d["verdict_label"], "| 仍按冻结版本：", d["frozen_plan"]["standard_version"])

    line("3. 设备固件升级：系统自动定位受影响病例并暂停")
    evt, affected, review = svc.report_device_change(
        seed.ENGINEER, device_id="D-1001", change_type=M.CHANGE_FIRMWARE,
        old="fw-3.1.0", new="fw-3.2.0", reason="厂家例行安全补丁")
    print(f"变更单 {evt.data['change_id']}，分级：{review}，自动暂停病例：{affected}")
    print("放行：", svc.evaluate_clearance(case_id=cid, actor=seed.PHYSICIAN,
                                    persist=False)["verdict_label"])
    svc.requalify_device(seed.ENGINEER, "D-1001", evidence_id="EV-REG-77")
    svc.accept_device_change(seed.DIRECTOR, case_id=cid,
                             plan_id=svc.reg.cases[cid]["plan_id"],
                             change_id=evt.data["change_id"], evidence_id="EV-REG-77",
                             equivalence_note="回归测试与本院抽检判定治疗输出等价")
    print("核验合格并登记等价性接受后：",
          svc.evaluate_clearance(case_id=cid, actor=seed.PHYSICIAN,
                          persist=False)["verdict_label"],
          "（冻结快照固件仍为", svc.reg.plans[svc.reg.cases[cid]["plan_id"]]["firmware_version"], "）")

    line("4. 床旁网络中断：操作与异常处置本地落盘，恢复后补传")
    spool = BedsideSpool(os.path.join(workdir, "bedside-BS01"), bedside_id="BS-W001-07")
    pid = svc.reg.cases[cid]["plan_id"]
    spool.record("bedside.operation",
                 {"plan_id": pid, "action": "session_start", "session_seq": 1}, actor="nurse-sun")
    spool.record("bedside.operation",
                 {"plan_id": pid, "action": "abnormal_handled",
                  "abnormal_code": "EEG_ARTIFACT", "handling": "暂停刺激、检查阻抗后复测正常"},
                 actor="nurse-sun")
    spool.record("observation.recorded",
                 {"plan_id": pid, "session_seq": 1, "findings": "治疗中EEG平稳，无不适",
                  "normal": True}, actor="nurse-sun")
    spool.record("bedside.operation",
                 {"plan_id": pid, "action": "session_end", "session_seq": 1}, actor="nurse-sun")
    print("断网期间本地待补传：", len(spool.pending()), "条；本地哈希链：", spool.verify_local_chain())
    print("重连补传：", spool.flush(svc.ingest_bedside))

    line("5. 严重不良事件：系统自动隔离同批设备并阻断全部同批在治病例")
    svc.report_sae(seed.NURSE, case_id=cid, device_id="D-1001",
                   description="治疗结束后患者出现新发持续震颤，按SAE上报")
    d = svc.evaluate_clearance(case_id=cid, actor=seed.PHYSICIAN, persist=False)
    print("批次状态：", svc.reg.batches["B-NS-2025-0301"]["status"])
    print("放行：", d["verdict_label"], "| 硬性阻断：",
          [r["code"] for r in d["rules"] if not r["passed"]])

    line("6. 审计还原：当时的规范、证据、人工决定与设备状态")
    tl = svc.case_timeline(cid)
    print("计划快照：", [(p["plan_id"], p["standard_version"], p["firmware_version"])
                        for p in tl["plan_snapshots"]])
    print("人工决定：", [(x.get("category"), x.get("by", x.get("approver")))
                        for x in tl["case"].get("resolutions", [])])
    print("事件链：", svc.store.verify_chain())
    print("数据目录：", workdir)


if __name__ == "__main__":
    main()
