# UI-12.3a Durable Permission Decision Receipts

## 1. 交付目标

把 Engine 内存中的 permission bubble 从“历史事实源”降为即时交互事件，并为用户参与的终态权限决定建立可重启
读取、可防篡改、可被 ARC-04 execution grant 消费的持久回执。本切片覆盖 `allow_once`、
`session_granted`、`bypass_enabled` 和 `denied`；pending、confirmation error 与 grant revoke 仍是瞬态事件。

## 2. 回执合同

`PermissionDecisionReceipt` schema v1 绑定：

- receipt/request/session/run/call identity；
- agent、tool 与 tool family；
- canonical JSON 参数 SHA-256，不保存 command、环境变量、reason 或用户消息；
- outcome、actor、source、permission mode、risk 与可选 session grant id；
- timezone-aware 决策时间和完整 receipt digest。

同一 `session_id + call_id` 只能存在一个终态决定。完全相同的 retry 返回首次回执；不同 outcome、参数、来源或
mode 冲突并 fail closed。允许类决定必须先持久化再执行；bypass 只有在回执成功后才切换全权限模式；session
grant 回执失败会撤回刚创建的内存授权。拒绝始终保持拒绝，即使审计写入失败也不会放行。

## 3. 持久化与生命周期

- Composition Root 惰性装配 `runtime_data_dir/permission-decisions.db`；构造 Engine 或只读诊断不创建文件。
- SQLite `user_version=1`；POSIX 父目录与数据库分别收紧为 0700/0600。
- 未版本化非空库、未来 schema、错误路径类型、JSON 篡改和索引列不一致均拒绝读取。
- Store Catalog 登记为 `runtime.permission_decisions`、restricted、audit-long-term。
- 当前 Permission Center 只读取活动 session 的有界历史；会话切换不会串历史。session 删除暂不删除审计行，
  后续需与正式 retention/export policy 一起决定 tombstone 或物理清理。

## 4. UI 与 ARC-04 集成

新 UI 与 TUI 继续消费同一个 `PermissionPanelSnapshot`。若 Engine 提供 durable getter，面板不再把内存 bubble
冒充历史；payload 和文本 renderer 显示 actor、source、decided_at、call/run 与 receipt id。bubble 仍负责
pending 和当前操作反馈。

`ExecutionGrantAuthority.issue()` 会读取 `authorization_reference` 指向的真实回执，并机械核对 session、run、
call、tool、参数摘要、permission mode、source 与 outcome。缺失、拒绝或不匹配回执不能签发 grant，任意字符串
不再构成授权证明；签发时回执还必须位于 300 秒 freshness window 内，未来时间同样拒绝。

## 5. 验收证据

- issue→关闭→reopen→get/list 保持同一回执，数据库 bytes 不含原始参数 secret；
- retry 幂等，冲突终态决定拒绝，denied 永不授权；
- tampered/future-schema/wrong-type path fail closed；
- Engine 的 allow once、session grant、bypass 与 deny 定向路径通过；
- Permission Center payload/TUI renderer 展示 durable actor/source/time；
- execution grant 对缺失、来源错误、参数变化和调用绑定变化拒绝；
- RuntimePaths、Resource override、Store Catalog 与 Doctor 共用同一物理路径事实。

## 6. 未完成边界

- UI-12.2 pending queue、UI-12.6 断线恢复尚未实现；
- 非交互 policy allow/block、Hook block、plan-mode block 尚未形成同一 durable decision taxonomy；
- session grant 本体仍是 Engine 内存对象，UI-12.5 workspace grant 未实现；
- ARC-04.2b 已实现 ImmutableToolJob admission 并消费本回执；daemon transport、ToolJob lifecycle completion
  receipt 和真实 Shell worker 尚未实现；
- 下一最小跨文档切片应做 ARC-04.2c lifecycle receipt，先冻结副作用未知态和终态幂等，再接 Shell worker。
