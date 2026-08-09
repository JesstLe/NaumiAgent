# EVO-05.5f5a Percentage Cohort Assignment

## 目标

把 current `opt_in → percentage` Stage Entry authority 与 current signed installation population snapshot 组合为
可复现、可审计且具有安装私钥持有证明的 exact percentage cohort assignment。本切片只确定一个已注册安装是否属于
limited percentage cohort；它不下载或激活 build，不扩大真实流量，也不声明 rollout、稳定性或 promotion 已完成。

## Exact 输入

每次 Assignment 必须同时消费并动态重验：

1. current [EVO-05.5f4e](EVO-05-5f4e-opt-in-stage-advance-authorization.md) `advance` Receipt；
2. Receipt 绑定的 current Rollout Plan ID/digest、candidate revision/target 与 percentage exposure；
3. channel 的 current [ARC-07.5c](../architecture/ARC-07-5c-signed-installation-population.md) signed complete snapshot；
4. snapshot 中 exact Registry-signed installation credential；
5. installation private key 对 exact Assignment challenge 的 Ed25519 proof-of-possession。

客户端不能提交任意 member string 参与分桶。`member_id` 来自 Registry-only、channel-separated HMAC pseudonym，Snapshot
提供完整、有序且签名的 denominator；Assignment Service 只能从其中解析 exact Credential。

## 确定性分桶

算法版本为 `sha256-ranked-population-v1`：

- seed 冻结 exact Plan ID/digest、candidate ID/revision 与 channel；
- 对 Snapshot 中每个 member 计算 `SHA-256(seed:member_id)`，再以 member ID 作为确定性 tie-break；
- target count 严格为 `max(1, ceil(population_denominator × exposure_percent / 100))`；
- 排名前 target count 的成员构成 cohort，Assignment 冻结完整有序 selected set、set digest、member rank 与 selected flag。

seed 不包含 Snapshot ID，因此相同 Plan 下人口新增或减少时尽量保持排序稳定；但每个 Assignment 仍绑定 exact Snapshot。
新 Snapshot 成为 channel latest 后，旧 Assignment 即时撤权，调用方必须对新 denominator 重新签名和分配，不能沿用历史结果。

## 安装 proof-of-possession

challenge 冻结 Advance、Snapshot、Credential、Plan、candidate、channel、算法、seed、selected-set digest、denominator、
target count、member rank、selected 与签名时间。Service 只接收异步 signer port 的 canonical Base64 Ed25519 signature：

- 公钥必须与 Registry Credential 完全一致；
- 签名在模型构造和 durable restore 时均执行密码学验证；
- 只持久化公钥、signature 与 digest，不接收或保存安装私钥；
- 错误私钥、非 canonical signature 或 challenge 改写均 fail closed。

## Durable Store 与并发

- Assignment 与 Advance/Plan 共用 exact Evolution SQLite evidence DB；Population Snapshot 保留独立 Registry trust DB；
- 唯一来源键为 `(advance_receipt_id, snapshot_id, member_id)`；
- 写入前读取全部 live View，并在 installation signing 后再次读取，避免签名期间 source 漂移；
- `BEGIN IMMEDIATE` 内再次核对 durable Advance 与 Plan 行；
- 多 Service 实例并发产生相同 source assignment 时收敛到首个 durable artifact，不同 selection/source 冲突 fail closed；
- Artifact 使用 canonical JSON、SHA-256 content identity，并限制为 4 MiB。

View 每次重新读取 Assignment、Advance、Snapshot、Credential、Plan 并重算整个 deterministic selection。任一 durable row
损坏、trust 撤销、Snapshot 过期/非 latest、Credential 变化、Plan 漂移或 Stage Entry 失效都会撤销 current authority。

## 权限边界

source current 时：

- `population_assignment_enforced=true` 表示该安装已经按 authoritative population 执行分配；
- 仅当 `member_selected=true` 时，`percentage_cohort_membership_authority=true`。

以下能力始终为 false：

- `percentage_rollout_authority`；
- `deployment_authority`、`process_started`；
- `stable_rollout_authority`、`promotion_authority`；
- `git_write_executed`、`publish_executed`。

非成员 Assignment 是有意义的 authoritative negative result，不是错误，也绝不能被 UI 或下一层解释为“已 rollout”。

## 验收结果

- 复用真实 Candidate → opt-in Deployment → Liveness → managed ChatRun → Outcome → Stage Completion → Advance 证据链；
- 以 128 个真实 Ed25519 installation public key 生成 Registry Credential 和 signed Snapshot；
- exact target count 与 ceiling percentage 一致，成员和非成员结果均被验证；
- 四个独立 Service/Store 并发分配收敛到同一 Assignment；
- 错误安装私钥不能生成 Proof，未写入 Assignment；
- 下一 Snapshot 成为 latest 后，旧成员 Assignment 即时失去全部 current membership authority；
- SQLite JSON 篡改在 restore 时 fail closed；
- ruff、compile、公共 lazy import 与本模块测试通过；未运行全量测试。

## 当前边界与下一步

EVO-05.5f5a 只有 assignment authority，没有安装回执、percentage activation、群体 exposure accounting 或 guardrail
observation。[ARC-07.5d1](../architecture/ARC-07-5d1-signed-release-channel-catalog.md) 已补齐 signed distribution catalog 与
target Resolution，[ARC-07.5d2](../architecture/ARC-07-5d2-verified-artifact-fetch.md) 已补齐有界下载、原子落盘和动态撤权
Download Receipt；[ARC-07.5d3](../architecture/ARC-07-5d3-verified-archive-admission.md) 已补齐安全解包、构建证明重验和
immutable inactive-slot Admission。[EVO-05.5f5b](EVO-05-5f5b-percentage-deployment-intent.md) 已进一步把 current selected
Assignment、exact Admission、Credential、本机 target 与 previous pointer CAS 冻结为一次性 5 分钟 Intent；
[EVO-05.5f5c](EVO-05-5f5c-percentage-boot-preparation.md) 已进一步形成 current candidate Prepared Receipt。两者均未执行
activation 或真实 exposure；下一最小切片为 `EVO-05.5f5d Percentage Activation Reconciliation`。
