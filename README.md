<div align="center">
  <img src="assets/logo.svg" width="128" height="128" alt="NaumiAgent Logo">
  <h1>NaumiAgent</h1>
  <p>能阅读、执行、记忆、协作并自我改进的本地 Agent 系统。</p>
</div>

## 当前状态

`naumi` 默认启动新一代 Node Terminal UI，并以当前目录作为工作区。主界面聚焦对话与执行时间线，工具、权限、任务和运行状态通过结构化卡片持续更新；旧 Prompt Toolkit CLI 与 Textual TUI 仅作为显式兼容入口保留。

核心能力包括：

- **多模型路由**：通过 LiteLLM 统一调用模型，支持 fast/capable/reasoning tier。
- **工具执行**：文件读写、代码执行、shell、Web、浏览器、记忆、任务、调度等工具走统一权限与预算控制。
- **会话与记忆**：SQLite 会话历史、Chroma 长期记忆、上下文压缩、`/resume` 与 `/history` 恢复链路。
- **运行态面板**：`/todo`、`/tasks`、`/runtime` 汇总 todo、subagent、后台任务、浏览器任务和 hook 状态。
- **自我演进**：`/self-review`、`/evolve`、`/forge`、`/pursue` 支持源码审查、自我修改、工具锻造和目标追踪。
- **多界面**：Node Terminal UI、Prompt Toolkit 兼容 CLI、Textual fallback、REST API/WebSocket 和原生 Mac Workbench。

## 快速开始

### 安装

#### 一键安装（推荐）

像 Claude Code 一样，一条命令完成安装：

```bash
curl -sSL https://raw.githubusercontent.com/JesstLe/NaumiAgent/main/scripts/install.sh | bash
```

安装脚本会自动：
- 检测 Python 3.12+
- 检测 Node.js 20+
- 使用 `uv` 或 `pip` 安装依赖
- 安装浏览器自动化与搜索回退所需的 Chromium
- 将 `naumi` 命令链接到 `~/.local/bin`
- 安装 Node UI 依赖

安装完成后直接运行：

```bash
naumi
```

首次启动会进入交互式引导，询问模型 API Key、模型提供商、工作区和权限模式，自动生成不含密钥的 `.naumi/config.yaml`。模型密钥保存在系统凭据库中；已经设置 `NAUMI_MODELS__API_KEY` 的环境不会重复保存。旧项目的根目录 `config.yaml` 仍会被兼容读取，不会被自动复制或删除。

网络搜索默认无需搜索引擎 API Key：系统会依次尝试免 Key 搜索，并在失败时自动回退到浏览器搜索。`BRAVE_SEARCH_API_KEY` 只是可选增强项，用于提升结果质量和稳定性，不会阻塞首次安装或基本搜索。

需要更换 provider、模型或过期密钥时，运行：

```bash
naumi configure
```

自动化环境可使用 `--non-interactive --provider <name>`，并通过环境变量复用现有凭据；需要更新密钥时使用 `--api-key-stdin` 从标准输入传入，避免密钥进入 shell history。

配置完成后可以先运行纯本地诊断；显式增加 `--live` 才会发送一次最多 8 token 的真实模型请求：

```bash
naumi doctor
naumi doctor --live
```

实时诊断会区分 provider/model/API Base 混配、401 凭据失效、404 模型或地址错误、429 限流和连接超时，并且不会显示模型响应正文或服务端原始错误。

#### 本地开发安装

```bash
uv sync --extra dev
# 或
pip install -e ".[dev]"
```

### Windows 初始化

Windows 原生开发使用 Python/uv/Node.js，并通过 Git for Windows Bash 保持 Agent 的 Bash 命令语义。先用隐藏输入保存 Kimi 密钥到当前 Windows 用户环境：

```powershell
$kimiKey = Read-Host "Kimi API Key" -MaskInput
[Environment]::SetEnvironmentVariable("NAUMI_MODELS__API_KEY", $kimiKey, "User")
Remove-Variable kimiKey
```

重新打开 PowerShell，然后运行幂等初始化脚本：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/windows/setup.ps1
```

初始化完成后，可在 PowerShell 中直接启动新版终端 UI：

```powershell
naumi
```

`naumiagent --tui` 作为 Windows 早期版本的兼容别名继续可用。脚本会检查 Python 3.12+、uv、Node.js 20+ 与 Git Bash，创建 `.venv` 和无密钥的本地 `.naumi/config.yaml`，并验证配置。若 Git Bash 不在标准 Git for Windows 目录，可设置 `NAUMI_GIT_BASH` 指向 `bin\bash.exe`。脚本不会覆盖已有的现代配置；若发现旧根目录 `config.yaml`，会继续使用旧配置而不生成竞争副本。

### 配置

如果你选择跳过引导，可以手动配置：

```bash
mkdir -p .naumi
cp config.yaml.example .naumi/config.yaml
export NAUMI_MODELS__API_KEY=your-key
```

默认模型配置面向 Kimi Coding API：

```yaml
models:
  provider: "kimi"
  default_model: "openai/kimi-for-coding"
  fast_model: "openai/kimi-for-coding"
  reasoning_model: "openai/kimi-for-coding"
  temperature: 1.0
  api_base: "https://api.kimi.com/coding/v1"
