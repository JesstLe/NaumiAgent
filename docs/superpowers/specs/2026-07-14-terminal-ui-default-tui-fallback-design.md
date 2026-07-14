# 新 Terminal UI 默认入口与 Textual 自动回退设计

## 文档状态

- 日期：2026-07-14
- 状态：已批准，待实施
- 范围：交互式命令入口、Terminal UI 启动失败回退、macOS/Linux/Windows 安装提示
- 非目标：删除 Typer 管理命令、删除旧 Prompt Toolkit CLI 代码、重写 Textual、富文本渲染、加载动画、全量文档治理

## 1. 目标

NaumiAgent 只保留两个用户可见的交互前端：

1. 新一代 Node Terminal UI 是所有默认交互入口；
2. Textual TUI 是唯一 fallback，既可显式启动，也可在新 UI 无法启动时自动接管。

Prompt Toolkit 全屏对话前端退出公共产品面，不再通过 `--classic` 启动。Typer 命令控制层、
onboarding、斜杠命令路由与命令元数据仍是共享后端，不属于“旧版 CLI”退役范围。

## 2. 当前事实

- `naumi`、`naumi chat` 与 `naumi ui` 已默认调用 Node Terminal UI；
- Node 不存在、版本低于 20 或打包资源缺失时，当前行为是退出并提示用户手动执行
  `naumi chat --classic` 或 `naumi ui --legacy`；
- `naumiagent --tui` 当前名称与行为相反：它实际启动新 Node UI；
- macOS/Linux 安装脚本把 Node 20+ 当作整个产品的硬依赖，导致本可运行的 Textual 也无法使用；
- Windows 脚本同样把 Node 与 `naumiagent --tui` 作为硬编码兼容入口；
- `src/naumi_agent/cli/` 中混有 Prompt Toolkit 前端和仍被新 UI/TUI 使用的共享模块，不能按目录
  整体删除。

## 3. 方案比较

### 方案 A：公共入口收口，Textual 自动回退（采用）

删除 `--classic` 路由，新增 `naumi tui`，统一默认入口，启动失败自动回退。完整保留 Prompt
Toolkit 实现文件，但它们不再是用户可达产品入口。该方案改动边界清晰，
可以独立验证，并为后续目录治理提供真实的死代码依据。

### 方案 B：保留 `--classic`，只显示弃用警告

兼容性最强，但会继续维持三个交互前端，测试和文档也必须长期同步，不满足“废弃旧版 CLI”。

### 方案 C：立即删除整个 `cli/` 与 Prompt Toolkit 依赖

表面最彻底，但当前 Textual 仍复用 slash router、completion metadata、onboarding 和部分宽度/
主题逻辑。一次删除会把入口迁移、模块重构和依赖替换混成大改动，不符合单功能交付原则。

## 4. 公共命令契约

实施后的交互入口：

| 命令 | 行为 |
|---|---|
| `naumi` | 启动新 Terminal UI；启动失败自动进入 Textual |
| `naumi chat` | `naumi` 的兼容别名，行为相同 |
| `naumi ui` | `naumi` 的兼容别名，行为相同 |
| `naumi tui` | 显式启动 Textual，不尝试 Node UI |
| `naumi --tui` | 一期迁移兼容入口，直接启动 Textual |
| `naumi chat --tui` | 一期迁移兼容入口，直接启动 Textual |
| `naumi ui --legacy` | 一期弃用别名，直接启动 Textual 并提示改用 `naumi tui` |
| `naumiagent` | Windows/旧安装兼容别名，默认启动新 UI 并具备自动回退 |
| `naumiagent --tui` | 显式启动 Textual，名称与行为一致 |

`naumi --classic` 与 `naumi chat --classic` 不再注册，Typer 返回标准“未知选项”错误。批处理命令
`run`、`serve`、`configure`、`doctor`、`workbench` 等不改变，也不会因为交互 UI 失败而进入
Textual。

一期保留 `chat`、`ui` 和 `naumiagent` 是为了命令迁移，不代表保留旧 Prompt Toolkit 前端。
这些别名不拥有独立启动实现，全部调用同一个共享启动策略。

## 5. 启动与回退状态机

新增单一启动协调函数：

```text
_launch_interactive_ui(
    config_path: str,
) -> int
```

行为：

1. 构建 Node UI 命令，校验入口资源与 Node 20+；
2. 以用户当前目录启动 Node UI，Python bridge 继续使用结构化 argv；
3. Node UI 返回 `0` 时正常结束；
4. 返回 `130` 或 `143` 时视为用户中断/进程终止，透传退出码，不启动 Textual；
5. `TerminalUiLaunchError`、`OSError` 或其他非零退出码视为启动/运行失败；
6. 在 stderr/console 输出一条中文原因和“正在切换到 Textual TUI”，随后调用 `_launch_tui()`；
7. Textual 成功退出后，整个命令返回 `0`；Textual 自身也失败时，显示两个阶段的安全错误摘要并
   返回 `1`，不递归重试。

