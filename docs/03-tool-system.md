# 第三部分：工具系统

## 1. 设计原则

工具是 Agent 的"手脚"。根据 Anthropic "Prompt Engineering your Tools" 建议：

1. **给模型足够的 token 去"思考"** — 不要让工具参数格式成为负担
2. **格式接近自然文本** — JSON 优于 diff，markdown 优于纯文本转义
3. **防呆设计（Poka-yoke）** — 参数设计应让犯错变难（如用绝对路径代替相对路径）
4. **投入 ACI 和投入 HCI 同等的精力** — 工具定义 = 给初级开发者的 docstring

## 2. 工具注册表

### 2.1 工具基类

```python
# src/naumi_agent/tools/registry.py

from dataclasses import dataclass
from typing import Any, Callable
from pydantic import BaseModel

class ToolSchema(BaseModel):
    """工具的 JSON Schema 定义，直接暴露给 LLM"""
    name: str
    description: str  # 关键：描述要像给初级开发者写的 docstring
    parameters: dict  # JSON Schema 格式
    required: list[str]

@dataclass
class Tool:
    name: str
    description: str
    schema: ToolSchema
    handler: Callable
    permission_level: str  # "safe" | "moderate" | "dangerous"
    sandboxed: bool = False

class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        return self._tools[name]

    def get_schemas(self) -> list[dict]:
        """返回所有工具的 JSON Schema（给 LLM 用）"""
        return [
            {
                "name": t.schema.name,
                "description": t.schema.description,
                "input_schema": {
                    "type": "object",
                    "properties": t.schema.parameters,
                    "required": t.schema.required,
                },
            }
            for t in self._tools.values()
        ]

    def by_permission(self, level: str) -> list[Tool]:
        """按权限级别筛选工具"""
        levels = {"safe": ["safe"], "moderate": ["safe", "moderate"], "dangerous": ["safe", "moderate", "dangerous"]}
        return [t for t in self._tools.values() if t.permission_level in levels[level]]

    def load_builtins(self) -> None:
        """加载所有内置工具"""
        from .builtin import filesystem, shell, browser, code_sandbox, web_search
        for module in [filesystem, shell, browser, code_sandbox, web_search]:
            for tool in module.tools():
                self.register(tool)
```

### 2.2 工具定义示例（Anthropic 推荐的 ACI 最佳实践）

```python
# src/naumi_agent/tools/builtin/filesystem.py

file_read_tool = Tool(
    name="file_read",
    description="""读取文件的完整内容。

支持的参数：
- path: 文件的绝对路径（必须用绝对路径，不要用相对路径）
- encoding: 文件编码，默认 utf-8
- offset: 从第几行开始读（可选，从 1 开始）
- limit: 最多读多少行（可选）

示例：
- 读整个文件：file_read(path="/home/user/code/main.py")
- 读第 10-20 行：file_read(path="/home/user/code/main.py", offset=10, limit=10)

注意：如果文件不存在，会返回错误。先用 file_exists 检查。
""",
    schema=ToolSchema(
        name="file_read",
        description="读取指定文件的内容",
        parameters={
            "path": {
                "type": "string",
                "description": "文件的绝对路径（必须使用绝对路径）",
            },
            "encoding": {
                "type": "string",
                "description": "文件编码",
                "default": "utf-8",
            },
            "offset": {
                "type": "integer",
                "description": "起始行号（从 1 开始）",
            },
            "limit": {
                "type": "integer",
                "description": "读取的最大行数",
            },
        },
        required=["path"],
    ),
    handler=_read_file,
    permission_level="safe",
)

file_edit_tool = Tool(
    name="file_edit",
    description="""编辑文件的部分内容。使用搜索-替换模式，不需要重写整个文件。

参数：
- path: 文件的绝对路径
- old_text: 要替换的原文本（必须精确匹配文件中的内容，包括缩进）
- new_text: 替换后的新文本

这个工具会精确查找 old_text 并替换为 new_text。如果 old_text 在文件中出现多次，
必须提供足够的上下文使匹配唯一。

示例（修复一个 typo）：
old_text: "def claculate_sum(a, b):"
new_text: "def calculate_sum(a, b):"
""",
    schema=ToolSchema(
        name="file_edit",
        description="搜索并替换文件中的部分内容",
        parameters={
            "path": {"type": "string", "description": "文件绝对路径"},
            "old_text": {"type": "string", "description": "要被替换的原文本"},
            "new_text": {"type": "string", "description": "替换后的新文本"},
        },
        required=["path", "old_text", "new_text"],
    ),
    handler=_edit_file,
    permission_level="moderate",
)
```

