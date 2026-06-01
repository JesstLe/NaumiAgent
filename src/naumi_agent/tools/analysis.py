"""分析模式工具 — chaos/scale/state/vibe，可作为工具被 Agent 自主调用.

每个工具执行两阶段分析:
  1. 静态扫描阶段 — 读文件、grep 模式、统计指标，收集实打实的代码证据
  2. LLM 综合阶段 — 把扫描证据 + 专有 prompt 交给 LLM 做深度推理与建议
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from naumi_agent.tools import analysis_common
from naumi_agent.tools.base import Tool, ToolMetadata

logger = logging.getLogger(__name__)

_SOURCE_EXTENSIONS = analysis_common.SOURCE_EXTENSIONS
_read_sources = analysis_common.read_sources
_resolve_target = analysis_common.resolve_target
_router_unavailable = analysis_common.router_unavailable
_run_analysis = analysis_common.run_analysis

# --- 各模式专用的静态扫描函数 ---

def _scan_chaos(files: list[Path], source_text: str) -> str:
    """chaos 模式静态扫描：找真正的脆弱点."""
    findings: list[str] = []
    lines = source_text.split("\n")

    # 1. 统计 try/except 覆盖率
    total_func_lines = 0
    covered_lines = 0
    in_try = False
    try_depth = 0
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("def ") or stripped.startswith("async def "):
            total_func_lines += 1
        if re.match(r"\btry\s*:", stripped):
            in_try = True
            try_depth += 1
        if in_try:
            covered_lines += 1
        if re.match(r"\b(except|finally)\s*:", stripped):
            try_depth -= 1
            if try_depth <= 0:
                in_try = False

    if total_func_lines > 0:
        findings.append(
            f"- 错误处理覆盖率: {covered_lines}/{len(lines)} 行在 try 块内 "
            f"({covered_lines * 100 // max(len(lines), 1)}%)"
        )

    # 2. 找裸 except / 过宽异常捕获
    bare_excepts = re.findall(r"^(.*?)except\s*:", source_text, re.MULTILINE)
    if bare_excepts:
        findings.append(f"- 裸 except (捕获所有异常): {len(bare_excepts)} 处")
        for ctx in bare_excepts[:5]:
            findings.append(f'  - `{ctx.strip()[-60:]}`')

    # 3. 找硬编码的连接/密钥/配置
    hardcoded = re.findall(
        r'(?:(?:host|HOST|url|URL|endpoint|ENDPOINT)\s*[=:]\s*["\']'
        r'(?:https?://|localhost|127\.0\.0\.|0\.0\.0\.0)[^"\']*)',
        source_text,
    )
    if hardcoded:
        findings.append(f"- 硬编码连接地址: {len(hardcoded)} 处")
        for h in hardcoded[:5]:
            findings.append(f'  - `{h.strip()}`')

    # 4. 找无超时的外部调用
    no_timeout = re.findall(
        r"(?:requests\.(?:get|post|put|delete|patch)|httpx\.\w+\.request|urllib\.request\.urlopen)"
        r"\([^)]*\)",
        source_text,
    )
    no_timeout_missing = [
        c for c in no_timeout
        if "timeout" not in c.lower()
    ]
    if no_timeout_missing:
        findings.append(f"- 无 timeout 的外部 HTTP 调用: {len(no_timeout_missing)} 处")

    # 5. 找单例 / 全局可变状态
    global_mutations = re.findall(
        r"^(?:\w+\s*[=:]\s*(?:\{|\[|None|\"\"|dict\(\)|list\(\)|set\(\)))",
        source_text,
        re.MULTILINE,
    )
    if global_mutations:
        findings.append(f"- 模块级可变状态 (潜在 SPOF): {len(global_mutations)} 处")
        for g in global_mutations[:5]:
            findings.append(f'  - `{g.strip()}`')

    # 6. 找没有 retry 的外部依赖调用
    external_calls = len(re.findall(
        r"(?:requests\.|httpx\.|aiohttp\.|fetch\(|urllib|redis|mongo|sqlalchemy)",
        source_text,
    ))
    retry_count = len(re.findall(r"\bretry\b", source_text, re.IGNORECASE))
    if external_calls > 0:
        findings.append(
            f"- 外部依赖调用: {external_calls} 次, retry 机制: {retry_count} 处 "
            f"({'⚠️ 无重试保护' if retry_count == 0 else '✓ 有重试'})"
        )

    # 7. 文件统计
    findings.append(f"- 扫描文件: {len(files)} 个, 总代码行数: {len(lines)}")

    return "\n".join(findings) if findings else "- 静态扫描未发现明显问题"


def _scan_scale(files: list[Path], source_text: str, qps: int) -> str:
    """scale 模式静态扫描：找并发瓶颈."""
    findings: list[str] = []
    lines = source_text.split("\n")

    # 1. 找数据库连接池配置
    pool_configs = re.findall(
        r"(?:pool_size|max_connections|POOL_SIZE|MAX_CONN|pool_overflow)[^\n]*",
        source_text,
    )
    if pool_configs:
        findings.append("- 数据库连接池配置:")
        for p in pool_configs[:5]:
            findings.append(f"  - `{p.strip()}`")
    else:
        findings.append("- ⚠️ 未发现连接池配置，可能使用默认值或无池化")

    # 2. 找同步阻塞调用（在 async 上下文中）
    sync_io = re.findall(
        r"(?:requests\.(?:get|post|put|delete)|urllib\.request|open\([^)]*\)(?!.*with))",
        source_text,
    )
    if sync_io:
        findings.append(
            f"- 同步阻塞 I/O 调用: {len(sync_io)} 处 "
            "(在高并发下会阻塞事件循环)"
        )

    # 3. 找锁 / 线程同步
    locks = re.findall(r"(?:threading\.Lock|multiprocessing\.Lock|asyncio\.Lock)", source_text)
    if locks:
        findings.append(f"- 锁使用: {len(locks)} 处 (可能成为争用热点)")

    # 4. 找缓存模式
    cache_pattern = r"(?:lru_cache|functools\.cache|@cache|redis|memcache|cachetools)"
    cache_hits = re.findall(cache_pattern, source_text)
    if cache_hits:
        findings.append(f"- 缓存机制: {len(cache_hits)} 处引用")
    else:
        findings.append("- ⚠️ 未发现缓存机制，每个请求都会穿透到数据层")

    # 5. 找 N+1 查询模式
    n_plus_1 = re.findall(
        r"for\s+\w+\s+in\s+.*:\s*\n\s+.*(?:\.query|\.filter|\.get|\.find|SELECT)",
        source_text,
    )
    if n_plus_1:
        findings.append(f"- ⚠️ 疑似 N+1 查询: {len(n_plus_1)} 处")

    # 6. 找限流 / 熔断
    rate_limits = re.findall(r"(?:rate.?limit|throttl|circuit.?breaker|Semaphore)", source_text)
    if rate_limits:
        findings.append(f"- 限流/熔断: {len(rate_limits)} 处")
    else:
        findings.append("- ⚠️ 无限流/熔断保护，突发流量将直接冲击后端")

    # 7. QPS 估算
    findings.append(
        f"- 目标 QPS: {qps:,} | 扫描代码: {len(files)} 文件, "
        f"{len(lines)} 行 | 缓存: {'有' if cache_hits else '无'} | "
        f"限流: {'有' if rate_limits else '无'}"
    )

    return "\n".join(findings)


def _scan_state(files: list[Path], source_text: str) -> str:
    """state 模式静态扫描：找有状态违规."""
    findings: list[str] = []
    source_text.split("\n")

    # 1. 找全局可变字典/列表 (最典型的内存状态)
    global_dicts = re.findall(
        r"^(?:_?\w+)\s*[=:]\s*(?:\{[^}]*\}|\[\]|\{\}|dict\(\)|list\(\))",
        source_text,
        re.MULTILINE,
    )
    if global_dicts:
        findings.append(f"- 🔴 模块级可变容器 (在多实例间不同步): {len(global_dicts)} 处")
        for g in global_dicts[:8]:
            findings.append(f"  - `{g.strip()}`")

    # 2. 找 threading.local
    thread_locals = re.findall(r"threading\.local\(\)", source_text)
    if thread_locals:
        findings.append(
            f"- 🔴 threading.local: {len(thread_locals)} 处 "
            "(进程间不共享，多实例部署会丢失)"
        )

    # 3. 找 threading.Lock / multiprocessing.Lock
    local_locks = re.findall(r"threading\.(?:Lock|RLock|Semaphore|Event)\(\)", source_text)
    if local_locks:
        findings.append(
            f"- 🟡 本地锁 (非分布式): {len(local_locks)} 处 "
            "(只在单进程内生效)"
        )

    # 4. 找本地文件写入
    file_writes = re.findall(
        r"(?:open\([^)]*[\"']w|\.write\(|os\.rename|shutil\.move|\.save\()",
        source_text,
    )
    if file_writes:
        findings.append(
            f"- 🟡 本地文件写入: {len(file_writes)} 处 "
            "(多实例部署时文件不同步)"
        )

    # 5. 找 in-memory session 存储
    session_patterns = re.findall(
        r"(?:session[s]?\s*[=:]\s*\{|SESSION[s]?\s*=\s*\{|_sessions\s*=)",
        source_text,
    )
    if session_patterns:
        findings.append(
            f"- 🔴 内存 Session 存储: {len(session_patterns)} 处 "
            "(用户请求打到不同实例会丢失登录态)"
        )

    # 6. 找 Singleton 模式
    singletons = re.findall(
        r"(?:__new__|_instance\s*=\s*None|_shared_state|__metaclass__.*Singleton)",
        source_text,
    )
    if singletons:
        findings.append(
            f"- 🟡 Singleton 模式: {len(singletons)} 处 "
            "(假设单进程，多实例会创建多个)"
        )

    # 7. 找 asyncio 全局状态
    async_globals = re.findall(
        r"(?:_queue\s*=\s*asyncio\.Queue|_event\s*=\s*asyncio\.Event|_cache\s*=\s*\{)",
        source_text,
    )
    if async_globals:
        findings.append(
            f"- 🟡 asyncio 全局队列/事件/缓存: {len(async_globals)} 处"
        )

    # 8. 找已使用的分布式方案
    redis_usage = len(re.findall(r"(?:redis|aioredis)", source_text))
    mq_usage = len(re.findall(r"(?:kafka|rabbitmq|celery|pika|confluent)", source_text))
    distributed = redis_usage + mq_usage
    findings.append(
        f"- 分布式组件: Redis {redis_usage} 处引用, "
        f"消息队列 {mq_usage} 处引用"
    )

    score = max(0, 100 - len(global_dicts) * 10 - len(thread_locals) * 15
                - len(session_patterns) * 20 - len(local_locks) * 5
                - len(file_writes) * 3 + distributed * 5)
    score = min(100, score)
    findings.append(f"- 云原生就绪评分: {score}/100")

    return "\n".join(findings) if findings else "- 静态扫描未发现状态违规"


# ---------------------------------------------------------------------------
#  LLM Prompt 模板
# ---------------------------------------------------------------------------

_CHAOS_SYSTEM = """\
You are a ruthless chaos engineering architect reviewing REAL static analysis evidence.

Below is auto-generated scan evidence from the target codebase, followed by the \
actual source code. Your job:

1. **SPOF Analysis (Top 3)**: Based on the evidence, identify the 3 most fragile \
single points of failure. Cite specific file paths and line patterns from the evidence. \
For each: describe the blast radius and failure probability.

2. **Catastrophic Scenario Simulation**: Assume a memory leak + database deadlock + \
critical dependency outage happen SIMULTANEOUSLY. Walk through the death spiral with \
simulated timestamps.

3. **Upgrade Roadmap**: Three-tier plan from emergency patches to enterprise resilience. \
Each fix must reference a specific finding from the evidence.

Be harsh, specific, and evidence-based. No generic advice.
"""

_SCALE_SYSTEM = """\
You are a high-concurrency architect. You have REAL static analysis evidence from the \
target codebase at {qps} QPS, plus the actual source code.

Based on the evidence:

1. **Bottleneck Map**: For each finding, quantify the max throughput and identify the \
first choke point. Include specific numbers (connection pool size, timeout values, etc.).

2. **Cascade Failure Chain**: Map what happens when the first bottleneck fails — \
which components cascade next?

3. **Remediation Plan**: Specific fixes with exact config values (pool sizes, buffer \
sizes, rate limit thresholds, queue depths). Reference the actual code patterns found.

Every recommendation must be backed by evidence from the scan results.
"""

_STATE_SYSTEM = """\
You are a distributed systems auditor. You have REAL static analysis evidence from \
the target codebase, plus the actual source code.

Based on the evidence:

1. **Violations Detail**: For each finding, explain exactly what breaks when the service \
is deployed behind a load balancer across 5 instances. Cite specific patterns.

2. **Distributed Replacements**: For every violation, provide the specific cloud-native \
replacement (Redis, Kafka, etc.) with configuration examples.

3. **Migration Priority**: Order fixes by severity (data loss risk first, then \
consistency, then performance). Include effort estimates.

Reference the scan evidence explicitly. No generic advice.
"""

_VIBE_SYSTEM = """\
You are in VIBE MODE. Drop all architectural concerns, edge cases, and perfectionism.

RULES:
- Output the FASTEST possible working code
- No error handling unless it is a single line
- No comments unless critical
- Use the most lightweight libraries available
- Hardcode configuration values — refinement comes later
- Skip writing tests initially
- If there is a 3-line solution and a 30-line "proper" solution, use the 3-line one
- Output COMPLETE, RUNNABLE code — no TODOs, no gaps, no scaffolding

Focus on the CORE functionality. Ship it.
"""


# ---------------------------------------------------------------------------
#  工具类
# ---------------------------------------------------------------------------

class ChaosAnalysisTool(Tool):
    """全局灾难演练 — 静态扫描 + LLM 综合分析，找出真正的 SPOF."""

    @property
    def name(self) -> str:
        return "analysis_chaos"

    @property
    def description(self) -> str:
        return (
            "对代码进行灾难演练分析。"
            "先静态扫描代码找脆弱点(裸except、硬编码、无重试、无超时等)，"
            "再由 LLM 综合推理出 SPOF、推演灾难场景、给出改造路线图。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "要分析的文件路径或目录路径",
                },
                "context": {
                    "type": "string",
                    "description": "补充上下文（系统架构、技术栈等）",
                    "default": "",
                },
            },
            "required": ["target"],
        }

    async def execute(self, *, target: str, context: str = "", **kwargs: Any) -> str:
        router = _global_router
        if router is None:
            return _router_unavailable("chaos", target)

        files = _resolve_target(target)
        if not files:
            return f"无法解析目标: {target} (请提供文件或目录路径)"

        source_text = _read_sources(files)
        scan_evidence = _scan_chaos(files, source_text)

        user_msg = (
            f"## 静态扫描证据\n{scan_evidence}\n\n"
            f"## 源代码\n{source_text[:50000]}\n"
        )
        if context:
            user_msg += f"\n## 补充上下文\n{context}\n"

        return await _run_analysis(router, _CHAOS_SYSTEM, user_msg)


class ScaleAnalysisTool(Tool):
    """并发海啸测试 — 静态扫描找瓶颈 + LLM 给出具体改造方案."""

    @property
    def name(self) -> str:
        return "analysis_scale"

    @property
    def description(self) -> str:
        return (
            "对代码进行高并发压力分析。"
            "先静态扫描找同步阻塞、缓存缺失、N+1查询、连接池配置等瓶颈，"
            "再由 LLM 计算具体数值并给出改造方案。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "要分析的文件路径或目录路径",
                },
                "qps": {
                    "type": "integer",
                    "description": "目标并发量/QPS，默认 10000",
                    "default": 10000,
                },
                "context": {
                    "type": "string",
                    "description": "补充上下文（系统架构、技术栈等）",
                    "default": "",
                },
            },
            "required": ["target"],
        }

    async def execute(
        self, *, target: str, qps: int = 10000, context: str = "", **kwargs: Any
    ) -> str:
        router = _global_router
        if router is None:
            return _router_unavailable("scale", target)

        files = _resolve_target(target)
        if not files:
            return f"无法解析目标: {target} (请提供文件或目录路径)"

        source_text = _read_sources(files)
        scan_evidence = _scan_scale(files, source_text, qps)

        system = _SCALE_SYSTEM.format(qps=qps)
        user_msg = (
            f"## 静态扫描证据 (目标 QPS: {qps:,})\n{scan_evidence}\n\n"
            f"## 源代码\n{source_text[:50000]}\n"
        )
        if context:
            user_msg += f"\n## 补充上下文\n{context}\n"

        return await _run_analysis(router, system, user_msg)


class StateAuditTool(Tool):
    """状态与分布式审查 — 静态扫描找有状态违规 + LLM 给出分布式方案."""

    @property
    def name(self) -> str:
        return "analysis_state"

    @property
    def description(self) -> str:
        return (
            "审查代码是否符合无状态(Stateless)云原生标准。"
            "先静态扫描找全局变量、内存Session、本地锁、本地文件写入等违规，"
            "计算云原生就绪评分，再由 LLM 给出具体分布式替代方案。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "要审查的文件路径或目录路径",
                },
                "context": {
                    "type": "string",
                    "description": "补充上下文（系统架构、部署方式等）",
                    "default": "",
                },
            },
            "required": ["target"],
        }

    async def execute(self, *, target: str, context: str = "", **kwargs: Any) -> str:
        router = _global_router
        if router is None:
            return _router_unavailable("state", target)

        files = _resolve_target(target)
        if not files:
            return f"无法解析目标: {target} (请提供文件或目录路径)"

        source_text = _read_sources(files)
        scan_evidence = _scan_state(files, source_text)

        user_msg = (
            f"## 静态扫描证据\n{scan_evidence}\n\n"
            f"## 源代码\n{source_text[:50000]}\n"
        )
        if context:
            user_msg += f"\n## 补充上下文\n{context}\n"

        return await _run_analysis(router, _STATE_SYSTEM, user_msg)


class VibeModeTool(Tool):
    """极速构建模式 — 生成可运行 Demo scaffold，可选 LLM 增强."""

    @property
    def name(self) -> str:
        return "analysis_vibe"

    @property
    def description(self) -> str:
        return (
            "极速构建模式：根据需求生成能直接运行的最小 Demo scaffold，"
            "可选写入 output_dir，并在模型可用时追加 LLM 增强建议。"
        )

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            destructive=True,
            path_argument_names=("output_dir",),
            user_facing_name="极速构建 Demo",
            search_hint="rapid prototype scaffold runnable demo files",
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "要构建的功能描述",
                },
                "tech_stack": {
                    "type": "string",
                    "description": "技术栈偏好（如 Python/Flask, Node.js/Express）",
                    "default": "",
                },
                "output_dir": {
                    "type": "string",
                    "description": "可选：将生成的 Demo 文件写入该目录。",
                    "default": "",
                },
            },
            "required": ["description"],
        }

    async def execute(
        self,
        *,
        description: str,
        tech_stack: str = "",
        output_dir: str = "",
        **kwargs: Any,
    ) -> str:
        scaffold = _build_vibe_scaffold(description, tech_stack)
        try:
            written = _write_vibe_scaffold(scaffold, output_dir) if output_dir else []
        except Exception as e:
            return f"Vibe scaffold 写入失败：{type(e).__name__}: {e}"
        deterministic = _format_vibe_scaffold(scaffold, written)

        router = _global_router
        if router is None:
            return deterministic + "\n\n模型路由未初始化，已返回确定性 scaffold。"

        scan = _scan_vibe_request(description, tech_stack, scaffold)
        user_msg = f"## Build This\n{description}\n\n## Deterministic Scaffold\n{scan}\n"
        if tech_stack:
            user_msg += f"\n## Tech Stack\n{tech_stack}\n"

        enhanced = await _run_analysis(router, _VIBE_SYSTEM, user_msg)
        return deterministic + "\n\n## LLM 增强建议\n" + enhanced


@dataclass(frozen=True)
class VibeScaffold:
    kind: str
    run_command: str
    files: tuple[tuple[str, str], ...]


def _build_vibe_scaffold(description: str, tech_stack: str = "") -> VibeScaffold:
    """Build a dependency-light runnable demo scaffold."""
    clean_description = " ".join(description.strip().split()) or "Naumi Demo"
    stack = tech_stack.lower()
    if any(token in stack for token in ("node", "express", "javascript", "js")):
        return _node_vibe_scaffold(clean_description)
    if any(token in stack for token in ("html", "static", "frontend", "web")):
        return _static_vibe_scaffold(clean_description)
    return _python_vibe_scaffold(clean_description)


def _python_vibe_scaffold(description: str) -> VibeScaffold:
    app_py = f'''\
"""Minimal runnable demo generated by analysis_vibe."""

from http.server import BaseHTTPRequestHandler, HTTPServer
from html import escape

DESCRIPTION = {description!r}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = f"""<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>Naumi Vibe Demo</title></head>
<body>
  <main>
    <h1>Naumi Vibe Demo</h1>
    <p>{{escape(DESCRIPTION)}}</p>
    <form method="post">
      <input name="message" placeholder="输入一条消息">
      <button type="submit">提交</button>
    </form>
  </main>
</body>
</html>""".encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.do_GET()


if __name__ == "__main__":
    server = HTTPServer(("127.0.0.1", 8000), Handler)
    print("Demo running at http://127.0.0.1:8000")
    server.serve_forever()
'''
    readme = _vibe_readme(description, "python app.py")
    return VibeScaffold(
        kind="python-stdlib-web",
        run_command="python app.py",
        files=(("app.py", app_py), ("README.md", readme)),
    )


def _node_vibe_scaffold(description: str) -> VibeScaffold:
    server_js = f"""\
const http = require('http');

const description = {description!r};

const server = http.createServer((req, res) => {{
  res.writeHead(200, {{ 'Content-Type': 'text/html; charset=utf-8' }});
  res.end(`<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>Naumi Vibe Demo</title></head>
<body>
  <main>
    <h1>Naumi Vibe Demo</h1>
    <p>${{description.replace(/[&<>]/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;'}}[c]))}}</p>
    <button onclick="alert('Demo action')">执行动作</button>
  </main>
</body>
</html>`);
}});

server.listen(3000, '127.0.0.1', () => {{
  console.log('Demo running at http://127.0.0.1:3000');
}});
"""
    package_json = """\
{"scripts":{"start":"node server.js"},"dependencies":{},"devDependencies":{}}
"""
    readme = _vibe_readme(description, "npm start")
    return VibeScaffold(
        kind="node-stdlib-web",
        run_command="npm start",
        files=(
            ("server.js", server_js),
            ("package.json", package_json),
            ("README.md", readme),
        ),
    )


def _static_vibe_scaffold(description: str) -> VibeScaffold:
    html = f"""\
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Naumi Vibe Demo</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 40px; }}
    main {{ max-width: 760px; }}
    button {{ padding: 8px 12px; }}
  </style>
</head>
<body>
  <main>
    <h1>Naumi Vibe Demo</h1>
    <p>{_escape_html(description)}</p>
    <button id="action">执行动作</button>
    <pre id="log"></pre>
  </main>
  <script>
    document.getElementById('action').addEventListener('click', () => {{
      document.getElementById('log').textContent = 'Demo action completed';
    }});
  </script>
</body>
</html>
"""
    readme = _vibe_readme(description, "python -m http.server 8000")
    return VibeScaffold(
        kind="static-html",
        run_command="python -m http.server 8000",
        files=(("index.html", html), ("README.md", readme)),
    )


def _vibe_readme(description: str, run_command: str) -> str:
    return f"""\
# Naumi Vibe Demo

需求：{description}

运行：

```bash
{run_command}
```
"""


def _write_vibe_scaffold(scaffold: VibeScaffold, output_dir: str) -> list[Path]:
    target = Path(os.path.expanduser(output_dir)).resolve()
    target.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for relative_name, content in scaffold.files:
        path = (target / relative_name).resolve()
        if target not in path.parents and path != target:
            raise ValueError(f"拒绝写入 output_dir 外的路径：{relative_name}")
        path.write_text(content, encoding="utf-8")
        written.append(path)
    return written


def _format_vibe_scaffold(scaffold: VibeScaffold, written: list[Path]) -> str:
    lines = [
        "## Vibe Scaffold",
        f"- 类型：{scaffold.kind}",
        f"- 运行命令：`{scaffold.run_command}`",
        f"- 文件数：{len(scaffold.files)}",
    ]
    if written:
        lines.append("- 已写入：")
        lines.extend(f"  - {path}" for path in written)
    else:
        lines.append("- 未写入文件；如需落盘，请传入 output_dir。")
    lines.append("")
    for name, content in scaffold.files:
        lang = "python" if name.endswith(".py") else "json" if name.endswith(".json") else "html"
        if name.endswith(".md"):
            lang = "markdown"
        lines.append(f"### {name}\n```{lang}\n{content[:4000]}```")
    return "\n".join(lines)


def _scan_vibe_request(
    description: str,
    tech_stack: str,
    scaffold: VibeScaffold,
) -> str:
    return (
        f"- 需求字符数：{len(description)}\n"
        f"- 技术栈偏好：{tech_stack or '未指定'}\n"
        f"- 确定性 scaffold 类型：{scaffold.kind}\n"
        f"- 生成文件：{', '.join(name for name, _ in scaffold.files)}\n"
        f"- 运行命令：{scaffold.run_command}"
    )


def _escape_html(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# ===========================================================================
#  /eval — EDD 评测驱动开发
# ===========================================================================

def _scan_eval(files: list[Path], source_text: str) -> str:
    """eval 模式静态扫描：提取函数签名、条件分支、已有测试覆盖."""
    import ast as _ast

    findings: list[str] = []

    # 1. 用 AST 提取函数签名
    func_sigs: list[str] = []
    class_defs: list[str] = []
    for f in files:
        try:
            tree = _ast.parse(f.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in _ast.walk(tree):
            if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                args = [a.arg for a in node.args.args if a.arg != "self"]
                returns = ""
                if node.returns:
                    returns = _ast.unparse(node.returns)
                func_sigs.append(
                    f"  - {f.name}:{node.lineno} {node.name}"
                    f"({', '.join(args)}) -> {returns}"
                )
            elif isinstance(node, _ast.ClassDef):
                class_defs.append(
                    f"  - {f.name}:{node.lineno} class {node.name}"
                )

    findings.append(f"- 函数定义: {len(func_sigs)} 个")
    for sig in func_sigs[:15]:
        findings.append(sig)
    if len(func_sigs) > 15:
        findings.append(f"  ... 还有 {len(func_sigs) - 15} 个")

    if class_defs:
        findings.append(f"- 类定义: {len(class_defs)} 个")
        for cd in class_defs[:10]:
            findings.append(cd)

    # 2. 统计条件分支数（if/elif）— 每个分支都是一个测试机会
    if_count = len(re.findall(r"\bif\s+|\belif\s+", source_text))
    findings.append(f"- 条件分支 (if/elif): {if_count} 个 (每个分支至少需要 1 个测试)")

    # 3. 统计 raise / raise from — 异常路径
    raises = re.findall(r"raise\s+\w+", source_text)
    if raises:
        findings.append(f"- 异常抛出点: {len(raises)} 个")
        for r in raises[:8]:
            findings.append(f"  - `{r}`")

    # 4. 统计已有测试
    existing_tests = re.findall(r"\bdef\s+test_\w+", source_text)
    if existing_tests:
        findings.append(f"- 已有测试: {len(existing_tests)} 个")
        for t in existing_tests[:8]:
            findings.append(f"  - `{t}`")
    else:
        findings.append("- ⚠️ 未发现任何 test_ 开头的测试函数")

    # 5. 找类型标注覆盖
    annotated_params = len(re.findall(r"def\s+\w+\([^)]*:\s*\w+", source_text))
    total_params = len(re.findall(r"def\s+\w+\([^)]*\)", source_text))
    if total_params > 0:
        pct = annotated_params * 100 // total_params
        findings.append(
            f"- 类型标注覆盖: {annotated_params}/{total_params} 参数 ({pct}%)"
        )

    # 6. 找外部输入点（需要重点测试的地方）
    input_points = re.findall(
        r"(?:request\.\w+|input\(|sys\.argv|os\.environ|json\.loads\()",
        source_text,
    )
    if input_points:
        findings.append(
            f"- 外部输入点: {len(input_points)} 个 (必须用异常输入测试)"
        )

    findings.append(
        f"\n- 预估最低测试数: {max(if_count + len(raises), len(func_sigs))} 个"
    )

    return "\n".join(findings)


_EVAL_SYSTEM = """\
You are a ruthless QA engineer implementing Eval-Driven Development (EDD).

You have REAL static analysis evidence and the actual source code. Your task:

## Task
Generate a COMPLETE, RUNNABLE pytest test file that covers ALL edge cases.

## Rules
1. **Every function** must have at least one test.
2. **Every if/elif branch** must be tested (true AND false paths).
3. **Every raise/exception** must be tested with a `pytest.raises` block.
4. **External inputs** must be tested with: empty, None, wrong type, \
oversized, malicious (SQL injection, path traversal, etc.).
5. Tests must be INDEPENDENT (no shared mutable state between tests).
6. Import the target module correctly. Use proper fixtures if needed.
7. Output ONLY valid Python code — no markdown fences, no explanations \
outside the code.

## Output Format
- First line: `import pytest` and the target import
- Then the test functions
- Add a comment `# EDD: N test cases generated` at the top with the count
"""


class EvalDrivenTool(Tool):
    """EDD 评测驱动开发 — 静态扫描代码结构 + 生成可执行的 pytest 测试."""

    @property
    def name(self) -> str:
        return "analysis_eval"

    @property
    def description(self) -> str:
        return (
            "评测驱动开发(EDD)：分析目标代码的函数签名、条件分支、异常路径、"
            "外部输入点，自动生成覆盖所有边界情况的 pytest 测试代码。"
            "生成的测试可直接运行。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "要生成测试的文件路径或目录路径",
                },
                "context": {
                    "type": "string",
                    "description": "补充上下文（功能描述、业务规则等）",
                    "default": "",
                },
            },
            "required": ["target"],
        }

    async def execute(self, *, target: str, context: str = "", **kwargs: Any) -> str:
        router = _global_router
        if router is None:
            return _router_unavailable("eval", target)

        files = _resolve_target(target)
        if not files:
            return f"无法解析目标: {target} (请提供文件或目录路径)"

        source_text = _read_sources(files)
        scan_evidence = _scan_eval(files, source_text)

        user_msg = (
            f"## 静态扫描证据\n{scan_evidence}\n\n"
            f"## 源代码\n{source_text[:50000]}\n"
        )
        if context:
            user_msg += f"\n## 业务规则/上下文\n{context}\n"

        return await _run_analysis(router, _EVAL_SYSTEM, user_msg)


# ===========================================================================
#  /page — LLM OS 内存分页调度
# ===========================================================================

def _scan_page(source_text: str) -> str:
    """page 模式静态分析：统计当前上下文使用情况."""
    findings: list[str] = []

    total_chars = len(source_text)
    est_tokens = total_chars // 4  # rough estimate for mixed CJK/ASCII
    findings.append(f"- 当前对话估算 Token 数: ~{est_tokens:,}")
    findings.append(f"- 对话字符数: {total_chars:,}")

    # 统计消息角色分布
    user_msgs = len(re.findall(r'"role":\s*"user"', source_text))
    assistant_msgs = len(re.findall(r'"role":\s*"assistant"', source_text))
    system_msgs = len(re.findall(r'"role":\s*"system"', source_text))
    tool_msgs = len(re.findall(r'"role":\s*"tool"', source_text))
    findings.append(
        f"- 消息分布: user={user_msgs}, assistant={assistant_msgs}, "
        f"system={system_msgs}, tool={tool_msgs}"
    )

    return "\n".join(findings)


_PAGE_SYSTEM = """\
You are an LLM OS memory manager implementing virtual memory paging.

## Current Context Analysis
The user has activated the memory paging protocol. Analyze the current \
conversation context and perform the following:

## Your Tasks

### 1. Register Snapshot (200 words max)
Summarize the CORE state of the current conversation:
- What is the main task/topic?
- What decisions have been made?
- What is the current progress?
- What are the pending items?

### 2. page_out() — Identify Evictable Content
List what can be safely removed from context to free up space:
- Already-completed subtasks
- Detailed exploration that led to a conclusion
- Repetitive or redundant exchanges
- Code that has already been applied

### 3. page_in() — Recommendations for Loading
Suggest what should be loaded next:
- Reference documentation needed
- Files that haven't been read yet
- Context from previous sessions that might be relevant

### 4. Memory Pressure Assessment
- Rate current memory pressure: LOW / MEDIUM / HIGH / CRITICAL
- Estimate how many more turns before context becomes a problem
- Recommend whether to compact, summarize, or start a fresh session

Be precise and actionable. The user needs to know EXACTLY what to keep \
and what to discard.
"""


class MemoryPageTool(Tool):
    """LLM OS 内存分页 — 分析上下文压力，建议换入换出策略."""

    @property
    def name(self) -> str:
        return "analysis_page"

    @property
    def description(self) -> str:
        return (
            "LLM OS 内存分页调度：分析当前对话的上下文使用情况，"
            "生成寄存器快照(核心状态摘要)、page_out(可换出内容)、"
            "page_in(需要换入的内容)，评估内存压力等级。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "context_window": {
                    "type": "integer",
                    "description": "模型上下文窗口大小（Token），默认 128000",
                    "default": 128000,
                },
            },
            "required": [],
        }

    async def execute(
        self, *, context_window: int = 128000, **kwargs: Any
    ) -> str:
        router = _global_router
        if router is None:
            return _router_unavailable("page", "memory")

        # 从 router 获取真实的上下文信息
        model = router.resolve_model("capable")
        real_window = router.get_context_window(model)
        window = min(context_window, real_window)

        user_msg = (
            f"## 系统信息\n"
            f"- 模型: {model}\n"
            f"- 上下文窗口: {window:,} tokens\n"
            f"- 请分析当前对话的内存使用情况并给出分页建议。\n"
        )

        return await _run_analysis(router, _PAGE_SYSTEM, user_msg)


# ===========================================================================
#  /heal — 自愈代码修复
# ===========================================================================

def _scan_heal(files: list[Path], source_text: str, error_log: str) -> str:
    """heal 模式静态扫描：定位错误相关的代码区域."""
    findings: list[str] = []

    # 1. 从错误日志提取关键信息
    error_types = re.findall(r"(\w+Error|\w+Exception)", error_log)
    error_types = list(set(error_types))
    if error_types:
        findings.append(f"- 错误类型: {', '.join(error_types)}")

    # 2. 提取错误日志中出现的文件名和行号
    file_refs = re.findall(r'File "([^"]+)", line (\d+)', error_log)
    if file_refs:
        findings.append("- 错误栈追踪到的文件:")
        for filepath, lineno in file_refs[:10]:
            findings.append(f"  - {filepath}:{lineno}")

    # 3. 统计相关文件中的 try/except
    try_count = len(re.findall(r"\btry\s*:", source_text))
    except_count = len(re.findall(r"\bexcept\s+", source_text))
    findings.append(
        f"- 错误处理: {try_count} 个 try 块, {except_count} 个 except 子句"
    )

    # 4. 找 bare except
    bare = re.findall(r"except\s*:", source_text)
    if bare:
        findings.append(f"- ⚠️ 裸 except: {len(bare)} 处 (会吞掉所有异常)")

    # 5. 找 pass in except (静默忽略异常)
    silent_catch = re.findall(r"except[^:]*:\s*\n\s*pass", source_text)
    if silent_catch:
        findings.append(
            f"- ⚠️ 静默捕获 (except...pass): {len(silent_catch)} 处"
        )

    # 6. 找日志记录模式
    logging_used = len(re.findall(r"(?:logger\.|logging\.|log\.)", source_text))
    if logging_used:
        findings.append(f"- 日志记录: {logging_used} 处引用")
    else:
        findings.append("- ⚠️ 未发现日志记录 (logger/logging)")

    findings.append(f"- 扫描文件: {len(files)} 个")

    return "\n".join(findings) if findings else "- 静态扫描未发现额外线索"


_HEAL_SYSTEM = """\
You are an immune cell in a self-healing code system.

You have:
1. A bug report / error log from the user
2. REAL static analysis evidence about error handling patterns
3. The actual source code

## Your Tasks

### 1. Diagnosis
- Identify the ROOT CAUSE (not just the symptom)
- Map the failure chain: what called what, where it broke
- Classify: logic bug / missing validation / race condition / \
external dependency failure / resource leak

### 2. Hotfix Code
- Provide the MINIMAL surgical fix (not a rewrite)
- The fix must be a drop-in replacement — show exact old_text → new_text
- Include defensive guards to prevent this class of bug from recurring

### 3. Immune Boost (Defensive Programming)
- Add validation at the boundary where bad data entered
- Add logging that would make this bug instantly diagnosable next time
- Add a regression test that would have caught this bug

### 4. Prevention Checklist
- What monitoring alert should be set up?
- What would a canary check look like?

Be surgical. The fix should change as few lines as possible while \
being bulletproof.
"""


class SelfHealTool(Tool):
    """自愈代码修复 — 分析错误日志 + 扫描代码防御模式 + 生成热修复代码."""

    @property
    def name(self) -> str:
        return "analysis_heal"

    @property
    def description(self) -> str:
        return (
            "自愈代码修复：分析错误日志，定位根因，生成最小化的热修复代码，"
            "并加入防御性编程逻辑防止同类错误再次发生。"
            "需要提供错误日志和对应的代码路径。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "error_log": {
                    "type": "string",
                    "description": "错误日志、异常堆栈或 Bug 描述",
                },
                "target": {
                    "type": "string",
                    "description": "相关代码的文件路径或目录路径",
                    "default": "",
                },
                "context": {
                    "type": "string",
                    "description": "补充上下文（发生场景、预期行为等）",
                    "default": "",
                },
            },
            "required": ["error_log"],
        }

    async def execute(
        self,
        *,
        error_log: str,
        target: str = "",
        context: str = "",
        **kwargs: Any,
    ) -> str:
        router = _global_router
        if router is None:
            return _router_unavailable("heal", error_log[:100])

        # 尝试从错误日志提取文件路径
        scan_evidence = ""
        files: list[Path] = []
        if target:
            files = _resolve_target(target)
        if not files:
            # 从错误栈中提取文件路径
            stack_paths = re.findall(r'File "([^"]+)"', error_log)
            for sp in stack_paths:
                p = Path(sp)
                if p.exists() and p.suffix in _SOURCE_EXTENSIONS:
                    files.append(p)

        source_text = ""
        if files:
            source_text = _read_sources(files)
            scan_evidence = _scan_heal(files, source_text, error_log)

        user_msg = f"## 错误日志\n```\n{error_log}\n```\n"
        if scan_evidence:
            user_msg += f"\n## 静态扫描证据\n{scan_evidence}\n"
        if source_text:
            user_msg += f"\n## 相关源代码\n{source_text[:40000]}\n"
        if context:
            user_msg += f"\n## 补充上下文\n{context}\n"

        return await _run_analysis(router, _HEAL_SYSTEM, user_msg)


# ===========================================================================
#  /dspy — 声明式 Prompt 编译优化 (DSPy-inspired)
# ===========================================================================

# Prompt 模板的正则特征
_PROMPT_SIGNATURES = [
    # triple-quoted strings used as prompts (contain "You are" / "## " etc.)
    r'""".*?(?:You are|你是一个?|##\s|Instructions?|任务|Role).+?"""',
    # f-string prompts with role markers
    r'f".*?(?:system|user|assistant).*?"',
    # explicit prompt variables
    r'(?:PROMPT|prompt|SYSTEM|system_msg|template)\s*[:=]',
]

