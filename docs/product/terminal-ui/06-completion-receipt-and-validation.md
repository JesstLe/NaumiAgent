# 06 完成收据与验证

## 1. 目标

每次可执行任务结束时给用户一份真实、可核查、可继续操作的完成收据。它不是 Agent 自述，而是由运行事件、文件变更、测试结果、权限和 Git 状态聚合出的结构化终态。

## 2. 完成收据结构

```text
CompletionReceipt
  receipt_id
  run_id
  outcome: completed | partial | failed | cancelled
  summary
  changes[]
  validations[]
  unverified[]
  approvals[]
  risks[]
  git_state
  next_actions[]
  evidence_refs[]
  started_at / completed_at / duration_ms
```

## 3. 字段语义

- `changes`：真实文件、配置或外部对象变更；包含来源工具和统计，不凭模型文本生成事实。
- `validations`：实际执行的命令、范围、退出码、通过/失败数量和日志引用。
- `unverified`：未运行的测试、未覆盖平台、无法访问的依赖或未完成验收。
- `approvals`：用户允许、拒绝和 bypass 的动作及范围。
- `risks`：仍存在的高风险、数据损失可能、兼容性或安全限制。
- `git_state`：分支、工作区是否干净、提交和推送事实；只有真实成功才标记。
- `next_actions`：可直接触发的继续、重试失败测试、审查 diff、提交或打开任务。

## 4. 生成规则

1. `AgentEngine.run_streaming()` 的中立 `ChatRunRecorder` 在运行开始前记录 Git 基线，持续观察工具、验证和审批事件，并在返回前生成、持久化收据；Bridge 不重复推断事实。
2. Agent 可以提供 `summary` 建议，但不能覆盖验证结果和 Git 事实。
3. 缺少证据的声明进入 `unverified`，不得写成通过。
4. 取消和失败同样生成收据，保留已完成变更和恢复动作。
5. 收据写入会话存储，可被重放、Inspector 和 `/tasks` 引用。

## 5. 验证节奏

- 小切片：只运行受影响模块的 lint、单元测试和真实场景。
- 大模块完成：运行完整 Node 测试、相关 Python UI/Bridge 测试和真实端到端链路。
- 发布候选：执行项目质量清单中的 `ruff check src/`、`pytest tests/ -x`、构建和安装态启动验证。

收据必须明确测试范围，不能用“测试通过”替代“哪些测试通过”。

## 6. 时间线呈现

收据默认显示结果、摘要、测试计数和未验证计数。展开后显示文件、命令、风险和证据。失败项优先于成功项；存在人工审批需求时给出显著动作。成功收据不使用夸张庆祝，不掩盖残余风险。

## 7. 协议

新增结构化 `completion/receipt` 服务端事件，字段版本独立于展示文案。现有 `run/completed` 保留为生命周期信号，并引用 `receipt_id`。客户端收到 completed 但未收到收据时请求补发，不自行拼接不完整收据。

## 8. 测试与验收

覆盖完全成功、部分成功、测试失败、用户取消、无文件改动、Git 未安装、未提交改动、审批拒绝和断线期间完成。验收要求用户仅阅读收据就能回答：改了什么、验证了什么、什么没验证、是否需要我批准、下一步是什么。

## 9. 当前实现状态（0.1.212）

- 后端：`naumi_agent.runs` 提供冻结且有界的 `CompletionReceipt`、真实 Git 探测、事件证据构建、SQLite 运行记录与精确回执查询。
- 生命周期：引擎正常结束、失败、预算/轮次终止和取消都会在控制权返回 UI 前写入回执；API、JSONL Bridge 与 TUI 共用同一个 `chat-runs.db`。
- 协议：`completion/receipt` 先于 `run/completed`；后者只携带 `receipt_id/run_id`。新 UI 若发现回执缺失，会发送 `receipt/request`，Bridge 按会话边界从 SQLite 补发。
- 新 Terminal UI：中文卡片展示结果、摘要、Git、文件、验证、审批、风险、未验证项和下一步；同一 `receipt_id` 去重，历史恢复可重放。
- Textual TUI：消费同一类型化 `CompletionReceiptMessage`，使用与新 UI 等价的证据分组，不在本地重新计算事实。
- 真实验收：`tests/e2e/test_terminal_completion_receipt.py` 使用临时真实 Git 仓库、真实通过/失败 pytest 子进程、SQLite 重开、真实 `JsonlEngineBridge`、Node 协议/状态/渲染和 Textual 格式化验证完整链路。

## 10. 已知边界

- Git 归因使用运行前后状态与文件指纹的净差异。若另一个进程在同一运行窗口修改同一路径，系统能证明文件发生了变化，但无法仅凭 Git 判断具体修改者；回执不会声称修改一定由某个 Agent 独占完成。
- 超过 500 个状态路径、单文件超过 8 MiB、Git 命令超时或不可读文件会产生明确 `unverified`，不会静默丢弃或伪造完整证据。
- 验证统计目前解析 pytest、TAP/Node 和 Swift 的常见输出；其他命令仍以真实退出码判定通过/失败，可能没有细分的通过/失败用例数。
