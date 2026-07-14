# 07 CLI 兼容与迁移

## 1. 目标

让 Node Terminal UI 成为唯一默认交互产品面，同时把 Textual 作为真实、受支持的自动 fallback。
旧 Prompt Toolkit CLI 代码继续用于兼容研究和共享模块依赖，但不再形成第四个用户入口。

## 2. 当前表面分级

| 表面 | 当前定位 | 新功能策略 |
|---|---|---|
| Node Terminal UI | 默认产品界面 | 承载全部新交互能力 |
| Textual TUI | 显式与自动 fallback | 同步关键协议、状态与严重缺陷修复 |
| Prompt Toolkit CLI | 保留源码、无公共入口 | 保留实现/测试/依赖，不继续扩展产品能力 |
| 非交互 CLI | 自动化接口 | 保持退出码与机器可读输出稳定 |
| REST API / WebSocket | 外部集成接口 | 与 Agent Engine 和统一事件协议对齐 |

## 3. 已完成的入口收口

- 默认入口、`chat`、`ui` 和 `naumiagent` 已统一走 Node UI 启动协调器。
- Node 启动失败或普通非零退出自动切换一次 Textual。
- `naumi tui` 是显式 fallback；旧的 legacy alias 仍可迁移脚本，但会打印弃用提示。
- Prompt Toolkit 的公开启动选项已经移除；其 `_chat()`、布局、渲染、历史、补全、测试和必要依赖
  均保留，没有物理删除。
- README、安装器和 onboarding 不再把 Prompt Toolkit 作为无 Node 环境的解决方案。

这里的“废弃旧版 CLI”只表示退出公共产品入口，不授权删除旧代码。未来若要物理删除，必须重新审计
共享依赖、会话兼容和测试价值，并形成单独设计。

## 4. 兼容约束

- Node UI 与 Textual 共用 Agent Engine、SQLite 会话、权限、预算和 Bridge 语义，不复制执行链。
- Textual 遇到新消息类型时显示安全文本摘要，不因未知扩展字段崩溃。
- 新 Bridge 在协议版本范围内兼容受支持客户端；超出范围明确拒绝。
- 非交互命令的 stdout 保持机器可读，诊断与 fallback 信息进入 stderr。
- 配置和环境变量只有一个解析实现，各入口只负责参数传递。
- `bypass` 在所有界面都表示全权限通过，不增加高风险二次确认；其他模式继续按统一权限策略运行。

## 5. 帮助与文案

`naumi --help` 首先说明默认进入新 Terminal UI，并把 `naumi tui` 列为受支持 fallback。迁移 alias
只在对应命令帮助或实际调用时出现，避免主路径充斥内部前端历史。

fallback 必须说明“为什么切换、正在进入什么界面、如何下次显式选择”，且不得重复打印底层异常。

## 6. 回滚策略

严重 Node UI 回归时，自动 fallback 继续保证可用性；发布修复应优先恢复新 UI，而不是重新开放
Prompt Toolkit。若启动协调器本身故障，用户仍可显式运行 `naumi tui`。

回滚不得改变会话格式、`.naumi/` 配置布局或权限含义，也不得把旧 CLI 代码保留误写成可公开运行。

## 7. 测试与验收

命令矩阵覆盖：无参数、配置路径、显式 TUI、迁移 alias、非 TTY、Node 缺失/损坏、Node 普通失败、
用户中断、wheel 安装和源码运行。使用同一 SQLite 会话从 Node UI 与 Textual 打开，确认消息可读且
不会重复执行。

验收要求 README、帮助、安装脚本和运行行为一致；用户只需要理解“默认 UI”和“Textual fallback”，
无需了解 Prompt Toolkit 的内部保留代码。
