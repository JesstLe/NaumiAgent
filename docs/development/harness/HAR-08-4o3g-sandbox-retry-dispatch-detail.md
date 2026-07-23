# HAR-08.4o3g Sandbox Retry Dispatch Detail

## 1. 交付结论

本切片为一个既有 Sandbox retry dispatch 提供工作区隔离、只读、可校验的详情投影，补齐
HAR-08.4o3f 启动恢复队列中的“先审查，再决定是否恢复”。它不 claim ticket、不续租、不恢复执行，
也不删除或归档事实。

用户与 Agent 共用同一底层 Service/Tool：

```text
/harness eval sandbox retry-detail <retry-action> \
  --dispatch <dispatch-id> [--assessed-at <ISO8601>]
```

New UI 将命令保留在共享 Slash 通道并渲染 Markdown；Textual TUI 复用同一命令与 Tool，不维护第二套
查询逻辑。

## 2. 权威读取边界

`HarnessStore.get_sandbox_retry_detail()` 在一个 SQLite 只读连接中读取并机械校验：

1. `retry_action_id + dispatch_id` 成对匹配的 retry dispatch；
2. accepted retry receipt 与 cancel receipt chain；
3. 原始 Sandbox Eval Request Manifest；
4. 当前 admission ticket fence（若已 claim）；
5. 与 Manifest 同 batch/suite 的连续 H5a 持久样本前缀；
6. dispatch、ticket 与 receipt 的 digest、epoch、状态及终态一致性。

读取不会调用可使过期 ticket 转态的 `get_sandbox_admission()`。固定 `assessed_at` 只影响恢复分类，
不改变数据库。

## 3. 公开投影

`HarnessSandboxRetryDetailSnapshot` 是严格、冻结、拒绝额外字段的 schema v2 模型。v2 在 v1
基础上补入 source ticket digest 与保护引用。它公开：

- retry action、dispatch、retry/cancel receipt 的 ID 与 digest；
- dispatch state/epoch、时间与恢复分类；
- Manifest identity、源码 revision/tree、Profile digest、检查规格摘要；
- 当前 ticket ID/state/epoch/lease/digest；
- 连续 H5a 的 index/status/result digest；
- retention 后续切片必须保护的显式引用；
- 仅在 `pending` 或 `recovery_required` 时生成的 receipt-bound resume 命令。

它不公开 workspace 绝对路径、owner、actor、用户 reason、execution authority、permission/run grant、
命令输出或原始证据路径。

Snapshot 使用 canonical JSON SHA-256，并由 digest 派生 `hsrrd_` ID。模型重建时会重新校验 resume
identity fence、连续 H5a、保护引用集合与 snapshot digest，篡改会 fail closed。

## 4. 恢复分类

| 分类 | 含义 | 提供 resume |
|---|---|---|
| `pending` | 尚未首次 claim | 是 |
| `live` | 当前 ticket lease 有效 | 否 |
| `recovery_required` | claim 后 lease 已过期 | 是 |
| `reconcile_required` | ticket 已终态但 dispatch 未对账 | 否 |
| `clock_regression` | 评估时钟早于持久更新时间 | 否 |
| `terminal` | ticket/dispatch 已一致终态 | 否 |

## 5. Retention 保护集合

详情显式列出 dispatch、retry receipt、cancel receipt、Request Manifest、source ticket、
当前 retry ticket（若有）和每个连续 H5a sample result。HAR-08.4o3h 已消费该集合
生成只读 retention preview；详情本身仍不定义保留期限、不删除记录，也不签发 prune receipt。

## 6. 验收证据

聚焦验证覆盖真实临时 Git workspace、Request Manifest、cancel/retry/dispatch/ticket 与 H5a；读取
前后逐表 SQLite facts 完全一致；交叉工作区和 identity 错配返回 `not_found`；公开 JSON/Markdown
不泄露敏感内部字段；篡改 resume 命令被拒绝；Tool 为 `read_only + concurrency_safe`；真实 Engine
Slash 与 New UI 共享 submit 通道均复用同一权威。

只运行本切片相关的 Python/JavaScript 测试与受影响文件静态检查，不运行全量测试。

## 7. 后续依赖

HAR-08.4o3h 已读取本详情的完整保护引用并生成有界、只读、可解释的候选清单。下一最小切片
HAR-08.4o3i 是显式 prune receipt authority；清理执行仍必须独立交付，不能直接从详情增加删除按钮。