## 3. 内置工具集

### 3.1 文件系统操作

| 工具名 | 权限 | 功能 |
|--------|------|------|
| `file_read` | safe | 读取文件内容 |
| `file_write` | moderate | 写入/创建文件 |
| `file_edit` | moderate | 搜索替换编辑文件 |
| `file_exists` | safe | 检查文件是否存在 |
| `file_list` | safe | 列出目录内容 |
| `file_search` | safe | Grep 搜索文件内容 |
| `file_glob` | safe | 按模式匹配文件路径 |

**关键实现细节**：

```python
# 沙箱路径检查 — 所有文件操作必须经过
def _validate_path(path: str, allowed_dirs: list[str]) -> str:
    """确保路径在允许的目录内"""
    abs_path = os.path.abspath(path)
    if not any(abs_path.startswith(allowed) for allowed in allowed_dirs):
        raise PermissionError(
            f"路径 '{path}' 不在允许的工作目录范围内"
        )
    return abs_path

async def _read_file(path: str, encoding: str = "utf-8", offset: int | None = None, limit: int | None = None) -> str:
    validated = _validate_path(path, config.allowed_dirs)
    with open(validated, "r", encoding=encoding) as f:
        lines = f.readlines()
    if offset:
        lines = lines[offset - 1:]
    if limit:
        lines = lines[:limit]
    return "".join(lines)
```

### 3.2 Shell 命令执行

| 工具名 | 权限 | 功能 |
|--------|------|------|
| `bash_run` | dangerous | 在沙箱中执行 shell 命令 |
| `bash_background` | dangerous | 后台运行长时命令 |

```python
async def _execute_shell(
    command: str,
    cwd: str | None = None,
    timeout: int = 30,
    env: dict | None = None,
) -> ShellResult:
    """在沙箱中执行 shell 命令"""
    # 命令白名单检查
    base_cmd = command.split()[0]
    if base_cmd in BLOCKED_COMMANDS:
        return ShellResult(
            exit_code=-1,
            error=f"命令 '{base_cmd}' 被禁止。禁止列表：{BLOCKED_COMMANDS}",
        )

    proc = await asyncio.create_subprocess_exec(
        "/bin/bash", "-c", command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=env,
    )

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
    except asyncio.TimeoutError:
        proc.kill()
        return ShellResult(
            exit_code=-1,
            error=f"命令超时（{timeout}秒）",
        )

    return ShellResult(
        exit_code=proc.returncode,
        stdout=stdout.decode("utf-8", errors="replace"),
        stderr=stderr.decode("utf-8", errors="replace"),
    )

BLOCKED_COMMANDS = {
    "rm", "rmdir", "mkfs", "dd", "format",
    "shutdown", "reboot", "halt", "poweroff",
    "sudo", "su", "passwd",
    "curl", "wget",  # 使用专用工具代替
}
```

### 3.3 浏览器自动化

基于 Playwright，实现类似 Claude Computer Use 的浏览器操作。

| 工具名 | 权限 | 功能 |
|--------|------|------|
| `browser_goto` | safe | 打开网页 |
| `browser_observe` | safe | SoM 观察页面元素 |
| `browser_screenshot` | safe | 截取当前页面 |
| `browser_click` | moderate | 点击元素 |
| `browser_type` | moderate | 输入文字 |
| `browser_scroll` | safe | 滚动页面 |
| `browser_evaluate` | dangerous | 执行 JavaScript |

