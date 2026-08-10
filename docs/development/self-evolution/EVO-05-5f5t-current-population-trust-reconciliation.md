# EVO-05.5f5t Current Population Trust Reconciliation

## 目标

把 [EVO-05.5f5s](EVO-05-5f5s-stable-population-candidate-preview.md) 的 durable Completion 候选与
[ARC-07.5c](../architecture/ARC-07-5c-signed-installation-population.md) 的 current signed managed-installation Population
在生产 `AgentEngine` 中真实组合。该切片回答候选所引用的 Snapshot 是否仍是 latest、Registry trust 是否 current、是否已进入
有效期且未过期，
以及候选成员是否确实属于该完整 Population。

它只建立 current Population source authority，不逐成员动态重验 5f5r 上游 evidence，因此
`dynamic_revalidation_authority`、`stable_rollout_authority` 与 `promotion_authority` 仍固定为 false。

## 最小 ARC 前置

`load_release_population_trust_policy()` 从 installer-owned trust 文件读取 public artifact：

- 默认生产路径：`<release-root>/trust/trusted-population.json`；
- 只接受非空普通文件，拒绝符号链接与非 regular file；
- 上限 512 KiB，打开前后核对 device/inode/size/mtime/ctime，读取期间变化失败关闭；
- 使用 strict `ReleasePopulationTrustPolicyDocument` 重算 policy identity；
- 缺失、不可读、超限或内容无效均返回稳定错误码，不读取钥匙串、不请求 Registry private key。

生产 Engine 同时组合独立的 `<release-root>/state/release-population.db` 与 Population Store。Engine 初始化不会立即读取 trust
文件；只有存在待对账 Snapshot 时才按需调用 provider，因此未配置 Registry 的普通本地用户不会在启动时遇到凭据提示。

## 动态对账

每次 `/evolution stable-population-preview` 在选定 Snapshot 后调用 Store `inspect()`，核对：

1. durable Snapshot artifact 内容身份仍可读取；
2. Snapshot 仍是 channel latest head；
3. Registry key 仍在 current Trust Policy 中 active 且签名有效；
4. Snapshot 已进入 `valid_from` 且未到 `expires_at`；
5. receipt 的 Snapshot ID/SHA-256/sequence/denominator 与 signed artifact 精确一致；
6. 所有已观察 `installation_member_id` 均属于 Snapshot credential set。

投影公开 source-current、latest、trust-current、not-yet-valid、expired、membership-consistent 与有界撤权原因。只有六项同时满足时
`population_snapshot_authority=true`。该字段不改变 durable `candidate_complete` 的历史事实，也不授予发布执行权限。

## 用户与 Agent 通道

- Agent Tool：`evolution_stable_population_candidate_preview`；
- Slash：`/evolution stable-population-preview [population-snapshot-id] [limit]`；
- CLI、Textual TUI 与 New UI 继续复用同一 Tool、Slash Router 和中文 renderer；
- renderer 明确区分 Current Population authority 与尚未组合的 dynamic 5f5r；
- 所有 permission mode 均为只读、无需二次确认，bypass 不增加确认。

## 验收标准

- exact 两成员 passing receipts + current signed Population 得到 `population_snapshot_authority=true`；
- 显式查询 current Snapshot 但尚无 Completion 时仍显示 `0/N`、缺失 N，Population authority 与 candidate completion 分离；
- 新 Snapshot 入库后旧候选出现 `newer_snapshot_exists` 并即时撤权；
- Registry key revoked 后出现 `registry_trust_changed` 并即时撤权；
- Snapshot 尚未进入 `valid_from` 时出现 `snapshot_not_yet_valid` 并保持无 authority；
- receipt member 不属于 signed credential set 时出现 `population_lineage_or_membership_mismatch`；
- 无 Population Store 的库级使用固定无 authority，并显示 `population_store_not_configured`；
- trust loader 拒绝 malformed JSON 与符号链接；
- Engine 真实组合 Store、loader provider 与现有 Tool，空库调用不触碰缺失 trust 文件；
- 只运行本模块小测试、ruff、compile、public import 与 YAML 校验，不运行全量测试。

## 自我审视与下一步

本切片证明“历史候选引用的 Population 当前可信”。后续
[EVO-05.5f5u](EVO-05-5f5u-dynamic-stage-completion-inspection-port.md) 已增加只读组合端口，对每个 current member 调用现有
5f5r `inspect()` 并形成动态 authority；默认生产 read graph 的自动构造仍是下一独立切片。

远端 Registry HTTP 分发、key rotation channel、credential renewal/revocation API 和 10,000 以上 Merkle 分页仍属于 ARC-07
后续工作，不在本切片伪装完成。
