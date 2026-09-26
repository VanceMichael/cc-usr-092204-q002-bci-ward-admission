"""事后还原：管理方可复原当时采用的规范、证据、人工决定与设备状态。"""
from __future__ import annotations

from dataclasses import asdict

from src.admission.store import Store


def plan_trail(store: Store, plan_id: str) -> list[dict]:
    """该计划在审计日志中的全部痕迹（含床旁同步上来的条目）。"""
    return [
        entry
        for entry in store.audit_log
        if entry.get("details", {}).get("plan_id") == plan_id
        or plan_id in entry.get("details", {}).get("plan_ids", [])
    ]


def reconstruct_decision(store: Store, decision_id: str) -> dict:
    """还原一次放行结论的完整上下文。"""
    decision = store.get("decisions", decision_id)
    snapshot = decision.snapshot
    trail = plan_trail(store, decision.plan_id)
    manual = [
        entry for entry in trail if entry.get("action", "").startswith("manual_")
    ]
    return {
        "decision": asdict(decision),
        "standard": snapshot["standard"],
        "evidence": {
            "ethics": snapshot["ethics"],
            "consent": snapshot["consent"],
            "operator": snapshot["operator"],
            "observations": snapshot["observations"],
        },
        "device_state": snapshot["batch"],
        "manual_decisions": manual,
        "plan_trail": trail,
    }
