# HAR-08.4o3d Sandbox Retry Dispatch Catalog

## 状态

已实现，2026-07-23。

本切片为 HAR-08.4o3a-4o3c 的 durable retry dispatch 建立工作区隔离、有界、可跨进程继续翻页的只读目录。
它是后续 Bridge 启动恢复、dispatch retention 和历史详情的共同权威入口，但自身不 claim ticket、不续租、
不重放 Tool，也不删除记录。

## Catalog 合同

`HarnessStore.list_sandbox_retry_dispatches()` 返回
`HarnessSandboxRetryCatalogPage`：

- `workspace_root`：规范化工作区；
- `assessed_at`：本轮所有页固定的 UTC 评估时间；
- `state_filter`：`all | open | terminal`；
- `limit`：1..100；
- `items`：最多 `limit` 个完整校验的 dispatch；
- `next_cursor`：空字符串表示结束。

目录按 `updated_at DESC, retry_action_id ASC` 排序。新增：

- `idx_harness_sandbox_retry_dispatch_catalog`
- `idx_harness_sandbox_retry_dispatch_state_catalog`

索引通过 Store v21 的幂等 schema 初始化补入，不提升数据库版本。

## 每项权威验证

Catalog 不把数据库行原样返回。每项必须同时通过：

1. dispatch immutable digest 与 `dispatch_id` 校验；
2. accepted retry receipt digest 校验；
3. action、retry receipt、request SHA、execution authority 在 receipt/dispatch 间一致；
4. 原 Request Manifest 能被 Pydantic 重新解析，workspace、request、batch、suite identity 一致；
5. 当前 admission ticket 的 digest、workspace、sandbox lane、owner、epoch、execution authority 和
   requested samples 与 dispatch/manifest 一致；
6. 终态 dispatch 与 ticket 的 state/terminal code 一致；
7. H5a 结果逐项验证，并且 sample index 是从 0 开始、不超过 requested samples 的连续前缀。

任一链条缺失、摘要损坏或 fence 漂移时，整个读取失败关闭，不把可疑记录显示为可恢复。

## Recovery 分类

Catalog 只机械分类，不执行恢复：

| `recovery_status` | 含义 |
|---|---|
| `pending` | dispatch 尚未绑定 ticket |
| `live` | claimed ticket 的 lease 在固定评估时间仍有效 |
| `recovery_required` | ticket 已 expired，或 lease 已到期但 Store 尚未做状态转换；只表示需要恢复 |
| `reconcile_required` | ticket 已终态而 dispatch 尚为 claimed，需先同步终态 |
| `clock_regression` | ticket 更新时间晚于评估时间，禁止据此恢复 |
| `terminal` | dispatch 与 ticket 已处于一致终态 |

因此，catalog 不会把“进程不在了”直接等价为“可以接管”；只有持久 ticket lease fence 到期才标记
`recovery_required`。

## Opaque Cursor

Cursor 是 canonical JSON + SHA-256 envelope 的 URL-safe Base64，最大 1024 字符，绑定：

- schema version；
- workspace path digest，不暴露路径；
- state filter；
- 固定 `assessed_at`；
- 上页最后一项的 `updated_at + retry_action_id`。

解码拒绝未知字段、非法 Base64/JSON、摘要变化、版本不兼容、跨 workspace、filter 漂移、显式评估时间漂移和
非法 action/time。调用方只提交 cursor 而未提交 `assessed_at` 时，Store 从已校验 cursor 继续使用第一页时间；
显式提交不同时间仍失败关闭。

Cursor 提供只读完整性和作用域绑定，不是认证凭据。跨页不持有 SQLite 长事务；并发更新遵循
read-committed，刷新首屏才能得到新的排序视图。

## Tool 与 Slash

新增只读 Agent Tool：

```text
harness_eval_sandbox_retries(
  state = all|open|terminal,
  limit = 1..100,
  cursor = "",
  assessed_at = optional ISO8601
)
```

新增共享 Slash：

```text
/harness eval sandbox retries
  [--state all|open|terminal]
  [--limit 1..100]
  [--cursor <opaque>]
  [--assessed-at <ISO8601>]
```

Slash 通过 `AgentEngine.execute_tool()` 调用同一个只读 Tool；New UI 与 Textual TUI 不各自读取 Store。
Renderer 不显示 owner ID 或 execution authority，只显示 dispatch/action、retry/cancel receipt、batch/suite、
H5a、ticket fence、lease 和机械分类。HAR-08.4o3e 已补齐 receipt-bound resume 入口；
`pending/recovery_required` 项会生成绑定既有 dispatch/receipt 的命令，不创建新 action，也不重新消费
cancel receipt。

New UI 的 Sandbox Eval command parser 明确排除 `cancel/retry/resume/retries` 控制子命令，避免把控制词错当成
Profile check ID 并打开错误的 live Batch 页面；这些控制命令保持在共享 Slash channel。

## 验收证据

- 三个真实 SQLite dispatch 按 live、recovery-required、terminal 稳定排序；
- HAR-08.4o3e 原子产生的 pending dispatch 可被 catalog 读取且不伪造 ticket/lease；
- limit=2 跨新 `HarnessStore` 实例继续第二页，无重复；
- open/terminal filter 只返回对应 durable states；
- cursor 绑定 workspace、评估时间和 filter，并拒绝篡改；
- 2/5 H5a 连续前缀在 catalog 中保持 2/5；
- 非连续 `0,4` H5a 失败关闭；
- terminal ticket 被改成不同 state/code 时失败关闭；
- catalog 索引存在且全量过滤查询使用目标索引；
- 只读 Tool 声明 concurrency-safe，参数严格有界；
- Tool 输出包含 retry action、cancel receipt/SHA，不泄露 owner/execution authority；
- Slash 通过同一个只读 Tool pipeline；
- New UI 不把 `retry/retries` 误解析成 Sandbox check；
- 真实 Engine + Git workspace + SQLite expired dispatch 可由共享 Slash 读出并明确标记为只读恢复事实；
- 空目录返回明确中文结果，不创建任何 dispatch。

## 自我审视与未完成

已确认：

- Catalog 不是恢复 executor，也不拥有 dispatch 状态迁移；
- 不是只按 `state` 猜测恢复资格，而是校验完整 receipt/manifest/ticket/H5a 链；
- 固定评估时间不要求用户手工维护第二份隐藏状态；
- Agent Tool、New UI Slash 与 Textual TUI Slash 共用 Service/Store；
- 首次 retry 和 catalog recovery 的 action 语义不会被混淆：本切片不伪造 recovery action。

HAR-08.4o3f 已让 Bridge 与 TUI 启动时复用本 catalog 的 `state=open, limit=20` 只读扫描，
以 tamper-evident snapshot 建立人工恢复队列；启动过程不会 claim 或重放。

仍未实现：

- dispatch detail typed protocol 与 New UI 专用历史页；
- retention policy、保护集合、preview/prune receipt；
- 跨主机 admission；
- Linux/Windows 的真实隔离 Worker CI。

HAR-08.4o3e 已实现 receipt-bound resume authority，并补齐 accepted intent 与 pending dispatch 的原子落盘；
HAR-08.4o3f 已实现启动后可发现但不自动执行的恢复快照。下一切片应比较 HAR-06 retention 与 dispatch
detail，不能自动重放或删除 catalog 中的任务。
