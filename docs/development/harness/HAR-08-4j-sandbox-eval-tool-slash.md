# HAR-08.4j Sandbox Eval Tool and Slash Surface

## 目标

把 HAR-08.4i 的共享 Service 变成用户和 Agent 都能正式触发的能力，同时保持单一治理链：

- Agent 调用 `harness_eval_sandbox` Tool；
- 用户调用 `/harness eval sandbox ...`；
- Slash 必须通过 `engine.execute_tool()`，不能直接调用 Service；
- 两条入口使用同一 Tool schema、Permission receipt、Service、H5a 和完成 renderer。

## Tool 合同

`harness_eval_sandbox` 是可执行、非 destructive、concurrency-safe Tool，声明只委托 `bash_run`。参数全部显式：

```text
check_ids: 1..80 个唯一 Profile check IDs
samples: 5..100
batch_id: 1..128 字符的稳定 Batch identity
run_id: 当前 Runtime/会话的稳定运行 identity
```

Tool 不接受任意 command、cwd、environment、网络或依赖安装参数；命令只能来自受信 Profile，真正执行仍由
HAR-08.4i Service 与 ARC-04 Worker 完成。成功时使用共享 `render_sandbox_eval_batch_receipt()`，展示 batch、
Suite、H5a 数量、checks、跨恢复 grant 数、request 和完成 receipt；失败时展示稳定错误 code 与安全中文说明。

## 权限语义

`TOOL_PERMISSIONS` 为该 Tool 建立独立 `harness_eval_execution` family：

- BYPASS、PERMISSIVE、MODERATE、STRICT 可用，LOCKDOWN 拒绝；
- bounded medium risk、每 session 最多 10 次（BYPASS 保持全权限语义，不受调用上限限制）；
- 不做额外二次确认；
- 父回执必须绑定非空 `run_id`；
- `arguments_sha256` 必须精确覆盖 `check_ids + samples + batch_id + run_id`；
- 只有 `bash_run` 可以从该父回执派生子权限，Batch coordinator 再基于同一父回执签发一个可撤销 Run Grant。

因此修改 checks、samples、batch 或 Runtime identity 后不能复用旧权限。

## Slash 语法

```text
/harness eval sandbox <check-id...> [--samples 5] [--batch <id>]
```

- check IDs 必须位于 options 之前，顺序就是执行顺序；
- 重复 check、缺失 check、重复/未知 option、option 缺值或 option 后继续追加 check 均只显示用法，不创建权限回执；
- `--samples` 默认 5；
- 未给 `--batch` 时路由生成 `sandbox-<随机 identity>`；
- 路由先创建/恢复当前 session，生成 `manual:<session-id>` run identity，再构造包含全部显式参数的 ToolCall；
- CLI、TUI 与 New UI 的 slash backend 都经过共享 `_handle_command`，不会分叉解析器。

## 真实验收

macOS 本地真实 Shell Worker 场景执行：

1. 初始化并信任真实临时 Git workspace/Profile；
2. 调用 `/harness eval sandbox unit --samples 5 --batch sandbox-surface-1`；
3. 5 个 samples 均从精确 commit materialize，并实际运行 Profile 中的 Python 命令；
4. H5a 出现连续 0..4 五条 passed records；
5. artifact 目录产生五份包含真实命令输出的日志，sandbox 临时目录全部清理；
6. 权限历史包含一个 `harness_eval_sandbox` 父回执和五个 `bash_run` 子回执；
7. 五个子回执绑定同一 batch Run Grant，完成后该 grant 验证为不可执行；
8. 完成视图显示 5/5、batch、Suite 与 receipt。

Tool/schema/permission/registration/参数错误路径另有小模块测试；未运行全量测试。

## 当前边界与后续

HAR-08.4k 已将 `HarnessSandboxBatchCoordinator.on_progress` 转换为闭集 Runtime event，并同步到 New UI/TUI；
两端直接显示 Store-confirmed persisted 数，不估算样本进度。详见
`HAR-08-4k-sandbox-eval-typed-progress.md`。

真实 `queued`、跨进程排队位置、取消和重试仍依赖 durable Sandbox admission authority，不能由前端或进程内
semaphore 推断。
