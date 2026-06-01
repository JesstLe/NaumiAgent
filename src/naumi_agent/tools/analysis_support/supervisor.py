"""Deterministic supervisor-tree audit helpers."""

from __future__ import annotations

import re

from naumi_agent.tools import analysis_common

MONOLITH_PATTERNS = [
    (
        r"(?:main|run|execute|process)\s*\([^)]*\)\s*:\s*\n"
        r"\s*(?:await|result|call)",
        "单线程顺序执行 (一崩全崩)",
    ),
    (
        r"while\s+True\s*:\s*\n\s*(?:await\s+\w+\.\w+){3,}",
        "无限循环串行调用 (无断路器)",
    ),
    (
        r"try:\s*\n(?:\s+.*\n){10,}\s*except",
        "巨型 try-except (试图穷举所有错误 — 反模式)",
    ),
    (
        r"(?:Agent|Worker|Runner)\s*\(\s*[^)]*\)\s*\.\s*run\s*\(\s*\)",
        "单一 Agent 直接运行 (无守护包装)",
    ),
]

WORKER_PATTERNS = [
    (
        r"(?:llm|model|gpt|claude|ai|neural)\s*.\s*(?:call|generate|run)",
        "LLM 调用 (高智能但不可靠)",
    ),
    (r"(?:crawl|scrape|parse|extract|analyze)\s*\(", "外部数据抓取/解析 (高失败率)"),
    (r"(?:compile|build|transpile|generate)\s*\(", "代码生成/编译 (可能产出非法结果)"),
    (r"(?:creative|brainstorm|ideate|explore)\s*", "创意性/发散性操作 (天生不稳定)"),
]

SUPERVISOR_PATTERNS = [
    (r"(?:supervisor|guardian|watcher|monitor|overseer)\s*", "守护/监督者角色"),
    (r"(?:restart_policy|restart_strategy|max_retries)\s*[:=]", "重启策略配置"),
    (r"(?:child_spec|worker_spec|process_spec)\s*[:=]", "子进程规格定义"),
    (r"(?:spawn|fork|Process|Thread)\s*\([^)]*target", "隔离式进程/线程启动"),
    (r"(?:supervise|supervisor_tree|sup_tree)\s*", "Erlang 式守护者树"),
    (r"(?:on_failure|on_error|on_crash|error_handler)\s*[:=]", "崩溃回调处理"),
]

ISOLATION_ERROR_PATTERNS = [
    (
        r"(?:try:.*\n.*except\s+\w+.*\n\s*(?:log|report|notify))",
        "异常隔离 + 日志记录",
    ),
    (r"(?:catch|except)\s*.*:\s*\n\s*(?:restart|retry|spawn)", "异常触发重启"),
    (r"(?:finally|cleanup|teardown|dispose)\s*:", "清理/资源释放"),
    (r"(?:circuit.?breaker|bulkhead|timeout)\s*", "熔断/舱壁/超时隔离"),
    (r"(?:isolate|quarantine|fence|contain)\s*", "故障隔离机制"),
]


def _collect_label_hits(
    lines: list[str],
    patterns: list[tuple[str, str]],
) -> dict[str, list[int]]:
    hits: dict[str, list[int]] = {}
    for pattern, label in patterns:
        for index, line in enumerate(lines, 1):
            if re.search(pattern, line, re.IGNORECASE):
                hits.setdefault(label, []).append(index)
    return hits


