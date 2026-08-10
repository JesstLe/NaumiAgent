# HAR-09.6d3 Post-Rollback Long-Term Observation Assessment

## 目标

从 `HAR-09.6d2` 准入的 exact managed runtime startup chain 读取真实 Harness observation ledger，按照
`HAR-09.6d1` 冻结规则机械生成可持久化、可动态撤权的长期窗口评估。本切片首次记录长期 metrics 与 sustained-health
判定，但不签发 promoted Outcome，不授予 policy learning、promotion 或执行权限。

## Bounded ledger 读取

Service 先读取 exact Binding 与 heartbeat head，再从 admission origin 或最近 5000 条 suffix 分页：

1. 单页最多 500 条；
2. 每页必须绑定相同 Binding ID/SHA；
3. Harness Store 负责验证 sequence、previous-sample hash 与 content identity；
4. suffix 首样本保留非空 `slice_anchor_sha256`；
5. 读取完成后再次读取 heartbeat head 与 Binding；
6. 最后一条 sample 必须逐字段等于 heartbeat head；
7. head 或 Binding 在分页期间变化则失败关闭并重试，不签发混合快照。

最多 5000 条是证据边界，不是“忽略早期故障”。terminal `failed/stopped` 无法恢复到同一 incarnation 的 active phase；
对持续运行超过上限的进程，suffix 仍覆盖远大于 1 小时/12 样本的当前健康窗口，并通过 anchor 证明它来自同一 hash chain。

## 四态机械判定

优先级固定为：

1. `breached`：出现 `failed`、相邻 gap 超过 heartbeat timeout，或 active head 年龄超过 timeout；
2. `censored`：没有 breach，但最新 phase 为 `draining` 或 `stopped`；
3. `insufficient`：仍 active，但连续 operational duration 少于 3600 秒，或 operational samples 少于 12；
4. `passing`：当前 phase 为 `running/waiting`，时间与样本均达标，且无 gap/stale/failed。

`running` 与 `waiting` 都属于 operational phase。持续时间只计算当前连续 operational segment；starting、terminal 或其他
非 operational phase 不能被计入。`assessed_at` 不能早于 ledger head，最新样本年龄按向上取整秒数记录。

## Artifact、Store 与动态 authority

`EvolutionPostRollbackLongTermObservationAssessment` 内容寻址并嵌入 exact Contract、Admission 与 bounded sample slice，
同时记录：

- origin/suffix scope、anchor、first/last/head sequence 与 SHA；
- sample count、operational count、duration；
- maximum gap、latest age；
- insufficient、breach、censor reasons；
- historical status 与 assessed time。

Session Store 在 `BEGIN IMMEDIATE` 内逐字节复验 durable Contract/Admission JSON；写入前还从独立 Harness DB 重读 exact
sample slice。相同 head/clock 的并发评估收敛到同一 content identity，不使用 last-write-wins。
Latest receipt 先按 `ledger_head_sequence DESC` 单调选择，同一 head 再按 `assessed_at` 排序，避免同秒 head 推进时被
content ID 字典序错误覆盖。

View 每次 inspect 都重新执行 Contract、Admission、Binding、ledger/head 与当前时钟评估。历史 receipt 即使曾为 passing，
只要 head 变 stale、出现新 failed sample、上游撤权或 durable source 变化，`long_term_health_authority` 立即撤销。
`breached` 可产生只读 `health_alert_authority`，但不能自动停止进程或执行 rollback。

## 双通道与回执

- Agent Tool：`evolution_post_rollback_long_term_assessment`；
- 共享 Slash：`/evolution outcome-assess-long-term <rollback-request-id> <runtime-subject-id>`；
- New UI、CLI、TUI 走同一 Tool/Service；
- 评估只读取 ledger 并写治理 artifact，Moderate 与 Bypass 无二次确认。

回执展示当前四态、runtime、样本/operational 数、持续时间、gap、最新年龄和三类 reason，并始终明确
learning/promotion/execution authority 为 false。

## 验收标准

- [x] 真实 Harness Store 产生 startup + running/waiting/failed/stopped sample chain；
- [x] 1 小时、13 个连续 operational samples 产生 passing；
- [x] 短窗口产生 exact insufficient reasons；
- [x] gap 与 failed 产生 breached 和 health alert；
- [x] graceful stopped 产生 censored，不误报 breach/passing；
- [x] passing receipt 在 head 超时后动态撤销并变为 heartbeat-stale breach；
- [x] 分页后 head/Binding 二次读取与最后 sample 对账；
- [x] 502 条真实 ledger samples 跨过 500 条 page boundary 并保持 origin/head 连续；
- [x] 同 head 并发评估收敛；
- [x] durable row 篡改读取失败关闭；
- [x] Tool/Slash 共用 Service，Moderate/Bypass 无二次确认；
- [x] 相关小模块 pytest、ruff、compile、docs governance 与 diff check 通过；未运行全量测试。

## 当前边界与下一步

本切片可以授予动态 `long_term_health_authority` 或 `health_alert_authority`，但固定：

- `learning_authority=false`；
- `promotion_authority=false`；
- `execution_authority=false`。

下一独立切片 [HAR-09.6e1](HAR-09-6e1-post-rollback-long-term-outcome-authority.md) 已把 recovered Matrix 与当前
passing Assessment 组合成新的 durable Outcome revision，并建立 rolled_back → recovery-observed 的 append-only
supersede ledger。它仍不能绕过独立 promotion policy、人工 approval 或配置/数据 rollback 证据。
