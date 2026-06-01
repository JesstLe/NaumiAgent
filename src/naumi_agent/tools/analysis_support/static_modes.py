"""Deterministic static scanners for core analysis modes."""

from __future__ import annotations

import re
from pathlib import Path


def read_sources_for_ast(files: list[Path], max_chars: int = 80000) -> str:
    """Read source files with comment headers so AST parsing can still work."""
    parts: list[str] = []
    total = 0
    for file in files:
        try:
            content = file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        header = f"\n# file: {file}\n"
        if total + len(header) + len(content) > max_chars:
            remaining = max_chars - total
            if remaining > len(header) + 200:
                parts.append(header + content[: remaining - len(header)] + "\n# truncated")
            break
        parts.append(header + content)
        total += len(header) + len(content)
    return "".join(parts)


def format_static_scan_result(title: str, scan_evidence: str, files: list[Path]) -> str:
    """Format shared static scan output."""
    return "\n".join(
        [
            f"## {title}",
            f"- 扫描文件数：{len(files)}",
            "",
            scan_evidence,
        ]
    )


def scan_chaos(files: list[Path], source_text: str) -> str:
    """Find resilience and blast-radius risks for chaos analysis."""
    findings: list[str] = []
    lines = source_text.split("\n")

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

    bare_excepts = re.findall(r"^(.*?)except\s*:", source_text, re.MULTILINE)
    if bare_excepts:
        findings.append(f"- 裸 except (捕获所有异常): {len(bare_excepts)} 处")
        for ctx in bare_excepts[:5]:
            findings.append(f'  - `{ctx.strip()[-60:]}`')

    hardcoded = re.findall(
        r'(?:(?:host|HOST|url|URL|endpoint|ENDPOINT)\s*[=:]\s*["\']'
        r'(?:https?://|localhost|127\.0\.0\.|0\.0\.0\.0)[^"\']*)',
        source_text,
    )
    if hardcoded:
        findings.append(f"- 硬编码连接地址: {len(hardcoded)} 处")
        for item in hardcoded[:5]:
            findings.append(f'  - `{item.strip()}`')

    no_timeout = re.findall(
        r"(?:requests\.(?:get|post|put|delete|patch)|httpx\.\w+\.request|urllib\.request\.urlopen)"
        r"\([^)]*\)",
        source_text,
    )
    no_timeout_missing = [call for call in no_timeout if "timeout" not in call.lower()]
    if no_timeout_missing:
        findings.append(f"- 无 timeout 的外部 HTTP 调用: {len(no_timeout_missing)} 处")

    global_mutations = re.findall(
        r"^(?:\w+\s*[=:]\s*(?:\{|\[|None|\"\"|dict\(\)|list\(\)|set\(\)))",
        source_text,
        re.MULTILINE,
    )
    if global_mutations:
        findings.append(f"- 模块级可变状态 (潜在 SPOF): {len(global_mutations)} 处")
        for item in global_mutations[:5]:
            findings.append(f'  - `{item.strip()}`')

    external_calls = len(
        re.findall(
            r"(?:requests\.|httpx\.|aiohttp\.|fetch\(|urllib|redis|mongo|sqlalchemy)",
            source_text,
        )
    )
    retry_count = len(re.findall(r"\bretry\b", source_text, re.IGNORECASE))
    if external_calls > 0:
        findings.append(
            f"- 外部依赖调用: {external_calls} 次, retry 机制: {retry_count} 处 "
            f"({'⚠️ 无重试保护' if retry_count == 0 else '✓ 有重试'})"
        )

    findings.append(f"- 扫描文件: {len(files)} 个, 总代码行数: {len(lines)}")

    return "\n".join(findings) if findings else "- 静态扫描未发现明显问题"


