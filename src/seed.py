"""演示/测试种子：把去标识基础档案装入准入系统。

标准内容刻意只放与准入相关的字段；非侵入式产品的三类适应症使用范围在此冻结，
后续通过 issue_standard 发布 v2 演示“升级不追溯”。
"""
from __future__ import annotations

import json
from pathlib import Path

from src import models as M

ADMIN = {"id": "admin", "role": M.ROLE_ADMIN}
ETHICS = {"id": "ec-gd", "role": M.ROLE_ETHICS_COMMITTEE, "hospital_id": "H-001"}
PHYSICIAN = {"id": "dr-zhao", "role": M.ROLE_TREATING_PHYSICIAN, "hospital_id": "H-001"}
DIRECTOR = {"id": "dir-qian", "role": M.ROLE_MEDICAL_DIRECTOR, "hospital_id": "H-001"}
NURSE = {"id": "nurse-sun", "role": M.ROLE_BEDSIDE_NURSE, "hospital_id": "H-001"}
ENGINEER = {"id": "eng-li", "role": M.ROLE_ENGINEER, "vendor": "NeuroSync"}
CONSULTANT = {"id": "dr-fang-consult", "role": M.ROLE_CONSULTANT}

STD_CODE = "T/BMIA-001"


def load_hospitals() -> list[dict]:
    path = Path(__file__).resolve().parents[1] / "fixtures" / "hospitals.json"
    return json.loads(path.read_text(encoding="utf-8"))


def seed(service) -> dict:
    """装入基础档案，返回常用 id，便于演示与测试。"""
    for h in load_hospitals():
        service.register_hospital(ADMIN, {
            "hospital_id": h["hospital_id"], "name": h["name"], "city": h["city"]})

    # 团体标准 v1：非侵入式产品三类适应症的使用范围冻结
    service.issue_standard(ADMIN, {
        "code": STD_CODE, "version": "2025.1",
        "title": "脑机接口病房设置与非侵入式设备临床使用管理规范",
        "effective_at": "2025-03-01",
        "consent_template_version": "IC-NI-2025.1",
        "content_hash": "std2025-1-content-sha256",
        "scope": {
            M.INDICATION_HEMIPLEGIA: {"settings": ["inpatient", "outpatient"],
                                      "max_plan_sessions": 30},
            M.INDICATION_EPILEPSY: {"settings": ["inpatient"], "max_plan_sessions": 20},
            M.INDICATION_STROKE_REHAB: {"settings": ["inpatient", "outpatient"],
                                        "max_plan_sessions": 40},
        }})

    # 病房条件按 v1 认证（演示 H-001）
    service.register_ward(ADMIN, {"ward_id": "W-001", "hospital_id": "H-001",
                                  "name": "脑机康复一病区"})
    service.certify_ward(ADMIN, {"ward_id": "W-001", "standard_version": "2025.1",
                                 "conditions_hash": "ward-w001-cond-2025-1"})

    # 非侵入式设备批次与设备
    service.register_batch(ADMIN, {
        "batch_id": "B-NS-2025-0301", "model": "NeuroSync-Cap-X2",
        "vendor": "NeuroSync", "device_class": "noninvasive",
        "registered_at": "2025-03-02"})
    service.register_device(ENGINEER, {
        "device_id": "D-1001", "batch_id": "B-NS-2025-0301",
        "firmware_version": "fw-3.1.0", "algorithm_version": "alg-2.4.0",
        "calibration_id": "CAL-2025-0302-A"})

    # 操作者资质
    service.register_operator(ADMIN, {
        "operator_id": "op-zhao", "credential_id": "CRED-BCI-0881",
        "name": "赵医师", "valid_from": "2025-01-01", "valid_until": "2027-01-01",
        "scope": {"indications": list(M.INDICATIONS),
                  "device_models": ["NeuroSync-Cap-X2"]}})

    # 变更审查分级策略（固件=暂停核验；校准=暂停核验；算法=重新审批）
    service.register_change_policy(ADMIN, "NeuroSync-Cap-X2", M.CHANGE_FIRMWARE, "requalify")
    service.register_change_policy(ADMIN, "NeuroSync-Cap-X2", M.CHANGE_CALIBRATION, "requalify")
    service.register_change_policy(ADMIN, "NeuroSync-Cap-X2", M.CHANGE_ALGORITHM, "reapprove")

    return {"standard_code": STD_CODE, "ward_id": "W-001",
            "batch_id": "B-NS-2025-0301", "device_id": "D-1001",
            "operator_id": "op-zhao"}


def seed_case(service, *, case_id="C-0001", indication=M.INDICATION_STROKE_REHAB,
              setting="inpatient", ethics_no="EC-H001-2025-017",
              consent_id="IC-C-0001-01") -> dict:
    """建立一个已开伦理批件、已授权、可冻结计划的演示病例。"""
    service.grant_ethics(ETHICS, {
        "approval_no": ethics_no, "hospital_id": "H-001",
        "standard_code": STD_CODE, "standard_version": "2025.1",
        "valid_from": "2025-03-10", "valid_until": "2026-12-31",
        "scope": {"indications": [indication]},
        "study_title": "非侵入式脑机接口脑卒中康复辅助治疗"})
    evt, case_id = service.open_case(PHYSICIAN, {
        "case_id": case_id, "hospital_id": "H-001",
        "patient_ref": "P-REDACTED-0001", "indication": indication})
    service.grant_consent(PHYSICIAN, {
        "consent_id": consent_id, "case_id": case_id,
        "template_version": "IC-NI-2025.1", "signed_at": "2025-03-11"})
    service.freeze_plan(PHYSICIAN, case_id=case_id, ethics_approval_no=ethics_no,
                        consent_id=consent_id, device_id="D-1001", operator_id="op-zhao",
                        ward_id="W-001", setting=setting,
                        observation_protocol_id="OBS-P-NI-1.0")
    return {"case_id": case_id, "ethics_no": ethics_no, "consent_id": consent_id}
