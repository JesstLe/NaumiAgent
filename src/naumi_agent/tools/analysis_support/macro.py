"""Deterministic multi-agent market audit helpers."""

from __future__ import annotations

import re

from naumi_agent.tools import analysis_common

CENTRALIZED_PATTERNS = [
    (
        r"(?:main|master|primary|controller|coordinator)\s*.\s*(?:decide|plan|route)",
        "中心化决策节点 (单点瓶颈)",
    ),
    (r"(?:if|switch|match)\s+\w+\s*(?:==|in)\s*\(", "中心化条件路由 (硬编码分发)"),
    (r"(?:router|dispatcher|scheduler)\s*=\s*\w+", "单一调度器 (无竞争机制)"),
    (r"(?:global|singleton)\s+\w+", "全局单例 (无并行替代)"),
]

DATA_MARKET_PATTERNS = [
    (r"(?:api|fetch|scrape|crawl|collect)\s*\(", "数据采集操作 (可作为数据商)"),
    (r"(?:parse|extract|transform|clean)\s*\(", "数据处理操作 (可定价出售)"),
    (r"(?:cache|store|database|persist)\s*\(", "数据存储 (可做数据交易所)"),
    (r"(?:query|search|filter|aggregate)\s*\(", "数据查询 (可按次收费)"),
]

INCENTIVE_PATTERNS = [
    (r"(?:reward|score|rating|credit|token)\s*[:=]", "奖励/积分机制"),
    (r"(?:penalty|fine|deduct|cost)\s*[:=]", "惩罚/成本机制"),
    (r"(?:bid|auction|price|offer)\s*[:=]", "竞价/定价机制"),
    (r"(?:budget|balance|wallet|account)\s*[:=]", "预算/账户系统"),
    (r"(?:stake|bond|deposit|collateral)\s*[:=]", "质押/保证金机制"),
]

COMPETITION_PATTERNS = [
    (r"(?:compete|rank|leaderboard|scoreboard)\s*", "竞争/排名机制"),
    (r"(?:evolve|mutate|breed|crossover)\s*\(", "进化/变异操作"),
    (r"(?:kill|retire|deprecate|sunset|remove)\s*\(", "淘汰/退出机制"),
    (r"(?:spawn|fork|replicate|clone)\s*\(", "繁殖/复制操作"),
    (r"(?:fitness|adapt|survive)\s*", "适应度/生存评估"),
]


