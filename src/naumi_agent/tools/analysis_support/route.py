"""Deterministic MoE route analysis helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

DOMAIN_KEYWORDS: dict[str, list[str]] = {
    "backend": [
        "api",
        "server",
        "database",
        "sql",
        "orm",
        "redis",
        "cache",
        "queue",
        "kafka",
        "grpc",
        "rest",
        "endpoint",
        "migration",
    ],
    "frontend": [
        "ui",
        "component",
        "css",
        "html",
        "react",
        "vue",
        "dom",
        "render",
        "style",
        "layout",
        "responsive",
        "animation",
    ],
    "infra": [
        "docker",
        "k8s",
        "kubernetes",
        "ci/cd",
        "terraform",
        "deploy",
        "nginx",
        "load.balance",
        "monitoring",
        "prometheus",
        "grafana",
    ],
    "security": [
        "auth",
        "jwt",
        "oauth",
        "encrypt",
        "decrypt",
        "ssl",
        "tls",
        "vulnerability",
        "xss",
        "csrf",
        "sql.inject",
        "firewall",
    ],
    "data": [
        "etl",
        "pipeline",
        "spark",
        "hadoop",
        "warehouse",
        "lake",
        "analytics",
        "metric",
        "dashboard",
        "visualization",
        "pandas",
    ],
    "ml": [
        "model",
        "training",
        "inference",
        "neural",
        "transformer",
        "embedding",
        "vector",
        "fine.tun",
        "prompt",
        "llm",
        "rag",
    ],
    "finance": [
        "stock",
        "portfolio",
        "alpha",
        "beta",
        "sharpe",
        "volatility",
        "option",
        "futures",
        "yield",
        "bond",
        "quantitative",
        "backtest",
    ],
    "architecture": [
        "microservice",
        "monolith",
        "event.driven",
        "cqrs",
        "ddd",
        "clean.arch",
        "hexagonal",
        "soa",
        "design.pattern",
        "solid",
    ],
}


@dataclass(frozen=True)
class RouteExpert:
    domain: str
    focus: str
    confidence: int


def scan_route(files: list[Path], source_text: str, task: str) -> str:
    """Analyze task domains and expert routing hints."""
    findings: list[str] = []
    task_lower = task.lower()
    source_lower = source_text.lower()

    task_domains: dict[str, list[str]] = {}
    for domain, keywords in DOMAIN_KEYWORDS.items():
        matched = [keyword for keyword in keywords if keyword in task_lower]
        if matched:
            task_domains[domain] = matched

    if task_domains:
        findings.append("- 任务涉及领域:")
        for domain, keywords in task_domains.items():
            findings.append(f"  - {domain}: {', '.join(keywords)}")
    else:
        findings.append("- 任务领域: 未匹配到明确领域关键词（将由 LLM 判断）")

    code_domains: dict[str, int] = {}
    for domain, keywords in DOMAIN_KEYWORDS.items():
        count = sum(source_lower.count(keyword) for keyword in keywords)
        if count > 0:
            code_domains[domain] = count

    if code_domains:
        findings.append("- 代码库领域分布:")
        for domain, count in sorted(code_domains.items(), key=lambda x: x[1], reverse=True):
            findings.append(f"  - {domain}: {count} 次引用")

    class_count = len(re.findall(r"\bclass\s+\w+", source_text))
    func_count = len(re.findall(r"\bdef\s+\w+", source_text))
    async_count = len(re.findall(r"\basync\s+def\s+", source_text))
    findings.append(f"- 代码规模: {class_count} 个类, {func_count} 个函数 ({async_count} 个异步)")

    modules = set()
    for file in files:
        parts = file.parts
        if "src" in parts:
            idx = parts.index("src")
            if idx + 1 < len(parts):
                modules.add(parts[idx + 1])
    if modules:
        findings.append(f"- 模块划分: {len(modules)} 个 ({', '.join(sorted(modules)[:8])})")

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
        for item in cross_cutting:
            findings.append(f"  - {item}")

    all_domains = set(task_domains.keys()) | set(code_domains.keys())
    if all_domains:
        findings.append(f"\n- 推荐专家小组: {len(all_domains)} 位专家")
        for domain in sorted(all_domains):
            findings.append(f"  - 🧑‍💻 {domain} 专家")

    return "\n".join(findings)


def build_route_report(task: str, scan_evidence: str, source_text: str = "") -> str:
    """Build a deterministic expert panel and synthesis skeleton."""
    experts = select_route_experts(task, source_text)
    lines = [
        "## MoE 确定性专家路由",
        f"- 任务：{task}",
        f"- 专家数：{len(experts)}",
        "",
        "## 路由扫描",
        scan_evidence,
        "",
        "## Expert Panel",
    ]
    for expert in experts:
        lines.extend(
            [
                f"### {expert.domain} 专家",
                f"- Focus: {expert.focus}",
                f"- Confidence: {expert.confidence}/10",
                f"- Recommendation: {route_domain_recommendation(expert.domain)}",
                f"- Concern: {route_domain_concern(expert.domain)}",
            ]
        )
    lines.extend(
        [
            "",
            "## Cross-Expert Resolution",
            *route_conflicts(experts),
            "",
            "## Synthesized Plan",
        ]
    )
    for idx, expert in enumerate(experts, 1):
        lines.append(f"{idx}. [{expert.domain}] 先完成 `{expert.focus}` 的最小可验证改动。")
    lines.extend(
        [
            f"{len(experts) + 1}. [qa] 为每个专家结论补一条 targeted regression test。",
            "",
            "## Resource Estimate",
            f"- Complexity: {route_complexity(experts)}",
            f"- Recommended team: {max(2, min(len(experts), 5))} 人",
            "- Phasing: 先处理安全/数据一致性风险，再处理体验和性能优化。",
        ]
    )
    return "\n".join(lines)


def select_route_experts(task: str, source_text: str = "") -> list[RouteExpert]:
    """Select 3-5 deterministic experts from task and source signals."""
    combined = f"{task}\n{source_text}".lower()
    scored: list[tuple[int, str]] = []
    for domain, keywords in DOMAIN_KEYWORDS.items():
        score = sum(combined.count(keyword) for keyword in keywords)
        if score:
            scored.append((score, domain))
    if not scored:
        scored = [(1, "architecture"), (1, "backend"), (1, "security")]
    selected = [domain for _score, domain in sorted(scored, reverse=True)[:5]]
    if len(selected) < 3:
        for fallback in ("architecture", "backend", "security"):
            if fallback not in selected:
                selected.append(fallback)
            if len(selected) >= 3:
                break
    return [
        RouteExpert(
            domain=domain,
            focus=route_domain_focus(domain),
            confidence=8 if domain in {"security", "backend", "architecture"} else 7,
        )
        for domain in selected[:5]
    ]


def route_domain_focus(domain: str) -> str:
    """Return the core review focus for a domain expert."""
    return {
        "backend": "接口契约、数据流、错误处理和事务边界",
        "frontend": "交互流程、状态同步、可访问性和视觉回归",
        "infra": "部署拓扑、可观测性、容量和回滚策略",
        "security": "认证授权、输入边界、敏感数据和注入风险",
        "data": "数据模型、迁移、质量校验和血缘追踪",
        "ml": "模型调用、评测集、RAG 质量和幻觉防护",
        "finance": "定价假设、风险敞口、回测偏差和审计要求",
        "architecture": "模块边界、耦合度、扩展点和演进路径",
    }.get(domain, "任务拆解、风险识别和验证策略")


def route_domain_recommendation(domain: str) -> str:
    """Return a deterministic first recommendation for a domain expert."""
    return {
        "security": "先定义威胁模型和输入边界，任何写入/执行能力都要有权限检查。",
        "backend": "先稳定 API/工具契约，再实现内部逻辑，避免调用方反复适配。",
        "architecture": "先画清模块边界和依赖方向，再拆分实现。",
        "frontend": "先覆盖核心用户路径，再做视觉和交互细节。",
        "infra": "先定义部署、监控、回滚和资源上限。",
        "data": "先定义 schema、迁移策略和数据质量断言。",
        "ml": "先定义可重复评测集和失败样例，再调 prompt/model。",
        "finance": "先定义风险指标和审计口径，再实现策略逻辑。",
    }.get(domain, "先收敛目标和验收标准，再实施。")


def route_domain_concern(domain: str) -> str:
    """Return the primary risk concern for a domain expert."""
    return {
        "security": "权限遗漏会把普通功能变成越权入口。",
        "backend": "契约漂移会导致工具调用和 CLI/TUI 行为不一致。",
        "architecture": "抽象过早或边界错误会增加后续演进成本。",
        "frontend": "状态反馈不足会让用户误判工具是否完成。",
        "infra": "无观测和回滚会让线上失败不可控。",
        "data": "隐式 schema 变化会破坏历史数据和评测。",
        "ml": "没有评测集时优化只是在调感觉。",
        "finance": "回测偏差会制造虚假的收益确定性。",
    }.get(domain, "主要风险是缺少可验证证据。")


def route_conflicts(experts: list[RouteExpert]) -> list[str]:
    """Detect deterministic cross-expert tradeoffs."""
    domains = {expert.domain for expert in experts}
    conflicts: list[str] = []
    if "security" in domains and "frontend" in domains:
        conflicts.append("- 安全 vs 体验：默认安全优先，用明确授权和状态反馈降低摩擦。")
    if "architecture" in domains and "backend" in domains:
        conflicts.append("- 架构 vs 交付：先保留兼容入口，逐步抽内部模块。")
    if "data" in domains and "ml" in domains:
        conflicts.append("- 数据确定性 vs 模型弹性：评测数据和输出 schema 必须先稳定。")
    if not conflicts:
        conflicts.append("- 未发现强冲突；按风险从高到低串行推进。")
    return conflicts


def route_complexity(experts: list[RouteExpert]) -> str:
    """Map expert panel size to a simple delivery complexity label."""
    count = len(experts)
    if count >= 5:
        return "XL"
    if count == 4:
        return "L"
    if count == 3:
        return "M"
    return "S"
