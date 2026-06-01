"""Deterministic self-evolution audit helpers."""

from __future__ import annotations

import re

from naumi_agent.tools import analysis_common

RIGIDITY_PATTERNS = [
    (r"(?:MAGIC_NUMBER|HARD_CODED|FIXME|HACK)\s*[:=]", "硬编码常量 (无法运行时调整)"),
    (r"(?:MAX_RETRIES|TIMEOUT|BUFFER_SIZE|PORT)\s*=\s*\d+", "编译时固定参数 (无配置化)"),
    (r"if\s+\w+\s*(?:==|!=)\s*['\"]", "硬编码字符串比较"),
    (r"import\s+\w+", "静态导入 (无动态加载)"),
    (r"class\s+\w+\s*\([^)]*\):", "固定类继承 (无运行时混入)"),
    (
        r"(?:api_key|secret|password|token)\s*=\s*['\"][^'\"]+['\"]",
        "硬编码密钥 (安全+灵活性双杀)",
    ),
]

EVOLUTION_PATTERNS = [
    (r"(?:importlib|__import__|import_module)\s*\(", "动态导入机制"),
    (r"(?:getattr|setattr|delattr|hasattr)\s*\(", "运行时属性操作"),
    (r"(?:exec|eval|compile)\s*\(", "运行时代码执行"),
    (r"(?:type|__class__|__bases__|__dict__)\s*[.=]", "元类/类型操作"),
    (r"(?:plugin|extension|addon|module)\s*_?(?:load|register)", "插件/扩展加载机制"),
    (r"(?:reload|hot.?reload|watch)\s*\(", "热重载机制"),
    (r"(?:config|setting)\s*\.\s*(?:get|load|from)", "外部配置加载"),
    (r"(?:@property|__slots__|__getattr__|__setattr__)", "动态属性描述符"),
    (r"(?:decorator|wrapper|factory)\s*", "装饰器/工厂模式 (可组合)"),
]

REFLECTION_PATTERNS = [
    (r"(?:inspect|dis|ast|symtable)\s*\.", "代码内省模块"),
    (r"(?:__name__|__file__|__doc__|__module__)\s*", "自我元信息访问"),
    (r"(?:sys\.modules|globals|locals)\s*\(", "运行时命名空间访问"),
    (r"(?:__init_subclass__|__set_name__|__class_getitem__)", "类生命周期钩子"),
    (r"(?:abstractmethod|Protocol|ABC)\s*", "抽象接口定义 (可替换实现)"),
]

FLEXIBILITY_PATTERNS = [
    (r"(?:strategy|policy|adapter|bridge)\s*", "设计模式 (可替换组件)"),
    (r"(?:register|registry|factory)\s*[\[(]", "注册表/工厂 (动态实例化)"),
    (r"(?:config\.yaml|config\.json|\.env|toml)", "外部配置文件引用"),
    (r"(?:ABC|Protocol|interface)\s*", "接口抽象层"),
]


