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

1. Bridge 在 `run/completed` 前收集结构化证据并生成收据。
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