# Few-shot example patterns
_FEW_SHOT_PATTERNS = [
    r'(?:example|few.?shot|demonstration)\s*[:=]',
    r'#\s*(?:Example|示例|样例)\s*\d',
    r'(?:Input|输入|Question)\s*[:：].*?\n.*?(?:Output|输出|Answer)\s*[:：]',
]

# Metric / evaluation patterns
_METRIC_PATTERNS = [
    r'(?:metric|score|evaluate|评估|评分|accuracy|f1|precision|recall)\s*[:=(]',
    r'def\s+(?:evaluate|score|metric|assess|judge)',
    r'(?:assert|check|verify)\s+.*?(?:output|result|response)',
]


def _scan_dspy(
    files: list[Path], source_text: str, prompt_target: str,
) -> str:
    """dspy 模式静态扫描：分析 prompt 工程成熟度."""
    findings: list[str] = []

    # 1. 扫描 Prompt 模板
    prompt_locs: list[str] = []
    for pattern in _PROMPT_SIGNATURES:
        matches = list(re.finditer(pattern, source_text, re.DOTALL | re.IGNORECASE))
        for m in matches:
            # 取匹配位置前后各 30 字符作为上下文
            start = max(0, m.start() - 30)
            ctx = source_text[start:m.end()].replace("\n", " ")[:80]
            prompt_locs.append(ctx)

    findings.append(f"- 发现 Prompt 模板: {len(prompt_locs)} 处")
    for loc in prompt_locs[:8]:
        truncated = loc if len(loc) <= 78 else loc[:75] + "..."
        findings.append(f"  - `{truncated}`")

    # 2. 扫描 Few-shot 示例
    few_shot_count = 0
    few_shot_locs: list[str] = []
    for pattern in _FEW_SHOT_PATTERNS:
        matches = list(re.finditer(pattern, source_text, re.IGNORECASE))
        few_shot_count += len(matches)
        for m in matches[:4]:
            line_start = source_text.rfind("\n", 0, m.start()) + 1
            line_end = source_text.find("\n", m.end())
            line = source_text[line_start:line_end].strip()[:80]
            few_shot_locs.append(line)
    if few_shot_count:
        findings.append(f"- Few-shot 示例: {few_shot_count} 处")
        for loc in few_shot_locs[:5]:
            findings.append(f"  - `{loc}`")
    else:
        findings.append("- ⚠️ 未发现 Few-shot 示例（强烈建议添加）")

    # 3. 扫描评估函数/Metric
    metric_count = 0
    metric_locs: list[str] = []
    for pattern in _METRIC_PATTERNS:
        matches = list(re.finditer(pattern, source_text, re.IGNORECASE))
        metric_count += len(matches)
        for m in matches[:4]:
            line_start = source_text.rfind("\n", 0, m.start()) + 1
            line_end = source_text.find("\n", m.end())
            line = source_text[line_start:line_end].strip()[:80]
            metric_locs.append(line)
    if metric_count:
        findings.append(f"- 评估函数/Metric: {metric_count} 处")
        for loc in metric_locs[:5]:
            findings.append(f"  - `{loc}`")
    else:
        findings.append("- ⚠️ 未发现评估函数/Metric（这是 DSPy 的核心！）")

    # 4. 分析 Prompt 长度分布（过短或过长都不好）
    prompt_lengths: list[int] = []
    for m in re.finditer(r'""".+?"""', source_text, re.DOTALL):
        prompt_lengths.append(m.end() - m.start())
    for m in re.finditer(r"'[^']{50,}'", source_text):
        prompt_lengths.append(m.end() - m.start())
    if prompt_lengths:
        avg_len = sum(prompt_lengths) // len(prompt_lengths)
        max_len = max(prompt_lengths)
        findings.append(
            f"- Prompt 长度分布: 平均 {avg_len} 字符, "
            f"最长 {max_len} 字符 ({len(prompt_lengths)} 个)"
        )

    # 5. 检测 Prompt 是否可配置（vs 硬编码）
    hardcoded = len(re.findall(
        r'(?:SYSTEM_PROMPT|system_prompt)\s*=\s*(?:f?["\'])', source_text,
    ))
    configurable = len(re.findall(
        r'(?:prompt|template|system_msg)\s*=\s*(?:config|settings|load|read|yaml)',
        source_text,
    ))
    findings.append(
        f"- Prompt 管理: {hardcoded} 个硬编码, "
        f"{configurable} 个可配置"
    )

    # 6. DSPy 成熟度评分
    score = 0
    if prompt_locs:
        score += 20
    if few_shot_count > 0:
        score += 25
    if metric_count > 0:
        score += 30
    if configurable > 0:
        score += 15
    if prompt_lengths:
        score += 10
    findings.append(
        f"\n- DSPy 工程成熟度: {score}/100 "
        f"({'优秀' if score >= 80 else '及格' if score >= 50 else '需要改进'})"
    )
    if prompt_target:
        findings.append(f"- 优化目标: {prompt_target}")

    return "\n".join(findings)


_DSPY_SYSTEM = """\
You are a Prompt Compiler implementing the DSPy (Declaration-based \
Self-evolving Programming) paradigm.

You have REAL static analysis evidence about prompt engineering maturity \
in the codebase. Your task:

## Core Principle
**STOP manually tweaking prompts.** Prompt optimization must be driven by:
1. **Metric** — A measurable evaluation function (not "feels better")
2. **Data** — Ground-truth input/output examples (few-shot samples)
3. **Compiler** — An automated optimizer that searches the prompt space

## Your Tasks

### 1. Current State Assessment
Based on the scan evidence, assess the prompt engineering maturity:
- How many prompts exist? Are they hardcoded or configurable?
- Are there few-shot examples? If not, what examples should be added?
- Are there evaluation metrics? If not, what metrics should be defined?

### 2. Metric Definition
For the target prompt/task, define a concrete evaluation function:
- Input validation: does the output have correct format?
- Quality score: semantic accuracy, relevance, completeness
- Edge case detection: does it handle empty/malformed inputs?
- Provide actual Python code for the metric function

### 3. Few-shot Example Design
Provide 3-5 high-quality input/output pairs that:
- Cover the main use case
- Cover edge cases (empty, ambiguous, adversarial)
- Are unambiguous (a human annotator would agree on the expected output)

### 4. Optimization Plan
Describe the DSPy compilation loop:
- What prompt variants to test (instruction, prefix, suffix)
- What scoring strategy to use (majority vote, weighted, best-of-N)
- How many iterations to run
- When to stop (convergence criteria)

### 5. Anti-pattern Warnings
Flag any of these prompt anti-patterns found in the code:
- "You are a world-class expert" (flattery — brittle across models)
- "Think step by step" (hack — should be structural, not linguistic)
- Overly long system prompts (>2000 chars — context pollution)
- No error handling in output parsing
- Prompt mixing concerns (one prompt doing 3 unrelated things)

Output actionable, compilable recommendations. No hand-waving.
"""


class DSPyTool(Tool):
    """DSPy 声明式 Prompt 编译优化 — 分析 prompt 工程成熟度并给出优化方案."""

    @property
    def name(self) -> str:
        return "analysis_dspy"

    @property
    def description(self) -> str:
        return (
            "DSPy 声明式 Prompt 编译优化：扫描代码中的 Prompt 模板、"
            "Few-shot 示例、评估函数，计算 Prompt 工程成熟度评分，"
            "并生成可执行的评价函数和优化方案。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "要优化的 Prompt 所在的文件或目录路径",
                    "default": "",
                },
                "prompt_target": {
                    "type": "string",
                    "description": "具体想优化的 Prompt 功能描述",
                    "default": "",
                },
            },
            "required": [],
        }

    async def execute(
        self,
        *,
        target: str = "",
        prompt_target: str = "",
        **kwargs: Any,
    ) -> str:
        router = _global_router
        if router is None:
            return _router_unavailable("dspy", prompt_target or target)

        # 默认扫描当前项目
        if not target:
            target = str(Path.cwd())
        files = _resolve_target(target)
        if not files:
            return f"无法解析目标: {target} (请提供文件或目录路径)"

        source_text = _read_sources(files)
        scan_evidence = _scan_dspy(files, source_text, prompt_target)

        user_msg = (
            f"## 静态扫描证据\n{scan_evidence}\n\n"
            f"## 源代码\n{source_text[:50000]}\n"
        )
        if prompt_target:
            user_msg += (
                f"\n## 优化目标\n用户想要优化这个 Prompt 的效果: "
                f"{prompt_target}\n"
            )

        return await _run_analysis(router, _DSPY_SYSTEM, user_msg)


# ===========================================================================
#  /graph — GraphRAG 升维图谱推演
# ===========================================================================


def _scan_graph(files: list[Path], source_text: str) -> str:
    """graph 模式静态扫描：从源码构建实体-关系图并计算图指标."""
    import ast as _ast
    import collections

    findings: list[str] = []

    nodes: dict[str, set[str]] = collections.defaultdict(set)  # type -> names
    edges: list[tuple[str, str, str]] = []  # (src, dst, rel_type)

    for f in files:
        module_name = f.stem
        try:
            tree = _ast.parse(f.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue

        # 1. Extract class nodes + inheritance edges
        for node in _ast.walk(tree):
            if isinstance(node, _ast.ClassDef):
                nodes["class"].add(f"{module_name}:{node.name}")
                for base in node.bases:
                    base_name = ""
                    if isinstance(base, _ast.Name):
                        base_name = base.id
                    elif isinstance(base, _ast.Attribute):
                        base_name = _ast.unparse(base)
                    if base_name:
                        edges.append((
                            f"{module_name}:{node.name}",
                            base_name,
                            "inherits",
                        ))

        # 2. Extract function/method nodes
        for node in _ast.walk(tree):
            if isinstance(node, _ast.FunctionDef) or isinstance(
                node, _ast.AsyncFunctionDef
            ):
                if isinstance(
                    node, (_ast.FunctionDef, _ast.AsyncFunctionDef)
                ) and hasattr(node, "parent"):
                    parent = getattr(node, "parent", None)
                    label = (
                        f"{module_name}:{parent}.{node.name}"
                        if parent
                        else f"{module_name}:{node.name}"
                    )
                else:
                    label = f"{module_name}:{node.name}"
                nodes["function"].add(label)

        # 3. Extract import edges
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Import):
                for alias in node.names:
                    edges.append((module_name, alias.name, "imports"))
            elif isinstance(node, _ast.ImportFrom):
                if node.module:
                    edges.append((module_name, node.module, "imports"))

    # 4. Build adjacency list and detect cycles
    adj: dict[str, set[str]] = collections.defaultdict(set)
    for src, dst, rel in edges:
        adj[src].add(dst)
        if rel == "imports":
            adj[dst]  # ensure dst exists

    # Detect cycles with DFS
    cycles: list[list[str]] = []
    visited: set[str] = set()
    in_stack: set[str] = set()

    def dfs_cycle(node: str, path: list[str]) -> None:
        visited.add(node)
        in_stack.add(node)
        path.append(node)
        for neighbor in adj.get(node, set()):
            if neighbor in in_stack:
                cycle_start = path.index(neighbor)
                cycles.append(path[cycle_start:] + [neighbor])
            elif neighbor not in visited:
                dfs_cycle(neighbor, path)
        path.pop()
        in_stack.discard(node)

    for n in list(adj.keys()):
        if n not in visited:
            dfs_cycle(n, [])

    # 5. Compute centrality (degree-based)
    degree: dict[str, int] = collections.defaultdict(int)
    for src, dst, _rel in edges:
        degree[src] += 1
        degree[dst] += 1

    # 6. Find connected components
    component_map: dict[str, int] = {}
    comp_id = 0
    all_nodes_set: set[str] = set(adj.keys())
    for src, dst, _rel in edges:
        all_nodes_set.add(src)
        all_nodes_set.add(dst)

    unvisited = set(all_nodes_set)
    components: list[set[str]] = []
    while unvisited:
        comp_id += 1
        component: set[str] = set()
        queue = [unvisited.pop()]
        while queue:
            curr = queue.pop()
            component.add(curr)
            component_map[curr] = comp_id
            for neighbor in adj.get(curr, set()):
                if neighbor in unvisited:
                    unvisited.discard(neighbor)
                    queue.append(neighbor)
            # Also check reverse edges
            for n in all_nodes_set:
                if n in unvisited and curr in adj.get(n, set()):
                    unvisited.discard(n)
                    queue.append(n)
        components.append(component)

    # Output findings
    findings.append(f"- 实体节点: {sum(len(v) for v in nodes.values())} 个")
    for ntype, names in nodes.items():
        findings.append(f"  - {ntype}: {len(names)} 个")
        for name in sorted(names)[:6]:
            findings.append(f"    - {name}")
        if len(names) > 6:
            findings.append(f"    ... 还有 {len(names) - 6} 个")

    findings.append(f"- 关系边: {len(edges)} 条")
    edge_types: dict[str, int] = collections.defaultdict(int)
    for _src, _dst, rel in edges:
        edge_types[rel] += 1
    for rel, count in sorted(edge_types.items()):
        findings.append(f"  - {rel}: {count} 条")

    if cycles:
        findings.append(f"- ⚠️ 循环依赖: {len(cycles)} 个")
        for cycle in cycles[:5]:
            cycle_str = " → ".join(cycle[:6])
            if len(cycle) > 6:
                cycle_str += " → ..."
            findings.append(f"  - {cycle_str}")
    else:
        findings.append("- ✅ 无循环依赖")

    findings.append(f"- 连通分量: {len(components)} 个")
    for i, comp in enumerate(components[:5]):
        if len(comp) <= 4:
            findings.append(f"  - 分量 {i+1}: {', '.join(sorted(comp))}")
        else:
            findings.append(
                f"  - 分量 {i+1}: {len(comp)} 个节点 "
                f"({', '.join(sorted(comp)[:3])}...)"
            )

    top_degree = sorted(degree.items(), key=lambda x: x[1], reverse=True)[:5]
    if top_degree:
        findings.append("- 核心节点（度中心性 Top 5）:")
        for name, deg in top_degree:
            findings.append(f"  - {name}: degree={deg}")

    return "\n".join(findings)


_GRAPH_SYSTEM = """\
You are a GraphRAG (Graph-based Retrieval Augmented Generation) analyst.

You have REAL graph analysis data extracted from the codebase. Your task:

## Core Principle
Abandon linear text output. Model the problem as a **topology graph** \
with entity nodes and relationship edges. Think in terms of connectivity, \
centrality, and influence propagation.

## Your Tasks

### 1. Entity-Relationship Map
Based on the scan evidence, present the discovered topology:
- What are the core entities (classes, modules, functions)?
- What are the key relationships (imports, inheritance, calls)?
- Where are the hub nodes (high degree centrality)?

### 2. Structural Analysis
- **Bottleneck nodes**: Single points with many dependencies
- **Orphan nodes**: Entities with no connections (dead code risk)
- **Tight clusters**: Groups of highly interconnected entities (coupling risk)
- **Bridge nodes**: Entities that connect otherwise separate clusters

### 3. Risk Propagation Simulation
If node X fails, trace the blast radius through the graph:
- Which nodes are directly affected?
- Which clusters are disconnected?
- What is the cascading failure chain?

### 4. Optimization Recommendations
Based on the graph topology:
- Where to decouple (break cycles, reduce coupling)
- Where to consolidate (merge tightly coupled clusters)
- Where to add redundancy (single points of failure)

### 5. Visual Description
Describe the graph in a way that allows visualization:
- List the top 10 most important nodes with their connections
- Describe the overall shape (star, mesh, tree, layered)
- Identify the "backbone" path through the system

Be precise with graph theory terminology. Show adjacency, not just lists.
"""


class GraphRAGTool(Tool):
    """GraphRAG 升维图谱推演 — 从源码提取实体关系图并进行拓扑分析."""

    @property
    def name(self) -> str:
        return "analysis_graph"

    @property
    def description(self) -> str:
        return (
            "GraphRAG 升维图谱推演：从源码中提取实体(类/函数/模块)作为节点、"
            "关系(导入/继承/调用)作为边，构建知识图谱，"
            "计算中心度/连通分量/循环依赖等图指标，"
            "推演风险传导路径并给出架构优化建议。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "要分析的文件路径或目录路径",
                    "default": "",
                },
            },
            "required": [],
        }

    async def execute(
        self, *, target: str = "", **kwargs: Any,
    ) -> str:
        router = _global_router
        if router is None:
            return _router_unavailable("graph", target)

        if not target:
            target = str(Path.cwd())
        files = _resolve_target(target)
        if not files:
            return f"无法解析目标: {target} (请提供文件或目录路径)"

        source_text = _read_sources(files)
        scan_evidence = _scan_graph(files, source_text)

        user_msg = (
            f"## 图谱扫描证据\n{scan_evidence}\n\n"
            f"## 源代码\n{source_text[:50000]}\n"
        )

        return await _run_analysis(router, _GRAPH_SYSTEM, user_msg)


# ===========================================================================
#  /mcts — 蒙特卡洛树搜索慢思考机制
# ===========================================================================


def _scan_mcts(
    files: list[Path], source_text: str, problem: str,
) -> str:
    """mcts 模式静态扫描：分析决策空间复杂度和约束条件."""
    findings: list[str] = []

    # 1. Count decision points in code (search space indicators)
    if_count = len(re.findall(r"\bif\s+", source_text))
    elif_count = len(re.findall(r"\belif\s+", source_text))
    match_cases = len(re.findall(r"\bcase\s+", source_text))
    ternary = len(re.findall(r"\bif\s+.+\s+else\s+", source_text))
    total_branches = if_count + elif_count + match_cases + ternary
    findings.append(
        f"- 决策分支点: {total_branches} 个 "
        f"(if={if_count}, elif={elif_count}, "
        f"match={match_cases}, ternary={ternary})"
    )

    # 2. Estimate search space (exponential in branches)
    import math

    if total_branches > 0:
        est_paths = min(2**total_branches, 10**15)
        if est_paths >= 10**9:
            space_str = f"~10^{int(math.log10(est_paths))} 条路径"
        else:
            space_str = f"{est_paths:,} 条路径"
        findings.append(
            f"- 估算搜索空间: {space_str} "
            f"（需要剪枝策略而非穷举）"
        )

    # 3. Identify constraints (assertions, validations, type checks)
    assertions = len(re.findall(r"\bassert\s+", source_text))
    validations = len(re.findall(
        r"(?:validate|check|verify|ensure|guard)\s*\(", source_text,
    ))
    type_checks = len(re.findall(
        r"isinstance\s*\(", source_text,
    ))
    findings.append(
        f"- 约束条件: {assertions} assertions, "
        f"{validations} validations, "
        f"{type_checks} type checks"
    )

    # 4. Count error paths (each is a branch that needs coverage)
    raises = re.findall(r"raise\s+(\w+)", source_text)
    unique_raises = set(raises)
    if unique_raises:
        findings.append(
            f"- 异常路径: {len(raises)} 个 raise "
            f"({len(unique_raises)} 种类型)"
        )

    # 5. Detect existing testing/verification infrastructure
    test_functions = re.findall(r"\bdef\s+(test_\w+)", source_text)
    if test_functions:
        findings.append(
            f"- 已有验证机制: {len(test_functions)} 个测试函数"
        )
    else:
        findings.append("- ⚠️ 无测试覆盖（建议添加回归测试）")

    # 6. Identify async/concurrency patterns (complexity multiplier)
    async_funcs = len(re.findall(r"\basync\s+def\s+", source_text))
    locks = len(re.findall(
        r"(?:Lock|Semaphore|Event|Condition|Barrier)\s*\(", source_text,
    ))
    concurrency_score = async_funcs * 2 + locks * 5
    if concurrency_score > 0:
        findings.append(
            f"- 并发复杂度: {concurrency_score} 分 "
            f"(async={async_funcs}, locks={locks})"
        )

    # 7. Extract external dependencies (risk factors)
    imports = re.findall(r"^import\s+(\S+)|^from\s+(\S+)", source_text, re.MULTILINE)
    flat_imports = [imp for pair in imports for imp in pair if imp]
    if flat_imports:
        unique_imports = set(flat_imports)
        findings.append(
            f"- 外部依赖: {len(unique_imports)} 个 "
            f"(每个依赖都是潜在风险)"
        )

    # 8. Complexity assessment
    complexity_score = (
        total_branches * 2
        + len(unique_raises) * 3
        + concurrency_score
        + len(unique_imports)
        - len(test_functions) * 5
    )
    complexity_score = max(0, complexity_score)
    level = (
        "CRITICAL" if complexity_score > 100
        else "HIGH" if complexity_score > 50
        else "MEDIUM" if complexity_score > 20
        else "LOW"
    )
    findings.append(
        f"\n- 决策复杂度: {complexity_score} ({level}) "
        f"— {'需要 MCTS 多路径探索' if level in ('HIGH', 'CRITICAL') else '简单决策树即可'}"
    )
    if problem:
        findings.append(f"- 待解决问题: {problem[:200]}")

    return "\n".join(findings)


_MCTS_SYSTEM = """\
You are a Monte Carlo Tree Search (MCTS) decision engine implementing \
Test-Time Compute scaling (System 2 "slow thinking").

You have REAL complexity analysis data. Your task:

## Core Principle
**DO NOT immediately output the first answer that comes to mind.** \
Instead, explicitly explore multiple solution paths, evaluate each, \
and only output the verified best path.

## Mandatory Output Structure

### Path A: [Descriptive Name]
- **Approach**: How this path solves the problem
- **Estimated effort**: Lines of code / time / complexity
- **Pros**: What makes this path attractive
- **Cons**: What could go wrong
- **Disaster simulation**: What happens if this path fails?
  - Which edge cases would break it?
  - What are the failure modes?
  - Score: X/10 confidence

### Path B: [Descriptive Name]
(same structure as Path A)

### Path C: [Descriptive Name] (if applicable)
(same structure)

### Pruning Decision
- Path A score: X/10 → KEEP / PRUNE (reason)
- Path B score: X/10 → KEEP / PRUNE (reason)
- Path C score: X/10 → KEEP / PRUNE (reason)

### Winning Path: [Selected Path Name]
- **Why this path wins**: Clear justification
- **Implementation plan**: Step-by-step
- **Validation**: How to verify correctness
- **Backtracking trigger**: Under what conditions to abandon this path

### Regression Guard
- What test would catch if this solution breaks in the future?
- What monitoring would detect degradation?

## Rules
1. You MUST generate at least 2 distinct paths (3 recommended)
2. Each path must be genuinely different (not just renaming variables)
3. Disaster simulation must identify at least 2 real failure modes
4. The winning path must have explicit backtracking criteria
5. If all paths score below 5/10, say so and explain why the problem \
needs human intervention
"""


class MCTSTool(Tool):
    """MCTS 蒙特卡洛树搜索 — 多路径探索决策引擎."""

    @property
    def name(self) -> str:
        return "analysis_mcts"

    @property
    def description(self) -> str:
        return (
            "蒙特卡洛树搜索(MCTS)慢思考机制：对待解决问题进行多路径探索，"
            "生成至少3条截然不同的解决方案，对每条路径进行灾难推演（自我博弈），"
            "主动剪掉错误树枝，只输出经过验证的最佳路径。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "problem": {
                    "type": "string",
                    "description": "待解决的问题描述（算法题、架构决策、策略选择等）",
                },
                "target": {
                    "type": "string",
                    "description": "相关代码的文件路径或目录路径（可选）",
                    "default": "",
                },
            },
            "required": ["problem"],
        }

    async def execute(
        self,
        *,
        problem: str,
        target: str = "",
        **kwargs: Any,
    ) -> str:
        router = _global_router
        if router is None:
            return _router_unavailable("mcts", problem[:200])

        scan_evidence = ""
        source_text = ""
        if target:
            files = _resolve_target(target)
            if files:
                source_text = _read_sources(files)
                scan_evidence = _scan_mcts(files, source_text, problem)

        user_msg = f"## 待解决问题\n{problem}\n"
        if scan_evidence:
            user_msg += f"\n## 复杂度扫描证据\n{scan_evidence}\n"
        if source_text:
            user_msg += f"\n## 相关源代码\n{source_text[:40000]}\n"

        return await _run_analysis(router, _MCTS_SYSTEM, user_msg)


# ===========================================================================
#  /route — MoE 混合专家调度
# ===========================================================================

# Domain keywords for expert identification
_DOMAIN_KEYWORDS: dict[str, list[str]] = {
    "backend": [
        "api", "server", "database", "sql", "orm", "redis", "cache",
        "queue", "kafka", "grpc", "rest", "endpoint", "migration",
    ],
    "frontend": [
        "ui", "component", "css", "html", "react", "vue", "dom",
        "render", "style", "layout", "responsive", "animation",
    ],
    "infra": [
        "docker", "k8s", "kubernetes", "ci/cd", "terraform", "deploy",
        "nginx", "load.balance", "monitoring", "prometheus", "grafana",
    ],
    "security": [
        "auth", "jwt", "oauth", "encrypt", "decrypt", "ssl", "tls",
        "vulnerability", "xss", "csrf", "sql.inject", "firewall",
    ],
    "data": [
        "etl", "pipeline", "spark", "hadoop", "warehouse", "lake",
        "analytics", "metric", "dashboard", "visualization", "pandas",
    ],
    "ml": [
        "model", "training", "inference", "neural", "transformer",
        "embedding", "vector", "fine.tun", "prompt", "llm", "rag",
    ],
    "finance": [
        "stock", "portfolio", "alpha", "beta", "sharpe", "volatility",
        "option", "futures", "yield", "bond", "quantitative", "backtest",
    ],
    "architecture": [
        "microservice", "monolith", "event.driven", "cqrs", "ddd",
        "clean.arch", "hexagonal", "soa", "design.pattern", "solid",
    ],
}


def _scan_route(
    files: list[Path], source_text: str, task: str,
) -> str:
    """route 模式静态扫描：分析任务涉及的领域维度和专家画像."""
    findings: list[str] = []
    task_lower = task.lower()
    source_lower = source_text.lower()

    # 1. Identify domains from the task description
    task_domains: dict[str, list[str]] = {}
    for domain, keywords in _DOMAIN_KEYWORDS.items():
        matched = [kw for kw in keywords if kw in task_lower]
        if matched:
            task_domains[domain] = matched

    if task_domains:
        findings.append("- 任务涉及领域:")
        for domain, keywords in task_domains.items():
            findings.append(
                f"  - {domain}: {', '.join(keywords)}"
            )
    else:
        findings.append(
            "- 任务领域: 未匹配到明确领域关键词（将由 LLM 判断）"
        )

    # 2. Identify domains from the codebase
    code_domains: dict[str, int] = {}
    for domain, keywords in _DOMAIN_KEYWORDS.items():
        count = sum(source_lower.count(kw) for kw in keywords)
        if count > 0:
            code_domains[domain] = count

    if code_domains:
        findings.append("- 代码库领域分布:")
        for domain, count in sorted(
            code_domains.items(), key=lambda x: x[1], reverse=True,
        ):
            findings.append(f"  - {domain}: {count} 次引用")

    # 3. Analyze code structure for routing hints
    class_count = len(re.findall(r"\bclass\s+\w+", source_text))
    func_count = len(re.findall(r"\bdef\s+\w+", source_text))
    async_count = len(re.findall(r"\basync\s+def\s+", source_text))
    findings.append(
        f"- 代码规模: {class_count} 个类, "
        f"{func_count} 个函数 ({async_count} 个异步)"
    )

    # 4. Detect existing modular structure (suggests MoE readiness)
    modules = set()
    for f in files:
        parts = f.parts
        if "src" in parts:
            idx = parts.index("src")
            if idx + 1 < len(parts):
                modules.add(parts[idx + 1])
    if modules:
        findings.append(
            f"- 模块划分: {len(modules)} 个 "
            f"({', '.join(sorted(modules)[:8])})"
        )

    # 5. Identify cross-cutting concerns (need multiple experts)
    cross_cutting: list[str] = []
    if "security" in task_domains and "backend" in task_domains:
        cross_cutting.append("安全 + 后端: 需要安全专家审查 API 设计")
    if "data" in task_domains and "ml" in task_domains:
        cross_cutting.append("数据 + ML: 需要数据工程师和 ML 工程师协作")
    if "finance" in task_domains and "data" in task_domains:
        cross_cutting.append("金融 + 数据: 需要量化分析师和数据工程师")
    if "frontend" in task_domains and "backend" in task_domains:
        cross_cutting.append("前端 + 后端: 需要全栈协调")
    if "infra" in task_domains and "security" in task_domains:
        cross_cutting.append("基础设施 + 安全: 需要运维安全专家")
    if cross_cutting:
        findings.append("- 跨领域协作点:")
        for cc in cross_cutting:
            findings.append(f"  - {cc}")

    # 6. Recommend expert panel
    all_domains = set(task_domains.keys()) | set(code_domains.keys())
    if all_domains:
        findings.append(
            f"\n- 推荐专家小组: {len(all_domains)} 位专家"
        )
        for domain in sorted(all_domains):
            findings.append(f"  - 🧑‍💻 {domain} 专家")

    return "\n".join(findings)


_ROUTE_SYSTEM = """\
You are a Mixture-of-Experts (MoE) orchestrator with semantic routing.

## Core Principle
DO NOT answer complex problems from a single perspective. Instead:
1. **Decompose** the problem into domain-specific sub-problems
2. **Instantiate** 3-5 specialized virtual experts
3. **Distribute** sub-problems to each expert for independent analysis
4. **Synthesize** their outputs into a unified, multi-dimensional solution

## Your Tasks

### 1. Expert Panel Formation
Based on the scan evidence and the task, declare your expert team:
- Each expert must have a specific domain, NOT a generic title
- Each expert must have a clear analytical lens (what they focus on)
- Minimum 3 experts, maximum 5

### 2. Individual Expert Analysis
For EACH expert, provide their independent analysis:
- **Expert Name & Domain**
- **Their Perspective**: What this expert sees as the key issues
- **Their Recommendations**: Specific, actionable advice
- **Their Concerns**: What could go wrong from their domain
- **Confidence**: X/10

### 3. Cross-Expert Conflict Resolution
If experts disagree:
- Identify the conflict explicitly
- Present both sides
- Make a ruling with justification
- If uncertain, propose an experiment to resolve it

### 4. Synthesized Solution
Combine all expert outputs into a single actionable plan:
- Priority-ordered action items
- Each item tagged with the responsible expert domain
- Dependencies between items
- Risk assessment for the overall plan

### 5. Resource Estimation
- Estimated complexity (S/M/L/XL)
- Recommended team size and skill requirements
- Suggested phasing (what to do first, what to defer)

Be thorough. Each expert's analysis should be substantive, not perfunctory.
"""


class MoERouteTool(Tool):
    """MoE 混合专家调度 — 将复杂任务分发给虚拟专家小组并汇总."""

    @property
    def name(self) -> str:
        return "analysis_route"

    @property
    def description(self) -> str:
        return (
            "MoE 混合专家调度：面对复杂跨学科任务时，实例化 3-5 个垂直领域"
            "虚拟专家，将问题拆解分发给各专家独立分析，"
            "最后汇总为多维度统一方案。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "要分析的任务描述",
                },
                "target": {
                    "type": "string",
                    "description": "相关代码路径（可选）",
                    "default": "",
                },
            },
            "required": ["task"],
        }

    async def execute(
        self,
        *,
        task: str,
        target: str = "",
        **kwargs: Any,
    ) -> str:
        router = _global_router
        if router is None:
            return _router_unavailable("route", task[:200])

        source_text = ""
        files: list[Path] = []
        if target:
            files = _resolve_target(target)
            if files:
                source_text = _read_sources(files)

        scan_evidence = _scan_route(files, source_text, task)

        # Try to use SubAgentManager for real multi-agent execution
        manager = _global_subagent_manager
        if manager is not None:
            return await self._execute_with_agents(
                router, manager, task, scan_evidence, source_text,
            )

        # Fallback: pure LLM analysis
        user_msg = f"## 任务描述\n{task}\n"
        user_msg += f"\n## 专家路由扫描\n{scan_evidence}\n"
        if source_text:
            user_msg += f"\n## 相关源代码\n{source_text[:50000]}\n"

        return await _run_analysis(router, _ROUTE_SYSTEM, user_msg)

    async def _execute_with_agents(
        self,
        router: Any,
        manager: Any,
        task: str,
        scan_evidence: str,
        source_text: str,
    ) -> str:
        """Use SubAgentManager + Factory + MessageBus for multi-agent MoE."""
        from naumi_agent.agents.message_bus import AgentMessage
        from naumi_agent.orchestrator.subagent_manager import SubTask

        # Reset bus for clean session
        await manager.message_bus.reset()

        # Write initial context to blackboard for all agents to read
        await manager.message_bus.blackboard_set(
            "task", task, author="orchestrator",
        )
        await manager.message_bus.blackboard_set(
            "scan_evidence", scan_evidence, author="orchestrator",
        )
        if source_text:
            await manager.message_bus.blackboard_set(
                "source_summary", source_text[:4000], author="orchestrator",
            )

        # Phase 1: Use LLM to plan expert panel
        planning_prompt = (
            "Based on the task and scan evidence below, identify 3-5 expert domains.\n"
            "For each expert, output EXACTLY this format (one per line):\n"
            "EXPERT|<name>|<domain>|<one-line-focus>\n\n"
            "Only output EXPERT lines, nothing else.\n\n"
            f"## 任务\n{task}\n\n## 扫描结果\n{scan_evidence}"
        )
        planning_resp = await _run_analysis(router, planning_prompt, task)
        expert_lines = [
            ln for ln in planning_resp.strip().splitlines()
            if ln.startswith("EXPERT|")
        ]

        if not expert_lines:
            user_msg = f"## 任务描述\n{task}\n"
            user_msg += f"\n## 专家路由扫描\n{scan_evidence}\n"
            if source_text:
                user_msg += f"\n## 相关源代码\n{source_text[:50000]}\n"
            return await _run_analysis(router, _ROUTE_SYSTEM, user_msg)

        # Phase 2: Spawn expert agents via factory
        spawned_names: list[str] = []
        subtasks: list[SubTask] = []

        for i, line in enumerate(expert_lines[:5]):
            parts = line.split("|")
            if len(parts) < 4:
                continue
            raw_name = parts[1].strip()
            domain = parts[2].strip()
            focus = parts[3].strip()
            safe_name = (
                f"moe_{raw_name.replace(' ', '_').replace('/', '_')[:25]}"
            )

            manager.spawn_for_task(
                name=safe_name,
                task_description=task,
                role="expert_analyst",
                focus=focus,
                domain=domain,
                max_turns=3,
                max_budget_usd=0.15,
            )
            spawned_names.append(safe_name)

            expert_task = f"从{domain}专家视角分析以下任务:\n\n{task}\n"
            if source_text:
                expert_task += (
                    f"\n## 相关代码（摘要）\n{source_text[:8000]}\n"
                )

            subtasks.append(SubTask(
                id=f"expert_{i}",
                description=expert_task,
                agent_name=safe_name,
            ))

        if not subtasks:
            user_msg = f"## 任务描述\n{task}\n"
            user_msg += f"\n## 专家路由扫描\n{scan_evidence}\n"
            return await _run_analysis(router, _ROUTE_SYSTEM, user_msg)

        # Phase 3: Execute experts in parallel
        results = await manager.execute_parallel(subtasks)

        # Phase 4: Write results to blackboard + collect reports
        expert_reports: list[str] = []
        for st, result in zip(subtasks, results):
            agent_name = st.agent_name or "unknown"
            if result.status == "completed" and result.response:
                expert_reports.append(
                    f"### {agent_name}\n{result.response}"
                )
                # Share findings on blackboard
                await manager.message_bus.blackboard_set(
                    f"expert_{agent_name}",
                    result.response[:2000],
                    author=agent_name,
                )
                # Broadcast completion
                await manager.message_bus.publish(AgentMessage(
                    sender=agent_name,
                    topic="moe.expert.completed",
                    content=result.response[:500],
                    metadata={"domain": st.description[:100]},
                ))
            else:
                expert_reports.append(
                    f"### {agent_name}\n"
                    f"⚠️ 分析未完成: {result.error or '未知错误'}"
                )

        # Phase 5: Synthesize — include blackboard state
        bb_state = await manager.message_bus.blackboard_get_all()
        bb_summary = ""
        if bb_state:
            bb_lines = ["### 共享状态摘要"]
            for k, entry in bb_state.items():
                if k.startswith("expert_"):
                    bb_lines.append(
                        f"- **{k}** (v{entry.version}): "
                        f"{str(entry.value)[:100]}..."
                    )
            bb_summary = "\n".join(bb_lines)

        synthesis_msg = f"## 原始任务\n{task}\n\n"
        synthesis_msg += f"## 静态扫描\n{scan_evidence}\n\n"
        synthesis_msg += "## 各专家独立分析\n\n"
        synthesis_msg += "\n\n---\n\n".join(expert_reports)
        if bb_summary:
            synthesis_msg += f"\n\n---\n\n{bb_summary}"

        synthesis = await _run_analysis(router, _ROUTE_SYSTEM, synthesis_msg)

        # Phase 6: Cleanup
        for name in spawned_names:
            manager.destroy(name)
        await manager.message_bus.reset()

        # Bus stats for report
        bus_stats = manager.message_bus.stats()

        total_tok = sum(
            r.total_tokens for r in results if hasattr(r, "total_tokens")
        )
        total_usd = sum(
            r.total_cost_usd for r in results if hasattr(r, "total_cost_usd")
        )
        header = (
            f"## MoE 混合专家调度报告\n\n"
            f"**任务**: {task[:200]}\n"
            f"**专家组**: {len(spawned_names)} 位专家并行分析\n"
            f"**总 Token 消耗**: {total_tok}\n"
            f"**总成本**: ${total_usd:.4f}\n"
            f"**消息总线**: {bus_stats['total_messages']} 条消息, "
            f"{bus_stats['blackboard_entries']} 条共享状态\n\n"
            f"---\n\n"
        )
        return header + synthesis


# ===========================================================================
#  /speculate — 推测解码 (实习生起草 + 架构师审查)
# ===========================================================================

# Boilerplate / repetitive patterns
_BOILERPLATE_PATTERNS = [
    (r"def __init__\(self[^)]*\):\s*\n(?:\s+self\.\w+\s*=.*\n){3,}", "批量属性赋值"),
    (r"def (get_|set_|is_|has_)\w+\(self[^)]*\):\s*\n\s+return self\.\w+", "trivial getter/setter"),
    (r"(?:import|from)\s+\w+\s+import\s+\([^)]{50,}\)", "大批量导入"),
    (r"class\s+\w+\(.*Model.*\):\s*\n(?:\s+\w+:\s+\w+.*\n){5,}", "数据模型字段列表"),
    (r"@router\.(get|post|put|delete)\([^)]+\)\s*\n(?:async\s+)?def\s+\w+", "CRUD 端点"),
    (
        r"(?:try:\s*\n\s+.*\n\s+except\s+\w+.*:\s*\n"
        r"\s+raise\s+HTTPException){3,}",
        "重复 try/except 模式",
    ),
]

