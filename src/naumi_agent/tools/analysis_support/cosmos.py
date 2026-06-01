"""Deterministic world-engine audit helpers."""

from __future__ import annotations

import re

from naumi_agent.tools import analysis_common

STATE_RICHNESS_PATTERNS = [
    (r"(?:position|coordinate|location|vector|matrix)\s*[:=]", "空间/位置状态"),
    (r"(?:velocity|speed|acceleration|momentum|force)\s*[:=]", "运动/力学状态"),
    (r"(?:mass|density|volume|temperature|energy)\s*[:=]", "物理属性状态"),
    (r"(?:color|texture|material|light|shadow)\s*[:=]", "视觉/材质状态"),
    (r"(?:health|hunger|mood|personality|emotion)\s*[:=]", "生命体内部状态"),
    (r"(?:relationship|friendship|trust|reputation)\s*[:=]", "社会关系状态"),
    (r"(?:memory|history|experience|knowledge)\s*[:=]", "记忆/认知状态"),
    (r"(?:resource|inventory|currency|supply|demand)\s*[:=]", "经济/资源状态"),
    (r"(?:rule|law|policy|constraint|boundary)\s*[:=]", "规则/法则状态"),
    (r"(?:time|tick|frame|step|epoch|generation)\s*[:=]", "时间/演化状态"),
]

GENERATIVE_PATTERNS = [
    (r"(?:random|rand|noise|stochastic|sample)\s*\(", "随机性/噪声生成"),
    (r"(?:procedural|generate|synthesize|create)\s*\(", "程序化生成"),
    (r"(?:mutate|evolve|crossover|breed)\s*\(", "进化/变异操作"),
    (r"(?:compose|assemble|combine|blend|interpolate)\s*\(", "组合/混合操作"),
    (r"(?:Perlin|Simplex|Worley|Voronoi|fractal)\s*", "程序化噪声/分形算法"),
    (r"(?:LLM|GPT|Claude|model|neural)\s*.\s*(?:generate|create)", "LLM 生成能力"),
    (r"(?:seed|initialize|bootstrap)\s*\(", "种子/初始化机制"),
]

SOCIAL_PATTERNS = [
    (r"(?:agent|character|npc|entity|actor)\s*", "智能体定义"),
    (r"(?:interact|communicate|message|talk|negotiate)\s*\(", "交互/通信机制"),
    (r"(?:observe|perceive|sense|detect)\s*\(", "感知/观测机制"),
    (r"(?:remember|recall|forget|memory|experience)\s*", "记忆/经验系统"),
    (r"(?:decide|choose|plan|intend|goal)\s*\(", "决策/意图系统"),
    (r"(?:emote|express|react|respond)\s*\(", "情感/反应系统"),
    (r"(?:group|faction|tribe|culture|norm)\s*", "群体/文化结构"),
    (r"(?:trade|exchange|barter|gift|share)\s*\(", "交易/共享机制"),
]

OBSERVER_PATTERNS = [
    (r"(?:on_click|on_hover|on_touch|on_key|input)\s*", "用户输入响应"),
    (r"(?:event|trigger|callback|listener|subscribe)\s*", "事件驱动机制"),
    (r"(?:stream|real.?time|live|update|render)\s*", "实时渲染/流式更新"),
    (r"(?:camera|viewport|frustum|visibility)\s*", "视点/可见性系统"),
    (r"(?:LOD|level.?of.?detail|chunk|region|tile)\s*", "细节层次/分块加载"),
    (r"(?:lazy|on.?demand|just.?in.?time|procedural)\s*", "按需/延迟生成"),
]


