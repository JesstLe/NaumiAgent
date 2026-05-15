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

        user_msg = f"## 任务描述\n{task}\n"
        user_msg += f"\n## 专家路由扫描\n{scan_evidence}\n"
        if source_text:
            user_msg += f"\n## 相关源代码\n{source_text[:50000]}\n"

        return await _run_analysis(router, _ROUTE_SYSTEM, user_msg)


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
    sentences = re.split(r"[。！？.!?\n]", source_text + conversation)
    sentences = [s.strip() for s in sentences if len(s.strip()) > 5]
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
    entropy_score = max(0, (repeated / total) * 40)
    temp = (
        "CRITICAL" if entropy_score > 60
        else "HIGH" if entropy_score > 35
        else "MEDIUM" if entropy_score > 15
        else "LOW"
    )
    findings.append(f"- 上下文温度: {entropy_score:.0f} ({temp})")
    return "\n".join(findings)


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
        router = _global_router
        if router is None:
            return _router_unavailable("entropy", context[:200])
        scan_evidence = _scan_entropy("", context)
        user_msg = (
            f"## 熵值扫描\n{scan_evidence}\n\n"
            f"## 当前上下文\n{context[:60000]}\n"
        )
        if goal:
            user_msg += f"\n## 原始目标\n{goal}\n"
        return await _run_analysis(router, _ENTROPY_SYSTEM, user_msg)


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
    ]