def scan_scale(files: list[Path], source_text: str, qps: int) -> str:
    """Find concurrency bottlenecks for scale analysis."""
    findings: list[str] = []
    lines = source_text.split("\n")

    pool_configs = re.findall(
        r"(?:pool_size|max_connections|POOL_SIZE|MAX_CONN|pool_overflow)[^\n]*",
        source_text,
    )
    if pool_configs:
        findings.append("- 数据库连接池配置:")
        for config in pool_configs[:5]:
            findings.append(f"  - `{config.strip()}`")
    else:
        findings.append("- ⚠️ 未发现连接池配置，可能使用默认值或无池化")

    sync_io = re.findall(
        r"(?:requests\.(?:get|post|put|delete)|urllib\.request|open\([^)]*\)(?!.*with))",
        source_text,
    )
    if sync_io:
        findings.append(f"- 同步阻塞 I/O 调用: {len(sync_io)} 处 (在高并发下会阻塞事件循环)")

    locks = re.findall(r"(?:threading\.Lock|multiprocessing\.Lock|asyncio\.Lock)", source_text)
    if locks:
        findings.append(f"- 锁使用: {len(locks)} 处 (可能成为争用热点)")

    cache_pattern = r"(?:lru_cache|functools\.cache|@cache|redis|memcache|cachetools)"
    cache_hits = re.findall(cache_pattern, source_text)
    if cache_hits:
        findings.append(f"- 缓存机制: {len(cache_hits)} 处引用")
    else:
        findings.append("- ⚠️ 未发现缓存机制，每个请求都会穿透到数据层")

    n_plus_1 = re.findall(
        r"for\s+\w+\s+in\s+.*:\s*\n\s+.*(?:\.query|\.filter|\.get|\.find|SELECT)",
        source_text,
    )
    if n_plus_1:
        findings.append(f"- ⚠️ 疑似 N+1 查询: {len(n_plus_1)} 处")

    rate_limits = re.findall(r"(?:rate.?limit|throttl|circuit.?breaker|Semaphore)", source_text)
    if rate_limits:
        findings.append(f"- 限流/熔断: {len(rate_limits)} 处")
    else:
        findings.append("- ⚠️ 无限流/熔断保护，突发流量将直接冲击后端")

    findings.append(
        f"- 目标 QPS: {qps:,} | 扫描代码: {len(files)} 文件, "
        f"{len(lines)} 行 | 缓存: {'有' if cache_hits else '无'} | "
        f"限流: {'有' if rate_limits else '无'}"
    )

    return "\n".join(findings)


def scan_state(files: list[Path], source_text: str) -> str:
    """Find stateful patterns that break multi-instance deployment."""
    findings: list[str] = []

    global_dicts = re.findall(
        r"^(?:_?\w+)\s*[=:]\s*(?:\{[^}]*\}|\[\]|\{\}|dict\(\)|list\(\))",
        source_text,
        re.MULTILINE,
    )
    if global_dicts:
        findings.append(f"- 🔴 模块级可变容器 (在多实例间不同步): {len(global_dicts)} 处")
        for item in global_dicts[:8]:
            findings.append(f"  - `{item.strip()}`")

    thread_locals = re.findall(r"threading\.local\(\)", source_text)
    if thread_locals:
        findings.append(
            f"- 🔴 threading.local: {len(thread_locals)} 处 (进程间不共享，多实例部署会丢失)"
        )

    local_locks = re.findall(r"threading\.(?:Lock|RLock|Semaphore|Event)\(\)", source_text)
    if local_locks:
        findings.append(f"- 🟡 本地锁 (非分布式): {len(local_locks)} 处 (只在单进程内生效)")

    file_writes = re.findall(
        r"(?:open\([^)]*[\"']w|\.write\(|os\.rename|shutil\.move|\.save\()",
        source_text,
    )
    if file_writes:
        findings.append(f"- 🟡 本地文件写入: {len(file_writes)} 处 (多实例部署时文件不同步)")

    session_patterns = re.findall(
        r"(?:session[s]?\s*[=:]\s*\{|SESSION[s]?\s*=\s*\{|_sessions\s*=)",
        source_text,
    )
    if session_patterns:
        findings.append(
            f"- 🔴 内存 Session 存储: {len(session_patterns)} 处 "
            "(用户请求打到不同实例会丢失登录态)"
        )

    singletons = re.findall(
        r"(?:__new__|_instance\s*=\s*None|_shared_state|__metaclass__.*Singleton)",
        source_text,
    )
    if singletons:
        findings.append(f"- 🟡 Singleton 模式: {len(singletons)} 处 (假设单进程，多实例会创建多个)")

    async_globals = re.findall(
        r"(?:_queue\s*=\s*asyncio\.Queue|_event\s*=\s*asyncio\.Event|_cache\s*=\s*\{)",
        source_text,
    )
    if async_globals:
        findings.append(f"- 🟡 asyncio 全局队列/事件/缓存: {len(async_globals)} 处")

    redis_usage = len(re.findall(r"(?:redis|aioredis)", source_text))
    mq_usage = len(re.findall(r"(?:kafka|rabbitmq|celery|pika|confluent)", source_text))
    distributed = redis_usage + mq_usage
    findings.append(f"- 分布式组件: Redis {redis_usage} 处引用, 消息队列 {mq_usage} 处引用")

    score = max(
        0,
        100
        - len(global_dicts) * 10
        - len(thread_locals) * 15
        - len(session_patterns) * 20
        - len(local_locks) * 5
        - len(file_writes) * 3
        + distributed * 5,
    )
    score = min(100, score)
    findings.append(f"- 云原生就绪评分: {score}/100")

    return "\n".join(findings) if findings else "- 静态扫描未发现状态违规"
