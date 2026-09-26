"""字段级访问控制：多科室会诊、企业工程师只能处理职责内信息。

实现方式：给敏感数据键打标签（身份/临床/知情同意/伦理/设备技术/设备状态），
角色只持有一组标签；对放行结论、病例时间线等视图递归裁剪，越权字段替换为
``[redacted:<tag>]``。企业工程师另有“本企业设备”的行级范围限制。
"""
from __future__ import annotations

from typing import Any

from . import models as M

TAG_PATIENT_IDENTITY = "patient_identity"
TAG_CLINICAL = "clinical"
TAG_CONSENT = "consent"
TAG_ETHICS = "ethics"
TAG_DEVICE_TECH = "device_tech"      # 固件/算法/校准细节（企业工程师口径）
TAG_DEVICE_STATUS = "device_status"  # 设备/批次是否可用（医护口径）
TAG_VERDICT = "verdict"
TAG_AUDIT = "audit"

# 键 -> 标签（递归遍历时命中即按标签裁剪）
TAG_BY_KEY: dict[str, str] = {
    "patient_ref": TAG_PATIENT_IDENTITY,
    "patient_name": TAG_PATIENT_IDENTITY,
    "indication": TAG_CLINICAL,
    "findings": TAG_CLINICAL,
    "metrics": TAG_CLINICAL,
    "normal": TAG_CLINICAL,
    "observations": TAG_CLINICAL,
    "observation_protocol_id": TAG_CLINICAL,
    "observed_sessions": TAG_CLINICAL,
    "missing_session_seq": TAG_CLINICAL,
    "consent": TAG_CONSENT,
    "consent_id": TAG_CONSENT,
    "consent_template_version": TAG_CONSENT,
    "template_version": TAG_CONSENT,
    "supplemented_versions": TAG_CONSENT,
    "ethics": TAG_ETHICS,
    "ethics_approval_no": TAG_ETHICS,
    "approval_no": TAG_ETHICS,
    "firmware_version": TAG_DEVICE_TECH,
    "algorithm_version": TAG_DEVICE_TECH,
    "calibration_id": TAG_DEVICE_TECH,
    "current": TAG_DEVICE_TECH,
    "frozen": TAG_DEVICE_TECH,
    "acceptance": TAG_DEVICE_TECH,
    "ledger_tail_hash": TAG_AUDIT,
    "snapshot_hash": TAG_AUDIT,
    "standard_content_hash": TAG_AUDIT,
    "content_hash": TAG_AUDIT,
}

# 设备状态键：医护可见、技术细节不可见
DEVICE_STATUS_KEYS = {"device_id", "batch_id", "status", "batch_status", "hold_reason"}

# 角色 -> 可见标签
ROLE_TAGS: dict[str, set[str]] = {
    M.ROLE_ADMIN: {TAG_PATIENT_IDENTITY, TAG_CLINICAL, TAG_CONSENT, TAG_ETHICS,
                   TAG_DEVICE_TECH, TAG_DEVICE_STATUS, TAG_VERDICT, TAG_AUDIT},
    M.ROLE_AUDITOR: {TAG_CLINICAL, TAG_CONSENT, TAG_ETHICS, TAG_DEVICE_TECH,
                     TAG_DEVICE_STATUS, TAG_VERDICT, TAG_AUDIT},
    M.ROLE_MEDICAL_DIRECTOR: {TAG_PATIENT_IDENTITY, TAG_CLINICAL, TAG_CONSENT, TAG_ETHICS,
                              TAG_DEVICE_STATUS, TAG_VERDICT},
    M.ROLE_TREATING_PHYSICIAN: {TAG_PATIENT_IDENTITY, TAG_CLINICAL, TAG_CONSENT, TAG_ETHICS,
                                TAG_DEVICE_STATUS, TAG_VERDICT},
    M.ROLE_BEDSIDE_NURSE: {TAG_PATIENT_IDENTITY, TAG_CLINICAL, TAG_DEVICE_STATUS, TAG_VERDICT},
    M.ROLE_CONSULTANT: {TAG_CLINICAL, TAG_VERDICT},
    M.ROLE_ETHICS_COMMITTEE: {TAG_CLINICAL, TAG_CONSENT, TAG_ETHICS, TAG_VERDICT},
    M.ROLE_ENGINEER: {TAG_DEVICE_TECH, TAG_DEVICE_STATUS},
}

