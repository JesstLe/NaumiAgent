# HAR-09.6c2b1 Post-Rollback Behavioral Matrix Core

## 状态

已实现。

## 目标

把 HAR-09.6c2a1 Coverage Contract 冻结的完整 lane 集收口为一份不可变、可动态撤权的行为矩阵。矩阵同时消费：

- 当前主机由 HAR-09.6c2a 形成的 `EvolutionPostRollbackBehavioralLane`；
- 目标主机由 HAR-09.6c2a3f 形成的 Ed25519-signed Remote Result ingestion receipt 与 canonical H5c。

只有 Contract 中每个 lane 都存在唯一、exact、当前有效的证据时，Service 才能持久化
`behavioral_evaluation_recorded=true`。矩阵仍不包含长期指标，不授予 learning 或 promotion authority。

## 为什么需要独立矩阵 artifact

Coverage Contract 只冻结“必须有哪些 lane”，单 lane/remote receipt 只证明“这一条 lane 已执行”。任何一个局部证据都不能回答：

1. Final Evaluation 要求的 lane 是否全部齐全；
2. 本机与远端证据是否对应同一 rolled-back Outcome、原 H5c 和 Suite；
3. 是否存在 missing、stale 或 conflict lane；
4. 多个 lane 的 recovery status 应如何机械聚合；
5. 之后长期观察和 policy learning 应绑定哪一份完整行为证据。

因此 6c2b1 签发新的 content-addressed Matrix，而不是修改 Coverage Contract 或把单 lane 的权限位改成 true。

## 证据解析

### 本机 lane

必须重新调用原 Behavioral Lane Service，并逐项核对：

- Outcome、lane order/kind/platform/Suite；
- 原 Comparison ID/SHA 与 baseline cohort identity；
- fresh H5c 与 Runtime Receipt authority；
- 当前 active baseline authority。

Matrix 只记录 lane ID/SHA、fresh H5c ID/SHA、recovery status 与 evaluated time，不复制完整 H5a payload。

### 远端 lane

Remote Result Store 新增 Outcome + original Comparison 的有界索引。索引不是 authority：读取时必须将每个索引列重新与
HMAC-attested、Worker-signed manifest payload 比对。旧 admission 可在最多 1000 条的有界迁移窗口内建立索引；超限失败关闭。

每个 remote lane 必须恰好找到一份 admission，并满足：

- Result View 为 `ingested` 且 `lane_evaluation_authority=true`；
- Outcome/Request/original Comparison/Suite/platform 与 Coverage lane exact 一致；
- ingestion receipt 的 H5c ID/SHA 仍存在且动态有效；
- recovery status 从当前 canonical H5c 重新计算，不能接受 Worker 自报 verdict。

零份证据返回 missing；多份 admission 返回 conflict。在 retry/supersede 协议完成前，不自动选择“最新一份”。

## Matrix 模型与总体判定

`EvolutionPostRollbackBehavioralMatrix` 固定绑定：

- workspace、Outcome 与 rollback Request；
- Coverage Contract ID/SHA；
- Runtime Verification、Before/After Evidence 与 Final Evaluation ID/SHA；
- 连续排序的完整 lane 集；
- 每条 lane 的 local/remote evidence kind、source ID/SHA、fresh H5c 与 recovery status；
- 本机/远端 lane 数、总体 recovery verdict 与 deterministic recorded time。

总体判定采用 fail-closed 机械策略：

| lane 集 | 总体 verdict |
| --- | --- |
| 任一 `incompatible` | `incompatible` |
| 否则任一 `inconclusive` | `inconclusive` |
| 全部 `recovered` | `recovered` |
| 其余完整可比较集合 | `changed` |

`changed` 不等于“变好”，也不会自动变为 promoted。`recovered` 只表示当前完整行为矩阵与原 baseline 的约定判定一致。

## 持久化、并发与撤权

- Matrix 与 Coverage/Lane/Remote Result 共用 session SQLite；构造期发现 Store split 立即失败；
- 写入使用 `BEGIN IMMEDIATE`，事务内重读 exact durable Coverage ID/SHA；
- Outcome、Request 与 Coverage Contract 都是单飞键；相同内容并发收敛，不同内容永久冲突；
- `recorded_at` 取所有 lane evidence time 的最大值，不依赖并发调用者时钟；
- Matrix JSON 上限 1 MiB，读取时重验 row columns 与 content-addressed identity；
- `inspect` 每次重验 Coverage、所有 local lane、remote signed admission 与 H5c；任一事实漂移即返回 `stale` 并撤销
  behavioral evaluation authority，但保留不可变审计 artifact。

## 权威边界

成功 Matrix 固定：

- `behavioral_evaluation_recorded=true`；
- 动态 View 的 `behavioral_evaluation_authority=true`；
- `long_term_metrics_recorded=false`；
- `learning_authority=false`；
- `promotion_authority=false`。

它不改变 rolled-back Outcome，不签发 promoted Outcome，不建立长期观察窗口，也不覆盖 rollback 的配置/数据恢复缺口。

## 双通道入口

- Agent Tool：`evolution_post_rollback_behavioral_matrix(request_id)`；
- CLI/TUI/New UI 共享 Slash：`/evolution outcome-behavior-matrix <rollback-request-id>`。

Tool 与 Slash 共用同一 Service；normal/moderate/bypass 均无二次确认。bypass 只跳过交互确认，不能绕过 Coverage、签名、H5c、
集合完整性或 Store 一致性校验。

## 验收证据

`tests/unit/test_post_rollback_behavioral_matrix.py` 与相邻 remote-result 测试验证：

1. 一个本机 interventional lane 与一个 Windows remote adversarial lane 形成完整 typed Matrix；
2. 相同输入重复记录与两个独立 Service 并发记录收敛为同一行；
3. missing remote lane 在 Matrix side effect 前失败；
4. 已记录 Matrix 的 remote authority 失效后动态投影为 `stale`；
5. recovered/changed/inconclusive/incompatible 聚合顺序 fail-closed；
6. Remote Result lane index 与 signed manifest 不一致时失败关闭；
7. Tool、共享 Slash、moderate 与 bypass 使用同一 Service 且不二次确认；
8. Engine registry、Ruff、py_compile、相关模块测试与文档治理通过。

上游 6c2a/6c2a3f 已分别验证真实 installed-runtime H5 路径和签名 admission/H5 ingestion。本切片的自动化验收使用真实
SQLite/Harness authority 对象与 typed 两 lane 场景，但当前开发主机没有真实 Windows/Linux daemon，因此不能把该测试宣称为
生产跨主机验收。

## 当前不足与下一切片

1. `HAR-09.6c2b2`：把 Matrix/Lane authority 作为 typed Workbench Reviews 详情投影到 New UI 与 Textual TUI fallback；
2. Remote retry/supersede：显式决定旧 admission 的撤权与替代关系，之后才能接受同 lane 多 attempt；
3. 真实 Windows/Linux runner 验收与 signed build manifest 顶层 binary digest 双向比对；
4. `HAR-09.6d`：长期 Outcome window、覆盖率、censoring、持续健康与动态撤权；
5. promoted Outcome、supersede ledger、配置/数据 rollback 与最终 policy learning 闭环。