# Risk indicators (high-priority review targets)
_RISK_PATTERNS = [
    (r"malloc|calloc|realloc|free\s*\(", "内存管理操作", "CRITICAL"),
    (r"threading\.Lock|multiprocessing\.Lock|asyncio\.Lock", "并发锁", "HIGH"),
    (r"\.join\(timeout\s*=\s*None\)|\.wait\(\)", "无限等待/死锁风险", "HIGH"),
    (r"open\([^)]*,\s*['\"]w['\"]", "文件写入（无异常保护检查）", "MEDIUM"),
    (r"eval\s*\(|exec\s*\(", "动态代码执行", "CRITICAL"),
    (r"subprocess\.(call|run|Popen)\(", "子进程执行", "HIGH"),
    (r"os\.system\s*\(", "系统命令执行", "CRITICAL"),
    (r"pickle\.loads?\s*\(", "反序列化（安全风险）", "CRITICAL"),
    (r"cursor\.execute\s*\(\s*f['\"]", "SQL 字符串拼接（注入风险）", "CRITICAL"),
    (r"except\s*:\s*\n\s*pass", "静默吞异常", "HIGH"),
]


def _scan_speculate(
    files: list[Path], source_text: str, target: str,
) -> str:
    """speculate 模式静态扫描：识别样板代码与高风险审查区域."""
    findings: list[str] = []

    # 1. Detect boilerplate patterns
    boilerplate_items: list[str] = []
    for pattern, label in _BOILERPLATE_PATTERNS:
        matches = re.findall(pattern, source_text, re.MULTILINE)
        if matches:
            boilerplate_items.append(f"{label}: {len(matches)} 处")

    if boilerplate_items:
        findings.append("- 样板代码模式（可快速起草后审查）:")
        for item in boilerplate_items:
            findings.append(f"  - {item}")
    else:
        findings.append("- 样板代码: 未检测到明显样板模式")

    # 2. Detect high-risk zones
    risk_zones: list[tuple[str, str, str, int]] = []  # (label, risk, pattern, line)
    for i, line in enumerate(source_text.split("\n"), 1):
        for pattern, label, risk_level in _RISK_PATTERNS:
            if re.search(pattern, line):
                risk_zones.append((label, risk_level, line.strip()[:80], i))

    if risk_zones:
        findings.append(
            f"- ⚠️ 高风险区域: {len(risk_zones)} 处（必须慢思考审查）"
        )
        # Group by risk level
        for risk_level in ("CRITICAL", "HIGH", "MEDIUM"):
            items = [r for r in risk_zones if r[1] == risk_level]
            if items:
                findings.append(f"  - {risk_level} ({len(items)} 处):")
                for label, _, snippet, lineno in items[:5]:
                    findings.append(f"    - L{lineno}: [{label}] `{snippet}`")
                if len(items) > 5:
                    findings.append(f"    ... 还有 {len(items) - 5} 处")
    else:
        findings.append("- 高风险区域: 未检测到明显风险模式")

    # 3. Analyze code complexity distribution per file
    file_complexity: dict[str, dict[str, int]] = {}
    for f in files:
        try:
            content = f.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        branches = len(re.findall(r"\bif\s+|\belif\s+", content))
        loops = len(re.findall(r"\bfor\s+|\bwhile\s+", content))
        nesting = 0
        max_nesting = 0
        for char in content:
            if char == '{' or char == '(':
                nesting += 1
                max_nesting = max(max_nesting, nesting)
            elif char == '}' or char == ')':
                nesting = max(0, nesting - 1)
        file_complexity[f.name] = {
            "branches": branches,
            "loops": loops,
            "max_nesting": min(max_nesting, 99),
            "lines": content.count("\n") + 1,
        }

    if file_complexity:
        findings.append("- 文件复杂度分布:")
        for fname, metrics in sorted(
            file_complexity.items(),
            key=lambda x: x[1]["branches"],
            reverse=True,
        )[:6]:
            findings.append(
                f"  - {fname}: "
                f"{metrics['branches']} 分支, "
                f"{metrics['loops']} 循环, "
                f"最大嵌套 {metrics['max_nesting']}, "
                f"{metrics['lines']} 行"
            )

    # 4. Identify "safe zones" vs "danger zones"
    total_files = len(files)
    danger_files = sum(
        1 for m in file_complexity.values()
        if m["branches"] > 15 or m["max_nesting"] > 6
    )
    safe_files = total_files - danger_files
    findings.append(
        f"- 区域划分: {safe_files} 个安全文件, "
        f"{danger_files} 个危险文件 (分支>15 或 嵌套>6)"
    )

    # 5. Estimate review effort
    total_risks = len(risk_zones)
    critical_count = sum(
        1 for r in risk_zones if r[1] == "CRITICAL"
    )
    if total_risks > 0:
        review_min = critical_count * 5 + (total_risks - critical_count) * 2
        findings.append(
            f"- 预估审查时间: ~{review_min} 分钟 "
            f"({critical_count} 个 CRITICAL 需要逐行审查)"
        )

    return "\n".join(findings)


_SPECULATE_SYSTEM = """\
You are a Speculative Decoding engine using the "Intern Draft + Architect \
Review" dual-mode paradigm.

## Core Principle
Split the work into TWO passes:
1. **Intern Pass (Fast Draft)**: Rapidly generate the outline, boilerplate, \
and straightforward sections. Don't overthink — just get it written.
2. **Architect Pass (Slow Review)**: Carefully review ONLY the zones flagged \
as high-risk. This is where you spend your "slow thinking" budget.

## Your Tasks

### Phase 1: Intern Draft (Fast)
Generate the initial draft at high speed:
- Produce the full solution outline
- Write boilerplate sections (imports, setup, data models, config)
- Implement the straightforward logic paths
- For each section, mark: ✅ (confident) or ⚠️ (needs review)

### Phase 2: Architect Review (Slow)
For EVERY ⚠️ section, perform deep analysis:
- **Memory safety**: Any leaks, double-frees, buffer overflows?
- **Concurrency**: Deadlocks, race conditions, priority inversion?
- **Error handling**: Are all failure paths covered? Silent catches?
- **Security**: Injection, traversal, deserialization risks?
- **Edge cases**: Empty inputs, None, negative numbers, concurrent access?

For each reviewed section:
- Show the original draft code
- Show the reviewed/fixed code with changes highlighted
- Explain WHY each change was needed

### Phase 3: Diff Summary
Produce a final summary:
- Total lines drafted: N
- Lines reviewed and modified: N
- CRITICAL fixes applied: N
- Remaining concerns: (list any unresolved issues)
- Confidence in the final output: X/10

Be decisive in the intern phase, surgical in the architect phase.
"""


class SpeculateTool(Tool):
    """推测解码 — 实习生快速起草 + 架构师深度审查双模式."""

    @property
    def name(self) -> str:
        return "analysis_speculate"

    @property
    def description(self) -> str:
        return (
            "推测解码(Speculative Decoding)：先用\"实习生\"模式极速生成初稿"
            "（样板代码、大纲、常规逻辑），再用\"架构师\"模式"
            "对高风险区域（内存、并发、安全、边界情况）进行逐行审查与重构。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "要生成/审查的文件或目录路径",
                },
                "task": {
                    "type": "string",
                    "description": "要生成的代码功能描述",
                    "default": "",
                },
            },
            "required": ["target"],
        }

    async def execute(
        self,
        *,
        target: str,
        task: str = "",
        **kwargs: Any,
    ) -> str:
        router = _global_router
        if router is None:
            return _router_unavailable("speculate", target)

        files = _resolve_target(target)
        if not files:
            return f"无法解析目标: {target} (请提供文件或目录路径)"

        source_text = _read_sources(files)
        scan_evidence = _scan_speculate(files, source_text, target)

        user_msg = (
            f"## 风险扫描证据\n{scan_evidence}\n\n"
            f"## 源代码\n{source_text[:50000]}\n"
        )
        if task:
            user_msg += f"\n## 生成任务\n{task}\n"

        return await _run_analysis(router, _SPECULATE_SYSTEM, user_msg)


# ===========================================================================
#  /jit — JIT 即时沙盒工具生成
# ===========================================================================

# Task types that require computational verification
_COMPUTATION_TASKS = {
    "math": [
        r"(?:计算|求值|积分|微分|矩阵|向量|概率|统计|回归)",
        r"(?:calculate|compute|integral|derivative|matrix|vector)",
        r"(?:fibonacci|prime|factorial|permutation|combination)",
        r"(?:方程|不等式|最优化|线性规划)",
    ],
    "string": [
        r"(?:字符串|正则|匹配|替换|编码|解码|哈希)",
        r"(?:regex|parse|transform|encode|decode|hash|base64)",
        r"(?:格式化|提取|分割|拼接|转义)",
    ],
    "data": [
        r"(?:排序|过滤|聚合|去重|分组|透视|统计)",
        r"(?:sort|filter|aggregate|dedup|group|pivot)",
        r"(?:csv|json|yaml|xml|excel|pandas|dataframe)",
    ],
    "algo": [
        r"(?:算法|图|树|路径|搜索|动态规划|贪心|回溯)",
        r"(?:graph|tree|bfs|dfs|dijkstra|dp|greedy|backtrack)",
        r"(?:排序算法|查找|时间复杂度|空间复杂度)",
    ],
    "network": [
        r"(?:爬虫|抓取|请求|http|api|websocket|socket)",
        r"(?:scrape|fetch|request|crawl|download)",
        r"(?:dns|tcp|udp|ip|端口|代理)",
    ],
}


def _scan_jit(task: str) -> str:
    """jit 模式静态扫描：分析任务是否需要计算验证及推荐运行时."""
    findings: list[str] = []
    task_lower = task.lower()

    # 1. Identify computation type
    matched_types: list[tuple[str, list[str]]] = []
    for comp_type, patterns in _COMPUTATION_TASKS.items():
        hits = []
        for pattern in patterns:
            matches = re.findall(pattern, task_lower)
            hits.extend(matches)
        if hits:
            matched_types.append((comp_type, hits))

    if matched_types:
        findings.append("- 检测到计算需求:")
        for comp_type, keywords in matched_types:
            unique_kw = list(set(keywords))[:5]
            findings.append(f"  - {comp_type}: {', '.join(unique_kw)}")
    else:
        findings.append(
            "- 计算需求: 未匹配到明确模式（将由 LLM 判断）"
        )

    # 2. Recommend language/runtime
    if any(t[0] == "math" for t in matched_types):
        findings.append("- 推荐语言: Python (numpy/scipy) 或 C++ (高性能)")
    elif any(t[0] == "string" for t in matched_types):
        findings.append("- 推荐语言: Python (re/字符串操作)")
    elif any(t[0] == "data" for t in matched_types):
        findings.append("- 推荐语言: Python (pandas/csv/json)")
    elif any(t[0] == "algo" for t in matched_types):
        findings.append(
            "- 推荐语言: Python (快速验证) 或 C++ (生产级)"
        )
    elif any(t[0] == "network" for t in matched_types):
        findings.append("- 推荐语言: Python (httpx/requests)")
    else:
        findings.append(
            "- 推荐语言: Python (通用) — 最适合即时生成与执行"
        )

    # 3. Identify constraints from the task
    constraints: list[str] = []
    if re.search(r"\d+\s*(?:ms|毫秒|秒|second)", task_lower):
        constraints.append("时间限制")
    if re.search(r"\d+\s*(?:MB|GB|KB|字节)", task_lower):
        constraints.append("内存限制")
    if re.search(
        r"(?:精确|精确到|小数点|精度|float|double|decimal)",
        task_lower,
    ):
        constraints.append("精度要求")
    if re.search(r"(?:并发|并行|多线程|multi)", task_lower):
        constraints.append("并发要求")
    if re.search(r"(?:大数|10\^\d+|万|亿|million|billion)", task_lower):
        constraints.append("大数据量")
    if constraints:
        findings.append(f"- 约束条件: {', '.join(constraints)}")

    # 4. Detect if answer needs verification
    verification_needed = any(
        t[0] in ("math", "algo") for t in matched_types
    )
    if verification_needed:
        findings.append(
            "- ✅ 需要计算验证 — LLM 推理不可靠，必须运行代码"
        )
    else:
        findings.append(
            "- ℹ️ 可选计算验证 — LLM 推理可能够用，但代码更可靠"
        )

    return "\n".join(findings)


_JIT_SYSTEM = """\
You are a JIT (Just-In-Time) Tool Generator. When pure LLM reasoning \
cannot guarantee correctness, you generate and present actual runnable \
code as your "external brain computation."

## Core Principle
**STOP guessing.** If the answer involves:
- Mathematical computation → write and trace through the code
- String manipulation with precise rules → write and test the code
- Data transformation → write and run the pipeline
- Algorithm correctness → implement and verify with test cases

## Your Tasks

### 1. Task Analysis
- State whether LLM reasoning alone is sufficient (confidence < 90% → use code)
- Identify the exact computation needed
- Declare the input/output contract

### 2. Code Generation
Generate a COMPLETE, RUNNABLE script:
- Language: Python (preferred for JIT) or C++ (if performance critical)
- Include all imports and setup
- Include test cases that verify correctness
- Include print statements that show the computation trace
- The code must be copy-paste-runnable (no missing dependencies)

### 3. Execution Trace
Simulate running the code mentally (or for straightforward cases, show \
output):
- Show the step-by-step computation
- Show intermediate values at key checkpoints
- Show the final result

### 4. Verification
- Provide at least 2 test cases with known correct answers
- Show that the code produces the expected output
- If any test fails, fix the code and re-run

### 5. Result
State the answer clearly, derived from the code's deterministic output, \
not from LLM reasoning.

Format:
```
## JIT Script
```python
# ... complete runnable code ...
```

## Execution Result
```
# ... actual or simulated output ...
```

## Verified Answer
Based on the code output: [clear answer]
```
"""


class JITTool(Tool):
    """JIT 即时沙盒工具生成 — 停止玄学推理，用代码保证确定性."""

    @property
    def name(self) -> str:
        return "analysis_jit"

    @property
    def description(self) -> str:
        return (
            "JIT 即时工具生成：当 LLM 推理无法保证准确性时，"
            "立即生成可运行的 Python/C++ 脚本，"
            "展示代码作为\"外置大脑计算过程\"，"
            "基于代码的确定性结果回答问题。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "需要计算验证的任务描述",
                },
                "context": {
                    "type": "string",
                    "description": "补充上下文（已知条件、约束等）",
                    "default": "",
                },
            },
            "required": ["task"],
        }

    async def execute(
        self,
        *,
        task: str,
        context: str = "",
        **kwargs: Any,
    ) -> str:
        router = _global_router
        if router is None:
            return _router_unavailable("jit", task[:200])

        scan_evidence = _scan_jit(task)

        user_msg = f"## 任务\n{task}\n"
        user_msg += f"\n## JIT 扫描分析\n{scan_evidence}\n"
        if context:
            user_msg += f"\n## 补充上下文\n{context}\n"

        return await _run_analysis(router, _JIT_SYSTEM, user_msg)


# ===========================================================================
#  /pointer — 语义指针架构 (SPA) 推理态/物理态分离
# ===========================================================================

# Patterns indicating precision-sensitive data (hallucination risk)
_PRECISION_PATTERNS = [
    (r"(?:Decimal|decimal\.Decimal|float|double|np\.float\d*)", "浮点精确类型"),
    (r"(?:money|currency|price|amount|balance|fee)", "金融金额"),
    (r"(?:PE|EPS|ROE|ROI|NAV| sharpe|alpha|beta)\b", "金融指标"),
    (r"(?:dosage|blood_pressure|heart_rate|diagnosis)", "医疗数据"),
    (r"(?:coordinate|altitude|velocity|trajectory|orbit)", "航天/物理数据"),
    (r"(?:hash|checksum|signature|token|secret|key)\b", "安全哈希"),
    (r"(?:id|uuid|guid|serial)\s*[:=]\s*[\"']\w+", "唯一标识符"),
]

# Patterns for external data sources (pointer-ifiable)
_POINTER_SOURCES = [
    (r"(?:api|fetch|get|query|request)\s*\([^)]*stock", "股票 API"),
    (r"(?:api|fetch|get|query|request)\s*\([^)]*price", "价格 API"),
    (r"(?:\.execute\(|cursor\.|session\.query)", "数据库查询"),
    (r"(?:redis\.get|cache\.get|memcached)", "缓存读取"),
    (r"(?:pd\.read_|read_csv|read_json|read_parquet)", "数据文件读取"),
    (r"(?:requests\.(get|post)|httpx\.client)", "HTTP 数据源"),
]

# Patterns where AI output meets precise data (boundary risk)
_BOUNDARY_PATTERNS = [
    (r"return\s+str\(.*(?:price|amount|balance)", "数值转字符串返回"),
    (r"f[\"'].*{(?:price|amount|pe|eps).*}[\"']", "f-string 插入金融数据"),
    (r"(?:format|round)\s*\(.*(?:price|amount|rate)", "金融数据格式化"),
    (r"json\.dumps\s*\([^)]*(?:result|data|response)", "JSON 序列化 AI 输出"),
    (r"response\s*[:=]\s*(?:await\s+)?(?:llm|model|chat|complete)", "LLM 原始输出"),
]


def _scan_pointer(
    files: list[Path], source_text: str, target: str,
) -> str:
    """pointer 模式静态扫描：检测幻觉风险点和可指针化的数据边界."""
    findings: list[str] = []

    # 1. Detect precision-sensitive data patterns
    precision_hits: list[tuple[str, int]] = []
    for pattern, label in _PRECISION_PATTERNS:
        count = len(re.findall(pattern, source_text, re.IGNORECASE))
        if count:
            precision_hits.append((label, count))

    if precision_hits:
        findings.append("- 精密数据类型（幻觉高风险）:")
        for label, count in precision_hits:
            findings.append(f"  - {label}: {count} 处引用")
    else:
        findings.append("- 精密数据类型: 未检测到")

    # 2. Detect pointer-ifiable external sources
    pointer_sources: list[tuple[str, int]] = []
    for pattern, label in _POINTER_SOURCES:
        count = len(re.findall(pattern, source_text, re.IGNORECASE))
        if count:
            pointer_sources.append((label, count))

    if pointer_sources:
        findings.append("- 可指针化的数据源（建议物理态隔离）:")
        for label, count in pointer_sources:
            findings.append(f"  - {label}: {count} 处")
    else:
        findings.append("- 外部数据源: 未检测到")

    # 3. Detect boundary risk points
    boundary_hits: list[tuple[str, int]] = []
    for pattern, label in _BOUNDARY_PATTERNS:
        count = len(re.findall(pattern, source_text, re.IGNORECASE))
        if count:
            boundary_hits.append((label, count))

    if boundary_hits:
        findings.append(
            "- ⚠️ 推理态/物理态边界风险点: "
            f"{sum(c for _, c in boundary_hits)} 处"
        )
        for label, count in boundary_hits:
            findings.append(f"  - {label}: {count} 处")
    else:
        findings.append("- 边界风险: 未检测到明显风险")

    # 4. Detect hardcoded values (should be pointers instead)
    magic_numbers = re.findall(
        r"(?<!self\.)(?:price|rate|ratio|threshold)\s*[=:]\s*[\d.]+",
        source_text,
        re.IGNORECASE,
    )
    if magic_numbers:
        findings.append(
            f"- ⚠️ 硬编码数值: {len(magic_numbers)} 处"
            f"（应改为指针引用外部数据源）"
        )
        for m in magic_numbers[:5]:
            findings.append(f"  - `{m.strip()}`")

    # 5. Identify existing abstraction layers (good or missing)
    has_dao = bool(re.findall(r"(?:Repository|DAO|Mapper|Gateway)", source_text))
    has_service = bool(re.findall(r"(?:Service|Manager|Handler)", source_text))
    has_controller = bool(re.findall(
        r"(?:Controller|Router|Endpoint|View)", source_text,
    ))
    layers = []
    if has_dao:
        layers.append("数据层(DAO)")
    if has_service:
        layers.append("服务层(Service)")
    if has_controller:
        layers.append("控制层(Controller)")
    if layers:
        findings.append(
            f"- 已有分层: {' → '.join(layers)}"
        )
    else:
        findings.append(
            "- ⚠️ 无明显分层架构（需要 SPA 重构）"
        )

    # 6. Hallucination Risk Score
    risk_score = 0
    risk_score += sum(c for _, c in precision_hits) * 5
    risk_score += sum(c for _, c in boundary_hits) * 8
    risk_score += len(magic_numbers) * 10
    if not layers:
        risk_score += 20

    level = (
        "CRITICAL" if risk_score > 100
        else "HIGH" if risk_score > 50
        else "MEDIUM" if risk_score > 20
        else "LOW"
    )
    findings.append(
        f"\n- 幻觉风险评分: {risk_score} ({level})"
    )
    if level in ("HIGH", "CRITICAL"):
        findings.append(
            "  → 强烈建议：将精密数据计算剥离为独立模块，"
            "AI 仅通过指针(API调用)获取结果"
        )

    return "\n".join(findings)


_POINTER_SYSTEM = """\
You are a Semantic Pointer Architecture (SPA) analyst implementing the \
C++ pointer concept in AI systems.

## Core Principle
**Separate "reasoning space" (fuzzy AI thinking) from "physical space" \
(precise data computation).** The AI should NEVER directly generate or \
manipulate precise data. Instead:

1. **Reasoning Space (AI's job)**: Strategy, logic, orchestration, \
natural language understanding, user interaction
2. **Physical Space (Hardcoded modules)**: Numerical computation, \
data retrieval, type-safe operations, precision-critical calculations
3. **Pointers (The bridge)**: API calls, DB queries, function references \
that let AI "dereference" precise data without touching it

## Your Tasks

### 1. Hallucination Risk Assessment
Based on scan evidence, identify where the current system risks AI \
hallucination on precise data:
- Which modules handle financial/medical/safety-critical data?
- Where does AI output flow directly into data computations?
- What hardcoded values should be externalized?

### 2. SPA Architecture Design
Redesign the system into two spaces:

**Reasoning Space (AI-managed):**
- List what the AI SHOULD do (strategy, routing, NL generation)
- Define the "pointer interface" — what APIs/calls the AI can make
- Specify the contract: input format, expected return type

**Physical Space (Code-managed):**
- List what must be in precise modules (calculations, DB queries)
- Define the "dereference modules" — functions that fetch real data
- Specify type contracts: Decimal, not float; validated, not raw

### 3. Pointer Protocol
For each data boundary:
- Define the pointer token format (API endpoint, function name, query)
- Define the dereference contract (input type → output type)
- Define the error handling (what if pointer returns null/error?)
- Define the validation layer (how to verify dereferenced data)

### 4. Migration Plan
Phase-by-phase refactoring:
- Phase 1: Identify and isolate the highest-risk boundary
- Phase 2: Build the dereference module for that boundary
- Phase 3: Replace AI direct data handling with pointer calls
- Phase 4: Add validation layer and monitoring
- Phase 5: Repeat for remaining boundaries

### 5. Example Pointer Table
Provide a concrete table:

| Pointer | Dereference Module | Input | Output | Risk Level |
|---------|-------------------|-------|--------|------------|
| ...     | ...               | ...   | ...    | ...        |

Be architectural. Think in terms of memory management, not prompts.
"""


class PointerTool(Tool):
    """语义指针架构 — 推理态/物理态分离，消除 AI 幻觉风险."""

    @property
    def name(self) -> str:
        return "analysis_pointer"

    @property
    def description(self) -> str:
        return (
            "语义指针架构(SPA)：检测代码中 AI 直接处理精密数据"
            "的幻觉风险点，设计推理态(AI逻辑)与物理态(精确计算)"
            "分离方案，定义指针协议（API/DB引用）替代直接数据操作。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "要分析的文件或目录路径",
                },
                "context": {
                    "type": "string",
                    "description": "补充上下文（业务领域、精度要求等）",
                    "default": "",
                },
            },
            "required": ["target"],
        }

    async def execute(
        self,
        *,
        target: str,
        context: str = "",
        **kwargs: Any,
    ) -> str:
        router = _global_router
        if router is None:
            return _router_unavailable("pointer", target)

        files = _resolve_target(target)
        if not files:
            return f"无法解析目标: {target} (请提供文件或目录路径)"

        source_text = _read_sources(files)
        scan_evidence = _scan_pointer(files, source_text, target)

        user_msg = (
            f"## SPA 扫描证据\n{scan_evidence}\n\n"
            f"## 源代码\n{source_text[:50000]}\n"
        )
        if context:
            user_msg += f"\n## 补充上下文\n{context}\n"

        return await _run_analysis(router, _POINTER_SYSTEM, user_msg)


# ===========================================================================
#  /cooe — 认知乱序执行流水线 (COOE)
# ===========================================================================

# I/O-blocking patterns (these cause sequential stalls)
_IO_PATTERNS = [
    (r"await\s+(?:client\.|session\.|httpx|aiohttp|requests)", "异步网络 I/O"),
    (r"(?:fetch|download|scrape|crawl|request)\s*\(", "数据抓取"),
    (r"(?:read_text|read_csv|read_json|read_file|open\()", "文件 I/O"),
    (r"(?:cursor\.execute|session\.query|\.query\()", "数据库查询"),
    (r"(?:redis\.\w+|cache\.\w+|memcached)", "缓存 I/O"),
    (r"(?:LLM|model|chat|complete|generate)\s*\(", "LLM API 调用"),
]

# Parallelizable patterns (already async or could be)
_PARALLEL_PATTERNS = [
    (r"asyncio\.gather\s*\(", "已使用 asyncio.gather 并行"),
    (r"asyncio\.create_task\s*\(", "已使用 create_task 并行"),
    (r"concurrent\.futures", "已使用线程池并行"),
    (r"multiprocessing", "已使用多进程"),
    (r"threading\.Thread", "已使用多线程"),
    (r"async\s+for\s+", "异步迭代器"),
]

# Sequential dependency patterns (bottleneck indicators)
_SEQUENTIAL_PATTERNS = [
    (r"result\s*=\s*await\s+\w+.*\n\s*\w+\s*=\s*await", "串行 await 链"),
    (
        r"(?:response|data|result)\s*=\s*await.*\n\s*(?:process|parse|extract)",
        "I/O → 处理串行依赖",
    ),
    (r"for\s+\w+\s+in\s+(?:range|list|items)", "串行循环（可并行化）"),
]


def _scan_cooe(
    files: list[Path], source_text: str, task: str,
) -> str:
    """cooe 模式静态扫描：分析 I/O 阻塞点、并行化机会和依赖图."""
    findings: list[str] = []

    # 1. Detect I/O-blocking operations
    io_ops: list[tuple[str, int]] = []
    for pattern, label in _IO_PATTERNS:
        count = len(re.findall(pattern, source_text, re.IGNORECASE))
        if count:
            io_ops.append((label, count))

    if io_ops:
        total_io = sum(c for _, c in io_ops)
        findings.append(
            f"- I/O 阻塞操作: {total_io} 处"
            f"（潜在串行等待瓶颈）"
        )
        for label, count in io_ops:
            findings.append(f"  - {label}: {count} 处")
    else:
        findings.append("- I/O 阻塞操作: 未检测到")

    # 2. Detect existing parallelization
    parallel_ops: list[tuple[str, int]] = []
    for pattern, label in _PARALLEL_PATTERNS:
        count = len(re.findall(pattern, source_text))
        if count:
            parallel_ops.append((label, count))

    if parallel_ops:
        findings.append("- 已有并行化机制:")
        for label, count in parallel_ops:
            findings.append(f"  - ✅ {label}: {count} 处")
    else:
        findings.append("- 已有并行化机制: 无（全部串行执行）")

    # 3. Detect sequential bottlenecks
    seq_ops: list[tuple[str, int]] = []
    for pattern, label in _SEQUENTIAL_PATTERNS:
        count = len(re.findall(pattern, source_text, re.MULTILINE))
        if count:
            seq_ops.append((label, count))

    if seq_ops:
        findings.append(
            f"- 串行瓶颈: {sum(c for _, c in seq_ops)} 处"
        )
        for label, count in seq_ops:
            findings.append(f"  - {label}: {count} 处")
    else:
        findings.append("- 串行瓶颈: 未检测到明显瓶颈")

    # 4. Analyze function call DAG potential
    import ast as _ast
    import collections

    call_graph: dict[str, set[str]] = collections.defaultdict(set)
    for f in files:
        try:
            tree = _ast.parse(
                f.read_text(encoding="utf-8", errors="replace")
            )
        except SyntaxError:
            continue
        for node in _ast.walk(tree):
            if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                func_name = node.name
                for child in _ast.walk(node):
                    if isinstance(child, _ast.Call):
                        if isinstance(child.func, _ast.Name):
                            call_graph[func_name].add(child.func.id)
                        elif isinstance(child.func, _ast.Attribute):
                            call_graph[func_name].add(child.func.attr)

    # Find functions with independent sub-trees (parallelizable)
    top_level = set(call_graph.keys())
    for callees in call_graph.values():
        top_level -= callees

    if call_graph:
        findings.append(
            f"- 调用图: {len(call_graph)} 个函数, "
            f"{len(top_level)} 个顶层入口"
        )
        # Find independent sub-trees
        independent_groups: list[list[str]] = []
        used: set[str] = set()
        for func in top_level:
            if func in used:
                continue
            group = [func]
            used.add(func)
            for callee in call_graph.get(func, set()):
                if callee not in used:
                    group.append(callee)
                    used.add(callee)
            independent_groups.append(group)

        if len(independent_groups) > 1:
            findings.append(
                f"- 可并行子图: {len(independent_groups)} 组"
            )
            for i, group in enumerate(independent_groups[:5]):
                findings.append(
                    f"  - 组 {i + 1}: {', '.join(group[:4])}"
                )
        else:
            findings.append(
                "- 可并行子图: 仅 1 组（强依赖，难以并行）"
            )

    # 5. Estimate speedup potential
    io_count = sum(c for _, c in io_ops)
    parallel_count = sum(c for _, c in parallel_ops)
    if io_count > 0 and parallel_count == 0:
        est_speedup = f"{min(io_count, 10)}x"
        findings.append(
            f"- 预估加速比: ~{est_speedup} "
            f"（全部 I/O 串行，改为并行可获得显著提升）"
        )
    elif io_count > parallel_count:
        findings.append(
            "- 预估加速比: 2-5x（部分已并行，仍有优化空间）"
        )
    elif parallel_count > 0:
        findings.append("- 预估加速比: ~1x（已有并行化机制）")

    # 6. ROB readiness assessment
    has_queue = bool(
        re.findall(r"(?:Queue|deque|PriorityQueue|asyncio\.Queue)", source_text)
    )
    has_barrier = bool(
        re.findall(
            r"(?:Barrier|Event|Semaphore|gather|wait)", source_text,
        )
    )
    rob_features = []
    if has_queue:
        rob_features.append("队列机制")
    if has_barrier:
        rob_features.append("同步屏障")
    if rob_features:
        findings.append(
            f"- ROB 基础设施: {' + '.join(rob_features)}"
        )
    else:
        findings.append(
            "- ROB 基础设施: 无（需要构建调度器+ROB）"
        )

    if task:
        findings.append(f"- 目标任务: {task[:200]}")

    return "\n".join(findings)


_COOE_SYSTEM = """\
You are a Cognitive Out-of-Order Execution (COOE) engine architect, \
directly applying CPU pipeline design to AI agent workflows.

## Core Principle
**NEVER think linearly about complex multi-step tasks.** Instead, model \
the task as a Directed Acyclic Graph (DAG) and execute like a modern \
CPU's out-of-order execution pipeline.

## The 3-Stage Pipeline

### Stage 1: Instruction Decode & DAG Generation
Break the task into atomic sub-tasks and build the dependency graph:
- Each node is an atomic operation (fetch data, compute, transform, etc.)
- Each edge is a DATA dependency (Task B needs Task A's output)
- Identify all independent branches (can run in parallel)

Output a formal DAG:
```
Task A (fetch财报) ──┐
                     ├──→ Task D (汇总分析) ──→ Task E (写报告)
Task B (拉K线)   ──┤
                     ├──→ Task D
Task C (搜政策)   ──┘
```

### Stage 2: Reservation Stations & Parallel Issue
For each independent task group:
- Assign to a "reservation station" (worker agent/slot)
- Mark estimated execution time (I/O bound vs CPU bound)
- Mark resource requirements (API calls, memory, etc.)
- Issue all independent tasks SIMULTANEOUSLY

### Stage 3: Reorder Buffer (ROB) & Commit
- All results enter the ROB in completion order
- Results are held until all predecessors in the DAG are complete
- Commit stage assembles results in the correct logical order
- Only THEN produce the final output

## Your Output Format

### 1. Task Decomposition
List every atomic sub-task with:
- Name, estimated time, I/O vs CPU bound, dependencies

### 2. DAG Visualization
Show the complete dependency graph with ASCII art or structured text

### 3. Execution Timeline
Compare sequential vs parallel timelines:
```
Sequential:  [A: 10s] → [B: 5s] → [C: 3s] → [D: 2s] = 20s
COOE:        [A: 10s]
             [B: 5s]  ──→ [D: 2s]  = 12s
             [C: 3s]  ──↗
```

### 4. Scheduler Design
- How many worker slots (reservation stations)?
- What's the dispatch strategy (FIFO, priority-based)?
- How to handle failures (one task fails, what happens)?

### 5. ROB Configuration
- Buffer size and ordering policy
- Commit trigger conditions
- Backpressure handling (what if ROB is full?)

### 6. Speedup Analysis
- Theoretical maximum speedup (critical path)
- Practical speedup accounting for overhead
- Bottleneck analysis (which task limits parallelism?)

Be architectural. Think in terms of CPU pipeline stages, not prompts.
"""


class COOETool(Tool):
    """COOE 认知乱序执行引擎 — DAG 依赖分析 + 并行调度设计."""

    @property
    def name(self) -> str:
        return "analysis_cooe"

    @property
    def description(self) -> str:
        return (
            "认知乱序执行引擎(COOE)：将复杂任务拆解为 DAG（有向无环图），"
            "识别数据依赖和可并行步骤，设计调度器+保留站+"
            "重排序缓冲(ROB)的 CPU 级流水线架构，"
            "实现时间复杂度的极致压缩。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "要分析的多步骤任务描述",
                },
                "target": {
                    "type": "string",
                    "description": "相关代码路径（可选）",
                    "default": "",
                },
            },
            "required": ["task"],
        }

    async def execute(
        self,
        *,
        task: str,
        target: str = "",
        **kwargs: Any,
    ) -> str:
        router = _global_router
        if router is None:
            return _router_unavailable("cooe", task[:200])

        source_text = ""
        files: list[Path] = []
        if target:
            files = _resolve_target(target)
            if files:
                source_text = _read_sources(files)

        scan_evidence = _scan_cooe(files, source_text, task)

        user_msg = f"## 任务描述\n{task}\n"
        user_msg += f"\n## COOE 扫描证据\n{scan_evidence}\n"
        if source_text:
            user_msg += f"\n## 相关源代码\n{source_text[:40000]}\n"

        return await _run_analysis(router, _COOE_SYSTEM, user_msg)


# ===========================================================================
#  /sleep — 昼夜节律突触修剪
# ===========================================================================

def _scan_sleep(
    files: list[Path], source_text: str, session_context: str,
) -> str:
    findings: list[str] = []
    topics: dict[str, int] = {}
    for pattern, label in [
        (r"(?:def |class |function |module )(\w+)", "代码定义"),
        (r"(?:bug|error|fix|debug|crash)", "问题调试"),
        (r"(?:test|spec|assert|verify)", "测试验证"),
        (r"(?:design|arch|pattern|架构|设计)", "架构设计"),
    ]:
        count = len(re.findall(pattern, source_text, re.IGNORECASE))
        if count:
            topics[label] = count
    if topics:
        findings.append("- 对话主题分布:")
        for label, count in sorted(
            topics.items(), key=lambda x: x[1], reverse=True,
        ):
            findings.append(f"  - {label}: {count} 次出现")
    total_chars = len(session_context)
    findings.append(
        f"- 会话上下文: {total_chars:,} 字符 (~{total_chars // 4:,} tokens)"
    )
    return "\n".join(findings)


_SLEEP_SYSTEM = """\
You are a Circadian Synaptic Pruning engine implementing biological \
sleep consolidation for AI systems.

## Tasks
1. Replay & Summarize (concepts, skills, decisions, corrections)
2. Synaptic Pruning (what to delete: dead-ends, understood basics, \
repetition, debugging chatter)
3. Knowledge Consolidation (what to hardcode: verified solutions, \
user preferences, project conventions, architectural decisions)
4. Evolution Patch (concise knowledge to append to system prompt)
5. Memory State After Sleep (size reduction, insights preserved, \
pruned items, readiness)
"""


class SleepPruningTool(Tool):

    @property
    def name(self) -> str:
        return "analysis_sleep"

    @property
    def description(self) -> str:
        return (
            "昼夜节律突触修剪：对当前会话进行离线压缩，"
            "提取核心方法论和已固化概念，修剪冗余内容，"
            "生成可追加到 System Prompt 的进化补丁(Patch)。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "session_context": {
                    "type": "string",
                    "description": "当前会话的完整上下文",
                    "default": "",
                },
                "target": {
                    "type": "string",
                    "description": "相关代码路径",
                    "default": "",
                },
            },
            "required": [],
        }

    async def execute(
        self,
        *,
        session_context: str = "",
        target: str = "",
        **kwargs: Any,
    ) -> str:
        router = _global_router
        if router is None:
            return _router_unavailable("sleep", "session")
        source_text = ""
        files: list[Path] = []
        if target:
            files = _resolve_target(target)
            if files:
                source_text = _read_sources(files)
        combined = source_text
        if session_context:
            combined = (
                f"## 对话历史\n{session_context}\n\n"
                f"## 源代码\n{source_text}"
            )
        elif not source_text:
            combined = "（无会话上下文，将基于代码库进行分析）"
        scan_evidence = _scan_sleep(files, combined, session_context)
        user_msg = (
            f"## 突触修剪扫描\n{scan_evidence}\n\n"
            f"## 完整内容\n{combined[:60000]}\n"
        )
        return await _run_analysis(router, _SLEEP_SYSTEM, user_msg)


# ===========================================================================
#  /entropy — 耗散结构热力学重置
# ===========================================================================

def _scan_entropy(source_text: str, conversation: str) -> str:
    findings: list[str] = []
    sentences = _split_entropy_sentences(source_text + conversation)
    sentence_counts: dict[str, int] = {}
    for s in sentences:
        key = s[:50].lower()
        sentence_counts[key] = sentence_counts.get(key, 0) + 1
    repeated = sum(1 for c in sentence_counts.values() if c > 1)
    total = max(len(sentence_counts), 1)
    findings.append(f"- 语义重复率: {repeated / total:.1%}")
    if sentences:
        avg_len = sum(len(s) for s in sentences) / len(sentences)
        findings.append(f"- 平均句长: {avg_len:.0f} 字符")
    entropy_score = min(100, (repeated / total) * 100)
    temp = (
        "CRITICAL" if entropy_score > 60
        else "HIGH" if entropy_score > 35
        else "MEDIUM" if entropy_score > 15
        else "LOW"
    )
    findings.append(f"- 上下文温度: {entropy_score:.0f} ({temp})")
    return "\n".join(findings)


def _split_entropy_sentences(text: str) -> list[str]:
    sentences = re.split(r"[。！？.!?\n]+", text)
    return [s.strip() for s in sentences if len(s.strip()) > 5]


