# EVO-06.4c1a Shadow Run Admission

## 目标

为 current EVO-06.4b Observation Contract 签发一次有界、独占、可撤销且可机械复验的 Provider 调用准入。
本模块只建立 authority 与预算 envelope，不调用 Provider、不构造请求、不写 Observation Receipt、不执行候选
Tool，也不授予 Limited Activation。4c1b Runner 必须在每次调用前后重读实时 `ready` View，不能仅凭 SQLite
中的 Admission JSON 或 Run Grant 直接调用模型。

## 为什么不能复用普通 BudgetTracker

`BudgetTracker` 是会话内事后累计器：没有持久 reservation、workspace 隔离、跨 Runtime fence、崩溃恢复或
原子并发裁决。4c1a 因此复用现有 Harness authority：

1. `HarnessStore.acquire_run_lease()` 提供 workspace + runtime run ID 的独占 owner/epoch；
2. `RunDelegationGrantAuthority` 绑定父权限回执、session/run、workspace digest、lease fence、下游 Tool scope
   与硬截止时间；
3. 4b Contract 提供模型、样本、token、成本与 wall-clock 上限；
4. Admission Store 只允许同一 workspace/Candidate 存在一个 active Admission。

## 签发输入

`issue` 必须同时满足：

- 4b View 为实时 `ready`，且 exact Contract ID/SHA/binding、模型 contract SHA 和 sampling policy SHA 未漂移；
- 当前父 `PermissionDecisionReceipt` 允许执行，tool 为
  `evolution_capability_shadow_run_admission`，arguments SHA 精确绑定 `issue/candidate_id/run_id`；
- Service 从 Permission Store 重读同一 receipt 并要求完整对象一致，拒绝只在内存中伪造的投影；
- 父回执绑定非空 session/run，并显式委托唯一未来 scope
  `evolution_capability_shadow_observation_run`；
- 父回执仍位于 Run Grant 的 300 秒签发窗口；
- Harness 可以取得 `runtime` Run Lease，Run Grant 可以绑定同一 owner/epoch；
- Candidate 当前没有其他 active Admission。

未来 Runner 名称只是委托 scope，不会在 4c1a 注册为可调用 Tool，也不会被生产模型看到。

## Admission Contract

不可变、content-addressed Contract 冻结：

- Candidate、4b Contract ID/SHA/binding；
- Model Contract SHA 与 Sampling Policy SHA；
- 父权限 receipt ID/SHA、session/run；
- 当前 Runtime instance ID；
- Harness lease owner/epoch/expiry；
- Run Grant ID/SHA/expiry 与唯一 delegated Runner Tool；
- 最大 calls、input/output token、µUSD 成本和 wall-clock；
- Admission 截止时间，机械取 lease、grant 和 `wall-clock + 60s cleanup` 的最小值。

安全事实：

- `provider_call_scope_reserved=true`；
- `provider_call_completed=false`；
- `observation_recorded=false`；
- `candidate_execution_authorized=false`；
- `side_effects_allowed=false`；
- `activation_authorized=false`。

只有实时 View 为 `ready` 时，投影字段 `provider_call_authorized=true`。这表示 4c1b 可在同一 authority 下尝试
有界调用，不表示已经调用或允许跳过 child permission、调用前重验、使用量核验和终态清理。

## 实时状态与撤权

- `missing`：尚未签发；
- `ready`：4b、Runtime、Run Grant、Run Lease、expiry 全部 exact；
- `contract_revoked`：4b descriptor/catalog/model/sample 任一漂移；
- `runtime_detached`：当前进程不是签发 Runtime；
- `authority_revoked`：Grant 撤销/篡改或 lease release/epoch takeover/篡改；
- `expired`：Admission 到达硬截止时间；
- `revoked`：用户显式撤销；
- `released`：为未来 Runner 成功/失败终态预留，4c1a 不会伪造该状态。

已有 `ready` Admission 仅在同一 Runtime、同一 run 下幂等返回；其他 run/Runtime 必须收到 active conflict。
`revoke` 必须使用同一 `run_id` 和新的精确父权限回执，只允许签发 Runtime 操作。Service 先撤销 Run Grant，
再释放 exact lease owner/epoch，二者完整后才写终态；清理不完整返回稳定错误，不伪装为 revoked。

## Store 与并发

Store 使用 partial unique index 保证同一 workspace/Candidate 仅一个 active Admission。两个 Runtime 并发签发
时最多一个成功；失败方清理自己已经取得的 Grant/lease，并返回稳定 Admission 错误。读取时同时验证：

- 外层 payload SHA；
- 内层 content-addressed Admission ID/SHA；
- `admission_id/candidate_id/observation_contract_id/run_id/run_grant_id` 关系裁决列；
- active 与 terminal time/reason 的状态一致性。

## 双通道与界面

- Agent Tool：`evolution_capability_shadow_run_admission`，支持 `inspect/issue/revoke`；
- CLI/Textual TUI：`/evolution capability-shadow-run <candidate-id>`、
  `capability-shadow-run-status`、`capability-shadow-run-revoke`；
- New UI：同名 typed actions；issue/revoke 统一经 Engine 权限回执入口，status 只读取实时 View；
- Renderer 展示 Contract、Run/lease epoch、Grant、截止时间、完整预算和未执行/未激活事实。

## 验收证据

- [x] 真实 4b Contract + Harness Lease + Run Grant + Permission Receipt 签发 Admission；
- [x] Contract、Grant、lease、Runtime 和 expiry 任一失效均撤销 provider-call authority；
- [x] 双 Runtime 并发最多一个 active owner，失败方返回稳定错误；
- [x] 显式撤销同时清理 exact Grant 与 lease；
- [x] 内外摘要、关系字段篡改与未持久化父回执投影均 fail closed；
- [x] Agent Tool、CLI/TUI 与 New UI typed action 共享同一 Service；
- [x] 真实 ARC-04 → 3b2c → 4a → 4b → 4c1a 链路通过，源码漂移后 Admission 自动撤权；
- [x] 只运行 Ruff、py_compile、定向 Python/Node 测试和单条真实 E2E，未运行全量测试。

## 下一步

EVO-06.4c1b 实现 Bounded Observation Runner：从实时 `ready` Admission 派生不可递归的短期 child permission，
对每个样本剥离 `expected_recommendation` 后构造固定结构化请求；调用前后重验 Admission，禁止 Tool execution，
核验 provider model、usage、cost、finish reason 和 schema，最后无论成功、失败、取消或超时都撤销 Grant、释放
lease，并形成无原始 CoT、无私密异常的 content-addressed Observation Receipt。4c2 才能进行 precision、
false-positive、indeterminate、成本和稳定性聚合。
