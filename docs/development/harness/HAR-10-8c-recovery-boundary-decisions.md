# HAR-10.8c 恢复链路机械边界裁判

## 目标

HAR-10.8b 已把新 Pursuit 主循环的完成、等待、阻塞、取消和预算越界收口到确定性裁判，但
`resume_persisted()`、background reconcile 和 checkpoint 持久化错误仍有直接写
`PursuitRun.status/phase` 的历史分支。

本切片让恢复链路也只消费同一 `PursuitBoundaryFacts → PursuitBoundaryDecision` 权威合同。恢复器负责
收集 checkpoint、行动账本、后台回执和 interaction authority 事实；它不能自行制造另一个终态系统。

## Schema 2

`PursuitBoundaryDecision.schema_version=2` 增加：

- `criteria_state=known|unknown`：区分“明确没有成功标准”和“旧恢复记录没有 criterion 快照”；
- `waiting_detail`：最多 300 字符的单行、受限等待事实；
- `blocker_detail`：最多 300 字符的单行、受限恢复阻塞事实；
- 恢复专用 blocker code。

详情进入内容寻址 facts 和 `facts_sha256`，不是 UI 临时拼接。控制字符、首尾空白、没有对应
waiting/blocker 却携带详情、unknown criterion 携带计数或最终验证都会被拒绝。

Schema 1 保持可读：

- 复验时使用 schema 1 原始 facts 和 decision 哈希字段集；
- schema 1 不允许伪装携带 8c 新事实；
- Store 重存已解析的旧决策时，若原始 JSON 与新增默认字段只存在语义等价差异，不产生 identity conflict；
- New UI protocol 严格接受 schema 1/2，拒绝其他版本，并保留实际版本号。

所有决策除内容摘要外还会重新执行确定性裁判并核对 status、code、reason、next action、
terminal/resumable，攻击者不能通过同时改 payload 和重算 SHA 把机械事实映射为另一结果。

## 稳定恢复裁判码

| code | status | 触发事实 | 用户下一步 |
|---|---|---|---|
| `criteria_state_unknown` | running | 旧等待记录缺少 criterion 快照 | 加载并验证 checkpoint |
| `checkpoint_persistence_error` | blocked | checkpoint 写入失败 | 审查持久化故障后新建后续运行 |
| `checkpoint_required` | blocked | 运行没有可验证 checkpoint | 补全或人工审查 checkpoint |
| `checkpoint_inconsistent` | blocked | goal、轮次、证据游标、worktree 或终态摘要不一致 | 核对持久事实 |
| `reconcile_required` | blocked | action ledger/外部副作用尚未核对 | 完成 HAR-10.5 reconcile |
| `waiting_for_background` | waiting | reconcile 证明后台任务仍活跃 | 等待终态回执 |
| `waiting_for_interaction` | waiting | durable interaction authority 仍为 pending | 回答同一 interaction |
| `interaction_required` | blocked | interaction 缺少 authority、记录或 subject 不一致 | 修复 authority 或重新提问 |
| `interaction_terminal` | blocked | interaction 已 expired/cancelled | 用户创建新的受治理决定 |
| `resume_inconsistent` | blocked | 同时出现后台等待和 interaction 恢复等冲突事实 | 人工核对多个 authority |

## 生产恢复顺序

1. 读取并校验 PursuitRun 与 checkpoint；`checkpoint_error` 旧记录保持只读拒绝。
2. 在新 lease epoch 下回收后台结果并执行 typed action reconcile。
3. 核对 stable interaction authority：
   - answered：幂等补写证据并清除 checkpoint 引用；
   - pending：进入 `waiting_for_interaction`，不调用模型；
   - expired/cancelled：进入 `interaction_terminal`；
   - authority 缺失或 subject 不一致：进入 `interaction_required`。