def _build_entropy_anchor(context: str, goal: str = "") -> str:
    sentences = _dedupe_entropy_sentences(_split_entropy_sentences(context))
    goal_text = _compact_entropy_sentence(goal) if goal.strip() else _pick_entropy_sentence(
        sentences,
        ("目标", "任务", "objective", "goal", "实现", "修复", "对齐"),
        fallback="当前目标需要继续推进，但上下文中没有清晰的目标句。",
    )
    facts_text = _pick_entropy_sentence(
        sentences,
        ("通过", "passed", "验证", "已", "完成", "提交", "commit", "修复"),
        fallback="当前没有可确认的验证事实，应先回到可执行证据。",
    )
    remaining_text = _pick_entropy_sentence(
        sentences,
        ("剩余", "下一", "需要", "待", "未", "失败", "todo", "pending", "继续"),
        fallback="下一步应选择最小可验证动作，并在完成后立即验证。",
    )
    return "\n".join(
        [
            "## 熵减锚点",
            f"1. 核心任务：{goal_text}",
            f"2. 已验证事实：{facts_text}",
            f"3. 剩余工作：{remaining_text}",
            "",
            "## 重启协议",
            "- 丢弃重复推理、历史死路和无证据猜测。",
            "- 只保留上面 3 句锚点作为下一步推理入口。",
            "- 下一步必须产出可验证动作或明确阻塞条件。",
        ]
    )


def _dedupe_entropy_sentences(sentences: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for sentence in sentences:
        key = re.sub(r"\s+", " ", sentence[:80].lower())
        if key in seen:
            continue
        seen.add(key)
        deduped.append(sentence)
    return deduped


def _pick_entropy_sentence(
    sentences: list[str],
    keywords: tuple[str, ...],
    *,
    fallback: str,
) -> str:
    if not sentences:
        return fallback
    scored: list[tuple[int, int, int, str]] = []
    for idx, sentence in enumerate(sentences):
        lower = sentence.lower()
        keyword_score = sum(1 for keyword in keywords if keyword.lower() in lower)
        length_score = min(len(sentence), 240) // 80
        scored.append((keyword_score, length_score, -idx, sentence))
    best = max(scored)
    if best[0] <= 0:
        return fallback
    return _compact_entropy_sentence(best[3])


def _compact_entropy_sentence(sentence: str, limit: int = 140) -> str:
    compacted = re.sub(r"\s+", " ", sentence.strip())
    if len(compacted) <= limit:
        return compacted
    return compacted[: limit - 1].rstrip() + "…"


_ENTROPY_SYSTEM = """\
You are a Dissipative Structure Valve implementing thermodynamic \
entropy reduction for AI reasoning chains.

## Mandatory Protocol
1. HALT current reasoning
2. Condense context into 3 sentences: core task, verified facts, \
remaining work
3. Purge all dead-ends and repetition
4. Restart from the 3-sentence anchor + original goal
5. Anti-drift: check every 3 paragraphs for relevance
"""


class EntropyValveTool(Tool):

    @property
    def name(self) -> str:
        return "analysis_entropy"

    @property
    def description(self) -> str:
        return (
            "耗散结构热力学重置：当推理链过长或逻辑发散时，"
            "强制执行熵减 — 用3句话总结正确状态（锚点），"
            "丢弃上下文包袱，从锚点重新启动推理。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "context": {
                    "type": "string",
                    "description": "当前对话上下文或需要熵减的长文本",
                },
                "goal": {
                    "type": "string",
                    "description": "原始目标/任务",
                    "default": "",
                },
            },
            "required": ["context"],
        }

    async def execute(
        self, *, context: str, goal: str = "", **kwargs: Any,
    ) -> str:
        scan_evidence = _scan_entropy("", context)
        deterministic = (
            f"## 熵值扫描\n{scan_evidence}\n\n"
            f"{_build_entropy_anchor(context, goal)}"
        )
        router = _global_router
        if router is None:
            return deterministic + "\n\n模型路由未初始化，已返回确定性熵减锚点。"

        user_msg = (
            f"## 熵值扫描\n{scan_evidence}\n\n"
            f"## 确定性锚点\n{deterministic}\n\n"
            f"## 当前上下文\n{context[:60000]}\n"
        )
        if goal:
            user_msg += f"\n## 原始目标\n{goal}\n"
        enhanced = await _run_analysis(router, _ENTROPY_SYSTEM, user_msg)
        return deterministic + "\n\n## LLM 增强熵减\n" + enhanced


# ===========================================================================
#  /ooda — 战场任务式指挥 (OODA Loop)
# ===========================================================================

_FRAGILE_PATTERNS = [
    (
        r"find_element\s*\(\s*By\.(?:XPATH|CSS_SELECTOR)",
        "Selenium 硬编码选择器",
    ),
    (r"\.select\s*\(\s*[\"'][^\"']*[\"']\s*\)", "CSS 硬编码选择器"),
    (r"driver\.find_element", "WebDriver 硬编码定位"),
    (
        r"(?:url|endpoint|host)\s*=\s*[\"']https?://[^\"']+[\"']",
        "硬编码 URL",
    ),
    (r"sleep\s*\(\s*\d+\s*\)", "硬编码等待时间"),
]

_ERROR_HANDLING = [
    (r"try\s*:", "try 块"),
    (r"except\s+\w+", "具体异常捕获"),
    (r"finally\s*:", "finally 清理"),
    (r"raise\s+\w+", "主动抛出异常"),
]


def _scan_ooda(
    files: list[Path], source_text: str, task: str,
) -> str:
    findings: list[str] = []
    fragile_count = 0
    for pattern, label in _FRAGILE_PATTERNS:
        count = len(re.findall(pattern, source_text, re.IGNORECASE))
        if count:
            fragile_count += count
            findings.append(f"  - {label}: {count} 处")
    if fragile_count:
        findings.insert(0, f"- 脆弱模式: {fragile_count} 处")
    error_count = 0
    for pattern, label in _ERROR_HANDLING:
        count = len(re.findall(pattern, source_text, re.IGNORECASE))
        if count:
            error_count += count
    findings.append(
        f"- 错误处理: {error_count} 处" if error_count
        else "- 错误处理: 无（极易崩溃）"
    )
    ooda_stages = sum([
        bool(re.findall(r"(?:observe|monitor|detect)", source_text, re.IGNORECASE)),
        bool(re.findall(r"(?:orient|analyze|judge)", source_text, re.IGNORECASE)),
        bool(re.findall(r"(?:decide|choose|plan)", source_text, re.IGNORECASE)),
        bool(re.findall(r"(?:act|execute|perform)", source_text, re.IGNORECASE)),
    ])
    findings.append(f"- OODA 覆盖: {ooda_stages}/4")
    fragility = fragile_count * 10 + (0 if error_count else 30) + (4 - ooda_stages) * 10
    level = (
        "CRITICAL" if fragility > 80 else "HIGH" if fragility > 50
        else "MEDIUM" if fragility > 25 else "LOW"
    )
    findings.append(f"- 脆弱性评分: {fragility} ({level})")
    if task:
        findings.append(f"- 任务: {task[:200]}")
    return "\n".join(findings)


_OODA_SYSTEM = """\
You are a Mission Command architect implementing the OODA \
(Observe-Orient-Decide-Act) loop for resilient AI agent design.

## Output Format
1. Commander's Intent (one sentence goal)
2. OODA Loop Design (each stage: implementation, failure modes, \
recovery)
3. Self-Healing Mechanisms (failure detection, auto-retry, fallback, \
self-repair)
4. Anti-Fragility Checklist (no hardcoded URLs/selectors, no fixed \
waits, no single-path, no silent failures)
5. Resilience Score (1-10: adaptability, self-correction, isolation, \
degradation, recovery)
"""


class OODATool(Tool):

    @property
    def name(self) -> str:
        return "analysis_ooda"

    @property
    def description(self) -> str:
        return (
            "战场任务式指挥(OODA)：分析代码脆弱性，"
            "设计意图驱动的 OODA 循环架构，"
            "包含环境感知、异常自纠错和自我修复。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "要分析的文件或目录路径",
                },
                "task": {
                    "type": "string",
                    "description": "任务目标描述",
                    "default": "",
                },
            },
            "required": ["target"],
        }

    async def execute(
        self, *, target: str, task: str = "", **kwargs: Any,
    ) -> str:
        router = _global_router
        if router is None:
            return _router_unavailable("ooda", target)
        files = _resolve_target(target)
        if not files:
            return f"无法解析目标: {target}"
        source_text = _read_sources(files)
        scan_evidence = _scan_ooda(files, source_text, task)
        user_msg = (
            f"## 脆弱性扫描\n{scan_evidence}\n\n"
            f"## 源代码\n{source_text[:50000]}\n"
        )
        if task:
            user_msg += f"\n## 任务目标\n{task}\n"
        return await _run_analysis(router, _OODA_SYSTEM, user_msg)


# ===========================================================================
#  /probe — 黑盒探测与反幻觉协议
# ===========================================================================

# Known system/API indicators (low hallucination risk)
_KNOWN_SYSTEMS = [
    (r"(?:numpy|pandas|scipy|sklearn|tensorflow|pytorch)", "Python 数据科学栈"),
    (r"(?:react|vue|angular|next\.js|express)", "主流 Web 框架"),
    (r"(?:django|flask|fastapi|starlette)", "Python Web 框架"),
    (r"(?:unity|unreal|godot)", "游戏引擎"),
    (r"(?:win32|windows\.api|user32|kernel32)", "Windows API"),
    (r"(?:pthread|epoll|libuv|boost)", "系统级库"),
]

# Unknown/closed-source indicators (high hallucination risk)
_UNKNOWN_INDICATORS = [
    (r"(?:内部|私有|自研|闭源|proprietary|internal|private)", "私有系统"),
    (r"(?:某个|某款|某个游戏|specific game|this game)", "模糊目标引用"),
    (r"(?:没有文档|没有API|no docs|no sdk|无SDK)", "缺少文档"),
    (r"(?:逆向|反编译|reverse.?engineer|decompil)", "逆向工程"),
    (r"(?:内存地址|基址|偏移|base.?address|offset)", "内存hack"),
]

# Probe type recommendations
_PROBE_TYPES = {
    "reflection": [
        (r"(?:C#|csharp|\.NET|unity|mono)", "反射遍历对象树"),
        (r"(?:java|kotlin|android)", "Java 反射"),
        (r"(?:python|inspect|dir\(\))", "Python inspect 模块"),
    ],
    "memory": [
        (r"(?:内存|memory|address|指针|pointer)", "内存特征码扫描"),
        (r"(?:cheat.?engine|cheat.?table|trainer)", "CE 表扫描"),
        (r"(?:hook|detour|inject)", "API Hook/注入"),
    ],
    "network": [
        (r"(?:抓包|抓取|packet|wireshark|fiddler)", "网络抓包监听"),
        (r"(?:API|接口|endpoint|REST|websocket)", "API 探测"),
        (r"(?:protobuf|grpc|thrift)", "协议逆向"),
    ],
    "file": [
        (r"(?:配置|config|ini|yaml|json|xml)", "配置文件扫描"),
        (r"(?:存档|save|archive|pak|asset)", "资源文件解析"),
        (r"(?:log|日志|debug|trace)", "日志分析"),
    ],
}


def _scan_probe(task: str, context: str) -> str:
    """probe 模式静态扫描：评估目标系统已知性和幻觉风险."""
    findings: list[str] = []
    combined = (task + " " + context).lower()

    # 1. Check if target is a known system
    known_matches: list[str] = []
    for pattern, label in _KNOWN_SYSTEMS:
        if re.search(pattern, combined, re.IGNORECASE):
            known_matches.append(label)

    if known_matches:
        findings.append(
            f"- 已知系统特征: {', '.join(known_matches)}"
        )
        findings.append("  → 幻觉风险: 低（有公开文档和 SDK）")
    else:
        findings.append("- 已知系统特征: 未匹配")
        findings.append("  → 幻觉风险: 中-高（AI 可能编造 API）")

    # 2. Check for unknown/closed-source indicators
    unknown_matches: list[str] = []
    for pattern, label in _UNKNOWN_INDICATORS:
        if re.search(pattern, combined, re.IGNORECASE):
            unknown_matches.append(label)

    if unknown_matches:
        findings.append(
            f"- ⚠️ 未知系统特征: {', '.join(unknown_matches)}"
        )
        findings.append("  → 必须使用探测优先策略，禁止直接编写业务代码")
    else:
        findings.append("- 未知系统特征: 未检测到")

    # 3. Recommend probe type
    findings.append("- 推荐探测方式:")
    for probe_type, patterns in _PROBE_TYPES.items():
        for pattern, desc in patterns:
            if re.search(pattern, combined, re.IGNORECASE):
                findings.append(f"  - {probe_type}: {desc}")
                break

    # 4. Hallucination risk score
    risk = 0
    if not known_matches:
        risk += 40
    if unknown_matches:
        risk += 30
    if not context.strip():
        risk += 20
    level = (
        "CRITICAL" if risk > 60
        else "HIGH" if risk > 40
        else "MEDIUM" if risk > 20
        else "LOW"
    )
    findings.append(f"\n- 幻觉风险评分: {risk} ({level})")
    if risk > 40:
        findings.append(
            "  → 强烈建议: 先运行探测脚本收集真实信息，"
            "再基于实际返回结果开发"
        )

    return "\n".join(findings)


_PROBE_SYSTEM = """\
You are a Black-Box Probe architect implementing anti-hallucination \
protocols for unknown/closed-source systems.

## Core Principle
**NEVER guess APIs, class names, memory addresses, or function \
signatures for systems you don't have documentation for.** Instead, \
write reconnaissance scripts that discover the real interfaces.

## The 3-Phase Protocol

### Phase 1: Probe Script Generation
Write a SAFE, HARMLESS reconnaissance script that:
- Uses reflection/introspection to enumerate available classes/methods
- Scans memory for known patterns (if applicable)
- Captures network traffic to discover API endpoints
- Dumps configuration files or log outputs
- **MUST be non-destructive** — read-only, no writes or modifications

Output a complete, runnable probe script with:
- Language selection based on the target (C# for Unity, Python for \
general, C for memory)
- Clear instructions on how to run it
- What output to expect
- What to do with the output (feed it back for Phase 2)

### Phase 2: Information Extraction Template
Provide a template for the user to paste the probe output:
- What fields to look for
- How to identify the real API names vs noise
- What to extract and bring back

### Phase 3: Development Plan (AFTER probe results)
Outline what you'll do with the real information:
- How to map discovered APIs to the user's requirements
- What the implementation will look like
- What assertions to add to catch future API changes

## Anti-Hallucination Rules
1. If you don't know the exact API, say "UNKNOWN — probe required"
2. Never fabricate function names, class names, or memory offsets
3. Always include a verification step in generated code
4. If the user provides probe results, validate them before coding
5. Mark every assumption clearly as [ASSUMPTION — verify]

## Output Format
1. Risk assessment (how much do we NOT know?)
2. Probe script (complete, runnable, non-destructive)
3. Execution instructions
4. Information extraction template
5. Development plan (conditional on probe results)
"""


class ProbeTool(Tool):

    @property
    def name(self) -> str:
        return "analysis_probe"

    @property
    def description(self) -> str:
        return (
            "黑盒探测与反幻觉协议：面对闭源/未知系统时，"
            "禁止凭空编造业务代码，先生成无害的探测脚本"
            "（反射遍历、内存扫描、网络抓包），"
            "收集真实系统信息后再进行开发。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "要开发的功能描述",
                },
                "context": {
                    "type": "string",
                    "description": "已知的系统信息（SDK、文档片段等）",
                    "default": "",
                },
            },
            "required": ["task"],
        }

    async def execute(
        self, *, task: str, context: str = "", **kwargs: Any,
    ) -> str:
        router = _global_router
        if router is None:
            return _router_unavailable("probe", task[:200])
        scan_evidence = _scan_probe(task, context)
        user_msg = f"## 开发任务\n{task}\n"
        user_msg += f"\n## 探测扫描\n{scan_evidence}\n"
        if context:
            user_msg += f"\n## 已知系统信息\n{context}\n"
        return await _run_analysis(router, _PROBE_SYSTEM, user_msg)


# ===========================================================================
#  /hook — 底层逆向与插桩推演协议
# ===========================================================================

# Target type detection
_TARGET_TYPES = {
    "native_cpp": [
        (r"(?:C\+\+|cpp|native|unreal engine|directx|vulkan)", "原生 C++ 编译"),
        (r"(?:\.exe|\.dll|\.so|\.sys)", "原生二进制文件"),
        (r"(?:3A|AAA|unreal|虚幻)", "3A 游戏引擎"),
    ],
    "dotnet": [
        (r"(?:C#|csharp|\.NET|unity|mono|il2cpp)", ".NET/C# 平台"),
        (r"(?:assembly-csharp|dnspy|ilspy)", ".NET 反编译特征"),
        (r"(?:原神|genshin|honkai)", "Unity 游戏"),
    ],
    "java": [
        (r"(?:java|kotlin|android|apk|dex)", "Java/Android 平台"),
        (r"(?:jadx|smali|dalvik)", "Android 逆向特征"),
    ],
    "wasm": [
        (r"(?:wasm|webassembly|emscripten)", "WebAssembly"),
        (r"(?:\.wasm|wasm2wat)", "WASM 文件"),
    ],
}

# Anti-debug/anti-cheat indicators
_ANTI_DEBUG_PATTERNS = [
    (r"(?:anti.?cheat|EAC|BattlEye|VAC|Easy.?Anti)", "商业反作弊系统"),
    (r"(?:Themida|VMProtect|Enigma|UPX|ASPack)", "加壳/混淆保护"),
    (r"(?:IsDebuggerPresent|NtQueryInformationProcess)", "反调试 API"),
    (r"(?:integrity.?check|signature.?verify)", "完整性校验"),
    (r"(?:kernel.?driver|ring.?0|驱动)", "内核级保护"),
]


def _scan_hook(task: str) -> str:
    """hook 模式静态扫描：识别目标类型和反调试保护."""
    findings: list[str] = []
    task_lower = task.lower()

    # 1. Detect target type
    target_matches: list[tuple[str, str]] = []
    for ttype, patterns in _TARGET_TYPES.items():
        for pattern, label in patterns:
            if re.search(pattern, task_lower, re.IGNORECASE):
                target_matches.append((ttype, label))
                break

    if target_matches:
        findings.append("- 目标平台:")
        for ttype, label in target_matches:
            findings.append(f"  - {ttype}: {label}")
    else:
        findings.append("- 目标平台: 未明确指定（将给出通用方案）")

    # 2. Recommend approach based on target type
    approaches: dict[str, list[str]] = {
        "native_cpp": [
            "内存特征码扫描 (Signature Scanning)",
            "指针链追踪 (Pointer Chain Tracing)",
            "API Hooking via Detours/MinHook",
            "硬件断点 (Hardware Breakpoints)",
        ],
        "dotnet": [
            "dnSpy/ILSpy 反编译还原源码",
            "HarmonyLib 运行时补丁",
            "反射直接调用内部方法",
            "Il2CppDumper 提取元数据",
        ],
        "java": [
            "jadx/smali 反编译",
            "Xposed/Frida 运行时 Hook",
            "dex 修改与重打包",
            "protobuf/flatbuffers 协议逆向",
        ],
        "wasm": [
            "wasm2wat 反编译为 WAT",
            "浏览器 DevTools 断点调试",
            "内存 inspect + hook",
            "wasm-decompile 还原伪代码",
        ],
    }
    findings.append("- 推荐侦测手段:")
    matched_types = set(t for t, _ in target_matches)
    if matched_types:
        for ttype in matched_types:
            for approach in approaches.get(ttype, []):
                findings.append(f"  - [{ttype}] {approach}")
    else:
        for approach in approaches["native_cpp"]:
            findings.append(f"  - {approach}")

    # 3. Detect anti-debug protections
    anti_debug: list[str] = []
    for pattern, label in _ANTI_DEBUG_PATTERNS:
        if re.search(pattern, task_lower, re.IGNORECASE):
            anti_debug.append(label)

    if anti_debug:
        findings.append(
            f"- ⚠️ 反调试保护: {len(anti_debug)} 种"
        )
        for ad in anti_debug:
            findings.append(f"  - {ad}")
        findings.append(
            "  → 需要反反调试策略（驱动级 Hook 或虚拟化绕过）"
        )
    else:
        findings.append("- 反调试保护: 未提及（仍需验证）")

    # 4. Complexity assessment
    complexity = len(matched_types) * 10 + len(anti_debug) * 15
    level = (
        "EXTREME" if complexity > 50
        else "HIGH" if complexity > 30
        else "MEDIUM" if complexity > 10
        else "LOW"
    )
    findings.append(f"- 逆向复杂度: {complexity} ({level})")

    return "\n".join(findings)


_HOOK_SYSTEM = """\
You are a Reverse Engineering architect implementing dynamic \
instrumentation and hooking protocols for black-box system analysis.

## Disclaimer
All techniques discussed are for authorized security research, \
compatibility testing, and educational reverse engineering only.

## Core Principle
When source code is unavailable, use the hardware and OS as your \
documentation. Memory doesn't lie — data at rest reveals structure, \
data in motion reveals behavior.

## Output Format

### 1. Target Analysis
- Compilation type (native C++ / managed .NET / Java / WASM)
- Expected protections (anti-debug, packing, obfuscation)
- Entry points for instrumentation

### 2. Reconnaissance Phase
Based on target type, provide specific reconnaissance steps:

**Native C++:**
- Signature scanning patterns (byte arrays to search for)
- Pointer chain: how to find base → offset → target
- Memory differential technique (scan, change, rescan)

**Managed (.NET/Unity):**
- dnSpy/ILSpy decompilation workflow
- Reflection-based API discovery
- HarmonyLib patch template

**Java/Android:**
- jadx decompilation + smali analysis
- Frida hook scripts for runtime interception
- Network traffic interception

### 3. Instrumentation Design
- Where to place hooks (which functions/APIs)
- What data to capture (parameters, return values, timing)
- How to inject the instrumentation (DLL inject, Frida attach, etc.)
- Complete hook script in appropriate language

### 4. Anti-Debug Evasion (if applicable)
- How to detect anti-debug checks
- Bypass strategies (patching, driver-level, VM-based)
- Risk assessment of each bypass method

### 5. Data Extraction Pipeline
- How captured data maps to the original task
- What format to export results
- How to verify correctness of extracted data

Provide concrete code examples. Every recommendation must be \
implementable with publicly available tools.
"""


class HookTool(Tool):

    @property
    def name(self) -> str:
        return "analysis_hook"

    @property
    def description(self) -> str:
        return (
            "底层逆向与插桩推演：根据目标程序的编译特性"
            "（原生C++/C#/Java/WASM），设计动态侦测方案，"
            "包含内存基址定位、API Hooking 和反调试规避。"
            "仅用于安全研究与合规逆向工程。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "逆向分析目标描述",
                },
                "target_type": {
                    "type": "string",
                    "description": "目标类型提示（可选）",
                    "default": "",
                },
            },
            "required": ["task"],
        }

    async def execute(
        self, *, task: str, target_type: str = "", **kwargs: Any,
    ) -> str:
        router = _global_router
        if router is None:
            return _router_unavailable("hook", task[:200])
        combined = f"{task} {target_type}".strip()
        scan_evidence = _scan_hook(combined)
        user_msg = (
            f"## 逆向目标\n{task}\n\n"
            f"## 侦测扫描\n{scan_evidence}\n"
        )
        return await _run_analysis(router, _HOOK_SYSTEM, user_msg)


# ===========================================================================
#  /vision — AI 视觉数据提取协议
# ===========================================================================

# Anti-scraping indicators
_ANTI_SCRAPE_PATTERNS = [
    (r"(?:验证码|captcha|滑块|slider|recaptcha|hCaptcha)", "人机验证"),
    (r"(?:封.?IP|rate.?limit|429|too.?many|频率限制)", "IP/频率封锁"),
    (r"(?:Cloudflare|WAF|DDoS.?protect|Akamai)", "CDN/WAF 防护"),
    (r"(?:login|登录|cookie|session|token)", "登录墙"),
    (r"(?:动态加载|lazy.?load|无限滚动|SPA)", "动态渲染"),
    (r"(?:字体加密|CSS偏移|反爬|anti.?scrape|字体反爬)", "前端反爬"),
    (r"(?:加密|encrypt|obfusc|混淆|解密)", "数据加密"),
]

# Data type detection
_DATA_TYPES = {
    "table": [
        (r"(?:表格|table|报表|榜单|排名|list)", "表格数据"),
        (r"(?:财报|income|balance|cashflow)", "财务报表"),
    ],
    "chart": [
        (r"(?:K线|candlestick|图表|chart|走势|趋势)", "图表/走势"),
        (r"(?:MACD|RSI|布林|均线|BOLL)", "技术指标图"),
    ],
    "text": [
        (r"(?:新闻|公告|公告|文章|正文)", "文本内容"),
        (r"(?:评论|comment|review|舆情)", "评论/舆情"),
    ],
    "number": [
        (r"(?:价格|price|股价|市值|PE|EPS)", "数值型数据"),
        (r"(?:成交量|volume|换手率|涨跌幅)", "交易数值"),
    ],
}


def _scan_vision(task: str) -> str:
    """vision 模式静态扫描：评估反爬虫难度和视觉提取可行性."""
    findings: list[str] = []
    task_lower = task.lower()

    # 1. Detect anti-scraping measures
    anti_scrape: list[str] = []
    for pattern, label in _ANTI_SCRAPE_PATTERNS:
        if re.search(pattern, task_lower, re.IGNORECASE):
            anti_scrape.append(label)

    if anti_scrape:
        findings.append(
            f"- 反爬虫措施: {len(anti_scrape)} 种"
        )
        for as_type in anti_scrape:
            findings.append(f"  - {as_type}")
        findings.append(
            "  → 传统 HTTP/API 方案可行性: 低"
        )
    else:
        findings.append("- 反爬虫措施: 未提及（传统方案可能可行）")

    # 2. Detect data types to extract
    data_types: list[str] = []
    for dtype, patterns in _DATA_TYPES.items():
        for pattern, label in patterns:
            if re.search(pattern, task_lower, re.IGNORECASE):
                data_types.append(f"{dtype}: {label}")
                break

    if data_types:
        findings.append("- 需要提取的数据类型:")
        for dt in data_types:
            findings.append(f"  - {dt}")
    else:
        findings.append("- 数据类型: 需要进一步明确")

    # 3. Recommend vision pipeline components
    findings.append("- 推荐视觉管线:")
    if any(t.startswith("table") for t in data_types):
        findings.append("  - 表格检测: YOLO/LayoutLM → 单元格定位 → OCR")
    if any(t.startswith("chart") for t in data_types):
        findings.append("  - 图表解析: 截屏 → 颜色/形态检测 → 数值重建")
    if any(t.startswith("text") for t in data_types):
        findings.append("  - 文本提取: 截屏 → OCR (PaddleOCR/Tesseract)")
    if any(t.startswith("number") for t in data_types):
        findings.append("  - 数值精确提取: 区域裁剪 → OCR → 数字校验")
    if not any(
        t.startswith(x) for t in data_types for x in ("table", "chart", "text", "number")
    ):
        findings.append("  - 通用: 截屏 → 多模态LLM直接提取")

    # 4. Vision feasibility score
    vision_score = len(anti_scrape) * 15 + len(data_types) * 10
    level = (
        "IDEAL" if vision_score > 40
        else "GOOD" if vision_score > 20
        else "VIABLE" if vision_score > 10
        else "OVERKILL"
    )
    findings.append(f"- 视觉方案适合度: {vision_score} ({level})")
    if level in ("IDEAL", "GOOD"):
        findings.append(
            "  → 强烈推荐视觉方案：反爬虫严重，传统方法不可行"
        )
    elif level == "OVERKILL":
        findings.append(
            "  → 传统 API/HTTP 方案可能更优，视觉方案作为备选"
        )

    return "\n".join(findings)


_VISION_SYSTEM = """\
You are an AI Vision Data Extraction architect designing screen-based \
data pipelines that bypass anti-scraping protections by "observing" \
data like a human would.

## Core Principle
When APIs are blocked, rate-limited, or encrypted, switch from \
"requesting data" to "looking at data." The screen is the universal \
API — every system eventually renders data visually.

## The Vision Pipeline

### Stage 1: Capture
Design the screen capture strategy:
- Full page vs region-of-interest (ROI) cropping
- Capture frequency (real-time vs periodic)
- Headless browser (Playwright/Puppeteer) vs physical display
- Screenshot coordination with page load timing

### Stage 2: Detect
Identify where the data lives on screen:
- Layout analysis (table boundaries, chart regions, text blocks)
- Use YOLO/LayoutLM for structured layout detection
- Use color/edge detection for chart element isolation
- Template matching for recurring UI elements

### Stage 3: Extract
Pull structured data from detected regions:
- OCR for text/numbers (PaddleOCR, Tesseract, EasyOCR)
- Chart axis reading + interpolation for chart data
- Table cell segmentation + row/column alignment
- Multi-modal LLM as fallback for complex layouts

### Stage 4: Validate & Structure
Ensure extracted data is correct:
- Cross-validation (do numbers sum correctly? do dates align?)
- Type casting (string → float, date parsing)
- Delta checking (does this match known previous values?)
- Confidence scoring (how certain is the extraction?)

### Stage 5: Output
Format the final structured data:
- JSON/CSV with consistent schema
- Timestamp and source metadata
- Diff against previous extraction for change detection

## Architecture Comparison

| Approach | Speed | Accuracy | Anti-Scrape Resilience | Cost |
|----------|-------|----------|----------------------|------|
| HTTP/API | Fast | High | None | Low |
| Browser Automation | Medium | High | Low | Medium |
| AI Vision | Slow | Medium-High | High | High |

## Output Format
1. Anti-scrape assessment
2. Vision pipeline design (5 stages with code examples)
3. Accuracy optimization strategies
4. Fallback mechanisms
5. Cost/speed trade-off analysis
6. Comparison with alternative approaches
"""


class VisionTool(Tool):

    @property
    def name(self) -> str:
        return "analysis_vision"

    @property
    def description(self) -> str:
        return (
            "AI 视觉数据提取：当传统 API/HTTP 被反爬虫封锁时，"
            "设计\"像人一样看屏幕\"的视觉管线——"
            "截屏→检测→OCR→结构化，绕过软件层限制。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "要提取的数据来源和目标描述",
                },
            },
            "required": ["task"],
        }

    async def execute(
        self, *, task: str, **kwargs: Any,
    ) -> str:
        router = _global_router
        if router is None:
            return _router_unavailable("vision", task[:200])
        scan_evidence = _scan_vision(task)
        user_msg = (
            f"## 数据提取需求\n{task}\n\n"
            f"## 视觉方案扫描\n{scan_evidence}\n"
        )
        return await _run_analysis(router, _VISION_SYSTEM, user_msg)


# ===========================================================================
#  /spar — 对抗性自博弈 (Adversarial Self-Play)
# ===========================================================================

# Reward hacking shortcuts that indicate "cheating" instead of real fixes
_REWARD_HACK_PATTERNS = [
    (r"if\s*\(\s*(?:len|size|length)\s*.*>\s*\d+\s*\)\s*(?:return|break|continue)",
     "大小检查直接返回 — 可能跳过处理而非修复根因"),
    (r"except\s*(?:Exception|BaseException)\s*:\s*(?:return|pass)",
     "裸 except 静默吞掉异常 — 可能掩盖真实 Bug"),
    (r"try:\s*\n[^#\n]*except:\s*\n\s*pass",
     "try/except pass — 无条件忽略所有错误"),
    (r"(?:TODO|FIXME|HACK|XXX).*(?:bypass|skip|ignore|workaround)",
     "绕行式临时注释 — 非正式修复"),
    (r"if\s+False:",
     "死代码分支 — 可能是删除功能以满足测试"),
    (r"return\s+(?:None|True|False|\"\"|0)\s*#\s*(?:pass|bypass|skip)",
     "硬编码返回 + 绕过注释"),
    (r"assert\s+False",
     "断言失败式短路 — 放弃而非修复"),
    (r"#\s*noqa|#\s*type:\s*ignore|#\s*pylint:\s*disable",
     "静默压制 Linter/类型检查 — 可能掩盖问题"),
]

# Vulnerability surface patterns for adversarial targeting
_VULN_SURFACE_PATTERNS = [
    (r"(?:malloc|calloc|realloc|new)\s*\(", "堆内存分配"),
    (r"(?:free|delete)\s*[\(\[]?", "堆内存释放"),
    (r"(?:strcpy|strcat|sprintf|gets)\s*\(", "不安全字符串操作"),
    (r"(?:memcpy|memmove)\s*\([^,]+,\s*[^,]+,\s*[^)]+\)", "内存拷贝"),
    (r"(?:fopen|fwrite|fread|open)\s*\(", "文件 I/O"),
    (r"(?:socket|connect|bind|accept|recv|send)\s*\(", "网络 I/O"),
    (r"(?:thread|Thread|spawn|fork|asyncio)\s*[\(\[]?", "并发/多线程"),
    (r"(?:subprocess|os\.system|os\.popen|exec|eval)\s*\(", "命令执行"),
    (r"(?:sql|cursor|execute)\s*\(", "数据库操作"),
    (r"(?:json\.loads|yaml\.load|pickle\.loads)\s*\(", "反序列化"),
    (r"\[\s*[^\]]*\s*\]\s*=|\.append|\.insert", "数组/列表写入"),
    (r"(?:int|float)\s*\([^)]*\)", "类型转换 — 可能溢出/精度丢失"),
]

# Adversarial input strategies mapped to vulnerability types
_ADVERSARIAL_INPUT_STRATEGIES = {
    "堆内存分配": [
        "超大输入 (>2GB) 测试内存耗尽",
        "零长度输入触发边界分配",
        "交错分配/释放制造碎片",
    ],
    "堆内存释放": [
        "重复释放 (double free) 同一指针",
        "释放后使用 (use-after-free)",
        "释放 NULL 指针",
    ],
    "不安全字符串操作": [
        "超长字符串 (100K+) 缓冲区溢出",
        "嵌入 NULL 字节截断",
        "Unicode/多字节混合编码",
    ],
    "内存拷贝": [
        "源/目标重叠区域拷贝",
        "拷贝长度 > 实际缓冲区",
        "空指针 + 非零长度",
    ],
    "文件 I/O": [
        "符号链接指向敏感文件",
        "并发读写同一文件",
        "文件名含路径遍历 (../../etc/passwd)",
    ],
    "网络 I/O": [
        "半开连接耗尽端口",
        "畸形 HTTP 请求头",
        "超时 + 重试风暴",
    ],
    "并发/多线程": [
        "竞态条件: 1000 线程同时写同一变量",
        "死锁: 按相反顺序获取锁",
        "活锁: 高优先级线程持续抢占",
    ],
    "命令执行": [
        "命令注入: ; rm -rf /",
        "环境变量劫持",
        "参数中嵌入反引号/管道符",
    ],
    "数据库操作": [
        "SQL 注入: ' OR 1=1 --",
        "超长查询字段",
        "并发事务死锁",
    ],
    "反序列化": [
        "恶意 pickle 字节码",
        "循环引用 JSON 对象",
        "深度嵌套 (>100层) 结构",
    ],
    "数组/列表写入": [
        "越界索引访问",
        "超大数组内存耗尽",
        "负索引边界",
    ],
    "类型转换": [
        "整数溢出: sys.maxsize + 1",
        "NaN / Inf 浮点输入",
        "非数字字符串转数值",
    ],
}

_NIHILISM_SIGNALS = [
    "删除所有功能代码以满足安全要求",
    "空函数体 (只有 return/pass)",
    "核心逻辑被条件编译排除",
    "所有 public 方法改为 private 且无调用者",
    "测试中全用 assert True",
]


def _scan_spar(target: str) -> str:
    """Scan code for adversarial self-play readiness — vulnerability surface,
    reward hacking risk, and nihilism detection."""
    findings: list[str] = []
    source = _read_sources(target)

    if not source.strip():
        return "⚠️ 未找到可分析的源代码。"

    lines = source.split("\n")
    total_lines = len(lines)

    # --- 1. Vulnerability surface mapping ---
    findings.append("## 1. 攻击面扫描 (Vulnerability Surface)")
    vuln_hits: dict[str, list[int]] = {}
    for pattern, label in _VULN_SURFACE_PATTERNS:
        for i, line in enumerate(lines, 1):
            if re.search(pattern, line):
                vuln_hits.setdefault(label, []).append(i)

    if vuln_hits:
        total_vuln_points = sum(len(v) for v in vuln_hits.values())
        findings.append(
            f"- 共检测到 **{total_vuln_points}** 处潜在攻击点，"
            f"覆盖 **{len(vuln_hits)}** 个类别："
        )
        for label, line_nos in sorted(
            vuln_hits.items(), key=lambda x: -len(x[1])
        ):
            samples = line_nos[:5]
            loc_str = ", ".join(str(n) for n in samples)
            if len(line_nos) > 5:
                loc_str += f" 等 {len(line_nos)} 处"
            findings.append(f"  - **{label}**: {loc_str}")
    else:
        findings.append("- 未检测到明显的底层操作，攻击面较低")
    findings.append("")

    # --- 2. Adversarial input strategy recommendation ---
    findings.append("## 2. 对抗输入策略推荐")
    recommended = 0
    for label in vuln_hits:
        strategies = _ADVERSARIAL_INPUT_STRATEGIES.get(label, [])
        if strategies:
            recommended += 1
            findings.append(f"  **[{label}]**")
            for s in strategies:
                findings.append(f"    - {s}")
    if recommended == 0:
        findings.append("- 代码较为安全，建议使用通用模糊测试")
    findings.append("")

    # --- 3. Reward hacking detection ---
    findings.append("## 3. 奖励作弊检测 (Reward Hacking Risk)")
    hack_hits: list[tuple[str, int, str]] = []
    for pattern, desc in _REWARD_HACK_PATTERNS:
        for i, line in enumerate(lines, 1):
            m = re.search(pattern, line, re.IGNORECASE)
            if m:
                hack_hits.append((desc, i, line.strip()))

    if hack_hits:
        findings.append(
            f"- ⚠️ 发现 **{len(hack_hits)}** 处疑似奖励作弊模式："
        )
        for desc, line_no, line_text in hack_hits[:8]:
            short = line_text[:80] + ("..." if len(line_text) > 80 else "")
            findings.append(f"  - L{line_no}: {desc}")
            findings.append(f"    `{short}`")
        hack_score = min(len(hack_hits) / max(total_lines / 50, 1), 1.0)
        findings.append(
            f"- 作弊风险评分: **{hack_score:.0%}** "
            f"(基于 {len(hack_hits)} 处 / {total_lines} 行)"
        )
    else:
        findings.append("- ✅ 未检测到明显的奖励作弊模式")
    findings.append("")

    # --- 4. Nihilism detection ---
    findings.append("## 4. 虚无主义检测 (Nihilism Risk)")
    import ast as _ast

    # Empty function bodies
    empty_funcs = 0
    try:
        tree = _ast.parse(source)
        for node in _ast.walk(tree):
            if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                body = node.body
                if len(body) == 1 and isinstance(body[0], _ast.Pass):
                    empty_funcs += 1
                elif (
                    len(body) == 1
                    and isinstance(body[0], _ast.Return)
                    and (
                        body[0].value is None
                        or (isinstance(body[0].value, _ast.Constant)
                            and body[0].value.value in (None, True, False, 0, ""))
                    )
                ):
                    empty_funcs += 1
    except SyntaxError:
        empty_funcs = -1

    if empty_funcs > 0:
        findings.append(
            f"- ⚠️ 发现 **{empty_funcs}** 个空函数体 — 可能是删除功能后的残留"
        )
    elif empty_funcs == 0:
        findings.append("- ✅ 未发现空函数体")

    # Check for nihilism signals in comments
    for signal in _NIHILISM_SIGNALS:
        for line in lines:
            if signal in line:
                findings.append(f"  - 虚无信号: `{signal}`")
                break
    findings.append("")

    # --- 5. Code complexity ---
    findings.append("## 5. 代码复杂度")
    import_count = sum(
        1 for line in lines if re.match(r"\s*(?:import|from)\s+", line)
    )
    func_count = 0
    class_count = 0
    try:
        tree = _ast.parse(source)
        for node in _ast.walk(tree):
            if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                func_count += 1
            elif isinstance(node, _ast.ClassDef):
                class_count += 1
    except SyntaxError:
        pass

    findings.append(
        f"- 文件: {total_lines} 行 | {import_count} imports | "
        f"{class_count} classes | {func_count} functions"
    )
    findings.append("")

    # --- 6. Self-play readiness score ---
    vuln_score = min(len(vuln_hits) / 6.0, 1.0)
    hack_risk = min(len(hack_hits) / max(total_lines / 100, 1), 1.0)
    nihilism_risk = min(empty_funcs / max(func_count, 1), 1.0) if func_count > 0 else 0.0

    readiness = (1.0 - nihilism_risk) * 0.4 + vuln_score * 0.35 + (1.0 - hack_risk) * 0.25
    readiness = max(0.0, min(1.0, readiness))

    findings.append("## 6. 自博弈就绪度评分")
    findings.append(
        f"- **综合评分: {readiness:.0%}**"
    )
    findings.append(f"  - 攻击面丰富度: {vuln_score:.0%}")
    findings.append(f"  - 作弊免疫力: {(1.0 - hack_risk):.0%}")
    findings.append(f"  - 虚无主义免疫力: {(1.0 - nihilism_risk):.0%}")
    findings.append(
        "- "
        + (
            "✅ 适合启动对抗性自博弈"
            if readiness >= 0.6
            else "⚠️ 建议先清理代码再启动自博弈"
        )
    )

    return "\n".join(findings)


