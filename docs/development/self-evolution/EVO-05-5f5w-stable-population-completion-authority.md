# EVO-05.5f5w Stable Population Completion Authority

## 目标

把 5f5s–5f5v 的一次性只读候选预演收口为 durable Stable Population Completion Receipt。只有 exact current
signed Population 的全部 installation member 都存在 passing 5f5r Completion，并且每个成员通过默认生产 read graph 的动态
`inspect()`，才允许写入 Completion。

该 Receipt 只证明“这一组 Population/member source 已完整通过 stable-stage”。它不授权执行 stable rollout，不切换版本指针，
不执行 Git 写入，也不授予 promotion authority。

## Authority material

`EvolutionStablePopulationCandidatePreviewService.inspect_authority_material()` 在同一次读取中返回：

- 完整 Candidate Preview；
- 按 `installation_member_id` 稳定排序的全部 current 5f5r Receipts；
- 每个成员的 intent、subject、evidence identity 与内部 source-set digest。

用户界面仍可用 `limit=1..100` 截断展示，但 authority material 不截断，最多 10000 个成员。这样不会把“只显示前 100 项”
错误解释为“只验证前 100 项”。material 强制成员唯一、排序稳定、Snapshot identity 一致，且数量等于 Preview 的
`observed_members`。

## Durable Receipt

`EvolutionStablePopulationCompletionReceipt` 是 schema v1 的 strict、content-addressed artifact，冻结：

- canonical workspace；
- Population Snapshot ID/SHA/sequence/denominator；
- candidate version/target 与 Rollout Plan identity；
- Candidate Preview 的完整 durable source-set digest；
- 所有 member/intent/subject/evidence ID；
- 所有 5f5r evidence SHA 与各自内部 source-set SHA；
- issue 时的 Population、candidate-complete 和 dynamic-revalidation authority；
- deterministic `completed_at`，取全部成员 5f5r `assessed_at` 的最大值。

Receipt ID、Receipt SHA 和 Population source-set SHA 都由 canonical JSON 计算。相同 source-set 在不同进程、不同调用时间产生相同
Receipt，因此并发重试不会因 wall clock 形成冲突。Receipt 固定：

```text
stable_population_completion_fact = true
stable_rollout_authority = false
promotion_authority = false
```

本地 authority 不读取钥匙串或 installation private key；防篡改边界来自 content identity、原始 5f5r durable dependency 对账和
SQLite writer fence。未来若 Receipt 离开本机控制面，必须另加 exporter/signature envelope，不能把当前本地 Receipt 当作远程签名凭据。

## Writer fence 与幂等

Store 在同一 Evolution SQLite 中执行 `BEGIN IMMEDIATE`：

1. 确认 5f5r source table 存在；
2. 用 `intent_id -> MAX(rowid)` CTE 重建该 Snapshot 的完整 current set；
3. 逐条 strict 解析 evidence JSON，并核对列 identity；
4. 按 member 排序后与 Receipt 的全部 member/intent/subject/evidence/source SHA 精确比较；
5. 以唯一 `source_set_sha256` 幂等插入。

Preview 完成后、Receipt 写入前新增/替换任何 current 5f5r evidence，都会令事务内完整集合不同并拒绝签发。SQLite writer lock 阻止
对账与插入之间出现第二个本地 writer。相同 source-set 重试返回原 Receipt；同一 source-set 绑定不同内容则稳定报冲突。

## 动态 View 与撤权

Receipt 是历史事实，不重写；`EvolutionStablePopulationCompletionService.inspect()` 每次重新检查：

- Receipt durable source 仍可认证；
- 它仍是该 Snapshot 最新 Completion；
- Snapshot identity、latest-for-channel、trust 与 validity 当前有效；
- Population membership 和 candidate completeness 当前一致；
- 全部 member source-set 未变化；
- 全部 5f5r View 仍有 dynamic authority。

任一条件失败，`stable_population_completion_authority=false`，并返回有界、稳定排序的 invalidation reason。新增 Snapshot、撤销
Registry trust、5f5r evidence/source 漂移、成员冲突和检查端口失败都会动态撤权；历史 Receipt 仍保留用于审计，但不能继续进入后续
rollout authorization。

## Agent Tool 与 Slash

- Agent Tool：`evolution_stable_population_completion`；
- 签发：`/evolution stable-population-completion complete [population-snapshot-id]`；
- 重验：`/evolution stable-population-completion inspect <completion-receipt-id>`；
- 两条通道调用同一 Service，并使用 `render_stable_population_completion()`；
- Store 写入 non-destructive、并发安全，不需要高风险二次确认；
- 输出明确显示 Completion、Stable rollout 和 Promotion 三种 authority，后两者始终为 false。

Engine 默认复用 5f5v 的 source-lazy read graph；空候选仍不会初始化图或读取 trust artifact。

## 验收标准

- 展示 limit 不影响完整 authority member set；
- 两成员 signed Population 可以签发 exact Receipt；
- 四个并发 complete 调用得到同一 Receipt，数据库仅一行；
- writer fence 能拒绝 Preview 后新增的 current 5f5r source；
- 5f5r 列/JSON 篡改稳定失败关闭；
- 单成员动态失权后历史 Receipt 不变，但 View 立即撤权；
- 新 Population Snapshot 或 Registry trust 变化立即撤权；
- Tool 与 Slash 输出同一 renderer；
- Engine 默认组合 Store、Service 和 Tool；
- public lazy exports、Ruff、compile、YAML 和相关小模块测试通过；
- 不运行全量测试。

## 自我审视与下一步

本切片形成了 durable Population Completion，但它不是 rollout executor 的授权票据，也没有 expected-pointer、release slot、kill switch、
rollback plan 或 single-use execution nonce。当前 content-addressed Receipt 适用于同一本地控制面；尚未提供跨控制面签名/透明日志证明。

[EVO-05.5f5x1](EVO-05-5f5x1-stable-rollback-readiness.md) 已先完成最小安全前置：消费 current 5f5w View 与 current Stable
Deployment，绑定真实 expected active pointer、retained prior slot 和原始 Boot Receipt，形成 binary-only Readiness。健康 rollout
不消费 6a breach-only Rollback Request；配置/数据 rollback authority 仍关闭。

下一最小切片是 EVO-05.5f5x2 Stable Rollout Authorization：重新消费 current 5f5w View 与 5f5x1 Readiness，冻结 kill-switch
generation、短期 expiry 和 single-use nonce；执行器必须在 CAS 前再次动态重验。Promotion authority 继续独立，不能与 stable
rollout authorization 合并。
