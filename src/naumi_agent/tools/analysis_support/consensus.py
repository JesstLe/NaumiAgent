"""Deterministic consensus audit helpers."""

from __future__ import annotations

import re

from naumi_agent.tools import analysis_common

HIGH_RISK_PATTERNS = [
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

SINGLE_POINT_PATTERNS = [
    (
        r"(?:result|decision|answer)\s*=\s*(?:await\s+)?(?:llm|ai|model|gpt)\.\w+",
        "单模型直接决策",
    ),
    (
        r"if\s+(?:await\s+)?(?:llm|ai|model)\.\w+\([^)]*\)\s*:",
        "单模型条件分支",
    ),
    (
        r"return\s+(?:await\s+)?(?:llm|ai|model)\.\w+\([^)]*\)",
        "直接返回单模型结果",
    ),
    (
        r"(?:llm|ai|model|chat)\.?\w*\s*\(\s*[^)]*\)\s*\.\s*(?:json|parse)",
        "单模型输出直接解析",
    ),
]

DIVERSITY_PATTERNS = [
    (r"(?:model|provider|backend)\s*=\s*[\"'][^\"']+[\"']", "模型选择参数"),
    (r"temperature\s*=\s*[0-9.]+", "温度参数"),
    (r"(?:retry|fallback|backup|secondary)\s*\(", "重试/备选机制"),
    (r"(?:vote|quorum|majority|consensus)\s*\(", "表决/共识机制"),
    (r"for\s+\w+\s+in\s+(?:models|providers|agents)", "多模型遍历"),
    (r"(?:parallel|concurrent|gather)\s*\([^)]*model", "并行多模型调用"),
]


def scan_consensus(target: str) -> str:
    """Scan high-risk decisions, single-model risks, and quorum readiness."""
    findings: list[str] = []
    source = analysis_common.read_sources(analysis_common.resolve_target(target))

    if not source.strip():
        return "⚠️ 未找到可分析的源代码。"

    lines = source.split("\n")

    findings.append("## 1. 高风险决策点 (High-Risk Decisions)")
    risk_points: dict[str, list[int]] = {}
    for pattern, label in HIGH_RISK_PATTERNS:
        for index, line in enumerate(lines, 1):
            if re.search(pattern, line, re.IGNORECASE):
                risk_points.setdefault(label, []).append(index)

    total_risks = sum(len(line_nos) for line_nos in risk_points.values())
    if risk_points:
        findings.append(
            f"- 检测到 **{total_risks}** 处高风险操作，"
            f"**{len(risk_points)}** 类："
        )
        for label, line_nos in sorted(
            risk_points.items(),
            key=lambda item: -len(item[1]),
        ):
            findings.append(f"  - {label}: {len(line_nos)} 处")
    else:
        findings.append("- 未检测到明显的高风险决策操作")
    findings.append("")

    findings.append("## 2. 单点决策风险 (Single-Point-of-Decision)")
    spod_hits: list[tuple[str, int, str]] = []
    for pattern, desc in SINGLE_POINT_PATTERNS:
        for index, line in enumerate(lines, 1):
            if re.search(pattern, line, re.IGNORECASE):
                spod_hits.append((desc, index, line.strip()))

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

    findings.append("## 3. 多样性与冗余度 (Diversity & Redundancy)")
    diversity_hits: dict[str, list[int]] = {}
    for pattern, label in DIVERSITY_PATTERNS:
        for index, line in enumerate(lines, 1):
            if re.search(pattern, line, re.IGNORECASE):
                diversity_hits.setdefault(label, []).append(index)

    if diversity_hits:
        findings.append(f"- 检测到 **{len(diversity_hits)}** 类多样性机制：")
        for label, line_nos in sorted(
            diversity_hits.items(),
            key=lambda item: -len(item[1]),
        ):
            findings.append(f"  - {label}: {len(line_nos)} 处")
    else:
        findings.append("- ❌ 未检测到任何多样性/冗余机制 — 系统完全依赖单一决策源")
    findings.append("")

    has_voting = any("表决" in label or "共识" in label for label in diversity_hits)
    has_parallel = any("并行" in label for label in diversity_hits)
    has_retry = any("重试" in label or "备选" in label for label in diversity_hits)

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
        findings.append("- 🔴 存在高风险操作但无共识机制 — 强烈建议引入多模型表决")
    elif consensus_score >= 0.7:
        findings.append("- ✅ 共识架构较为成熟")
    elif consensus_score >= 0.4:
        findings.append("- ⚠️ 部分具备共识能力，需补强多样性")
    else:
        findings.append("- ❌ 缺乏共识机制，高风险操作应引入拜占庭容错")

    return "\n".join(findings)


def build_consensus_inventory_script(target: str) -> str:
    """Build a dependency-free quorum-readiness scanner."""
    return f'''\
"""Consensus inventory script generated by analysis_consensus."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

TARGET = {target!r}
SOURCE_SUFFIXES = {{".py", ".js", ".ts", ".tsx", ".jsx"}}
HIGH_RISK_PATTERNS = [
    (r"(?:delete|drop|remove|truncate|destroy)\\s*\\(", "数据删除操作"),
    (r"(?:deploy|publish|release|promote)\\s*\\(", "生产发布"),
    (r"(?:refund|withdraw|transfer|payment)\\s*\\(", "资金流转"),
    (r"(?:grant|revoke|permission|role|auth)\\s*\\(", "权限变更"),
    (r"(?:send|dispatch|notify|email|sms)\\s*\\(", "对外消息发送"),
]
SINGLE_POINT_PATTERNS = [
    (
        r"(?:result|decision|answer)\\s*=\\s*(?:await\\s+)?(?:llm|ai|model|gpt)\\.\\w+",
        "单模型直接决策",
    ),
    (r"return\\s+(?:await\\s+)?(?:llm|ai|model|gpt)\\.\\w+\\(", "直接返回单模型结果"),
    (r"(?:llm|ai|model|chat)\\.?\\w*\\([^)]*\\)\\s*\\.\\s*(?:json|parse)", "单模型输出直接解析"),
]
DIVERSITY_PATTERNS = [
    (r"(?:vote|quorum|majority|consensus)\\s*\\(", "表决/共识机制"),
    (r"for\\s+\\w+\\s+in\\s+(?:models|providers|agents)", "多模型遍历"),
    (r"(?:retry|fallback|backup|secondary)\\s*\\(", "重试/备选机制"),
    (r"(?:parallel|concurrent|gather)\\s*\\([^)]*(?:model|agent)", "并行多模型调用"),
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
    high_risk = find_hits(source, HIGH_RISK_PATTERNS)
    single_points = find_hits(source, SINGLE_POINT_PATTERNS)
    diversity = find_hits(source, DIVERSITY_PATTERNS)
    return {{
        "path": str(path),
        "found": True,
        "high_risk": high_risk[:80],
        "single_points": single_points[:80],
        "diversity": diversity[:80],
        "quorum_contracts": build_quorum_contracts(high_risk, single_points, diversity),
    }}


def build_quorum_contracts(
    high_risk: list[dict[str, object]],
    single_points: list[dict[str, object]],
    diversity: list[dict[str, object]],
) -> list[dict[str, object]]:
    contracts = []
    if high_risk or single_points:
        contracts.append({{
            "required_voters": 3,
            "quorum": 2,
            "must_include": [
                "primary_model",
                "independent_second_model",
                "deterministic_validator",
            ],
            "fuse_on": "模型分歧、验证失败、高风险副作用前",
            "current_diversity_signals": len(diversity),
        }})
    return contracts


def summarize(targets: list[str]) -> dict[str, object]:
    files = []
    for target in targets:
        for source in collect_sources(target):
            files.append(inspect_file(source))
    high_risk_total = sum(len(item.get("high_risk", [])) for item in files)
    single_total = sum(len(item.get("single_points", [])) for item in files)
    diversity_total = sum(len(item.get("diversity", [])) for item in files)
    return {{
        "status": "ok" if files else "no_source_files",
        "target": TARGET,
        "files": files,
        "summary": {{
            "files": len(files),
            "high_risk": high_risk_total,
            "single_points": single_total,
            "diversity": diversity_total,
        }},
        "gate": [
            "高风险副作用前必须满足 quorum",
            "单模型输出必须经过独立模型或确定性验证器交叉检查",
            "分歧时熔断并要求人工确认",
        ],
    }}


def main() -> None:
    targets = [arg for arg in sys.argv[1:] if arg]
    print(json.dumps(summarize(targets), ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
'''


def build_consensus_report(target: str, scan_evidence: str) -> str:
    """Build deterministic consensus audit output."""
    script = build_consensus_inventory_script(target)
    return (
        "## Consensus 确定性共识审计\n"
        "- 执行锚点: 高风险决策 inventory + 单点模型决策检测 + quorum 契约。\n"
        "- 审计目标: 防止单一模型输出直接触发删除、发布、资金、权限等副作用。\n\n"
        f"## 静态共识扫描\n{scan_evidence}\n\n"
        "## Consensus Inventory Script\n"
        "```python\n"
        f"{script}"
        "```\n\n"
        "## 执行方式\n"
        "- 保存脚本后运行：`python consensus_inventory.py <source_file_or_directory>`。\n"
        "- 输出 `high_risk`、`single_points`、`diversity`、`quorum_contracts`。\n"
        "- 脚本只读源码，不调用模型、不执行目标代码。\n\n"
        "## Quorum 契约\n"
        "- 高风险副作用前至少需要 3 个投票源，其中 2 个同意才允许继续。\n"
        "- 投票源必须包含：主模型、独立第二模型、确定性验证器。\n"
        "- 模型分歧、验证失败或缺少证据时必须熔断，不能自动选择看似合理的答案。\n\n"
        "## 渐进式实施计划\n"
        "1. 先把 `single_points` 包装为提案对象，禁止直接执行。\n"
        "2. 为 `high_risk` 加 deterministic validator 和 dry-run。\n"
        "3. 引入 quorum runner，记录每个 voter 的结论、理由、置信度和版本。\n"
        "4. 将 inventory JSON 固化为共识边界回归样本。\n"
    )
