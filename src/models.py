"""脑机病房设备准入后台——领域模型。

只保存与领域语义直接相关的常量与结构，不包含流程逻辑。
所有时间在系统内部统一使用 ISO-8601 字符串（UTC 由调用方决定，默认本地时区日期）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# 适应症（非侵入式产品的冻结使用范围）
# ---------------------------------------------------------------------------
INDICATION_HEMIPLEGIA = "hemiplegia"          # 偏瘫
INDICATION_EPILEPSY = "epilepsy"              # 癫痫
INDICATION_STROKE_REHAB = "stroke_rehab"      # 脑卒中康复
INDICATIONS = (INDICATION_HEMIPLEGIA, INDICATION_EPILEPSY, INDICATION_STROKE_REHAB)

INDICATION_LABELS = {
    INDICATION_HEMIPLEGIA: "偏瘫",
    INDICATION_EPILEPSY: "癫痫",
    INDICATION_STROKE_REHAB: "脑卒中康复",
}

# ---------------------------------------------------------------------------
# 设备部件变更类型
# ---------------------------------------------------------------------------
CHANGE_FIRMWARE = "firmware"        # 固件
CHANGE_CALIBRATION = "calibration"  # 传感器校准
CHANGE_ALGORITHM = "algorithm"      # 算法版本
CHANGE_TYPES = (CHANGE_FIRMWARE, CHANGE_CALIBRATION, CHANGE_ALGORITHM)

# ---------------------------------------------------------------------------
# 病例/计划状态
# ---------------------------------------------------------------------------
CASE_DRAFT = "draft"
CASE_APPROVED = "approved"                    # 治疗计划已冻结、可执行
CASE_SUSPENDED = "suspended"                  # 暂停（设备变更核查 / SAE 批次停用）
CASE_SUPPLEMENT_NOTICE = "supplement_notice"  # 需补充告知与知情同意
CASE_REAPPROVAL = "reapproval_required"       # 需重新伦理审批
CASE_COMPLETED = "completed"
CASE_WITHDRAWN = "withdrawn"

ACTIVE_CASE_STATES = {CASE_APPROVED, CASE_SUSPENDED, CASE_SUPPLEMENT_NOTICE, CASE_REAPPROVAL}

# ---------------------------------------------------------------------------
# 批次/设备状态
# ---------------------------------------------------------------------------
BATCH_ACTIVE = "active"
BATCH_QUARANTINED = "quarantined"   # SAE 后立即隔离停用
BATCH_STOPPED = "stopped"           # 调查结论：永久停用

DEVICE_ACTIVE = "active"
DEVICE_HELD = "held"                # 变更后待核验
DEVICE_BLOCKED = "blocked"          # 所属批次隔离

# ---------------------------------------------------------------------------
# 放行结论
# ---------------------------------------------------------------------------
VERDICT_GO = "go"                         # 放行
VERDICT_GO_OVERRIDE = "go_with_override"  # 人工背书后附条件放行
VERDICT_HOLD = "hold"                     # 暂缓
VERDICT_NO_GO = "no_go"                   # 不予放行

VERDICT_LABELS = {
    VERDICT_GO: "放行",
    VERDICT_GO_OVERRIDE: "附条件放行（人工背书）",
    VERDICT_HOLD: "暂缓",
    VERDICT_NO_GO: "不予放行",
}

# 不可被人工背书覆盖的硬性阻断
HARD_BLOCK_RULES = {
    "STD_SCOPE",        # 适应症超出冻结标准范围
    "ETHICS_MISSING",   # 伦理批件缺失
    "ETHICS_REVOKED",   # 伦理批件被撤销
    "ETHICS_EXPIRED",   # 伦理批件过期
    "CONSENT_MISSING",  # 知情授权缺失
    "CONSENT_WITHDRAWN",
    "CONSENT_VERSION",  # 授权与冻结告知版本不一致且未补充告知
    "DEVICE_MISMATCH",  # 设备固件/算法与冻结快照不一致
    "BATCH_SAE_HOLD",   # 同批设备因严重不良事件停用
    "PLAN_REAPPROVAL",  # 计划已被标记需重新审批
    "OPERATOR_CREDENTIAL_MISSING",
    "OPERATOR_CREDENTIAL_SCOPE",
    "OPERATOR_CREDENTIAL_EXPIRED",
    "SETTING_MISMATCH", # 治疗场所与冻结的使用范围不一致
}

# 可由医疗负责人人工背书的暂缓项
OVERRIDABLE_RULES = {
    "OBS_PRIOR_MISSING",  # 上一次临床观察记录暂缺（可限期补录）
}

# ---------------------------------------------------------------------------
# 角色
# ---------------------------------------------------------------------------
ROLE_ADMIN = "admin"                    # 联合体管理方
ROLE_AUDITOR = "auditor"                # 审计
ROLE_TREATING_PHYSICIAN = "treating_physician"
ROLE_BEDSIDE_NURSE = "bedside_nurse"
ROLE_CONSULTANT = "consultant"          # 多科室会诊专家
ROLE_ETHICS_COMMITTEE = "ethics_committee"
ROLE_ENGINEER = "vendor_engineer"       # 企业工程师
ROLE_MEDICAL_DIRECTOR = "medical_director"

ALL_ROLES = {
    ROLE_ADMIN, ROLE_AUDITOR, ROLE_TREATING_PHYSICIAN, ROLE_BEDSIDE_NURSE,
    ROLE_CONSULTANT, ROLE_ETHICS_COMMITTEE, ROLE_ENGINEER, ROLE_MEDICAL_DIRECTOR,
}


@dataclass
class FrozenPlan:
    """治疗计划的冻结快照：创建时整体封存，标准升级不追溯改变其中任何条件。"""

    plan_id: str
    case_id: str
    hospital_id: str
    ward_id: str
    indication: str
    setting: str  # 冻结时标准允许的治疗场所，如 inpatient / outpatient
    standard_code: str
    standard_version: str
    standard_content_hash: str
    ethics_approval_no: str
    consent_id: str
    consent_template_version: str
    device_id: str
    batch_id: str
    firmware_version: str
    algorithm_version: str
    calibration_id: str
    operator_id: str
    credential_id: str
    observation_protocol_id: str
    frozen_at: str
    plan_version: int = 1
    supersedes: str | None = None
    snapshot_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}  # type: ignore[attr-defined]


@dataclass
class RuleResult:
    """单条准入规则的核对结果，是可解释放行结论的基本单元。"""

    code: str
    title: str
    passed: bool
    severity: str  # hard / hold / info
    detail: str
    evidence: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "title": self.title,
            "passed": self.passed,
            "severity": self.severity,
            "detail": self.detail,
            "evidence": self.evidence,
        }


class DomainError(ValueError):
    """领域规则拒绝（区别于程序异常），错误码可直接呈现给操作者。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message
