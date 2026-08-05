# EVO-05.3f3b5 Fresh Decision Aggregation

## 目标

把一个 exact Fresh Approval Requirement 下的新用户 consent、全部专业角色 Response、current Ed25519 Signature Receipt
和 Fresh technical gates 聚合为新的 append-only Decision。聚合完全由持久 authority 与机械规则决定，不调用 LLM，也不复用
旧 Decision。

## 输入与结果

`EvolutionRevalidationApprovalDecisionService` 每次动态重签发 current Requirement，并按 step 顺序重读唯一 Response：

- user `approve` 只有在本地 session identity 已验证时计入 quorum；
- professional `approve` 必须存在本轮 Signature Receipt，且其 Requirement、Response、Principal event、key generation、role
  和 expiry 仍 current；
- `reject`、`request_changes` 无需补签即可形成明确负向 outcome；
- missing response、unverified identity、missing signature 形成 `pending`；
- expired Requirement、stale signature 或 current Requirement replacement 形成 `stale`。

五个 technical gate 由 current Requirement 的重新签发提供证据：runtime contract current、Fresh Final eligible、Reapproval
authorized、current target bound、Fresh Promotion Input complete。Requirement 到期会使全部 gate blocking。

## 确定性优先级

1. Requirement expired 或任一 signature stale：`stale`；
2. 任一明确 reject：`rejected`；
3. 任一 request changes：`changes_requested`；
4. 缺 response/identity/signature 或 blocking gate：`pending`；
5. 所有 required role 与 gate 满足：`approved`。

只有 current `approved` Decision 设置 `staged_rollout_eligible=true`。Receipt 始终声明不执行 promotion、rollout、Git、merge、
push 或 publish。

## 持久化与并发

- source-set digest 绑定 Requirement/Input/target、全部 role decisions、Signature receipt identity 与 gate decisions；
- 同一 source-set 的重复或 8 路并发执行只保存一个 Receipt；
- authority 变化追加 sequence，并通过 previous decision id/digest 形成 Requirement-scoped hash chain；
- Service 写前双采集，Store 使用 `BEGIN IMMEDIATE` 复核 Requirement、Response、Signature SQLite authority 和 chain head；
- inspect 动态重读签名 Principal/current key；历史签名仍可审计，但换钥后历史 approved Decision 立即显示 current stale。

## 验收结果

- 全角色 Response、无专业签名生成 sequence 1 pending；
- 全部真实 Ed25519 签名后生成 sequence 2 approved，previous link 正确；
- 8 路并发 approved 聚合只得到一个 Receipt；
- professional Principal 换钥后历史 approved 动态变 stale；
- 明确 reject 在其他角色缺失时仍形成 rejected；
- 完整角色测试发现并修复 `independent_reviewer` 交互标题超过 40 字符的真实 UX 阻断；
- 聚焦 Ruff、Decision/Response 测试、Engine/public import 与 YAML 通过；未运行全量测试。

## 后续依赖

本 Decision 只开放 staged rollout 的输入资格。[EVO-05.4a](EVO-05-4a-immutable-rollout-plan.md) 已建立不可变 rollout plan，
但 local canary、分阶段 promotion authority 和 kill switch 仍由 EVO-05.4b 实现；随后 EVO-05.5/05.6 才能用运行信号触发自动回滚，EVO-05.7/06 才把 outcome 重新注入下一轮候选、
评测和策略选择，形成真实自进化闭环。
