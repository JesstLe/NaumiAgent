# UI-10.1 Workbench Snapshot 一致性设计

## 目标

让现有 `WorkbenchService.dashboard_snapshot()` 成为终端 Workbench 页的唯一权威快照：用户可用
`/workbench` 显式请求，前端可识别重复、乱序、会话串线和后端进程重启，并在事件断序时请求
完整快照。首帧只读，不创建任务、worktree、Agent、审批或模型调用。

## 当前缺口

- `workbench/snapshot` 没有 revision 或生产者实例标识，前端无条件覆盖。
- 没有 `workbench/request`，因而 `/workbench` 无法从当前会话主动刷新。
- `workbench/event` 没有可验证的连续序号，丢包后继续追加会形成看似正常的错误时间线。
- 快照已有任务、issue、lease、approval 等真实数据，但没有稳定 counts 和 active selection。

## 权威与版本语义

- producer：现有 `WorkbenchService`；不创建第二个 Store 或前端推导层。
- `stream_id`：每个 Service 实例生成一次；后端重启后变化。
- `revision`：按 session 独立计数。快照规范内容指纹变化时加一；相同内容重复查询保持 revision。
- `schema_version=1`、`full=true`；完整快照可在 stream 变化时替换旧状态。
- 前端只接受当前 session。相同 stream 下 revision 小于等于当前值时幂等忽略。
- revisioned `workbench/event` 必须是当前 revision + 1；断序、缺少基线或 stream 改变时不追加，返回
  `refresh_workbench` action 请求完整快照。
- 旧的无 revision 事件只保留兼容追加行为，后续 UI-10.5 移除兼容窗口。

## 快照摘要

服务端提供：mission/task/worktree/review/failure counts，以及当前 active mission/task/worktree/review。
worktree 来自 issue 的 `related_worktree` 和 active lease 的 `worktree_name` 去重；review 以等待审批为准。
active task 优先 `in_progress`，active mission 优先 `active/planning`，其余为空，不由 UI 猜测。

## 请求与失败

`workbench/request` 接受 `session_id`、`known_stream_id`、`known_revision`。Bridge 只允许当前会话，
调用一次 `dashboard_snapshot()` 并返回完整快照。Service 不可用、会话不匹配、查询失败均返回固定中文
错误码；异常内部信息不进入 UI。

## 本切片边界

实现协议、版本状态机、`/workbench` 请求入口和真实 Store→Service→Bridge→Node 验证。本切片不实现
UI-10.2 Overview 视觉页、不发增量事件、不提供 approve/reject/cancel 动作，也不实现 TUI fallback。

## 验收

- 相同数据重复请求 revision 不变；数据变化 revision +1；新 Service 的 stream id 改变。
- 同 stream 的重复/旧 snapshot 不覆盖；跨 session snapshot 被拒绝。
- revisioned event 连续时追加，断序时只触发一次完整刷新且不污染时间线。
- `/workbench` 不进入聊天消息或模型；Bridge 首帧只读。
- 真实 SQLite Workbench Store 经新 Service、Bridge JSONL 和 Node reducer 得到同一 counts/selection。
