# 01 默认入口与运行壳

## 1. 目标

让 `naumi` 成为可靠的产品入口：自动完成环境检查，启动 Node Terminal UI 与 Python Bridge，正确传播配置和工作目录，并在正常退出、Bridge 崩溃或终端异常时恢复终端状态。

## 2. 当前证据

- `src/naumi_agent/main.py` 已有 `chat` 和 `ui` 命令，新 UI 仍需显式调用。
- `frontend/terminal-ui/src/index.js` 会进入 alternate screen、隐藏光标并生成 Bridge 子进程。
- 默认 Bridge 依赖 `uv run python -m naumi_agent.ui.bridge`，已支持外部 bridge command 覆盖。
- 新 Terminal UI 已具备渲染层专用启动欢迎页：进程启动立即显示响应式 NAUMI，Bridge ready 后展示权威版本、工作区、模型与权限模式；首条 chat/task 提交后收起，不写入消息、会话或 UI snapshot。
- Bridge 的预算对象保留可空上限；默认状态显示“预算: 不限 · 已用 $…”，只有用户显式配置数值时才显示有限上限。`bypass` 只改变工具权限，不改变预算执行。
- 仍未完成的是启动前依赖诊断、ready 超时恢复选择、安装态资源解析和旧入口迁移；这些不由欢迎页伪装为完成。

## 3. 目标命令面

| 命令 | 行为 |
|---|---|
| `naumi` | 启动新 Terminal UI |
| `naumi --config PATH` | 使用指定配置启动新 UI |
| `naumi chat` | 进入新 UI 并聚焦普通对话 |
| `naumi chat --classic` | 启动旧 Prompt Toolkit CLI |
| `naumi ui --legacy` | 启动旧 Textual TUI |
| `naumi doctor` | 非交互输出启动依赖和配置诊断 |

旧脚本依赖的非交互子命令保持原语义；仅无子命令入口发生切换。

## 4. 启动阶段状态机

`booting -> checking -> spawning_bridge -> handshaking -> ready`

失败终态包括：

- `dependency_missing`：Node、前端包或 Python 运行时不可用。
- `configuration_invalid`：配置无法解析或必要 provider 缺失。
- `bridge_failed`：子进程启动后在 ready 前退出。
- `protocol_incompatible`：前后端协议版本不匹配。

每个失败必须提供原因、诊断命令和可用回退入口，不能只打印堆栈。

## 5. 进程与信号规则

1. Terminal UI 是前台父进程，Bridge 是唯一受管子进程。
2. `SIGINT` 第一次用于取消当前运行；无运行时才退出。连续第二次强制退出。
3. `SIGTERM` 发送 `shutdown`，等待短暂宽限期后终止 Bridge。
4. Node 异常、未捕获 Promise、stdout 写入失败均必须执行终端恢复函数。
5. Bridge 退出码透传为产品化退出码，错误文本进入本地 debug log。
6. 不遗留孤儿 Bridge、raw mode 或隐藏光标。

## 6. 运行时路径解析

前端资源按以下优先级解析：开发仓库、已安装包内资源、显式环境变量。解析结果必须校验 `package.json`、入口脚本和协议契约同时存在，禁止“找到目录即视为可用”。

工作目录始终使用用户启动 `naumi` 时的目录；配置路径转换为绝对路径后传给 Bridge。UI 的当前项目身份由规范化工作目录和仓库根共同确定。

## 7. 用户体验

- 正常启动不显示依赖探测噪声，握手超过 300 ms 才展示阶段提示。
- 超过 3 秒显示“正在启动本地运行时”，并给出 debug log 路径。
- 失败页提供：重试、运行诊断、经典模式、退出四个明确动作。
- 中文默认；英文文案必须通过 locale key 获取，不在渲染层硬编码两套逻辑。

## 8. 测试

- Python：Typer 命令路由、资源解析、参数透传、退出码。
- Node：信号处理、Bridge 早退、ready 超时、终端恢复幂等。
- 契约：版本不兼容、非法 ready、重复 ready。
- 真实场景：从源码和 wheel 安装环境分别执行 `naumi`，完成一次对话后退出，确认无残留进程。

## 9. 验收标准

1. `naumi` 在有效环境中一次进入新 UI。
2. 缺 Node、缺配置、Bridge 崩溃均有可行动错误页。
3. 任意退出路径都恢复光标、颜色和输入模式。
4. `naumi chat --classic` 与 `naumi ui --legacy` 可明确回退。
5. 入口改造不改变其他非交互 CLI 子命令。
6. 默认不限预算和显式有限预算在 Footer、Runtime Inspector 与兼容 UI 中含义一致，不出现 `$0.00` 伪上限。