```python
from playwright.async_api import async_playwright, Page, Browser

class BrowserTool:
    def __init__(self):
        self._playwright = None
        self._browser: Browser | None = None
        self._page: Page | None = None

    async def _ensure_browser(self) -> Page:
        if not self._browser:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox"],
            )
            self._page = await self._browser.new_page(viewport={"width": 1280, "height": 720})
        return self._page

    async def navigate(self, url: str) -> dict:
        page = await self._ensure_browser()
        response = await page.goto(url, wait_until="domcontentloaded")
        return {
            "status": response.status if response else "unknown",
            "url": page.url,
            "title": await page.title(),
        }

    async def screenshot(self) -> dict:
        """截取当前页面截图，返回 base64 编码的图片"""
        page = await self._ensure_browser()
        screenshot_bytes = await page.screenshot(full_page=False)
        import base64
        return {
            "image_base64": base64.b64encode(screenshot_bytes).decode(),
            "url": page.url,
            "title": await page.title(),
        }

    async def click(self, selector: str) -> dict:
        """点击指定元素。selector 使用 CSS 选择器或文本匹配"""
        page = await self._ensure_browser()
        try:
            await page.click(selector, timeout=5000)
            return {"status": "clicked", "selector": selector}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    async def extract_text(self) -> dict:
        """提取页面的主要文本内容"""
        page = await self._ensure_browser()
        text = await page.evaluate("""() => {
            // 移除 script 和 style 标签
            for (const el of document.querySelectorAll('script, style, nav, footer')) {
                el.remove();
            }
            return document.body.innerText;
        }""")
        return {"text": text[:5000], "url": page.url}  # 限制长度
```

### 3.4 代码沙箱执行

| 工具名 | 权限 | 功能 |
|--------|------|------|
| `code_execute` | dangerous | 在隔离沙箱中执行代码 |
| `code_install` | moderate | 安装 Python/JS 包 |

```python
class CodeSandbox:
    """基于 Docker 的代码执行沙箱"""

    def __init__(self, config: SandboxConfig):
        self.config = config
        self._container = None

    async def execute(
        self,
        code: str,
        language: str = "python",
        timeout: int = 30,
        packages: list[str] | None = None,
    ) -> SandboxResult:
        """在 Docker 容器中执行代码"""
        import docker
        client = docker.from_env()

        image = f"naumi-sandbox-{language}:latest"

        # 准备执行环境
        if packages:
            install_cmd = self._install_command(language, packages)
            code = f"{install_cmd}\n{code}"

        container = client.containers.run(
            image,
            command=self._run_command(language, code),
            mem_limit=self.config.memory_limit,
            cpu_period=100000,
            cpu_quota=int(100000 * self.config.cpu_limit),
            network_disabled=not self.config.allow_network,
            remove=True,
            timeout=timeout,
        )

        return SandboxResult(
            stdout=container.decode("utf-8"),
            exit_code=0,
        )
```

### 3.5 网络搜索

| 工具名 | 权限 | 功能 |
|--------|------|------|
| `web_search` | safe | 搜索网络信息 |
| `web_fetch` | safe | 获取网页内容 |

```python
async def web_search(query: str, max_results: int = 5) -> dict:
    """搜索网络，返回结果摘要"""
    # 支持多个搜索后端
    results = await search_backend.search(query, max_results)
    return {
        "query": query,
        "results": [
            {
                "title": r.title,
                "url": r.url,
                "snippet": r.snippet,
            }
            for r in results
        ],
    }

async def web_fetch(url: str, format: str = "markdown") -> dict:
    """获取并解析网页内容"""
    import httpx
    from readability import Document
    from markdownify import markdownify

    async with httpx.AsyncClient(follow_redirects=True) as client:
        response = await client.get(url, timeout=15)
        response.raise_for_status()

    doc = Document(response.text)
    title = doc.title()
    content = markdownify(doc.summary())

    return {
        "url": url,
        "title": title,
        "content": content[:10000],  # 限制长度节省 token
    }
```

