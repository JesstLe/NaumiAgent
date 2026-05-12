# 第八部分：技术选型与实现计划

## 1. 技术选型详解

### 1.1 为什么选 LangGraph？

经过对 8 大框架的深入对比，**LangGraph** 作为编排层最为合适：

| 对比维度 | LangGraph | CrewAI | OpenAI SDK | Claude SDK | 自建 |
|---------|-----------|--------|------------|------------|------|
| 状态持久化 | ✅ 内建检查点 | ❌ | ❌ | ❌ | 需自建 |
| 崩溃恢复 | ✅ Time-travel | ❌ | ❌ | ❌ | 需自建 |
| 灵活性 | ✅ 任意 DAG | ⚠️ 角色 | ⚠️ 线性 | ⚠️ 子Agent | ✅ 完全 |
| 模型无关 | ✅ 任意模型 | ✅ | ❌ OpenAI | ❌ Claude | ✅ |
| 复杂度 | 中 | 低 | 低 | 中 | 高 |
| 可观测性 | ✅ LangSmith | ⚠️ | ✅ | ⚠️ | 需自建 |

**结论**：通用 Agent 需要状态持久化和灵活的工作流，LangGraph 是最合适的选择。但不完全依赖它 — 在 LangGraph 之上封装我们自己的语义接口。

### 1.2 为什么用 LiteLLM？

```python
# 一个接口调用所有模型
from litellm import completion

# Claude
response = completion(model="claude-sonnet-4-6", messages=[...])
# GPT-4o
response = completion(model="gpt-4o", messages=[...])
# Gemini
response = completion(model="gemini/gemini-2.5-flash", messages=[...])
# 本地模型
response = completion(model="ollama/qwen3", messages=[...])
```

优势：
- **一个接口** — 40+ 模型提供商统一 API
- **自动重试** — 模型 A 失败自动切换模型 B
- **费用追踪** — 内建 token 计数和成本估算
- **流式输出** — 所有模型统一流式接口

### 1.3 为什么用 ChromaDB？

长期记忆的向量存储：

| 选项 | 优势 | 劣势 |
|------|------|------|
| **ChromaDB** | 嵌入式、零配置、Python原生 | 大规模性能一般 |
| pgvector | PostgreSQL生态、生产级 | 需要 PG 实例 |
| Milvus | 高性能分布式 | 重量级 |

**选择 ChromaDB** — 开发阶段零配置启动，后期可迁移到 pgvector。

### 1.4 为什么用 Playwright？

浏览器自动化：

| 选项 | 优势 | 劣势 |
|------|------|------|
| **Playwright** | 多浏览器、自动等待、截图好 | 无 |
| Selenium | 生态大 | API老旧、速度慢 |
| Puppeteer | Chrome专精 | 仅Chrome |

**选择 Playwright** — 和 MCP 生态兼容性最好，截图+DOM 操作能力强。

## 2. 依赖清单

```toml
# pyproject.toml

[project]
name = "naumi-agent"
version = "0.1.0"
requires-python = ">=3.12"

dependencies = [
    # 核心
    "litellm>=1.60",              # 多模型统一接口
    "langgraph>=0.4",             # 有状态图编排
    "langchain-core>=0.3",        # LangGraph 依赖

    # 工具
    "mcp>=1.0",                   # MCP 协议客户端
    "playwright>=1.50",           # 浏览器自动化

    # 记忆
    "chromadb>=1.0",              # 向量数据库（长期记忆）
    "aiosqlite>=0.21",            # 异步 SQLite（会话存储）

    # 接口
    "typer>=0.15",                # CLI 框架
    "rich>=14.0",                 # 终端美化
    "fastapi>=0.115",             # REST API
    "websockets>=14.0",           # WebSocket

    # 安全与可观测
    "pydantic>=2.10",             # 数据验证
    "opentelemetry-api>=1.30",    # 分布式追踪
    "opentelemetry-sdk>=1.30",

    # 工具辅助
    "httpx>=0.28",                # HTTP 客户端
    "readability-lxml>=0.8",      # 网页正文提取
    "markdownify>=1.1",           # HTML → Markdown
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.25",
    "pytest-cov>=6.0",
    "ruff>=0.11",
    "mypy>=1.15",
]
docker = [
    "docker>=7.0",               # 沙箱执行
]
e2b = [
    "e2b-code-interpreter>=1.0",  # E2B 沙箱
]

[project.scripts]
naumi = "naumi_agent.main:cli"
```

