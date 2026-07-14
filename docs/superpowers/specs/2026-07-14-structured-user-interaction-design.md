# Agent 结构化用户交互设计

日期：2026-07-14

## 1. 目标

允许模型在缺少关键决策、多个方案存在实质取舍或需要用户提供自定义信息时，主动暂停当前运行并发起结构化询问。新 Terminal UI 与 Textual TUI 同时支持：

- 2–3 个互斥选项；
- 每个选项的短标签与影响说明；
- `↑/↓`、数字和 Enter 选择；
- 可选“其他”入口，输入自定义内容；
- 回答后原运行从工具结果继续，而不是开启一条无关联的新消息。

## 2. 方案比较

### 方案 A：把问题写进 assistant 文本

无法可靠区分普通回复和等待输入，不能让正在执行的工具调用继续，也无法提供键盘选择和校验。不采用。

### 方案 B：复用 permission/request

实现快，但会把产品决策错误地记录成安全授权，`allow/deny/bypass` 语义也无法表达方案选项。权限与业务交互必须隔离，不采用。

### 方案 C：独立工具、回调与协议

新增 `request_user_input` 工具，Engine 通过独立 callback 请求宿主；Bridge 使用 Future 暂停工具并发送 `interaction/request`，前端回传 `interaction_response`，最终以 `interaction/resolved` 更新 UI。Textual 直接通过同一 Engine callback 展示 Modal。采用此方案。

## 3. 后端工具

工具名：`request_user_input`。

输入：

```json
{
  "header": "实现策略",
  "question": "你希望采用哪种持久化范围？",
  "options": [
    {"value": "workspace", "label": "工作区", "description": "同一仓库共享目标"},
    {"value": "session", "label": "当前会话", "description": "仅本次会话有效"}
  ],
  "allow_custom": true,
  "custom_label": "其他方案"
}
```

约束：

- question 1–2000 字符；header 1–40；
- options 必须 2–3 项，value 唯一且 1–80 字符，label 1–80，description 最多 300；
- 自定义输入最多 4000 字符；
- 清除 C0/C1 控制字符，保留换行仅用于 question/description/custom；
- 工具只在确实需要用户决策时调用，不能把可安全推断的小问题都推给用户；
- 没有交互宿主时返回中文可操作错误，不永久挂起。

工具结果使用 JSON，明确 `kind=option|custom`、value、label 和 custom_text，供模型无歧义继续执行。

## 4. Engine 与宿主

Engine 新增独立 `set_user_interaction_handler()` 和 `request_user_input()`，不依赖权限层。Bridge 与 Textual 启动时注册 handler；关闭时清空所有 pending Future：

- Bridge 关闭：以取消结果解除等待，然后取消活动 run；
- Textual 关闭：Modal 随 App 卸载，运行取消；
- 没有 handler：立即抛出 `UserInteractionUnavailable`，工具转为中文错误结果。

多个并行工具可能同时询问。Bridge 后端允许多个 pending request；Terminal UI 用 FIFO 队列一次展示一个，其他卡片显示“排队等待”。每个 request_id 独立解析，不能串答。

## 5. JSONL 协议

新增：

- Client `interaction_response`
- Server `interaction/request`
- Server `interaction/resolved`

request payload 只含公开字段：request_id、session_id、run_id、agent_name、header、question、options、allow_custom、custom_label、status。

response：

- 选项：`{request_id, kind:"option", value:"workspace"}`
- 自定义：`{request_id, kind:"custom", custom_text:"..."}`

Bridge 必须重新校验 value、allow_custom、文本长度和 pending 状态。未知/重复/越权响应返回稳定错误码，不解除其他请求。

## 6. 新 Terminal UI

时间线插入持久卡片，活动时黄色，回答后绿色：

```text
+ 需要你的选择 --------------------------------------------------+
| 实现策略                                                        |
| 你希望采用哪种持久化范围？                                      |
| › 1. 工作区 · 同一仓库共享目标                                  |
|   2. 当前会话 · 仅本次会话有效                                  |
|   3. 其他方案                                                    |
+-----------------------------------------------------------------+
```

活动选择器位于 footer，优先级低于安全权限、高于 Agent 页和 composer：

- `↑/↓` 或数字移动；Enter 确认；
- 选择“其他”后进入独立输入缓冲，支持左右、Home/End、Backspace、Delete；
- Enter 提交非空自定义文本，Esc 返回选项；
- Ctrl+C 仍取消当前 run；
- 发送后进入 `submitting`，防止重复响应；
- 卡片和 footer 均经过宽度、ANSI 与 CJK 安全渲染。

交互态不进入 UI snapshot；显式 resume 只恢复后端仍存在的请求，避免重启显示已失效 Modal。

## 7. Textual TUI

新增 `UserInteractionScreen`：问题、选项按钮、自定义 Input。方向键在按钮间移动，Enter 确认；选择“其他”后聚焦 Input。结果与 Bridge 使用同一 response 字典。

Modal 通过 `asyncio.Lock` 串行展示并发请求，避免多个 screen 叠加；关闭 App 时不伪造用户选择。

## 8. 测试

只运行相关小模块：

- 工具：schema、2/3 项、重复 value、长度、handler 缺失、option/custom 返回；
- Engine：callback 注入与工具注册；
- Bridge：请求挂起、合法响应、非法 value、自定义禁用、未知 ID、shutdown；
- Node state：FIFO、键盘移动、数字、custom、重复提交保护、resolved；
- Node render：窄/宽/CJK 和不同状态颜色；
- Textual：Modal 选项与自定义结果的组件测试；
- protocol、Ruff、py_compile、node --check、git diff --check。

## 9. 自我审视

- 交互不能替代 Agent 自主判断。工具描述明确“只有决定会实质改变结果且无法从上下文推断时使用”。
- 必选问题不提供隐式 Esc 取消；用户可通过 Ctrl+C 明确取消整个 run，避免模型收到伪造默认答案。
- 并发请求必须按 request_id 隔离并在前端排队，否则子智能体会互相串答。
- 自定义文本是用户输入，后续只能作为不可信数据返回模型，不能被 Bridge 当命令执行。
- 本轮不持久化跨进程 pending interaction；Bridge 进程退出时原运行也不存在，恢复一个失效 Future 会制造假状态。
