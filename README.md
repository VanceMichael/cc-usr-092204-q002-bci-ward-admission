# 脑机病房设备准入后台

面向联合体二十八家已设脑机接口病房/中心的医疗机构，统一**病房条件、适用患者、异常处置**
的准入口径。系统围绕一次治疗计划做六类要素的冻结核对，给出治疗前可解释的放行结论，
并在事后可完整还原“当时采用的规范、证据、人工决定和设备状态”。

## 领域不变量

1. **使用范围冻结到一次治疗计划**：非侵入式产品在偏瘫、癫痫、脑卒中康复中的使用范围
   （适应症、场所、标准版本、伦理批件、授权告知版本、设备固件/算法/校准、操作者资质、
   观察方案）在 `plan.frozen` 时整体封存为不可变快照 `P-<病例>-V<n>`。
2. **标准升级不追溯**：团体标准发布新版本只增不改；旧计划放行始终按冻结版本核对，
   新版本既不会自动放宽、也不会由系统自动收紧旧病例（旧版本被*废止*属安全方向，会暂停待人工决定）。
3. **设备变更自动分级处置**：固件/传感器校准/算法版本变化按策略映射为
   `暂停待核验 / 补充告知 / 重新审批`，系统自动定位同设备（核验/告知级）或所需范围的在治病例。
4. **SAE 立即停用同批**：严重不良事件触发系统自动 `batch.quarantined`，同批全部在治病例
   立即暂停；该阻断为**硬阻断，不可人工背书**，批次解除后仍需逐例人工评估恢复。
5. **职责最小可见**：多科室会诊须逐病例授权；企业工程师只见本企业设备的技术字段，
   看不到病情与患者身份；补传通道有事件白名单，不能提权。
6. **断网不丢日志**：床旁操作先本地 JSONL 落盘 + 本地哈希链，重连按序幂等补传，
   崩溃重启后未确认文件自动重发。

## 架构

```
命令(HTTP/API) → AdmissionService（领域校验+副作用）→ EventStore（追加式哈希链 WAL）
                                       ↓ 重放
                                    Registry（投影：档案/病例/设备/批次…）
放行引擎 evaluate_clearance：按冻结快照 → 逐条规则 → go / go_with_override / hold / no_go
床旁 BedsideSpool：断网本地缓冲 → 恢复后 ingest_bedside 幂等入账
审计：case_timeline（全要素时间线）+ as_of_view（按时点重放还原“当时”）
```

- `src/models.py`：领域枚举、冻结计划 `FrozenPlan`、规则结果、硬阻断/可背书规则集。
- `src/eventstore.py`：线程安全、`fsync` 持久化、SHA-256 哈希链、幂等去重、防篡改校验、时点重放。
- `src/service.py`：投影 `Registry`、`AdmissionService`（命令、变更影响、SAE 守卫、放行引擎、审计）。
- `src/access.py`：字段级标签裁剪 + 行级范围（会诊授权、厂商隔离、机构隔离）。
- `src/bedside.py`：床旁断网缓冲与本地哈希链。
- `src/seed.py`：28 家机构与一份非侵入式产品基础档案/演示病例。
- `src/server.py`：HTTP 后台。`src/demo.py`：端到端演示。

## 六类准入要素 → 放行规则

| 要素 | 规则码（节选） | 硬阻断情形 |
|---|---|---|
| 团体标准/适用患者 | `STD_SCOPE`、`STD_VERSION_PINNED` | 适应症或场所超出**冻结版本**范围 |
| 病房条件 | `WARD_CERT` | 未按冻结标准版本认证/认证被撤销 |
| 伦理批件 | `ETHICS_OK/MISSING/REVOKED/EXPIRED/SCOPE` | 缺、撤、过期、机构/适应症/标准版本不符 |
| 患者授权 | `CONSENT_OK/MISSING/WITHDRAWN/VERSION` | 缺、撤回、告知模板版本与冻结要求不一致 |
| 设备批次 | `BATCH_SAE_HOLD`、`DEVICE_MISMATCH` | 同批 SAE 隔离；固件/算法/校准与快照不一致 |
| 操作者资质 | `OPERATOR_*`；临床观察 `OBS_PRIOR_MISSING` | 资质缺/过期/超范围；观察缺失仅 *hold*（可限期补录背书） |

`OBS_PRIOR_MISSING` 是唯一默认可由医疗负责人背书的暂缓项；其余失败均为硬阻断。

## 设备变更分级

| 变更 | 默认分级 | 系统动作 | 继续治疗的条件 |
|---|---|---|---|
| 固件 firmware | requalify 暂停待核验 | 设备挂起、同设备在治病例 `suspended` | 设备核验合格 + 医疗负责人登记版本等价性接受（快照不改写） |
| 传感器校准 calibration | requalify / 可配置 supplement | 暂停 或 `supplement_notice` | 核验接受；或患者补充签署后接受 |
| 算法 algorithm | reapprove 重新审批 | 在治病例 `reapproval_required` | 重新伦理审批 + 冻结**新计划版本**，不接受等价绕过 |

分级可由 `register_change_policy(model, change_type, review_class)` 按型号配置。

## HTTP 接口

- `POST /api/command` `{command, actor, args}`：通用命令入口（如 `report_sae`、`issue_standard`）。
- `POST /api/clearance` `{case_id, actor, setting?, operator_id?}`：治疗前放行，按 `actor` 角色裁剪字段。
- `POST /api/timeline` `{case_id, actor}`：病例全要素时间线（含行级权限校验）。
- `POST /api/replay` `{case_id, as_of}`：按时点重放并重新核对，还原当时状态。
- `POST /api/bedside/ingest` `{events:[...]}`：床旁断网日志幂等补传（仅白名单事件）。
- `GET /api/verify`：中心哈希链完整性；`GET /health`、`/context`、`/api/cases`、`/api/devices`。

启动：`python3 -m src.server`（日志路径由环境变量 `ADMISSION_LOG` 指定，默认 `data/admission_log.jsonl`）。

## 本地检查

```bash
python3 -m unittest discover -s tests -v   # 24 项：冻结/升级、变更分级、SAE、RBAC、断网、时点还原、防篡改
python3 -m src.demo                        # 端到端演示
```

## 说明

系统只保存去标识标识（如 `P-REDACTED-0001`），患者身份明文不进入本后台；
事件一经入链不可变，任何删改都会在 `verify_chain` 的哈希校验中暴露。
