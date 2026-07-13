# Background Task Retention Design

## Goal

后台任务进入终态后立即释放运行资源；通知被 Agent 消费后从活跃 footer 消失；历史和输出日志仅保留 7 天且最多 100 条。

## Existing Behavior

- `_watch()` 的 `finally` 已从 `_processes` 和 `_watchers` 移除任务。
- 完成、失败、取消和超时记录永久保存在 `tasks.json`。
- `collect_notifications()` 会设置 `notified=True`，但 footer 仍统计所有历史 `failed` 和 `timed_out`，造成永久 `bg!`。

## Lifecycle

1. `running`：记录、进程和 watcher 均存在，footer 计入 `bg`。
2. `terminal_unacknowledged`：进程和 watcher 已释放；失败或超时尚未通知 Agent，footer 计入 `bg!`。
3. `terminal_acknowledged`：`notified=True`；不再计入 footer，可在 `/tasks history` 查看。
4. `expired`：超过 7 天或超出最近 100 条，删除记录及受管日志文件。

`completed` 不需要 attention，但仍产生一次完成通知。通知被收集即视为确认。

## Retention Service

`BackgroundTaskStore.prune(retention_days=7, max_records=100)` 负责：

- 永远保留所有 `running` 记录。
- 对终态记录先按完成时间降序保留未过期项，再施加 100 条上限。
- 只删除位于 `artifacts_dir` 内且文件名属于对应任务的输出文件。
- 使用解析后的绝对路径验证父目录，拒绝路径穿越和外部文件删除。
- 原子重写 `tasks.json`，返回删除记录数和日志数。

Runner 在初始化后的首次操作、任务进入终态、`collect_notifications()` 和手动 `cleanup()` 后调用轻量 prune。一次进程生命周期只需执行一次启动清理。

## User Interface

- Bridge footer 仅把 `failed/timed_out and not notified` 计入 `background_attention`。
- `/tasks` 默认展示运行中和未确认终态。
- `/tasks history` 展示已确认历史，最多 100 条。
- `background_cleanup` 的结果同时报告终止、陈旧标记、历史记录删除和日志删除数量。

## Error Handling

清理单个日志失败时保留对应记录并返回失败明细，不影响其他任务。损坏的 `tasks.json` 保持现有安全降级为空列表，但不得主动删除 artifacts。

## Verification

- 真实子进程完成后 `_processes`、`_watchers` 为空。
- 通知前失败任务显示 attention，通知后立即归零。
- 7 天和 100 条两个边界分别测试，运行中任务永不被清理。
- 路径穿越记录不能删除受管目录外文件。
- `/tasks` 与 `/tasks history` 的过滤行为有 bridge 级测试。

