"""放行评估：治疗前输出可解释的准入结论，并留存证据快照。

七道闸门：计划状态、团体标准条件、伦理批件、患者授权、
设备批次、操作者资质、临床观察。每道闸门都给出通过与否、
理由和证据引用，结论与快照一并入库供事后还原。
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime

from src.admission.models import (
    OPERATOR_LEVELS,
    WARD_LEVELS,
    BatchStatus,
    CheckResult,
    ClearanceDecision,
    ConsentStatus,
    EthicsStatus,
    PlanStatus,
    level_rank,
)
from src.admission.standards import get_standard
from src.admission.store import Store


def _batch_versions(batch) -> dict:
    return {
        "firmware_version": batch.firmware_version,
        "calibration_version": batch.calibration_version,
        "algorithm_version": batch.algorithm_version,
    }


def evaluate(
    store: Store, plan_id: str, now: datetime, decided_by: str = "system"
) -> ClearanceDecision:
    plan = store.get("plans", plan_id)
    case = store.get("cases", plan.case_ref)
    institution = store.get("institutions", plan.institution_id)
    standard = get_standard(store, plan.standard_id, plan.standard_version)
    ethics = store.get("ethics", plan.ethics_approval_id)
    consent = store.get("consents", plan.consent_id)
    batch = store.get("batches", plan.batch_id)
    operator = store.get("operators", plan.operator_id)
    observations = store.find("observations", plan_id=plan_id)
    today = now.date()

    checks: list[CheckResult] = []

    # 闸门一：计划状态
    ok = plan.status == PlanStatus.ACTIVE
    checks.append(
        CheckResult(
            "计划状态",
            ok,
            "计划进行中"
            if ok
            else f"计划处于{plan.status}（{plan.status_reason or '未注明原因'}），须恢复后方可放行",
            [plan.plan_id],
        )
    )

    # 闸门二：团体标准条件（按计划冻结的版本评估）
    failures: list[str] = []
    if level_rank(WARD_LEVELS, institution.ward_level) < level_rank(
        WARD_LEVELS, standard.min_ward_level
    ):
        failures.append(
            f"病房等级{institution.ward_level}低于标准要求的{standard.min_ward_level}"
        )
    if standard.emergency_kit_required and not institution.emergency_kit:
        failures.append("病房未按标准配置急救设备")
    if plan.indication not in standard.allowed_indications:
        failures.append(f"{plan.indication}不在标准允许的治疗范围内")
    if not standard.age_min <= case.age <= standard.age_max:
        failures.append(
            f"患者年龄{case.age}岁超出标准范围{standard.age_min}-{standard.age_max}岁"
        )
    excluded = sorted(set(case.conditions) & set(standard.excluded_conditions))
    if excluded:
        failures.append("存在标准排除情形：" + "、".join(excluded))
    checks.append(
        CheckResult(
            "团体标准条件",
            not failures,
            "；".join(failures)
            if failures
            else f"符合 {plan.standard_id} {plan.standard_version}（计划冻结版本）的病房与患者条件",
            [f"{plan.standard_id}@{plan.standard_version}"],
        )
    )

    # 闸门三：伦理批件
    failures = []
    if ethics.status != EthicsStatus.VALID:
        failures.append(f"伦理批件状态为{ethics.status}")
    if ethics.institution_id != plan.institution_id:
        failures.append("伦理批件不属于本机构")
    if plan.indication not in ethics.indications:
        failures.append("伦理批件未覆盖该治疗范围")
    if batch.model not in ethics.device_models:
        failures.append("伦理批件未覆盖该设备型号")
    if not (
        date.fromisoformat(ethics.valid_from)
        <= today
        <= date.fromisoformat(ethics.valid_to)
    ):
        failures.append(f"伦理批件不在有效期{ethics.valid_from}至{ethics.valid_to}内")
    checks.append(
        CheckResult(
            "伦理批件",
            not failures,
            "；".join(failures) if failures else f"批件 {ethics.protocol_no} 有效",
            [ethics.approval_id],
        )
    )

    # 闸门四：患者授权（设备版本变化后须完成补充告知）
    failures = []
    if consent.status != ConsentStatus.VALID:
        failures.append(f"授权状态为{consent.status}")
    if "治疗" not in consent.scope:
        failures.append("授权范围不含治疗")
    if consent.device_snapshot != _batch_versions(batch):
        failures.append("设备版本较签署时已变化，需完成补充告知")
    checks.append(
        CheckResult(
            "患者授权",
            not failures,
            "；".join(failures) if failures else "授权有效且覆盖当前设备版本",
            [consent.consent_id],
        )
    )

    # 闸门五：设备批次
    ok = batch.status == BatchStatus.ACTIVE
    checks.append(
        CheckResult(
            "设备批次",
            ok,
            f"批次 {batch.batch_id} 在用（固件{batch.firmware_version} / "
            f"校准{batch.calibration_version} / 算法{batch.algorithm_version}）"
            if ok
            else f"批次 {batch.batch_id} 已{batch.status}，禁止使用",
            [batch.batch_id],
        )
    )

    # 闸门六：操作者资质
    failures = []
    matched = [
        q
        for q in operator.qualifications
        if q["device_model"] == batch.model and q["indication"] == plan.indication
    ]
    if not matched:
        failures.append("操作者不具备该设备型号与治疗范围的资质")
    else:
        qual = matched[0]
        if date.fromisoformat(qual["valid_to"]) < today:
            failures.append("操作者资质已过期")
        if level_rank(OPERATOR_LEVELS, qual["level"]) < level_rank(
            OPERATOR_LEVELS, standard.min_operator_level
        ):
            failures.append(
                f"操作者等级{qual['level']}低于标准要求的{standard.min_operator_level}"
            )
    checks.append(
        CheckResult(
            "操作者资质",
            not failures,
            "；".join(failures) if failures else f"{operator.name} 资质有效",
            [operator.operator_id],
        )
    )

    # 闸门七：临床观察
    baseline = [o for o in observations if o.kind == "基线评估"]
    checks.append(
        CheckResult(
            "临床观察",
            bool(baseline),
            "已完成基线评估" if baseline else "缺少基线评估记录",
            [o.obs_id for o in baseline],
        )
    )

    allowed = all(check.passed for check in checks)
    if allowed:
        conclusion = "放行：七项准入条件全部满足"
    else:
        failed = "；".join(f"{c.gate}不满足" for c in checks if not c.passed)
        conclusion = f"不予放行：{failed}"

    snapshot = {
        "standard": asdict(standard),
        "institution": asdict(institution),
        "ethics": asdict(ethics),
        "consent": {
            "consent_id": consent.consent_id,
            "status": consent.status,
            "scope": consent.scope,
            "device_snapshot": consent.device_snapshot,
        },
        "batch": asdict(batch),
        "operator": asdict(operator),
        "case": {
            "case_ref": case.case_ref,
            "age": case.age,
            "conditions": case.conditions,
        },
        "observations": [asdict(o) for o in observations],
        "plan_status": plan.status,
    }

    decision = ClearanceDecision(
        decision_id=store.next_id("DEC", 6),
        plan_id=plan_id,
        allowed=allowed,
        checks=[asdict(c) for c in checks],
        conclusion=conclusion,
        snapshot=snapshot,
        decided_by=decided_by,
        decided_at=now.isoformat(),
    )
    store.put("decisions", decision.decision_id, decision)
    store.log(
        decided_by,
        "clearance_evaluated",
        {
            "plan_id": plan_id,
            "decision_id": decision.decision_id,
            "allowed": allowed,
        },
        decision.decided_at,
    )
    return decision
