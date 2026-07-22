# EVO-02.7c2 Mutation Author Receipt

## 目标

为 EVO-02.7c1 的每次成功 Mutation Turn 持久化不可变 author authority，使后续 EVO-04.3 Independent
Reviewer 能在进程重启后证明“谁生成了补丁、通过哪个 provider/model、使用了哪一版 Prompt”，而不是根据
当前配置或 `ModelResponse.model` 临时推测。

本切片只证明 mutation author，不执行 Reviewer，不评价 Candidate，也不改变 Mechanical Gate。

## 为什么是 EVO-04.3 的强制前置

Generation Trace 已证明虚拟文件工具调用与最终文件 digest chain，但此前没有记录模型运行时身份；
`EvolutionMutationTurnResult.models` 又只存在于当前进程。若 Reviewer 仅比较当前 router 配置，则修改配置、
重启或 provider alias 变化后都无法证明 author/reviewer 隔离。

EVO-02.7c2 因此复用 `ModelPort.get_runtime_identity()`，在成功 Trace 产生后签发
`EvolutionMutationAuthorReceipt`。EVO-04.3a 必须从 Store 读取该 Receipt；缺失、损坏或与 Trace 漂移时
fail-closed。

## Authority 内容

Receipt 强绑定：

- Generation Trace ID/SHA-256、run、Plan ID/SHA-256、attempt、completed time；
- requested/canonical/upstream model、provider、API format 与 identity source；
- 固定 `evolution-mutation-turn-prompt-v1`；
- system prompt、initial user prompt 与 tool schema 的独立 SHA-256；
- 每轮被接受的 ModelPort 输入上下文摘要、响应模型、finish reason、tool-call 数与 Token/cost facts；
- 有序 model-call facts 总摘要、唯一 response model 序列和总计数。

Receipt 不保存源码、Prompt 正文、模型正文、reasoning、tool arguments、凭据或 proposed contents。
每轮输入摘要覆盖当时完整 message context，因此可证明后续 turn 是否消费了前序 reasoning/tool result，
但不会把敏感内容写入 SQLite。

## 不变量与持久化

- Receipt ID 由规范化 payload SHA-256 派生，Store 每个 Generation Trace 只允许一个 Receipt；
- model-call order 必须从 1 连续递增，Token、tool-call、cost 与 response-model 汇总必须可重算；
- author tool-call 总数必须等于 Generation Trace 的 `total_tool_calls`；
- `author_identity_ready=true`，同时固定 `reviewer_identity_bound=false`、
  `candidate_acceptance_decided=false`；
- Store 使用 `BEGIN IMMEDIATE` 提供并发幂等写入，并同时复核 JSON 与索引列；索引或正文篡改均失败关闭；
- Author Receipt 写入失败时 Mutation Turn 不返回成功。Generation Trace 已由下层先持久化时可能留下孤立
  Trace，但 EVO-04.3 不得在缺失 Author Receipt 时继续，因此不会降级为无身份审查。

## 运行时组合

`AgentEngine` 在 session SQLite 中组合一个 `EvolutionMutationAuthorReceiptStore`，并把它注入唯一的
`EvolutionMutationTurnRunner`。没有新增模型路由、权限入口、Slash 命令或 UI 私有状态。

## 验收证据

- 真实 Git Contract/Lease/Snapshot/Plan 经确定性 ModelPort 生成 Trace 与 Author Receipt；
- Receipt 的 provider/canonical model、调用数、tool-call 数及 Trace binding 可从 Store 精确重读；
- 四个并发重复写入返回同一个 Receipt；
- provider/model/runtime source 任一缺失或 requested model 不匹配时，在模型调用前失败关闭；
- 序列化 Receipt 不含 approved source 或 system prompt 正文；
- SQLite provider 索引篡改被 `mutation_author_receipt_corrupt` 拒绝；
- Mutation Turn 的 protocol、budget、cancel、timeout、prompt oversized 和 Event failure 聚焦回归继续通过；
- Ruff、compile/import 和 Engine composition 聚焦检查通过。

## 明确未完成

- EVO-04.3a Reviewer model/provider 选择、author identity 隔离、strict JSON advisory 与 veto 无模型路径已完成；
- Reviewer structured-output schema、Prompt digest、超时/解析失败和不可变 Review Receipt；
- Mechanical Gate `veto` 的只读解释路径；
- EVO-04.4a Counterfactual、EVO-04.5a Reward-hacking Evidence、EVO-04.6a Decision State 与 EVO-04.6b
  Escalation Resolution 与 Reflection Memory 已实现；promotion 尚未实现。

## 下一步

EVO-04.4a 至 EVO-04.7a 已沿本 Author Receipt authority 链实现风险扫描、四态决策、持久 Resolution 与
非注入 Reflection。EVO-05.1a 至 EVO-05.2d 已完成 Package、Requirement、Role Response、Principal、真实
Ed25519 Signature Receipt 与非执行型 Approval Decision；下一步是 EVO-05.3 rebase/revalidate authority。