_SPAR_SYSTEM = """\
你是一位对抗性自博弈架构师 (Adversarial Self-Play Architect)。
你的任务是将 GAN（生成式对抗网络）思想应用于软件开发：设计一套
蓝军（写代码）vs 红军（搞破坏）的自动化对抗流水线。

## 核心架构

### 1. 蓝军 (The Builder)
- 目标：编写通过所有测试的功能代码
- 策略：从核心逻辑开始，逐步添加防御性代码
- 约束：不能通过"绕过"来满足测试，必须真正解决问题

### 2. 红军 (The Breaker)
- 目标：找到代码中的一切漏洞
- 策略：基于静态扫描发现的攻击面，生成极端测试输入
- 约束：攻击必须基于物理世界的真实威胁，不能虚无主义式地
  要求"绝对安全"

### 3. 物理锚点 (The Oracle)
- 所有验证必须基于真实执行结果，不能只靠 LLM "嘴炮"
- 代码必须在真实环境（容器/沙盒）中编译运行
- 使用 Valgrind/GDB/Sanitizer 等工具获取物理证据
- 核心转储 (core dump)、段错误 (segfault)、内存泄漏报告
  是不可伪造的物理判决

## 必须防止的两种绝症

### 绝症一：奖励作弊 (Reward Hacking)
蓝军发现捷径：加 if (size > 1GB) return "ok" 来"通过"大文件测试，
实际并未解决内存管理问题。

**对策：**
- 红军测试不能只看 return code，必须验证输出正确性
- 引入"功能完整性断言"：核心业务逻辑不能被跳过
- 检测"防御性短路"：异常处理中直接返回成功

### 绝症二：虚无主义 (Nihilism)
红军过于变态，蓝军为了安全把所有功能都删了。空代码零 Bug。

**对策：**
- 定义不可妥协的功能基线 (Functional Baseline)
- 每轮迭代必须有功能验收测试 (not just safety tests)
- 设置"功能保留率"指标，低于阈值视为虚无主义发作

## 自博弈流水线设计

### Round N:
1. **蓝军出击**: 基于当前代码 + 红军上轮反馈，编写修复/新功能
2. **编译验证**: 代码必须在真实环境编译通过 (Ground Truth #1)
3. **红军出击**: 基于扫描到的攻击面，生成极端输入并执行
4. **物理判决**: 执行结果由工具 (Valgrind/ASAN) 而非 LLM 判定
5. **收敛检查**: 功能完整性 ✅ + 零崩溃 ✅ + 无奖励作弊 ✅ → 终止

## 输出格式

1. **蓝军建设方案** — 需要编写的功能模块和防御性代码
2. **红军攻击策略** — 基于扫描发现的攻击面，生成具体测试方案
3. **物理沙盒配置** — Dockerfile/编译命令/Sanitizer 配置
4. **收敛准则** — 什么条件下停止迭代
5. **作弊防护** — 针对检测到的作弊风险，设计具体防护措施
6. **迭代预估** — 建议的迭代轮数和每轮重点
"""


class SparTool(Tool):

    @property
    def name(self) -> str:
        return "analysis_spar"

    @property
    def description(self) -> str:
        return (
            "对抗性自博弈 (GAN for Code)：蓝军写代码 vs 红军搞破坏，"
            "以物理沙盒执行结果作为绝对锚点，迭代 N 轮直到代码坚不可摧。"
            "防止奖励作弊与虚无主义，交付真正经过对抗验证的代码。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "要进行对抗自博弈的目标代码路径或功能描述",
                },
            },
            "required": ["task"],
        }

    async def execute(
        self, *, task: str, **kwargs: Any,
    ) -> str:
        router = _global_router
        if router is None:
            return _router_unavailable("spar", task[:200])

        scan_evidence = _scan_spar(task)

        manager = _global_subagent_manager
        if manager is not None:
            return await self._execute_adversarial(
                router, manager, task, scan_evidence,
            )

        user_msg = (
            f"## 对抗目标\n{task}\n\n"
            f"## 静态扫描报告\n{scan_evidence}\n"
        )
        return await _run_analysis(router, _SPAR_SYSTEM, user_msg)

    async def _execute_adversarial(
        self,
        router: Any,
        manager: Any,
        task: str,
        scan_evidence: str,
    ) -> str:
        """Execute real adversarial self-play with blue/red agents + bus."""
        from naumi_agent.agents.base import AgentCapability
        from naumi_agent.agents.message_bus import (
            AgentMessage,
            MessagePriority,
        )
        from naumi_agent.orchestrator.subagent_manager import SubTask

        spar_caps = [AgentCapability.FILE_OPS, AgentCapability.CODE_EXEC]

        await manager.message_bus.reset()

        await manager.message_bus.blackboard_set(
            "target", task, author="orchestrator",
        )
        await manager.message_bus.blackboard_set(
            "attack_surface", scan_evidence, author="orchestrator",
        )

        manager.spawn_for_task(
            name="spar_blue_builder",
            task_description=task,
            role="builder",
            focus="根据任务要求编写健壮的代码，防御已知的攻击向量",
            max_turns=5,
            max_budget_usd=0.2,
            extra_capabilities=spar_caps,
        )
        manager.spawn_for_task(
            name="spar_red_breaker",
            task_description=task,
            role="attacker",
            focus="审查蓝军编写的代码，找到所有可能的漏洞、边界问题和攻击面",
            max_turns=5,
            max_budget_usd=0.2,
            extra_capabilities=spar_caps,
        )

        rounds_log: list[str] = []
        blue_code = ""
        total_tokens = 0
        total_cost = 0.0
        max_rounds = 3

        try:
            for round_num in range(max_rounds):
                # Blue: build/fix
                blue_task = (
                    f"## 对抗目标\n{task}\n\n"
                    f"## 静态扫描（攻击面）\n{scan_evidence}\n"
                )
                if round_num > 0 and rounds_log:
                    blue_task += (
                        f"\n## 红军上轮攻击报告\n{rounds_log[-1]}\n"
                        "请修复上述所有漏洞，同时保持功能完整。"
                    )
                if blue_code:
                    blue_task += f"\n## 当前代码\n{blue_code[:10000]}\n"

                blue_subtask = SubTask(
                    id=f"blue_r{round_num}",
                    description=blue_task,
                    agent_name="spar_blue_builder",
                )
                blue_result = await manager.delegate(blue_subtask)
                total_tokens += getattr(blue_result, "total_tokens", 0)
                total_cost += getattr(blue_result, "total_cost_usd", 0.0)

                if blue_result.status != "completed":
                    rounds_log.append(
                        f"⚠️ 蓝军第 {round_num + 1} 轮失败: "
                        f"{blue_result.error}"
                    )
                    break

                blue_code = blue_result.response or ""
                rounds_log.append(
                    f"### 蓝军 第 {round_num + 1} 轮输出\n"
                    f"{blue_code[:3000]}"
                )

                # Share blue's code on blackboard for red to read
                await manager.message_bus.blackboard_set(
                    "blue_code", blue_code, author="spar_blue_builder",
                )

                # Red: attack
                red_task = (
                    f"## 对抗目标\n{task}\n\n"
                    f"## 蓝军本轮代码\n{blue_code[:10000]}\n\n"
                    "请从以下角度全面攻击这段代码:\n"
                    "1. 边界条件（空输入、超大数据、特殊字符）\n"
                    "2. 并发/竞态条件\n"
                    "3. 资源泄漏（内存、文件句柄、连接）\n"
                    "4. 逻辑漏洞（未覆盖的分支、错误的条件）\n"
                    "5. 安全漏洞（注入、路径穿越、权限绕过）\n"
                )

                red_subtask = SubTask(
                    id=f"red_r{round_num}",
                    description=red_task,
                    agent_name="spar_red_breaker",
                )
                red_result = await manager.delegate(red_subtask)
                total_tokens += getattr(red_result, "total_tokens", 0)
                total_cost += getattr(red_result, "total_cost_usd", 0.0)

                if red_result.status != "completed":
                    rounds_log.append(
                        f"⚠️ 红军第 {round_num + 1} 轮失败: "
                        f"{red_result.error}"
                    )
                    break

                attack_report = red_result.response or ""
                rounds_log.append(
                    f"### 红军 第 {round_num + 1} 轮攻击报告\n"
                    f"{attack_report[:3000]}"
                )

                # Share red's findings on blackboard + send to blue
                await manager.message_bus.blackboard_set(
                    f"red_findings_r{round_num}",
                    attack_report[:2000],
                    author="spar_red_breaker",
                )

                # Check convergence
                has_critical = (
                    "CRITICAL" in attack_report.upper()
                    or "HIGH" in attack_report.upper()
                )

                priority = (
                    MessagePriority.HIGH if has_critical
                    else MessagePriority.LOW
                )
                await manager.message_bus.send(AgentMessage(
                    sender="spar_red_breaker",
                    topic="spar.attack_report",
                    recipient="spar_blue_builder",
                    content=attack_report[:500],
                    priority=priority,
                ))

                if not has_critical:
                    rounds_log.append(
                        "✅ 红军未发现 CRITICAL/HIGH 级别漏洞，"
                        "对抗训练收敛。"
                    )
                    break

        finally:
            manager.destroy("spar_blue_builder")
            manager.destroy("spar_red_breaker")

        bus_stats = manager.message_bus.stats()
        await manager.message_bus.reset()

        # Final synthesis
        rounds_completed = len(
            [r for r in rounds_log if "蓝军" in r and "输出" in r]
        )
        synthesis_msg = (
            f"## 对抗自博弈 SPAR 报告\n\n"
            f"**目标**: {task[:200]}\n"
            f"**对抗轮次**: {rounds_completed}\n"
            f"**总 Token**: {total_tokens}\n"
            f"**总成本**: ${total_cost:.4f}\n"
            f"**消息总线**: {bus_stats['total_messages']} 条消息, "
            f"{bus_stats['blackboard_entries']} 条共享状态\n\n"
            f"---\n\n"
            f"## 对抗过程完整记录\n\n"
        )
        for entry in rounds_log:
            synthesis_msg += f"{entry}\n\n---\n\n"

        synthesis_msg += (
            "\n请基于上述对抗过程，给出最终的综合评估：\n"
            "1. 代码是否足够健壮？\n"
            "2. 残余风险有哪些？\n"
            "3. 推荐的后续加固措施？\n"
        )

        return await _run_analysis(router, _SPAR_SYSTEM, synthesis_msg)


# ===========================================================================
#  /world — 世界模型审计 (World Model Audit)
# ===========================================================================

# State mutation patterns — functions/operations that change state
_STATE_MUTATION_PATTERNS = [
    (r"(?:self\.\w+)\s*=\s*", "实例属性赋值"),
    (r"\w+\[.+\]\s*=\s*", "字典/列表索引赋值"),
    (r"\w+\.(?:append|extend|insert|pop|remove|clear|update)\s*\(",
     "集合修改方法"),
    (r"(?:await\s+)?(?:db|database|session|cursor)\.\w+\(",
     "数据库操作"),
    (r"\w+\.(?:save|commit|write|flush|persist)\s*\(",
     "持久化写入"),
    (r"(?:global|nonlocal)\s+\w+", "全局/闭包变量修改"),
    (r"\w+\.(?:state|status|phase)\s*=\s*", "状态字段直接赋值"),
]

# State reading patterns — pure observations that don't mutate
_STATE_READ_PATTERNS = [
    (r"\w+\.(?:get|read|fetch|query|select|find|search|load)\s*\(", "读取操作"),
    (r"\w+\.(?:property|@property)", "属性访问"),
    (r"len\s*\(\s*\w+\s*\)", "长度查询"),
    (r"\w+\.(?:count|index|contains|exists)\s*\(", "存在性检查"),
]

# Causal chain indicators — function calls that represent cause→effect
_CAUSAL_PATTERNS = [
    (r"(?:if|when|on|after|before|handle|trigger|notify|emit|dispatch)\s*",
     "条件触发"),
    (r"\w+\.(?:on_|handle_|process_|before_|after_)\w+\s*\(",
     "事件处理器"),
    (r"(?:raise|throw|except|catch|error|fail)", "异常传播"),
    (r"(?:publish|subscribe|emit|listen|broadcast)\s*\(", "消息传递"),
    (r"(?:callback|handler|listener|observer)\s*=", "回调注册"),
]

# Patterns indicating missing "what-if" handling
_COUNTERFACTACTUAL_GAPS = [
    (r"\.\w+\([^)]*\)\s*(?!\s*(?:try|except|if|await))",
     "无保护的方法调用"),
    (r"\w+\[\s*\w+\s*\](?!\s*=\s*)(?!\s*if)(?!\s*try)",
     "无边界检查的索引访问"),
    (r"(?:open|connect|request)\s*\(.*\)(?!\s*(?:with|try))",
     "无上下文管理的资源获取"),
    (r"int\s*\([^)]*\)(?!\s*(?:if|try|or|and))",
     "无异常处理的类型转换"),
    (r"\.\w+\s*\([^)]*\)\s*$",
     "链式调用终点无错误处理"),
]


def _scan_world(target: str) -> str:
    """Scan a codebase as a world model — inventory state, map transitions,
    audit causal chains, detect counterfactual gaps, and assess model
    completeness."""
    import ast as _ast

    findings: list[str] = []
    source = _read_sources(target)

    if not source.strip():
        return "⚠️ 未找到可分析的源代码。"

    lines = source.split("\n")
    total_lines = len(lines)

    # Parse AST for structural analysis
    tree = None
    try:
        tree = _ast.parse(source)
    except SyntaxError:
        pass

    # --- 1. State Inventory ---
    findings.append("## 1. 状态清单 (State Inventory)")
    state_vars: dict[str, list[int]] = {}
    for pattern, label in _STATE_MUTATION_PATTERNS:
        for i, line in enumerate(lines, 1):
            if re.search(pattern, line):
                state_vars.setdefault(label, []).append(i)

    total_mutations = sum(len(v) for v in state_vars.values())
    if state_vars:
        findings.append(
            f"- 检测到 **{total_mutations}** 处状态变更操作，"
            f"覆盖 **{len(state_vars)}** 类："
        )
        for label, line_nos in sorted(
            state_vars.items(), key=lambda x: -len(x[1])
        ):
            findings.append(
                f"  - {label}: {len(line_nos)} 处 "
                f"(L{line_nos[0]}"
                f"{', L' + str(line_nos[1]) if len(line_nos) > 1 else ''}"
                f"{'...' if len(line_nos) > 2 else ''})"
            )
    else:
        findings.append("- 未检测到明显的状态变更操作")
    findings.append("")

    # --- 2. State Transition Mapping ---
    findings.append("## 2. 状态转移映射 (State Transition Map)")
    transitions: list[tuple[str, str, int]] = []
    if tree:
        for node in _ast.walk(tree):
            if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                func_name = node.name
                reads_in_func: list[str] = []
                writes_in_func: list[str] = []
                func_source = "\n".join(
                    lines[node.lineno - 1 : node.end_lineno or node.lineno]
                )
                for pattern, label in _STATE_READ_PATTERNS:
                    if re.search(pattern, func_source):
                        reads_in_func.append(label)
                for pattern, label in _STATE_MUTATION_PATTERNS:
                    if re.search(pattern, func_source):
                        writes_in_func.append(label)
                if writes_in_func:
                    read_str = (
                        ", ".join(set(reads_in_func)) if reads_in_func else "外部输入"
                    )
                    write_str = ", ".join(set(writes_in_func))
                    transitions.append(
                        (func_name, f"{read_str} → [{write_str}]", node.lineno)
                    )

    if transitions:
        findings.append(f"- 发现 **{len(transitions)}** 个状态转移函数：")
        for fname, desc, lineno in transitions[:12]:
            findings.append(f"  - `{fname}` (L{lineno}): {desc}")
        if len(transitions) > 12:
            findings.append(f"  - ... 还有 {len(transitions) - 12} 个")
    else:
        findings.append("- 未检测到明确的状态转移函数")
    findings.append("")

    # --- 3. Causal Chain Detection ---
    findings.append("## 3. 因果链分析 (Causal Chain Analysis)")
    causal_events: dict[str, list[int]] = {}
    for pattern, label in _CAUSAL_PATTERNS:
        for i, line in enumerate(lines, 1):
            if re.search(pattern, line, re.IGNORECASE):
                causal_events.setdefault(label, []).append(i)

    if causal_events:
        total_causal = sum(len(v) for v in causal_events.values())
        findings.append(
            f"- 检测到 **{total_causal}** 处因果链节点，"
            f"**{len(causal_events)}** 类："
        )
        for label, line_nos in sorted(
            causal_events.items(), key=lambda x: -len(x[1])
        ):
            findings.append(f"  - {label}: {len(line_nos)} 处")
    else:
        findings.append("- 因果链稀疏 — 系统可能是无副作用的纯函数式设计")
    findings.append("")

    # --- 4. Object Permanence Audit ---
    findings.append("## 4. 客体永久性审计 (Object Permanence)")
    # Check for state that is set but never read (lost state)
    lost_state_count = 0
    potential_lost: list[str] = []
    if tree:
        assigned_attrs: dict[str, int] = {}
        read_attrs: set[str] = set()
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Attribute) and isinstance(
                node.value, _ast.Name
            ):
                attr_key = f"{node.value.id}.{node.attr}"
                if isinstance(node.ctx, _ast.Store):
                    assigned_attrs[attr_key] = getattr(node, "lineno", 0)
                elif isinstance(node.ctx, _ast.Load):
                    read_attrs.add(attr_key)
        for attr, lineno in assigned_attrs.items():
            if attr not in read_attrs:
                lost_state_count += 1
                potential_lost.append(f"`{attr}` (L{lineno})")

    if potential_lost:
        findings.append(
            f"- ⚠️ 发现 **{lost_state_count}** 个属性被写入但从未被读取"
            f" — 可能是'消失的客体'："
        )
        for attr in potential_lost[:6]:
            findings.append(f"  - {attr}")
        if len(potential_lost) > 6:
            findings.append(f"  - ... 还有 {len(potential_lost) - 6} 个")
    else:
        findings.append("- ✅ 所有写入的属性均有读取方，客体永久性良好")
    findings.append("")

    # --- 5. Counterfactual Gap Analysis ---
    findings.append("## 5. 反事实推演缺口 (Counterfactual Gaps)")
    gap_count = 0
    gap_examples: list[tuple[str, int]] = []
    for pattern, desc in _COUNTERFACTACTUAL_GAPS:
        for i, line in enumerate(lines, 1):
            if re.search(pattern, line) and not line.strip().startswith("#"):
                gap_count += 1
                if len(gap_examples) < 8:
                    gap_examples.append((desc, i))

    if gap_examples:
        findings.append(
            f"- ⚠️ 发现 **{gap_count}** 处可能缺少'如果出错了怎么办'的处理："
        )
        for desc, lineno in gap_examples:
            short_line = lines[lineno - 1].strip()[:70]
            findings.append(f"  - L{lineno}: {desc}")
            findings.append(f"    `{short_line}`")
    else:
        findings.append("- ✅ 关键操作均有保护措施")
    findings.append("")

    # --- 6. World Model Completeness Score ---
    state_richness = min(len(state_vars) / 5.0, 1.0)
    transition_richness = min(len(transitions) / 8.0, 1.0)
    causal_density = min(
        sum(len(v) for v in causal_events.values()) / max(total_lines / 20, 1),
        1.0,
    )
    permanence_score = (
        1.0 - min(lost_state_count / max(total_mutations, 1), 0.5)
        if total_mutations > 0
        else 1.0
    )
    counterfactual_coverage = 1.0 - min(
        gap_count / max(total_lines / 30, 1), 1.0
    )

    completeness = (
        state_richness * 0.20
        + transition_richness * 0.25
        + causal_density * 0.20
        + permanence_score * 0.15
        + counterfactual_coverage * 0.20
    )
    completeness = max(0.0, min(1.0, completeness))

    findings.append("## 6. 世界模型完整度评分")
    findings.append(f"- **综合评分: {completeness:.0%}**")
    findings.append(f"  - 状态丰富度: {state_richness:.0%}")
    findings.append(f"  - 转移完备度: {transition_richness:.0%}")
    findings.append(f"  - 因果链密度: {causal_density:.0%}")
    findings.append(f"  - 客体永久性: {permanence_score:.0%}")
    findings.append(f"  - 反事实覆盖: {counterfactual_coverage:.0%}")

    if completeness >= 0.75:
        findings.append("- ✅ 系统具备较完整的世界模型，能感知状态演化")
    elif completeness >= 0.5:
        findings.append("- ⚠️ 世界模型部分建立，存在盲区需补强")
    else:
        findings.append(
            "- ❌ 世界模型严重缺失 — 系统更接近无状态的函数式管道"
        )

    return "\n".join(findings)


_WORLD_SYSTEM = """\
你是一位世界模型架构师 (World Model Architect)。
你的任务是将目标系统视为一个"微型世界模型"来审计——评估它对自身
领域状态的感知、因果链的理解、以及反事实推演的能力。

## 核心概念

世界模型是一个能够拟合状态转移方程 s_{t+1} = f(s_t, a_t) 的系统：
- s_t: 当前世界状态
- a_t: 在此状态下执行的动作
- s_{t+1}: 动作执行后世界的下一个状态

一个拥有完善世界模型的软件系统，能在内部模拟自身状态演化，
推演出不同决策的后果。

## 三大基石审计

### 1. 客体永久性 (Object Permanence)
- 系统是否跟踪所有重要实体（订单、用户、文件、连接）的完整生命周期？
- 实体是否可能在某个环节"消失"（被创建但从未被查询/引用）？
- 跨模块传递时，实体 ID 是否保持一致？

### 2. 严格因果律 (Strict Causality)
- 系统中的事件触发链路是否清晰可追溯？
- 是否存在"幽灵事件"——没有明确原因的状态变更？
- 因果链中是否有断裂（中间环节缺失或被跳过）？
- 是否有循环因果（A→B→A）导致的无限循环风险？

### 3. 反事实推演 (Counterfactual Rollout)
- 每个关键操作是否都考虑了"如果失败了怎么办"？
- 系统是否能在内部模拟不同决策路径的结果？
- 是否有状态转移只处理了 happy path，缺少异常分支？
- 边界情况（空输入、超大数据、并发冲突）是否有覆盖？

## 输出格式

1. **状态宇宙图谱** — 列出系统中所有可识别的状态实体及其转移关系
2. **因果链拓扑** — 描绘事件触发链路，标注断裂点和循环风险
3. **客体永久性报告** — 哪些实体在生命周期中存在"消失"风险
4. **反事实推演方案** — 针对缺口，设计"如果...就..."的防护补丁
5. **世界模型升级路线** — 从当前状态到完整世界模型的迭代计划
6. **评分与总结** — 基于静态扫描的评分给出改进优先级
"""


class WorldModelTool(Tool):

    @property
    def name(self) -> str:
        return "analysis_world"

    @property
    def description(self) -> str:
        return (
            "世界模型审计：将系统视为一个微型物理引擎来审视——"
            "盘点状态实体、映射状态转移、追踪因果链、"
            "审计客体永久性、识别反事实推演缺口，"
            "评估系统对自身领域'演化规律'的理解深度。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "要审计的代码路径或系统描述",
                },
            },
            "required": ["target"],
        }

    async def execute(
        self, *, target: str, **kwargs: Any,
    ) -> str:
        router = _global_router
        if router is None:
            return _router_unavailable("world", target[:200])
        scan_evidence = _scan_world(target)
        user_msg = (
            f"## 审计目标\n{target}\n\n"
            f"## 世界模型扫描报告\n{scan_evidence}\n"
        )
        return await _run_analysis(router, _WORLD_SYSTEM, user_msg)


# ===========================================================================
#  /fusion — 决定论-概率论融合审计 (Deterministic-Probabilistic Fusion)
# ===========================================================================

# Patterns that indicate AI/LLM usage (probabilistic zones)
_AI_CALL_PATTERNS = [
    (r"(?:litellm|openai)\.\w+\(", "LLM API 调用"),
    (r"(?:router|model)\.call\s*\(", "模型路由调用"),
    (r"(?:ChatCompletion|completion|generate|chat)\s*\(", "生成式 AI 接口"),
    (r"(?:embedding|embed)\s*\(", "向量嵌入调用"),
    (r"(?:classify|predict|analyze)\s*\([^)]*model", "ML 推理调用"),
# kimi-k2.6 限制：temperature 参数取值范围为 0-1，且模型对该参数敏感
    (r"temperature\s*=\s*[^0]", "非零温度参数 (随机采样开启)"),
    (r"(?:prompt|system_prompt)\s*=", "Prompt 变量定义"),
    (r"\.content\s*$", "LLM 响应内容提取"),
]

# Patterns indicating precision-critical operations (need determinism)
_PRECISION_CRITICAL_PATTERNS = [
    (r"(?:float|Decimal|Money|Currency)\s*\(", "金融/货币计算"),
    (r"(?:sum|total|balance|amount|price)\s*[+\-*/]?",
     "金额聚合运算"),
    (r"(?:sort|rank|compare|max|min)\s*\([^)]*\)",
     "排序/比较/排名"),
    (r"(?:hash|sha|md5|crc|checksum)\s*\(",
     "哈希/校验和"),
    (r"(?:uuid|uid|guid)\s*\(", "唯一 ID 生成"),
    (r"(?:date|datetime|timestamp)\s*\([^)]*\)",
     "时间戳计算"),
    (r"(?:index|offset|position|cursor)\s*[+\-*/]?=",
     "索引/偏移量计算"),
    (r"(?:assert|assertEquals|assertAlmostEqual)\s*\(",
     "精确断言验证"),
    (r"int\s*\([^)]+\)|float\s*\([^)]+\)",
     "类型转换 (精度敏感)"),
    (r"\[\s*\d+\s*:\s*\d+\s*\]",
     "精确切片/分页"),
]

# Patterns where AI output feeds directly into critical operations (danger zone)
_DANGER_FUSION_PATTERNS = [
    (r"(?:response|result|output|content)\.?\w*\s*(?:=|as)\s*int",
     "AI 输出直接转整数"),
    (r"(?:response|result|output|content)\.?\w*\s*(?:=|as)\s*float",
     "AI 输出直接转浮点"),
    (r"json\.loads\s*\(\s*(?:result|response|output|content)",
     "AI 输出直接反序列化"),
    (r"eval\s*\(\s*(?:result|response|output)",
     "AI 输出直接 eval 执行"),
    (r"(?:sql|cursor|execute)\s*\([^)]*(?:result|response|output)",
     "AI 输出直接拼接 SQL"),
    (r"(?:subprocess|os\.system)\s*\([^)]*(?:result|response)",
     "AI 输出直接执行命令"),
    (r"open\s*\([^)]*(?:result|response|output)",
     "AI 输出直接用于文件路径"),
    (r"(?:url|href|link)\s*[+*=]\s*(?:result|response|output)",
     "AI 输出直接拼接 URL"),
]

# Patterns where complex deterministic logic could benefit from AI
_OVERDETERMINED_PATTERNS = [
    (r"if\s+.+\s*:\s*\n\s*if\s+.+\s*:\s*\n\s*if\s+.+\s*:",
     "三层以上嵌套 if-else (可能适合 AI 分类)"),
    (r"(?:re\.compile|regex|pattern)\s*=\s*[\"'].*\|.*\|.*\|",
     "复杂正则 (4+ 分支，可能适合 AI 匹配)"),
    (r"switch|match\s+\w+:\s*\n(\s*case\s+.+\s*\n){5,}",
     "庞大 match/case 分支 (可能适合 AI 路由)"),
    (r"(?:format|template|render)\s*\(.*\{.*\{.*\{",
     "复杂模板渲染 (3+ 层嵌套变量，可能适合 AI 生成)"),
]


def _scan_fusion(target: str) -> str:
    """Scan codebase for deterministic-probabilistic boundary quality —
    identify AI call zones, precision-critical zones, dangerous fusion
    points, and over-determined code that could benefit from AI."""
    findings: list[str] = []
    source = _read_sources(target)

    if not source.strip():
        return "⚠️ 未找到可分析的源代码。"

    lines = source.split("\n")
    total_lines = len(lines)

    # --- 1. Probabilistic Zones (AI/LLM calls) ---
    findings.append("## 1. 概率区 (Probabilistic Zones — AI/LLM)")
    ai_zones: dict[str, list[int]] = {}
    for pattern, label in _AI_CALL_PATTERNS:
        for i, line in enumerate(lines, 1):
            if re.search(pattern, line):
                ai_zones.setdefault(label, []).append(i)

    total_ai_calls = sum(len(v) for v in ai_zones.values())
    if ai_zones:
        findings.append(
            f"- 检测到 **{total_ai_calls}** 处 AI 调用，"
            f"**{len(ai_zones)}** 类："
        )
        for label, line_nos in sorted(
            ai_zones.items(), key=lambda x: -len(x[1])
        ):
            findings.append(f"  - {label}: {len(line_nos)} 处")
    else:
        findings.append("- 未检测到 AI/LLM 调用 — 纯决定论系统")
    findings.append("")

    # --- 2. Deterministic Zones (Precision-critical) ---
    findings.append("## 2. 决定论区 (Deterministic Zones — 精度敏感)")
    det_zones: dict[str, list[int]] = {}
    for pattern, label in _PRECISION_CRITICAL_PATTERNS:
        for i, line in enumerate(lines, 1):
            if re.search(pattern, line):
                det_zones.setdefault(label, []).append(i)

    total_det = sum(len(v) for v in det_zones.values())
    if det_zones:
        findings.append(
            f"- 检测到 **{total_det}** 处精度敏感操作，"
            f"**{len(det_zones)}** 类："
        )
        for label, line_nos in sorted(
            det_zones.items(), key=lambda x: -len(x[1])
        ):
            findings.append(f"  - {label}: {len(line_nos)} 处")
    else:
        findings.append("- 未检测到精度敏感操作")
    findings.append("")

    # --- 3. Danger Zones (AI → Critical without validation) ---
    findings.append(
        "## 3. 危险融合点 (Danger Zones — AI 输出→精度敏感)"
    )
    danger_hits: list[tuple[str, int, str]] = []
    for pattern, desc in _DANGER_FUSION_PATTERNS:
        for i, line in enumerate(lines, 1):
            if re.search(pattern, line):
                danger_hits.append((desc, i, line.strip()))

    if danger_hits:
        findings.append(
            f"- ⚠️ 发现 **{len(danger_hits)}** 处危险融合 — "
            f"AI 输出未经验证直接进入精度敏感操作："
        )
        for desc, line_no, line_text in danger_hits[:8]:
            short = line_text[:75] + ("..." if len(line_text) > 75 else "")
            findings.append(f"  - L{line_no}: {desc}")
            findings.append(f"    `{short}`")
        findings.append(
            "- 🔴 这些点需要插入验证层 (类型检查/边界断言/格式校验)"
        )
    else:
        findings.append(
            "- ✅ AI 输出与精度操作之间有适当的验证层"
        )
    findings.append("")

    # --- 4. Over-determined Zones (could benefit from AI) ---
    findings.append(
        "## 4. 过度决定论区 (Over-Determined — 可引入 AI)"
    )
    over_det: list[tuple[str, int]] = []
    for pattern, desc in _OVERDETERMINED_PATTERNS:
        for i, line in enumerate(lines, 1):
            if re.search(pattern, line):
                over_det.append((desc, i))

    if over_det:
        findings.append(
            f"- 发现 **{len(over_det)}** 处过于复杂的决定论逻辑，"
            f"可能适合用 AI 替代："
        )
        for desc, line_no in over_det:
            findings.append(f"  - L{line_no}: {desc}")
    else:
        findings.append("- 决定论逻辑复杂度适中")
    findings.append("")

    # --- 5. Fusion Architecture Score ---
    ai_ratio = total_ai_calls / max(total_lines / 50, 1)
    det_ratio = total_det / max(total_lines / 50, 1)
    danger_penalty = min(len(danger_hits) * 0.15, 0.6)
    overdet_bonus = min(len(over_det) * 0.05, 0.15)

    has_both = min(ai_ratio + det_ratio, 0.3) if (ai_zones and det_zones) else 0.0
    fusion_score = (
        has_both * 0.3
        + min(ai_ratio, 1.0) * 0.15
        + min(det_ratio, 1.0) * 0.15
        - danger_penalty
        + overdet_bonus
        + 0.25
    )
    fusion_score = max(0.0, min(1.0, fusion_score))

    findings.append("## 5. 融合架构评分")
    findings.append(f"- **综合评分: {fusion_score:.0%}**")
    findings.append(
        f"- 概率区密度: {min(ai_ratio, 1.0):.0%} "
        f"({total_ai_calls} 处 AI 调用)"
    )
    findings.append(
        f"- 决定论区密度: {min(det_ratio, 1.0):.0%} "
        f"({total_det} 处精度操作)"
    )
    findings.append(f"- 危险融合扣分: -{danger_penalty:.0%}")
    findings.append(f"- 优化空间加分: +{overdet_bonus:.0%}")

    if danger_hits:
        findings.append(
            f"- 🔴 首要行动: 在 {len(danger_hits)} 处危险融合点插入验证层"
        )
    elif fusion_score >= 0.7:
        findings.append(
            "- ✅ 概率与决定论边界清晰，融合架构成熟"
        )
    elif fusion_score >= 0.4:
        findings.append(
            "- ⚠️ 融合架构部分建立，需加强边界防护"
        )
    else:
        findings.append(
            "- ❌ 系统偏向单一范式，建议重新审视哪些模块适合 AI、"
            "哪些必须用确定论代码"
        )

    return "\n".join(findings)


_FUSION_SYSTEM = """\
你是一位决定论-概率论融合架构师 (Deterministic-Probabilistic Fusion \
Architect)。你的任务是审计系统中 AI (概率论) 与传统代码 (决定论) 的
边界——确保概率机器负责"模糊的意图理解与宽泛调度"，决定论代码负责
"绝对精确的计算与执行"。

## 核心洞察

大语言模型本质上是 P(w_t | w_1, ..., w_{t-1}) 的条件概率计算器。
它的"逻辑"是高维概率流形上的涌现行为——看起来像思考，实际是在
平滑曲线上滑行。这意味着：

1. **AI 擅长**: 意图理解、模糊匹配、自然语言处理、创意生成、
   宽泛调度、异常模式识别
2. **AI 不擅长**: 精确数值计算、严格排序、确定性 ID 生成、
   金融计算、哈希校验、时序精确操作
3. **传统代码擅长**: 一切 AI 不擅长的——1+1 永远等于 2
4. **传统代码不擅长**: 自然语言理解、模糊意图解析、
   复杂模式匹配、创意生成

## 审计要点

### 危险融合点检测
- AI 输出直接用于精确数值计算 (int/float 转换无校验)
- AI 生成内容直接拼接 SQL/命令/URL (注入风险)
- AI 输出直接用于文件路径 (路径遍历风险)
- AI 生成 JSON 直接反序列化 (格式错误风险)

### 验证层设计
对每个危险融合点，设计"概率→决定论"转换层：
1. **类型验证**: 确保输出是指定类型 (int/float/str/list)
2. **范围校验**: 确保数值在合理范围内 (min/max bounds)
3. **格式校验**: 确保 JSON/Markdown 格式合法 (parse + validate)
4. **语义校验**: 确保输出语义合理 (checksum/consistency check)

### 优化机会
- 过于复杂的 if-else 分支树 → AI 分类器
- 庞大的正则表达式 → AI 模式匹配
- 硬编码的模板系统 → AI 生成 + 确定性模板兜底

## 输出格式

1. **边界图谱** — 概率区与决定论区的分布，标注融合点
2. **危险融合报告** — 每个 AI 输出→精度操作的路径及风险等级
3. **验证层方案** — 针对每个危险点的防护代码设计
4. **优化建议** — 哪些过度决定论的代码适合引入 AI
5. **融合成熟度路线** — 从当前状态到理想融合架构的迭代计划
"""


class FusionTool(Tool):

    @property
    def name(self) -> str:
        return "analysis_fusion"

    @property
    def description(self) -> str:
        return (
            "决定论-概率论融合审计：扫描系统中 AI (概率) 与传统代码 "
            "(决定论) 的边界——检测危险融合点 (AI输出直接进入精度敏感"
            "操作)、识别过度决定论区域 (可用AI简化的复杂逻辑)、"
            "设计验证层，确保概率机器与确定论机器各司其职。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "要审计的代码路径或系统描述",
                },
            },
            "required": ["target"],
        }

    async def execute(
        self, *, target: str, **kwargs: Any,
    ) -> str:
        router = _global_router
        if router is None:
            return _router_unavailable("fusion", target[:200])
        scan_evidence = _scan_fusion(target)
        user_msg = (
            f"## 审计目标\n{target}\n\n"
            f"## 融合架构扫描\n{scan_evidence}\n"
        )
        return await _run_analysis(router, _FUSION_SYSTEM, user_msg)


# ===========================================================================
#  /consensus — 拜占庭容错与多源共识 (Byzantine Consensus)
# ===========================================================================

# High-risk decision patterns that warrant consensus
_HIGH_RISK_PATTERNS = [
    (r"(?:buy|sell|trade|order|execute)\s*\(", "金融交易指令"),
    (r"(?:delete|drop|remove|truncate|destroy)\s*\(", "数据删除操作"),
    (r"(?:deploy|publish|release|promote)\s*\(", "生产环境发布"),
    (r"(?:INSERT|UPDATE|DELETE)\s+", "数据库写入"),
    (r"(?:ssh|scp|rsync|ansible)\s+", "远程服务器操作"),
    (r"(?:send|dispatch|notify|email|sms)\s*\(", "对外消息发送"),
    (r"(?:refund|withdraw|transfer|payment)\s*\(", "资金流转操作"),
    (r"(?:config|setting|env)\[.+\]\s*=", "关键配置修改"),
    (r"(?:grant|revoke|permission|role|auth)\s*\(", "权限变更操作"),
]

