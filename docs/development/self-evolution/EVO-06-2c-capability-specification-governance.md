# EVO-06.2c Capability Specification Governance

## 目标

在 EVO-06.2b 完整规格与 EVO-06.3 Sandbox registration 之间建立不可绕过的治理门：系统独立重读并
重放五条 Harness 人工答案，形成确定性的 mechanical Assessment；只有 Assessment 全部通过，才允许用户
作出 first-terminal-wins 的 approve/reject 决策。

`approved` 的含义严格限制为“可以进入 Sandbox 实现设计”。它不生成 Tool、不写 Registry、不允许
Shadow、不授予执行 authority，也不因 bypass 改变这些边界。

## 独立 Assessment

`CapabilitySpecificationAssessor` 不信任持久 Specification 的结构化字段本身，而是依次执行：

1. `specification_complete`：revision 5、五个连续步骤、无 pending step；
2. `candidate_lineage`：Specification ID、Candidate ID/revision/digest 与当前 Proposal 一致；
3. `interaction_cardinality`：五个步骤各有且仅有一条来源；
4. `interaction_integrity`：逐条从 Harness authority 重读，核对 subject、answered state、sequence、
   digest、answered time，且 `answered_by=user`；
5. `answer_replay`：用 EVO-06.2b 同一 validator 重新解析每条原始 JSON，结果与对应结构化步骤完全一致；
6. `authority_closed`：Specification/View 的 Sandbox、Shadow、Registry、execute authority 全部仍为 false。

Assessment ID 绑定 policy version、完整 Specification digest、Candidate lineage 和六项机械结果。检查详情
不参与结论计算；`eligible_for_decision` 必须等于六项全部通过。Assessment 本身不产生任何 authority。

## 人工决策状态机

```text
complete specification
  -> mechanical assessment failed -> blocked, no interaction
  -> assessment passed -> awaiting_decision
       -> approve option -> approved
       -> custom rejection reason -> rejected
       -> defer -> no terminal decision

approved/rejected + different later answer -> conflict, first terminal remains
approved/rejected + evidence/assessment changed -> historical decision shown as revoked
```

- approve 必须来自 Harness 中 `answered_by=user` 的明确 option；Agent Tool 只能请求该交互，不能自批；
- reject 必须使用用户自定义原因，最长 1000 字符，并拒绝 secret/control characters；
- defer 不写终态，下一次使用新的 interaction attempt；
- Decision ID 绑定 Assessment digest、治理 interaction digest、outcome 与 reason；
- Decision row 同时保存 canonical Assessment JSON/digest 和 Decision JSON/digest，读取时交叉验证；
- SQLite `BEGIN IMMEDIATE` 与 Specification 主键实现 first-terminal-wins；同 interaction 重试幂等，另一条
  并发 approve/reject 得到稳定冲突；
- 用户答案已进入 Harness、进程在写 Decision 前崩溃时，下一次调用会对账唯一有效答案；多个未对账终态
  拒绝猜测；
- 回答后、落库前再次重建 current Proposal、Specification 与 Assessment；其 ID/digest 变化时旧答案失效。

## 动态撤权与排名变化

- Portfolio rank/anchor 改变可能产生新 Proposal ID，但同一 Candidate revision/digest 与完整 Specification
  仍使用相同 Assessment，因此不丢失已完成治理；
- Candidate source authority、cooldown 或 Portfolio rankability 失效时，当前 Proposal 不再存在，治理入口
  fail closed；
- Specification payload、任一 Harness interaction 或答案重放结果变化，会产生不同 Assessment；历史
  Decision 仍保留审计，但 `decision_effective=false`、state=`revoked`、Sandbox 设计资格立即关闭；
- bypass 只影响普通工具权限确认，不会伪造 `answered_by=user`，也不会开启注册、Shadow 或执行 authority。

## 双通道与 UI

- 用户 Slash：`/evolution capability-govern <candidate-id>`；
- Agent Tool：`evolution_capability_governance(action='inspect|decide', candidate_id='<id>')`；
- New UI：typed `evolution/review/request` 的 `action=capability-govern` 复用 Harness 交互面板；Candidate detail
  显示六项 checks、Decision、有效/撤销状态，以及独立的“Sandbox 实现设计资格”；
- Textual/fallback：共享 Slash 与 `render_capability_governance()`，不维护第二套决策逻辑；
- Node protocol 严格核对 Python authority 提供的 Specification digest，并重算 Assessment ID/digest 和
  Decision ID，核对 Candidate lineage、interaction ID、false authority，并只保留白名单字段。由于 JSON
  传输会丢失 `1.0` 与 `1` 的词法差异，前端不伪称能从解析后的 number 独立重建 Python canonical digest。

## 验收标准与证据

1. 真实 SQLite + Harness 五条答案全部重放后才能产生 eligible Assessment；
2. 非人工来源、缺失/篡改 interaction、重放差异或 authority 提升任一项阻断；
3. approve、reject、defer 均从真实交互到 Store 闭环，reject 原因脱敏；
4. 回答后崩溃可精确恢复一次，回答期间撤权不落 Decision；
5. 两条并发终态只有一条成功，Assessment/Decision payload 篡改读取失败；
6. 历史 approve 在 evidence replay 变化后投影为 revoked，所有下游 authority 关闭；
7. Slash、Agent Tool、New UI、Textual/fallback 共用同一 service；
8. Node 拒绝 forged registration/Shadow/execute、错误 Assessment/Decision 身份，常见终端宽度不越界。

## 自我审视与后续

本切片证明“完整规格来自哪些用户答案、谁明确批准它进入实现设计”，但尚未证明 Tool 代码存在或满足
规格。`sandbox_design_eligible=true` 不是 `sandbox_eligible`，更不是 Registry authority。

下一最小切片 EVO-06.3a 应先定义临时 namespace、内置 Tool 名冲突拒绝、approved Decision source binding、
实现 artifact contract 与可撤销 Registry 状态机；只有真实实现通过 schema/permission/fixture 验证后，
后续切片才可执行 Sandbox registration。不得直接进入 Shadow 或 Limited Activation。
