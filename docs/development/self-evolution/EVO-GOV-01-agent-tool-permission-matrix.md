# EVO-GOV-01 Evolution Agent Tool 权限矩阵

## 问题与目标

EVO-03.7a/3.7b1/3.7b2 与 EVO-04.1a/4.2a/4.3a 已注册六个非只读 Agent Tool。它们拥有真实的 durable
写入，但此前没有精确 `PermissionRule`，因此 normal runtime 将其判为 `UNKNOWN_TOOL`，Engine 的“所有注册
工具均受治理”门也会失败。

本基线为这些已经交付的 Tool 建立显式、可测试的权限矩阵。它不改变任何 artifact schema、issuer、executor、
Store、Slash 命令或后续 decision 语义。

## 风险分类依据

六个 Tool 都只能从已有签名 authority 派生并持久化不可变证据：

- 不运行项目代码或 Shell；
- 不修改 Candidate/worktree/main；
- 不扩大 Experiment scope、budget、network 或 dependency 权限；
- 不接受 Candidate，不产生最终 promotion；
- 重复调用由各 Store/Executor 幂等或 single-flight 收敛。

因此它们统一为 `MEDIUM`：高于只读查询，但不应像 Contract human-governance、baseline promotion 或仓库写入
那样逐次要求确认。

## 权限矩阵

| Tool | Artifact | Family | Session 上限 |
| --- | --- | --- | ---: |
| `evolution_evaluation_receipt` | 单 lane Evaluation Receipt | `evolution_evaluation_artifact` | 200 |
| `evolution_evaluation_contract` | Evaluation Aggregation Contract | `evolution_evaluation_artifact` | 50 |
| `evolution_final_evaluation_receipt` | Final Evaluation Receipt | `evolution_evaluation_artifact` | 50 |
| `evolution_decision_input` | Decision Input | `evolution_decision_artifact` | 50 |
| `evolution_mechanical_gate` | Mechanical Gate | `evolution_decision_artifact` | 50 |
| `evolution_independent_review` | Independent Review | `evolution_decision_artifact` | 20 |

Independent Review 的上限更低，因为首次成功路径会调用 Reviewer 模型；durable single-flight 仍负责同一 Gate
并发去重，权限上限负责限制一个会话内不同 Gate 的总调用面。

## 模式语义

- permissive/moderate/strict：允许，无逐次确认，不创建 session grant。
- lockdown：阻断所有六类派生写入；已有只读 Authority Tool 仍按各自只读规则工作。
- bypass：全权限直接通过，不要求确认，也不受本层 session call cap 限制。

bypass 只绕过交互 PermissionChecker；executor 仍必须重读 authority、验证 workspace/identity/digest/budget，
Store 冲突与 mechanical veto 仍不可被绕过。

## 验收

- 六个 Tool 在 permissive/moderate/strict 均返回 `ALLOW + MEDIUM`，family 精确匹配；
- lockdown 返回 `MODE_BLOCKED`；
- normal 模式达到各自上限后返回 `MAX_CALLS_EXCEEDED`；
- bypass 在超过同一上限后仍直接允许且无确认；
- `AgentEngine` 全注册表不存在未知非只读 Tool；所有 Tool schema 继续兼容 OpenAI function contract；
- 只运行权限与 Engine 注册表小模块，不以全量测试冒充本切片证据。

## 新 Tool 约束

以后任何 `metadata.read_only=false` 的 Evolution Tool 必须在同一提交中提供：精确 PermissionRule、风险依据、
允许模式、确认策略、调用上限、family、bypass/lockdown 测试和 authority 边界。依赖“未知 Tool 默认阻断”不是
完成权限设计。
