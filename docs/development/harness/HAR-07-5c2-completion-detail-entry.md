# HAR-07.5c2 完成回执直接进入 Harness Detail

## 目标

让用户看到具备权威 Harness 同源回执的完成卡后，不必手工复制 run id：New UI 与 Textual TUI
都可按 `Ctrl+O` 直接打开最近一张可见、可查询的 Harness 详情。现有
`/harness detail <run-id>` 保留为精确、可复制的后备入口；本切片不新增第二套详情查询或页面。

## 用户语义

- 同一 `run_id` 的通用 Completion Receipt 与 Harness Receipt 已配对时，卡片显示：
  `Ctrl+O 查看详情 · /harness detail <run-id>`。
- 只有通用回执时，卡片不显示详情入口；按 `Ctrl+O` 不发送后端请求，并明确提示当前没有可查看的
  Harness 完成回执。
- 存在更新的普通回执但没有 Harness 同伴时，New UI 选择时间线中最近一张仍具备 Harness 同伴的卡；
  Textual TUI 同样保留最近可见的有效目标，不让后续普通运行使旧卡入口失效。
- Textual TUI 的快捷键来自共享 `keybindings.open_latest_harness_detail`，默认 `Ctrl+O`；用户覆盖后
  卡片显示实际按键。New UI 当前使用固定 `Ctrl+O`。

## 权威与路由

### New UI

1. `completion_receipt` 消息必须持有同 `run_id`、revision 至少为 1 的类型化 `harness/receipt`。
2. 快捷动作从时间线尾部反向寻找最近的有效配对，不把普通回执猜成 Harness 运行。
3. 找到后调用既有 `openHarnessDetailRoute()`，并发发送精确 run id 的
   `harness/explain/request` 与 `harness/replay/request`。
4. 页面、revision cache、滚动锚点、`e/r/v` 与 Esc 返回语义全部沿用 HAR-07.3/5a/5b。

### Textual TUI

1. Engine sink 继续按精确 run id 合并先到的 Harness Receipt 与通用回执。
2. TUI 只保存最近可见的有效详情目标，不复制 Harness 详情事实，也不从卡片文本解析 run id。
3. `action_open_latest_harness_detail()` 调用既有共享
   `/harness detail <run-id>`；Service 仍负责工作区隔离、Explain 与 Replay。
4. 加载另一会话或清空会话会清除瞬态目标；开始新运行不会删除仍在屏幕上的上一张有效卡目标。

## 交互优先级与安全边界

- 权限确认、用户 Interaction、QuickOpen 和 Textual Modal 优先于详情快捷键；`Ctrl+O` 不得绕过或遮蔽
  待裁决交互。
- run id 只进入既有严格路由；卡片命令经过终端控制字符清理和 shell 参数引用。
- 详情动作只读，不执行模型、工具、Harness check、原任务或 Git 操作。
- 没有有效配对、run id 不一致、revision 无效或状态不受支持时 fail closed，不发请求。
- 详情路由是瞬态 UI 状态，不写入 session snapshot，也不在新进程启动时自动恢复。

## 验收证据

- Node reducer/action 覆盖最近有效配对、更新的普通回执、配对不一致和无目标零流量。
- New UI 卡片覆盖精确命令、无 Harness 兼容行为、ANSI 关闭及 80/120/200 列中文宽度边界。
- 真实 New UI 进程从 Harness/Completion 两类协议事件渲染卡片，实际输入 `Ctrl+O` 后只发送同 run 的
  Explain/Replay 请求；权限面板打开时相同按键保持本地且不发请求。
- Textual 真实 app 通过 Engine sink 合并一张卡，显示当前配置快捷键，并把动作路由到精确共享命令；
  无配对时只产生中文提示。
- 真实 SQLite Harness Store → Python Bridge → Node normalizer/reducer/card/action 链路证明动作使用已
  持久化运行身份，并保持 80/120/200 列有界。
- 只运行相关 Python/Node 模块、真实小场景、Ruff、compile、syntax 与 diff check，不运行全量测试。

## 自我审视与剩余边界

- 终端卡片没有鼠标焦点模型，因此“直接进入”采用全局快捷键和精确命令，不宣称实现鼠标点击。
- New UI 快捷键尚未接入 Python `config.yaml` 的共享按键配置；Textual TUI 已支持覆盖。若后续统一
  terminal frontend 配置协议，应让 Node 读取同一动作映射，而不是继续增加硬编码按键。
- 本切片不完成 HAR-07.4b 的客户端 ACK、cursor resend、活动运行恢复或 revision/gap 自动补发。
- 详情页的跨平台真实 PTY、IME 与终端组合键矩阵仍属于 UI-16 发布验证。
