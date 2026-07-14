# 01 默认入口与运行壳

## 1. 目标

让 `naumi` 成为可靠的产品入口：优先启动 Node Terminal UI 与 Python Bridge，在新 UI 无法使用时
自动切换到 Textual，并在正常退出、Bridge 崩溃或终端异常时恢复终端状态。

## 2. 当前已实现

- `naumi`、`naumi chat`、`naumi ui` 和兼容命令 `naumiagent` 统一进入 Node Terminal UI。
- `naumi tui`、根命令/`chat` 的 `--tui` 选项和带弃用提示的 legacy alias 直接进入 Textual。
- Node 缺失、版本过旧、前端资源缺失、进程启动失败或普通非零退出时，只自动 fallback 一次。
- 正常退出与用户中断退出码 `0`、`130`、`143` 不触发 fallback，避免用户退出后界面重新打开。
- 启动错误在展示前经过去重与敏感信息清理，不打印模型密钥或底层重复堆栈。
- macOS/Linux 安装脚本和 Windows 初始化脚本都把 Node 视为新 UI 的可选依赖；Node 不可用时仍保留
  可运行的 Textual 路径。
- 旧 Prompt Toolkit CLI 实现、测试和必要依赖保留，但不再注册公共启动选项。
- 启动欢迎页由 Bridge ready 后的权威状态填充版本、工作区、模型与权限；首条消息后收起。
- 默认预算显示“不限”，只有显式配置有限预算时才显示上限和百分比。

仍待后续独立切片完成的是更完整的安装态资源矩阵、交互式启动诊断页和 ready 超时恢复动作。

## 3. 当前命令面

| 命令 | 行为 |
|---|---|
| `naumi` | 优先启动新 Terminal UI，失败时自动进入 Textual |
| `naumi --config PATH` | 使用指定配置，保持同样的自动 fallback |
| `naumi chat` | 默认交互入口的兼容别名 |
| `naumi ui` | 显式请求新 Terminal UI，仍保留自动 fallback |
| `naumi tui` | 显式启动 Textual |
| `naumi --tui` / `naumi chat --tui` | Textual 兼容参数 |
| `naumi doctor` | 非交互输出依赖、配置和可选真实模型诊断 |

`naumi ui` 的 legacy alias 仅用于旧脚本迁移，并会提示改用 `naumi tui`。非交互子命令继续保持原语义。

## 4. 启动协调规则

Python Typer 层是唯一启动协调器：

1. 解析配置、工作目录和显式 TUI 选择；
2. 尝试解析并启动 Node UI；
3. 成功或用户中断时直接传播退出码；
4. 启动异常或普通非零退出时输出一次中文原因并进入 Textual；
5. Textual 退出后直接结束，不再次启动 Node，防止 fallback 循环。

Node UI 继续负责其 Bridge 子进程、alternate screen、raw mode 和光标恢复。Python 协调器不复制前端
生命周期逻辑。

## 5. 运行时路径与平台边界

前端资源按开发仓库和已安装 wheel 的受支持位置解析，并校验 `package.json`、入口脚本与协议资源。
工作目录始终使用用户启动命令时的目录；配置路径转为绝对路径后传给 Bridge。

安装器遵循同一降级原则：

- macOS/Linux：Python 3.12+ 为必要依赖，Node.js 20+ 为新 UI 可选依赖；
- Windows：Python/uv 和 Git for Windows Bash 提供运行与 shell 语义，Node.js 20+ 可选；
- Node/npm 检测或依赖安装失败只给出可行动警告，不破坏 Textual 安装。

## 6. 用户体验

- 正常启动不显示依赖探测噪声；发生 fallback 时只显示一次原因和当前动作。
- 错误文本不得包含 API Key、token、Authorization header 或完整凭据 URL。
- fallback 文案统一推荐 `naumi tui`，不引导用户寻找已退役的 Prompt Toolkit 入口。
- 中文为默认用户文案；终端颜色、光标和输入模式在任意退出路径都必须恢复。

## 7. 定向验证

- Python：入口路由、显式 TUI、退出码分类、异常清理、单次 fallback。
- 安装器：Node 缺失/过旧/损坏、npm 安装失败、Windows BOM 和命令检查。
- 真实 PTY：模拟 Node 非零退出，确认 Textual 标题出现且没有重复 warning。
- 文档：当前命令面由 `scripts/check_docs.py` 阻止退役入口重新出现。

## 8. 验收边界

1. 有效 Node 环境一次进入新 UI。
2. Node 不可用或 UI 普通失败时一次进入 Textual。
3. 用户中断不会触发意外重开。
4. 默认和显式 Textual 路径都使用同一真实 TUI 实现。
5. 旧 Prompt Toolkit 源码仍在仓库中，但用户无法通过公共选项启动它。
6. 默认不限预算和显式有限预算在 Footer、Inspector 与兼容 UI 中语义一致。