def scan_supervisor(target: str) -> str:
    """Scan system readiness for Erlang-style supervisor trees."""
    findings: list[str] = []
    source = analysis_common.read_sources(analysis_common.resolve_target(target))

    if not source.strip():
        return "⚠️ 未找到可分析的源代码。"

    lines = source.split("\n")

    findings.append("## 1. 单体风险检测 (Monolith Risk)")
    mono_hits: list[tuple[str, int, str]] = []
    for pattern, desc in MONOLITH_PATTERNS:
        for index, line in enumerate(lines, 1):
            if re.search(pattern, line):
                mono_hits.append((desc, index, line.strip()))

    if mono_hits:
        findings.append(
            f"- 🔴 发现 **{len(mono_hits)}** 处单体架构风险 — "
            f"一个模块崩溃会拖垮整个系统：",
        )
        for desc, line_no, line_text in mono_hits[:6]:
            short = line_text[:70] + ("..." if len(line_text) > 70 else "")
            findings.append(f"  - L{line_no}: {desc}")
            findings.append(f"    `{short}`")
    else:
        findings.append("- ✅ 未检测到明显的单体架构风险")
    findings.append("")

    findings.append("## 2. 进化节点候选 (Worker Candidates — 需要守护)")
    worker_hits = _collect_label_hits(lines, WORKER_PATTERNS)
    total_workers = sum(len(line_nos) for line_nos in worker_hits.values())
    if worker_hits:
        findings.append(
            f"- 检测到 **{total_workers}** 个高风险 Worker 候选，"
            f"**{len(worker_hits)}** 类：",
        )
        for label, line_nos in sorted(
            worker_hits.items(),
            key=lambda item: -len(item[1]),
        ):
            findings.append(f"  - {label}: {len(line_nos)} 处")
        findings.append("- 💡 这些模块应该被包裹在守护者(Supervisor)中运行")
    else:
        findings.append("- 高风险 Worker 较少，守护需求不高")
    findings.append("")

    findings.append("## 3. 守护基础设施 (Supervisor Infrastructure)")
    sup_hits = _collect_label_hits(lines, SUPERVISOR_PATTERNS)
    if sup_hits:
        total_sup = sum(len(line_nos) for line_nos in sup_hits.values())
        findings.append(
            f"- 检测到 **{total_sup}** 处守护者机制，"
            f"**{len(sup_hits)}** 类：",
        )
        for label, line_nos in sorted(
            sup_hits.items(),
            key=lambda item: -len(item[1]),
        ):
            findings.append(f"  - {label}: {len(line_nos)} 处")
    else:
        findings.append("- ❌ 无守护者基础设施 — 系统中无任何监督机制")
    findings.append("")

    findings.append("## 4. 错误隔离质量 (Error Isolation)")
    iso_hits = _collect_label_hits(lines, ISOLATION_ERROR_PATTERNS)
    if iso_hits:
        total_iso = sum(len(line_nos) for line_nos in iso_hits.values())
        findings.append(
            f"- 检测到 **{total_iso}** 处错误隔离机制，"
            f"**{len(iso_hits)}** 类：",
        )
        for label, line_nos in iso_hits.items():
            findings.append(f"  - {label}: {len(line_nos)} 处")
    else:
        findings.append("- ❌ 无错误隔离 — 一个模块的异常会传播到整个系统")
    findings.append("")

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
        findings.append("- ✅ 系统具备成熟的守护者树架构，可实施 Let-it-crash 哲学")
    elif readiness >= 0.4:
        findings.append("- ⚠️ 部分具备守护条件，需为高风险 Worker 添加 Supervisor 包裹")
    else:
        findings.append("- ❌ 系统缺乏守护架构，强烈建议引入 Erlang 式 Supervisor Tree")

    return "\n".join(findings)


