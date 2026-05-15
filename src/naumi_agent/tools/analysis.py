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
    r'(?:example|sample|few.?shot|demonstration)\s*[:=]',
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

### 3. Few-shot Sample Design
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

    # 5. Compute centrality (simple degree-based)
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
    ]
