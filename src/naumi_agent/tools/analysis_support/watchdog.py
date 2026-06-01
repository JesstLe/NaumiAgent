"""Deterministic disaster-recovery audit helpers."""

from __future__ import annotations

import re

from naumi_agent.tools import analysis_common

INPLACE_MOD_PATTERNS = [
    (
        r"(?:open|write)\([^)]*(?:__file__|sys\.argv\[0\]|self\.__class__)",
        "直接修改自身源文件 (原地手术)",
    ),
    (
        r"(?:shutil\.copy|os\.rename|os\.replace)\([^)]*\.\w+\.\w+",
        "直接替换运行中的文件 (热替换风险)",
    ),
    (
        r"(?:importlib\.reload|reload)\s*\(",
        "运行时重载模块 (可能导致状态不一致)",
    ),
    (
        r"(?:exec|eval)\s*\(\s*(?:open|read)",
        "读取并执行动态代码 (注入风险)",
    ),
    (
        r"(?:sys\.modules|globals)\s*\[\s*['\"][^'\"]+['\"]\s*\]\s*=",
        "运行时修改导入表 (全局污染)",
    ),
    (
        r"(?:setattr|__dict__)\s*\([^)]*class",
        "运行时修改类定义 (对象可能损坏)",
    ),
]

HEALTH_PATTERNS = [
    (r"(?:heartbeat|health.?check|ping|alive)\s*", "心跳/存活检测"),
    (r"(?:timeout|deadline|time.?limit)\s*[:=]", "超时/截止时间"),
    (r"(?:watchdog|monitor|supervisor|guard)\s*", "看门狗/监控进程"),
    (r"(?:is_alive|is_healthy|is_ready|is_running)\s*", "存活状态检查"),
    (r"(?:Thread|Process)\s*\([^)]*target.*alive", "线程/进程存活监控"),
]

ROLLBACK_PATTERNS = [
    (r"(?:backup|snapshot|checkpoint|savepoint)\s*", "备份/快照机制"),
    (r"(?:rollback|restore|revert|recover)\s*\(", "回滚/恢复操作"),
    (r"(?:version|revision|commit)\s*[:=]", "版本/修订管理"),
    (r"(?:git\s+checkout|git\s+revert|git\s+reset)", "Git 回滚操作"),
    (r"(?:copy|clone|mirror)\s*\([^)]*(?:before|pre)", "修改前备份"),
    (r"(?:try:.*\n.*except.*\n.*(?:restore|rollback|recover))", "异常触发回滚"),
]