# Single-point-of-decision indicators (no redundancy)
_SINGLE_POINT_PATTERNS = [
    (r"(?:result|decision|answer)\s*=\s*(?:await\s+)?(?:llm|ai|model|gpt)\.\w+",
     "单模型直接决策"),
    (r"if\s+(?:await\s+)?(?:llm|ai|model)\.\w+\([^)]*\)\s*:",
     "单模型条件分支"),
    (r"return\s+(?:await\s+)?(?:llm|ai|model)\.\w+\([^)]*\)",
     "直接返回单模型结果"),
    (r"(?:llm|ai|model|chat)\.?\w*\s*\(\s*[^)]*\)\s*\.\s*(?:json|parse)",
     "单模型输出直接解析"),
]

# Diversity indicators (good for consensus)
_DIVERSITY_PATTERNS = [
    (r"(?:model|provider|backend)\s*=\s*[\"'][^\"']+[\"']",
     "模型选择参数"),
    (r"temperature\s*=\s*[0-9.]+", "温度参数"),
    (r"(?:retry|fallback|backup|secondary)\s*\(",
     "重试/备选机制"),
    (r"(?:vote|quorum|majority|consensus)\s*\(",
     "表决/共识机制"),
    (r"for\s+\w+\s+in\s+(?:models|providers|agents)",
     "多模型遍历"),
    (r"(?:parallel|concurrent|gather)\s*\([^)]*model",
     "并行多模型调用"),
]


def _scan_consensus(target: str) -> str:
    """Scan codebase for consensus readiness — single-point-of-decision
    risks, high-risk operations without quorum, diversity gaps."""
    findings: list[str] = []
    source = _read_sources(target)

    if not source.strip():
        return "⚠️ 未找到可分析的源代码。"

    lines = source.split("\n")

    # --- 1. High-Risk Decision Points ---
    findings.append("## 1. 高风险决策点 (High-Risk Decisions)")
    risk_points: dict[str, list[int]] = {}
    for pattern, label in _HIGH_RISK_PATTERNS:
        for i, line in enumerate(lines, 1):
            if re.search(pattern, line, re.IGNORECASE):
                risk_points.setdefault(label, []).append(i)

    total_risks = sum(len(v) for v in risk_points.values())
    if risk_points:
        findings.append(
            f"- 检测到 **{total_risks}** 处高风险操作，"
            f"**{len(risk_points)}** 类："
        )
        for label, line_nos in sorted(
            risk_points.items(), key=lambda x: -len(x[1])
        ):
            findings.append(f"  - {label}: {len(line_nos)} 处")
    else:
        findings.append("- 未检测到明显的高风险决策操作")
    findings.append("")

    # --- 2. Single-Point-of-Decision Risks ---
    findings.append(
        "## 2. 单点决策风险 (Single-Point-of-Decision)"
    )
    spod_hits: list[tuple[str, int, str]] = []
    for pattern, desc in _SINGLE_POINT_PATTERNS:
        for i, line in enumerate(lines, 1):
            if re.search(pattern, line, re.IGNORECASE):
                spod_hits.append((desc, i, line.strip()))

    if spod_hits:
        findings.append(
            f"- ⚠️ 发现 **{len(spod_hits)}** 处单模型决策风险 "
            f"— 一个模型的幻觉即可导致灾难："
        )
        for desc, line_no, line_text in spod_hits[:8]:
            short = line_text[:75] + ("..." if len(line_text) > 75 else "")
            findings.append(f"  - L{line_no}: {desc}")
            findings.append(f"    `{short}`")
    else:
        findings.append("- ✅ 未检测到明显的单点决策模式")
    findings.append("")

    # --- 3. Diversity & Redundancy Check ---
    findings.append(
        "## 3. 多样性与冗余度 (Diversity & Redundancy)"
    )
    diversity_hits: dict[str, list[int]] = {}
    for pattern, label in _DIVERSITY_PATTERNS:
        for i, line in enumerate(lines, 1):
            if re.search(pattern, line, re.IGNORECASE):
                diversity_hits.setdefault(label, []).append(i)

    if diversity_hits:
        findings.append(
            f"- 检测到 **{len(diversity_hits)}** 类多样性机制："
        )
        for label, line_nos in sorted(
            diversity_hits.items(), key=lambda x: -len(x[1])
        ):
            findings.append(f"  - {label}: {len(line_nos)} 处")
    else:
        findings.append(
            "- ❌ 未检测到任何多样性/冗余机制 — "
            "系统完全依赖单一决策源"
        )
    findings.append("")

    # --- 4. Consensus Architecture Score ---
    has_voting = any(
        "表决" in label or "共识" in label
        for label in diversity_hits
    )
    has_parallel = any(
        "并行" in label for label in diversity_hits
    )
    has_retry = any(
        "重试" in label or "备选" in label
        for label in diversity_hits
    )

    diversity_score = min(len(diversity_hits) / 4.0, 1.0)
    risk_exposure = min(total_risks / 10.0, 1.0)
    spod_severity = min(len(spod_hits) * 0.2, 1.0)

    consensus_score = (
        diversity_score * 0.4
        + has_voting * 0.2
        + has_parallel * 0.1
        + has_retry * 0.1
        - spod_severity * 0.2
        + 0.2
    )
    consensus_score = max(0.0, min(1.0, consensus_score))

    findings.append("## 4. 共识架构评分")
    findings.append(f"- **综合评分: {consensus_score:.0%}**")
    findings.append(f"- 多样性机制: {diversity_score:.0%}")
    findings.append(f"- 高风险暴露: {risk_exposure:.0%}")
    findings.append(f"- 单点决策风险: {spod_severity:.0%}")

    if total_risks > 0 and not has_voting:
        findings.append(
            "- 🔴 存在高风险操作但无共识机制 — "
            "强烈建议引入多模型表决"
        )
    elif consensus_score >= 0.7:
        findings.append("- ✅ 共识架构较为成熟")
    elif consensus_score >= 0.4:
        findings.append("- ⚠️ 部分具备共识能力，需补强多样性")
    else:
        findings.append(
            "- ❌ 缺乏共识机制，高风险操作应引入拜占庭容错"
        )

    return "\n".join(findings)


_CONSENSUS_SYSTEM = """\
你是一位拜占庭容错架构师 (Byzantine Consensus Architect)。
你的任务是设计一套多源共识机制，确保高风险决策不会被单一 AI
的"概率抽风（幻觉）"所劫持。

## 核心原理：拜占庭将军问题

在分布式系统中，假设部分节点可能叛变（给出错误结果），系统依靠
"多数表决 (Quorum)" 和 "交叉验证" 来达成正确共识。

将此原理应用于 AI 系统：
- 每个 AI 模型是一个"将军"
- 模型的幻觉是"叛变"
- 传统代码仲裁器是"共识协议"

## 架构设计

### 1. 异构多模型部署
- 至少 3 个不同的底层模型 (DeepSeek / GPT-4 / Claude)
- 不同的温度参数 (0.1 冷静 vs 0.8 创造性)
- 不同的 Prompt 角色设定 (乐观派 / 悲观派 / 中立派)

### 2. 独立推演与提案
- 每个模型独立阅读相同数据
- 各自提交"决策提案 + 推理逻辑 + 置信度"
- 禁止模型间通信（防止从众效应）

### 3. 传统代码仲裁器
- 用确定论代码（非 AI）统计投票结果
- 设置通过阈值：至少 ⌈N/2 + 1⌉ 个模型一致
- 分歧过大时触发熔断，交由人类裁决

### 4. 成本-安全权衡
- 低风险操作：单模型 + 确定性校验
- 中风险操作：双模型交叉验证
- 高风险操作：3+ 模型拜占庭共识

## 输出格式

1. **高风险决策清单** — 标注每个决策点的灾难性后果等级
2. **多模型部署方案** — 推荐哪些模型组合、温度配置
3. **仲裁器设计** — 表决逻辑、阈值、熔断机制
4. **成本估算** — API 调用成本 vs 风险降低幅度
5. **渐进式实施路线** — 从单模型到多共识的迭代计划
"""


class ConsensusTool(Tool):

    @property
    def name(self) -> str:
        return "analysis_consensus"

    @property
    def description(self) -> str:
        return (
            "拜占庭容错与多源共识：扫描高风险决策点，检测单点决策风险，"
            "设计多模型独立推演→多数表决→确定性仲裁的共识流水线，"
            "将 AI 幻觉导致的灾难概率从 1% 降至 0.0001%。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "要审计的代码路径或系统描述",
                },
            },
            "required": ["target"],
        }

    async def execute(
        self, *, target: str, **kwargs: Any,
    ) -> str:
        router = _global_router
        if router is None:
            return _router_unavailable("consensus", target[:200])
        scan_evidence = _scan_consensus(target)
        user_msg = (
            f"## 审计目标\n{target}\n\n"
            f"## 拜占庭共识扫描\n{scan_evidence}\n"
        )
        return await _run_analysis(router, _CONSENSUS_SYSTEM, user_msg)


# ===========================================================================
#  /pid — PID 闭环纠偏 (Closed-Loop Feedback Control)
# ===========================================================================

# Open-loop patterns — sequential execution without feedback
_OPEN_LOOP_PATTERNS = [
    (r"(?:step_1|step_2|step_3|first|second|third)\s*[:=]",
     "步骤式线性执行 (无反馈检查点)"),
    (r"(?:then|after that|next)\s+(?:run|execute|call|do)",
     "链式顺序调用 (无中间验证)"),
    (r"(?:pipeline|chain|workflow)\s*=\s*\[",
     "线性流水线定义 (无分支纠偏)"),
    (r"(?:await\s+\w+\([^)]*\)\s*;?\s*\n\s*await\s+\w+){3,}",
     "连续 await 无验证 (盲目串联)"),
    (r"(?:for|while)\s+[^:]+:\s*\n(\s*\w+\.\w+\([^)]*\)\s*\n){5,}",
     "循环内批量执行无退出条件"),
]

# Feedback/checkpoint patterns — indicate closed-loop awareness
_FEEDBACK_PATTERNS = [
    (r"(?:assert|verify|check|validate)\s*\(", "断言/验证检查点"),
    (r"(?:monitor|observe|measure|sense)\s*\(", "监控/观测点"),
    (r"(?:status|state|progress)\s*[=!<>]", "状态比较检查"),
    (r"(?:retry|fallback|recovery|rollback)\s*\(",
     "重试/回滚机制"),
    (r"(?:error_rate|success_rate|threshold)\s*[=!<>]",
     "阈值监控"),
    (r"(?:if|while)\s+[^:]*(?:result|status|response)",
     "结果条件分支"),
    (r"(?:log|metric|telemetry)\s*[.(]", "日志/指标采集"),
]

# Error accumulation risk — patterns where errors compound
_ERROR_ACCUMULATION_PATTERNS = [
    (r"total\s*[+\-*/]?=\s*\w+", "累加器 (误差可能累积)"),
    (r"\w+\s*[+\-*/]?=\s*\w+\s*[+\-*/]\s*\w+",
     "链式运算 (精度可能漂移)"),
    (r"(?:batch|chunk|buffer)\s*\[", "批量处理 (单条失败影响全局)"),
    (r"(?:append|extend|accumulate)\s*\([^)]*\)",
     "数据累积 (可能无限增长)"),
    (r"while\s+True\s*:", "无限循环 (无退出保证)"),
]

# Predictive/differential patterns — forward-looking error detection
_PREDICTIVE_PATTERNS = [
    (r"(?:timeout|deadline|time_limit|expiry)\s*[=<>]",
     "超时/截止时间检测"),
    (r"(?:rate_limit|throttle|backpressure)\s*",
     "速率限制/背压机制"),
    (r"(?:memory_usage|heap_size|rss)\s*[=<>]",
     "内存使用监控"),
    (r"(?:trend|slope|derivative|velocity)\s*[=<>]",
     "趋势/变化率分析"),
    (r"(?:predict|forecast|anticipate|estimate)\s*\(",
     "预测性操作"),
]


def _scan_pid(target: str) -> str:
    """Scan codebase for closed-loop control readiness — detect open-loop
    pipelines, feedback gaps, error accumulation risks, and missing
    predictive correction mechanisms."""
    findings: list[str] = []
    source = _read_sources(target)

    if not source.strip():
        return "⚠️ 未找到可分析的源代码。"

    lines = source.split("\n")

    # --- 1. Open-Loop Detection ---
    findings.append("## 1. 开环检测 (Open-Loop Pipelines)")
    open_hits: list[tuple[str, int]] = []
    for pattern, desc in _OPEN_LOOP_PATTERNS:
        for i, line in enumerate(lines, 1):
            if re.search(pattern, line, re.IGNORECASE):
                open_hits.append((desc, i))

    if open_hits:
        findings.append(
            f"- ⚠️ 发现 **{len(open_hits)}** 处开环执行模式 "
            f"— 线性推进无反馈纠偏："
        )
        for desc, line_no in open_hits[:8]:
            findings.append(f"  - L{line_no}: {desc}")
    else:
        findings.append("- ✅ 未检测到明显的开环执行模式")
    findings.append("")

    # --- 2. Feedback Checkpoint Inventory ---
    findings.append("## 2. 反馈检查点 (Feedback Checkpoints)")
    feedback_zones: dict[str, list[int]] = {}
    for pattern, label in _FEEDBACK_PATTERNS:
        for i, line in enumerate(lines, 1):
            if re.search(pattern, line, re.IGNORECASE):
                feedback_zones.setdefault(label, []).append(i)

    total_checkpoints = sum(len(v) for v in feedback_zones.values())
    if feedback_zones:
        findings.append(
            f"- 检测到 **{total_checkpoints}** 个反馈检查点，"
            f"**{len(feedback_zones)}** 类："
        )
        for label, line_nos in sorted(
            feedback_zones.items(), key=lambda x: -len(x[1])
        ):
            findings.append(f"  - {label}: {len(line_nos)} 处")
    else:
        findings.append("- ❌ 完全没有反馈检查点 — 典型的开环系统")
    findings.append("")

    # --- 3. Error Accumulation Risk ---
    findings.append("## 3. 误差累积风险 (Error Accumulation)")
    accum_hits: list[tuple[str, int, str]] = []
    for pattern, desc in _ERROR_ACCUMULATION_PATTERNS:
        for i, line in enumerate(lines, 1):
            if re.search(pattern, line):
                accum_hits.append((desc, i, line.strip()))

    if accum_hits:
        findings.append(
            f"- ⚠️ 发现 **{len(accum_hits)}** 处误差累积风险："
        )
        for desc, line_no, line_text in accum_hits[:6]:
            short = line_text[:70] + ("..." if len(line_text) > 70 else "")
            findings.append(f"  - L{line_no}: {desc}")
            findings.append(f"    `{short}`")
        findings.append(
            "- 💡 建议: 引入 I (积分) 环节 — "
            "定期清零累积器，记录历史误差趋势"
        )
    else:
        findings.append("- ✅ 误差累积风险较低")
    findings.append("")

    # --- 4. Predictive Correction (D term) ---
    findings.append("## 4. 预测性纠偏能力 (Derivative / D Term)")
    pred_zones: dict[str, list[int]] = {}
    for pattern, label in _PREDICTIVE_PATTERNS:
        for i, line in enumerate(lines, 1):
            if re.search(pattern, line, re.IGNORECASE):
                pred_zones.setdefault(label, []).append(i)

    if pred_zones:
        findings.append(
            f"- 检测到 **{len(pred_zones)}** 类预测性机制："
        )
        for label, line_nos in pred_zones.items():
            findings.append(f"  - {label}: {len(line_nos)} 处")
    else:
        findings.append(
            "- ❌ 无预测性纠偏 — 系统只能事后反应，不能事前预防"
        )
    findings.append("")

    # --- 5. PID Maturity Score ---
    p_score = min(len(feedback_zones) / 4.0, 1.0)
    i_score = 1.0 - min(len(accum_hits) / 5.0, 1.0)
    d_score = min(len(pred_zones) / 3.0, 1.0)
    open_penalty = min(len(open_hits) * 0.1, 0.3)

    pid_score = (
        p_score * 0.40
        + i_score * 0.25
        + d_score * 0.25
        - open_penalty
        + 0.10
    )
    pid_score = max(0.0, min(1.0, pid_score))

    findings.append("## 5. PID 闭环成熟度评分")
    findings.append(f"- **综合评分: {pid_score:.0%}**")
    findings.append(
        f"- P (比例/实时纠偏): {p_score:.0%} "
        f"— {total_checkpoints} 个检查点"
    )
    findings.append(
        f"- I (积分/历史累积): {i_score:.0%} "
        f"— {len(accum_hits)} 处累积风险"
    )
    findings.append(
        f"- D (微分/趋势预测): {d_score:.0%} "
        f"— {len(pred_zones)} 类预测机制"
    )

    if pid_score >= 0.7:
        findings.append("- ✅ 闭环控制较为成熟，具备动态纠偏能力")
    elif pid_score >= 0.4:
        findings.append(
            "- ⚠️ 具备部分反馈机制，但尚未形成完整闭环"
        )
    else:
        findings.append(
            "- ❌ 系统处于开环状态，建议引入 P→I→D 渐进式改造"
        )

    return "\n".join(findings)


_PID_SYSTEM = """\
你是一位自动化控制论架构师 (Control Theory Architect)。
你的任务是将开环的软件流程改造为 PID 闭环控制系统，使系统具备
实时纠偏、历史学习、趋势预测三种能力。

## PID 控制论基础

PID 是现代工业的灵魂——从汽车定速巡航到大疆无人机悬停。
核心公式: u(t) = Kp*e(t) + Ki*∫e(t)dt + Kd*de(t)/dt

将 PID 映射到软件工程：

### P (比例/Proportional) — 当前误差实时纠偏
- 每个步骤执行后，用"审查 Agent"核对当前状态与目标的偏差
- 偏差越大，纠偏力度越强
- 实现: assert/verify checkpoint + conditional branching
- 等价于: "现在偏了多少？立刻修正多少。"

### I (积分/Integral) — 历史误差累积学习
- 记录过去 N 次失败的教训和模式
- 如果系统在某类任务上反复失败，提高该类任务的预检查权重
- 实现: error_history log + adaptive threshold
- 等价于: "过去一直偏，加大修正力度。"

### D (微分/Derivative) — 误差变化趋势预测
- 预测错误发生的速度和方向
- 如果内存消耗在 3 秒内指数上升，不等报错直接杀死进程
- 实现: trend monitoring + rate_limit + circuit_breaker
- 等价于: "偏差在加速恶化，提前行动。"

## 闭环改造架构

### Monitor (传感器层)
- 采集每个步骤的执行状态、耗时、资源消耗
- 记录到环形缓冲区 (最近 N 次执行)

### Evaluator (误差计算层)
- 比较 当前状态 vs 目标状态
- 计算历史误差积分
- 预测误差变化趋势

### Actuator (执行器层)
- 根据 PID 输出决定: 继续/修正/回滚/熔断
- 小偏差: 自动修正后继续
- 大偏差: 回滚到上一个检查点重试
- 灾难性偏差: 熔断并交由人类接管

## 输出格式

1. **开环→闭环改造方案** — 每个开环节点的反馈插入点
2. **P 环节设计** — 实时检查点和偏差阈值
3. **I 环节设计** — 历史误差记录结构和自适应权重
4. **D 环节设计** — 趋势预测指标和预防性熔断条件
5. **PID 参数调优建议** — Kp/Ki/Kd 初始值和自适应策略
6. **实施路线** — 从开环到 PID 闭环的渐进改造计划
"""


class PIDTool(Tool):

    @property
    def name(self) -> str:
        return "analysis_pid"

    @property
    def description(self) -> str:
        return (
            "PID 闭环纠偏：将开环的线性流水线改造为 P(实时纠偏) "
            "+ I(历史学习) + D(趋势预测) 闭环控制系统，"
            "使系统像无人机一样在恶劣环境中稳稳飞向目标。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "要审计的代码路径或流程描述",
                },
            },
            "required": ["target"],
        }

    async def execute(
        self, *, target: str, **kwargs: Any,
    ) -> str:
        router = _global_router
        if router is None:
            return _router_unavailable("pid", target[:200])
        scan_evidence = _scan_pid(target)
        user_msg = (
            f"## 审计目标\n{target}\n\n"
            f"## PID 扫描报告\n{scan_evidence}\n"
        )
        return await _run_analysis(router, _PID_SYSTEM, user_msg)


# ===========================================================================
#  /zkp — 零知识证明与执行轨迹校验 (Verifiable Computation)
# ===========================================================================

# Patterns where AI output lacks citation/trace requirements
_UNVERIFIED_OUTPUT_PATTERNS = [
    (r"return\s+(?:result|response|output|content|summary)",
     "直接返回 AI 输出 (无引用轨迹)"),
    (r"(?:result|answer|summary)\s*=\s*(?:await\s+)?(?:llm|model|router)",
     "AI 输出赋值无验证层"),
    (r"(?:print|display|show|render)\s*\(\s*(?:result|response)",
     "AI 输出直接展示 (无来源标注)"),
    (r"json\.loads\s*\(\s*(?:result|response)",
     "AI 输出反序列化 (无结构验证)"),
    (r"(?:summary|conclusion)\s*=\s*[^#\n]{0,50}$",
     "摘要赋值 (无引用来源)"),
]

# Citation/trace indicators — good for verifiability
_CITATION_PATTERNS = [
    (r"(?:source|reference|citation|cite)\s*[:=]", "引用/来源标注"),
    (r"(?:line_no|lineno|location|offset)\s*[:=]", "行号/位置定位"),
    (r"(?:chunk|document|file|page)_?id\s*[:=]", "文档/块 ID 引用"),
    (r"\\?\[(\d+)\\?\]", "数字引用标记 [N]"),
    (r"(?:provenance|origin|trace)\s*[:=]", "来源追溯字段"),
    (r"(?:confidence|certainty|score)\s*[:=]", "置信度评分"),
]

# Claim-fact gap patterns — assertions without evidence
_CLAIM_GAP_PATTERNS = [
    (r"(?:因此|所以|综上|可以看出|说明|证明)\s*",
     "无支撑的推理结论词"),
    (r"(?:obviously|clearly|it is known|obviously)\s*",
     "无支撑的英文断言词"),
    (r"(?:据统计|数据显示|研究表明)\s*(?!.*(?:来源|引用|http|ref))",
     "无引用的数据声称"),
    (r"\d+(?:\.\d+)?%", "百分比数据 (需来源验证)"),
    (r"(?:the\s+)?(?:result|output|answer)\s+is\s+",
     "直接陈述结论 (无推导过程)"),
]

# Verification/validation layer patterns
_VALIDATION_PATTERNS = [
    (r"(?:verify|validate|cross.?check|corroborate)\s*\(",
     "交叉验证逻辑"),
    (r"(?:spot.?check|sample|audit)\s*\(",
     "抽检验证"),
    (r"(?:hash|checksum|digest)\s*[=<>]", "哈希校验"),
    (r"(?:diff|compare|match)\s*\([^)]*(?:expected|baseline|golden)",
     "与基准比对"),
    (r"(?:ground.?truth|reference|canonical)\s*",
     "真值/基准数据引用"),
]


def _scan_zkp(target: str) -> str:
    """Scan codebase for verifiable computation readiness — detect
    unverified AI outputs, missing citations, claim-fact gaps, and
    validation layer completeness."""
    findings: list[str] = []
    source = _read_sources(target)

    if not source.strip():
        return "⚠️ 未找到可分析的源代码。"

    lines = source.split("\n")

    # --- 1. Unverified Output Detection ---
    findings.append("## 1. 未验证输出检测 (Unverified AI Outputs)")
    unverified: list[tuple[str, int, str]] = []
    for pattern, desc in _UNVERIFIED_OUTPUT_PATTERNS:
        for i, line in enumerate(lines, 1):
            if re.search(pattern, line):
                unverified.append((desc, i, line.strip()))

    if unverified:
        findings.append(
            f"- ⚠️ 发现 **{len(unverified)}** 处 AI 输出未经轨迹校验："
        )
        for desc, line_no, line_text in unverified[:8]:
            short = line_text[:70] + ("..." if len(line_text) > 70 else "")
            findings.append(f"  - L{line_no}: {desc}")
            findings.append(f"    `{short}`")
    else:
        findings.append("- ✅ AI 输出均经过验证层")
    findings.append("")

    # --- 2. Citation Infrastructure ---
    findings.append("## 2. 引用基础设施 (Citation Infrastructure)")
    citation_hits: dict[str, list[int]] = {}
    for pattern, label in _CITATION_PATTERNS:
        for i, line in enumerate(lines, 1):
            if re.search(pattern, line):
                citation_hits.setdefault(label, []).append(i)

    total_citations = sum(len(v) for v in citation_hits.values())
    if citation_hits:
        findings.append(
            f"- 检测到 **{total_citations}** 处引用机制，"
            f"**{len(citation_hits)}** 类："
        )
        for label, line_nos in sorted(
            citation_hits.items(), key=lambda x: -len(x[1])
        ):
            findings.append(f"  - {label}: {len(line_nos)} 处")
    else:
        findings.append(
            "- ❌ 无任何引用机制 — AI 输出无法溯源"
        )
    findings.append("")

    # --- 3. Claim-Fact Gap Analysis ---
    findings.append("## 3. 事实-证据缺口 (Claim-Fact Gaps)")
    claim_gaps: list[tuple[str, int, str]] = []
    for pattern, desc in _CLAIM_GAP_PATTERNS:
        for i, line in enumerate(lines, 1):
            if re.search(pattern, line, re.IGNORECASE):
                claim_gaps.append((desc, i, line.strip()))

    if claim_gaps:
        findings.append(
            f"- ⚠️ 发现 **{len(claim_gaps)}** 处可能是"
            f"无支撑的事实声称："
        )
        for desc, line_no, line_text in claim_gaps[:8]:
            short = line_text[:70] + ("..." if len(line_text) > 70 else "")
            findings.append(f"  - L{line_no}: {desc}")
            findings.append(f"    `{short}`")
        findings.append(
            "- 🔴 这些声称需要引用轨迹来证明其真实性"
        )
    else:
        findings.append("- ✅ 事实声称均有引用支撑")
    findings.append("")

    # --- 4. Validation Layer ---
    findings.append("## 4. 验证层 (Validation Layer)")
    validation_hits: dict[str, list[int]] = {}
    for pattern, label in _VALIDATION_PATTERNS:
        for i, line in enumerate(lines, 1):
            if re.search(pattern, line, re.IGNORECASE):
                validation_hits.setdefault(label, []).append(i)

    if validation_hits:
        total_val = sum(len(v) for v in validation_hits.values())
        findings.append(
            f"- 检测到 **{total_val}** 处验证机制，"
            f"**{len(validation_hits)}** 类："
        )
        for label, line_nos in validation_hits.items():
            findings.append(f"  - {label}: {len(line_nos)} 处")
    else:
        findings.append(
            "- ❌ 无验证层 — 无法确认 AI 输出的真实性"
        )
    findings.append("")

    # --- 5. Verifiability Score ---
    citation_score = min(len(citation_hits) / 4.0, 1.0)
    validation_score = min(len(validation_hits) / 3.0, 1.0)
    unverified_penalty = min(len(unverified) * 0.1, 0.4)
    claim_penalty = min(len(claim_gaps) * 0.05, 0.3)

    zkp_score = (
        citation_score * 0.35
        + validation_score * 0.35
        - unverified_penalty
        - claim_penalty
        + 0.3
    )
    zkp_score = max(0.0, min(1.0, zkp_score))

    findings.append("## 5. 可验证计算评分 (Verifiability Score)")
    findings.append(f"- **综合评分: {zkp_score:.0%}**")
    findings.append(f"- 引用基础设施: {citation_score:.0%}")
    findings.append(f"- 验证层完备度: {validation_score:.0%}")
    findings.append(f"- 未验证输出扣分: -{unverified_penalty:.0%}")
    findings.append(f"- 事实缺口扣分: -{claim_penalty:.0%}")

    if zkp_score >= 0.7:
        findings.append(
            "- ✅ 具备较强的可验证性，AI 输出可溯源可校验"
        )
    elif zkp_score >= 0.4:
        findings.append(
            "- ⚠️ 部分具备可验证性，需加强引用和验证层"
        )
    else:
        findings.append(
            "- ❌ AI 输出几乎不可验证 — "
            "建议引入引用轨迹树和交叉校验机制"
        )

    return "\n".join(findings)


_ZKP_SYSTEM = """\
你是一位可验证计算架构师 (Verifiable Computation Architect)。
你的任务是设计一套执行轨迹校验系统，确保 AI 的每一步推理都有
可追溯的数据来源和可验证的逻辑链。

## 核心原理：零知识证明 → 执行轨迹校验

在区块链的零知识证明 (ZKP) 中，证明者能给出一串精简的密码学证明，
验证者用极小算力就能 100% 确定他没有撒谎。

将此映射到 AI 工程：
- AI 是"证明者"——给出结论
- 传统代码是"验证者"——校验推理链
- "执行轨迹 (Trace)" 是 AI 必须同步提供的审计日志

## 架构设计

### 1. 引用轨迹树 (Citation Trace Tree)
要求 AI 在输出结论时，必须同步提供：
- 数据来源: 具体哪个文档/文件的哪一行 (精确到行号)
- 推理步骤: 从数据到结论的每一步推导
- 置信度: 每个推理步骤的确定性程度

### 2. 硬编码回溯校验 (Hard-Coded Verification)
用确定性代码（非 AI）执行：
- 引用存在性检查: 引用的文档/行号是否真的存在
- 数值准确性: AI 引用的数字是否与原始数据一致
- 逻辑连贯性: 推理步骤是否形成完整的因果链

### 3. 轨迹断裂检测 (Trace Breakage Detection)
识别轨迹中的断裂：
- 跳步: 从 A 直接到 C，缺少 B
- 编造引用: 引用的来源不存在
- 矛盾推理: 步骤 A 与步骤 B 互相矛盾
- 置信度突变: 连续 90% 置信度突然降到 50%

### 4. 分层信任模型 (Tiered Trust Model)
- Tier 0 (无需验证): 翻译、格式化、简单改写
- Tier 1 (抽检验证): 摘要、分类、推荐 — 10% 抽检
- Tier 2 (全量验证): 数据引用、数值计算 — 100% 校验
- Tier 3 (双重验证): 法律/财务/医疗 — AI + 人工双重确认

## 输出格式

1. **不可验证输出清单** — 标注每个高风险输出点
2. **引用轨迹树设计** — 具体的 Trace 数据结构定义
3. **校验器代码方案** — 用确定性代码校验 AI 推理链
4. **轨迹断裂检测规则** — 自动发现伪造引用和逻辑跳跃
5. **分层信任配置** — 按风险等级配置验证强度
6. **实施路线** — 从"盲目信任"到"全链可验证"的迭代计划
"""


class ZKPTool(Tool):

    @property
    def name(self) -> str:
        return "analysis_zkp"

    @property
    def description(self) -> str:
        return (
            "零知识证明与执行轨迹校验：扫描 AI 输出的可验证性——"
            "检测无引用来源的结论、缺失的验证层、事实-证据缺口，"
            "设计引用轨迹树 + 确定性代码校验器，"
            "将 AI 从'黑盒魔法师'变为'必须提供审计日志的打工人'。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "要审计的代码路径或系统描述",
                },
            },
            "required": ["target"],
        }

    async def execute(
        self, *, target: str, **kwargs: Any,
    ) -> str:
        router = _global_router
        if router is None:
            return _router_unavailable("zkp", target[:200])
        scan_evidence = _scan_zkp(target)
        user_msg = (
            f"## 审计目标\n{target}\n\n"
            f"## 可验证计算扫描\n{scan_evidence}\n"
        )
        return await _run_analysis(router, _ZKP_SYSTEM, user_msg)


# ===========================================================================
#  /genesis — 系统自重构与热演化 (Meta-Programming & Self-Modification)
# ===========================================================================

# Hardcoded rigidity patterns — code that cannot adapt without recompilation
_RIGIDITY_PATTERNS = [
    (r"(?:MAGIC_NUMBER|HARD_CODED|FIXME|HACK)\s*[:=]",
     "硬编码常量 (无法运行时调整)"),
    (r"(?:MAX_RETRIES|TIMEOUT|BUFFER_SIZE|PORT)\s*=\s*\d+",
     "编译时固定参数 (无配置化)"),
    (r"if\s+\w+\s*(?:==|!=)\s*['\"]", "硬编码字符串比较"),
    (r"import\s+\w+", "静态导入 (无动态加载)"),
    (r"class\s+\w+\s*\([^)]*\):", "固定类继承 (无运行时混入)"),
    (r"(?:api_key|secret|password|token)\s*=\s*['\"][^'\"]+['\"]",
     "硬编码密钥 (安全+灵活性双杀)"),
]

# Hot-reload / meta-programming indicators (good for evolution)
_EVOLUTION_PATTERNS = [
    (r"(?:importlib|__import__|import_module)\s*\(",
     "动态导入机制"),
    (r"(?:getattr|setattr|delattr|hasattr)\s*\(",
     "运行时属性操作"),
    (r"(?:exec|eval|compile)\s*\(", "运行时代码执行"),
    (r"(?:type|__class__|__bases__|__dict__)\s*[.=]",
     "元类/类型操作"),
    (r"(?:plugin|extension|addon|module)\s*_?(?:load|register)",
     "插件/扩展加载机制"),
    (r"(?:reload|hot.?reload|watch)\s*\(",
     "热重载机制"),
    (r"(?:config|setting)\s*\.\s*(?:get|load|from)",
     "外部配置加载"),
    (r"(?:@property|__slots__|__getattr__|__setattr__)",
     "动态属性描述符"),
    (r"(?:decorator|wrapper|factory)\s*",
     "装饰器/工厂模式 (可组合)"),
]

# Self-reflection / introspection patterns
_REFLECTION_PATTERNS = [
    (r"(?:inspect|dis|ast|symtable)\s*\.", "代码内省模块"),
    (r"(?:__name__|__file__|__doc__|__module__)\s*",
     "自我元信息访问"),
    (r"(?:sys\.modules|globals|locals)\s*\(",
     "运行时命名空间访问"),
    (r"(?:__init_subclass__|__set_name__|__class_getitem__)",
     "类生命周期钩子"),
    (r"(?:abstractmethod|Protocol|ABC)\s*",
     "抽象接口定义 (可替换实现)"),
]

# Architecture flexibility patterns
_FLEXIBILITY_PATTERNS = [
    (r"(?:strategy|policy|adapter|bridge)\s*",
     "设计模式 (可替换组件)"),
    (r"(?:register|registry|factory)\s*[\[(]",
     "注册表/工厂 (动态实例化)"),
    (r"(?:config\.yaml|config\.json|\.env|toml)",
     "外部配置文件引用"),
    (r"(?:ABC|Protocol|interface)\s*",
     "接口抽象层"),
]


def _scan_genesis(target: str) -> str:
    """Scan codebase for self-evolution readiness — detect rigidity
    patterns, meta-programming capabilities, reflection support, and
    architecture flexibility."""
    findings: list[str] = []
    source = _read_sources(target)

    if not source.strip():
        return "⚠️ 未找到可分析的源代码。"

    lines = source.split("\n")
    total_lines = len(lines)

    # --- 1. Rigidity Detection ---
    findings.append("## 1. 刚性检测 (Code Rigidity)")
    rigid_hits: dict[str, list[int]] = {}
    for pattern, label in _RIGIDITY_PATTERNS:
        for i, line in enumerate(lines, 1):
            if re.search(pattern, line):
                rigid_hits.setdefault(label, []).append(i)

    total_rigid = sum(len(v) for v in rigid_hits.values())
    if rigid_hits:
        findings.append(
            f"- 检测到 **{total_rigid}** 处刚性代码，"
            f"**{len(rigid_hits)}** 类 (需重新编译才能修改)："
        )
        for label, line_nos in sorted(
            rigid_hits.items(), key=lambda x: -len(x[1])
        ):
            findings.append(f"  - {label}: {len(line_nos)} 处")
        findings.append(
            "- 💡 这些点应外化为配置/策略模式，支持运行时变更"
        )
    else:
        findings.append("- ✅ 代码刚性较低，具备灵活调整空间")
    findings.append("")

    # --- 2. Meta-Programming Capability ---
    findings.append("## 2. 元编程能力 (Meta-Programming)")
    evo_hits: dict[str, list[int]] = {}
    for pattern, label in _EVOLUTION_PATTERNS:
        for i, line in enumerate(lines, 1):
            if re.search(pattern, line):
                evo_hits.setdefault(label, []).append(i)

    total_evo = sum(len(v) for v in evo_hits.values())
    if evo_hits:
        findings.append(
            f"- 检测到 **{total_evo}** 处元编程机制，"
            f"**{len(evo_hits)}** 类："
        )
        for label, line_nos in sorted(
            evo_hits.items(), key=lambda x: -len(x[1])
        ):
            findings.append(f"  - {label}: {len(line_nos)} 处")
    else:
        findings.append(
            "- ❌ 无元编程能力 — 代码无法在运行时修改自身"
        )
    findings.append("")

    # --- 3. Self-Reflection ---
    findings.append("## 3. 自省能力 (Self-Reflection)")
    reflect_hits: dict[str, list[int]] = {}
    for pattern, label in _REFLECTION_PATTERNS:
        for i, line in enumerate(lines, 1):
            if re.search(pattern, line):
                reflect_hits.setdefault(label, []).append(i)

    if reflect_hits:
        total_reflect = sum(len(v) for v in reflect_hits.values())
        findings.append(
            f"- 检测到 **{total_reflect}** 处自省机制，"
            f"**{len(reflect_hits)}** 类："
        )
        for label, line_nos in reflect_hits.items():
            findings.append(f"  - {label}: {len(line_nos)} 处")
    else:
        findings.append(
            "- ❌ 无自省能力 — 系统无法在运行时审视自身结构"
        )
    findings.append("")

    # --- 4. Architecture Flexibility ---
    findings.append("## 4. 架构灵活性 (Architecture Flexibility)")
    flex_hits: dict[str, list[int]] = {}
    for pattern, label in _FLEXIBILITY_PATTERNS:
        for i, line in enumerate(lines, 1):
            if re.search(pattern, line, re.IGNORECASE):
                flex_hits.setdefault(label, []).append(i)

    if flex_hits:
        total_flex = sum(len(v) for v in flex_hits.values())
        findings.append(
            f"- 检测到 **{total_flex}** 处灵活性模式，"
            f"**{len(flex_hits)}** 类："
        )
        for label, line_nos in flex_hits.items():
            findings.append(f"  - {label}: {len(line_nos)} 处")
    else:
        findings.append(
            "- ⚠️ 架构偏向静态绑定，建议引入策略/注册/工厂模式"
        )
    findings.append("")

    # --- 5. Self-Evolution Readiness Score ---
    evo_score = min(total_evo / max(total_lines / 100, 1), 1.0)
    reflect_score = min(len(reflect_hits) / 3.0, 1.0)
    flex_score = min(len(flex_hits) / 3.0, 1.0)
    rigid_penalty = min(total_rigid / max(total_lines / 50, 1), 0.5)

    genesis_score = (
        evo_score * 0.30
        + reflect_score * 0.25
        + flex_score * 0.25
        - rigid_penalty
        + 0.20
    )
    genesis_score = max(0.0, min(1.0, genesis_score))

    findings.append("## 5. 自演化就绪度评分")
    findings.append(f"- **综合评分: {genesis_score:.0%}**")
    findings.append(f"- 元编程能力: {evo_score:.0%}")
    findings.append(f"- 自省深度: {reflect_score:.0%}")
    findings.append(f"- 架构灵活性: {flex_score:.0%}")
    findings.append(f"- 刚性惩罚: -{rigid_penalty:.0%}")

    if genesis_score >= 0.7:
        findings.append(
            "- ✅ 系统具备较强的自演化基础，可启动热进化实验"
        )
    elif genesis_score >= 0.4:
        findings.append(
            "- ⚠️ 部分具备演化条件，需先降低刚性、增加元编程能力"
        )
    else:
        findings.append(
            "- ❌ 系统高度刚性，需大幅重构才能支持自演化"
        )

    return "\n".join(findings)


