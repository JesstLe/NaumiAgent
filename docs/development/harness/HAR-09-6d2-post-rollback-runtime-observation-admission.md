# HAR-09.6d2 Post-Rollback Runtime Observation Admission

## 目标

把 `HAR-09.6d1` 的长期观察契约绑定到一个真实 managed New UI/TUI runtime 和它的 startup-origin
heartbeat chain。该 admission 是契约规则与 Harness observation ledger 之间的最小桥梁：它证明“将要观察的是哪一个
回滚 baseline 进程”，但仍不聚合持续时间、样本覆盖或长期健康 verdict。

## 为什么不能比较两个 identity SHA

Post-Rollback Runtime Verification 的 digest 只覆盖 rollback recovery probes 的 slot/pointer/binary 共同字段；
Harness `ReleaseRuntimeIdentity` 还覆盖 runtime path、install root、boot receipt 和 terminal-process checks。两者 schema
不同，因此 digest 天然不同。Admission 必须逐字段匹配：

- slot ID / slot SHA；
- version / target；
- active pointer ID / SHA / generation；
- backend binary SHA。

任何字段不一致均返回 `post_rollback_runtime_admission_baseline_mismatch`，不能以 version 字符串相同或某一个 digest
相似为理由准入。

## Startup origin 约束

Service 从 `HarnessStore` 读取 exact `HarnessRuntimeReleaseBinding`，再读取 after-sequence 0、limit 1 的 verified page。
准入要求首样本同时满足：

1. binding/runtime identity/surface/subject/instance/epoch 全部一致；
2. `chain_origin_kind=startup`、`chain_origin_sequence=1`；
3. `heartbeat_sequence=1`、`phase=starting`；
4. `previous_sample_sha256` 为空；
5. origin time 不早于 6d1 的 `window_not_before_at`；
6. sample 由 Harness Store 的连续性与 content identity 读取路径验证。

这排除了 legacy snapshot、任意 suffix、不同 runtime incarnation、仅有 latest heartbeat 而无完整 ledger 等弱证据。
如果当前 UI runtime 早于 Contract，Service 返回独立的 `origin_predates_contract` 错误并明确提示用户重启 Naumi 后
准入新的 managed runtime；不会用旧进程已经存活的时间补算长期窗口。

## Artifact 与 authority

`EvolutionPostRollbackRuntimeObservationAdmission` 内容寻址并冻结：

- Outcome、Request、6d1 Contract ID/SHA；
- Harness Binding 与 Release Runtime Identity ID/SHA；
- surface、subject、instance、epoch；
- 逐字段 baseline runtime identity；
- startup origin sample ID/SHA/time 与 heartbeat timeout；
- deterministic admitted time。

Session SQLite 的 Store 在 `BEGIN IMMEDIATE` 内复验 exact durable 6d1 Contract，同一 Outcome/subject 的并发写入
必须收敛。Harness ledger 位于独立 `harness.db`，不能伪装成跨库原子事务；Service 采用“写前读取 + 写后 inspect”动态
复验。Contract、binding 或 origin 任一变化，view 立即变为 `stale`。

Artifact 只记录 `runtime_observation_input_recorded=true`。动态 view 在所有条件当前仍成立时授予
`runtime_observation_input_authority=true`；它始终固定 window、long-term metrics、learning、promotion、execution authority
为 false。

## 双通道与用户体验

- Agent Tool：`evolution_post_rollback_runtime_admission`；
- 共享 Slash：`/evolution outcome-admit-runtime <rollback-request-id> <runtime-subject-id>`；
- New UI、CLI、TUI 使用同一 Tool/Service；
- 只生成 governance artifact，不启动或停止 runtime，Moderate 与 Bypass 均不二次确认。

回执展示 Contract、Binding、surface/subject、instance/epoch、baseline、origin、timeout 与当前动态 authority，避免用户把
“已准入观察输入”误读成“长期健康已经通过”。

## 验收标准

- [x] 使用真实 Harness Store 原子写入 Binding、starting heartbeat 与 origin sample；
- [x] exact shared baseline fields 全量逐字段匹配；
- [x] startup sequence-1/origin/hash 约束完整；
- [x] Contract 与 origin time 的 not-before 边界生效；
- [x] 同一 Outcome/subject 并发独立 Service 写入收敛；
- [x] 不同 binary baseline 失败关闭且不写 Admission；
- [x] Contract authority 撤销或 origin 丢失令既有 Admission stale；
- [x] durable row index tamper 读取失败关闭；
- [x] Tool/Slash 共用 Service，Moderate/Bypass 无二次确认；
- [x] 相关小模块 pytest、ruff、compile、docs governance 与 diff check 通过；未运行全量测试。

## 下一步

`HAR-09.6d3` 应从 Admission 的 exact origin 开始分页读取最多 5000 个 ledger samples，验证 binding、sequence、hash、
timeout 和不早于窗口锚点，然后按 6d1 规则机械生成 `insufficient / passing / breached / censored` 长期窗口评估。
6d3 必须保留 `assessed_at`、verified cursor 与读取 head，支持当前 liveness 变化时动态撤权；不得仅把 admission 数量或
进程仍存在当成长期开窗成功。
