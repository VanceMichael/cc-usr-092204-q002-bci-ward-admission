"""角色权限：多科室会诊与企业工程师只能处理职责内信息。

- 医护人员：本机构病例与计划的完整视图；
- 会诊专家：仅被标记会诊的计划，且只见临床摘要字段；
- 企业工程师：仅本厂设备批次与计划的设备字段，不见任何患者信息；
- 管理方/审计：全量只读。
"""
from __future__ import annotations

ROLE_CLINICIAN = "clinician"
ROLE_CONSULTANT = "consultant"
ROLE_ENGINEER = "engineer"
ROLE_AUDITOR = "auditor"
ROLES = (ROLE_CLINICIAN, ROLE_CONSULTANT, ROLE_ENGINEER, ROLE_AUDITOR)

# None 表示全部字段；空列表表示该角色不可见此资源。
PLAN_FIELDS = {
    ROLE_CLINICIAN: None,
    ROLE_AUDITOR: None,
    ROLE_CONSULTANT: [
        "plan_id",
        "case_ref",
        "indication",
        "status",
        "standard_id",
        "standard_version",
    ],
    ROLE_ENGINEER: ["plan_id", "batch_id", "device_snapshot", "status"],
}

CASE_FIELDS = {
    ROLE_CLINICIAN: None,
    ROLE_AUDITOR: None,
    ROLE_CONSULTANT: ["case_ref", "age", "sex"],
    ROLE_ENGINEER: [],
}

BATCH_FIELDS = {
    ROLE_CLINICIAN: ["batch_id", "model", "status"],
    ROLE_AUDITOR: None,
    ROLE_CONSULTANT: [],
    ROLE_ENGINEER: None,
}


def _require_role(role: str) -> None:
    if role not in ROLES:
        raise PermissionError(f"未知角色：{role}")


def _filter(fields: list[str] | None, record: dict) -> dict:
    if fields is None:
        return dict(record)
    return {key: record[key] for key in fields if key in record}


def plan_view(role: str, plan: dict, ctx: dict) -> dict | None:
    """返回该角色可见的计划视图；无权查看时返回 None。"""
    _require_role(role)
    if role == ROLE_CLINICIAN and plan["institution_id"] != ctx.get("institution_id"):
        return None
    if role == ROLE_CONSULTANT and not plan.get("consult_flag"):
        return None
    return _filter(PLAN_FIELDS[role], plan)


def case_view(role: str, case: dict, ctx: dict) -> dict | None:
    _require_role(role)
    fields = CASE_FIELDS[role]
    if not fields and fields is not None:
        return None
    if role == ROLE_CLINICIAN and case["institution_id"] != ctx.get("institution_id"):
        return None
    return _filter(fields, case)


def batch_view(role: str, batch: dict, ctx: dict) -> dict | None:
    _require_role(role)
    fields = BATCH_FIELDS[role]
    if not fields and fields is not None:
        return None
    if role == ROLE_ENGINEER and batch["manufacturer"] != ctx.get("manufacturer"):
        return None
    return _filter(fields, batch)