def scan_genesis(target: str) -> str:
    """Scan self-evolution readiness and runtime adaptability signals."""
    findings: list[str] = []
    source = analysis_common.read_sources(analysis_common.resolve_target(target))

    if not source.strip():
        return "⚠️ 未找到可分析的源代码。"

    lines = source.split("\n")
    total_lines = len(lines)

    findings.append("## 1. 刚性检测 (Code Rigidity)")
    rigid_hits: dict[str, list[int]] = {}
    for pattern, label in RIGIDITY_PATTERNS:
        for index, line in enumerate(lines, 1):
            if re.search(pattern, line):
                rigid_hits.setdefault(label, []).append(index)

    total_rigid = sum(len(line_nos) for line_nos in rigid_hits.values())
    if rigid_hits:
        findings.append(
            f"- 检测到 **{total_rigid}** 处刚性代码，"
            f"**{len(rigid_hits)}** 类 (需重新编译才能修改)："
        )
        for label, line_nos in sorted(
            rigid_hits.items(),
            key=lambda item: -len(item[1]),
        ):
            findings.append(f"  - {label}: {len(line_nos)} 处")
        findings.append("- 💡 这些点应外化为配置/策略模式，支持运行时变更")
    else:
        findings.append("- ✅ 代码刚性较低，具备灵活调整空间")
    findings.append("")

    findings.append("## 2. 元编程能力 (Meta-Programming)")
    evo_hits: dict[str, list[int]] = {}
    for pattern, label in EVOLUTION_PATTERNS:
        for index, line in enumerate(lines, 1):
            if re.search(pattern, line):
                evo_hits.setdefault(label, []).append(index)

    total_evo = sum(len(line_nos) for line_nos in evo_hits.values())
    if evo_hits:
        findings.append(
            f"- 检测到 **{total_evo}** 处元编程机制，"
            f"**{len(evo_hits)}** 类："
        )
        for label, line_nos in sorted(evo_hits.items(), key=lambda item: -len(item[1])):
            findings.append(f"  - {label}: {len(line_nos)} 处")
    else:
        findings.append("- ❌ 无元编程能力 — 代码无法在运行时修改自身")
    findings.append("")

    findings.append("## 3. 自省能力 (Self-Reflection)")
    reflect_hits: dict[str, list[int]] = {}
    for pattern, label in REFLECTION_PATTERNS:
        for index, line in enumerate(lines, 1):
            if re.search(pattern, line):
                reflect_hits.setdefault(label, []).append(index)

    if reflect_hits:
        total_reflect = sum(len(line_nos) for line_nos in reflect_hits.values())
        findings.append(
            f"- 检测到 **{total_reflect}** 处自省机制，"
            f"**{len(reflect_hits)}** 类："
        )
        for label, line_nos in reflect_hits.items():
            findings.append(f"  - {label}: {len(line_nos)} 处")
    else:
        findings.append("- ❌ 无自省能力 — 系统无法在运行时审视自身结构")
    findings.append("")

    findings.append("## 4. 架构灵活性 (Architecture Flexibility)")
    flex_hits: dict[str, list[int]] = {}
    for pattern, label in FLEXIBILITY_PATTERNS:
        for index, line in enumerate(lines, 1):
            if re.search(pattern, line, re.IGNORECASE):
                flex_hits.setdefault(label, []).append(index)

    if flex_hits:
        total_flex = sum(len(line_nos) for line_nos in flex_hits.values())
        findings.append(
            f"- 检测到 **{total_flex}** 处灵活性模式，"
            f"**{len(flex_hits)}** 类："
        )
        for label, line_nos in flex_hits.items():
            findings.append(f"  - {label}: {len(line_nos)} 处")
    else:
        findings.append("- ⚠️ 架构偏向静态绑定，建议引入策略/注册/工厂模式")
    findings.append("")

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
        findings.append("- ✅ 系统具备较强的自演化基础，可启动热进化实验")
    elif genesis_score >= 0.4:
        findings.append("- ⚠️ 部分具备演化条件，需先降低刚性、增加元编程能力")
    else:
        findings.append("- ❌ 系统高度刚性，需大幅重构才能支持自演化")

    return "\n".join(findings)


