# EVO-06.1c2 Goal-backed Explicit Need Opportunity Discovery

## 状态

Implemented。本切片把用户通过 `/goal` 明确创建的 durable、未终结目标投影为可撤权的
`goal_need` Evidence 和 `capability` Candidate。它只开放人工 Review，不创建 Goal、不执行代码、
不签发 Experiment Contract，也不授予 promotion 或 learning authority。

## 依赖与最小前置

- UI-18 durable Goal Store：提供工作区隔离的真实用户授权来源；
- EVO-01 Candidate/Evidence Store：提供不可变候选、审计链和并发幂等；
- EVO-01.4b Composite Source Authority Router：允许 Goal reader 与 Outcome/Eval reader 并存；
- HAR-09 Feedback：保留 ordinary preference 与显式 Goal 的语义边界。

没有新建第二份需求数据库。Goal 是唯一 source of truth，Evolution 只保存可验证摘要。

## 权威边界

只有 `active`、`paused`、`blocked` Goal 是当前明确需求：

| Goal 状态 | 发现 | Review authority | 语义 |
| --- | --- | --- | --- |
| `active` | 允许 | 有效 | 正在推进的未满足需求 |
| `paused` | 允许 | 有效 | 用户暂缓，但未撤回需求 |
| `blocked` | 允许 | 有效 | 需求仍在，当前存在阻塞 |
| `completed` | 拒绝 | 撤销 | 需求已满足，不能继续冒充机会 |
| `cancelled` | 拒绝 | 撤销 | 用户已撤回 authority |

普通聊天、Agent 推测、HAR-09 `preference`、历史未发送输入和 Tool Search 瞬时 miss 均不能进入此
adapter。工具只接受现有 `goal_<12 hex>`，没有 objective 参数，不能替用户创建或改写 Goal。

## Evidence 契约

`goal_need` 使用动态 Evidence：

- `source_uri = goal://goals/<goal-id>`；
- `finding_code = user_explicit_need`；
- `scope = capability:need:<objective digest prefix>`；
- `root_fingerprint` 同时绑定 Goal ID 与 objective digest；
- `ref.sha256` 绑定 Goal ID、创建时间、objective digest 和 session ID；
- `observed_at` 使用 Goal 创建时间，不使用发现调用时间。

Candidate 和回执不保存 objective、note、session 文本或 workspace 绝对路径。Goal ID 被纳入 root，
因此用户未来重新创建内容相同的新 Goal 会形成新 Candidate，不会与已经完成或取消的旧需求合并。
Goal 状态不写入摘要：`active/paused/blocked` 之间切换不会制造虚假 Candidate revision；终结状态由
动态 authority 直接撤权。

## Candidate 与验证指标

该 Evidence 生成：

- `CandidateKind = capability`；
- expected metric `goal.user_explicit_need.completion`；
- direction `increase`，target `1`；
- verifier `goal_completion`。

`goal_completion` 已进入 typed contract，但当前 Metric Runner Registry 明确返回
`goal_completion_runner_unavailable`。在独立 acceptance runner 能把具体能力改动与 Goal 完成事实绑定前，
不得自动签发实验；不能把“Goal 后来被用户点为完成”单独解释成候选改动有效。

## 动态重验

每次 Review/Workbench gate 都重新读取 Goal Store 并重建完整 Evidence：

1. URI 必须精确解析为合法 Goal ID；
2. Goal 必须仍存在且状态未终结；
3. 重建 Evidence 必须与 Candidate 中原 Evidence 完全相等；
4. Store 不可读、记录损坏、目标内容/身份被篡改时失败关闭；
5. source router 不缓存 Goal authority，不接受 truthy 非布尔结果。

Evolution 模块只依赖最小 Goal Store 读取协议，不反向导入 Agent Engine，避免编排层与 Evolution 的
循环依赖。

## 双通道与界面

- 用户：`/evolution discover-goal <goal-id>`；
- Agent Tool：`evolution_discover_goal_need_opportunity`；
- 两个入口调用同一个 `EvolutionGoalNeedOpportunityService.discover()`；
- Review filter：`/evolution list --source goal_need`；
- New UI 与 Textual TUI 读取同一个 typed Review projection，不保存界面私有状态；
- normal/bypass 均无需二次确认，lockdown 禁止；该读取工具不能扩大文件或执行权限。

回执展示 Goal ID、状态、Candidate/Evidence ID、revision 和 authority 边界，但不展示目标正文。

## 验收证据

- [x] 8 个并发发现收敛到同一 Candidate revision，且 occurrence 不被幂等重试放大；
- [x] active/paused/blocked 可发现且保持同一 Evidence；
- [x] completed/cancelled 立即拒绝新发现并撤销既有 Candidate authority；
- [x] 直接篡改 SQLite 中 objective 后，既有 Candidate 动态重验失败；
- [x] Candidate、JSON 和用户回执不出现 objective、note、session 或 workspace 路径；
- [x] Agent Tool 与 Slash 共享同一 service，Tool schema 不能携带 objective；
- [x] `goal_completion` 在没有独立 runner 时明确 blocked，不能签发自动实验；
- [x] Source Router、Review filter、Engine composition、权限与三端协议测试通过；
- [x] Ruff 与相关小模块测试通过；按用户要求未运行全量测试。

本次定向验证为 478 passed、1 skipped：Goal 专项 8，Source/Review/UI/Permissions 389，
Candidate/Eligibility/Proposal/Experiment 81。

## 自我审视与未完成项

本切片证明“显式、当前、可撤权的用户需求能够进入 Candidate”，没有证明能力已经实现或 Goal 已被
候选改动满足。以下仍独立待办：

1. [EVO-06.1c3](EVO-06-1c3-durable-tool-catalog-miss-opportunity.md) 已把 exact Tool Search miss
   提升为机械 Evidence；自然语言缺失意图仍不能使用瞬时日志；
2. [EVO-06.1c4](EVO-06-1c4-cross-source-opportunity-prioritization.md) 已交付跨
   Outcome/Eval/Goal/Tool Catalog 的有界 domain 聚类、影响范围和可解释 Prioritization；
3. 可验证语义同根聚类仍待结构化 capability taxonomy，不能用 LLM 猜测替代；
4. EVO-06.2：完整 Capability Proposal 和真实 `goal_completion` acceptance runner；
5. Goal 完成只能作为用户事实之一，仍需 before/after、回归与长期 Outcome 证据。