def scan_macro(target: str) -> str:
    """Scan readiness for an agentic market economy architecture."""
    findings: list[str] = []
    source = analysis_common.read_sources(analysis_common.resolve_target(target))

    if not source.strip():
        return "⚠️ 未找到可分析的源代码。"

    lines = source.split("\n")

    findings.append("## 1. 中心化检测 (Centralization Audit)")
    central_hits: list[tuple[str, int, str]] = []
    for pattern, desc in CENTRALIZED_PATTERNS:
        for index, line in enumerate(lines, 1):
            if re.search(pattern, line, re.IGNORECASE):
                central_hits.append((desc, index, line.strip()))

    if central_hits:
        findings.append(f"- ⚠️ 发现 **{len(central_hits)}** 处中心化决策瓶颈：")
        for desc, line_no, line_text in central_hits[:8]:
            short = line_text[:70] + ("..." if len(line_text) > 70 else "")
            findings.append(f"  - L{line_no}: {desc}")
            findings.append(f"    `{short}`")
        findings.append("- 💡 这些点可拆分为多个自治 Agent，通过竞争提高系统整体智能")
    else:
        findings.append("- ✅ 决策架构较为去中心化")
    findings.append("")

    findings.append("## 2. 数据市场潜力 (Data Marketplace)")
    market_hits: dict[str, list[int]] = {}
    for pattern, label in DATA_MARKET_PATTERNS:
        for index, line in enumerate(lines, 1):
            if re.search(pattern, line, re.IGNORECASE):
                market_hits.setdefault(label, []).append(index)

    total_market = sum(len(line_nos) for line_nos in market_hits.values())
    if market_hits:
        findings.append(
            f"- 检测到 **{total_market}** 处可市场化的数据操作，"
            f"**{len(market_hits)}** 类："
        )
        for label, line_nos in sorted(
            market_hits.items(),
            key=lambda item: -len(item[1]),
        ):
            findings.append(f"  - {label}: {len(line_nos)} 处")
        findings.append("- 💡 这些数据流可封装为'数据商 Agent'，标价出售给'分析师 Agent'")
    else:
        findings.append("- 数据操作较少，市场潜力有限")
    findings.append("")

    findings.append("## 3. 激励机制 (Incentive Architecture)")
    incentive_hits: dict[str, list[int]] = {}
    for pattern, label in INCENTIVE_PATTERNS:
        for index, line in enumerate(lines, 1):
            if re.search(pattern, line, re.IGNORECASE):
                incentive_hits.setdefault(label, []).append(index)

    if incentive_hits:
        total_incentive = sum(len(line_nos) for line_nos in incentive_hits.values())
        findings.append(
            f"- 检测到 **{total_incentive}** 处激励/定价机制，"
            f"**{len(incentive_hits)}** 类："
        )
        for label, line_nos in incentive_hits.items():
            findings.append(f"  - {label}: {len(line_nos)} 处")
    else:
        findings.append("- ❌ 无激励/定价机制 — 无法驱动 Agent 间的市场竞争")
    findings.append("")

    findings.append("## 4. 竞争与淘汰 (Competition & Survival)")
    comp_hits: dict[str, list[int]] = {}
    for pattern, label in COMPETITION_PATTERNS:
        for index, line in enumerate(lines, 1):
            if re.search(pattern, line, re.IGNORECASE):
                comp_hits.setdefault(label, []).append(index)

    if comp_hits:
        total_comp = sum(len(line_nos) for line_nos in comp_hits.values())
        findings.append(
            f"- 检测到 **{total_comp}** 处竞争/淘汰机制，"
            f"**{len(comp_hits)}** 类："
        )
        for label, line_nos in comp_hits.items():
            findings.append(f"  - {label}: {len(line_nos)} 处")
    else:
        findings.append("- ❌ 无竞争/淘汰机制 — Agent 无法通过自然选择进化")
    findings.append("")

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
        findings.append("- ✅ 具备构建多 Agent 自由市场生态的基础设施")
    elif macro_score >= 0.4:
        findings.append("- ⚠️ 部分具备市场条件，需补强激励和淘汰机制")
    else:
        findings.append("- ❌ 系统高度中心化，需大幅改造才能支持市场博弈")

    return "\n".join(findings)