4. 同时存在 background 与 interaction waiting 时失败关闭为 `resume_inconsistent`。
5. 缺失或不一致 checkpoint 进入对应 blocker；不改写不可信 checkpoint。
6. 安全 checkpoint 恢复为 running 裁判后，才允许进入累计预算门和下一 assessment。

resume blocker 使用 `terminal-blocked` lease fence；waiting/running 使用当前 reconcile 或 resume
commit fence。checkpoint 持久化异常在原操作的 best-effort 错误路径记录，PursuitStore 仍会拒绝
lost lease 写入。`_record_recovery_boundary_owned()` 不会为了记录 blocker 重写尚未通过一致性门的
checkpoint。

## 用户可见投影

Goal projection 沿用 HAR-10.8b 的低敏字段。New UI 与 TUI/CLI fallback 都显示最近 code、status、短
decision ID 和机械原因，不读取完整 facts，也不在前端重算恢复结论。

pending durable interaction 的 `PursuitRun.status` 从历史 `blocked` 修正为 `waiting`；phase 仍为
`interaction_required`，并保留 interaction ID。缺少 durable authority 的旧交互仍为 blocked。

## 验收标准

- checkpoint 写失败使用 `checkpoint_persistence_error`，不持久化异常正文、私有路径或 token；
- 缺失 checkpoint 使用 `checkpoint_required`；
- checkpoint goal 不一致使用 `checkpoint_inconsistent`，且模型调用为零；
- action ledger 无法证明副作用时使用 `reconcile_required`，不重放工具；
- live background receipt 使用 `waiting_for_background`；
- durable pending interaction 使用 `waiting_for_interaction/waiting`，模型调用为零；
- legacy/missing/foreign interaction 使用 `interaction_required/blocked`；
- expired/cancelled interaction 使用 `interaction_terminal/blocked`；
- background 与 interaction 双重等待使用 `resume_inconsistent/blocked`；
- 安全 reconcile/restore 至少留下一个 running 裁判；
- 旧无 criterion 快照的等待结果使用 `criteria_state_unknown`，不伪造零成功标准；
- schema 1 决策可重开、复验和重存，schema 1 新字段注入被拒绝；
- 即使重算 decision ID，事实与输出语义不一致仍被拒绝；
- New UI 严格读取 schema 1/2，并拒绝 terminal/resumable 不一致。

## 定向验证

```bash
python3 -m pytest \
  tests/unit/test_pursuit_terminal.py \
  tests/unit/test_pursuit_terminal_runtime.py \
  tests/unit/test_pursuit.py \
  tests/unit/test_pursuit_checkpoint.py \
  tests/unit/test_pursuit_lease.py \
  tests/unit/test_pursuit_reconcile.py \
  tests/unit/test_pursuit_recovery.py \
  tests/unit/test_pursuit_action_ledger.py \
  tests/unit/test_goal_panel.py -q

node --test \
  --test-name-pattern='goal snapshot is strict, bounded, and preserves stable Pursuit links' \
  frontend/terminal-ui/test/protocol.test.js

node --test frontend/terminal-ui/test/goal-pursuit-page.test.js
```

## 自我审视与未完成项

- Pursuit 生产代码不再直接写 RUNNING/WAITING/BLOCKED；旧 `checkpoint_error` 记录不会在没有 lease
  的拒绝读取路径上伪造新裁判。
- 决策是确定性代码合同，不调用模型，也不是 prompt 包装。
- PursuitStore 内的 run/current-decision/evidence 写入共享 SQLite 事务，但 PursuitStore、
  Harness lease/interaction authority 和 BackgroundTaskStore 仍不是一个事务域。
- checkpoint 读取本身损坏时，恢复请求在获得 lease 前直接拒绝，不改写原运行；该拒绝回执尚未成为独立
  durable attempt event。
- 下一切片应实现跨 Store terminal commit/outbox 或 recovery attempt ledger；在此之前不应声称
  exactly-once，也不应直接跳到 24 小时 soak。
