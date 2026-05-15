"""分析模式工具 — chaos/scale/state/vibe，可作为工具被 Agent 自主调用.

每个工具执行两阶段分析:
  1. 静态扫描阶段 — 读文件、grep 模式、统计指标，收集实打实的代码证据
  2. LLM 综合阶段 — 把扫描证据 + 专有 prompt 交给 LLM 做深度推理与建议
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

from naumi_agent.tools.base import Tool

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
#  静态扫描引擎 — 所有工具共享
# ---------------------------------------------------------------------------

_SOURCE_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".java", ".rb", ".rs", ".php",
    ".c", ".cpp", ".h", ".hpp", ".cs", ".swift", ".kt", ".scala",
}


def _resolve_target(target: str) -> list[Path]:
    """将 target 字符串解析为文件列表（支持文件路径、目录路径、glob）."""
    p = Path(os.path.expanduser(target))
    if p.is_file():
        return [p]
    if p.is_dir():
        files = []
        for ext in _SOURCE_EXTENSIONS:
            files.extend(p.rglob(f"*{ext}"))
        return sorted(files)[:200]
    return []


def _read_sources(files: list[Path], max_chars: int = 80000) -> str:
    """读取源文件内容，拼成一段文本（带文件名标注）."""
    parts: list[str] = []
    total = 0
    for f in files:
        try:
            content = f.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        header = f"\n### {f}\n"
        if total + len(header) + len(content) > max_chars:
            remaining = max_chars - total
            if remaining > len(header) + 200:
                parts.append(header + content[: remaining - len(header)] + "\n... (truncated)")
            break
        parts.append(header + content)
        total += len(header) + len(content)
    return "".join(parts)


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
- Use the simplest libraries available
- Hardcode configuration values — refinement comes later
- Skip writing tests initially
- If there is a 3-line solution and a 30-line "proper" solution, use the 3-line one
- Output COMPLETE, RUNNABLE code — no TODOs, no placeholders, no stubs

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
    """极速构建模式 — 直接由 LLM 生成可运行代码."""

    @property
    def name(self) -> str:
        return "analysis_vibe"

    @property
    def description(self) -> str:
        return (
            "极速构建模式：忽略架构和边缘情况，以最快速度生成能跑通的核心 Demo 代码。"
            "适用于快速原型验证和概念验证。"
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
            },
            "required": ["description"],
        }

    async def execute(
        self, *, description: str, tech_stack: str = "", **kwargs: Any
    ) -> str:
        router = _global_router
        if router is None:
            return _router_unavailable("vibe", description)

        user_msg = f"## Build This\n{description}\n"
        if tech_stack:
            user_msg += f"\n## Tech Stack\n{tech_stack}\n"

        return await _run_analysis(router, _VIBE_SYSTEM, user_msg)


# ---------------------------------------------------------------------------
#  内部基础设施
# ---------------------------------------------------------------------------

_global_router: Any = None


def set_analysis_router(router: Any) -> None:
    """注入 ModelRouter 实例，供工具内部调用 LLM."""
    global _global_router
    _global_router = router


async def _run_analysis(router: Any, system_prompt: str, user_msg: str) -> str:
    """调用 LLM 综合分析并返回结果."""
    from naumi_agent.model.router import ModelTier

    try:
        response = await router.call(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_msg},
            ],
            tier=ModelTier.CAPABLE,
            max_tokens=8192,
            temperature=0.4,
        )
        return response.content
    except Exception as e:
        return f"分析失败: {type(e).__name__}: {e}"


def _router_unavailable(mode: str, target: str) -> str:
    """Router 未注入时的错误提示."""
    return (
        f"⚠️ 分析工具尚未初始化（Router 未注入）。\n"
        f"模式: {mode}\n"
        f"目标: {target[:200]}\n\n"
        f"请在 Agent 启动后使用。"
    )


def create_analysis_tools() -> list[Tool]:
    """创建所有分析模式工具."""
    return [
        ChaosAnalysisTool(),
        ScaleAnalysisTool(),
        StateAuditTool(),
        VibeModeTool(),
    ]