## 3. 配置系统

```python
# src/naumi_agent/config/settings.py

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field

class ModelConfig(BaseSettings):
    default_model: str = "claude-sonnet-4-6"
    fast_model: str = "claude-haiku-4-5"
    reasoning_model: str = "claude-opus-4-7"
    max_tokens: int = 4096
    temperature: float = 0.7

class MemoryConfig(BaseSettings):
    session_db_path: str = "data/sessions.db"
    vector_db_path: str = "data/chroma"
    compaction_threshold: float = 0.75

class SafetyConfig(BaseSettings):
    permission_mode: str = "moderate"
    allowed_dirs: list[str] = Field(default_factory=lambda: ["/workspace"])
    max_budget_usd: float = 5.0
    max_turns: int = 30
    max_input_tokens: int = 500000

class MCPConfig(BaseSettings):
    servers: dict[str, dict] = Field(default_factory=dict)

class AppConfig(BaseSettings):
    model_config = SettingsConfigDict(
        yaml_file="config.yaml",
        env_prefix="NAUMI_",
        env_nested_delimiter="__",
    )

    models: ModelConfig = Field(default_factory=ModelConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    mcp: MCPConfig = Field(default_factory=MCPConfig)
    custom_tools_dir: str | None = None
    log_level: str = "INFO"
```

```yaml
# config.example.yaml

models:
  default_model: "claude-sonnet-4-6"
  fast_model: "claude-haiku-4-5"
  reasoning_model: "claude-opus-4-7"
  max_tokens: 4096
  temperature: 0.7

memory:
  session_db_path: "data/sessions.db"
  vector_db_path: "data/chroma"
  compaction_threshold: 0.75

safety:
  permission_mode: "moderate"
  allowed_dirs:
    - "/workspace"
  max_budget_usd: 5.0
  max_turns: 30
  max_input_tokens: 500000

mcp:
  servers: {}

log_level: "INFO"
```

## 4. CLI 入口

```python
# src/naumi_agent/main.py

import typer
from rich.console import Console

app = typer.Typer(name="naumi", help="NaumiAgent — 通用智能 Agent")
console = Console()

@app.command()
def chat(
    session: str | None = typer.Option(None, "--session", "-s", help="恢复会话 ID"),
    config: str = typer.Option("config.yaml", "--config", "-c", help="配置文件路径"),
):
    """启动交互式对话"""
    import asyncio
    asyncio.run(_chat(session, config))

async def _chat(session_id: str | None, config_path: str):
    from .config.settings import AppConfig
    from .orchestrator.engine import AgentEngine

    config = AppConfig.from_yaml(config_path)
    engine = AgentEngine(config)

    if session_id:
        await engine.load_session(session_id)

    console.print("[bold green]NaumiAgent 已启动[/bold green]")
    console.print("输入任务开始对话，输入 /quit 退出\n")

    while True:
        user_input = console.input("[bold blue]你>[/bold blue] ")

        if user_input.strip() in ("/quit", "/exit", "exit"):
            await engine.save_current_session()
            console.print("[green]会话已保存，再见！[/green]")
            break

        if user_input.startswith("/"):
            await _handle_command(engine, user_input)
            continue

        with console.status("[bold green]NaumiAgent 思考中..."):
            result = await engine.run(user_input)

        console.print(f"\n[bold green]NaumiAgent>[/bold green] {result.response}")
        console.print(f"[dim]Token: {result.usage.total_cost:.4f} USD | {result.turns} 轮[/dim]\n")

@app.command()
def run(
    task: str = typer.Argument(help="要执行的任务"),
    config: str = typer.Option("config.yaml", "--config", "-c"),
):
    """执行单个任务"""
    import asyncio
    asyncio.run(_run_task(task, config))

async def _run_task(task: str, config_path: str):
    from .config.settings import AppConfig
    from .orchestrator.engine import AgentEngine

    config = AppConfig.from_yaml(config_path)
    engine = AgentEngine(config)
    result = await engine.run(task)
    console.print(result.response)

if __name__ == "__main__":
    app()
```