## 4. MCP 客户端集成

### 4.1 MCP 协议接口

MCP（Model Context Protocol）是 Anthropic 推动的工具集成标准。200+ 服务器实现。

```python
# src/naumi_agent/tools/mcp_client.py

from dataclasses import dataclass
from typing import Any

@dataclass
class MCPServerConfig:
    name: str
    transport: str  # "stdio" | "http" | "sse"
    command: str | None = None      # stdio 模式
    url: str | None = None          # http/sse 模式
    headers: dict[str, str] | None = None
    env: dict[str, str] | None = None

class MCPClient:
    """MCP 客户端 — 连接外部工具服务器"""

    def __init__(self):
        self._connections: dict[str, MCPConnection] = {}

    async def connect(self, config: MCPServerConfig) -> None:
        """连接 MCP 服务器"""
        match config.transport:
            case "stdio":
                conn = await self._connect_stdio(config)
            case "http" | "sse":
                conn = await self._connect_http(config)

        self._connections[config.name] = conn

        # 发现服务器提供的工具
        tools = await conn.list_tools()
        for tool_schema in tools:
            # 将 MCP 工具注册到全局工具表
            self._register_mcp_tool(config.name, tool_schema, conn)

    async def _connect_stdio(self, config: MCPServerConfig) -> MCPConnection:
        """通过 stdio 连接 MCP 服务器"""
        proc = await asyncio.create_subprocess_exec(
            config.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, **(config.env or {})},
        )
        return StdioMCPConnection(proc)

    async def _connect_http(self, config: MCPServerConfig) -> MCPConnection:
        """通过 HTTP/SSE 连接 MCP 服务器"""
        return HTTPMCPConnection(config.url, config.headers)

    def _register_mcp_tool(
        self, server_name: str, schema: dict, conn: MCPConnection
    ) -> None:
        """将 MCP 工具包装为本地 Tool 对象"""
        tool = Tool(
            name=f"mcp_{server_name}_{schema['name']}",
            description=schema.get("description", ""),
            schema=ToolSchema(
                name=f"mcp_{server_name}_{schema['name']}",
                description=schema.get("description", ""),
                parameters=schema.get("inputSchema", {}).get("properties", {}),
                required=schema.get("inputSchema", {}).get("required", []),
            ),
            handler=lambda **args: conn.call_tool(schema["name"], args),
            permission_level="moderate",
        )
        self.tool_registry.register(tool)
```

### 4.2 MCP 配置

```yaml
# config.yaml
mcp_servers:
  github:
    transport: http
    url: "https://api.githubcopilot.com/mcp/"
    headers:
      Authorization: "Bearer ${GITHUB_TOKEN}"

  slack:
    transport: stdio
    command: "npx -y @modelcontextprotocol/server-slack"
    env:
      SLACK_BOT_TOKEN: "${SLACK_BOT_TOKEN}"

  filesystem:
    transport: stdio
    command: "npx -y @modelcontextprotocol/server-filesystem /workspace"
```

## 5. 工具加载器

```python
# src/naumi_agent/tools/loader.py

class ToolLoader:
    """动态加载和初始化工具"""

    def __init__(self, tool_registry: ToolRegistry):
        self.registry = tool_registry

    async def load_from_config(self, config: AppConfig) -> None:
        """从配置文件加载所有工具"""
        # 1. 加载内置工具
        self.registry.load_builtins()

        # 2. 加载 MCP 服务器
        mcp_client = MCPClient(self.registry)
        for server_config in config.mcp_servers:
            try:
                await mcp_client.connect(server_config)
            except Exception as e:
                logger.warning(f"Failed to connect MCP server {server_config.name}: {e}")

        # 3. 加载自定义工具插件
        if config.custom_tools_dir:
            await self._load_custom_tools(config.custom_tools_dir)
```
