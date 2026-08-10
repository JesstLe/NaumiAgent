# EVO-05.6b2a1 Explicit Rollback Action

## 目标

把 EVO-05.6b2a 的内部 fenced rollback service 暴露为一个用户与 Agent 都能真实调用、权限语义一致、跨 New UI/TUI
一致展示的产品动作。该动作只接受 `Rollback Request ID`；调用方不能指定 slot、pointer generation、Source digest、
Boot Receipt、时间或 Outcome。

## 共享执行边界

唯一 Agent Tool 为 `evolution_revalidation_rollback_execute(request_id)`。用户入口为：

```text
/evolution revalidation-rollback-execute <rollback-request-id>
```

Slash handler 不直接调用 rollback service，而是构造同名 `ToolCall` 并进入 `AgentEngine.execute_tool()`。因此 Agent
自主调用、fallback TUI 和 New UI 经共享 Slash Router 发起的调用都经过同一条链：

```text
request_id validation
  -> PermissionChecker
  -> one allow-once decision when normal
  -> fenced rollback service
  -> durable rollback Receipt
  -> shared Markdown projection
```

这避免 Slash 绕过工具权限，也避免 UI 在 PermissionChecker 之外再次询问。

## 权限语义

`evolution_revalidation_rollback_execute` 是 `HIGH` 风险、`destructive=true` 的
`evolution_release_rollback` family：

- permissive/moderate/strict：允许，但必须 `allow_once` 或拒绝；`requires_double_confirm=false`；
- bypass：PermissionChecker 入口直接 `ALLOW`，不显示确认，不受 session call cap；
- lockdown：`MODE_BLOCKED`；
- 高风险动作不支持 session grant，避免一次批准扩展成后续回滚许可；
- normal 的一次批准只允许进入 executor，不替代 Source、paused control、boot probe、provenance 与 CAS 门禁；
- bypass 也只绕过交互确认，不能绕过上述 mechanical veto。

Session 上限为 20 次，用于限制不同 Request 的反复尝试；服务自己的 request lock、durable uniqueness 与 release-history
authority 继续负责幂等和崩溃恢复。

## 输入与失败关闭

Tool schema 拒绝额外字段，`request_id` 必须匹配 `evrerollbackreq_[0-9a-f]{24}`。服务仍会在 pointer 修改前机械验证：

1. Request、immutable Source、Rollout Plan 的 exact ID/digest；
2. HMAC-attested paused kill switch 仍 current；
3. candidate 与 baseline installed slot provenance；
4. baseline fresh `--version` boot probe；
5. expected-pointer SHA-256 CAS；
6. `data_restore_required=false`。

任何失败只返回中文 code/message，不把失败伪装为完成，也不写 workspace/Git，不启动用户进程。`data_restore_required=true`
继续等待 ARC-07.6。

## New UI / TUI 展示

两端都使用 `cli.slash_router.execute_slash_command()`，命令元数据也来自同一 `COMMANDS_META`。执行成功后展示
`render_revalidation_rollback_execution()` 的同一 durable projection，包括：

- Receipt ID、Request/Source；
- Candidate → Baseline 版本；
- pointer generation 变化；
- post-rollback Launch Resolution；
- 用户进程、workspace 与 Git 均未修改；
- `outcome_recorded=false`、不授予 promotion authority。

当前执行是 bounded local slot switch，通常不需要单独长任务进度页。若 ARC-07.6 引入数据恢复长阶段，必须新增 durable
phase/progress 事件，而不是用瞬时 spinner 冒充 authority。

## 验收证据

- 真实临时 Git baseline/candidate 与两个真实 POSIX bundle，经 Agent Tool 完成 rollback；
- 相同 Request 再经共享 Slash Router 调用，重载同一 durable Receipt；
- normal 返回单次 `CONFIRM + HIGH`，无 double confirm、无 session grant；
- bypass 返回直接 `ALLOW` 且不要求确认；
- lockdown 返回 `MODE_BLOCKED`；
- factory 注册 Tool，New UI/TUI command index 都包含新动作；
- Receipt 明示尚未形成 Outcome/promotion；
- 只运行 rollback、权限、Evolution Tool registry 与 command index 小模块，不运行全量测试。

## 明确未完成

- EVO-05.6b2b / ARC-07.6 配置与数据 snapshot/migration rollback；
- Windows 真实 `.exe` rollback runner；
- EVO-05.7 `rolled_back/superseded` Outcome authority；
- HAR-09.6 Proposal/Contract before-after 与最终 Outcome 回注。