def build_genesis_inventory_script(target: str) -> str:
    """Build a dependency-free self-evolution readiness scanner."""
    return f'''\
"""Self-evolution inventory script generated by analysis_genesis."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

TARGET = {target!r}
SOURCE_SUFFIXES = {{".py", ".js", ".ts", ".tsx", ".jsx"}}
RIGIDITY_PATTERNS = [
    (r"(?:MAX_RETRIES|TIMEOUT|BUFFER_SIZE|PORT)\\s*=\\s*\\d+", "固定参数"),
    (r"if\\s+\\w+\\s*(?:==|!=)\\s*['\\\"]", "硬编码字符串分支"),
    (r"(?:api_key|secret|password|token)\\s*=\\s*['\\\"][^'\\\"]+['\\\"]", "硬编码密钥"),
    (r"FIXME|HACK|MAGIC_NUMBER|HARD_CODED", "刚性标记"),
]
EVOLUTION_PATTERNS = [
    (r"(?:importlib|__import__|import_module)\\s*\\(", "动态导入"),
    (r"(?:getattr|setattr|hasattr)\\s*\\(", "运行时属性操作"),
    (r"(?:reload|hot.?reload|watch)\\s*\\(", "热重载/监听"),
    (r"(?:plugin|extension|module).*?(?:load|register)", "插件加载"),
]
REFLECTION_PATTERNS = [
    (r"(?:inspect|ast|symtable|dis)\\.", "代码内省"),
    (r"(?:__name__|__file__|__doc__|__module__)", "自我元信息"),
    (r"(?:globals|locals|sys\\.modules)\\s*\\(", "运行时命名空间"),
]
FLEX_PATTERNS = [
    (r"(?:strategy|policy|adapter|bridge)", "可替换设计模式"),
    (r"(?:register|registry|factory)", "注册表/工厂"),
    (r"(?:ABC|Protocol|interface)", "接口抽象"),
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
    rigidity = find_hits(source, RIGIDITY_PATTERNS)
    evolution = find_hits(source, EVOLUTION_PATTERNS)
    reflection = find_hits(source, REFLECTION_PATTERNS)
    flexibility = find_hits(source, FLEX_PATTERNS)
    return {{
        "path": str(path),
        "found": True,
        "rigidity": rigidity[:80],
        "evolution": evolution[:80],
        "reflection": reflection[:80],
        "flexibility": flexibility[:80],
        "evolution_contract": build_evolution_contract(
            rigidity, evolution, reflection, flexibility,
        ),
    }}


def build_evolution_contract(
    rigidity: list[dict[str, object]],
    evolution: list[dict[str, object]],
    reflection: list[dict[str, object]],
    flexibility: list[dict[str, object]],
) -> dict[str, object]:
    return {{
        "externalize_config_required": bool(rigidity),
        "plugin_loader_present": bool(evolution),
        "self_reflection_present": bool(reflection),
        "abstraction_present": bool(flexibility),
        "minimum_pipeline": [
            "生成候选补丁",
            "沙盒测试",
            "静态审计",
            "热加载或注册表切换",
            "失败自动回滚",
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
            "rigidity": sum(len(item.get("rigidity", [])) for item in files),
            "evolution": sum(len(item.get("evolution", [])) for item in files),
            "reflection": sum(len(item.get("reflection", [])) for item in files),
            "flexibility": sum(len(item.get("flexibility", [])) for item in files),
        }},
    }}


def main() -> None:
    targets = [arg for arg in sys.argv[1:] if arg]
    print(json.dumps(summarize(targets), ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
'''


def build_genesis_report(target: str, scan_evidence: str) -> str:
    """Build deterministic genesis self-evolution output."""
    script = build_genesis_inventory_script(target)
    return (
        "## Genesis 确定性自演化审计\n"
        "- 执行锚点: 刚性/元编程/自省/灵活性 inventory + evolution contract。\n"
        "- 审计目标: 把静态代码推向配置外化、插件注册、热加载、沙盒验证和回滚。\n\n"
        f"## 静态自演化扫描\n{scan_evidence}\n\n"
        "## Genesis Inventory Script\n"
        "```python\n"
        f"{script}"
        "```\n\n"
        "## 执行方式\n"
        "- 保存脚本后运行：`python genesis_inventory.py <source_file_or_directory>`。\n"
        "- 输出 `rigidity`、`evolution`、`reflection`、`flexibility` 和 `evolution_contract`。\n"
        "- 脚本只读源码，不执行目标代码。\n\n"
        "## Evolution Contract\n"
        "- 刚性配置必须外化为 YAML/TOML/env 或策略注册项。\n"
        "- 新能力必须先在沙盒测试，再通过注册表或插件加载，不直接覆盖稳定实现。\n"
        "- 每次热演化必须有版本记录、回滚点、静态审计和目标测试。\n\n"
        "## 实施计划\n"
        "1. 先处理 `rigidity`，降低硬编码和固定分支。\n"
        "2. 引入 registry/factory，让能力可以按名称切换。\n"
        "3. 增加 self-reflection inventory，供 Agent 自审源码结构。\n"
        "4. 建立生成补丁 -> 沙盒验证 -> 热加载 -> 回滚的演化流水线。\n"
    )
