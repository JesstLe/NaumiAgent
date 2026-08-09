# EVO-05.5f1 本机 Opt-in Deployment Intent

## 目标

在不切换 active runtime 的前提下，消费 current EVO-05.5e Candidate Bundle Admission 和
EVO-05.5d Stage Advance，为当前 Naumi 安装建立显式、持久、可动态失效的 opt-in enrollment，
并冻结后续 ARC-07 CAS activation 所需的 exact Deployment Intent。

本切片解决“谁同意、哪台安装、准备切到哪个 immutable slot、以哪个 previous pointer 为前提”的
authority 问题。它不执行 pointer switch、不启动进程、不产生 Deployment Receipt，也不宣称已经部署。

## 权威链

`EvolutionRevalidationOptInDeploymentIntentService.authorize()`：

1. 重验 Candidate Admission 的 slot、boot、active pointer、Build Trust Policy 与 Ed25519 Attestation；
2. 重验 Rollout Plan 的第二阶段仍是 `opt_in`，exposure 为 1%；
3. 通过 HAR-10.6 durable interaction 明确询问用户是否把当前本机安装加入 cohort；
4. `cancel` 作为 terminal answer 持久保留，重复调用不会重复询问，也不会形成 Intent；
5. `enroll` 绑定 exact Admission、plan stage、canonical workspace 与 installation identity；
6. 用户回答后再次读取 Admission 和 Stage Advance，关闭回答期间 pointer/control/trust 漂移窗口；
7. 冻结 previous pointer digest、预期下一 generation、candidate slot/boot receipt、trust-policy/attestation digest；
8. SQLite `BEGIN IMMEDIATE` 内重验 exact Admission、Stage Advance、Plan 与 latest control projection，再幂等写入 Intent。

Store 不信任 Service 构造的对象：它独立重读 authoritative Harness answer，校验交互问题与选项的 exact
静态 payload，并依据配置的 release root 重算 installation identity。直接调用 Store 不能伪造另一台安装、
替换问题语义或绕过已变化的 rollout control。

## Cohort 语义

当前 `EvolutionRevalidationOptInCohort` 只证明：

- 当前 local installation 的用户明确选择 `enroll`；
- enrollment 绑定计划中的 exact 1% opt-in stage；
- assignment mode 是 `explicit_local_installation_opt_in`。

它刻意保留 `population_assignment_enforced=false`。当前没有全局安装注册表、随机分桶或服务端流量路由，
因此不能把一台本机安装的 opt-in 虚报成已执行全局 1% rollout。`percentage_rollout_authority` 与
`stable_rollout_authority` 均为 false。

## 动态 fencing

`inspect()` 每次重验：

- Candidate Admission exact artifact 与 activation input authority；
- Stage Advance exact Receipt、control generation 和有效期；
- previous active pointer、candidate immutable slot 与 Boot Receipt；
- Build Trust Policy、trusted key 与 detached Attestation；
- authoritative Harness enrollment answer；
- Deployment Intent 自身有效期。

任一依赖变化都会令 `activation_intent_authority=false`，历史 Intent 仍保留审计。恢复原 trust policy
可以恢复对应 authority；pointer 已推进或时间已过期不会被历史记录掩盖。

## 并发与恢复

- 单进程内按 completion ID single-flight，多个并发调用只创建一个交互和一个 Intent；
- 数据库以 Admission、Completion、Interaction 和 content digest 唯一约束去重；
- unanswered interaction 返回 pending，不创建第二个问题；
- terminal decline 可重复读取；
- 5f1 崩溃最多留下 terminal interaction 或 immutable Intent，不改变 active runtime。

跨进程 interaction 创建仍由 HAR authority 的唯一 identity 约束；后续 5f2 必须继续以 pointer CAS 作为
最终并发边界，不能把 5f1 的进程锁当成部署锁。

## 验收结果

- 八个并发 authorize 调用只生成一个 durable interaction 和一个相同 Intent；
- enroll 形成 exact local cohort，active pointer 保持不变；
- cancel 可重复观察，不产生 Intent；
- trust-policy 轮换、pointer 变化和 expiry 会动态撤销 authority；
- 用户回答后 pointer 变化会阻止 Intent 落盘；
- Store 在同一事务内重验 Admission、Stage Advance、Plan 与 latest control；
- Engine 与公共 lazy exports 已接线；
- 4 个本模块用例、1 个 Engine 装配用例及 public import/ruff 检查通过；未运行全量测试。

## 下一切片

[EVO-05.5f2](EVO-05-5f2-opt-in-activation-reconciliation.md) 已只消费 current 5f1 Intent：

1. 最后一次重验 trust、slot、boot、interaction、control 与 expiry；
2. 用 `expected_previous_pointer_sha256` 调用 ARC-07 atomic CAS activation；
3. 重读 active chain，确认 candidate slot 与预期 generation；
4. append-only 写入 Deployment Receipt；
5. 若 pointer 已切换但 Receipt 未落盘，按 generation 与 target slot 机械补写；
6. 若 pointer 未切换则安全重试；若 pointer 指向其他目标则冲突失败，绝不重复切换或虚报部署。

5f2 额外把 exact Intent authority 纳入 v2 pointer event digest，避免未授权的同 slot activation 被 reconcile
错误认领。后续进入 opt-in runtime observation 前，仍需保持 population/percentage/stable authority 关闭。