```

`workspace_root: "."` 表示文件工具和 shell 默认作用于启动 `naumi` 时的当前目录。

### 启动

```bash
# 推荐：直接启动新一代终端 UI
naumi

# 等价的对话入口
naumi chat

# 等价的源码启动方式
python -m naumi_agent.main

# 显式启动新一代 Node 终端 UI
naumi ui

# 旧版 Prompt Toolkit CLI
naumi chat --classic

# 旧版 Textual TUI
naumi ui --legacy

# 单任务执行
naumi run "检查这个项目的测试风险"

# REST API 服务
naumi serve
```

`naumi`、`naumi chat` 与 `naumi ui` 需要 Node.js 20+；如果本机没有 Node 或版本过旧，可使用 `naumi chat --classic` 或 `naumi ui --legacy`。

如果需要查看 LiteLLM 可选 provider 的启动 warning，可显式打开：

```bash
NAUMI_SHOW_STARTUP_WARNINGS=1 naumi chat
```

## 常用斜杠命令

| 类别 | 命令 | 用途 |
| --- | --- | --- |
| 基础 | `/help` `/keybindings` `/style` `/doctor` | 查看帮助、快捷键、主题与运行环境诊断 |
| 文件 | `/glob` `/grep` `/read` `/write` `/edit` | 通过 Agent 工具路径搜索、读取和修改文件 |
| 会话 | `/history` `/resume` `/load <id>` `/new` `/clear` | 查看、恢复、加载、保存新开或清空当前会话 |
| 调试 | `/copy <all|last|error>` `/debug` `/debug-replay` `/diff` | 导出 transcript、查看结构化调试日志与 git diff |
| 任务 | `/todo` `/tasks` `/task` `/task-reply` `/task-abort` | 管理 todo、subagent、后台/browser 任务和人工接管 |
| 运行态 | `/runtime [分区]` `/team` `/background` `/schedule` | 查看运行态、团队协议、后台任务和调度提醒 |
| 浏览器 | `/browse` `/autobrowse` `/browser-state` `/bdaemon` | 浏览器操作、本地浏览器 daemon 和 SoM 调试 |
| 分析 | `/chaos` `/scale` `/state` `/graph` `/self-review` | 架构、扩展性、状态、图谱和源码自审查 |
| 自进化 | `/evolve <描述>` `/evolve-history` `/forge` `/pursue` | 自我修改、进化历史、工具锻造和目标追踪 |

命令补全来自 `src/naumi_agent/cli/completer.py`。输入 `/` 可查看全部命令，输入关键词可模糊匹配，例如 `hs` 可匹配 `/history`。`/histroy` 也会被容错映射到 `/history`。

## 架构

```text
src/naumi_agent/
├── orchestrator/     # ReAct 引擎、Planner、运行模式、subagent 调度
├── model/            # LiteLLM 模型路由、流式响应、工具调用历史修复
├── tools/            # 文件、浏览器、代码沙箱、网络、记忆、自进化等工具
├── tasks/            # todo/task 工具与 SQLite 存储
├── agents/           # 子 Agent、消息总线、团队协议
├── safety/           # 权限、预算、guardrails
├── memory/           # 会话持久化、长期记忆、上下文压缩
├── streaming/        # 事件总线
├── cli/              # prompt_toolkit 全屏 CLI、命令补全、渲染器
├── tui/              # Textual TUI fallback
├── ui/               # Node terminal UI bridge、协议、共享渲染组件
├── api/              # FastAPI REST + WebSocket
└── config/           # pydantic-settings + YAML 配置
```

## 开发

```bash
# Lint
uv run ruff check src tests

# 格式化
uv run ruff format src tests

# 测试
uv run pytest tests -q

# 类型检查
uv run mypy src/naumi_agent --ignore-missing-imports
```

日常改动建议优先跑与修改路径相关的 targeted tests。全量测试会覆盖更多外部集成和浏览器路径，耗时更长。

## Docker

```bash
cp .env.example .env
# 编辑 .env，填入 NAUMI_MODELS__API_KEY
mkdir -p workspace
docker compose up --build
```

启动后访问 `http://127.0.0.1:8080/docs`。完整部署说明见 [docs/deployment.md](docs/deployment.md)。

## 文档

- [架构概览](docs/01-architecture-overview.md)
- [工具系统](docs/03-tool-system.md)
- [记忆系统](docs/04-memory-system.md)
- [多 Agent 设计](docs/06-multi-agent.md)
- [安全与护栏](docs/07-safety-guardrails.md)
- [终端 UI 集成](docs/terminal-ui-integration.md)
- [CLI/TUI 路线图](docs/13-cli-tui-claude-code-roadmap.md)

## License

MIT