def scan_cosmos(target: str) -> str:
    """Scan world-creation potential across state, generation, society, and observation."""
    findings: list[str] = []
    source = analysis_common.read_sources(analysis_common.resolve_target(target))

    if not source.strip():
        return "⚠️ 未找到可分析的源代码。"

    lines = source.split("\n")

    findings.append("## 1. 状态维度丰富度 (State Dimensions)")
    state_dims: dict[str, list[int]] = {}
    for pattern, label in STATE_RICHNESS_PATTERNS:
        for index, line in enumerate(lines, 1):
            if re.search(pattern, line, re.IGNORECASE):
                state_dims.setdefault(label, []).append(index)

    if state_dims:
        total_state = sum(len(line_nos) for line_nos in state_dims.values())
        findings.append(
            f"- 检测到 **{total_state}** 处状态定义，"
            f"覆盖 **{len(state_dims)}** 个维度："
        )
        for label, line_nos in sorted(
            state_dims.items(),
            key=lambda item: -len(item[1]),
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

    findings.append("## 2. 生成能力 (Generative Capacity)")
    gen_hits: dict[str, list[int]] = {}
    for pattern, label in GENERATIVE_PATTERNS:
        for index, line in enumerate(lines, 1):
            if re.search(pattern, line, re.IGNORECASE):
                gen_hits.setdefault(label, []).append(index)

    if gen_hits:
        total_gen = sum(len(line_nos) for line_nos in gen_hits.values())
        findings.append(
            f"- 检测到 **{total_gen}** 处生成能力，"
            f"**{len(gen_hits)}** 类："
        )
        for label, line_nos in sorted(gen_hits.items(), key=lambda item: -len(item[1])):
            findings.append(f"  - {label}: {len(line_nos)} 处")
    else:
        findings.append("- ❌ 无程序化生成能力 — 世界无法自我扩展")
    findings.append("")

    findings.append("## 3. 社会模拟就绪度 (Social Simulation)")
    social_hits: dict[str, list[int]] = {}
    for pattern, label in SOCIAL_PATTERNS:
        for index, line in enumerate(lines, 1):
            if re.search(pattern, line, re.IGNORECASE):
                social_hits.setdefault(label, []).append(index)

    if social_hits:
        total_social = sum(len(line_nos) for line_nos in social_hits.values())
        findings.append(
            f"- 检测到 **{total_social}** 处社会模拟要素，"
            f"**{len(social_hits)}** 类："
        )
        for label, line_nos in sorted(
            social_hits.items(),
            key=lambda item: -len(item[1]),
        ):
            findings.append(f"  - {label}: {len(line_nos)} 处")
    else:
        findings.append("- ❌ 无社会模拟要素 — 无法涌现文明行为")
    findings.append("")

    findings.append("## 4. 观测者效应 (Observer Effect)")
    obs_hits: dict[str, list[int]] = {}
    for pattern, label in OBSERVER_PATTERNS:
        for index, line in enumerate(lines, 1):
            if re.search(pattern, line, re.IGNORECASE):
                obs_hits.setdefault(label, []).append(index)

    if obs_hits:
        total_obs = sum(len(line_nos) for line_nos in obs_hits.values())
        findings.append(
            f"- 检测到 **{total_obs}** 处观测响应机制，"
            f"**{len(obs_hits)}** 类："
        )
        for label, line_nos in obs_hits.items():
            findings.append(f"  - {label}: {len(line_nos)} 处")
        findings.append("- 💡 世界能根据观测者的行为动态展开现实")
    else:
        findings.append("- ⚠️ 无观测响应 — 世界是静态的，不因交互而改变")
    findings.append("")

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
    findings.append(f"- 物理法则维度: {state_score:.0%} ({len(state_dims)}/10 类状态)")
    findings.append(f"- 生成能力: {gen_score:.0%} ({len(gen_hits)} 类生成机制)")
    findings.append(f"- 社会模拟: {social_score:.0%} ({len(social_hits)} 类社会要素)")
    findings.append(f"- 观测响应: {observer_score:.0%} ({len(obs_hits)} 类响应机制)")

    if cosmos_score >= 0.7:
        findings.append("- ✅ 系统具备创世引擎雏形，可尝试构建微型世界模拟")
    elif cosmos_score >= 0.4:
        findings.append("- ⚠️ 部分具备创世条件，需补强缺失维度")
    else:
        findings.append("- ❌ 系统距创世引擎尚远，建议先建立状态空间和生成能力基础")

    return "\n".join(findings)


def build_cosmos_inventory_script(target: str) -> str:
    """Build a dependency-free world-engine readiness scanner."""
    return f'''\
"""Cosmos world-engine inventory script generated by analysis_cosmos."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

TARGET = {target!r}
SOURCE_SUFFIXES = {{".py", ".js", ".ts", ".tsx", ".jsx", ".yaml", ".yml"}}
STATE_PATTERNS = [
    (r"(?:position|coordinate|location|vector|matrix)\\s*[:=]", "空间/位置状态"),
    (r"(?:velocity|speed|acceleration|momentum|force)\\s*[:=]", "运动/力学状态"),
    (r"(?:mass|density|volume|temperature|energy)\\s*[:=]", "物理属性状态"),
    (r"(?:health|hunger|mood|personality|emotion)\\s*[:=]", "生命体内部状态"),
    (r"(?:relationship|trust|reputation|faction)\\s*[:=]", "社会关系状态"),
    (r"(?:memory|history|experience|knowledge)\\s*[:=]", "记忆/认知状态"),
    (r"(?:resource|inventory|currency|supply|demand)\\s*[:=]", "经济/资源状态"),
    (r"(?:rule|law|policy|constraint|boundary)\\s*[:=]", "规则/法则状态"),
    (r"(?:time|tick|frame|step|epoch|generation)\\s*[:=]", "时间/演化状态"),
]
GENERATION_PATTERNS = [
    (r"(?:random|rand|noise|stochastic|sample)\\s*\\(", "随机/采样"),
    (r"(?:procedural|generate|synthesize|create)\\s*\\(", "程序化生成"),
    (r"(?:mutate|evolve|crossover|breed)\\s*\\(", "进化/变异"),
    (r"(?:compose|assemble|combine|interpolate)\\s*\\(", "组合/插值"),
    (r"(?:seed|initialize|bootstrap)\\s*\\(", "种子/初始化"),
]
SOCIAL_PATTERNS = [
    (r"(?:agent|character|npc|entity|actor)", "智能体定义"),
    (r"(?:interact|communicate|message|talk|negotiate)\\s*\\(", "交互/通信"),
    (r"(?:observe|perceive|sense|detect)\\s*\\(", "感知/观测"),
    (r"(?:remember|recall|forget|memory|experience)", "记忆/经验"),
    (r"(?:decide|choose|plan|intend|goal)\\s*\\(", "决策/目标"),
    (r"(?:trade|exchange|barter|share)\\s*\\(", "交易/共享"),
]
OBSERVER_PATTERNS = [
    (r"(?:on_click|on_hover|on_touch|on_key|input)", "用户输入响应"),
    (r"(?:event|trigger|callback|listener|subscribe)", "事件驱动"),
    (r"(?:stream|real.?time|live|update|render)", "实时更新/渲染"),
    (r"(?:camera|viewport|frustum|visibility)", "视点/可见性"),
    (r"(?:lod|level.?of.?detail|chunk|region|tile)", "LOD/分块加载"),
    (r"(?:lazy|on.?demand|just.?in.?time|procedural)", "按需生成"),
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


def score_file(
    state: list[dict[str, object]],
    generation: list[dict[str, object]],
    social: list[dict[str, object]],
    observer: list[dict[str, object]],
) -> float:
    state_labels = {{str(item["label"]) for item in state}}
    generation_labels = {{str(item["label"]) for item in generation}}
    social_labels = {{str(item["label"]) for item in social}}
    observer_labels = {{str(item["label"]) for item in observer}}
    return round(min(1.0, (
        min(len(state_labels) / 8.0, 1.0) * 0.30
        + min(len(generation_labels) / 5.0, 1.0) * 0.25
        + min(len(social_labels) / 5.0, 1.0) * 0.25
        + min(len(observer_labels) / 4.0, 1.0) * 0.20
    )), 3)


def build_genesis_contract(score: float) -> dict[str, object]:
    return {{
        "minimum_score": 0.7,
        "current_score": score,
        "ready_for_micro_world": score >= 0.7,
        "required_planes": [
            "state_dimension_schema",
            "procedural_generation_seed",
            "agent_memory_and_goals",
            "observer_driven_collapse",
            "compute_budget_guardrail",
        ],
    }}


def inspect_file(path: Path) -> dict[str, object]:
    try:
        source = path.read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:
        return {{"path": str(path), "found": False, "error": str(exc)}}
    state = find_hits(source, STATE_PATTERNS)
    generation = find_hits(source, GENERATION_PATTERNS)
    social = find_hits(source, SOCIAL_PATTERNS)
    observer = find_hits(source, OBSERVER_PATTERNS)
    score = score_file(state, generation, social, observer)
    return {{
        "path": str(path),
        "found": True,
        "state": state[:80],
        "generation": generation[:80],
        "social": social[:80],
        "observer": observer[:80],
        "cosmos_score": score,
        "genesis_contract": build_genesis_contract(score),
    }}


def summarize(targets: list[str]) -> dict[str, object]:
    files = []
    for target in targets:
        for source in collect_sources(target):
            files.append(inspect_file(source))
    score = round(
        sum(float(item.get("cosmos_score", 0.0)) for item in files) / len(files),
        3,
    ) if files else 0.0
    return {{
        "status": "ok" if files else "no_source_files",
        "target": TARGET,
        "files": files,
        "summary": {{
            "files": len(files),
            "state": sum(len(item.get("state", [])) for item in files),
            "generation": sum(len(item.get("generation", [])) for item in files),
            "social": sum(len(item.get("social", [])) for item in files),
            "observer": sum(len(item.get("observer", [])) for item in files),
            "average_cosmos_score": score,
        }},
    }}


def main() -> None:
    targets = [arg for arg in sys.argv[1:] if arg]
    print(json.dumps(summarize(targets), ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
'''


def build_cosmos_report(target: str, scan_evidence: str) -> str:
    """Build deterministic world-engine audit output."""
    script = build_cosmos_inventory_script(target)
    return (
        "## Cosmos 确定性创世引擎审计\n"
        "- 执行锚点: 状态维度/生成能力/社会模拟/观测响应 inventory + genesis contract。\n"
        "- 审计目标: 判断系统是否能从静态程序演进为可观测、可生成、可涌现的微型世界。\n\n"
        f"## 静态创世扫描\n{scan_evidence}\n\n"
        "## Cosmos Inventory Script\n"
        "```python\n"
        f"{script}"
        "```\n\n"
        "## 执行方式\n"
        "- 保存脚本后运行：`python cosmos_inventory.py <source_file_or_directory>`。\n"
        "- 输出 `state`、`generation`、`social`、`observer` 和 `genesis_contract`。\n"
        "- 脚本只读源码，不启动仿真、不执行目标代码。\n\n"
        "## Genesis Contract\n"
        "- 世界必须声明状态维度 schema，而不是把状态散落在过程代码里。\n"
        "- 生成能力必须由 seed 驱动，保证可复现、可回放、可裁剪。\n"
        "- 智能体必须有 memory、goal、observe、decide 的最小闭环。\n"
        "- 观测者必须通过事件/视点/LOD 触发现实坍缩，不能全量预生成。\n"
        "- 每个世界 tick 必须绑定算力预算和失败降级策略。\n\n"
        "## 改造计划\n"
        "1. 把 `state` 命中点整理为统一 WorldState schema。\n"
        "2. 为 `generation` 补 seed、版本号和 deterministic replay。\n"
        "3. 为 `social` 补 Agent profile、memory store、decision loop。\n"
        "4. 为 `observer` 补事件驱动、LOD 分块和按需生成缓存。\n"
    )
