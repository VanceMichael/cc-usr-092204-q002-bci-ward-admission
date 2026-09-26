"""准入领域服务：命令校验、事件副作用、投影与放行规则引擎。

设计要点
--------
1. 治疗计划在 ``plan.frozen`` 时整体封存（标准版本/伦理批件/授权版本/设备固件算法校准/
   操作者资质/观察方案）。放行核对始终以冻结快照为准去查对应**版本**的档案，
   因此团体标准升级既不会放宽、也不会由系统自动改写旧病例。
2. 设备固件/传感器校准/算法版本变化（``device.changed``）按变更策略自动把在治病例
   分级标记为：暂停待核验 / 需补充告知 / 需重新审批。
3. 严重不良事件（``sae.reported``）触发系统自动追加 ``batch.quarantined``，同批次所有
   在治病例立即暂停；放行核对对此为不可人工覆盖的硬阻断。
"""
from __future__ import annotations

import copy
import hashlib
import json
import time
import uuid
from typing import Any, Callable

from . import models as M
from .eventstore import Event, EventStore


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _md5_list(**items: Any) -> str:
    blob = json.dumps(items, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# 投影
# ---------------------------------------------------------------------------
class Registry:
    """由事件流重放得到的当前状态（或某一时点状态）。"""

    def __init__(self) -> None:
        self.hospitals: dict[str, dict[str, Any]] = {}
        self.standards: dict[str, dict[str, Any]] = {}     # code -> {versions: {v: rec}}
        self.ethics: dict[str, dict[str, Any]] = {}
        self.consents: dict[str, dict[str, Any]] = {}
        self.wards: dict[str, dict[str, Any]] = {}
        self.batches: dict[str, dict[str, Any]] = {}
        self.devices: dict[str, dict[str, Any]] = {}
        self.operators: dict[str, dict[str, Any]] = {}
        self.cases: dict[str, dict[str, Any]] = {}
        self.plans: dict[str, dict[str, Any]] = {}
        self.observations: dict[str, list[dict[str, Any]]] = {}
        self.sessions: dict[str, list[int]] = {}
        self.overrides: dict[str, list[dict[str, Any]]] = {}
        # plan_id -> 同一冻结计划下经人工核验接受的版本等价性决定（不修改冻结快照本身）
        self.change_acceptances: dict[str, list[dict[str, Any]]] = {}
        self.saes: list[dict[str, Any]] = []
        self.change_policies: dict[tuple[str, str], str] = {}
        self.grants: list[dict[str, Any]] = []
        self.decisions: dict[str, list[dict[str, Any]]] = {}

    def apply(self, evt: Event) -> None:
        # 投影对事件数据做深拷贝后再原地演进，避免污染事件本身的哈希内容
        t, d = evt.event_type, copy.deepcopy(evt.data)
        if t == "hospital.registered":
            self.hospitals[d["hospital_id"]] = d
        elif t == "standard.issued":
            book = self.standards.setdefault(d["code"], {"versions": {}})
            book["versions"][d["version"]] = d
        elif t == "standard.deprecated":
            rec = self.standards[d["code"]]["versions"][d["version"]]
            rec["status"] = "deprecated"
        elif t == "ethics.approved":
            rec = dict(d)
            rec.setdefault("status", "active")
            self.ethics[d["approval_no"]] = rec
        elif t == "ethics.revoked":
            self.ethics[d["approval_no"]]["status"] = "revoked"
        elif t == "consent.granted":
            rec = dict(d)
            rec["status"] = "active"
            rec["supplemented_versions"] = []
            self.consents[d["consent_id"]] = rec
        elif t == "consent.withdrawn":
            self.consents[d["consent_id"]]["status"] = "withdrawn"
        elif t == "consent.supplemented":
            rec = self.consents[d["consent_id"]]
            rec["supplemented_versions"].append(
                {"template_version": d["new_template_version"], "signed_at": d["signed_at"],
                 "supplement_doc_id": d["supplement_doc_id"]})
        elif t == "ward.registered":
            self.wards[d["ward_id"]] = d
        elif t == "ward.certified":
            ward = self.wards[d["ward_id"]]
            ward.setdefault("certifications", {})[d["standard_version"]] = {
                "status": "certified", "conditions_hash": d["conditions_hash"],
                "certified_at": d.get("certified_at", evt.ts)}
        elif t == "ward.cert_revoked":
            ward = self.wards[d["ward_id"]]
            ward["certifications"][d["standard_version"]]["status"] = "revoked"
        elif t == "batch.registered":
            rec = dict(d)
            rec.setdefault("status", M.BATCH_ACTIVE)
            self.batches[d["batch_id"]] = rec
        elif t == "batch.quarantined":
            self.batches[d["batch_id"]].update(
                status=M.BATCH_QUARANTINED, hold_reason=d.get("reason"),
                held_since=evt.ts, held_by_sae=d.get("sae_event_id"))
        elif t == "batch.released":
            b = self.batches[d["batch_id"]]
            b.update(status=M.BATCH_ACTIVE, hold_reason=None, held_since=None,
                     released_by=d.get("investigation_id"), released_at=evt.ts)
        elif t == "batch.stopped":
            self.batches[d["batch_id"]].update(status=M.BATCH_STOPPED, stop_reason=d.get("reason"))
        elif t == "device.registered":
            rec = dict(d)
            rec.setdefault("status", M.DEVICE_ACTIVE)
            rec["history"] = [{
                "at": evt.ts, "change_type": "registration",
                "firmware_version": d["firmware_version"], "algorithm_version": d["algorithm_version"],
                "calibration_id": d["calibration_id"]}]
            self.devices[d["device_id"]] = rec
        elif t == "device.changed":
            dev = self.devices[d["device_id"]]
            key = {M.CHANGE_FIRMWARE: "firmware_version",
                   M.CHANGE_ALGORITHM: "algorithm_version",
                   M.CHANGE_CALIBRATION: "calibration_id"}[d["change_type"]]
            dev[key] = d["new"]
            # 重新审批/待核验级：设备挂起；补充告知级：风险门槛是患者补充签署，设备保持可用
            dev["status"] = M.DEVICE_HELD if d["review_class"] != "supplement" else M.DEVICE_ACTIVE
            dev["history"].append({"at": evt.ts, "change_id": d["change_id"],
                                   "change_type": d["change_type"],
                                   "old": d["old"], "new": d["new"], "review_class": d["review_class"]})
        elif t == "device.requalified":
            dev = self.devices[d["device_id"]]
            dev["status"] = M.DEVICE_ACTIVE
            dev["history"].append({"at": evt.ts, "change_type": "requalified",
                                   "evidence_id": d["evidence_id"], "note": d.get("note", "")})
        elif t == "changepolicy.registered":
            self.change_policies[(d["model"], d["change_type"])] = d["review_class"]
        elif t == "operator.registered":
            self.operators[d["operator_id"]] = dict(d)
        elif t == "case.opened":
            self.cases[d["case_id"]] = {
                "case_id": d["case_id"], "hospital_id": d["hospital_id"],
                "patient_ref": d["patient_ref"], "indication": d["indication"],
                "status": M.CASE_DRAFT, "plan_id": None, "flags": {}, "plan_history": [],
                "opened_at": evt.ts}
        elif t == "plan.frozen":
            plan = dict(d)
            self.plans[d["plan_id"]] = plan
            case = self.cases[d["case_id"]]
            if case["plan_id"]:
                old = self.plans[case["plan_id"]]
                old["status"] = "superseded"
            case["plan_id"] = d["plan_id"]
            case["status"] = M.CASE_APPROVED
            # 新计划版本重新冻结了设备/标准等条件，仅保留与在调批次 SAE 相关的硬阻断
            case["flags"] = {k: v for k, v in case.get("flags", {}).items()
                             if v.get("reason_code") == "BATCH_SAE_HOLD"}
            case["plan_history"].append(d["plan_id"])
        elif t == "case.flagged":
            case = self.cases[d["case_id"]]
            case["status"] = d["flag"]
            case["flags"][d["category"]] = {
                "reason_code": d["reason_code"], "at": evt.ts, "detail": d.get("detail", ""),
                "due_to": d.get("due_to", {}), "plan_id": d.get("plan_id", case["plan_id"])}
        elif t == "case.resumed":
            case = self.cases[d["case_id"]]
            case["flags"].pop(d["category"], None)
            if not case["flags"]:
                case["status"] = M.CASE_APPROVED
            case.setdefault("resolutions", []).append(
                {"category": d["category"], "at": evt.ts, "by": evt.actor,
                 "evidence": d.get("evidence", {}), "note": d.get("note", "")})
        elif t == "case.completed":
            self.cases[d["case_id"]]["status"] = M.CASE_COMPLETED
        elif t == "sae.reported":
            self.saes.append(dict(d, at=evt.ts))
        elif t == "observation.recorded":
            self.observations.setdefault(d["plan_id"], []).append(dict(d, at=evt.ts))
        elif t == "bedside.operation":
            if d.get("action") == "session_end":
                self.sessions.setdefault(d["plan_id"], []).append(d["session_seq"])
        elif t == "clearance.override":
            self.overrides.setdefault(d["plan_id"], []).append(dict(d, at=evt.ts))
        elif t == "device.change_accepted":
            self.change_acceptances.setdefault(d["plan_id"], []).append(dict(d, at=evt.ts))
        elif t == "access.granted":
            self.grants.append(dict(d, granted_at=evt.ts))
        elif t == "access.revoked":
            for g in self.grants:
                if g["grant_id"] == d["grant_id"]:
                    g["status"] = "revoked"
        elif t == "clearance.decided":
            self.decisions.setdefault(d["case_id"], []).append(dict(d, at=evt.ts))

    # ------------------------------------------------------------------ 查询
    def standard_effective_version(self, code: str, on_date: str | None = None) -> dict[str, Any] | None:
        book = self.standards.get(code)
        if not book:
            return None
        on_date = on_date or "9999-12-31"
        candidates = [v for v in book["versions"].values()
                      if v["effective_at"] <= on_date and v.get("status", "active") == "active"]
        return max(candidates, key=lambda v: v["version"], default=None)

    def active_plans_by_device(self, device_id: str) -> list[dict[str, Any]]:
        out = []
        for case in self.cases.values():
            plan_id = case.get("plan_id")
            if not plan_id or case["status"] not in M.ACTIVE_CASE_STATES:
                continue
            plan = self.plans[plan_id]
            if plan.get("status") != "active":
                continue
            if plan["device_id"] == device_id:
                out.append(plan)
        return out

    def active_plans_by_batch(self, batch_id: str) -> list[dict[str, Any]]:
        out = []
        for case in self.cases.values():
            plan_id = case.get("plan_id")
            if not plan_id or case["status"] not in M.ACTIVE_CASE_STATES:
                continue
            plan = self.plans[plan_id]
            if plan.get("status") == "active" and plan["batch_id"] == batch_id:
                out.append(plan)
        return out

    def active_grants(self, subject_id: str, case_id: str | None = None) -> list[dict[str, Any]]:
        return [g for g in self.grants
                if g.get("subject_id") == subject_id and g.get("status", "active") == "active"
                and (case_id is None or g.get("case_id") == case_id)]


# ---------------------------------------------------------------------------
# 领域服务
# ---------------------------------------------------------------------------
class AdmissionService:
    def __init__(self, store: EventStore, clock: Callable[[], str] | None = None):
        self.store = store
        self.reg = Registry()
        self._clock = clock or (lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        self._applied_ids: set[str] = set()
        store.replay(self._bootstrap)

    def _bootstrap(self, evt: Event) -> None:
        self.reg.apply(evt)
        self._applied_ids.add(evt.event_id)

    def now(self) -> str:
        return self._clock()

    # ------------------------------------------------------------------ 权限
    def _require(self, actor: dict[str, Any], roles: tuple[str, ...], what: str) -> None:
        if actor.get("role") not in roles:
            raise M.DomainError("ACCESS_DENIED", f"角色 {actor.get('role')} 无权执行：{what}")

    def _append(self, event_type: str, data: dict[str, Any], actor: dict[str, Any],
                event_id: str | None = None, ts: str | None = None) -> Event:
        # 服务时钟为事件时间的权威来源（床旁补传等显式 ts 除外）
        evt = self.store.append(event_type, data, actor=actor.get("id", "system"),
                                event_id=event_id, ts=ts or self.now())
        self.reg.apply(evt)
        self._applied_ids.add(evt.event_id)
        return evt

    # 床旁断网补传允许的事件类型白名单：补传通道不得成为提权入口
    BEDSIDE_INGEST_TYPES = {"bedside.operation", "observation.recorded"}

    def ingest_bedside(self, raw: dict[str, Any]) -> Event:
        """接收床旁端断网期间本地落盘的事件；按 event_id 幂等，重复补传不重复入账。"""
        if raw.get("event_type") not in self.BEDSIDE_INGEST_TYPES:
            raise M.DomainError("INGEST_FORBIDDEN",
                                f"补传通道不接受事件类型 {raw.get('event_type')}")
        evt = self.store.ingest(raw)
        if evt.event_id not in self._applied_ids:
            self.reg.apply(evt)
            self._applied_ids.add(evt.event_id)
        return evt

    # ------------------------------------------------------------------ 基础档案
    def register_hospital(self, actor, data):
        self._require(actor, (M.ROLE_ADMIN,), "登记医疗机构")
        return self._append("hospital.registered", data, actor)

    def issue_standard(self, actor, data):
        """发布团体标准版本。新版本绝不修改旧版本档案，旧计划继续按旧版本核对。"""
        self._require(actor, (M.ROLE_ADMIN,), "发布团体标准")
        data.setdefault("status", "active")
        return self._append("standard.issued", data, actor)

    def deprecate_standard(self, actor, code, version, reason):
        self._require(actor, (M.ROLE_ADMIN,), "废止团体标准版本")
        evt = self._append("standard.deprecated",
                           {"code": code, "version": version, "reason": reason}, actor)
        # 安全方向：旧版本被废止时，在治病例暂停等待医疗负责人决定（不放宽、只收紧）
        for case in list(self.reg.cases.values()):
            if case["status"] not in M.ACTIVE_CASE_STATES or not case["plan_id"]:
                continue
            plan = self.reg.plans[case["plan_id"]]
            if plan["standard_code"] == code and plan["standard_version"] == version:
                self._append("case.flagged", {
                    "case_id": case["case_id"], "plan_id": plan["plan_id"],
                    "flag": M.CASE_SUSPENDED, "category": "standard_deprecated",
                    "reason_code": "STD_DEPRECATED",
                    "detail": f"团体标准 {code} {version} 已废止：{reason}",
                    "due_to": {"type": "standard", "code": code, "version": version}}, actor)
        return evt

    def grant_ethics(self, actor, data):
        self._require(actor, (M.ROLE_ETHICS_COMMITTEE, M.ROLE_ADMIN), "出具伦理批件")
        data.setdefault("status", "active")
        return self._append("ethics.approved", data, actor)

    def revoke_ethics(self, actor, approval_no, reason):
        self._require(actor, (M.ROLE_ETHICS_COMMITTEE,), "撤销伦理批件")
        return self._append("ethics.revoked",
                            {"approval_no": approval_no, "reason": reason}, actor)

    def grant_consent(self, actor, data):
        self._require(actor, (M.ROLE_TREATING_PHYSICIAN,), "登记患者知情授权")
        return self._append("consent.granted", data, actor)

    def withdraw_consent(self, actor, consent_id, reason):
        self._require(actor, (M.ROLE_TREATING_PHYSICIAN, M.ROLE_ADMIN), "记录授权撤回")
        evt = self._append("consent.withdrawn",
                           {"consent_id": consent_id, "reason": reason}, actor)
        for plan in self.reg.plans.values():
            if plan.get("consent_id") != consent_id or plan.get("status") != "active":
                continue
            self._append("case.flagged", {
                "case_id": plan["case_id"], "plan_id": plan["plan_id"],
                "flag": M.CASE_SUSPENDED, "category": "consent_withdrawn",
                "reason_code": "CONSENT_WITHDRAWN",
                "detail": f"患者授权 {consent_id} 已撤回：{reason}",
                "due_to": {"type": "consent", "consent_id": consent_id}}, actor)
        return evt

    def supplement_consent(self, actor, consent_id, new_template_version, supplement_doc_id, signed_at=None):
        self._require(actor, (M.ROLE_TREATING_PHYSICIAN,), "登记补充告知与签署")
        return self._append("consent.supplemented", {
            "consent_id": consent_id, "new_template_version": new_template_version,
            "supplement_doc_id": supplement_doc_id,
            "signed_at": signed_at or self.now()}, actor)

    def register_ward(self, actor, data):
        self._require(actor, (M.ROLE_ADMIN,), "登记病房")
        return self._append("ward.registered", data, actor)

    def certify_ward(self, actor, data):
        self._require(actor, (M.ROLE_ADMIN, M.ROLE_AUDITOR), "认证病房条件")
        return self._append("ward.certified", data, actor)

    def register_batch(self, actor, data):
        self._require(actor, (M.ROLE_ADMIN,), "登记设备批次")
        data.setdefault("device_class", "noninvasive")
        if data["device_class"] != "noninvasive":
            raise M.DomainError("SCOPE_LIMIT", "当前冻结范围仅接纳非侵入式产品")
        return self._append("batch.registered", data, actor)

    def register_device(self, actor, data):
        self._require(actor, (M.ROLE_ADMIN, M.ROLE_ENGINEER), "登记设备")
        if actor.get("role") == M.ROLE_ENGINEER:
            batch = self.reg.batches.get(data["batch_id"])
            if not batch or batch.get("vendor") != actor.get("vendor"):
                raise M.DomainError(
                    "ACCESS_DENIED", "企业工程师只能登记本企业批次下的设备")
        data.setdefault("status", M.DEVICE_ACTIVE)
        return self._append("device.registered", data, actor)

    def register_change_policy(self, actor, model, change_type, review_class):
        self._require(actor, (M.ROLE_ADMIN,), "登记变更审查分级策略")
        if review_class not in ("requalify", "supplement", "reapprove"):
            raise M.DomainError("BAD_POLICY", "review_class 必须为 requalify/supplement/reapprove")
        return self._append("changepolicy.registered", {
            "model": model, "change_type": change_type, "review_class": review_class}, actor)

    def register_operator(self, actor, data):
        self._require(actor, (M.ROLE_ADMIN,), "登记操作者资质")
        return self._append("operator.registered", data, actor)

    def grant_access(self, actor, data):
        self._require(actor, (M.ROLE_ADMIN, M.ROLE_TREATING_PHYSICIAN), "授予职责内访问权限")
        data.setdefault("grant_id", f"G-{uuid.uuid4().hex[:10]}")
        data.setdefault("status", "active")
        return self._append("access.granted", data, actor)

    # ------------------------------------------------------------------ 病例与计划
    def open_case(self, actor, data):
        self._require(actor, (M.ROLE_TREATING_PHYSICIAN, M.ROLE_ADMIN), "建立病例")
        if data["indication"] not in M.INDICATIONS:
            raise M.DomainError("STD_SCOPE", "适应症不在非侵入式产品冻结目录内")
        if data["hospital_id"] not in self.reg.hospitals:
            raise M.DomainError("HOSPITAL_UNKNOWN", "医疗机构未登记")
        data.setdefault("case_id", f"C-{uuid.uuid4().hex[:10]}")
        return self._append("case.opened", data, actor), data["case_id"]

    def freeze_plan(self, actor, *, case_id, ethics_approval_no, consent_id, device_id,
                    operator_id, ward_id, setting, observation_protocol_id,
                    standard_code="T/BMIA-001"):
        """把一次治疗计划所需的全部条件冻结为不可变快照（plan vN）。"""
        self._require(actor, (M.ROLE_TREATING_PHYSICIAN, M.ROLE_ADMIN), "冻结治疗计划")
        case = self.reg.cases.get(case_id)
        if not case:
            raise M.DomainError("CASE_UNKNOWN", "病例不存在")
        if case["status"] in (M.CASE_COMPLETED, M.CASE_WITHDRAWN):
            raise M.DomainError("CASE_CLOSED", "病例已结束，不能新增治疗计划")

        std = self.reg.standard_effective_version(standard_code, self.now()[:10])
        if std is None:
            raise M.DomainError("STANDARD_MISSING", f"团体标准 {standard_code} 无生效版本")
        ethics = self.reg.ethics.get(ethics_approval_no)
        consent = self.reg.consents.get(consent_id)
        device = self.reg.devices.get(device_id)
        batch = self.reg.batches.get(device["batch_id"]) if device else None
        operator = self.reg.operators.get(operator_id)
        ward = self.reg.wards.get(ward_id)
        for name, obj, code in (("伦理批件", ethics, "ETHICS_MISSING"),
                                ("患者授权", consent, "CONSENT_MISSING"),
                                ("设备", device, "DEVICE_UNKNOWN"),
                                ("操作者资质", operator, "OPERATOR_CREDENTIAL_MISSING"),
                                ("病房", ward, "WARD_UNKNOWN")):
            if obj is None:
                raise M.DomainError(code, f"{name}不存在，不能冻结计划")
        if batch is None:
            raise M.DomainError("BATCH_UNKNOWN", "设备批次不存在")

        # 冻结前的硬性一致性核对（冻结时不允许带病入库）
        scope = std["scope"].get(case["indication"])
        if not scope or setting not in scope.get("settings", []):
            raise M.DomainError("STD_SCOPE",
                                f"标准 {std['code']} {std['version']} 不允许该适应症在 {setting} 使用")
        if ethics["status"] != "active" or ethics["valid_until"] < self.now()[:10]:
            raise M.DomainError("ETHICS_INVALID", "伦理批件无效或已过期")
        if ethics["hospital_id"] != case["hospital_id"] or case["indication"] not in ethics["scope"]["indications"]:
            raise M.DomainError("ETHICS_SCOPE", "伦理批件机构/适应症与病例不匹配")
        if ethics["standard_version"] != std["version"] or ethics["standard_code"] != std["code"]:
            raise M.DomainError("ETHICS_STANDARD_MISMATCH", "伦理批件对应的标准版本与冻结版本不一致")
        if consent["case_id"] != case_id or consent["status"] != "active":
            raise M.DomainError("CONSENT_INVALID", "授权与病例不匹配或已失效")
        if consent["template_version"] != std["consent_template_version"]:
            raise M.DomainError("CONSENT_VERSION",
                                f"授权告知版本 {consent['template_version']} 与标准要求 {std['consent_template_version']} 不一致")
        if batch["status"] != M.BATCH_ACTIVE or device["status"] != M.DEVICE_ACTIVE:
            raise M.DomainError("DEVICE_HELD", "设备或批次处于停用/待核验状态")
        if operator["valid_until"] < self.now()[:10]:
            raise M.DomainError("OPERATOR_CREDENTIAL_EXPIRED", "操作者资质已过期")
        if case["indication"] not in operator["scope"]["indications"] \
                or batch["model"] not in operator["scope"]["device_models"]:
            raise M.DomainError("OPERATOR_CREDENTIAL_SCOPE", "操作者资质范围不覆盖该适应症或型号")
        cert = ward.get("certifications", {}).get(std["version"])
        if not cert or cert["status"] != "certified":
            raise M.DomainError("WARD_CERT", f"病房条件未按 {std['version']} 认证")

        version = (max(int(p["plan_version"]) for p in self.reg.plans.values()
                       if p["case_id"] == case_id) + 1) if case["plan_history"] else 1
        plan_id = f"P-{case_id}-V{version}"
        plan = M.FrozenPlan(
            plan_id=plan_id, case_id=case_id, hospital_id=case["hospital_id"],
            ward_id=ward_id, indication=case["indication"], setting=setting,
            standard_code=std["code"], standard_version=std["version"],
            standard_content_hash=std["content_hash"],
            ethics_approval_no=ethics_approval_no, consent_id=consent_id,
            consent_template_version=consent["template_version"],
            device_id=device_id, batch_id=batch["batch_id"],
            firmware_version=device["firmware_version"],
            algorithm_version=device["algorithm_version"],
            calibration_id=device["calibration_id"],
            operator_id=operator_id, credential_id=operator["credential_id"],
            observation_protocol_id=observation_protocol_id,
            frozen_at=self.now(), plan_version=version,
            supersedes=case["plan_id"])
        snapshot = {k: v for k, v in plan.to_dict().items() if k != "snapshot_hash"}
        plan.snapshot_hash = _md5_list(**snapshot)
        evt = self._append("plan.frozen", {**snapshot, "snapshot_hash": plan.snapshot_hash,
                                           "status": "active"}, actor)
        return evt

    # ------------------------------------------------------------------ 设备变更
    def report_device_change(self, actor, *, device_id, change_type, old, new, reason=""):
        """上报固件/校准/算法变化，系统自动定位受影响在治病例并分级。"""
        if change_type not in M.CHANGE_TYPES:
            raise M.DomainError("BAD_CHANGE", "变更类型必须为 firmware/calibration/algorithm")
        device = self.reg.devices.get(device_id)
        if not device:
            raise M.DomainError("DEVICE_UNKNOWN", "设备不存在")
        if actor.get("role") == M.ROLE_ENGINEER:
            batch = self.reg.batches[device["batch_id"]]
            if batch.get("vendor") != actor.get("vendor"):
                raise M.DomainError("ACCESS_DENIED", "企业工程师只能处理本企业设备")
        else:
            self._require(actor, (M.ROLE_ADMIN,), "上报设备变更")
        batch = self.reg.batches[device["batch_id"]]
        review_class = self.reg.change_policies.get((batch["model"], change_type), {
            M.CHANGE_FIRMWARE: "requalify",
            M.CHANGE_CALIBRATION: "requalify",
            M.CHANGE_ALGORITHM: "reapprove",
        }[change_type])
        change_id = f"CHG-{uuid.uuid4().hex[:10]}"
        evt = self._append("device.changed", {
            "change_id": change_id, "device_id": device_id, "batch_id": device["batch_id"],
            "model": batch["model"], "change_type": change_type,
            "old": old, "new": new, "review_class": review_class,
            "reason": reason, "reported_by": actor.get("id")}, actor)

        flag_map = {
            "requalify": (M.CASE_SUSPENDED, "device_requalify", "DEVICE_REQUALIFY",
                          f"设备{ {'firmware':'固件','calibration':'传感器校准','algorithm':'算法版本'}[change_type] }"
                          f"由 {old} 变更为 {new}，计划暂停待核验"),
            "supplement": (M.CASE_SUPPLEMENT_NOTICE, "device_supplement", "DEVICE_SUPPLEMENT",
                           f"设备{ {'firmware':'固件','calibration':'传感器校准','algorithm':'算法版本'}[change_type] }"
                           f"变更（{old} → {new}），需向患者补充告知并重新签署"),
            "reapprove": (M.CASE_REAPPROVAL, "device_reapproval", "DEVICE_REAPPROVAL",
                          f"设备算法版本变更（{old} → {new}）超出冻结快照，需重新伦理审批"),
        }
        flag, category, reason_code, detail = flag_map[review_class]
        affected = []
        for plan in self.reg.active_plans_by_device(device_id):
            self._append("case.flagged", {
                "case_id": plan["case_id"], "plan_id": plan["plan_id"],
                "flag": flag, "category": category, "reason_code": reason_code,
                "detail": detail + f"；变更单 {change_id}",
                "due_to": {"type": "device_change", "change_id": change_id,
                           "change_type": change_type, "review_class": review_class}}, actor)
            affected.append(plan["case_id"])
        return evt, affected, review_class

    def requalify_device(self, actor, device_id, evidence_id, note=""):
        self._require(actor, (M.ROLE_ADMIN, M.ROLE_ENGINEER), "登记设备核验合格")
        device = self.reg.devices.get(device_id)
        if actor.get("role") == M.ROLE_ENGINEER and \
                self.reg.batches[device["batch_id"]]["vendor"] != actor.get("vendor"):
            raise M.DomainError("ACCESS_DENIED", "企业工程师只能处理本企业设备")
        return self._append("device.requalified", {
            "device_id": device_id, "evidence_id": evidence_id, "note": note}, actor)

    def accept_device_change(self, actor, *, case_id, plan_id, change_id, evidence_id,
                             equivalence_note, basis="requalify"):
        """设备变更后的“继续本计划”接受决定（冻结快照本身始终不改写）。

        - basis=requalify（暂停待核验级）：以设备核验合格为前提，依据技术等价证据；
        - basis=supplement（补充告知级）：以患者已就该变更补充签署知情为前提，
          evidence_id 须为已登记的补充告知书编号；
        - reapprove（重新审批级）不接受此通道，必须重新伦理审批并冻结计划新版本。
        该接受只覆盖指定变更单；设备再次变化后放行仍会硬阻断。
        """
        self._require(actor, (M.ROLE_MEDICAL_DIRECTOR,), "接受设备变更后继续治疗")
        case = self.reg.cases.get(case_id)
        plan = self.reg.plans.get(plan_id)
        if not case or not plan:
            raise M.DomainError("CASE_UNKNOWN", "病例或计划不存在")
        category = {"requalify": "device_requalify",
                    "supplement": "device_supplement"}.get(basis)
        if category is None or category not in case["flags"]:
            raise M.DomainError("FLAG_NOT_OPEN",
                                f"该病例没有待处理的 {basis} 级设备变更标记")
        flag = case["flags"][category]
        if flag["due_to"].get("change_id") != change_id:
            raise M.DomainError("CHANGE_MISMATCH", "接受决定对应的变更单与挂起标记不一致")
        device = self.reg.devices[plan["device_id"]]
        if basis == "requalify":
            if device["status"] != M.DEVICE_ACTIVE:
                raise M.DomainError("DEVICE_NOT_REQUALIFIED", "设备尚未核验合格，不能接受")
        else:
            consent = self.reg.consents.get(plan["consent_id"])
            if not any(s["supplement_doc_id"] == evidence_id
                       for s in consent["supplemented_versions"]):
                raise M.DomainError("SUPPLEMENT_MISSING",
                                    "患者尚未就该变更补充签署知情告知书")
        self._append("device.change_accepted", {
            "case_id": case_id, "plan_id": plan_id, "change_id": change_id,
            "device_id": plan["device_id"], "change_type": flag["due_to"]["change_type"],
            "basis": basis, "evidence_id": evidence_id,
            "equivalence_note": equivalence_note,
            "approver": actor.get("id")}, actor)
        return self.resume_case(
            actor, case_id, category,
            note=f"接受变更单 {change_id}（{basis}）：{equivalence_note}",
            evidence={"change_id": change_id, "evidence_id": evidence_id, "basis": basis})

    # ------------------------------------------------------------------ SAE
    def report_sae(self, actor, *, sae_id=None, case_id, device_id, description, severity="serious"):
        """严重不良事件：立即隔离同批设备并暂停同批全部在治病例。"""
        self._require(actor, (M.ROLE_TREATING_PHYSICIAN, M.ROLE_BEDSIDE_NURSE, M.ROLE_ADMIN),
                      "上报严重不良事件")
        device = self.reg.devices.get(device_id)
        if not device:
            raise M.DomainError("DEVICE_UNKNOWN", "设备不存在")
        batch_id = device["batch_id"]
        sae_id = sae_id or f"SAE-{uuid.uuid4().hex[:10]}"
        evt = self._append("sae.reported", {
            "sae_id": sae_id, "case_id": case_id, "device_id": device_id,
            "batch_id": batch_id, "severity": severity,
            "description": description, "reporter": actor.get("id")}, actor)
        # 系统自动动作（非人工）：同批立即停用，幂等（已隔离不重复挂起）
        sys_actor = {"id": "system:sae-guard"}
        if self.reg.batches[batch_id]["status"] != M.BATCH_QUARANTINED:
            self._append("batch.quarantined", {
                "batch_id": batch_id, "sae_event_id": sae_id,
                "reason": f"严重不良事件 {sae_id} 触发自动批次隔离"}, sys_actor)
        for plan in self.reg.active_plans_by_batch(batch_id):
            case = self.reg.cases[plan["case_id"]]
            if "batch_sae" not in case["flags"]:
                self._append("case.flagged", {
                    "case_id": plan["case_id"], "plan_id": plan["plan_id"],
                    "flag": M.CASE_SUSPENDED, "category": "batch_sae",
                    "reason_code": "BATCH_SAE_HOLD",
                    "detail": f"同批设备因 {sae_id} 立即停用，调查结束前不得治疗",
                    "due_to": {"type": "sae", "sae_id": sae_id, "batch_id": batch_id}}, sys_actor)
        return evt

    def release_batch(self, actor, batch_id, investigation_id, conclusion):
        """调查结案后解除批次隔离；病例仍需医疗负责人逐例评估后方可恢复。"""
        self._require(actor, (M.ROLE_ADMIN, M.ROLE_MEDICAL_DIRECTOR), "解除批次隔离")
        return self._append("batch.released", {
            "batch_id": batch_id, "investigation_id": investigation_id,
            "conclusion": conclusion}, actor)

    def resume_case(self, actor, case_id, category, note="", evidence=None):
        """人工解除某类暂停标记（如批次解除后的逐例恢复），决定本身留痕。"""
        self._require(actor, (M.ROLE_MEDICAL_DIRECTOR, M.ROLE_TREATING_PHYSICIAN), "恢复病例")
        case = self.reg.cases[case_id]
        if category not in case["flags"]:
            raise M.DomainError("FLAG_NOT_OPEN", "该病例没有这类待处理标记")
        return self._append("case.resumed", {
            "case_id": case_id, "category": category, "note": note,
            "evidence": evidence or {}}, actor)

    def complete_case(self, actor, case_id):
        self._require(actor, (M.ROLE_TREATING_PHYSICIAN,), "结束病例")
        return self._append("case.completed", {"case_id": case_id}, actor)

    # ------------------------------------------------------------------ 临床记录
    def record_observation(self, actor, *, plan_id, findings, normal=True, session_seq=None,
                           metrics=None):
        self._require(actor, (M.ROLE_TREATING_PHYSICIAN, M.ROLE_BEDSIDE_NURSE), "记录临床观察")
        plan = self.reg.plans.get(plan_id)
        if not plan:
            raise M.DomainError("PLAN_UNKNOWN", "治疗计划不存在")
        obs_id = f"OBS-{uuid.uuid4().hex[:10]}"
        return self._append("observation.recorded", {
            "obs_id": obs_id, "plan_id": plan_id, "case_id": plan["case_id"],
            "protocol_id": plan["observation_protocol_id"],
            "session_seq": session_seq, "findings": findings,
            "normal": normal, "metrics": metrics or {},
            "recorder": actor.get("id")}, actor)

    def log_bedside_operation(self, actor, data, event_id=None, ts=None):
        """床旁操作日志（会话开始/结束、异常处置等），可来自断网补传，幂等。"""
        self._require(actor, (M.ROLE_BEDSIDE_NURSE, M.ROLE_TREATING_PHYSICIAN), "记录床旁操作")
        data.setdefault("log_id", f"LOG-{uuid.uuid4().hex[:10]}")
        return self._append("bedside.operation", data, actor, event_id=event_id, ts=ts)

    def add_override(self, actor, *, plan_id, rule_code, rationale, expires_at):
        """医疗负责人对可背书的暂缓项进行人工背书；硬阻断一律拒绝。"""
        self._require(actor, (M.ROLE_MEDICAL_DIRECTOR,), "人工背书放行")
        if rule_code not in M.OVERRIDABLE_RULES:
            raise M.DomainError("OVERRIDE_FORBIDDEN",
                                f"规则 {rule_code} 属于硬性阻断，不允许人工背书")
        return self._append("clearance.override", {
            "plan_id": plan_id, "rule_code": rule_code, "rationale": rationale,
            "expires_at": expires_at, "approver": actor.get("id")}, actor)

    # ------------------------------------------------------------------ 放行引擎
    def evaluate_clearance(self, *, case_id, setting=None, operator_id=None,
                           actor=None, persist=True) -> dict[str, Any]:
        case = self.reg.cases.get(case_id)
        if not case or not case.get("plan_id"):
            raise M.DomainError("PLAN_MISSING", "病例尚无冻结的治疗计划")
        plan = self.reg.plans[case["plan_id"]]
        if plan.get("status") != "active":
            raise M.DomainError("PLAN_NOT_ACTIVE", "当前计划版本已被替代")
        today = self.now()[:10]
        setting = setting or plan["setting"]
        operator_id = operator_id or plan["operator_id"]
        std = self.reg.standards[plan["standard_code"]]["versions"][plan["standard_version"]]
        ethics = self.reg.ethics.get(plan["ethics_approval_no"])
        consent = self.reg.consents.get(plan["consent_id"])
        device = self.reg.devices.get(plan["device_id"])
        batch = self.reg.batches.get(plan["batch_id"])
        operator = self.reg.operators.get(operator_id)
        ward = self.reg.wards.get(plan["ward_id"])
        rules: list[M.RuleResult] = []

        def add(code, title, passed, severity, detail, evidence=None):
            rules.append(M.RuleResult(code, title, passed, severity, detail, evidence or []))

        # 1. 团体标准与冻结适应症范围 —— 用冻结版本，不读当前最新版本
        scope = std.get("scope", {}).get(plan["indication"])
        scope_ok = bool(scope) and setting in scope.get("settings", []) and setting == plan["setting"]
        add("STD_SCOPE", "团体标准适用范围（冻结版本）", scope_ok, "hard",
            f"按冻结的 {std['code']} {std['version']} 核对：适应症"
            f"“{M.INDICATION_LABELS[plan['indication']]}”允许场所 {scope.get('settings') if scope else None}；"
            f"本次场所 {setting}（计划冻结场所 {plan['setting']}）",
            [{"standard": std["code"], "version": std["version"],
              "content_hash": std["content_hash"], "frozen": True}])
        add("STD_VERSION_PINNED", "标准版本未被升级追溯改写", True, "info",
            f"计划冻结于 {std['code']} {std['version']}（{plan['frozen_at']}）；"
            f"库内最新生效版本 {self.reg.standard_effective_version(std['code'])['version']}，"
            "新标准不自动适用于本计划",
            [{"frozen_version": std["version"],
              "current_version": self.reg.standard_effective_version(std["code"])["version"]}])

        # 2. 病房条件
        cert = ward.get("certifications", {}).get(plan["standard_version"]) if ward else None
        add("WARD_CERT", "病房条件认证", bool(cert and cert["status"] == "certified"), "hard",
            f"病房 {plan['ward_id']} 按 {plan['standard_version']} 的条件认证："
            f"{cert['status'] if cert else '缺失'}",
            [{"ward_id": plan["ward_id"], "certification": cert}])

        # 3. 伦理批件
        if ethics is None:
            add("ETHICS_MISSING", "伦理批件", False, "hard", "批件不存在", [])
        else:
            ok = (ethics["status"] == "active" and ethics["valid_until"] >= today
                  and ethics["hospital_id"] == plan["hospital_id"]
                  and plan["indication"] in ethics["scope"]["indications"]
                  and ethics["standard_version"] == plan["standard_version"])
            code = "ETHICS_REVOKED" if ethics["status"] == "revoked" else \
                   "ETHICS_EXPIRED" if ethics["valid_until"] < today else \
                   "ETHICS_SCOPE" if not ok else "ETHICS_OK"
            add(code if not ok else "ETHICS_OK", "伦理批件有效性", ok, "hard",
                f"批件 {ethics['approval_no']}：状态 {ethics['status']}，有效期至 "
                f"{ethics['valid_until']}，覆盖适应症 {ethics['scope']['indications']}",
                [{"approval_no": ethics["approval_no"], "status": ethics["status"],
                  "valid_from": ethics["valid_from"], "valid_until": ethics["valid_until"],
                  "standard_version": ethics["standard_version"]}])

        # 4. 患者授权
        if consent is None:
            add("CONSENT_MISSING", "患者知情授权", False, "hard", "授权记录不存在", [])
        else:
            ok = (consent["status"] == "active" and consent["case_id"] == case["case_id"]
                  and consent["template_version"] == plan["consent_template_version"])
            add("CONSENT_VERSION" if not ok else "CONSENT_OK", "患者知情授权", ok, "hard",
                f"授权 {consent['consent_id']}：状态 {consent['status']}，告知模板版本 "
                f"{consent['template_version']}（冻结要求 {plan['consent_template_version']}），"
                f"已补充告知版本 {consent['supplemented_versions']}",
                [{"consent_id": consent["consent_id"], "status": consent["status"],
                  "template_version": consent["template_version"],
                  "signed_at": consent["signed_at"]}])

        # 5. 设备批次与设备状态（含 SAE 隔离、冻结版本一致性）
        if batch is None or device is None:
            add("DEVICE_MISMATCH", "设备与批次", False, "hard", "冻结设备/批次不存在", [])
        else:
            add("BATCH_SAE_HOLD", "批次未因不良事件停用",
                batch["status"] == M.BATCH_ACTIVE, "hard",
                f"批次 {batch['batch_id']} 状态 {batch['status']}"
                + (f"；停用原因：{batch.get('hold_reason')}" if batch["status"] != M.BATCH_ACTIVE else ""),
                [{"batch_id": batch["batch_id"], "status": batch["status"],
                  "hold_reason": batch.get("hold_reason")}])
            # 冻结快照本身永不变更；仅当存在针对“最近一次变更单”的人工等价性接受决定时，
            # 才把当前版本视为在本计划下被接受（设备再次变化后旧接受因 change_id 不符而自动失效）。
            latest_change = next((h for h in reversed(device["history"])
                                  if h.get("change_type") in M.CHANGE_TYPES), None)
            acceptance = None
            if latest_change:
                acceptance = next((a for a in self.reg.change_acceptances.get(plan["plan_id"], [])
                                   if a["change_id"] == latest_change["change_id"]), None)
            same_versions = (device["firmware_version"] == plan["firmware_version"]
                             and device["algorithm_version"] == plan["algorithm_version"]
                             and device["calibration_id"] == plan["calibration_id"])
            same = same_versions and device["status"] == M.DEVICE_ACTIVE
            accepted = (not same_versions and acceptance is not None
                        and device["status"] == M.DEVICE_ACTIVE)
            code = "DEVICE_MISMATCH" if not (same or accepted) else \
                   "DEVICE_CHANGE_ACCEPTED" if accepted else "DEVICE_FROZEN_MATCH"
            add(code, "设备固件/算法/校准与冻结快照一致", same or accepted, "hard",
                (f"设备 {device['device_id']} 当前固件 {device['firmware_version']} / 算法 "
                 f"{device['algorithm_version']} / 校准 {device['calibration_id']}（状态 "
                 f"{device['status']}）；冻结快照 {plan['firmware_version']} / "
                 f"{plan['algorithm_version']} / {plan['calibration_id']}")
                + (f"；差异已经 {acceptance['approver']} 依证据 {acceptance['evidence_id']} "
                   f"接受为等价（决定 {acceptance['at']}），冻结快照不改写" if accepted else ""),
                [{"device_id": device["device_id"], "status": device["status"],
                  "current": {"firmware_version": device["firmware_version"],
                              "algorithm_version": device["algorithm_version"],
                              "calibration_id": device["calibration_id"]},
                  "frozen": {"firmware_version": plan["firmware_version"],
                             "algorithm_version": plan["algorithm_version"],
                             "calibration_id": plan["calibration_id"]},
                  "acceptance": acceptance}])

        # 6. 操作者资质
        if operator is None:
            add("OPERATOR_CREDENTIAL_MISSING", "操作者资质", False, "hard",
                f"操作者 {operator_id} 无资质记录", [])
        else:
            ok = (operator["valid_until"] >= today
                  and plan["indication"] in operator["scope"]["indications"]
                  and batch and batch["model"] in operator["scope"]["device_models"])
            code = ("OPERATOR_CREDENTIAL_EXPIRED" if operator["valid_until"] < today
                    else "OPERATOR_CREDENTIAL_SCOPE" if not ok else "OPERATOR_OK")
            add(code if not ok else "OPERATOR_OK", "操作者资质与授权范围", ok, "hard",
                f"操作者 {operator['operator_id']}：资质 {operator['credential_id']}，"
                f"有效期至 {operator['valid_until']}，范围 {operator['scope']}",
                [{"operator_id": operator["operator_id"], "credential_id": operator["credential_id"],
                  "valid_until": operator["valid_until"], "scope": operator["scope"]}])

        # 7. 临床观察（上次治疗后必须有观察记录；首次治疗除外）
        completed = sorted(self.reg.sessions.get(plan["plan_id"], []))
        obs = self.reg.observations.get(plan["plan_id"], [])
        observed_sessions = {o.get("session_seq") for o in obs}
        missing = [s for s in completed if s not in observed_sessions]
        if not completed:
            add("OBS_FIRST_SESSION", "临床观察（首次治疗）", True, "info",
                f"本计划尚无已完成治疗，按方案 {plan['observation_protocol_id']} 执行治疗中/后观察",
                [{"protocol_id": plan["observation_protocol_id"]}])
        elif missing:
            add("OBS_PRIOR_MISSING", "上一次治疗后临床观察", False, "hold",
                f"第 {missing} 次治疗结束后未见观察记录（方案 {plan['observation_protocol_id']}），"
                "可由医疗负责人限期补录并背书",
                [{"missing_session_seq": missing, "protocol_id": plan["observation_protocol_id"]}])
        else:
            add("OBS_PRIOR_OK", "上一次治疗后临床观察", True, "info",
                f"已完成 {len(completed)} 次治疗，观察记录齐全（方案 "
                f"{plan['observation_protocol_id']}）",
                [{"completed_sessions": completed, "observations": len(obs)}])

        # 8. 病例流程状态（暂停/补充告知/重新审批标记）
        for cat, flag in case["flags"].items():
            hard = flag["reason_code"] in M.HARD_BLOCK_RULES
            add(flag["reason_code"], f"病例流程标记：{cat}", False,
                "hard" if hard else "hold",
                flag["detail"], [{"flag": flag}])

        # 9. 人工背书（只对可背书项生效）
        active_overrides = []
        for ov in self.reg.overrides.get(plan["plan_id"], []):
            if ov["expires_at"] >= today:
                active_overrides.append(ov)
        used: list[dict[str, Any]] = []
        for r in rules:
            if not r.passed:
                ov = next((o for o in active_overrides
                           if o["rule_code"] == r.code and r.code in M.OVERRIDABLE_RULES), None)
                if ov:
                    r.passed = True
                    r.detail += f"；已由 {ov['approver']} 人工背书：{ov['rationale']}（有效期至 {ov['expires_at']}）"
                    used.append(ov)

        failed = [r for r in rules if not r.passed]
        hard_failed = [r for r in failed if r.severity == "hard"]
        if hard_failed:
            verdict = M.VERDICT_NO_GO
        elif failed:
            verdict = M.VERDICT_HOLD
        elif used:
            verdict = M.VERDICT_GO_OVERRIDE
        else:
            verdict = M.VERDICT_GO

        decision = {
            "check_id": f"CHK-{uuid.uuid4().hex[:12]}",
            "decided_at": self.now(),
            "case_id": case_id,
            "plan_id": plan["plan_id"],
            "setting": setting,
            "operator_id": operator_id,
            "verdict": verdict,
            "verdict_label": M.VERDICT_LABELS[verdict],
            "rules": [r.to_dict() for r in rules],
            "overrides_used": used,
            "frozen_plan": plan,
            "manual_decisions": case.get("resolutions", []) + used,
            "evidence_bundle": {
                "standard": {"code": std["code"], "version": std["version"],
                             "content_hash": std["content_hash"]},
                "ethics": ethics and {"approval_no": ethics["approval_no"],
                                      "status": ethics["status"], "valid_until": ethics["valid_until"]},
                "consent": consent and {"consent_id": consent["consent_id"],
                                        "template_version": consent["template_version"],
                                        "status": consent["status"]},
                "device": device and {"device_id": device["device_id"], "batch_id": batch["batch_id"],
                                      "firmware_version": device["firmware_version"],
                                      "algorithm_version": device["algorithm_version"],
                                      "calibration_id": device["calibration_id"],
                                      "status": device["status"], "batch_status": batch["status"]},
                "operator": operator and {"operator_id": operator["operator_id"],
                                          "credential_id": operator["credential_id"],
                                          "valid_until": operator["valid_until"]},
                "ward": {"ward_id": plan["ward_id"], "certification": cert},
                "observations": self.reg.observations.get(plan["plan_id"], []),
            },
            "explanation": self._explain(verdict, rules),
            "rule_engine_version": "1.0.0",
            "ledger_tail_hash": self.store.tail_hash(),
        }
        if persist:
            self._append("clearance.decided", {
                k: v for k, v in decision.items()
                if k in ("check_id", "decided_at", "case_id", "plan_id", "setting",
                         "operator_id", "verdict", "verdict_label", "rules",
                         "overrides_used", "frozen_plan", "manual_decisions",
                         "evidence_bundle", "explanation", "rule_engine_version",
                         "ledger_tail_hash")},
                {"id": (actor or {}).get("id", "clinician"), "role": (actor or {}).get("role", "")})
        return decision

    @staticmethod
    def _explain(verdict: str, rules: list[M.RuleResult]) -> list[str]:
        lines = [f"结论：{M.VERDICT_LABELS[verdict]}"]
        for r in rules:
            mark = "✔" if r.passed else ("✖" if r.severity == "hard" else "⚠")
            lines.append(f"[{mark}] {r.code} {r.title}：{r.detail}")
        return lines

    # ------------------------------------------------------------------ 审计
    def case_timeline(self, case_id: str) -> dict[str, Any]:
        plan_ids = set()
        case = self.reg.cases.get(case_id)
        if case:
            plan_ids.update(case["plan_history"])
        related = []
        for evt in self.store.events():
            d = evt.data
            if d.get("case_id") == case_id or (case and d.get("plan_id") in plan_ids) \
                    or d.get("consent_id") in {self.reg.plans[p]["consent_id"] for p in plan_ids if p in self.reg.plans}:
                related.append(evt.to_dict())
        return {
            "case_id": case_id,
            "case": case,
            "plan_snapshots": [self.reg.plans[p] for p in sorted(plan_ids) if p in self.reg.plans],
            "decisions": self.reg.decisions.get(case_id, []),
            "events": related,
        }

    def as_of_view(self, ts: str, case_id: str, setting=None) -> dict[str, Any]:
        """按时间点重放全部事件，还原“当时采用的规范/证据/设备状态”并重新核对。"""
        frozen = Registry()
        self.store.replay(frozen.apply, until_ts=ts)
        svc = _ReplayView(self.store, frozen, ts)
        return svc.evaluate_clearance(case_id=case_id, setting=setting, persist=False)


class _ReplayView(AdmissionService):
    """仅用于时点重放：投影与时钟都固定在给定历史时刻，且不允许写库。"""

    def __init__(self, store: EventStore, reg: Registry, ts: str):
        self.store = store
        self.reg = reg
        self._applied_ids: set[str] = set()
        self._clock = lambda: ts  # noqa: E731
