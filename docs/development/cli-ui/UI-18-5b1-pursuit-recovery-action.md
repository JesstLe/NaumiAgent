# UI-18.5b1 Pursuit 受控恢复动作

## 目标

让 New UI 和 Textual TUI fallback 消费 HAR-10.8d 的恢复请求账本，在用户明确触发后通过现有
`pursuit_resume` ToolExecution 恢复一个持久 Pursuit。界面只展示 Python 恢复权威投影，不根据工具文案、
heartbeat 文本或本地按钮状态推断准入和完成结果。

本切片只交付 `resume`。`takeover`、`cleanup`、跨 run attempt catalog、cursor/retention 和自动恢复不在
范围内。

## 权威与数据流

```text
PursuitStore + Harness lease/heartbeat
             │
             ▼
build_pursuit_recovery_snapshot(schema 2)
  ├─ resume_action: available/busy/blocked/unavailable
  └─ attempts: requested/admitted/resolved/failed（最近 5 条）
             │
       goals/snapshot
             │
             ▼
New UI Goal 页面 ── x ── pursuit/recovery/resume
                              │
                              ▼
                    JsonlEngineBridge
                              │
                   重新读取 Python authority
                              │
                              ▼
               AgentEngine.execute_tool(pursuit_resume)
                     权限/持久回执/账本
                              │
                              ▼
              pursuit/recovery/action_result
                  + 刷新的 goals/snapshot
```

Textual TUI/CLI fallback 渲染同一个 `PursuitRecoverySnapshot`，展示准确命令
`/pursue resume <run_id>` 和最近 attempt；用户提交该命令时继续走共享 Slash 路由和
`pursuit_resume` ToolExecution，不建立第二套恢复逻辑。

## Typed contract

### `PursuitRecoverySnapshot` schema 2

- `resume_action.action` 固定为 `resume`；
- `resume_action.state` 仅允许 `available | busy | blocked | unavailable`；
- `resume_action.code/reason` 由 Python 根据 run、checkpoint、lease、heartbeat 和 attempt ledger 生成；
- `resume_action.command` 必须精确等于 `/pursue resume <run_id>`；
- `attempts` 最多 5 条，状态仅允许 `requested | admitted | resolved | failed`；
- attempt 不公开 `source_request_sha256`，时间、checkpoint、result code 与 boundary decision 均有界；
- attempt 阶段字段必须和 HAR-10.8d 状态机一致，畸形 terminal/admission 组合由 Node 消费边界拒绝。

旧 schema 1 仍可读取，但没有可执行 action，前端不得凭旧字段自行开启按钮。

### Client event

```json
{
  "type": "pursuit/recovery/resume",
  "payload": {"run_id": "pursuit-example"}
}
```

该事件要求协商 `pursuit_recovery_actions` capability。`run_id` 使用与 Goal/Pursuit snapshot 相同的稳定
ID 校验，未知字段被丢弃。

### Server event

`pursuit/recovery/action_result` schema 1 返回：

- 与请求绑定的 `run_id`；
- `requested | admitted | resolved | failed | blocked | error` 状态；
- 稳定、机器可判定的 `code`；
- 去 ANSI、去控制字符且最多 4000 字符的用户说明；
- 若账本已有记录，返回公开 attempt；
- 返回重新计算后的 `resume_action`，随后再发送完整 `goals/snapshot`。

结果状态以持久 attempt 为准。即使 ToolResult 文案写着“成功”，attempt 为 `resolved/operation_busy` 时，
UI 仍展示账本状态，不解析文案。

## 准入与并发

Bridge 在创建后台 ToolExecution 前重新读取一次恢复 snapshot：

- run 不存在：`run_not_found`；
- checkpoint 缺失或损坏：`checkpoint_required/checkpoint_invalid`；
- reconcile/inconsistent：阻断并展示权威原因；
- live worker 或 active attempt：`busy`，不重复发起；
- attempt ledger 不可验证：失败关闭；
- 同一 `request_id` single-flight；
- Bridge 最多同时保留 4 个恢复控制任务；
- Bridge 关闭时取消并 join 所有未完成恢复任务。

恢复任务必须异步执行，避免 moderate 权限确认在 Bridge 读循环内自锁。权限仍由
`AgentEngine.execute_tool()` 控制；bypass 只是由既有权限层直接批准，不绕过持久回执和恢复账本。

## 用户体验

- Goal 页面仅在 Python 返回 `available` 时允许按 `x`；
- capability 未协商、请求正在进行或 authority 阻断时，不发送网络事件；
- pending、成功说明和错误说明分别显示，刷新 snapshot 后以持久状态覆盖瞬时提示；
- action、attempt 和风险状态使用不同语义色；
- Textual TUI fallback 显示动作状态、原因、精确共享命令和最近 attempt；
- 旧 Bridge 明确提示使用兼容 `/pursue resume <run_id>`，不伪装 typed action 已执行。

## 验收证据

- Python projection 覆盖 waiting/active/terminal/reconcile、checkpoint 缺失/损坏、ledger 失败和 active
  attempt；
- Bridge 测试证明可恢复请求进入 `pursuit_resume` ToolExecution，结果来自真实 `PursuitStore`
  attempt，而不是 ToolResult 文案；
- Bridge 测试证明缺少 checkpoint 时不调用工具；
- Node protocol 拒绝 command/run 不匹配、attempt/status 不匹配及畸形阶段字段；
- Node state 仅在 capability、authority 和 single-flight 条件同时满足时发送事件；
- Goal 页面渲染 action、pending/result 和最近 attempt；
- CLI/TUI fallback 与 New UI 使用同一个 Python snapshot 和共享工具命令；
- 相关 Python、Node 小模块测试、语法检查、ruff 和 `git diff --check` 通过。

## 当前不足与下一步

- 本切片不提供 takeover/cleanup；这些动作需要独立权限、fencing 和 durable receipt，不能复用 resume
  按钮偷渡。
- attempt 只展示最近 5 条，没有 cursor、retention 和跨 run catalog。
- ToolExecution 已返回 typed action result，但长循环后续终态仍依赖 Goal snapshot 刷新；尚未新增专用
  attempt push stream。
- recovery attempt 与 Pursuit checkpoint 同库，Harness lease/heartbeat 属于另一事务域，仍不宣称
  exactly-once；HAR-10.8e 已用更高 RunLease epoch fencing 和同库不可变回执补齐显式 admitted
  terminal reconciliation，但自动 bounded outbox 仍未实现。
- 未执行 24 小时 soak、进程 kill 和跨平台故障矩阵，UI-18 与 HAR-10 继续保持 `partial`。

下一步应回到跨文档依赖图选择最小用户可见切片，不线性扩张 UI-18.5。候选优先级由 Harness、UI-13、
UI-14、未来架构和自进化文档的共同前置决定。
