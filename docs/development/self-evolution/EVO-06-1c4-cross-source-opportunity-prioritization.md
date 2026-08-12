# EVO-06.1c4 跨来源机会聚类与可解释优先级

## 状态

Implemented。本切片把 Candidate Review 从“按最近观察时间列出”升级为确定性、只读、可复放的 30 天
Opportunity Portfolio。它消费已经通过 Candidate Store 完整性校验、动态 Source Authority 与 Workbench
冷却 Gate 的 Candidate，不创建新 Evidence、不修改 Candidate、不签发 Experiment，也不把排序结果当成执行权限。

## 问题与边界

EVO-06.1a–1c3 已分别产生 rollback、stable promoted、H5c regression、durable Goal need 与 exact Tool
Catalog miss，但过去 Review 在 `--limit` 前按 `last_observed_at` 截断。较新的低质量信号可能遮住较旧但真实可审阅的
问题；Candidate Aggregation 也只描述单 Candidate 趋势，明确不代表严重度或优先级。

本模块解决“当前应该先审阅什么”，不声称解决语义同根因合并。Goal objective 只保留脱敏摘要，Tool Catalog 只保留
安全工具名；在没有可验证语义桥的前提下，系统不能假装知道二者指向同一个具体能力。因此 Cluster 是可解释的
`capability/performance/reliability/safety/correctness/maintainability` 机会域，不会把成员合并为一个 Candidate。

## 有界权威快照

1. Candidate Store 最多读取最近 500 条通过摘要与审计链校验的 Candidate；超过范围的旧详情仍可查看，但明确显示
   “不在当前 500 条有界快照中”，不伪造全局排名。
2. Anchor、排名与 Cluster 先基于完整 500 条全局快照形成；query/risk/source 只过滤可见 Candidate，`--limit`
   在全局排序后应用。因而列表过滤与详情保持同一个 P-rank，不会通过缩小过滤范围让旧 Candidate 重新变成 P1。
3. Anchor 固定为全局集合中最新 `last_observed_at`，窗口为 `(anchor-30d, anchor]`；同一 Store 快照反复读取结果
   一致，不因机器当前时间变化而漂移。
4. 每条 Candidate 在评分前重新读取动态 Source Authority 与 Workbench cooldown；失效、读取异常、受保护 scope、
   证据不足或 verifier 不成立均不参与排名。
5. 动态 authority 验证并发上限为 16，避免 500 条快照同时压垮 Goal、Outcome、Eval 或 Tool Catalog reader。

## Priority Policy v1

固定公式：

```text
score = severity * frequency * confidence * 5 / (implementation_cost * change_risk)
```

Score 以整数 basis points 计算后投影为 `0..100`，避免浮点比较决定排名。相同分数按 severity、Candidate ID
稳定排序。所有因子和公式都进入 Python Review、typed UI payload、New UI 与 TUI 详情。

### Severity

- safety：5；correctness/reliability：4；capability：3；maintainability：2；
- rollback guardrail breach 与 secret/prohibited scope 固定为 5；
- Eval regression 固定为 4；stable promotion 的下一轮改进固定为 2。

Severity 表达问题影响，不复用 Candidate `risk`。Candidate risk 表达修改风险，作为独立分母，避免“高风险修改”被
误解为“问题更值得自动执行”。

### Frequency 与反刷榜

- 只统计窗口内、已注册可信来源的 `(authority lane, UTC date)` 唯一组合；
- 同一天同一 lane 的 1 次与 10,000 次观察都只计 1；
- frequency 上限为 4，长期存在不会无限占据队首；
- `agent_interpreted_feedback` 不进入 frequency，也不能单独参与排名；
- occurrence、token、模型调用次数和 Cluster 成员数量都不进入分数。

### Confidence

| Authority lane | 基础置信度 |
| --- | ---: |
| verified rollback/promoted Outcome | 100 |
| 95% CI quantitative Eval | 95 |
| Harness failure | 90 |
| deterministic static scan | 80 |
| explicit Goal/direct user feedback | 75 |
| exact Tool Catalog fact | 55 |

同一 Candidate 若确有多个独立 lane，第二个起每 lane 增加 5，最高 100；重复同类来源不增加 confidence。
Tool Catalog miss 仍只证明“目录中没有该名字”，低置信度进入人工排序，不获得造工具或实验权限。

