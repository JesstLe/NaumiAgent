<div align="center">
  <img src="assets/logo.svg" width="128" height="128" alt="NaumiAgent Logo">
  <h1>NaumiAgent</h1>
  <p>能阅读、执行、记忆、协作并自我改进的本地 Agent 系统。</p>
</div>

## 当前状态

NaumiAgent 现在的主入口是全屏 CLI：`naumi chat`。它会以当前目录作为工作区启动，保留启动大 Logo 作为会话开场，流式输出按 5 FPS 节流刷新，并把模型、预算、工作区、todo 与工具活动固定在底部状态区。

核心能力包括：

- **多模型路由**：通过 LiteLLM 统一调用模型，支持 fast/capable/reasoning tier。
- **工具执行**：文件读写、代码执行、shell、Web、浏览器、记忆、任务、调度等工具走统一权限与预算控制。
- **会话与记忆**：SQLite 会话历史、Chroma 长期记忆、上下文压缩、`/resume` 与 `/history` 恢复链路。
- **运行态面板**：`/todo`、`/tasks`、`/runtime` 汇总 todo、subagent、后台任务、浏览器任务和 hook 状态。
- **自我演进**：`/self-review`、`/evolve`、`/forge`、`/pursue` 支持源码审查、自我修改、工具锻造和目标追踪。
- **多界面**：稳定全屏 CLI、Node 终端 UI、Textual TUI fallback、REST API/WebSocket。

## 快速开始

### 安装

推荐使用 `uv` 管理本地开发环境：

```bash
uv sync --extra dev
```

也可以使用 editable install：

```bash
pip install -e ".[dev]"
```

### 配置

```bash
cp config.yaml.example config.yaml
export NAUMI_MODELS__API_KEY=your-key
```

默认模型配置面向 Kimi Coding API：

```yaml
models:
  default_model: "openai/kimi-for-coding"
  fast_model: "openai/kimi-for-coding"
  reasoning_model: "openai/kimi-for-coding"
  api_base: "https://api.kimi.com/coding/v1"
```

`workspace_root: "."` 表示文件工具和 shell 默认作用于启动 `naumi` 时的当前目录。

### 启动

```bash
# 推荐：全屏 CLI
naumi chat

# 等价的源码启动方式
python -m naumi_agent.main chat

# 新一代 Node 终端 UI
naumi ui

# Textual TUI fallback
naumi ui --legacy
naumi chat --tui

# 单任务执行
naumi run "检查这个项目的测试风险"

# REST API 服务
naumi serve
```

`naumi ui` 需要 Node.js 20+；如果本机没有 Node 或版本过旧，可以先使用 `naumi chat`、`naumi ui --legacy` 或 `naumi chat --tui`。

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