ISOLATION_PATTERNS = [
    (r"(?:sandbox|container|docker|vm|jail)\s*", "沙盒/容器隔离"),
    (r"(?:namespace|cgroup|chroot|seccomp)\s*", "系统级隔离机制"),
    (r"(?:isolated|separate|staging|canary)\s*", "隔离环境/金丝雀部署"),
    (r"(?:blue.?green|a/?b|toggle|feature.?flag)\s*", "蓝绿部署/特性开关"),
    (r"(?:venv|virtualenv|conda)\s*", "Python 虚拟环境隔离"),
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


def scan_watchdog(target: str) -> str:
    """Scan disaster recovery readiness across watchdog, rollback, and isolation."""
    findings: list[str] = []
    source = analysis_common.read_sources(analysis_common.resolve_target(target))

    if not source.strip():
        return "⚠️ 未找到可分析的源代码。"

    lines = source.split("\n")

    findings.append("## 1. 原地修改风险 (In-Place Surgery Risks)")
    inplace_hits: list[tuple[str, int, str]] = []
    for pattern, desc in INPLACE_MOD_PATTERNS:
        for index, line in enumerate(lines, 1):
            if re.search(pattern, line):
                inplace_hits.append((desc, index, line.strip()))

    if inplace_hits:
        findings.append(
            f"- 🔴 发现 **{len(inplace_hits)}** 处危险的原地修改 — "
            f"AI 可能在运行时把自己改死：",
        )
        for desc, line_no, line_text in inplace_hits[:8]:
            short = line_text[:70] + ("..." if len(line_text) > 70 else "")
            findings.append(f"  - L{line_no}: {desc}")
            findings.append(f"    `{short}`")
        findings.append("- 💡 所有修改必须在沙盒副本上进行，通过验证后才能替换原文件")
    else:
        findings.append("- ✅ 未检测到原地修改风险")
    findings.append("")

    findings.append("## 2. 心跳与健康检查 (Heartbeat & Health Check)")
    health_hits = _collect_label_hits(lines, HEALTH_PATTERNS)
    if health_hits:
        total_health = sum(len(line_nos) for line_nos in health_hits.values())
        findings.append(
            f"- 检测到 **{total_health}** 处健康检查机制，"
            f"**{len(health_hits)}** 类：",
        )
        for label, line_nos in sorted(
            health_hits.items(),
            key=lambda item: -len(item[1]),
        ):
            findings.append(f"  - {label}: {len(line_nos)} 处")
    else:
        findings.append("- ❌ 无心跳/健康检查 — AI 崩溃后系统无法自动感知和恢复")
    findings.append("")

    findings.append("## 3. 回滚基础设施 (Rollback Infrastructure)")
    rollback_hits = _collect_label_hits(lines, ROLLBACK_PATTERNS)
    if rollback_hits:
        total_rollback = sum(len(line_nos) for line_nos in rollback_hits.values())
        findings.append(
            f"- 检测到 **{total_rollback}** 处回滚机制，"
            f"**{len(rollback_hits)}** 类：",
        )
        for label, line_nos in rollback_hits.items():
            findings.append(f"  - {label}: {len(line_nos)} 处")
    else:
        findings.append("- ❌ 无回滚机制 — 一旦崩溃只能人工恢复，无法自动回退")
    findings.append("")

    findings.append("## 4. 隔离级别 (Isolation Level)")
    iso_hits = _collect_label_hits(lines, ISOLATION_PATTERNS)
    if iso_hits:
        total_iso = sum(len(line_nos) for line_nos in iso_hits.values())
        findings.append(
            f"- 检测到 **{total_iso}** 处隔离机制，"
            f"**{len(iso_hits)}** 类：",
        )
        for label, line_nos in iso_hits.items():
            findings.append(f"  - {label}: {len(line_nos)} 处")
    else:
        findings.append("- ⚠️ 隔离级别低 — 建议引入沙盒/容器/蓝绿部署策略")
    findings.append("")

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
        findings.append("- ✅ 系统具备较强的灾难恢复能力，AI 自毁后可自动满血复活")
    elif phoenix_score >= 0.4:
        findings.append("- ⚠️ 部分具备恢复能力，需补强回滚和隔离")
    else:
        findings.append(
            "- ❌ 系统一旦被 AI 改坏就需要人工收尸，"
            "强烈建议引入看门狗 + A/B 分区 + 回滚通道",
        )

    return "\n".join(findings)


def build_watchdog_inventory_script(target: str) -> str:
    """Build a dependency-free watchdog readiness scanner."""
    return f'''\
"""Watchdog disaster inventory script generated by analysis_watchdog."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

TARGET = {target!r}
SOURCE_SUFFIXES = {{".py", ".js", ".ts", ".tsx", ".jsx", ".sh", ".yaml", ".yml"}}
INPLACE_PATTERNS = [
    (r"(?:open|write)\\([^)]*(?:__file__|sys\\.argv\\[0\\]|self\\.__class__)", "修改自身源文件"),
    (r"(?:importlib\\.reload|reload)\\s*\\(", "运行时模块重载"),
    (r"(?:exec|eval)\\s*\\(\\s*(?:open|read)", "读取并执行动态代码"),
    (r"(?:sys\\.modules|globals)\\s*\\[", "修改全局导入/命名空间"),
]
HEALTH_PATTERNS = [
    (r"(?:heartbeat|health.?check|ping|alive)", "心跳/健康检查"),
    (r"(?:timeout|deadline|time.?limit)\\s*[:=]", "超时/截止时间"),
    (r"(?:watchdog|monitor|supervisor|guard)", "外部监控"),
    (r"(?:is_alive|is_healthy|is_ready|is_running)", "存活状态检查"),
]
ROLLBACK_PATTERNS = [
    (r"(?:backup|snapshot|checkpoint|savepoint)", "备份/快照"),
    (r"(?:rollback|restore|revert|recover)\\s*\\(", "回滚/恢复"),
    (r"(?:version|revision|commit)\\s*[:=]", "版本/修订"),
]
ISOLATION_PATTERNS = [
    (r"(?:sandbox|container|docker|vm|jail)", "沙盒/容器"),
    (r"(?:isolated|separate|staging|canary)", "隔离/灰度环境"),
    (r"(?:blue.?green|feature.?flag|toggle)", "蓝绿/特性开关"),
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


def inspect_file(path: Path) -> dict[str, object]:
    try:
        source = path.read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:
        return {{"path": str(path), "found": False, "error": str(exc)}}
    inplace = find_hits(source, INPLACE_PATTERNS)
    health = find_hits(source, HEALTH_PATTERNS)
    rollback = find_hits(source, ROLLBACK_PATTERNS)
    isolation = find_hits(source, ISOLATION_PATTERNS)
    return {{
        "path": str(path),
        "found": True,
        "inplace": inplace[:80],
        "health": health[:80],
        "rollback": rollback[:80],
        "isolation": isolation[:80],
        "phoenix_contract": build_phoenix_contract(inplace, health, rollback, isolation),
    }}


def build_phoenix_contract(
    inplace: list[dict[str, object]],
    health: list[dict[str, object]],
    rollback: list[dict[str, object]],
    isolation: list[dict[str, object]],
) -> dict[str, object]:
    return {{
        "sandbox_required": bool(inplace),
        "external_watchdog_present": bool(health),
        "rollback_present": bool(rollback),
        "isolation_present": bool(isolation),
        "minimum_recovery_chain": [
            "pre_change_snapshot",
            "sandbox_validation",
            "heartbeat_timeout",
            "automatic_rollback",
            "failure_attribution",
            "circuit_breaker_after_repeated_failures",
        ],
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
            "inplace": sum(len(item.get("inplace", [])) for item in files),
            "health": sum(len(item.get("health", [])) for item in files),
            "rollback": sum(len(item.get("rollback", [])) for item in files),
            "isolation": sum(len(item.get("isolation", [])) for item in files),
        }},
    }}


def main() -> None:
    targets = [arg for arg in sys.argv[1:] if arg]
    print(json.dumps(summarize(targets), ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
'''


def build_watchdog_report(target: str, scan_evidence: str) -> str:
    """Build deterministic watchdog disaster-recovery output."""
    script = build_watchdog_inventory_script(target)
    return (
        "## Watchdog 确定性灾难隔离审计\n"
        "- 执行锚点: 原地修改/健康检查/回滚/隔离 inventory + phoenix contract。\n"
        "- 审计目标: AI 自修改失败后仍能被外部看门狗发现、回滚和隔离。\n\n"
        f"## 静态看门狗扫描\n{scan_evidence}\n\n"
        "## Watchdog Inventory Script\n"
        "```python\n"
        f"{script}"
        "```\n\n"
        "## 执行方式\n"
        "- 保存脚本后运行：`python watchdog_inventory.py <source_file_or_directory>`。\n"
        "- 输出 `inplace`、`health`、`rollback`、`isolation` 和 `phoenix_contract`。\n"
        "- 脚本只读源码，不重启服务、不修改文件。\n\n"
        "## Phoenix Contract\n"
        "- 自修改必须先创建 snapshot，再进入 sandbox validation。\n"
        "- 主进程必须对外发 heartbeat，由独立 watchdog 判断 timeout。\n"
        "- 发布必须有 rollback 通道、失败归因、连续失败熔断。\n"
        "- 高风险变更必须在隔离环境或蓝绿/灰度路径中验证。\n\n"
        "## 实施计划\n"
        "1. 把 `inplace` 修改点迁到沙盒副本。\n"
        "2. 为主进程补 heartbeat 文件或本地 health endpoint。\n"
        "3. 为每次自动修改补 snapshot、restore、failure log。\n"
        "4. 引入连续失败熔断，暂停自演化并要求人工确认。\n"
    )
