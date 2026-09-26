"""准入后台服务：把领域模块装配成可运行的后台门面。"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone

from src.admission import adverse, clearance, impact, oplog, rbac, standards
from src.admission.audit import plan_trail, reconstruct_decision
from src.admission.models import (
    OPERATOR_LEVELS,
    WARD_LEVELS,
    BatchStatus,
    Consent,
    ConsentStatus,
    CaseFile,
    DeviceBatch,
    EthicsApproval,
    Indication,
    Institution,
    Observation,
    Operator,
    PlanStatus,
    TreatmentPlan,
)
from src.admission.store import Store

DEFAULT_STANDARD_ID = "T/BCI-WARD"


class ScopeFrozenError(ValueError):
    """治疗计划范围已冻结，任何调整须终止当前计划并另行建档。"""


class AdmissionService:
    def __init__(self, store: Store | None = None, clock=None):
        self.store = store or Store()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.central_log = oplog.CentralLog(self.store)

    def _now(self) -> datetime:
        return self.clock()

    def _ts(self) -> str:
        return self._now().isoformat()

    # ---- 基础档案 ----

    def add_institution(self, *, institution_id=None, name, ward_level, emergency_kit):
        if ward_level not in WARD_LEVELS:
            raise ValueError(f"未知病房等级：{ward_level}")
        institution_id = institution_id or self.store.next_id("INST")
        record = Institution(institution_id, name, ward_level, emergency_kit)
        self.store.put("institutions", institution_id, record)
        return asdict(record)

    def add_ethics_approval(
        self,
        *,
        approval_id=None,
        institution_id,
        protocol_no,
        indications,
        device_models,
        valid_from,
        valid_to,
        status="有效",
    ):
        self.store.get("institutions", institution_id)
        approval_id = approval_id or self.store.next_id("ETH")
        record = EthicsApproval(
            approval_id,
            institution_id,
            protocol_no,
            list(indications),
            list(device_models),
            valid_from,
            valid_to,
            status,
        )
        self.store.put("ethics", approval_id, record)
        return asdict(record)

    def add_case(self, *, case_ref=None, institution_id, age, sex, conditions):
        self.store.get("institutions", institution_id)
        case_ref = case_ref or self.store.next_id("CASE")
        record = CaseFile(case_ref, institution_id, int(age), sex, list(conditions))
        self.store.put("cases", case_ref, record)
        return asdict(record)

    def add_consent(
        self,
        *,
        consent_id=None,
        case_ref,
        scope,
        device_snapshot,
        signed_at=None,
        status=ConsentStatus.VALID,
    ):
        self.store.get("cases", case_ref)
        consent_id = consent_id or self.store.next_id("CON")
        record = Consent(
            consent_id,
            case_ref,
            list(scope),
            dict(device_snapshot),
            signed_at or self._ts(),
            status,
        )
        self.store.put("consents", consent_id, record)
        return asdict(record)

    def add_batch(
        self,
        *,
        batch_id=None,
        model,
        manufacturer,
        firmware_version,
        calibration_version,
        algorithm_version,
    ):
        batch_id = batch_id or self.store.next_id("BAT")
        record = DeviceBatch(
            batch_id,
            model,
            manufacturer,
            firmware_version,
            calibration_version,
            algorithm_version,
            BatchStatus.ACTIVE,
        )
        self.store.put("batches", batch_id, record)
        return asdict(record)

    def add_operator(self, *, operator_id=None, name, institution_id, qualifications):
        self.store.get("institutions", institution_id)
        for qual in qualifications:
            missing = {"device_model", "indication", "level", "valid_to"} - set(qual)
            if missing:
                raise ValueError(f"资质条目缺少字段：{sorted(missing)}")
            if qual["level"] not in OPERATOR_LEVELS:
                raise ValueError(f"未知操作者等级：{qual['level']}")
            if qual["indication"] not in Indication.ALL:
                raise ValueError(f"资质治疗范围无效：{qual['indication']}")
        operator_id = operator_id or self.store.next_id("OP")
        record = Operator(operator_id, name, institution_id, list(qualifications))
        self.store.put("operators", operator_id, record)
        return asdict(record)

    def add_observation(self, *, plan_id, kind, recorded_by, summary, obs_id=None):
        self.store.get("plans", plan_id)
        obs_id = obs_id or self.store.next_id("OBS")
        record = Observation(
            obs_id, plan_id, kind, recorded_by, self._ts(), summary
        )
        self.store.put("observations", obs_id, record)
        return asdict(record)

    # ---- 团体标准 ----

    def publish_standard(self, **fields):
        record = standards.publish_standard(self.store, now=self._ts(), **fields)
        return asdict(record)

    def repin_standard(self, plan_id, version, actor):
        record = standards.repin_plan_standard(
            self.store, plan_id, version, now=self._ts(), actor=actor
        )
        return asdict(record)

    # ---- 治疗计划 ----

    def create_plan(
        self,
        *,
        case_ref,
        institution_id,
        indication,
        ethics_approval_id,
        consent_id,
        batch_id,
        operator_id,
        standard_id=DEFAULT_STANDARD_ID,
        consult_flag=False,
    ):
        """建立一次治疗计划：范围、标准版本与设备版本快照即刻冻结。"""
        if indication not in Indication.ALL:
            raise ValueError("非侵入式产品仅可用于偏瘫、癫痫或脑卒中康复")
        self.store.get("cases", case_ref)
        self.store.get("institutions", institution_id)
        self.store.get("ethics", ethics_approval_id)
        consent = self.store.get("consents", consent_id)
        if consent.case_ref != case_ref:
            raise ValueError("授权书与病例不匹配")
        batch = self.store.get("batches", batch_id)
        self.store.get("operators", operator_id)
        standard = standards.current_standard(self.store, standard_id)

        snapshot = {
            "firmware_version": batch.firmware_version,
            "calibration_version": batch.calibration_version,
            "algorithm_version": batch.algorithm_version,
        }
        plan = TreatmentPlan(
            plan_id=self.store.next_id("PLAN"),
            case_ref=case_ref,
            institution_id=institution_id,
            indication=indication,
            standard_id=standard_id,
            standard_version=standard.version,
            ethics_approval_id=ethics_approval_id,
            consent_id=consent_id,
            batch_id=batch_id,
            operator_id=operator_id,
            device_snapshot=snapshot,
            status=PlanStatus.ACTIVE,
            created_at=self._ts(),
            consult_flag=consult_flag,
        )
        self.store.put("plans", plan.plan_id, plan)
        self.store.log(
            "system",
            "plan_created",
            {"plan_id": plan.plan_id, "standard_version": standard.version},
            self._ts(),
        )
        return asdict(plan)

    def revise_plan_scope(self, plan_id, **_changes):
        """治疗计划范围一经冻结不得修改；调整须终止后另行建档。"""
        self.store.get("plans", plan_id)
        raise ScopeFrozenError(
            "治疗计划范围已冻结：任何范围调整须终止当前计划并另行建档"
        )

    # ---- 放行与人工决定 ----

    def evaluate(self, plan_id, decided_by="system"):
        decision = clearance.evaluate(self.store, plan_id, self._now(), decided_by)
        return asdict(decision)

    def manual_release(self, plan_id, actor, reason):
        """人工放行：记录人工决定，但不得绕过未满足的准入条件，
        严重不良事件阻断期间一律禁止放行。"""
        plan = self.store.get("plans", plan_id)
        batch = self.store.get("batches", plan.batch_id)
        if batch.status == BatchStatus.BLOCKED:
            raise PermissionError("严重不良事件阻断期间禁止人工放行")
        decision = clearance.evaluate(self.store, plan_id, self._now(), actor)
        if not decision.allowed:
            raise ValueError("存在未满足的准入条件，人工放行不得绕过：" + decision.conclusion)
        decision.manual = True
        self.store.log(
            actor,
            "manual_release",
            {"plan_id": plan_id, "reason": reason, "decision_id": decision.decision_id},
            self._ts(),
        )
        return asdict(decision)

    # ---- 变更、事件与恢复 ----

    def change_batch_versions(self, batch_id, changes, reason, actor="engineer"):
        return impact.register_version_change(
            self.store, batch_id, changes, reason, self._ts(), actor
        )

    def report_adverse(self, plan_id, severity, description, actor="system"):
        return adverse.report_adverse_event(
            self.store, plan_id, severity, description, self._ts(), actor
        )

    def reinstate_batch(self, batch_id, reason, actor):
        record = adverse.reinstate_batch(self.store, batch_id, reason, self._ts(), actor)
        return asdict(record)

    def resolve_supplement(self, plan_id, note, actor):
        record = impact.resolve_supplement(self.store, plan_id, note, self._ts(), actor)
        return asdict(record)

    def resolve_reapproval(self, plan_id, note, actor):
        record = impact.resolve_reapproval(self.store, plan_id, note, self._ts(), actor)
        return asdict(record)

    def resume_plan(self, plan_id, actor):
        record = impact.resume_plan(self.store, plan_id, self._ts(), actor)
        return asdict(record)

    # ---- 视图、日志与审计 ----

    def plan_view(self, plan_id, role, **ctx):
        plan = self.store.get("plans", plan_id)
        return rbac.plan_view(role, asdict(plan), ctx)

    def case_view(self, case_ref, role, **ctx):
        case = self.store.get("cases", case_ref)
        return rbac.case_view(role, asdict(case), ctx)

    def batch_view(self, batch_id, role, **ctx):
        batch = self.store.get("batches", batch_id)
        return rbac.batch_view(role, asdict(batch), ctx)

    def sync_logs(self, terminal_id, entries):
        return self.central_log.ingest(terminal_id, entries)

    def reconstruct(self, decision_id):
        return reconstruct_decision(self.store, decision_id)

    def plan_trail(self, plan_id):
        self.store.get("plans", plan_id)
        return plan_trail(self.store, plan_id)
