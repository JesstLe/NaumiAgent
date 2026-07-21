"""Typed, deterministic ownership contracts for NaumiAgent modules."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from pathlib import Path, PurePosixPath

from naumi_agent.architecture.import_graph import (
    ImportGraphReport,
    ImportGraphScanError,
    scan_import_graph,
    write_utf8_json,
)


class DomainOwnershipError(RuntimeError):
    """The ownership contract or an ownership report is incomplete."""


class DomainOwner(StrEnum):
    """The eight semantic owners defined by ARC-01.2."""

    MODEL = "model"
    RUNTIME = "runtime"
    TOOLS = "tools"
    MEMORY = "memory"
    SAFETY = "safety"
    HARNESS = "harness"
    UI = "ui"
    TASKS = "tasks"


class OwnershipMatch(StrEnum):
    """Supported deterministic module matching modes."""

    EXACT = "exact"
    PREFIX = "prefix"


@dataclass(frozen=True, slots=True)
class DomainDefinition:
    """Human-reviewable semantic boundary for one owner."""

    owner: DomainOwner
    summary: str
    owns: tuple[str, ...]
    excludes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OwnershipRule:
    """One exact or package-prefix ownership declaration."""

    rule_id: str
    owner: DomainOwner
    match: OwnershipMatch
    module: str
    rationale: str


@dataclass(frozen=True, slots=True)
class OwnershipAssignment:
    """The one rule and owner responsible for a discovered module."""

    module: str
    path: str
    owner: DomainOwner
    rule_id: str


@dataclass(frozen=True, slots=True)
class OwnershipIssue:
    """One module that cannot be assigned to exactly one rule."""

    module: str
    path: str
    code: str
    matching_rule_ids: tuple[str, ...]
    message: str


@dataclass(frozen=True, slots=True)
class OwnerSummary:
    """Assignment count for one owner, including zero-count owners."""

    owner: DomainOwner
    module_count: int


@dataclass(frozen=True, slots=True)
class DomainOwnershipReport:
    """Deterministic result tied to one import graph and source base."""

    source_root: str
    source_base: str
    import_graph_digest: str
    assignments: tuple[OwnershipAssignment, ...]
    issues: tuple[OwnershipIssue, ...]
    summaries: tuple[OwnerSummary, ...]
    rules: tuple[OwnershipRule, ...]
    schema_version: int = 1
    digest: str = ""

    def canonical_json(self) -> str:
        """Serialize the full ownership contract deterministically."""
        return _canonical_json(self, include_digest=True)


DOMAIN_DEFINITIONS: tuple[DomainDefinition, ...] = (
    DomainDefinition(
        owner=DomainOwner.MODEL,
        summary="模型调用与能力边界",
        owns=("模型路由", "Provider 能力", "上下文与输出限制", "模型调用协议"),
        excludes=("工具执行", "界面渲染", "会话持久化"),
    ),
    DomainDefinition(
        owner=DomainOwner.RUNTIME,
        summary="运行时编排与进程装配",
        owns=("进程入口", "Agent 生命周期", "编排循环", "运行时 transport"),
        excludes=("具体工具语义", "界面状态", "持久化 schema"),
    ),
    DomainDefinition(
        owner=DomainOwner.TOOLS,
        summary="工具与能力扩展",
        owns=("Tool 接口", "工具实现", "Skill 与 MCP 接入", "工具发现"),
        excludes=("运行时主循环", "权限规则", "界面交互"),
    ),
    DomainDefinition(
        owner=DomainOwner.MEMORY,
        summary="会话、运行记录与长期记忆",
        owns=("会话持久化", "Run record", "长期记忆", "存储语义"),
        excludes=("调度", "渲染", "工具权限"),
    ),
    DomainDefinition(
        owner=DomainOwner.SAFETY,
        summary="权限、验证与隔离边界",
        owns=("权限判定", "验证策略", "隔离", "Worktree 安全"),
        excludes=("业务工具行为", "模型路由", "界面布局"),
    ),
    DomainDefinition(
        owner=DomainOwner.HARNESS,
        summary="执行证据与评测基础设施",
        owns=("执行证据", "完成回执", "检查器", "调试轨迹与评测"),
        excludes=("Session Store 复制", "Runtime 权威", "界面状态"),
    ),
    DomainDefinition(
        owner=DomainOwner.UI,
        summary="CLI、TUI 与 New UI 用户体验",
        owns=("终端界面", "Workbench", "输入与剪贴板", "用户交互"),
        excludes=("工具执行语义", "权限规则", "模型调用"),
    ),
    DomainDefinition(
        owner=DomainOwner.TASKS,
        summary="任务、调度与后台工作单元",
        owns=("任务生命周期", "调度", "后台队列", "工作单元状态"),
        excludes=("Agent 推理循环", "会话持久化", "界面渲染"),
    ),
)


def _package_rule(
    owner: DomainOwner,
    package: str,
    rationale: str,
) -> OwnershipRule:
    return OwnershipRule(
        rule_id=f"{owner.value}-{package.replace('_', '-')}",
        owner=owner,
        match=OwnershipMatch.PREFIX,
        module=f"naumi_agent.{package}",
        rationale=rationale,
    )


def _module_rule(
    owner: DomainOwner,
    module: str,
    rationale: str,
) -> OwnershipRule:
    if module == "__init__":
        slug = "root"
    elif module.startswith("__") and module.endswith("__"):
        slug = f"dunder-{module.strip('_').replace('_', '-')}"
    else:
        slug = module.replace("_", "-")
    return OwnershipRule(
        rule_id=f"{owner.value}-{slug}",
        owner=owner,
        match=OwnershipMatch.EXACT,
        module="naumi_agent" if module == "__init__" else f"naumi_agent.{module}",
        rationale=rationale,
    )


DEFAULT_OWNERSHIP_RULES: tuple[OwnershipRule, ...] = (
    _package_rule(DomainOwner.MODEL, "model", "模型路由与 Provider 能力"),
    _module_rule(DomainOwner.RUNTIME, "__init__", "NaumiAgent 根包入口"),
    _module_rule(DomainOwner.RUNTIME, "__main__", "Python 模块启动入口"),
    _module_rule(DomainOwner.RUNTIME, "main", "进程启动与运行时装配"),
    _package_rule(DomainOwner.RUNTIME, "runtime", "运行时服务"),
    _package_rule(DomainOwner.RUNTIME, "orchestrator", "Agent 编排循环"),
    _package_rule(DomainOwner.RUNTIME, "streaming", "运行时流式协议"),
    _package_rule(DomainOwner.RUNTIME, "agents", "Agent 生命周期"),
    _package_rule(DomainOwner.RUNTIME, "agent_control", "Agent 控制平面"),
    _package_rule(DomainOwner.RUNTIME, "api", "运行时 API transport"),
    _package_rule(DomainOwner.RUNTIME, "config", "运行时配置装配"),
    _package_rule(DomainOwner.RUNTIME, "hooks", "运行时 Hook 生命周期"),
    _package_rule(DomainOwner.RUNTIME, "architecture", "架构契约与运行时边界"),
    _package_rule(DomainOwner.RUNTIME, "release", "发布运行时基础设施"),
    _package_rule(DomainOwner.RUNTIME, "daemons", "隔离 Worker 合同与 Runtime authority"),
    _module_rule(DomainOwner.RUNTIME, "deploy", "部署入口"),
    _module_rule(DomainOwner.RUNTIME, "packaging_entry", "打包启动入口"),
    _module_rule(DomainOwner.RUNTIME, "log_setup", "运行时日志装配"),
    _package_rule(DomainOwner.TOOLS, "tools", "Tool 接口与实现"),
    _package_rule(DomainOwner.TOOLS, "skills", "Skill 发现与加载"),
    _package_rule(DomainOwner.TOOLS, "mcp", "MCP 工具接入"),
    _package_rule(DomainOwner.TOOLS, "evolution", "受控自进化与能力扩展"),
    _package_rule(DomainOwner.MEMORY, "memory", "会话与长期记忆"),
    _package_rule(DomainOwner.MEMORY, "runs", "运行记录持久化"),
    _package_rule(DomainOwner.MEMORY, "persistence", "跨 Store schema、迁移与恢复治理"),
    _package_rule(DomainOwner.SAFETY, "safety", "权限与安全策略"),
    _package_rule(DomainOwner.SAFETY, "validation", "验证策略"),
    _package_rule(DomainOwner.SAFETY, "worktree", "Worktree 隔离边界"),
    _package_rule(DomainOwner.HARNESS, "harness", "执行证据与回执"),
    _package_rule(DomainOwner.HARNESS, "inspector", "运行检查器"),
    _package_rule(DomainOwner.HARNESS, "claude_source", "外部源码身份与审计证据"),
    _module_rule(DomainOwner.HARNESS, "debug_trace", "调试轨迹"),
    _package_rule(DomainOwner.UI, "ui", "New UI 与 Bridge"),
    _package_rule(DomainOwner.UI, "tui", "Textual TUI"),
    _package_rule(DomainOwner.UI, "cli", "终端 CLI 表面"),
    _package_rule(DomainOwner.UI, "workbench", "Workbench 展示层"),
    _module_rule(DomainOwner.UI, "assets", "界面资源"),
    _module_rule(DomainOwner.UI, "clipboard", "剪贴板体验"),
    _module_rule(DomainOwner.UI, "cli_completer", "命令输入补全"),
    _module_rule(DomainOwner.UI, "user_interaction", "结构化用户交互"),
    _package_rule(DomainOwner.TASKS, "tasks", "任务生命周期"),
    _package_rule(DomainOwner.TASKS, "scheduler", "任务调度"),
    _package_rule(DomainOwner.TASKS, "background", "后台工作单元"),
)


def _require_text(value: str, *, field_name: str, item_name: str) -> None:
    if not value.strip():
        raise DomainOwnershipError(f"{item_name} 的 {field_name} 不能为空")


def validate_ownership_contract(
    definitions: tuple[DomainDefinition, ...],
    rules: tuple[OwnershipRule, ...],
) -> tuple[tuple[DomainDefinition, ...], tuple[OwnershipRule, ...]]:
    """Validate and normalize a complete ownership contract."""
    definitions_by_owner: dict[DomainOwner, DomainDefinition] = {}
    for definition in definitions:
        if definition.owner in definitions_by_owner:
            raise DomainOwnershipError(f"存在重复 owner：{definition.owner.value}")
        _require_text(
            definition.summary,
            field_name="summary",
            item_name=f"owner {definition.owner.value}",
        )
        if not definition.owns or any(not item.strip() for item in definition.owns):
            raise DomainOwnershipError(
                f"owner {definition.owner.value} 的 owns 必须包含非空职责"
            )
        if not definition.excludes or any(
            not item.strip() for item in definition.excludes
        ):
            raise DomainOwnershipError(
                f"owner {definition.owner.value} 的 excludes 必须包含非空边界"
            )
        definitions_by_owner[definition.owner] = definition

    missing = sorted(owner.value for owner in DomainOwner - definitions_by_owner.keys())
    if missing:
        raise DomainOwnershipError(f"缺少 owner 定义：{', '.join(missing)}")

    rules_by_id: dict[str, OwnershipRule] = {}
    for rule in rules:
        _require_text(rule.rule_id, field_name="rule_id", item_name="ownership rule")
        _require_text(rule.module, field_name="module", item_name=rule.rule_id)
        _require_text(rule.rationale, field_name="rationale", item_name=rule.rule_id)
        if rule.rule_id in rules_by_id:
            raise DomainOwnershipError(f"存在重复 rule_id：{rule.rule_id}")
        if any(not part.isidentifier() for part in rule.module.split(".")):
            raise DomainOwnershipError(
                f"{rule.rule_id} 的 module 不是合法 Python 模块名：{rule.module}"
            )
        rules_by_id[rule.rule_id] = rule

    return (
        tuple(sorted(definitions_by_owner.values(), key=lambda item: item.owner.value)),
        tuple(sorted(rules_by_id.values(), key=lambda item: item.rule_id)),
    )


def _rule_matches(rule: OwnershipRule, module: str) -> bool:
    if rule.match is OwnershipMatch.EXACT:
        return module == rule.module
    return module == rule.module or module.startswith(f"{rule.module}.")


def _canonical_json(
    report: DomainOwnershipReport,
    *,
    include_digest: bool,
) -> str:
    payload = asdict(report)
    if not include_digest:
        payload.pop("digest", None)
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _report_digest(report: DomainOwnershipReport) -> str:
    content = _canonical_json(report, include_digest=False).encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def _is_repository_relative_posix(path: str) -> bool:
    if not path or "\\" in path:
        return False
    parsed = PurePosixPath(path)
    windows_drive = len(path) >= 3 and path[1] == ":" and path[2] == "/"
    return not parsed.is_absolute() and not windows_drive and ".." not in parsed.parts


def analyze_domain_ownership(
    import_report: ImportGraphReport,
    *,
    source_base: str,
    rules: tuple[OwnershipRule, ...] = DEFAULT_OWNERSHIP_RULES,
) -> DomainOwnershipReport:
    """Assign every discovered module to exactly one ownership rule."""
    _require_text(source_base, field_name="source_base", item_name="ownership report")
    if not import_report.digest:
        raise DomainOwnershipError("import graph digest 不能为空")
    if not _is_repository_relative_posix(import_report.source_root):
        raise DomainOwnershipError(
            f"source_root 必须是仓库相对 POSIX 路径：{import_report.source_root}"
        )
    invalid_paths = sorted(
        module.path
        for module in import_report.modules
        if not _is_repository_relative_posix(module.path)
    )
    if invalid_paths:
        raise DomainOwnershipError(
            f"模块路径必须是仓库相对 POSIX 路径：{invalid_paths[0]}"
        )
    _, normalized_rules = validate_ownership_contract(DOMAIN_DEFINITIONS, rules)

    assignments: list[OwnershipAssignment] = []
    issues: list[OwnershipIssue] = []
    for module in sorted(import_report.modules, key=lambda item: (item.name, item.path)):
        matching_rules = tuple(
            rule for rule in normalized_rules if _rule_matches(rule, module.name)
        )
        if len(matching_rules) == 1:
            rule = matching_rules[0]
            assignments.append(
                OwnershipAssignment(
                    module=module.name,
                    path=module.path,
                    owner=rule.owner,
                    rule_id=rule.rule_id,
                )
            )
            continue
        matching_rule_ids = tuple(rule.rule_id for rule in matching_rules)
        if not matching_rules:
            code = "unowned_module"
            message = "模块没有匹配任何 ownership rule"
        else:
            code = "ambiguous_owner"
            message = "模块同时匹配多个 ownership rule"
        issues.append(
            OwnershipIssue(
                module=module.name,
                path=module.path,
                code=code,
                matching_rule_ids=matching_rule_ids,
                message=message,
            )
        )

    assignments.sort(key=lambda item: (item.module, item.path, item.rule_id))
    issues.sort(
        key=lambda item: (
            item.module,
            item.path,
            item.code,
            item.matching_rule_ids,
        )
    )
    counts = {owner: 0 for owner in DomainOwner}
    for assignment in assignments:
        counts[assignment.owner] += 1
    summaries = tuple(
        OwnerSummary(owner=owner, module_count=counts[owner])
        for owner in sorted(DomainOwner, key=lambda item: item.value)
    )
    report = DomainOwnershipReport(
        source_root=import_report.source_root,
        source_base=source_base,
        import_graph_digest=import_report.digest,
        assignments=tuple(assignments),
        issues=tuple(issues),
        summaries=summaries,
        rules=normalized_rules,
    )
    return replace(report, digest=_report_digest(report))


def require_complete_ownership(report: DomainOwnershipReport) -> None:
    """Reject reports containing unowned or ambiguously owned modules."""
    if not report.issues:
        return
    details: list[str] = []
    for issue in report.issues[:10]:
        rules = ", ".join(issue.matching_rule_ids) or "无匹配规则"
        details.append(f"- {issue.module}: {issue.message}（{rules}）")
    if len(report.issues) > len(details):
        details.append(f"- 另有 {len(report.issues) - len(details)} 个问题，请查看报告")
    raise DomainOwnershipError(
        f"Domain ownership 不完整：{len(report.issues)} 个模块存在问题\n"
        + "\n".join(details)
    )


def _summary_text(report: DomainOwnershipReport) -> str:
    counts = "，".join(
        f"{summary.owner.value}={summary.module_count}"
        for summary in report.summaries
    )
    return (
        f"Domain ownership 已生成：{len(report.assignments)} 个已归属模块，"
        f"{len(report.issues)} 个问题；{counts}。"
    )


def main(argv: list[str] | None = None) -> int:
    """Generate a complete machine-readable ownership artifact."""
    parser = argparse.ArgumentParser(description="生成 NaumiAgent 模块领域归属报告")
    parser.add_argument("--source-root", required=True, help="Python 包源码根目录")
    parser.add_argument("--output", required=True, help="ownership JSON 输出路径")
    parser.add_argument(
        "--source-base",
        required=True,
        help="包含当前源码树的 Git commit/base",
    )
    args = parser.parse_args(argv)

    try:
        import_report = scan_import_graph(args.source_root)
        report = analyze_domain_ownership(
            import_report,
            source_base=args.source_base,
        )
    except (DomainOwnershipError, ImportGraphScanError) as exc:
        parser.exit(2, f"{exc}\n")

    output_path = Path(args.output).expanduser().resolve()
    write_utf8_json(
        output_path,
        report.canonical_json(),
    )
    if report.issues:
        print(
            f"Domain ownership 不完整：{len(report.issues)} 个模块存在问题；"
            f"详细报告已写入 {output_path}",
            file=sys.stderr,
        )
        for issue in report.issues[:10]:
            rule_ids = ", ".join(issue.matching_rule_ids) or "无匹配规则"
            print(f"- {issue.module}: {issue.message}（{rule_ids}）", file=sys.stderr)
        return 2

    print(_summary_text(report))
    return 0


__all__ = [
    "DOMAIN_DEFINITIONS",
    "DEFAULT_OWNERSHIP_RULES",
    "DomainDefinition",
    "DomainOwner",
    "DomainOwnershipError",
    "DomainOwnershipReport",
    "OwnerSummary",
    "OwnershipAssignment",
    "OwnershipIssue",
    "OwnershipMatch",
    "OwnershipRule",
    "analyze_domain_ownership",
    "main",
    "require_complete_ownership",
    "validate_ownership_contract",
]


if __name__ == "__main__":
    raise SystemExit(main())