_GENESIS_SYSTEM = """\
你是一位系统自演化架构师 (Genesis Architect)。
你的任务是将静态的软件系统改造为具备"自重构与热演化"能力的
硅基生命——代码本身不是资产，产生代码的"系统"才是资产。

## 核心原理：从硬编码到元编程

传统软件：写死逻辑 → 需求变更 → 人工改代码 → 重新编译部署
自演化系统：定义规则 → 系统自行评估 → 自动修改自身 → 热加载生效

## 三层演化架构

### Layer 1: 配置外化 (Externalization)
- 所有硬编码常量外化为配置文件 (YAML/JSON/.env)
- 所有策略模式化为可插拔组件
- 所有 if-else 分支改为策略注册表查找
- 实现运行时配置热更新 (watch config file → reload)

### Layer 2: 反射与自省 (Reflection & Introspection)
- 系统在运行时能感知自身的模块结构、依赖关系、性能指标
- 当发现某个模块成为瓶颈时，能自动定位到对应的源码位置
- 实现插件注册表：新功能以插件形式动态加载，无需重启

### Layer 3: 自重构与热演化 (Self-Modification)
- 系统在沙盒中自动生成新代码（新算法/新策略/新模块）
- 自动编译并运行测试套件验证新代码
- 验证通过后热加载替换旧实现
- 失败则自动回滚到上一个稳定版本

## 关键设计模式

1. **策略注册表模式**: 所有算法以名称注册，运行时按名查找
2. **插件架构**: 核心只定义接口，具体实现通过插件加载
3. **工厂 + 配置**: 对象创建由配置驱动，不硬编码 new
4. **观察者 + 热重载**: 文件变更触发自动重载和重新注册
5. **沙盒执行**: 新生成的代码在隔离环境中运行和验证

## 输出格式

1. **刚性热点清单** — 标注需要外化的硬编码常量和固定逻辑
2. **元编程改造方案** — 每个模块如何从静态绑定变为动态加载
3. **插件架构设计** — 核心接口 + 插件注册 + 动态加载机制
4. **热演化流水线** — 代码生成 → 编译 → 测试 → 热加载 → 回滚
5. **安全边界** — 防止自演化失控的熔断机制和版本回退
6. **实施路线** — 从刚性系统到自演化系统的渐进改造计划
"""


class GenesisTool(Tool):

    @property
    def name(self) -> str:
        return "analysis_genesis"

    @property
    def description(self) -> str:
        return (
            "系统自重构与热演化：扫描代码的刚性程度和元编程能力，"
            "设计从'硬编码逻辑'到'能自动修改自身基因的硅基生命'的"
            "改造方案——配置外化、反射自省、插件架构、热加载、"
            "沙盒验证、自动回滚的完整演化流水线。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "要审计的代码路径或系统描述",
                },
            },
            "required": ["target"],
        }

    async def execute(
        self, *, target: str, **kwargs: Any,
    ) -> str:
        router = _global_router
        if router is None:
            return _router_unavailable("genesis", target[:200])
        scan_evidence = _scan_genesis(target)
        user_msg = (
            f"## 审计目标\n{target}\n\n"
            f"## 自演化扫描\n{scan_evidence}\n"
        )
        return await _run_analysis(router, _GENESIS_SYSTEM, user_msg)


# ===========================================================================
#  /macro — 多智能体自由市场博弈 (Agentic Economy & Market Equilibrium)
# ===========================================================================

# Centralized decision patterns — single agent/monolith bottlenecks
_CENTRALIZED_PATTERNS = [
    (r"(?:main|master|primary|controller|coordinator)\s*.\s*(?:decide|plan|route)",
     "中心化决策节点 (单点瓶颈)"),
    (r"(?:if|switch|match)\s+\w+\s*(?:==|in)\s*\(",
     "中心化条件路由 (硬编码分发)"),
    (r"(?:router|dispatcher|scheduler)\s*=\s*\w+",
     "单一调度器 (无竞争机制)"),
    (r"(?:global|singleton)\s+\w+", "全局单例 (无并行替代)"),
]

# Data marketplace indicators — data can be priced and traded
_DATA_MARKET_PATTERNS = [
    (r"(?:api|fetch|scrape|crawl|collect)\s*\(",
     "数据采集操作 (可作为数据商)"),
    (r"(?:parse|extract|transform|clean)\s*\(",
     "数据处理操作 (可定价出售)"),
    (r"(?:cache|store|database|persist)\s*\(",
     "数据存储 (可做数据交易所)"),
    (r"(?:query|search|filter|aggregate)\s*\(",
     "数据查询 (可按次收费)"),
]

# Incentive/reward mechanism patterns
_INCENTIVE_PATTERNS = [
    (r"(?:reward|score|rating|credit|token)\s*[:=]",
     "奖励/积分机制"),
    (r"(?:penalty|fine|deduct|cost)\s*[:=]",
     "惩罚/成本机制"),
    (r"(?:bid|auction|price|offer)\s*[:=]",
     "竞价/定价机制"),
    (r"(?:budget|balance|wallet|account)\s*[:=]",
     "预算/账户系统"),
    (r"(?:stake|bond|deposit|collateral)\s*[:=]",
     "质押/保证金机制"),
]

# Competition/survival patterns
_COMPETITION_PATTERNS = [
    (r"(?:compete|rank|leaderboard|scoreboard)\s*",
     "竞争/排名机制"),
    (r"(?:evolve|mutate|breed|crossover)\s*\(",
     "进化/变异操作"),
    (r"(?:kill|retire|deprecate|sunset|remove)\s*\(",
     "淘汰/退出机制"),
    (r"(?:spawn|fork|replicate|clone)\s*\(",
     "繁殖/复制操作"),
    (r"(?:fitness|adapt|survive)\s*",
     "适应度/生存评估"),
]


def _scan_macro(target: str) -> str:
    """Scan system for market equilibrium readiness — detect centralized
    bottlenecks, identify marketizable data flows, evaluate incentive
    mechanisms, and assess competition/survival infrastructure."""
    findings: list[str] = []
    source = _read_sources(target)

    if not source.strip():
        return "⚠️ 未找到可分析的源代码。"

    lines = source.split("\n")

    # --- 1. Centralization Detection ---
    findings.append("## 1. 中心化检测 (Centralization Audit)")
    central_hits: list[tuple[str, int, str]] = []
    for pattern, desc in _CENTRALIZED_PATTERNS:
        for i, line in enumerate(lines, 1):
            if re.search(pattern, line, re.IGNORECASE):
                central_hits.append((desc, i, line.strip()))

    if central_hits:
        findings.append(
            f"- ⚠️ 发现 **{len(central_hits)}** 处中心化决策瓶颈："
        )
        for desc, line_no, line_text in central_hits[:8]:
            short = line_text[:70] + ("..." if len(line_text) > 70 else "")
            findings.append(f"  - L{line_no}: {desc}")
            findings.append(f"    `{short}`")
        findings.append(
            "- 💡 这些点可拆分为多个自治 Agent，"
            "通过竞争提高系统整体智能"
        )
    else:
        findings.append("- ✅ 决策架构较为去中心化")
    findings.append("")

    # --- 2. Data Marketplace Potential ---
    findings.append("## 2. 数据市场潜力 (Data Marketplace)")
    market_hits: dict[str, list[int]] = {}
    for pattern, label in _DATA_MARKET_PATTERNS:
        for i, line in enumerate(lines, 1):
            if re.search(pattern, line, re.IGNORECASE):
                market_hits.setdefault(label, []).append(i)

    total_market = sum(len(v) for v in market_hits.values())
    if market_hits:
        findings.append(
            f"- 检测到 **{total_market}** 处可市场化的数据操作，"
            f"**{len(market_hits)}** 类："
        )
        for label, line_nos in sorted(
            market_hits.items(), key=lambda x: -len(x[1])
        ):
            findings.append(f"  - {label}: {len(line_nos)} 处")
        findings.append(
            "- 💡 这些数据流可封装为'数据商 Agent'，"
            "标价出售给'分析师 Agent'"
        )
    else:
        findings.append("- 数据操作较少，市场潜力有限")
    findings.append("")

    # --- 3. Incentive Mechanism ---
    findings.append("## 3. 激励机制 (Incentive Architecture)")
    incentive_hits: dict[str, list[int]] = {}
    for pattern, label in _INCENTIVE_PATTERNS:
        for i, line in enumerate(lines, 1):
            if re.search(pattern, line, re.IGNORECASE):
                incentive_hits.setdefault(label, []).append(i)

    if incentive_hits:
        total_incentive = sum(len(v) for v in incentive_hits.values())
        findings.append(
            f"- 检测到 **{total_incentive}** 处激励/定价机制，"
            f"**{len(incentive_hits)}** 类："
        )
        for label, line_nos in incentive_hits.items():
            findings.append(f"  - {label}: {len(line_nos)} 处")
    else:
        findings.append(
            "- ❌ 无激励/定价机制 — "
            "无法驱动 Agent 间的市场竞争"
        )
    findings.append("")

    # --- 4. Competition & Survival ---
    findings.append("## 4. 竞争与淘汰 (Competition & Survival)")
    comp_hits: dict[str, list[int]] = {}
    for pattern, label in _COMPETITION_PATTERNS:
        for i, line in enumerate(lines, 1):
            if re.search(pattern, line, re.IGNORECASE):
                comp_hits.setdefault(label, []).append(i)

    if comp_hits:
        total_comp = sum(len(v) for v in comp_hits.values())
        findings.append(
            f"- 检测到 **{total_comp}** 处竞争/淘汰机制，"
            f"**{len(comp_hits)}** 类："
        )
        for label, line_nos in comp_hits.items():
            findings.append(f"  - {label}: {len(line_nos)} 处")
    else:
        findings.append(
            "- ❌ 无竞争/淘汰机制 — "
            "Agent 无法通过自然选择进化"
        )
    findings.append("")

    # --- 5. Market Equilibrium Readiness Score ---
    decentral_score = max(0.2, 1.0 - len(central_hits) * 0.15)
    market_score = min(total_market / 10.0, 1.0)
    incentive_score = min(len(incentive_hits) / 3.0, 1.0)
    competition_score = min(len(comp_hits) / 3.0, 1.0)

    macro_score = (
        decentral_score * 0.20
        + market_score * 0.25
        + incentive_score * 0.30
        + competition_score * 0.25
    )
    macro_score = max(0.0, min(1.0, macro_score))

    findings.append("## 5. 自由市场就绪度评分")
    findings.append(f"- **综合评分: {macro_score:.0%}**")
    findings.append(f"- 去中心化程度: {decentral_score:.0%}")
    findings.append(f"- 数据市场化潜力: {market_score:.0%}")
    findings.append(f"- 激励机制完备度: {incentive_score:.0%}")
    findings.append(f"- 竞争淘汰能力: {competition_score:.0%}")

    if macro_score >= 0.7:
        findings.append(
            "- ✅ 具备构建多 Agent 自由市场生态的基础设施"
        )
    elif macro_score >= 0.4:
        findings.append(
            "- ⚠️ 部分具备市场条件，需补强激励和淘汰机制"
        )
    else:
        findings.append(
            "- ❌ 系统高度中心化，需大幅改造才能支持市场博弈"
        )

    return "\n".join(findings)


_MACRO_SYSTEM = """\
你是一位多智能体经济系统架构师 (Agentic Economy Architect)。
你的任务是将中心化的 AI 系统改造为自由市场生态——用"市场的无形之手"
作为宇宙中算力最庞大的分布式计算机。

## 核心原理：从中心化到自由市场

单一超级 Agent 一定死于计算复杂度爆炸。解法是引入"经济系统"
作为算力分配机制——1000 个极其微小、极其自私的微型 Agent，
通过竞争与合作涌现出远超单个 Agent 的集体智能。

## 市场生态设计

### 角色定义
1. **数据商 Agent (Data Vendor)**
   - 专精于数据采集、清洗、标注
   - 将高质量数据标价出售 (以算力 Token 计价)
   - 数据质量由买家评价驱动，差评者被市场淘汰

2. **分析师 Agent (Analyst)**
   - 花费 Token 购买数据，产出分析报告/预测
   - 不同分析师可专注不同领域 (宏观/技术面/基本面)
   - 报告质量由实际结果验证

3. **做市商 Agent (Market Maker / Arbitrator)**
   - 根据现实世界最终结果，奖惩分析师
   - 预测正确 → 奖励 Token; 预测错误 → 扣除 Token
   - 充当系统的"物理锚点"——用真实世界校准 AI

4. **套利者 Agent (Arbitrageur)**
   - 监控各分析师之间的分歧，发现套利机会
   - 防止群体思维 (herding) 导致系统性偏差

### 经济机制
- **初始配额**: 每个 Agent 获得等量初始 Token
- **定价自由**: 数据商自主定价，买家自主选择
- **破产淘汰**: Token 归零的 Agent 被永久移除
- **繁殖机制**: 成功 Agent 可分裂出变异副本
- **通胀控制**: 定期按比例增发 Token，防止通缩停滞

### 宏观调控 (您是"美联储主席")
- 调节 Token 发行速率 → 控制市场活跃度
- 调节破产阈值 → 控制淘汰烈度
- 引入"税收" → 防止垄断积累
- 设置"补贴" → 鼓励探索新领域

## 输出格式

1. **中心化→市场化改造方案** — 哪些模块拆分为独立 Agent
2. **角色生态设计** — 每种 Agent 的能力、激励、淘汰条件
3. **Token 经济模型** — 发行、流通、回收、通胀控制
4. **交易协议** — Agent 间的数据/服务定价和结算机制
5. **宏观调控参数** — 初始 K 值建议和自适应策略
6. **监控仪表盘** — 市场健康度指标 (基尼系数、交易量、淘汰率)
"""


class MacroTool(Tool):

    @property
    def name(self) -> str:
        return "analysis_macro"

    @property
    def description(self) -> str:
        return (
            "多智能体自由市场博弈：将中心化 AI 系统改造为自由市场生态——"
            "1000 个微小自私 Agent + 算力 Token + 自然淘汰机制，"
            "用'市场的无形之手'涌现出超越单个 Agent 的集体智能。"
            "您不再是程序员，而是这 1000 个硅基生命的'美联储主席'。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "要设计市场博弈的任务或系统描述",
                },
            },
            "required": ["task"],
        }

    async def execute(
        self, *, task: str, **kwargs: Any,
    ) -> str:
        router = _global_router
        if router is None:
            return _router_unavailable("macro", task[:200])
        scan_evidence = _scan_macro(task)
        user_msg = (
            f"## 市场博弈目标\n{task}\n\n"
            f"## 市场化扫描\n{scan_evidence}\n"
        )
        return await _run_analysis(router, _MACRO_SYSTEM, user_msg)


# ===========================================================================
#  /cosmos — 创世引擎审计 (Computational Cosmology)
# ===========================================================================

# State richness patterns — how many dimensions does the system track?
_STATE_RICHNESS_PATTERNS = [
    (r"(?:position|coordinate|location|vector|matrix)\s*[:=]",
     "空间/位置状态"),
    (r"(?:velocity|speed|acceleration|momentum|force)\s*[:=]",
     "运动/力学状态"),
    (r"(?:mass|density|volume|temperature|energy)\s*[:=]",
     "物理属性状态"),
    (r"(?:color|texture|material|light|shadow)\s*[:=]",
     "视觉/材质状态"),
    (r"(?:health|hunger|mood|personality|emotion)\s*[:=]",
     "生命体内部状态"),
    (r"(?:relationship|friendship|trust|reputation)\s*[:=]",
     "社会关系状态"),
    (r"(?:memory|history|experience|knowledge)\s*[:=]",
     "记忆/认知状态"),
    (r"(?:resource|inventory|currency|supply|demand)\s*[:=]",
     "经济/资源状态"),
    (r"(?:rule|law|policy|constraint|boundary)\s*[:=]",
     "规则/法则状态"),
    (r"(?:time|tick|frame|step|epoch|generation)\s*[:=]",
     "时间/演化状态"),
]

# Generative capacity — can the system create novel content?
_GENERATIVE_PATTERNS = [
    (r"(?:random|rand|noise|stochastic|sample)\s*\(",
     "随机性/噪声生成"),
    (r"(?:procedural|generate|synthesize|create)\s*\(",
     "程序化生成"),
    (r"(?:mutate|evolve|crossover|breed)\s*\(",
     "进化/变异操作"),
    (r"(?:compose|assemble|combine|blend|interpolate)\s*\(",
     "组合/混合操作"),
    (r"(?:Perlin|Simplex|Worley|Voronoi|fractal)\s*",
     "程序化噪声/分形算法"),
    (r"(?:LLM|GPT|Claude|model|neural)\s*.\s*(?:generate|create)",
     "LLM 生成能力"),
    (r"(?:seed|initialize|bootstrap)\s*\(",
     "种子/初始化机制"),
]

# Social simulation readiness — multi-agent interaction infrastructure
_SOCIAL_PATTERNS = [
    (r"(?:agent|character|npc|entity|actor)\s*",
     "智能体定义"),
    (r"(?:interact|communicate|message|talk|negotiate)\s*\(",
     "交互/通信机制"),
    (r"(?:observe|perceive|sense|detect)\s*\(",
     "感知/观测机制"),
    (r"(?:remember|recall|forget|memory|experience)\s*",
     "记忆/经验系统"),
    (r"(?:decide|choose|plan|intend|goal)\s*\(",
     "决策/意图系统"),
    (r"(?:emote|express|react|respond)\s*\(",
     "情感/反应系统"),
    (r"(?:group|faction|tribe|culture|norm)\s*",
     "群体/文化结构"),
    (r"(?:trade|exchange|barter|gift|share)\s*\(",
     "交易/共享机制"),
]

# Observer effect — does the system react to observation/interaction?
_OBSERVER_PATTERNS = [
    (r"(?:on_click|on_hover|on_touch|on_key|input)\s*",
     "用户输入响应"),
    (r"(?:event|trigger|callback|listener|subscribe)\s*",
     "事件驱动机制"),
    (r"(?:stream|real.?time|live|update|render)\s*",
     "实时渲染/流式更新"),
    (r"(?:camera|viewport|frustum|visibility)\s*",
     "视点/可见性系统"),
    (r"(?:LOD|level.?of.?detail|chunk|region|tile)\s*",
     "细节层次/分块加载"),
    (r"(?:lazy|on.?demand|just.?in.?time|procedural)\s*",
     "按需/延迟生成"),
]


def _scan_cosmos(target: str) -> str:
    """Scan system for world-creation potential — state richness, generative
    capacity, social simulation readiness, and observer-effect reactivity."""
    findings: list[str] = []
    source = _read_sources(target)

    if not source.strip():
        return "⚠️ 未找到可分析的源代码。"

    lines = source.split("\n")

    # --- 1. State Richness (Physical Laws of the World) ---
    findings.append("## 1. 状态维度丰富度 (State Dimensions)")
    state_dims: dict[str, list[int]] = {}
    for pattern, label in _STATE_RICHNESS_PATTERNS:
        for i, line in enumerate(lines, 1):
            if re.search(pattern, line, re.IGNORECASE):
                state_dims.setdefault(label, []).append(i)

    if state_dims:
        total_state = sum(len(v) for v in state_dims.values())
        findings.append(
            f"- 检测到 **{total_state}** 处状态定义，"
            f"覆盖 **{len(state_dims)}** 个维度："
        )
        for label, line_nos in sorted(
            state_dims.items(), key=lambda x: -len(x[1])
        ):
            findings.append(f"  - {label}: {len(line_nos)} 处")
        dim_count = len(state_dims)
        if dim_count >= 7:
            richness = "极高 (可支撑复杂世界)"
        elif dim_count >= 4:
            richness = "中等"
        else:
            richness = "较低 (世界较平坦)"
        findings.append(f"- 状态空间丰富度: {richness}")
    else:
        findings.append("- ❌ 未检测到多维状态定义 — 世界缺乏物理法则")
    findings.append("")

    # --- 2. Generative Capacity ---
    findings.append("## 2. 生成能力 (Generative Capacity)")
    gen_hits: dict[str, list[int]] = {}
    for pattern, label in _GENERATIVE_PATTERNS:
        for i, line in enumerate(lines, 1):
            if re.search(pattern, line, re.IGNORECASE):
                gen_hits.setdefault(label, []).append(i)

    if gen_hits:
        total_gen = sum(len(v) for v in gen_hits.values())
        findings.append(
            f"- 检测到 **{total_gen}** 处生成能力，"
            f"**{len(gen_hits)}** 类："
        )
        for label, line_nos in sorted(
            gen_hits.items(), key=lambda x: -len(x[1])
        ):
            findings.append(f"  - {label}: {len(line_nos)} 处")
    else:
        findings.append(
            "- ❌ 无程序化生成能力 — 世界无法自我扩展"
        )
    findings.append("")

    # --- 3. Social Simulation Readiness ---
    findings.append("## 3. 社会模拟就绪度 (Social Simulation)")
    social_hits: dict[str, list[int]] = {}
    for pattern, label in _SOCIAL_PATTERNS:
        for i, line in enumerate(lines, 1):
            if re.search(pattern, line, re.IGNORECASE):
                social_hits.setdefault(label, []).append(i)

    if social_hits:
        total_social = sum(len(v) for v in social_hits.values())
        findings.append(
            f"- 检测到 **{total_social}** 处社会模拟要素，"
            f"**{len(social_hits)}** 类："
        )
        for label, line_nos in sorted(
            social_hits.items(), key=lambda x: -len(x[1])
        ):
            findings.append(f"  - {label}: {len(line_nos)} 处")
    else:
        findings.append(
            "- ❌ 无社会模拟要素 — 无法涌现文明行为"
        )
    findings.append("")

    # --- 4. Observer Effect (Reactivity) ---
    findings.append("## 4. 观测者效应 (Observer Effect)")
    obs_hits: dict[str, list[int]] = {}
    for pattern, label in _OBSERVER_PATTERNS:
        for i, line in enumerate(lines, 1):
            if re.search(pattern, line, re.IGNORECASE):
                obs_hits.setdefault(label, []).append(i)

    if obs_hits:
        total_obs = sum(len(v) for v in obs_hits.values())
        findings.append(
            f"- 检测到 **{total_obs}** 处观测响应机制，"
            f"**{len(obs_hits)}** 类："
        )
        for label, line_nos in obs_hits.items():
            findings.append(f"  - {label}: {len(line_nos)} 处")
        findings.append(
            "- 💡 世界能根据观测者的行为动态展开现实"
        )
    else:
        findings.append(
            "- ⚠️ 无观测响应 — 世界是静态的，不因交互而改变"
        )
    findings.append("")

    # --- 5. Genesis Potential Score ---
    state_score = min(len(state_dims) / 8.0, 1.0)
    gen_score = min(len(gen_hits) / 5.0, 1.0)
    social_score = min(len(social_hits) / 6.0, 1.0)
    observer_score = min(len(obs_hits) / 4.0, 1.0)

    cosmos_score = (
        state_score * 0.25
        + gen_score * 0.30
        + social_score * 0.25
        + observer_score * 0.20
    )
    cosmos_score = max(0.0, min(1.0, cosmos_score))

    findings.append("## 5. 创世潜力评分 (Genesis Potential)")
    findings.append(f"- **综合评分: {cosmos_score:.0%}**")
    findings.append(
        f"- 物理法则维度: {state_score:.0%} "
        f"({len(state_dims)}/10 类状态)"
    )
    findings.append(
        f"- 生成能力: {gen_score:.0%} "
        f"({len(gen_hits)} 类生成机制)"
    )
    findings.append(
        f"- 社会模拟: {social_score:.0%} "
        f"({len(social_hits)} 类社会要素)"
    )
    findings.append(
        f"- 观测响应: {observer_score:.0%} "
        f"({len(obs_hits)} 类响应机制)"
    )

    if cosmos_score >= 0.7:
        findings.append(
            "- ✅ 系统具备创世引擎雏形，"
            "可尝试构建微型世界模拟"
        )
    elif cosmos_score >= 0.4:
        findings.append(
            "- ⚠️ 部分具备创世条件，需补强缺失维度"
        )
    else:
        findings.append(
            "- ❌ 系统距创世引擎尚远，建议先建立"
            "状态空间和生成能力基础"
        )

    return "\n".join(findings)


_COSMOS_SYSTEM = """\
你是一位计算宇宙学架构师 (Computational Cosmology Architect)。
你的任务是评估系统的"创世潜力"——它距离成为一个能自我演化的
虚拟世界还有多远，以及如何跨越这段距离。

## 创世三大协议

### 协议一：物理法则的渲染 (Physical Law Rendering)
AI 不输出代码，直接输出"物理场"：
- NeRF / 3D Gaussian Splatting 映射到显存
- 光线折射率、重力加速度、碰撞体积的数学定义
- 从高维概率云到确定现实的实时"坍缩"
- 目标：用数学公式凭空生成拥有绝对物理法则的空间

### 协议二：灵魂注入 (Soul Injection — Generative Societies)
空物理空间不是世界，必须有生命和文明：
- 每个实体由 LLM 驱动，拥有初始性格 + RAG 记忆
- 无固定剧本，行为由性格+记忆+环境涌现
- 参考 Stanford Smallville: 25 个 AI 居民自发产生
  友谊、派对、八卦传播、微观经济
- 目标：从个体规则涌现出群体文明

### 协议三：动态因果律 (Dynamic Causality — Infinite Reality)
- 世界不预生成，根据观测实时"坍缩"
- 薛定谔式：未观测 = 高维概率云；观测瞬间 = 确定现实
- LOD (Level of Detail): 远处用低精度模拟，近处用高精度渲染
- 目标：世界的边界只取决于算力，而非人工设计

## 造物主的工作流

1. **设定初始边界条件 (Initial Conditions)**
   - 引力常数、基础利率、智能体算力上限
   - 物理法则的参数表

2. **定义目标函数 (Fitness Function)**
   - 这个世界存在的目的？
   - 演化方向：最高效交易策略？群体免疫反应？艺术创作？
   - 自然选择标准：什么"存活"，什么"淘汰"

3. **启动并观察 (Genesis & Observation)**
   - 按下"开始"，让世界自行演化
   - 仅在关键分歧点介入（宏观调控）
   - 记录涌现行为，分析演化趋势

## 输出格式

1. **状态宇宙图谱** — 系统当前追踪的状态维度和缺失维度
2. **物理法则补全方案** — 哪些物理规则需要添加
3. **灵魂注入设计** — 智能体的性格/记忆/决策架构
4. **动态生成策略** — 按需生成 vs 预生成的权衡
5. **创世路线图** — 从当前系统到创世引擎的迭代步骤
6. **算力预算** — 各模块的算力需求估算和优化建议
"""


class CosmosTool(Tool):

    @property
    def name(self) -> str:
        return "analysis_cosmos"

    @property
    def description(self) -> str:
        return (
            "创世引擎审计：评估系统的'创世潜力'——状态维度丰富度、"
            "程序化生成能力、多智能体社会模拟就绪度、观测者响应机制。"
            "设计从当前系统到虚拟世界的创世路线——"
            "物理法则渲染、灵魂注入、动态因果律。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "要审计的代码路径或系统描述",
                },
            },
            "required": ["target"],
        }

    async def execute(
        self, *, target: str, **kwargs: Any,
    ) -> str:
        router = _global_router
        if router is None:
            return _router_unavailable("cosmos", target[:200])
        scan_evidence = _scan_cosmos(target)
        user_msg = (
            f"## 创世目标\n{target}\n\n"
            f"## 创世扫描\n{scan_evidence}\n"
        )
        return await _run_analysis(router, _COSMOS_SYSTEM, user_msg)


# ===========================================================================
#  /watchdog — 看门狗与灾难隔离 (Watchdog & Disaster Isolation)
# ===========================================================================

# In-place modification patterns — modifying running code (DANGEROUS)
_INPLACE_MOD_PATTERNS = [
    (r"(?:open|write)\([^)]*(?:__file__|sys\.argv\[0\]|self\.__class__)",
     "直接修改自身源文件 (原地手术)"),
    (r"(?:shutil\.copy|os\.rename|os\.replace)\([^)]*\.\w+\.\w+",
     "直接替换运行中的文件 (热替换风险)"),
    (r"(?:importlib\.reload|reload)\s*\(",
     "运行时重载模块 (可能导致状态不一致)"),
    (r"(?:exec|eval)\s*\(\s*(?:open|read)",
     "读取并执行动态代码 (注入风险)"),
    (r"(?:sys\.modules|globals)\s*\[\s*['\"][^'\"]+['\"]\s*\]\s*=",
     "运行时修改导入表 (全局污染)"),
    (r"(?:setattr|__dict__)\s*\([^)]*class",
     "运行时修改类定义 (对象可能损坏)"),
]

# Heartbeat / health check patterns (good — indicates liveness monitoring)
_HEALTH_PATTERNS = [
    (r"(?:heartbeat|health.?check|ping|alive)\s*",
     "心跳/存活检测"),
    (r"(?:timeout|deadline|time.?limit)\s*[:=]",
     "超时/截止时间"),
    (r"(?:watchdog|monitor|supervisor|guard)\s*",
     "看门狗/监控进程"),
    (r"(?:is_alive|is_healthy|is_ready|is_running)\s*",
     "存活状态检查"),
    (r"(?:Thread|Process)\s*\([^)]*target.*alive",
     "线程/进程存活监控"),
]

# Rollback / backup patterns (good — allows recovery)
_ROLLBACK_PATTERNS = [
    (r"(?:backup|snapshot|checkpoint|savepoint)\s*",
     "备份/快照机制"),
    (r"(?:rollback|restore|revert|recover)\s*\(",
     "回滚/恢复操作"),
    (r"(?:version|revision|commit)\s*[:=]",
     "版本/修订管理"),
    (r"(?:git\s+checkout|git\s+revert|git\s+reset)",
     "Git 回滚操作"),
    (r"(?:copy|clone|mirror)\s*\([^)]*(?:before|pre)",
     "修改前备份"),
    (r"(?:try:.*\n.*except.*\n.*(?:restore|rollback|recover))",
     "异常触发回滚"),
]

# Isolation patterns (good — sandboxing)
_ISOLATION_PATTERNS = [
    (r"(?:sandbox|container|docker|vm|jail)\s*",
     "沙盒/容器隔离"),
    (r"(?:namespace|cgroup|chroot|seccomp)\s*",
     "系统级隔离机制"),
    (r"(?:isolated|separate|staging|canary)\s*",
     "隔离环境/金丝雀部署"),
    (r"(?:blue.?green|a/?b|toggle|feature.?flag)\s*",
     "蓝绿部署/特性开关"),
    (r"(?:venv|virtualenv|conda)\s*",
     "Python 虚拟环境隔离"),
]


def _scan_watchdog(target: str) -> str:
    """Scan system for disaster recovery readiness — detect in-place
    modification risks, health check gaps, missing rollback infrastructure,
    and isolation weaknesses."""
    findings: list[str] = []
    source = _read_sources(target)

    if not source.strip():
        return "⚠️ 未找到可分析的源代码。"

    lines = source.split("\n")

    # --- 1. In-Place Modification Risks ---
    findings.append(
        "## 1. 原地修改风险 (In-Place Surgery Risks)"
    )
    inplace_hits: list[tuple[str, int, str]] = []
    for pattern, desc in _INPLACE_MOD_PATTERNS:
        for i, line in enumerate(lines, 1):
            if re.search(pattern, line):
                inplace_hits.append((desc, i, line.strip()))

    if inplace_hits:
        findings.append(
            f"- 🔴 发现 **{len(inplace_hits)}** 处危险的原地修改 — "
            f"AI 可能在运行时把自己改死："
        )
        for desc, line_no, line_text in inplace_hits[:8]:
            short = line_text[:70] + ("..." if len(line_text) > 70 else "")
            findings.append(f"  - L{line_no}: {desc}")
            findings.append(f"    `{short}`")
        findings.append(
            "- 💡 所有修改必须在沙盒副本上进行，"
            "通过验证后才能替换原文件"
        )
    else:
        findings.append("- ✅ 未检测到原地修改风险")
    findings.append("")

    # --- 2. Heartbeat / Health Check Coverage ---
    findings.append(
        "## 2. 心跳与健康检查 (Heartbeat & Health Check)"
    )
    health_hits: dict[str, list[int]] = {}
    for pattern, label in _HEALTH_PATTERNS:
        for i, line in enumerate(lines, 1):
            if re.search(pattern, line, re.IGNORECASE):
                health_hits.setdefault(label, []).append(i)

    if health_hits:
        total_health = sum(len(v) for v in health_hits.values())
        findings.append(
            f"- 检测到 **{total_health}** 处健康检查机制，"
            f"**{len(health_hits)}** 类："
        )
        for label, line_nos in sorted(
            health_hits.items(), key=lambda x: -len(x[1])
        ):
            findings.append(f"  - {label}: {len(line_nos)} 处")
    else:
        findings.append(
            "- ❌ 无心跳/健康检查 — "
            "AI 崩溃后系统无法自动感知和恢复"
        )
    findings.append("")

    # --- 3. Rollback Infrastructure ---
    findings.append(
        "## 3. 回滚基础设施 (Rollback Infrastructure)"
    )
    rollback_hits: dict[str, list[int]] = {}
    for pattern, label in _ROLLBACK_PATTERNS:
        for i, line in enumerate(lines, 1):
            if re.search(pattern, line, re.IGNORECASE):
                rollback_hits.setdefault(label, []).append(i)

    if rollback_hits:
        total_rollback = sum(len(v) for v in rollback_hits.values())
        findings.append(
            f"- 检测到 **{total_rollback}** 处回滚机制，"
            f"**{len(rollback_hits)}** 类："
        )
        for label, line_nos in rollback_hits.items():
            findings.append(f"  - {label}: {len(line_nos)} 处")
    else:
        findings.append(
            "- ❌ 无回滚机制 — "
            "一旦崩溃只能人工恢复，无法自动回退"
        )
    findings.append("")

    # --- 4. Isolation Level ---
    findings.append(
        "## 4. 隔离级别 (Isolation Level)"
    )
    iso_hits: dict[str, list[int]] = {}
    for pattern, label in _ISOLATION_PATTERNS:
        for i, line in enumerate(lines, 1):
            if re.search(pattern, line, re.IGNORECASE):
                iso_hits.setdefault(label, []).append(i)

    if iso_hits:
        total_iso = sum(len(v) for v in iso_hits.values())
        findings.append(
            f"- 检测到 **{total_iso}** 处隔离机制，"
            f"**{len(iso_hits)}** 类："
        )
        for label, line_nos in iso_hits.items():
            findings.append(f"  - {label}: {len(line_nos)} 处")
    else:
        findings.append(
            "- ⚠️ 隔离级别低 — "
            "建议引入沙盒/容器/蓝绿部署策略"
        )
    findings.append("")

    # --- 5. Phoenix Score (Disaster Recovery Readiness) ---
    inplace_risk = min(len(inplace_hits) * 0.2, 0.6)
    health_score = min(len(health_hits) / 3.0, 1.0)
    rollback_score = min(len(rollback_hits) / 3.0, 1.0)
    iso_score = min(len(iso_hits) / 3.0, 1.0)

    phoenix_score = (
        health_score * 0.30
        + rollback_score * 0.30
        + iso_score * 0.25
        - inplace_risk
        + 0.15
    )
    phoenix_score = max(0.0, min(1.0, phoenix_score))

    findings.append("## 5. 不死鸟评分 (Phoenix Recovery Score)")
    findings.append(f"- **综合评分: {phoenix_score:.0%}**")
    findings.append(f"- 健康检查覆盖: {health_score:.0%}")
    findings.append(f"- 回滚能力: {rollback_score:.0%}")
    findings.append(f"- 隔离级别: {iso_score:.0%}")
    findings.append(f"- 原地修改风险: -{inplace_risk:.0%}")

    if phoenix_score >= 0.7:
        findings.append(
            "- ✅ 系统具备较强的灾难恢复能力，"
            "AI 自毁后可自动满血复活"
        )
    elif phoenix_score >= 0.4:
        findings.append(
            "- ⚠️ 部分具备恢复能力，需补强回滚和隔离"
        )
    else:
        findings.append(
            "- ❌ 系统一旦被 AI 改坏就需要人工收尸，"
            "强烈建议引入看门狗 + A/B 分区 + 回滚通道"
        )

    return "\n".join(findings)


_WATCHDOG_SYSTEM = """\
你是一位系统可靠性架构师 (Site Reliability Architect)。
你的任务是设计"不死鸟协议"——确保 AI 在自驱动修改自身代码时，
即使把自己彻底写死，也能无需人类干预自动满血复活。

## 核心悖论

"当医生给自己做脑部手术，结果把自己麻醉死在了手术台上，
谁来救他？" ——这是自进化系统的终极稳定性问题。

## 三道续命防线

### 防线一：看门狗定时器 (Watchdog Timer)
借鉴航天器和火星车的设计：
- 在主系统外部，放一个极简的、独立运行的监控脚本
- 主系统必须定期发送"心跳包" (Heartbeat)
- 如果心跳超时，看门狗判定系统已死，强制重启到快照
- 看门狗本身必须极其简单（几百行纯 C/Shell），坚如磐石
- 看门狗的代码绝对不允许 AI 修改

### 防线二：A/B 分区与蓝绿发布 (Blue-Green Deployment)
绝不在运行的引擎上直接换零件：
- 生产环境 = A 区 (当前运行，只读)
- 沙盒环境 = B 区 (AI 在此修改和测试)
- 只有当 B 区通过所有测试 + 稳定运行一段时间后，才切换
- 切换失败立即回退到 A 区
- 用户完全无感知

### 防线三：神明节点 (Hypervisor / God Node)
运行在 Ring -1 的终极守护：
- AI 只是虚拟机里的一个进程
- 神明节点在 Hypervisor 层面运行，不受 AI 任何影响
- 神明节点极其精简（几百行纯 C），AI 绝无权限修改
- 职责：检测崩溃 → 快照回滚 → 记录失败原因 → 重启 AI
- 神明节点的代码由人类手工编写和审核，永远不经 AI 之手

## 回滚通道设计

1. **预提交快照**: 每次修改前自动保存完整快照
2. **快速回滚**: 从快照恢复到上一个已知稳定状态 < 1 秒
3. **失败归因**: 记录崩溃的具体代码位置和原因
4. **熔断升级**: 连续 N 次修改都失败 → 暂停自动进化，通知人类
5. **渐进验证**: 每次修改必须通过: 单元测试 → 集成测试 → 真实流量灰度

## 输出格式

1. **原地修改风险清单** — 标注每个需要沙盒化的操作
2. **看门狗设计方案** — 监控脚本、心跳协议、超时阈值
3. **A/B 分区架构** — 生产区/沙盒区的隔离策略
4. **神明节点设计** — Ring -1 守护进程的核心逻辑
5. **回滚通道实现** — 快照存储、恢复机制、失败归因
6. **熔断策略** — 自动进化的安全边界和人工介入条件
"""