def build_macro_inventory_script(task: str) -> str:
    """Build a dependency-free market-readiness scanner."""
    return f'''\
"""Agentic market inventory script generated by analysis_macro."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

TASK = {task!r}
SOURCE_SUFFIXES = {{".py", ".js", ".ts", ".tsx", ".jsx"}}
CENTRALIZED_PATTERNS = [
    (r"(?:main|master|primary|controller|coordinator).*?(?:decide|plan|route)", "中心化决策"),
    (r"(?:router|dispatcher|scheduler)\\s*=\\s*\\w+", "单一调度器"),
    (r"(?:global|singleton)\\s+\\w+", "全局单例"),
]
DATA_MARKET_PATTERNS = [
    (r"(?:api|fetch|scrape|crawl|collect)\\s*\\(", "数据采集"),
    (r"(?:parse|extract|transform|clean)\\s*\\(", "数据处理"),
    (r"(?:cache|store|database|persist)\\s*\\(", "数据存储"),
    (r"(?:query|search|filter|aggregate)\\s*\\(", "数据查询"),
]
INCENTIVE_PATTERNS = [
    (r"(?:reward|score|rating|credit|token)\\s*[:=]", "奖励/积分"),
    (r"(?:penalty|fine|deduct|cost)\\s*[:=]", "惩罚/成本"),
    (r"(?:bid|auction|price|offer)\\s*[:=]", "竞价/定价"),
    (r"(?:budget|balance|wallet|account)\\s*[:=]", "预算/账户"),
]
COMPETITION_PATTERNS = [
    (r"(?:compete|rank|leaderboard|scoreboard)", "竞争/排名"),
    (r"(?:kill|retire|deprecate|sunset|remove)\\s*\\(", "淘汰/退出"),
    (r"(?:spawn|fork|replicate|clone)\\s*\\(", "繁殖/复制"),
    (r"(?:fitness|adapt|survive)", "适应度/生存评估"),
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
    centralized = find_hits(source, CENTRALIZED_PATTERNS)
    data_market = find_hits(source, DATA_MARKET_PATTERNS)
    incentive = find_hits(source, INCENTIVE_PATTERNS)
    competition = find_hits(source, COMPETITION_PATTERNS)
    return {{
        "path": str(path),
        "found": True,
        "centralized": centralized[:80],
        "data_market": data_market[:80],
        "incentive": incentive[:80],
        "competition": competition[:80],
        "market_contract": build_market_contract(centralized, data_market, incentive, competition),
    }}


def build_market_contract(
    centralized: list[dict[str, object]],
    data_market: list[dict[str, object]],
    incentive: list[dict[str, object]],
    competition: list[dict[str, object]],
) -> dict[str, object]:
    return {{
        "decentralize_required": bool(centralized),
        "data_vendor_candidates": len(data_market),
        "token_economy_present": bool(incentive),
        "selection_pressure_present": bool(competition),
        "minimum_market_roles": [
            "data_vendor",
            "analyst",
            "arbitrator",
            "budget_controller",
        ],
        "minimum_controls": [
            "token_budget",
            "quality_score",
            "bankruptcy_threshold",
            "anti_monopoly_cap",
        ],
    }}


def summarize(targets: list[str]) -> dict[str, object]:
    files = []
    for target in targets:
        for source in collect_sources(target):
            files.append(inspect_file(source))
    return {{
        "status": "ok" if files else "no_source_files",
        "task": TASK,
        "files": files,
        "summary": {{
            "files": len(files),
            "centralized": sum(len(item.get("centralized", [])) for item in files),
            "data_market": sum(len(item.get("data_market", [])) for item in files),
            "incentive": sum(len(item.get("incentive", [])) for item in files),
            "competition": sum(len(item.get("competition", [])) for item in files),
        }},
    }}


def main() -> None:
    targets = [arg for arg in sys.argv[1:] if arg]
    print(json.dumps(summarize(targets), ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
'''


def build_macro_report(task: str, scan_evidence: str) -> str:
    """Build deterministic agentic market audit output."""
    script = build_macro_inventory_script(task)
    return (
        "## Macro 确定性多智能体市场审计\n"
        "- 执行锚点: 中心化/数据市场/激励/竞争 inventory + market contract。\n"
        "- 审计目标: 把单点调度拆成可交易、可评分、可淘汰的 Agent 市场。\n\n"
        f"## 静态市场化扫描\n{scan_evidence}\n\n"
        "## Market Inventory Script\n"
        "```python\n"
        f"{script}"
        "```\n\n"
        "## 执行方式\n"
        "- 保存脚本后运行：`python macro_inventory.py <source_file_or_directory>`。\n"
        "- 输出 `centralized`、`data_market`、`incentive`、`competition` 和 `market_contract`。\n"
        "- 脚本只读源码，不启动 Agent、不执行目标代码。\n\n"
        "## Market Contract\n"
        "- 每个数据商必须有价格、质量评分、可复用数据产品和退款/惩罚规则。\n"
        "- 每个分析师必须有预算、收益记录、失败成本和淘汰阈值。\n"
        "- 仲裁器必须基于真实结果或确定性验证器结算，不能由单个模型自评。\n\n"
        "## 改造计划\n"
        "1. 将 `centralized` 决策点拆成多个可竞价 Agent。\n"
        "2. 把 `data_market` 数据流封装为可定价产品。\n"
        "3. 补 token/score/budget 账本和失败惩罚。\n"
        "4. 引入排行榜、破产淘汰和成功策略复制机制。\n"
    )
