# 脑机病房设备准入控制

本项目服务于整理脑机接口病房、病例授权和设备使用条件之间的关系。仓库保存领域资料、交换契约和可运行的准入后台，便于参与方在同一语义下协作。

## 领域事实

- 广东已有二十八家机构设立相关临床病房或中心
- 病房设置与管理规范开始编制
- 当前临床产品以非侵入式无创设备为主

## 准入后台

`src/admission/` 覆盖六类准入要素：团体标准、伦理批件、患者授权、设备批次、操作者资质、临床观察，并实现以下规则：

| 规则 | 实现 |
| --- | --- |
| 非侵入式产品使用范围（偏瘫/癫痫/脑卒中康复）冻结到一次治疗计划 | `service.create_plan` 冻结范围、标准版本与设备快照；`revise_plan_scope` 一律拒绝 |
| 标准升级不追溯放宽旧病例 | 计划按冻结版本评估；`standards.repin_plan_standard` 拒绝更宽松的绑定 |
| 固件/校准/算法版本变化自动找出受影响病例 | `impact.register_version_change`：固件→补充告知、校准→暂停、算法→重新审批 |
| 会诊专家与企业工程师只见职责内信息 | `rbac.py` 字段级与行级过滤 |
| 床旁断网操作日志不丢失 | `oplog.py` 本地哈希链日志池，恢复后幂等同步 |
| 严重不良事件立即阻断同批设备 | `adverse.report_adverse_event` 阻断批次并暂停同批计划 |
| 治疗前可解释放行结论 | `clearance.evaluate` 七道闸门逐项给出理由与证据 |
| 事后还原规范、证据、人工决定与设备状态 | `audit.reconstruct_decision` 读取结论快照与审计日志 |

## 目录说明

- `contracts/` 保存交换数据的结构约定（领域上下文、治疗计划、放行结论）。
- `fixtures/` 提供去标识的领域样例与种子数据。
- `src/catalog.py` 提供领域资料读取；`src/server.py` 提供 HTTP 入口。
- `src/admission/` 为准入后台领域代码。
- `tests/` 核对领域资料与全部业务规则。

## 本地检查

运行 `python3 -m unittest discover -s tests -v` 可以检查当前资料与服务入口。

## 运行服务

```bash
python3 -m src.server --port 8000 --seed fixtures/seed.json --store data/store.json
```

主要接口：

- `POST /api/plans` 建立治疗计划（范围即刻冻结）
- `POST /api/plans/{id}/clearance` 治疗前放行评估，返回可解释结论
- `POST /api/plans/{id}/manual-release` 人工放行（阻断期间禁止，且不得绕过未满足条件）
- `POST /api/plans/{id}/resolve` 完成补充告知 / 重新审批 / 恢复暂停
- `POST /api/plans/{id}/repin` 重新绑定标准版本（仅允许不更宽松）
- `POST /api/standards` 发布标准新版本（旧版本转为已替代）
- `POST /api/batches/{id}/changes` 登记版本变更，返回受影响病例清单
- `POST /api/batches/{id}/reinstate` 调查结案后恢复批次
- `POST /api/adverse-events` 登记不良事件（严重即阻断同批设备）
- `POST /api/logs/sync` 床旁离线日志同步
- `GET /api/plans/{id}` 按角色过滤的计划视图（`X-Role` / `X-Institution` / `X-Manufacturer` 头部，中文取值需 URL 编码）
- `GET /api/audit/decisions/{id}` 还原一次放行结论的完整上下文
- `GET /api/audit/plans/{id}/trail` 计划的全部审计痕迹
