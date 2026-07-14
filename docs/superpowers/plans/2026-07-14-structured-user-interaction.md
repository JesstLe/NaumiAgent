# Agent 结构化用户交互实施计划

> 对应设计：`docs/superpowers/specs/2026-07-14-structured-user-interaction-design.md`

## 任务 1：后端领域模型与工具

1. 新建 `tests/unit/test_user_interaction.py`，先覆盖校验、handler 缺失和合法 option/custom。
2. 新建 `src/naumi_agent/user_interaction.py` 与 `tools/user_interaction.py`。
3. 给 Engine 增加 callback API 并注册工具。
4. 跑该单测文件至通过。

## 任务 2：Bridge 协议与 Future

1. 在协议枚举加入 request/response/resolved。
2. 给 Bridge 增加 pending interaction、公开 payload、解析、错误码与 shutdown 清理。
3. 在 `tests/unit/test_ui_bridge.py` 增加请求/响应/非法/关闭测试。
4. 只运行新增 Bridge 测试。

## 任务 3：新 Terminal UI 状态、键盘与渲染

1. 在 Node state 测试中固定 FIFO、选择、custom 和 resolved 行为。
2. 实现 interaction state、queue 与按键处理。
3. 新增 interaction card/footer，并接入 message/render。
4. 增加组件及真实进程协议测试。
5. 只运行对应 Node 测试名称。

## 任务 4：Textual TUI

1. 增加 Modal 结果测试。
2. 实现 `UserInteractionScreen` 与串行 callback。
3. 注册 Engine handler。
4. 只运行对应 Textual 测试。

## 任务 5：验证与提交

1. 重跑本功能 Python/Node 定向测试。
2. Ruff、py_compile、node --check、git diff --check。
3. 审查并发、shutdown、控制字符和重复响应。
4. 以英文独立提交。