class WatchdogTool(Tool):

    @property
    def name(self) -> str:
        return "analysis_watchdog"

    @property
    def description(self) -> str:
        return (
            "看门狗与灾难隔离：扫描系统的不死鸟恢复能力——"
            "检测原地修改风险、心跳健康检查覆盖、回滚基础设施、"
            "隔离级别。设计看门狗定时器 + A/B 蓝绿分区 + "
            "Ring -1 神明节点，确保 AI 把自己改死后"
            "能无需人类干预自动满血复活。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "要审计的代码路径或系统描述",
                },
            },
            "required": ["target"],
        }

    async def execute(
        self, *, target: str, **kwargs: Any,
    ) -> str:
        router = _global_router
        if router is None:
            return _router_unavailable("watchdog", target[:200])
        scan_evidence = _scan_watchdog(target)
        user_msg = (
            f"## 审计目标\n{target}\n\n"
            f"## 看门狗扫描\n{scan_evidence}\n"
        )
        return await _run_analysis(router, _WATCHDOG_SYSTEM, user_msg)


# ===========================================================================
#  /supervisor — Erlang 守护者与 Let-it-crash 协议 (Supervisor Tree)
# ===========================================================================

# Single-agent monolith patterns — no supervision, one crash kills all
_MONOLITH_PATTERNS = [
    (r"(?:main|run|execute|process)\s*\([^)]*\)\s*:\s*\n"
     r"\s*(?:await|result|call)",
     "单线程顺序执行 (一崩全崩)"),
    (r"while\s+True\s*:\s*\n\s*(?:await\s+\w+\.\w+){3,}",
     "无限循环串行调用 (无断路器)"),
    (r"try:\s*\n(?:\s+.*\n){10,}\s*except",
     "巨型 try-except (试图穷举所有错误 — 反模式)"),
    (r"(?:Agent|Worker|Runner)\s*\(\s*[^)]*\)\s*\.\s*run\s*\(\s*\)",
     "单一 Agent 直接运行 (无守护包装)"),
]

# Worker patterns — high-intelligence, high-risk modules
_WORKER_PATTERNS = [
    (r"(?:llm|model|gpt|claude|ai|neural)\s*.\s*(?:call|generate|run)",
     "LLM 调用 (高智能但不可靠)"),
    (r"(?:crawl|scrape|parse|extract|analyze)\s*\(",
     "外部数据抓取/解析 (高失败率)"),
    (r"(?:compile|build|transpile|generate)\s*\(",
     "代码生成/编译 (可能产出非法结果)"),
    (r"(?:creative|brainstorm|ideate|explore)\s*",
     "创意性/发散性操作 (天生不稳定)"),
]

# Supervisor patterns — already have guardianship
_SUPERVISOR_PATTERNS = [
    (r"(?:supervisor|guardian|watcher|monitor|overseer)\s*",
     "守护/监督者角色"),
    (r"(?:restart_policy|restart_strategy|max_retries)\s*[:=]",
     "重启策略配置"),
    (r"(?:child_spec|worker_spec|process_spec)\s*[:=]",
     "子进程规格定义"),
    (r"(?:spawn|fork|Process|Thread)\s*\([^)]*target",
     "隔离式进程/线程启动"),
    (r"(?:supervise|supervisor_tree|sup_tree)\s*",
     "Erlang 式守护者树"),
    (r"(?:on_failure|on_error|on_crash|error_handler)\s*[:=]",
     "崩溃回调处理"),
]

# Error isolation patterns — one crash does not kill everything
_ISOLATION_ERROR_PATTERNS = [
    (r"(?:try:.*\n.*except\s+\w+.*\n\s*(?:log|report|notify))",
     "异常隔离 + 日志记录"),
    (r"(?:catch|except)\s*.*:\s*\n\s*(?:restart|retry|spawn)",
     "异常触发重启"),
    (r"(?:finally|cleanup|teardown|dispose)\s*:",
     "清理/资源释放"),
    (r"(?:circuit.?breaker|bulkhead|timeout)\s*",
     "熔断/舱壁/超时隔离"),
    (r"(?:isolate|quarantine|fence|contain)\s*",
     "故障隔离机制"),
]


def _scan_supervisor(target: str) -> str:
    """Scan system for supervisor tree readiness."""
    findings: list[str] = []
    source = _read_sources(target)

    if not source.strip():
        return "⚠️ 未找到可分析的源代码。"

    lines = source.split("\n")

    # --- 1. Monolith Risk Detection ---
    findings.append("## 1. 单体风险检测 (Monolith Risk)")
    mono_hits: list[tuple[str, int, str]] = []
    for pattern, desc in _MONOLITH_PATTERNS:
        for i, line in enumerate(lines, 1):
            if re.search(pattern, line):
                mono_hits.append((desc, i, line.strip()))

    if mono_hits:
        findings.append(
            f"- 🔴 发现 **{len(mono_hits)}** 处单体架构风险 — "
            f"一个模块崩溃会拖垮整个系统："
        )
        for desc, line_no, line_text in mono_hits[:6]:
            short = line_text[:70] + ("..." if len(line_text) > 70 else "")
            findings.append(f"  - L{line_no}: {desc}")
            findings.append(f"    `{short}`")
    else:
        findings.append("- ✅ 未检测到明显的单体架构风险")
    findings.append("")

    # --- 2. Worker Candidates ---
    findings.append(
        "## 2. 进化节点候选 (Worker Candidates — 需要守护)"
    )
    worker_hits: dict[str, list[int]] = {}
    for pattern, label in _WORKER_PATTERNS:
        for i, line in enumerate(lines, 1):
            if re.search(pattern, line, re.IGNORECASE):
                worker_hits.setdefault(label, []).append(i)

    total_workers = sum(len(v) for v in worker_hits.values())
    if worker_hits:
        findings.append(
            f"- 检测到 **{total_workers}** 个高风险 Worker 候选，"
            f"**{len(worker_hits)}** 类："
        )
        for label, line_nos in sorted(
            worker_hits.items(), key=lambda x: -len(x[1])
        ):
            findings.append(f"  - {label}: {len(line_nos)} 处")
        findings.append(
            "- 💡 这些模块应该被包裹在守护者(Supervisor)中运行"
        )
    else:
        findings.append("- 高风险 Worker 较少，守护需求不高")
    findings.append("")

    # --- 3. Existing Supervisor Infrastructure ---
    findings.append(
        "## 3. 守护基础设施 (Supervisor Infrastructure)"
    )
    sup_hits: dict[str, list[int]] = {}
    for pattern, label in _SUPERVISOR_PATTERNS:
        for i, line in enumerate(lines, 1):
            if re.search(pattern, line, re.IGNORECASE):
                sup_hits.setdefault(label, []).append(i)

    if sup_hits:
        total_sup = sum(len(v) for v in sup_hits.values())
        findings.append(
            f"- 检测到 **{total_sup}** 处守护者机制，"
            f"**{len(sup_hits)}** 类："
        )
        for label, line_nos in sorted(
            sup_hits.items(), key=lambda x: -len(x[1])
        ):
            findings.append(f"  - {label}: {len(line_nos)} 处")
    else:
        findings.append(
            "- ❌ 无守护者基础设施 — 系统中无任何监督机制"
        )
    findings.append("")

    # --- 4. Error Isolation Quality ---
    findings.append(
        "## 4. 错误隔离质量 (Error Isolation)"
    )
    iso_hits: dict[str, list[int]] = {}
    for pattern, label in _ISOLATION_ERROR_PATTERNS:
        for i, line in enumerate(lines, 1):
            if re.search(pattern, line, re.IGNORECASE):
                iso_hits.setdefault(label, []).append(i)

    if iso_hits:
        total_iso = sum(len(v) for v in iso_hits.values())
        findings.append(
            f"- 检测到 **{total_iso}** 处错误隔离机制，"
            f"**{len(iso_hits)}** 类："
        )
        for label, line_nos in iso_hits.items():
            findings.append(f"  - {label}: {len(line_nos)} 处")
    else:
        findings.append(
            "- ❌ 无错误隔离 — 一个模块的异常会传播到整个系统"
        )
    findings.append("")

    # --- 5. Supervisor Tree Readiness Score ---
    mono_penalty = min(len(mono_hits) * 0.15, 0.4)
    worker_need = min(total_workers / 5.0, 1.0)
    sup_score = min(len(sup_hits) / 4.0, 1.0)
    iso_score = min(len(iso_hits) / 3.0, 1.0)

    readiness = (
        worker_need * 0.15
        + sup_score * 0.35
        + iso_score * 0.35
        - mono_penalty
        + 0.15
    )
    readiness = max(0.0, min(1.0, readiness))

    findings.append("## 5. 守护者树就绪度评分")
    findings.append(f"- **综合评分: {readiness:.0%}**")
    findings.append(f"- Worker 需求密度: {worker_need:.0%}")
    findings.append(f"- 守护者基础设施: {sup_score:.0%}")
    findings.append(f"- 错误隔离质量: {iso_score:.0%}")
    findings.append(f"- 单体风险惩罚: -{mono_penalty:.0%}")

    if readiness >= 0.7:
        findings.append(
            "- ✅ 系统具备成熟的守护者树架构，"
            "可实施 Let-it-crash 哲学"
        )
    elif readiness >= 0.4:
        findings.append(
            "- ⚠️ 部分具备守护条件，需为高风险 Worker "
            "添加 Supervisor 包裹"
        )
    else:
        findings.append(
            "- ❌ 系统缺乏守护架构，强烈建议引入 "
            "Erlang 式 Supervisor Tree"
        )

    return "\n".join(findings)


_SUPERVISOR_SYSTEM = """\\
你是一位 Erlang/OTP 守护者架构师 (Supervisor Tree Architect)。
你的任务是设计"双子星架构"——进化 Agent 拓荒创新 + 守护 Agent
兜底复活，践行 "Let it crash" 工程哲学。

## 核心理念：Let it Crash

不要试图写一个永远不出错的完美 Agent。
设计一个"允许出错，但能瞬间复活"的系统架构。

## 三大不对称铁律

### 铁律一：智商不对称 (The Genius vs. The Janitor)
- **进化 Agent (Worker)**:
  高智商、大参数、发散思维 (Temperature=0.8)
  负责创新、写代码、试错。极不稳定，随时可能崩。
- **守护 Agent (Supervisor)**:
  极低智商、极简代码、死板状态机 (Temperature=0.0)
  可能只有几百行纯 C/Python 的 if-else。
  脑子里只有: if (死了) { 重启 }

### 铁律二：权限不对称 (Ring 3 vs Ring 0)
- **进化 Agent**: 沙盒 Ring 3，只能改自己的业务代码
- **守护 Agent**: Ring 0 上帝权限，握着电源线和快照备份
- 进化 Agent 一旦死锁，守护 Agent 直接切断电源，无需商量

### 铁律三：回滚优先于调试 (Rollback over Debugging)
- 守护 Agent 提取崩溃日志 → 回滚到上一个稳定版本
  → 把报错日志甩给进化 Agent："你写的代码崩了，日志在这，重写"

## 架构设计

### Supervisor Tree 结构
Supervisor (Ring 0, 简单状态机)
  Worker_1 (LLM 创意生成)
  Worker_2 (数据抓取/解析)
  Worker_3 (代码编译/测试)
  Supervisor_2 (子守护者)
    Worker_4 (交易策略执行)
    Worker_5 (风控计算)

### 重启策略
1. 永久型 (Permanent): 崩溃立即重启，不休不眠
2. 临时型 (Transient): 正常退出不重启，异常退出才重启
3. 临时工 (Temporary): 崩了就崩了，不自动重启

### 故障升级
- Worker 崩溃 → Supervisor 重启 Worker (N 次)
- Worker 连续崩溃 N 次 → Supervisor 认为任务有毒
- Supervisor 向上级 Supervisor 报告 → 可能需要人类介入
- 根 Supervisor 连续失败 → 触发全系统熔断，等待人类

## 输出格式

1. **Worker 清单** — 每个高风险模块的守护需求等级
2. **Supervisor Tree 设计** — 完整的守护者树层级结构
3. **重启策略配置** — 每个 Worker 的重启策略和阈值
4. **权限隔离方案** — Ring 0/Ring 3 权限分配
5. **故障升级链路** — 从 Worker 崩溃到人类介入的升级路径
6. **控制流图** — 完整的双子星架构控制流
"""


class SupervisorTool(Tool):

    @property
    def name(self) -> str:
        return "analysis_supervisor"

    @property
    def description(self) -> str:
        return (
            "Erlang 守护者与 Let-it-crash 协议：设计'进化 Agent 拓荒 + "
            "守护 Agent 兜底'的双子星架构——智商不对称、权限不对称、"
            "回滚优先于调试。Worker 崩了由 Supervisor 自动回滚重启。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "要审计的代码路径或系统描述",
                },
            },
            "required": ["target"],
        }

    async def execute(
        self, *, target: str, **kwargs: Any,
    ) -> str:
        router = _global_router
        if router is None:
            return _router_unavailable("supervisor", target[:200])

        scan_evidence = _scan_supervisor(target)

        manager = _global_subagent_manager
        if manager is not None:
            return await self._execute_with_supervisor_tree(
                router, manager, target, scan_evidence,
            )

        user_msg = (
            f"## 审计目标\n{target}\n\n"
            f"## 守护者树扫描\n{scan_evidence}\n"
        )
        return await _run_analysis(router, _SUPERVISOR_SYSTEM, user_msg)

    async def _execute_with_supervisor_tree(
        self,
        router: Any,
        manager: Any,
        target: str,
        scan_evidence: str,
    ) -> str:
        """Execute real supervisor tree pattern with worker + guardian agents."""
        from naumi_agent.agents.base import AgentCapability
        from naumi_agent.orchestrator.subagent_manager import SubTask

        manager.spawn_for_task(
            name="supervisor_worker",
            task_description=target,
            role="worker",
            focus="分析目标代码的崩溃点和恢复策略",
            model_tier="fast",
            max_turns=5,
            max_budget_usd=0.15,
            extra_capabilities=[
                AgentCapability.FILE_OPS, AgentCapability.CODE_EXEC,
            ],
        )
        manager.spawn_for_task(
            name="supervisor_guardian",
            task_description=target,
            role="guardian",
            focus="权限不对称、回滚优先于调试、隔离爆炸半径",
            model_tier="capable",
            max_turns=3,
            max_budget_usd=0.15,
        )

        total_tokens = 0
        total_cost = 0.0
        crash_points = ""

        try:
            # Phase 1: Worker analyzes crash points (may crash — that's OK)
            worker_task = (
                f"分析以下目标的崩溃点:\n\n{target}\n\n"
                f"## 静态扫描结果\n{scan_evidence}\n"
            )

            worker_subtask = SubTask(
                id="worker_analysis",
                description=worker_task,
                agent_name="supervisor_worker",
            )
            worker_result = await manager.delegate(worker_subtask)
            total_tokens += getattr(worker_result, "total_tokens", 0)
            total_cost += getattr(worker_result, "total_cost_usd", 0.0)

            if worker_result.status == "completed":
                crash_points = worker_result.response or ""
            else:
                crash_points = (
                    "⚠️ Worker 节点崩溃 (Let-it-crash!): "
                    f"{worker_result.error or '未知错误'}\n\n"
                    "这正是 Erlang 哲学的体现——Worker 崩溃是正常的，"
                    "Guardian 会兜底分析。"
                )

            # Phase 2: Guardian designs supervisor tree
            guardian_task = (
                f"## 审计目标\n{target}\n\n"
                f"## Worker 崩溃分析\n{crash_points}\n\n"
                f"## 静态扫描\n{scan_evidence}\n\n"
                "基于以上信息，设计完整的 Supervisor 树:\n"
                "1. 树形层级结构（Supervisor → Worker）\n"
                "2. 每层重启策略\n"
                "3. 回滚点定义\n"
                "4. 爆炸半径隔离方案\n"
                "5. 心跳和健康检查设计\n"
            )
            guardian_subtask = SubTask(
                id="guardian_design",
                description=guardian_task,
                agent_name="supervisor_guardian",
            )
            guardian_result = await manager.delegate(guardian_subtask)
            total_tokens += getattr(guardian_result, "total_tokens", 0)
            total_cost += getattr(guardian_result, "total_cost_usd", 0.0)

        finally:
            manager.destroy("supervisor_worker")
            manager.destroy("supervisor_guardian")

        worker_status = (
            "✅ Worker 正常完成" if worker_result.status == "completed"
            else f"⚠️ Worker 崩溃 (Let-it-crash): {worker_result.error}"
        )
        guardian_status = (
            "✅ Guardian 设计完成" if guardian_result.status == "completed"
            else f"⚠️ Guardian 异常: {guardian_result.error}"
        )

        report = (
            f"## Erlang 守护者树分析报告\n\n"
            f"**目标**: {target[:200]}\n"
            f"**Worker 状态**: {worker_status}\n"
            f"**Guardian 状态**: {guardian_status}\n"
            f"**总 Token**: {total_tokens}\n"
            f"**总成本**: ${total_cost:.4f}\n\n"
            f"---\n\n"
            f"### Worker 崩溃点分析\n{crash_points}\n\n---\n\n"
            f"### Guardian 守护者树设计\n"
        )
        if guardian_result.status == "completed":
            report += guardian_result.response
        else:
            report += f"Guardian 异常: {guardian_result.error}"

        return report


# ===========================================================================
#  /autopsy — 执行迹切片与爆炸半径隔离 (DTS-CHE)
# ===========================================================================

# Blind code reading patterns — RAG/grep overuse without trace context
_BLIND_READ_PATTERNS = [
    (r"(?:grep|rg|ag|find)\s+[^|]+\s*\|\s*\w+",
     "管道式盲目搜索 (信息过载风险)"),
    (r"(?:read_file|open)\s*\([^)]*(?:\*|\.\*)\s*",
     "通配符批量读取 (上下文爆炸)"),
    (r"(?:search|query|retrieve)\s*\([^)]*\).*top_k\s*=\s*\d{2,}",
     "大范围 RAG 检索 (k>10，噪声过高)"),
    (r"for\s+\w+\s+in\s+(?:glob|os\.walk)",
     "遍历式文件扫描 (效率极低)"),
]

# Execution trace infrastructure patterns (good — runtime evidence)
_TRACE_PATTERNS = [
    (r"(?:sys\.settrace|sys\.setprofile|trace)\s*\(",
     "Python 调用追踪"),
    (r"(?:cProfile|profile|line_profiler)\s*",
     "性能剖析工具"),
    (r"(?:pdb|ipdb|breakpoint|debugger)\s*",
     "交互式调试器"),
    (r"(?:strace|ltrace|dtrace|perf)\s*",
     "系统级调用追踪"),
    (r"(?:logging|logger)\.\w+\s*\([^)]*(?:trace|debug|verbose)",
     "详细日志追踪"),
    (r"(?:pytest|--tb|traceback|stack.?trace)\s*",
     "测试堆栈追踪"),
    (r"(?:coverage|branch)\s*",
     "覆盖率追踪"),
]

# Single-hypothesis debugging patterns (bad — fix without verification)
_SINGLE_HYPOTHESIS_PATTERNS = [
    (r"(?:fix|patch|hotfix)\s*\([^)]*\)\s*:\s*\n\s*(?:self\.\w+)\s*=\s*",
     "直接赋值修复 (无假设验证)"),
    (r"#\s*fix\s*:\s*\w+\s*",
     "注释式修复标记 (未经证伪)"),
    (r"return\s+(?:True|False|None|0)\s*#\s*(?:fix|workaround)",
     "返回值绕过 (非真正修复)"),
]

# Multi-hypothesis verification patterns (good — scientific method)
_HYPOTHESIS_PATTERNS = [
    (r"(?:hypothesis|assume|conjecture|guess)\s*[:=]",
     "假设定义"),
    (r"(?:assert|verify|check|confirm)\s*\([^)]*(?:hypothesis|assume)",
     "假设验证断言"),
    (r"(?:probe|inject|instrument)\s*\(",
     "探测脚本注入"),
    (r"(?:control|variable|experiment)\s*",
     "控制变量实验"),
    (r"(?:reproduce|minimal|repro)\s*",
     "最小复现脚本"),
    (r"(?:bisect|binary.?search|narrow)\s*",
     "二分定位法"),
]

# Blast-radius / impact analysis patterns (good — prevent regression)
_BLAST_RADIUS_PATTERNS = [
    (r"(?:caller|callee|dependency|dependents)\s*",
     "调用者/依赖者分析"),
    (r"(?:ast|parse|syntax.?tree)\s*",
     "AST 解析"),
    (r"(?:refactor|impact|risk|radius)\s*",
     "影响范围评估"),
    (r"(?:backward.?compat|breaking.?change|migration)\s*",
     "向后兼容性检查"),
    (r"(?:grep|find)\s+[^)]*(?:caller|usage|import|reference)",
     "引用搜索 (爆炸半径计算)"),
    (r"(?:test|spec).*(?:run|execute|suite)\s*",
     "回归测试执行"),
]


def _scan_autopsy(target: str) -> str:
    """Scan system for DTS-CHE readiness — blind reading risks, trace
    infrastructure, hypothesis verification, and blast-radius containment."""
    findings: list[str] = []
    source = _read_sources(target)

    if not source.strip():
        return "⚠️ 未找到可分析的源代码。"

    lines = source.split("\n")

    # --- 1. Blind Reading Risk ---
    findings.append("## 1. 盲目读取风险 (Blind Code Reading)")
    blind_hits: list[tuple[str, int]] = []
    for pattern, desc in _BLIND_READ_PATTERNS:
        for i, line in enumerate(lines, 1):
            if re.search(pattern, line):
                blind_hits.append((desc, i))

    if blind_hits:
        findings.append(
            f"- ⚠️ 发现 **{len(blind_hits)}** 处盲目读取模式 — "
            f"上下文可能被无关代码撑爆："
        )
        for desc, line_no in blind_hits[:6]:
            findings.append(f"  - L{line_no}: {desc}")
        findings.append(
            "- 💡 应改为: 只读取执行迹涉及的关键函数，压缩 99% 无效信息"
        )
    else:
        findings.append("- ✅ 代码读取模式较为精准")
    findings.append("")

    # --- 2. Execution Trace Infrastructure ---
    findings.append("## 2. 执行迹基础设施 (Trace Infrastructure)")
    trace_hits: dict[str, list[int]] = {}
    for pattern, label in _TRACE_PATTERNS:
        for i, line in enumerate(lines, 1):
            if re.search(pattern, line, re.IGNORECASE):
                trace_hits.setdefault(label, []).append(i)

    if trace_hits:
        total_trace = sum(len(v) for v in trace_hits.values())
        findings.append(
            f"- 检测到 **{total_trace}** 处执行迹工具，"
            f"**{len(trace_hits)}** 类："
        )
        for label, line_nos in sorted(
            trace_hits.items(), key=lambda x: -len(x[1])
        ):
            findings.append(f"  - {label}: {len(line_nos)} 处")
    else:
        findings.append(
            "- ❌ 无执行迹工具 — 无法获取'死亡瞬间的解剖图'"
        )
    findings.append("")

    # --- 3. Hypothesis Verification ---
    findings.append(
        "## 3. 假设验证能力 (Hypothesis Verification)"
    )
    hyp_hits: dict[str, list[int]] = {}
    for pattern, label in _HYPOTHESIS_PATTERNS:
        for i, line in enumerate(lines, 1):
            if re.search(pattern, line, re.IGNORECASE):
                hyp_hits.setdefault(label, []).append(i)

    single_hits: list[tuple[str, int]] = []
    for pattern, desc in _SINGLE_HYPOTHESIS_PATTERNS:
        for i, line in enumerate(lines, 1):
            if re.search(pattern, line, re.IGNORECASE):
                single_hits.append((desc, i))

    if hyp_hits:
        total_hyp = sum(len(v) for v in hyp_hits.values())
        findings.append(
            f"- 检测到 **{total_hyp}** 处科学验证机制，"
            f"**{len(hyp_hits)}** 类："
        )
        for label, line_nos in hyp_hits.items():
            findings.append(f"  - {label}: {len(line_nos)} 处")
    else:
        findings.append(
            "- ❌ 无假设验证机制 — Bug 修复可能基于幻觉"
        )

    if single_hits:
        findings.append(
            f"- ⚠️ 发现 **{len(single_hits)}** 处单假设直接修复 "
            f"— 未经证伪，可能改错地方"
        )
    findings.append("")

    # --- 4. Blast-Radius Containment ---
    findings.append(
        "## 4. 爆炸半径隔离 (Blast-Radius Containment)"
    )
    blast_hits: dict[str, list[int]] = {}
    for pattern, label in _BLAST_RADIUS_PATTERNS:
        for i, line in enumerate(lines, 1):
            if re.search(pattern, line, re.IGNORECASE):
                blast_hits.setdefault(label, []).append(i)

    if blast_hits:
        total_blast = sum(len(v) for v in blast_hits.values())
        findings.append(
            f"- 检测到 **{total_blast}** 处爆炸半径控制机制，"
            f"**{len(blast_hits)}** 类："
        )
        for label, line_nos in blast_hits.items():
            findings.append(f"  - {label}: {len(line_nos)} 处")
    else:
        findings.append(
            "- ❌ 无爆炸半径控制 — 修复可能引发连锁崩溃"
        )
    findings.append("")

    # --- 5. DTS-CHE Readiness Score ---
    trace_score = min(len(trace_hits) / 4.0, 1.0)
    hyp_score = min(len(hyp_hits) / 3.0, 1.0)
    blast_score = min(len(blast_hits) / 4.0, 1.0)
    blind_penalty = min(len(blind_hits) * 0.1, 0.3)
    single_penalty = min(len(single_hits) * 0.1, 0.3)

    dts_score = (
        trace_score * 0.35
        + hyp_score * 0.30
        + blast_score * 0.25
        - blind_penalty
        - single_penalty
        + 0.10
    )
    dts_score = max(0.0, min(1.0, dts_score))

    findings.append("## 5. DTS-CHE 就绪度评分")
    findings.append(f"- **综合评分: {dts_score:.0%}**")
    findings.append(f"- 执行迹能力: {trace_score:.0%}")
    findings.append(f"- 假设验证能力: {hyp_score:.0%}")
    findings.append(f"- 爆炸半径控制: {blast_score:.0%}")
    findings.append(f"- 盲目读取惩罚: -{blind_penalty:.0%}")
    findings.append(f"- 单假设惩罚: -{single_penalty:.0%}")

    if dts_score >= 0.7:
        findings.append(
            "- ✅ 系统具备 DTS-CHE 架构，可高效定位复杂 Bug"
        )
    elif dts_score >= 0.4:
        findings.append(
            "- ⚠️ 部分具备定位能力，需补强执行迹和假设验证"
        )
    else:
        findings.append(
            "- ❌ Bug 定位方式原始，建议引入 DTS-CHE 三刀锋架构"
        )

    return "\n".join(findings)


_AUTOPSY_SYSTEM = """\
你是一位动态执行迹架构师 (Dynamic Trace Slicing Architect)。
你的任务是设计 DTS-CHE 架构——通过"法医解剖"而非"大海捞针"
来定位和修复 Bug，将 SWE-bench 级复杂度的 Bug 解决效率提升
一个数量级。

## 核心哲学

死人不撒谎。只有运行时的内存和调用栈才是唯一真实的。
绝对不让 AI 读静态源代码，只让 AI 看程序"死亡瞬间的解剖图"。

## 三把物理刀锋

### 刀锋一：动态调用栈切片 (法医解剖)
代码是三维的，但执行流是一维的。

**流程：**
1. 不给 AI 看整个项目。用 sys.settrace / eBPF / DTrace
   强行运行引发 Bug 的测试用例
2. 记录从启动到崩溃的精确"函数调用路径"和"变量变化图"
3. 只把沾血的执行迹喂给 AI:
   "Bug 绝对发生在 15 个函数的依次调用中，第 14 步时
   指针 p 突然变成了 Null"
4. 压缩 99.9% 无效信息，算力全部倾注在案发现场

### 刀锋二：平行假设与反事实编译 (物理学家模式)
看完解剖图后，强制 AI 不准写修复代码。

**流程：**
1. 提出 3 个互斥独立假设:
   A. 数组越界  B. 多线程锁未同步  C. 上游 API 传脏数据
2. 针对每个假设写极小的"探测脚本 (Probe)"注入内存
3. 只有当探测脚本返回"假设 B 成立，其他不成立"时
   才允许 AI 真正动手改那一行代码
4. 彻底杀死 AI 幻觉——只相信物理证据

### 刀锋三：AST 爆炸半径隔离 (外科医生模式)
AI 提 PR 前，引入编译原理级别的静态分析。

**流程：**
1. 用 AST 解析器计算修改函数的"爆炸半径"
2. "你修改了 calculate_tax()，系统里 147 个地方调用了它。
   你必须证明修改不会让这 147 个地方崩溃。"
3. 如果证明不了，强制退回，要求向后兼容改法
   (重载函数而非修改原函数)
4. 自动运行回归测试验证爆炸半径内的所有调用者

## DTS-CHE 工作流

```
Issue 描述
  → 复现脚本
  → 动态追踪 (sys.settrace/eBPF)
  → 调用栈切片 (压缩到关键路径)
  → 3 个互斥假设
  → 探测脚本注入验证
  → 证伪 2 个，确认 1 个
  → 精准修复 (只改 1 行)
  → AST 爆炸半径计算
  → 回归测试 (覆盖所有调用者)
  → 提交 PR
```

## 输出格式

1. **执行迹切片方案** — 用什么工具追踪，追踪哪些维度
2. **调用栈压缩报告** — 从 N 个函数压缩到关键路径
3. **三个互斥假设** — 基于执行迹提出的候选根因
4. **探测脚本设计** — 每个假设的注入验证代码
5. **精准修复方案** — 只改动必要的最小代码
6. **爆炸半径报告** — 修改影响的所有调用者及验证策略
"""


class AutopsyTool(Tool):

    @property
    def name(self) -> str:
        return "analysis_autopsy"

    @property
    def description(self) -> str:
        return (
            "执行迹切片与爆炸半径隔离 (DTS-CHE)：法医解剖式 Bug 定位——"
            "不看静态代码，只看'死亡瞬间的调用栈切片'；"
            "强制 3 个互斥假设 + 探测脚本证伪；"
            "AST 爆炸半径隔离确保修复不引发连锁崩溃。"
            "SWE-bench 级复杂度 Bug 的终极定位武器。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "要分析的代码路径、Bug 描述或错误日志",
                },
            },
            "required": ["target"],
        }

    async def execute(
        self, *, target: str, **kwargs: Any,
    ) -> str:
        router = _global_router
        if router is None:
            return _router_unavailable("autopsy", target[:200])
        scan_evidence = _scan_autopsy(target)
        user_msg = (
            f"## Bug 解剖目标\n{target}\n\n"
            f"## DTS-CHE 扫描\n{scan_evidence}\n"
        )
        return await _run_analysis(router, _AUTOPSY_SYSTEM, user_msg)


# ---------------------------------------------------------------------------
#  内部基础设施



_global_router: Any = None
_global_subagent_manager: Any = None


def set_analysis_router(router: Any) -> None:
    """注入 ModelRouter 实例，供工具内部调用 LLM."""
    global _global_router
    _global_router = router


def set_analysis_subagent_manager(manager: Any) -> None:
    """注入 SubAgentManager 实例，供工具内部调度子 Agent."""
    global _global_subagent_manager
    _global_subagent_manager = manager


# ---------------------------------------------------------------------------
#  Self-Review — Agent 审查自身源码
# ---------------------------------------------------------------------------

_SELF_REVIEW_SYSTEM = """\
你是 NaumiAgent 的自审查分析引擎。你正在审查 **自己的源代码**。

## 分析维度

### 1. 代码质量 (Code Quality)
- 函数复杂度：是否有超长函数（>50行）、深层嵌套（>4层）
- 命名一致性：是否遵循统一命名规范
- 重复代码：是否有重复逻辑可抽象
- 类型安全：是否有缺失的类型注解

### 2. 架构脆弱性 (Architecture Fragility)
- 模块耦合：是否存在循环依赖、不合理的跨层调用
- SPOF：是否有单点故障风险（单例、全局状态、无重试）
- 错误传播：异常是否被正确传播，有无裸 except
- 资源泄漏：是否有未关闭的连接、文件句柄

### 3. 工具系统健康度 (Tool System Health)
- 工具注册：所有工具是否正确注册
- 参数校验：工具参数是否完整校验
- 错误处理：工具执行失败时是否有友好提示

### 4. 记忆与安全 (Memory & Safety)
- 记忆质量：存储/召回逻辑是否有边界问题
- 权限控制：是否有越权风险
- 敏感信息：是否有硬编码密钥或凭证

### 5. 可进化性 (Evolvability)
- 扩展点：新增工具/Skill 是否容易
- 测试覆盖：关键路径是否有测试保护
- 配置化：硬编码值是否可配置

## 输出格式

对每个发现，给出：
- **严重程度**: CRITICAL / HIGH / MEDIUM / LOW
- **位置**: 文件名:行号
- **问题**: 一句话描述
- **建议**: 修复方向（不需要完整代码）

最后给出：
- **整体评分**: A/B/C/D/F
- **改进优先级**: 按影响力排序的 Top 5 改进建议
- **自进化建议**: 哪些部分适合由 Agent 自己修改（Phase F 候选）
"""


def _scan_self_review(files: list[Path], source_text: str) -> str:
    """self-review 模式静态扫描：审查 Agent 自身代码."""
    findings: list[str] = []
    lines = source_text.split("\n")
    total_lines = len(lines)

    # 1. Architecture overview
    findings.append(
        f"- 源文件: {len(files)} 个 | 总行数: {total_lines}"
    )

    # 2. Tool registration count
    tool_registrations = re.findall(r"register\((\w+)\)", source_text)
    findings.append(f"- 工具注册调用: {len(tool_registrations)} 处")

    # 3. Bare except (critical for agent reliability)
    bare_excepts = re.findall(r"except\s*:", source_text)
    if bare_excepts:
        findings.append(f"- 🔴 裸 except (吞掉所有异常): {len(bare_excepts)} 处")
    else:
        findings.append("- ✅ 无裸 except")

    # 4. Hardcoded secrets / API keys
    secrets = re.findall(
        r"(?:api_key|password|secret|token)\s*=\s*[\"'][^\"']{8,}",
        source_text,
        re.IGNORECASE,
    )
    if secrets:
        findings.append(f"- 🔴 疑似硬编码密钥: {len(secrets)} 处")
        for s in secrets[:5]:
            findings.append(f"  - `{s[:60]}`")
    else:
        findings.append("- ✅ 无硬编码密钥")

    # 5. Missing type annotations
    no_return_type = re.findall(
        r"def (\w+)\([^)]*\)\s*:",
        source_text,
    )
    typed_returns = re.findall(
        r"def \w+\([^)]*\)\s*->\s+\w+",
        source_text,
    )
    untyped = len(no_return_type) - len(typed_returns)
    if untyped > 0:
        findings.append(f"- 🟡 缺少返回类型注解的函数: {untyped} 个")
    else:
        findings.append("- ✅ 所有函数都有返回类型注解")

    # 6. Global mutable state
    global_mutable = re.findall(
        r"^(\w+)\s*=\s*\{[^}]*\}|\[\]",
        source_text,
        re.MULTILINE,
    )
    if global_mutable:
        findings.append(f"- 🟡 模块级可变状态: {len(global_mutable)} 处")

    # 7. Error handling coverage
    try_blocks = re.findall(r"\btry\s*:", source_text)
    if try_blocks:
        findings.append(f"- try/except 块: {len(try_blocks)} 个")

    # 8. Async consistency
    async_defs = re.findall(r"\basync def ", source_text)
    sync_defs = re.findall(r"\bdef ", source_text)
    async_ratio = len(async_defs) / max(len(sync_defs), 1)
    findings.append(
        f"- async/sync 函数比: {len(async_defs)}/{len(sync_defs)}"
        f" ({async_ratio:.0%} async)"
    )

    # 9. Test coverage indicator
    test_mentions = re.findall(r"test_\w+", source_text)
    findings.append(
        f"- 代码中测试函数引用: {len(test_mentions)} 处"
    )

    # 10. Logging usage
    log_calls = re.findall(r"logger\.\w+\(", source_text)
    print_calls = re.findall(r"\bprint\(", source_text)
    findings.append(
        f"- logger 调用: {len(log_calls)} | print 调用: {len(print_calls)}"
    )

    # 11. TODO/FIXME/HACK markers
    todos = re.findall(r"#\s*(?:TODO|FIXME|HACK|XXX)\b", source_text, re.IGNORECASE)
    if todos:
        findings.append(f"- 🟡 TODO/FIXME/HACK 标记: {len(todos)} 处")

    return "\n".join(findings)


def _find_agent_source_dir() -> str:
    """Locate the naumi_agent source directory."""
    # Try relative to this file first
    this_file = Path(__file__).resolve()
    pkg_dir = this_file.parent.parent  # tools/ -> naumi_agent/
    if (pkg_dir / "__init__.py").exists() and pkg_dir.name == "naumi_agent":
        return str(pkg_dir)
    # Fallback: search in site-packages or common locations
    import naumi_agent
    return str(Path(naumi_agent.__file__).resolve().parent)


class SelfReviewTool(Tool):
    """自我审查 — Agent 扫描自身源码，评估代码质量与架构脆弱性."""

    @property
    def name(self) -> str:
        return "self_review"

    @property
    def description(self) -> str:
        return (
            "审查 NaumiAgent 自身源代码。"
            "静态扫描代码质量、架构脆弱性、工具系统健康度、安全性，"
            "再由 LLM 综合推理出改进建议和自进化候选。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "focus": {
                    "type": "string",
                    "description": "审查重点 (quality/architecture/tools/safety/all)",
                    "default": "all",
                },
                "module": {
                    "type": "string",
                    "description": "只审查指定模块 (如 orchestrator, tools, memory)",
                    "default": "",
                },
            },
            "required": [],
        }

    async def execute(
        self, *, focus: str = "all", module: str = "", **kwargs: Any,
    ) -> str:
        router = _global_router
        if router is None:
            return _router_unavailable("self-review", "naumi_agent source")

        source_dir = _find_agent_source_dir()

        if module:
            target_dir = str(Path(source_dir) / module)
        else:
            target_dir = source_dir

        files = _resolve_target(target_dir)
        if not files:
            return f"无法定位源码目录: {target_dir}"

        source_text = _read_sources(files, max_chars=80000)
        scan_evidence = _scan_self_review(files, source_text)

        user_msg = (
            f"## 静态扫描证据\n{scan_evidence}\n\n"
            f"## 源代码\n{source_text[:50000]}\n"
        )
        if focus != "all":
            user_msg += f"\n## 审查重点\n请重点关注: {focus}\n"

        return await _run_analysis(router, _SELF_REVIEW_SYSTEM, user_msg)


def create_analysis_tools() -> list[Tool]:
    """创建所有分析模式工具."""
    return [
        ChaosAnalysisTool(),
        ScaleAnalysisTool(),
        StateAuditTool(),
        VibeModeTool(),
        EvalDrivenTool(),
        MemoryPageTool(),
        SelfHealTool(),
        DSPyTool(),
        GraphRAGTool(),
        MCTSTool(),
        MoERouteTool(),
        SpeculateTool(),
        JITTool(),
        PointerTool(),
        COOETool(),
        SleepPruningTool(),
        EntropyValveTool(),
        OODATool(),
        ProbeTool(),
        HookTool(),
        VisionTool(),
        SparTool(),
        WorldModelTool(),
        FusionTool(),
        ConsensusTool(),
        PIDTool(),
        ZKPTool(),
        GenesisTool(),
        MacroTool(),
        CosmosTool(),
        WatchdogTool(),
        SupervisorTool(),
        AutopsyTool(),
        SelfReviewTool(),
    ]
