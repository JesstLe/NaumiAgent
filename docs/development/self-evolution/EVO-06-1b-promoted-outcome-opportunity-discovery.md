# EVO-06.1b Promoted Outcome-backed Opportunity Discovery

## 状态

Implemented。依赖 EVO-05.7b4c 的 append-only Stable Promotion Outcome、EVO-05.7b4d 的
current promoted authority、EVO-06.1a 的 Outcome Opportunity Service 与 EVO-01 Candidate Store。

## 目标与用户价值

成功发布不再是自进化链路的终点。用户或 Agent 可以把当前、动态有效的
`EvolutionStablePromotionOutcome` 回注下一轮 Candidate，在不复制源码和自由文本的前提下保留
“这项能力已经通过稳定观察”的机械事实。回注形成新的 Candidate ID，不原地改写上一轮 Candidate，
也不把一次成功直接冒充 learning、execution 或 promotion authority。

## 架构决策

### 单一持久权威

- 继续复用 `EvolutionCandidateStore`，不增加 Opportunity 表或第二套数据库。
- Outcome Opportunity Service 构造时强制注入 rollback 与 stable promoted 两个 authority；禁止只注册
  Tool schema 却缺失 promoted 运行依赖的半配置状态。
- 继续复用 `EvolutionOutcomeOpportunityService.discover()`、Agent Tool
  `evolution_discover_outcome_opportunity` 和 Slash `/evolution discover-outcome`。
- 服务按 content-addressed ID 前缀确定来源类型：
  `evrerollbackout_*` 走 rollback authority，`evstablepromout_*` 走 promoted authority。
- Agent Tool、Slash、New UI 与 Textual TUI 最终读取同一 Candidate Store 投影。

### 确定性映射

| Candidate 字段 | Stable Promotion Outcome 映射 |
|---|---|
| Source kind | `promoted_outcome` |
| Source URI | `evolution-outcome://promoted/<outcome-id>` |
| Finding | `stable_promotion_improvement` |
| Scope | `evolution:promoted:<candidate-target-sha-prefix>` |
| Root | 原 Candidate ID/SHA、target SHA、finding 与 scope 的 canonical SHA-256 |
| Evidence ID | Outcome ID/SHA 与 root SHA 的 canonical SHA-256 前缀 |
| Metric | `harness.stable_promotion_improvement.regression_rate decrease 0` |

scope 只保留 target 的摘要，不落盘绝对路径、源码内容、Workbench Proposal 自由文本或凭据。
Finding 与 root 都不同于上一轮 Candidate，因此新的成功 Outcome 必然开启新的 Candidate identity；
同一 Outcome 的并发重放仍由 Evidence identity 和 Candidate Store 事务收敛到 revision 1。

## 动态 authority 门

发现时和每次 Review/Proposal projection 前都调用
`EvolutionStablePromotionOutcomeService.inspect(outcome_id=...)`，并同时验证：

1. durable Outcome/Event pair 未变化；
2. supersession hash chain 完整；
3. promote Decision 与 Eligibility 仍是 current authority；
4. Outcome 仍是 Proposal 的 projection head，未被新 Outcome supersede；
5. workspace canonical root 与当前 Engine 一致；
6. Outcome ID、完整 SHA-256 与 Candidate Evidence ref 完全一致。

任一条件失败时 `source_authority` hard block。旧 promoted Outcome 被 supersede 后不能新建 Candidate，
已经写入的 Candidate 也不能生成 Proposal。

## 双通道与界面

- Agent Tool：`evolution_discover_outcome_opportunity(outcome_id=...)`
- Slash：`/evolution discover-outcome <rollback|stable-promoted-outcome-id>`
- Review filter：`/evolution list --source promoted_outcome`
- 回执显示来源类型 `promoted`、Candidate revision、Evidence ID 和动态 authority 状态。
- 回执契约升级为 `schema_version=2`、`evolution-outcome-opportunity-v2`，避免新增必填来源类型
  被旧消费者误按 v1 解析。
- 回执明确标注不可执行，未授予实验或推广权限。
- New UI/TUI 通过现有 `evolution/review` typed event 接受 `promoted_outcome` 过滤值；没有界面专属状态。

## 权限与安全边界

- 沿用现有 Opportunity Tool 权限：normal mode 仍受 PermissionChecker，bypass 直通权限决策；
  两者都不能跳过 source/workspace/digest authority。
- Candidate 固定 `experiment_eligible=false`。
- 本模块不签发 Experiment Contract、不修改源码、不执行工具、不学习策略、不提升模型权重。
- Stable Outcome 服务未注入、ID 非法、Outcome 缺失/损坏、跨工作区或 authority 失效均 fail closed，
  且不会写入 Candidate Store。

## 并发与恢复

- 8 路同时发现同一 promoted Outcome 只产生一个 Evidence 和 revision 1。
- Candidate Store 的 `BEGIN IMMEDIATE`、Evidence content identity 与 revision CAS 是 durable 幂等边界。
- 服务重启后由 Outcome Store 和 Candidate Store 重建同一结果，不依赖进程内缓存。
- Stable Outcome 被新 head supersede 后，旧 Candidate 的动态 authority 在下一次 Review 时失效，
  不需要删除历史 Evidence。

## 验收标准

- [x] 真实 Stable Population → Eligibility → promote Decision → Stable Outcome 链可回注 Candidate。
- [x] current promoted Outcome 形成不同于来源 Candidate 的新 Candidate ID。
- [x] superseded promoted Outcome 拒绝回注，current head 正常回注。
- [x] 8 路并发重放只产生一个 Evidence、revision 1。
- [x] Evidence 不包含 candidate target 明文、workspace 绝对路径、Proposal ID、源码或补丁。
- [x] Review 前动态重验 Stable Outcome authority；撤权后 hard block。
- [x] Tool schema 同时接受 rollback/promoted ID，Slash 与 Tool 共享 `discover()`。
- [x] Python typed protocol 接受 `promoted_outcome` filter，New UI/TUI 读取同一投影。
- [x] Ruff、聚焦单元、并发、Engine composition 与真实 SQLite 链路通过。

## 未包含的后续模块

- EVO-06.1c：成本/延迟热点、明确缺失能力与跨 Outcome 时间窗聚类/排序。
- EVO-06.2：把 review-ready Opportunity 扩展为完整 Capability Proposal。
- EVO-06.3–06.8：Sandbox registration、Shadow、Limited Activation、Selection、Retirement 与
  Meta-governance。

Promoted Outcome 回注只关闭“成功结果无法进入下一轮发现”的断点，不代表自进化闭环已经全部完成。