# 这些规则的文字结论本身含设备技术细节；无设备技术标签时整体替换为通用表述
DEVICE_TECH_RULE_CODES = {
    "DEVICE_MISMATCH", "DEVICE_CHANGE_ACCEPTED", "DEVICE_FROZEN_MATCH",
    "DEVICE_REQUALIFY", "DEVICE_SUPPLEMENT", "DEVICE_REAPPROVAL",
}

REDACTED = "[redacted:{}]"


class AccessDenied(M.DomainError):
    def __init__(self, message: str):
        super().__init__("ACCESS_DENIED", message)


def tags_for(subject: dict[str, Any]) -> set[str]:
    return ROLE_TAGS.get(subject.get("role", ""), set())


def assert_case_scope(subject: dict[str, Any], case: dict[str, Any],
                      plan: dict[str, Any] | None, registry) -> None:
    """行级范围：会诊专家需有该病例的显式授权；企业工程师只能触及本企业批次设备。"""
    role = subject.get("role")
    if role in (M.ROLE_ADMIN, M.ROLE_AUDITOR, M.ROLE_MEDICAL_DIRECTOR):
        return
    if role == M.ROLE_ENGINEER:
        if not plan:
            raise AccessDenied("企业工程师只能查看与设备相关的视图")
        batch = registry.batches.get(plan["batch_id"])
        if not batch or batch.get("vendor") != subject.get("vendor"):
            raise AccessDenied("企业工程师只能处理本企业设备的信息")
        return
    if role in (M.ROLE_TREATING_PHYSICIAN, M.ROLE_BEDSIDE_NURSE, M.ROLE_ETHICS_COMMITTEE):
        if case.get("hospital_id") != subject.get("hospital_id"):
            raise AccessDenied("只能访问所在机构的病例")
        return
    if role == M.ROLE_CONSULTANT:
        grants = registry.active_grants(subject["id"], case["case_id"])
        if not grants:
            raise AccessDenied("会诊专家需经逐病例授权后方可查看")
        return
    raise AccessDenied(f"角色 {role} 无病例访问权限")


def _redact(value: Any, tags: set[str], *, parent_key: str = "") -> Any:
    if parent_key in TAG_BY_KEY:
        tag = TAG_BY_KEY[parent_key]
        if tag not in tags:
            return REDACTED.format(tag)
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k in TAG_BY_KEY and TAG_BY_KEY[k] not in tags:
                out[k] = REDACTED.format(TAG_BY_KEY[k])
            elif parent_key == "" and k == "device" and isinstance(v, dict):
                out[k] = _redact_device_bundle(v, tags)
            else:
                out[k] = _redact(v, tags, parent_key=k)
        return out
    if isinstance(value, list):
        return [_redact(v, tags, parent_key=parent_key) for v in value]
    return value


def _redact_device_bundle(bundle: dict[str, Any], tags: set[str]) -> dict[str, Any]:
    if TAG_DEVICE_TECH in tags:
        return bundle
    out = {}
    for k, v in bundle.items():
        if k in DEVICE_STATUS_KEYS:
            out[k] = v
        else:
            out[k] = REDACTED.format(TAG_DEVICE_TECH)
    return out


def redact_decision(decision: dict[str, Any], subject: dict[str, Any]) -> dict[str, Any]:
    """按角色裁剪放行结论：床旁治疗前看到的是职责内的可解释结论。"""
    tags = tags_for(subject)
    view = _redact(decision, tags)
    if TAG_DEVICE_TECH not in tags:
        view["rules"] = [
            ({**r, "detail": "设备技术状态以工程口径核验，本角色不展示版本细节",
              "evidence": []} if r["code"] in DEVICE_TECH_RULE_CODES else r)
            for r in view.get("rules", [])
        ]
        view["explanation"] = [
            line for line in view.get("explanation", [])
            if not any(code in line for code in DEVICE_TECH_RULE_CODES)
        ] or view.get("explanation", [])
    if TAG_VERDICT not in tags:
        for k in ("verdict", "verdict_label", "explanation", "rules"):
            view[k] = REDACTED.format(TAG_VERDICT)
    view["_viewer"] = {"id": subject.get("id"), "role": subject.get("role"),
                       "visible_tags": sorted(tags)}
    return view


def redact_timeline(timeline: dict[str, Any], subject: dict[str, Any]) -> dict[str, Any]:
    return _redact(timeline, tags_for(subject))
