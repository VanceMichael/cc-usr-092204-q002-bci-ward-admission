"""团体标准：版本发布、宽松度比较与绑定守卫。

核心规则：治疗计划在创建时绑定当时的标准版本；标准升级后，
旧病例仍按原版本评估，任何更宽松的新版本都不得追溯套用。
"""
from __future__ import annotations

from src.admission.models import (
    OPERATOR_LEVELS,
    WARD_LEVELS,
    StandardStatus,
    StandardVersion,
    level_rank,
)
from src.admission.store import Store


def publish_standard(store: Store, *, now: str, **fields) -> StandardVersion:
    """发布标准新版本，同标准的旧发布版本转为已替代。"""
    fields.pop("status", None)
    fields.pop("issued_at", None)
    standard_id = fields["standard_id"]
    previous = store.find(
        "standards", standard_id=standard_id, status=StandardStatus.PUBLISHED
    )
    for old in previous:
        old.status = StandardStatus.SUPERSEDED
    if previous and not fields.get("supersedes"):
        fields["supersedes"] = previous[0].version
    version = StandardVersion(
        status=StandardStatus.PUBLISHED, issued_at=now, **fields
    )
    store.put("standards", f"{standard_id}@{version.version}", version)
    store.log(
        "system",
        "standard_published",
        {"standard_id": standard_id, "version": version.version},
        now,
    )
    return version


def get_standard(store: Store, standard_id: str, version: str) -> StandardVersion:
    return store.get("standards", f"{standard_id}@{version}")


def current_standard(store: Store, standard_id: str) -> StandardVersion:
    published = store.find(
        "standards", standard_id=standard_id, status=StandardStatus.PUBLISHED
    )
    if not published:
        raise KeyError(f"标准 {standard_id} 尚无已发布版本")
    return published[0]


def loosenings(new: StandardVersion, old: StandardVersion) -> list[str]:
    """列出新版本相对旧版本放宽的维度；返回空列表表示未放宽。"""
    reasons: list[str] = []
    if level_rank(WARD_LEVELS, new.min_ward_level) < level_rank(
        WARD_LEVELS, old.min_ward_level
    ):
        reasons.append("病房等级要求降低")
    if old.emergency_kit_required and not new.emergency_kit_required:
        reasons.append("急救配置要求取消")
    if set(new.allowed_indications) > set(old.allowed_indications):
        reasons.append("适用治疗范围扩大")
    if new.age_min < old.age_min or new.age_max > old.age_max:
        reasons.append("适用年龄范围放宽")
    if set(new.excluded_conditions) < set(old.excluded_conditions):
        reasons.append("排除情形减少")
    if level_rank(OPERATOR_LEVELS, new.min_operator_level) < level_rank(
        OPERATOR_LEVELS, old.min_operator_level
    ):
        reasons.append("操作者等级要求降低")
    if new.sae_report_hours > old.sae_report_hours:
        reasons.append("严重不良事件报告时限放宽")
    return reasons


def is_looser(new: StandardVersion, old: StandardVersion) -> bool:
    return bool(loosenings(new, old))


def repin_plan_standard(store: Store, plan_id: str, version: str, *, now: str, actor: str):
    """为既有计划重新绑定标准版本。

    仅允许绑定不更宽松的版本（如监管要求收紧）；试图借标准升级
    追溯放宽旧病例条件时拒绝。
    """
    plan = store.get("plans", plan_id)
    old = get_standard(store, plan.standard_id, plan.standard_version)
    new = get_standard(store, plan.standard_id, version)
    reasons = loosenings(new, old)
    if reasons:
        raise ValueError("标准升级不得追溯放宽旧病例条件：" + "、".join(reasons))
    plan.standard_version = version
    store.log(
        actor,
        "plan_standard_repinned",
        {"plan_id": plan_id, "from": old.version, "to": version},
        now,
    )
    return plan
