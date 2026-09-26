"""设备版本变更影响分析。

固件、传感器校准或算法版本变化时，自动扫描该批次下未结案的治疗计划，
按部件确定处置动作：固件→补充告知，校准→暂停使用，算法→重新审批；
一次变更多个部件时取最重动作。
"""
from __future__ import annotations

from src.admission.models import (
    BatchStatus,
    ConsentStatus,
    PlanStatus,
)
from src.admission.store import Store

COMPONENT_ACTIONS = {
    "firmware_version": ("补充告知", PlanStatus.PENDING_SUPPLEMENT),
    "calibration_version": ("暂停使用", PlanStatus.SUSPENDED),
    "algorithm_version": ("重新审批", PlanStatus.PENDING_REAPPROVAL),
}

ACTION_SEVERITY = {"补充告知": 1, "暂停使用": 2, "重新审批": 3}

_OPEN_STATUSES = (
    PlanStatus.ACTIVE,
    PlanStatus.SUSPENDED,
    PlanStatus.PENDING_SUPPLEMENT,
    PlanStatus.PENDING_REAPPROVAL,
)


def register_version_change(
    store: Store,
    batch_id: str,
    changes: dict,
    reason: str,
    now: str,
    actor: str = "engineer",
) -> dict:
    """登记批次版本变更，返回受影响病例清单及各自处置动作。"""
    batch = store.get("batches", batch_id)
    unknown = sorted(set(changes) - set(COMPONENT_ACTIONS))
    if unknown:
        raise ValueError(f"未知版本部件：{'、'.join(unknown)}")

    record = {"at": now, "reason": reason, "changes": {}}
    for component, new_version in changes.items():
        old_version = getattr(batch, component)
        if old_version != new_version:
            record["changes"][component] = {"from": old_version, "to": new_version}
            setattr(batch, component, new_version)
    if not record["changes"]:
        return {"batch_id": batch_id, "impacts": [], "note": "版本未发生变化"}
    batch.history.append(record)

    impacts = []
    for plan in store.find("plans", batch_id=batch_id):
        if plan.status not in _OPEN_STATUSES:
            continue
        action, target_status = max(
            (COMPONENT_ACTIONS[c] for c in record["changes"]),
            key=lambda pair: ACTION_SEVERITY[pair[0]],
        )
        plan.status = target_status
        plan.status_reason = (
            f"批次{batch_id}版本变更（{record['changes']}），需{action}"
        )
        if "firmware_version" in record["changes"]:
            consent = store.get("consents", plan.consent_id)
            consent.status = ConsentStatus.NEED_SUPPLEMENT
        impacts.append(
            {"plan_id": plan.plan_id, "action": action, "reason": plan.status_reason}
        )

    store.log(
        actor,
        "batch_version_changed",
        {
            "batch_id": batch_id,
            "changes": record["changes"],
            "impacts": impacts,
            "plan_ids": [item["plan_id"] for item in impacts],
        },
        now,
    )
    return {"batch_id": batch_id, "impacts": impacts}


def resolve_supplement(
    store: Store, plan_id: str, note: str, now: str, actor: str
):
    """完成补充告知：授权恢复有效，设备快照更新为当前版本。"""
    plan = store.get("plans", plan_id)
    consent = store.get("consents", plan.consent_id)
    batch = store.get("batches", plan.batch_id)
    snapshot = {
        "firmware_version": batch.firmware_version,
        "calibration_version": batch.calibration_version,
        "algorithm_version": batch.algorithm_version,
    }
    consent.status = ConsentStatus.VALID
    consent.device_snapshot = snapshot
    consent.supplements.append({"note": note, "at": now, "versions": snapshot})
    if plan.status == PlanStatus.PENDING_SUPPLEMENT:
        plan.status = PlanStatus.ACTIVE
        plan.status_reason = ""
    store.log(actor, "supplement_resolved", {"plan_id": plan_id, "note": note}, now)
    return plan


def resolve_reapproval(
    store: Store, plan_id: str, note: str, now: str, actor: str
):
    """重新审批通过：计划恢复进行中。"""
    plan = store.get("plans", plan_id)
    if plan.status != PlanStatus.PENDING_REAPPROVAL:
        raise ValueError(f"计划状态为{plan.status}，无需重新审批")
    plan.status = PlanStatus.ACTIVE
    plan.status_reason = ""
    store.log(actor, "reapproval_resolved", {"plan_id": plan_id, "note": note}, now)
    return plan


def resume_plan(store: Store, plan_id: str, now: str, actor: str):
    """暂停后恢复：要求设备批次仍在用。"""
    plan = store.get("plans", plan_id)
    if plan.status != PlanStatus.SUSPENDED:
        raise ValueError(f"计划状态为{plan.status}，不在暂停中")
    batch = store.get("batches", plan.batch_id)
    if batch.status != BatchStatus.ACTIVE:
        raise ValueError(f"设备批次{batch.batch_id}仍处于{batch.status}，禁止恢复")
    plan.status = PlanStatus.ACTIVE
    plan.status_reason = ""
    store.log(actor, "plan_resumed", {"plan_id": plan_id}, now)
    return plan
