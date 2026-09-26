"""测试公共装置：固定时钟 + 一套完整的准入档案。"""
from datetime import datetime, timezone

from src.admission.service import AdmissionService

NOW = datetime(2026, 9, 26, 9, 0, tzinfo=timezone.utc)

STANDARD_V1 = {
    "standard_id": "T/BCI-WARD",
    "version": "1.0.0",
    "min_ward_level": "二级",
    "emergency_kit_required": True,
    "allowed_indications": ["偏瘫", "癫痫", "脑卒中康复"],
    "age_min": 18,
    "age_max": 75,
    "excluded_conditions": ["颅内金属植入", "严重心律失常"],
    "min_operator_level": "中级",
    "sae_report_hours": 24,
}

BATCH_VERSIONS = {
    "firmware_version": "F1.0",
    "calibration_version": "C1.0",
    "algorithm_version": "A1.0",
}


def build_service() -> AdmissionService:
    service = AdmissionService(clock=lambda: NOW)
    service.add_institution(
        institution_id="INST-A", name="示范医院甲", ward_level="三级", emergency_kit=True
    )
    service.add_institution(
        institution_id="INST-B", name="示范医院乙", ward_level="二级", emergency_kit=True
    )
    service.publish_standard(**STANDARD_V1)
    service.add_ethics_approval(
        approval_id="ETH-1",
        institution_id="INST-A",
        protocol_no="EC-2026-001",
        indications=["偏瘫", "癫痫", "脑卒中康复"],
        device_models=["BCI-X1"],
        valid_from="2026-01-01",
        valid_to="2026-12-31",
    )
    service.add_case(
        case_ref="CASE-1",
        institution_id="INST-A",
        age=60,
        sex="女",
        conditions=["高血压"],
    )
    service.add_batch(
        batch_id="BAT-1",
        model="BCI-X1",
        manufacturer="某医疗科技",
        **BATCH_VERSIONS,
    )
    service.add_consent(
        consent_id="CON-1",
        case_ref="CASE-1",
        scope=["治疗", "数据使用"],
        device_snapshot=dict(BATCH_VERSIONS),
    )
    service.add_operator(
        operator_id="OP-1",
        name="张某",
        institution_id="INST-A",
        qualifications=[
            {
                "device_model": "BCI-X1",
                "indication": "偏瘫",
                "level": "高级",
                "valid_to": "2027-01-01",
            }
        ],
    )
    return service


def build_plan(service: AdmissionService, **overrides) -> dict:
    params = {
        "case_ref": "CASE-1",
        "institution_id": "INST-A",
        "indication": "偏瘫",
        "ethics_approval_id": "ETH-1",
        "consent_id": "CON-1",
        "batch_id": "BAT-1",
        "operator_id": "OP-1",
        "consult_flag": True,
    }
    params.update(overrides)
    plan = service.create_plan(**params)
    service.add_observation(
        plan_id=plan["plan_id"],
        kind="基线评估",
        recorded_by="OP-1",
        summary="基线运动功能评分已记录",
    )
    return plan