显式 `naumi tui` 不经过该协调函数，避免 Node 检测和回退环。错误文案不得包含环境变量、密钥、
完整子进程环境或原始 traceback；调试日志仍可记录异常类型与退出码。

## 6. Prompt Toolkit 退役边界

本切片删除或停止注册：

- root callback 的 `--classic`；
- `chat --classic`；
- 所有调用 `_chat()` 的公共路径；
- README、安装脚本和启动错误中的 classic 推荐。

明确保留：

- `cli/slash_router.py`、`cli/commands_meta.py`、`cli/onboarding.py` 等共享模块；
- 当前仍被 Textual 或共享主题/宽度逻辑引用的 Prompt Toolkit 辅助模块；
- `_chat()`、Prompt Toolkit 布局、渲染、历史、补全及其测试代码；
- 旧 CLI 代码当前需要的依赖。

后续“目录与文档治理”切片可以把共享模块迁移到中性命名空间，并把旧 CLI 明确标记或隔离为
legacy 实现，但不得删除其源码、测试和必要依赖，除非用户以后明确改变决定。当前切片的成功
标准是旧交互入口不可达，不是删除历史实现。

## 7. 安装与跨平台行为

### macOS/Linux

`scripts/install.sh` 继续检测 Node：

- Node 20+：安装 Terminal UI 依赖，`naumi` 使用新 UI；
- Node 缺失、版本过旧或 npm 缺失：显示黄色警告，不中止 Python/Textual 安装；
- 安装结束只展示 `naumi` 和 `naumi tui`，说明默认 UI 会自动回退。

### Windows

`scripts/windows/setup.ps1`：

- Python、uv 与 Textual 所需环境仍是硬要求；
- Node 20+ 可用时启用新 UI；不可用时保留安装并明确 Textual fallback；
- `naumiagent` 默认新 UI，`naumiagent --tui` 才显式启动 Textual；
- 不把 WSL `bash.exe` 当作 Git Bash，现有防护不变。

本切片不声称 Windows 原生 Terminal UI 全部功能已经验收，只保证命令入口与降级策略一致。
完整 macOS/Linux/Windows 运行矩阵属于后续跨平台适配切片。

## 8. 测试与真实验证

只运行小模块，不跑全量测试。

Python 定向测试覆盖：

- `naumi`、`chat`、`ui`、`naumiagent` 进入同一个默认启动器；
- `naumi tui`、`--tui` 与弃用 `ui --legacy` 进入 Textual；
- `--classic` 已不可用；
- Node 缺失、旧版本、资源缺失、spawn `OSError`、普通非零退出自动回退；
- `0`、`130`、`143` 不回退；
- Textual 回退失败返回 `1` 且不重复启动；
- cwd 与 config 路径保持不变。

脚本静态测试覆盖：

- macOS/Linux 安装脚本不再因 Node 缺失直接退出；
- Windows 脚本命令提示与 fallback 语义一致；
- README 不再推荐 Prompt Toolkit 入口。

真实场景验证使用独立临时目录和当前虚拟环境：

1. 用伪 Node 可执行文件返回 `0`，证明默认入口不触发 Textual；
2. 用伪 Node 返回普通非零码，并用可观察的 Textual stub 证明只回退一次；
3. 使用不存在的 Node 路径证明预检失败仍进入 Textual；
4. 执行 `naumi --help`、`naumi tui --help` 和 `naumiagent --help` 检查中文入口；
5. `ruff check` 仅检查修改文件，`py_compile` 检查启动模块，`git diff --check` 检查补丁。

## 9. 文档边界

本切片只更新会直接误导启动行为的 README、安装脚本提示和入口集成文档。其余历史路线图、产品
课程、旧命令截图和目录结构在后续“全量文档治理”切片中统一分类：当前事实、历史归档、删除。
这样避免在入口功能提交中混入无法独立验证的大规模文档重写。

## 10. 后续顺序

1. 完成本设计的入口迁移并合并主干；
2. 全量文档治理与目录清理；
3. 新 UI 语义富文本、代码 diff、普通文字与数学公式色彩体系；
4. 模型工作中的动态加载图像；
5. Shell 兼容与 macOS/Linux/Windows 运行矩阵。

每项独立设计、测试、提交、推送。

## 11. 自审

- 默认 UI、显式 TUI、自动 fallback 和旧 Prompt Toolkit 退役的边界明确；
- 没有把 Typer 管理命令误判成旧交互 CLI；
- 没有在 Node 缺失时阻断本可运行的 Textual；
- 没有把正常退出或用户中断当作故障回退；
- 没有把共享 `cli/` 目录直接删除造成架构破坏；
- 完整保留旧 Prompt Toolkit CLI 源码、测试和必要依赖；
- 没有把后续富文本、动画、目录清理和完整跨平台验收混入本切片；
- 未发现占位内容或未定义成功条件。
