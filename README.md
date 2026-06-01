<div align="center">
  <img src="assets/logo.svg" width="128" height="128" alt="NaumiAgent Logo">
  <h1>NaumiAgent</h1>
  <p>通用智能 Agent 系统，支持多模型、工具调用、流式输出和长期记忆。</p>
</div>

## 快速开始

### 安装

```bash
pip install -e ".[dev]"
```

### 配置

1. 复制 `.env.example` 为 `.env`，填入 API Key
2. 或通过环境变量设置：`export NAUMI_MODELS__API_KEY=your-key`

### 启动

```bash
# CLI 交互对话
naumi chat

# TUI 界面（推荐）
naumi chat --tui

# 单任务执行
naumi run "你的任务"

# REST API 服务
naumi serve
```

## 架构

```
src/naumi_agent/
├── orchestrator/     # 核心引擎 — ReAct 循环、Planner、子 Agent
├── model/            # 模型路由 — LiteLLM 统一调用
├── tools/            # 工具系统 — 文件、浏览器、代码沙箱、网络、记忆
├── safety/           # 安全 — 权限、预算、输出护栏
├── memory/           # 记忆 — 会话持久化(SQLite)、长期记忆(ChromaDB)、上下文压缩
├── streaming/        # 事件总线 — 发布/订阅
├── tui/              # TUI 界面 — Textual
├── api/              # REST API — FastAPI
├── agents/           # 子 Agent — coder、researcher、browser
└── config/           # 配置 — pydantic-settings + YAML
```

## 模型配置

支持通过 `config.yaml` 配置模型和元数据：

```yaml
models:
  default_model: "openai/kimi-for-coding"
  api_base: "https://api.kimi.com/coding/v1"
  model_info:
    openai/kimi-for-coding:
      max_context: 256000
      max_output: 8192
```

模型元数据采用三级查找：config 覆盖 → litellm 内置 → 128K 回退。

## 开发

```bash
# Lint
ruff check src/ tests/
ruff format src/ tests/

# 测试
pytest tests/ -q

# 覆盖率
pytest tests/ --cov=src/naumi_agent --cov-report=term-missing

# 类型检查
mypy src/naumi_agent --ignore-missing-imports
```

## Docker

```bash
cp .env.example .env
# 编辑 .env，填入 NAUMI_MODELS__API_KEY
mkdir -p workspace
docker compose up --build
```

启动后访问 `http://127.0.0.1:8080/docs`。完整部署说明见
[docs/deployment.md](docs/deployment.md)。

## License

MIT