def build_supervisor_inventory_script(target: str) -> str:
    """Build a dependency-free supervisor-tree readiness scanner."""
    return f'''\
"""Supervisor tree inventory script generated by analysis_supervisor."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

TARGET = {target!r}
SOURCE_SUFFIXES = {{".py", ".js", ".ts", ".tsx", ".jsx", ".yaml", ".yml", ".toml"}}
MONOLITH_PATTERNS = [
    (r"(?:while\\s+True|for\\s+.*\\s+in\\s+range)\\s*:", "长生命周期循环"),
    (r"(?:global\\s+\\w+|singleton|shared_state)", "共享全局状态"),
    (r"(?:subprocess|threading|multiprocessing).*?(?!timeout)", "无超时并发/子进程"),
    (r"(?:except\\s+Exception\\s*:\\s*(?:pass|return))", "吞掉异常"),
]
WORKER_PATTERNS = [
    (r"(?:worker|job|task|agent|runner|daemon)", "Worker/任务节点"),
    (r"(?:model|llm|generate|infer|reason)", "模型推理节点"),
    (r"(?:fetch|crawl|scrape|download|upload)", "外部 I/O 节点"),
    (r"(?:compile|exec|eval|subprocess|shell)", "代码执行节点"),
]
SUPERVISOR_PATTERNS = [
    (r"(?:supervisor|guardian|watcher|monitor|overseer)", "守护/监督者"),
    (r"(?:restart_policy|restart_strategy|max_retries)\\s*[:=]", "重启策略"),
    (r"(?:child_spec|worker_spec|process_spec)\\s*[:=]", "子节点规格"),
    (r"(?:spawn|fork|Process|Thread)\\s*\\([^)]*target", "隔离启动"),
    (r"(?:on_failure|on_error|on_crash|error_handler)\\s*[:=]", "崩溃回调"),
]
ISOLATION_PATTERNS = [
    (r"(?:try|except|catch|finally|cleanup|teardown)", "异常隔离/清理"),
    (r"(?:circuit.?breaker|bulkhead|timeout|deadline)", "熔断/舱壁/超时"),
    (r"(?:sandbox|container|isolate|quarantine|fence)", "沙盒/隔离"),
    (r"(?:snapshot|rollback|restore|checkpoint)", "快照/回滚"),
]


def collect_sources(raw: str) -> list[Path]:
    path = Path(raw).expanduser().resolve()
    if not path.exists():
        return []
    if path.is_file() and path.suffix.lower() in SOURCE_SUFFIXES:
        return [path]
    if path.is_dir():
        return [
            child for child in list(path.rglob("*"))[:500]
            if child.is_file() and child.suffix.lower() in SOURCE_SUFFIXES
        ][:80]
    return []


def find_hits(source: str, patterns: list[tuple[str, str]]) -> list[dict[str, object]]:
    hits = []
    for line_no, line in enumerate(source.splitlines(), 1):
        for pattern, label in patterns:
            if re.search(pattern, line, re.IGNORECASE):
                hits.append({{"line": line_no, "label": label, "sample": line.strip()[:120]}})
    return hits


def build_restart_contract(
    monolith: list[dict[str, object]],
    worker: list[dict[str, object]],
    supervisor: list[dict[str, object]],
    isolation: list[dict[str, object]],
) -> dict[str, object]:
    return {{
        "supervisor_required": bool(worker or monolith),
        "monolith_risk": len(monolith),
        "worker_candidates": len(worker),
        "supervisor_present": bool(supervisor),
        "isolation_present": bool(isolation),
        "minimum_restart_chain": [
            "child_spec",
            "heartbeat",
            "timeout",
            "restart_policy",
            "max_restart_intensity",
            "escalation_to_parent",
        ],
    }}


def inspect_file(path: Path) -> dict[str, object]:
    try:
        source = path.read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:
        return {{"path": str(path), "found": False, "error": str(exc)}}
    monolith = find_hits(source, MONOLITH_PATTERNS)
    worker = find_hits(source, WORKER_PATTERNS)
    supervisor = find_hits(source, SUPERVISOR_PATTERNS)
    isolation = find_hits(source, ISOLATION_PATTERNS)
    return {{
        "path": str(path),
        "found": True,
        "monolith": monolith[:80],
        "worker": worker[:80],
        "supervisor": supervisor[:80],
        "isolation": isolation[:80],
        "restart_contract": build_restart_contract(
            monolith, worker, supervisor, isolation,
        ),
    }}


def summarize(targets: list[str]) -> dict[str, object]:
    files = []
    for target in targets:
        for source in collect_sources(target):
            files.append(inspect_file(source))
    return {{
        "status": "ok" if files else "no_source_files",
        "target": TARGET,
        "files": files,
        "summary": {{
            "files": len(files),
            "monolith": sum(len(item.get("monolith", [])) for item in files),
            "worker": sum(len(item.get("worker", [])) for item in files),
            "supervisor": sum(len(item.get("supervisor", [])) for item in files),
            "isolation": sum(len(item.get("isolation", [])) for item in files),
        }},
    }}


def main() -> None:
    targets = [arg for arg in sys.argv[1:] if arg]
    print(json.dumps(summarize(targets), ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
'''


def build_supervisor_report(target: str, scan_evidence: str) -> str:
    """Build deterministic supervisor-tree audit output."""
    script = build_supervisor_inventory_script(target)
    return (
        "## Supervisor 确定性守护者树审计\n"
        "- 执行锚点: 单体风险/Worker 候选/Supervisor/隔离 inventory + restart contract。\n"
        "- 审计目标: 让高风险 Worker 可以崩溃、被发现、被重启、被升级，而不是拖垮主进程。\n\n"
        f"## 静态守护者扫描\n{scan_evidence}\n\n"
        "## Supervisor Inventory Script\n"
        "```python\n"
        f"{script}"
        "```\n\n"
        "## 执行方式\n"
        "- 保存脚本后运行：`python supervisor_inventory.py <source_file_or_directory>`。\n"
        "- 输出 `monolith`、`worker`、`supervisor`、`isolation` 和 `restart_contract`。\n"
        "- 脚本只读源码，不启动进程、不重启服务。\n\n"
        "## Restart Contract\n"
        "- 每个 Worker 必须有 child_spec、heartbeat、timeout、restart_policy。\n"
        "- Supervisor 必须限制 max restart intensity，连续失败后上报父节点。\n"
        "- Worker 使用低权限沙盒；Supervisor 持有重启、回滚和隔离权限。\n"
        "- 崩溃路径必须写入 failure log，回滚优先于在线调试。\n\n"
        "## 改造计划\n"
        "1. 将 `worker` 命中点登记为 child_spec。\n"
        "2. 给每个高风险节点补 heartbeat、timeout、max_retries。\n"
        "3. 把共享全局状态迁到可恢复 snapshot 或外部状态存储。\n"
        "4. 建立 parent supervisor 的升级链路和人工介入熔断点。\n"
    )
