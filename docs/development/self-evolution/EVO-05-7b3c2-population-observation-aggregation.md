# EVO-05.7b3c2 Population Observation Aggregation

## 1. 目标与依赖裁决

7b3c1 已能从 Control Plane 完整验签 ledger 形成单 installation 的
`insufficient / passing / breached / censored` 结论，但单安装结论不知道 exact Population denominator，不能证明其他成员是否缺失，
也不能把一台机器的通过冒充 fleet 长期健康。

本切片只消费：

- current `EvolutionStableRemotePopulationFinalizationReceipt` 的 exact member set；
- current `EvolutionStablePromotionObservationContract` 的 denominator、Snapshot 和一小时观察规则；
- 每个 member 的 durable Runtime Admission；
- 每个 Admission 的 latest 7b3c1 Installation Assessment。

输出 content-addressed `EvolutionStablePromotionPopulationObservationAssessment` 和动态 View。它记录 Population 长期指标，
但不签发 promoted Outcome、learning、promotion 或 execution authority。下一独立切片 7b4 才能建立 promoted/superseded ledger。

## 2. exact member set 与五态

denominator 和成员身份只来自 current Population Finalization Receipt，不从“已有 Admission 数量”倒推。每个 exact member 投影为：

| 状态 | 机械条件 |
|---|---|
| `missing` | 无 Admission、无 Assessment，或非终态 Assessment 已超过 heartbeat deadline |
| `insufficient` | current Installation Assessment 尚未满足一小时或 12 个 operational samples |
| `passing` | current Installation Assessment 满足 duration、sample、gap 和 latest-age 规则 |
| `breached` | current Installation Assessment 发现 failed、heartbeat stale 或其他 breach |
| `censored` | current Installation Assessment 以 draining/stopped 终止观察 |

缺失原因进一步区分 `runtime_admission_missing`、`installation_assessment_missing` 和
`installation_assessment_expired`。同一 exact member 如果出现多个 durable Admission，不采用“最新获胜”；Service 与 writer 均以治理冲突失败关闭，等待后续显式 supersession。

## 3. durable member projection

Population Receipt 不复制每个 7b3c1 Assessment 的完整 revision ledger，避免 10000 member 时发生二次指数膨胀。每个 member 只冻结：

- exact finalization member/credential identity；
- finalization member source SHA；
- optional Admission ID/SHA；
- optional Installation Assessment ID/SHA；
- 五态、缺失原因、sample/operational sample/observation seconds；
- last observed time、heartbeat deadline 与签发时是否 current；
- member projection 自身 content SHA。

完整单安装证据仍由 7b3c1 Store 保存，Population Receipt 通过 ID/SHA 引用并在每次 inspect 时动态重验。

## 4. 群体指标与状态优先级

聚合器只使用整数运算：

- `assessment_coverage_bps = current_assessment_count * 10000 // denominator`；
- `duration_coverage_bps = sum(min(observation_seconds, 3600)) * 10000 // (denominator * 3600)`；
- passing、breached、censored、insufficient、missing 精确计数；
- `members_meeting_duration` 与稳定排序 `missing_member_ids`；
- 非终态 current member 的最早 heartbeat deadline 作为 Population `valid_until`。

Population 状态优先级为 `breached > censored > insufficient > passing`。因此任何 breach 都不会被多数 passing 稀释；无 breach
但有 censored 时不会伪装成通过；任意 missing/insufficient 都保持不足；只有 exact denominator 全部 passing 才是 passing。

## 5. SQLite writer fence

Population Store 与 Contract、Finalization、Admission、Installation Assessment Store 必须共享 session SQLite。
`record()` 使用 `BEGIN IMMEDIATE`，在写入前重新读取：

1. exact Contract JSON 与 SHA；
2. exact Population Finalization JSON 与 SHA；
3. 该 Contract 最多 10001 条 Admission，10001 立即拒绝；
4. Admission 是否只属于 exact finalization member set，且每 member 最多一条；
5. 每个 Admission 的 latest Installation Assessment；
6. 以 Receipt 的 `assessed_at` 重新机械投影全部 member，并与待写 tuple 完整比较。

writer 以 source-set SHA 幂等收口。同一 source-set 的并发 Service 只能得到一行同内容 Receipt；Contract、Finalization、Admission
或 Assessment 在 inspect 与 writer 之间变化会被同一事务拒绝。

## 6. 动态 View 与撤权

`inspect()` 每次重验：

- durable Population Assessment JSON 与行身份；
- 它仍是该 Contract 最后写入的 Assessment（SQLite `rowid`，不使用 content ID 猜时间）；
- Observation Contract authority；
- Population Finalization authority 与 ID/SHA；
- 全部 Admission/Installation Assessment source-set；
- Population 最早 heartbeat deadline。

任一项变化后历史 Receipt 保留，但 `current_assessment=None`。只有 current `passing` 才授予
`population_long_term_observation_authority`；只有 current `breached` 才授予 `population_health_alert_authority`。
missing、insufficient、censored 或 stale Receipt 都不授予两者。

## 7. Agent Tool、Slash 与前端同源

- Agent Tool：`evolution_stable_promotion_population_observation_assessment`；
- 签发：`/evolution stable-promotion-population-observation assess <population-finalization-receipt-id>`；
- 重验：`/evolution stable-promotion-population-observation inspect <population-assessment-id>`；
- Tool 与 Slash 共用同一 Service/renderer，New UI 与 Textual TUI 继续消费共享 Tool Result；
- permissive/moderate/strict/bypass 均允许且不二次确认，lockdown 拒绝；
- 操作只新增聚合证据，non-destructive、concurrency-safe。

## 8. 验收证据

- [x] 四成员真实 denominator 中，1 个 Admission、3 个缺失时准确记录四个 missing；
- [x] 单安装 Assessment 出现后 coverage 从 0 bps 更新到 2500 bps，3 个缺员仍保留；
- [x] 五态混合投影按 breach 优先，整数 duration coverage 可机械重算；
- [x] 两个 Service 并发签发同一 source-set 幂等收口；
- [x] 新 Assessment 使旧 Population Receipt 的 source-set 动态撤权；
- [x] 非终态 heartbeat deadline 到期使 Population Receipt 动态撤权；
- [x] forged member content SHA 被 strict model 拒绝；
- [x] Engine 默认组合 Store/Service，public lazy exports、Agent Tool、Slash 和权限同源；
- [x] targeted Ruff、compile、快速投影/Engine 测试和真实小模块测试通过；
- [x] 按用户要求未运行全量测试。

## 9. 自我审视与后续边界

本切片没有把部分覆盖包装成“群体通过”，也没有复制庞大的逐 revision ledger。但仍有明确边界：

- 同 member 多 Admission 当前 fail closed，尚无显式 supersession/仲裁 workflow；
- Population Receipt 仍是 Control Plane 本地 durable artifact，没有 exporter/signature envelope；
- missing member 没有主动追赶调度，本切片只诚实暴露 coverage；
- alert 是当前 breach authority，不等于 promoted Outcome，也不会自动回滚；
- 7b4 仍需 append-only promoted/superseded ledger、人工/策略决策边界和后续 policy learning gate。

下一步最小前置 [EVO-05.7b4a Stable Promotion Outcome Eligibility](EVO-05-7b4a-stable-promotion-outcome-eligibility.md)
已实现：它只把 current passing 转成独立审批资格。后续 7b4b/7b4c 仍必须分别完成 Decision 与 ledger，不能继续扩张
7b3c2 或把 rollback recovery Outcome 复用于成功推广路径。