### Cost 与 Change Risk

Implementation cost 是版本化的粗粒度启发式：maintainability 2、correctness 3、reliability 4、
capability/safety 5。Change risk 使用 Candidate 已确定的 low/medium/high/critical → 1/2/3/4。它们用于人工
队列的相对排序，不是工期承诺或生产风险预测。

## Opportunity Cluster 与影响范围

每个 rankable Candidate 投影到稳定 domain；`stable_promotion_improvement` 进入 capability，Eval regression
进入 performance，rollback breach 进入 reliability。Cluster ID 只由 policy version + domain 生成，成员变化不会
改变域身份。

Cluster 公开：

- 主 Candidate 与成员 Candidate ID；
- 窗口内可信 source kinds；
- Candidate、source、scope、provider、model、platform 唯一计数；
- Cluster score = 成员最大 score，而不是求和或平均。

取最大值保证新增大量低分 Candidate 不会抬高整个域。影响计数只帮助用户判断 blast radius，不参与排名。

## 双通道与 UI

- 用户：`/evolution priorities [--query ... --risk ... --source ... --limit N]`；原 `/evolution list` 同样按
  权威优先级展示，避免两个排序真相。
- Agent：`evolution_candidates(action="priorities", ...)`；与 Slash 共用 `EvolutionReviewService`。
- New UI：显示 30 天 ranked/excluded 统计、前三个机会簇、每条 P-rank/score；详情显示公式、全部因子、authority
  lanes 与排除原因。列表页按实际终端高度计算窗口，保证键盘选中的 Candidate 不会被 Cluster 摘要挤出屏幕。
- Textual TUI：共享 Markdown renderer 显示相同 portfolio 和详情；没有第二套评分实现。
- typed Node 协议接受 capability kind，并严格拒绝 score、因子、rank 状态与 portfolio 计数不一致。

所有入口只读；`priorities` 不等于 approve/enqueue/experiment，bypass 也不会改变该 authority 边界。

## 验收证据

- [x] 相同快照不同输入顺序生成逐字段一致的 Portfolio；
- [x] 30 天外 Candidate、动态撤权、Review Gate 未通过和 Agent-only 信号均 score 0 且公开排除原因；
- [x] 同 lane 同 UTC 日 20 次观察只计 frequency 1；
- [x] Goal 与 Tool Catalog 进入同一 capability domain，但保持两个独立 Candidate；
- [x] Cluster score 取 max，新增同域成员不抬分；输入超过 500 明确拒绝；
- [x] 40 条 authority 重验真实并发且峰值不超过 16；reader 失败时 Candidate 不进入排名；
- [x] filter 不改变全局 anchor/rank，`--limit 1` 在排序后应用，较新的 evidence-insufficient Candidate 不遮住
  review-ready Candidate；
- [x] Tool/Slash、Python typed projection、Node strict protocol、New UI renderer 与 TUI renderer 使用同一结果；
- [x] 80/120/200 列渲染不越界，Cluster 摘要存在时末端选中项仍可见；
- [x] 相关 Python/Node 小模块测试和 Ruff 通过；按用户要求未运行全量测试。

## 自我审视与未完成项

1. 这是 domain clustering，不是 embedding/LLM semantic clustering。当前脱敏 Evidence 不足以安全证明 Goal 与工具名
   的语义等价；未来若增加结构化 capability taxonomy，必须先建立可审计映射 authority。
2. Cost 是固定启发式，不是历史交付时长模型；在获得真实 Proposal/Experiment 成本 Outcome 前不得伪装为预测值。
3. 500 条是当前 Store API 的硬边界；尚无 snapshot-bound cursor 覆盖更大 Candidate 历史。
4. Priority 是派生只读视图，不持久化独立决策 ledger。Policy v1 的变化通过代码、版本与 Git 审计；若未来允许运行时
   调权，必须新增签名配置、变更审计与 replay 兼容，不能直接读取普通 YAML 权重。
5. 自然语言缺失意图仍未进入 Evidence。本切片之后应进入 EVO-06.2 Capability Proposal contract，而不是让排名直接
   触发自我修改。
