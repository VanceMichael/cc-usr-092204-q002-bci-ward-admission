"""不良事件：严重不良事件（SAE）立即阻断同批设备。"""
from __future__ import annotations

from src.admission.models import (
    AdverseEvent,
    BatchStatus,
    PlanStatus,
    Severity,
)
from src.admission.store import Store


def report_adverse_event(
    store: Store,
    plan_id: str,
    severity: str,
    description: str,
    now: str,
    actor: str = "system",
) -> dict:
    """登记不良事件；严重事件立即阻断同批设备并暂停相关计划。"""
    if severity not in (Severity.GENERAL, Severity.SERIOUS):
        raise ValueError(f"未知严重程度：{severity}")
    plan = store.get("plans", plan_id)
    prefix = "SAE" if severity == Severity.SERIOUS else "AE"
    event = AdverseEvent(
        event_id=store.next_id(prefix),
        plan_id=plan_id,
        batch_id=plan.batch_id,
        severity=severity,
        description=description,
        reported_at=now,
    )
    store.put("events", event.event_id, event)

    result = {"event_id": event.event_id, "blocked": False, "affected_plans": []}
    if severity == Severity.SERIOUS:
        batch = store.get("batches", plan.batch_id)
        batch.status = BatchStatus.BLOCKED
        for other in store.find("plans", batch_id=batch.batch_id):
            if other.status in (PlanStatus.COMPLETED, PlanStatus.TERMINATED):
                continue
            other.status = PlanStatus.SUSPENDED
            other.status_reason = f"严重不良事件{event.event_id}：同批设备已阻断"
            result["affected_plans"].append(other.plan_id)
        result["blocked"] = True

    store.log(
        actor,
        "adverse_event_reported",
        {
            "event_id": event.event_id,
            "plan_id": plan_id,
            "severity": severity,
            "blocked": result["blocked"],
            "affected_plans": result["affected_plans"],
        },
        now,
    )
    return result


def reinstate_batch(store: Store, batch_id: str, reason: str, now: str, actor: str):
    """调查结案后由管理方恢复批次使用；全程留痕。"""
    batch = store.get("batches", batch_id)
    if batch.status != BatchStatus.BLOCKED:
        raise ValueError(f"批次状态为{batch.status}，不在阻断中")
    batch.status = BatchStatus.ACTIVE
    store.log(
        actor, "batch_reinstated", {"batch_id": batch_id, "reason": reason}, now
    )
    return batch
