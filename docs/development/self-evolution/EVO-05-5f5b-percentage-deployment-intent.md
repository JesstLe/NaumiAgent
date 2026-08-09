# EVO-05.5f5b Percentage Deployment Intent

## 目标

把 current selected Percentage Assignment、current verified Archive Admission、exact managed-installation Credential、
当前主机 target 与 exact previous active pointer 组合为一次性、短期、可动态撤权的
`EvolutionRevalidationPercentageDeploymentIntent`。

本切片只建立“哪一个已注册安装被控制面允许准备部署哪一个 inactive slot，并以哪个 active pointer 作为 CAS 前提”的
authority。它不执行候选 boot、不切换 active pointer、不启动用户进程、不形成 Deployment Receipt，也不声明 percentage
rollout 或真实 exposure 已发生。

## 为什么 percentage 阶段不再次询问用户

Opt-in 的 [EVO-05.5f1](EVO-05-5f1-opt-in-deployment-intent.md) 必须逐安装获得明确用户 enrollment，因为其 cohort
本身由本机用户选择形成。Percentage 阶段的 authority 来源不同：

1. 管理员已通过 current `opt_in → percentage` Stage Advance 批准进入受控 rollout；
2. Registry-signed Population Snapshot 给出 authoritative denominator 和 managed-installation Credential；
3. installation private key 对 Assignment challenge 的 proof-of-possession 证明调用者持有 exact 安装身份；
4. deterministic Assignment 只允许 selected member 继续。

因此 5f5b 的 `authorization_mode` 是
`current_stage_advance_selected_managed_installation`，`explicit_user_prompt_required=false`。再次逐台弹窗会把自动
percentage rollout 错误退化为 opt-in；但这一决定只授权生成 Intent，不越过 boot、CAS activation 或 exposure receipt 边界。

## Exact 输入与绑定

`EvolutionRevalidationPercentageDeploymentIntentService.issue()` 同时消费并重验：

- current [EVO-05.5f5a](EVO-05-5f5a-percentage-cohort-assignment.md) Assignment，且
  `member_selected=true`、membership authority 为 true；
- Assignment 绑定的 current immutable Rollout Plan 与 `opt_in → percentage` Stage Advance；
- exact current Population Snapshot 中的 Registry-signed Credential；
- current [ARC-07.5d3](../architecture/ARC-07-5d3-verified-archive-admission.md) Admission，且 candidate slot
  完整、immutable、仍 inactive；
- 本机 `host_release_target()`；
- 当前 existing active pointer，作为下一 generation 的 exact CAS baseline。

Intent 冻结完整 Assignment、Plan、Admission、Credential 和 previous pointer，并机械校验：

- Credential ID/digest、member、channel、公钥摘要与 Assignment 完全一致；
- Catalog Resolution、installed slot 与本机 target 一致，target platform 在 Plan required platforms 中；
- slot/Build Attestation 的 source commit 等于 Plan target head；
- slot/Build Attestation 的 source tree 等于 Plan target tree；
- candidate slot 不是 previous pointer 的 current slot；
- `expected_activation_generation = previous_pointer.generation + 1`。

客户端不能提交任意 target、Credential、source commit/tree 或 expected pointer 覆盖这些投影。

## 一次性与短期语义

- authority window 最长 300 秒，并截断到 Stage Advance 与 Credential 的最早过期时间；
- Evolution evidence DB 对 `assignment_id` 和 `admission_id` 分别唯一，一个 Assignment 只能签发一次；
- 多 Service 实例即使使用不同签发微秒并发竞争，也按 exact authority source 收敛到第一个 durable Intent；
- 已过期 Intent 不允许经 Store 直接重放；过期后必须形成新的 authoritative Assignment/Admission 链，不能刷新历史 Intent；
- “一次性”当前指一次签发。后续 executor 必须以独立 Prepared/Deployment Receipt 标记消费结果，不能原地修改 Intent。

## Durable Store 与安全边界

Intent 与 Assignment、Plan 共用 Evolution SQLite evidence DB。Store 不信任 Service 构造的对象：

1. Pydantic strict/frozen model 重算所有 nested projection 和 content identity；
2. 写前重新读取 Assignment、Plan、Population、Admission、slot、host target 与 active pointer；
3. 使用 parameterized SQL，`BEGIN IMMEDIATE` 内重读 exact durable Assignment/Plan；
4. artifact 限制为 8 MiB，SQLite JSON 篡改在 restore 时 fail closed；
5. Store 使用 authoritative clock 拒绝未生效或已过期 artifact；
6. 不接收、持久化或记录 installation private key、Registry private key、下载 token 或其他 secret。

Archive、Population 与 release-slot DB 是独立 trust store，无法与 Evolution DB 做单一跨库写事务；因此 Service 在构造前、
构造后和 Store 写前多次重验，View 每次 inspect 再动态重验。任一依赖在最后窗口变化时，历史 artifact 保留审计，但 authority
立即关闭。

## 动态 View

`EvolutionRevalidationPercentageDeploymentIntentView` 分别暴露：

- durable Intent source 是否未损坏；
- Assignment、Plan、Archive Admission、Credential 是否 current；
- installation target 是否仍等于当前主机；
- previous pointer 是否仍完全一致；
- candidate slot 是否仍完整且 inactive；
- Intent 是否过期。

只有全部为真时 `deployment_intent_authority=true`。该字段表示“允许下一层执行 candidate boot preparation”，不是
deployment 已发生。以下字段始终为 false：

- `boot_executed`；
- `activation_intent_authority`、`active_pointer_switched`；
- `deployment_receipt_authority`、`process_started`；
- `percentage_rollout_authority`、`stable_rollout_authority`、`promotion_authority`。

## 验收结果

- 真实生成 Stage Completion/Advance、8 个 Ed25519 installation Credential、signed Population Snapshot、selected Assignment、
  signed release archive、immutable inactive candidate slot 和 booted active baseline；
- 四个独立 Service 以不同签发微秒并发签发，收敛到同一个一次性 Intent；
- Intent 最长 5 分钟，过期后 Service View 撤权，Store 直接重放也被拒绝；
- non-selected member 无法签发；
- active pointer 被其他版本推进后，CAS authority 即时失效；
- Population Snapshot 换代、Credential authority 变化、candidate runtime bytes 篡改均动态撤权；
- durable Intent JSON 摘要篡改在 restore 时 fail closed；
- 签发全过程没有 candidate boot、pointer switch、用户进程或 rollout claim；
- ruff、compile、public lazy import 与本模块 4 项真实链路测试通过；未运行全量测试。

## 当前边界与下一步

5f5b 已补齐 selected managed installation 的短期控制面 Intent。[EVO-05.5f5c](EVO-05-5f5c-percentage-boot-preparation.md)
已通过跨进程 claim/lease 对 exact candidate 执行真实 `--version` probe，并冻结独立 Prepared Receipt；它仍未切换 pointer。
[EVO-05.5f5d](EVO-05-5f5d-percentage-activation-reconciliation.md) 已再次重验 Intent、Prepared Receipt 与 expected
previous pointer，形成可崩溃对账的 Deployment Receipt；本机 pointer 切换仍不能被解释为完整 percentage exposure。
