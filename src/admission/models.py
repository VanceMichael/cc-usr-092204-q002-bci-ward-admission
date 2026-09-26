"""领域模型：脑机病房设备准入控制的实体、状态常量与共享词汇。

所有实体字段均为可 JSON 序列化的基础类型（字符串/数值/布尔/列表/字典），
保证 dataclass 的 asdict 结果可以直接落盘与回放。
"""
from __future__ import annotations

from dataclasses import dataclass, field


class Indication:
    """非侵入式产品允许的治疗范围，逐例冻结到一次治疗计划。"""

    HEMIPLEGIA = "偏瘫"
    EPILEPSY = "癫痫"
    STROKE_REHAB = "脑卒中康复"
    ALL = (HEMIPLEGIA, EPILEPSY, STROKE_REHAB)


class StandardStatus:
    DRAFT = "编制中"
    PUBLISHED = "已发布"
    SUPERSEDED = "已替代"


class EthicsStatus:
    VALID = "有效"
    REVOKED = "已撤销"


class ConsentStatus:
    VALID = "有效"
    NEED_SUPPLEMENT = "需补充告知"
    WITHDRAWN = "已撤回"


class BatchStatus:
    ACTIVE = "在用"
    SUSPENDED = "暂停"
    BLOCKED = "阻断"


class PlanStatus:
    ACTIVE = "进行中"
    SUSPENDED = "已暂停"
    PENDING_SUPPLEMENT = "待补充告知"
    PENDING_REAPPROVAL = "待重新审批"
    COMPLETED = "已完成"
    TERMINATED = "已终止"


class Severity:
    GENERAL = "一般"
    SERIOUS = "严重"


WARD_LEVELS = ("二级", "三级")
OPERATOR_LEVELS = ("初级", "中级", "高级")


def level_rank(scale: tuple[str, ...], value: str) -> int:
    if value not in scale:
        raise ValueError(f"未知等级：{value}")
    return scale.index(value)


@dataclass
class StandardVersion:
    """团体标准的一个版本：病房条件、适用患者与异常处置要求。"""

    standard_id: str
    version: str
    status: str
    issued_at: str
    min_ward_level: str
    emergency_kit_required: bool
    allowed_indications: list[str]
    age_min: int
    age_max: int
    excluded_conditions: list[str]
    min_operator_level: str
    sae_report_hours: int
    supersedes: str = ""
    change_notes: str = ""


@dataclass
class Institution:
    institution_id: str
    name: str
    ward_level: str
    emergency_kit: bool


@dataclass
class EthicsApproval:
    """伦理批件：限定机构、治疗范围、设备型号与有效期。"""

    approval_id: str
    institution_id: str
    protocol_no: str
    indications: list[str]
    device_models: list[str]
    valid_from: str
    valid_to: str
    status: str


@dataclass
class CaseFile:
    """去标识病例资料。"""

    case_ref: str
    institution_id: str
    age: int
    sex: str
    conditions: list[str]


@dataclass
class Consent:
    """患者授权：记录签署时认可的设备版本快照。"""

    consent_id: str
    case_ref: str
    scope: list[str]
    device_snapshot: dict
    signed_at: str
    status: str
    supplements: list[dict] = field(default_factory=list)


@dataclass
class DeviceBatch:
    """设备批次：固件、传感器校准与算法三个版本部件。"""

    batch_id: str
    model: str
    manufacturer: str
    firmware_version: str
    calibration_version: str
    algorithm_version: str
    status: str
    history: list[dict] = field(default_factory=list)


@dataclass
class Operator:
    """操作者及其资质条目（设备型号 × 治疗范围 × 等级 × 有效期）。"""

    operator_id: str
    name: str
    institution_id: str
    qualifications: list[dict]


@dataclass
class TreatmentPlan:
    """一次治疗计划：创建时冻结范围、标准版本与设备版本快照。"""

    plan_id: str
    case_ref: str
    institution_id: str
    indication: str
    standard_id: str
    standard_version: str
    ethics_approval_id: str
    consent_id: str
    batch_id: str
    operator_id: str
    device_snapshot: dict
    status: str
    created_at: str
    consult_flag: bool = False
    status_reason: str = ""


@dataclass
class Observation:
    """临床观察记录：基线评估、治疗前检查、随访。"""

    obs_id: str
    plan_id: str
    kind: str
    recorded_by: str
    recorded_at: str
    summary: str


@dataclass
class AdverseEvent:
    event_id: str
    plan_id: str
    batch_id: str
    severity: str
    description: str
    reported_at: str


@dataclass
class CheckResult:
    """单道准入闸门的结论与证据。"""

    gate: str
    passed: bool
    detail: str
    evidence: list[str]


@dataclass
class ClearanceDecision:
    """放行结论：可解释、含快照、可事后还原。"""

    decision_id: str
    plan_id: str
    allowed: bool
    checks: list[dict]
    conclusion: str
    snapshot: dict
    decided_by: str
    decided_at: str
    manual: bool = False