## 5. 实现路线图

### Phase 1：最小可用 Agent（2 周）

```
目标：能对话、能调用工具、能完成基本任务

Week 1:
├── 项目脚手架（pyproject.toml、配置、目录结构）
├── 模型层（LiteLLM 封装、路由）
├── 核心引擎（主循环、消息管理）
└── 基础工具（file_read、file_write、file_edit、bash_run）

Week 2:
├── CLI 入口（交互式对话）
├── 会话持久化（SQLite）
├── 上下文压缩
└── 基础测试（单元测试 + 集成测试）

交付物：
- `naumi chat` 能正常对话
- 能读写文件和执行基本命令
- 会话可以保存和恢复
```

### Phase 2：能力扩展（2 周）

```
目标：能浏览网页、执行代码、搜索信息

Week 3:
├── 浏览器自动化（Playwright 集成）
├── 代码沙箱执行（Docker 集成）
├── 网络搜索工具
└── 工具系统增强（MCP 客户端）

Week 4:
├── 规划器（任务分解、执行模式选择）
├── 评估器（结果质量检查）
├── 预算控制
└── 可观测性（OpenTelemetry 集成）

交付物：
- 能浏览网页和执行操作
- 能在沙箱中执行代码
- 任务能被自动分解和评估
```

### Phase 3：智能增强（2 周）

```
目标：记忆系统、多 Agent 协作

Week 5:
├── 长期记忆（ChromaDB 集成）
├── 记忆检索和上下文注入
├── 用户偏好学习
└── 经验积累和复用

Week 6:
├── 子 Agent 定义（Coder、Researcher、Browser）
├── Agent 调度器
├── 并行执行
├── 安全护栏（输入校验、输出审计）
└── 权限系统

交付物：
- 跨会话记忆
- 多 Agent 协作
- 完善的安全机制
```

### Phase 4：生产化（2 周）

```
目标：稳定性、性能、部署

Week 7:
├── REST API（FastAPI）
├── WebSocket 支持
├── 错误恢复增强
├── 性能优化（缓存、并发控制）
└── 压力测试

Week 8:
├── Docker 镜像
├── 部署文档
├── 用户文档
├── 示例和教程
└── CI/CD 流程

交付物：
- 可部署的 Agent 服务
- 完整文档和示例
- 生产级可靠性
```

## 6. 测试策略

```
tests/
├── unit/                         # 单元测试
│   ├── test_model_router.py      # 模型路由
│   ├── test_tool_registry.py     # 工具注册
│   ├── test_compaction.py        # 上下文压缩
│   ├── test_planner.py           # 规划器
│   ├── test_guardrails.py        # 安全护栏
│   └── test_budget.py            # 预算控制
├── integration/                  # 集成测试
│   ├── test_agent_engine.py      # Agent 主循环
│   ├── test_file_tools.py        # 文件工具（真实文件系统）
│   ├── test_browser_tools.py     # 浏览器工具（Playwright）
│   ├── test_memory_system.py     # 记忆系统（真实 DB）
│   └── test_subagents.py         # 子 Agent 协作
├── e2e/                          # 端到端测试
│   ├── test_simple_tasks.py      # 简单任务流程
│   ├── test_code_tasks.py        # 编程任务
│   ├── test_research_tasks.py    # 研究任务
│   └── test_multi_step_tasks.py  # 多步骤任务
└── fixtures/                     # 测试数据
    ├── sample_project/           # 示例项目
    └── mock_responses/           # Mock LLM 响应
```

**覆盖率要求**：80%+（单元 + 集成），核心引擎 90%+。

## 7. 关键风险与缓解

| 风险 | 概率 | 影响 | 缓解措施 |
|------|------|------|---------|
| LLM 幻觉导致错误操作 | 高 | 高 | 执行前确认 + 沙箱隔离 + 输出审计 |
| Token 成本失控 | 中 | 中 | 预算上限 + Haiku 路由 + 上下文压缩 |
| 工具调用死循环 | 中 | 中 | 轮次限制 + 循环检测 + 最大调用次数 |
| MCP 服务器不稳定 | 中 | 低 | 超时机制 + 自动重试 + 优雅降级 |
| Prompt Injection | 中 | 高 | 输入校验 + 权限分级 + 沙箱隔离 |
