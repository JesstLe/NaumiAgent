"""Read-only, privacy-bounded review surface for Evolution Candidates."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from naumi_agent.evolution.aggregation import CandidateAggregation, aggregate_candidate
from naumi_agent.evolution.eligibility import (
    CandidateEligibilityAssessment,
    assess_candidate_eligibility,
)
from naumi_agent.evolution.proposal import (
    EvolutionProposalPreview,
    generate_proposal_preview,
)
from naumi_agent.evolution.store import (
    EvolutionCandidateEvent,
    EvolutionCandidateStore,
    EvolutionStoredCandidate,
)

_RISK_LEVELS = frozenset({"low", "medium", "high", "critical"})
_SOURCE_KINDS = frozenset({
    "harness_failure",
    "self_review_static",
    "user_feedback",
    "agent_interpreted_feedback",
})


@dataclass(frozen=True, slots=True)
class EvolutionReviewFilter:
    query: str = ""
    risk: str = ""
    source_kind: str = ""
    limit: int = 50

    def __post_init__(self) -> None:
        if self.risk and self.risk not in _RISK_LEVELS:
            raise ValueError("risk 必须是 low/medium/high/critical。")
        if self.source_kind and self.source_kind not in _SOURCE_KINDS:
            raise ValueError("source 必须是已注册的 Evolution Evidence 类型。")
        if isinstance(self.limit, bool) or not 1 <= self.limit <= 100:
            raise ValueError("Candidate limit 必须在 1..100。")
        if len(self.query) > 256:
            raise ValueError("query 最多 256 字符。")
        if any(char in self.query for char in ("\x00", "\r", "\n")):
            raise ValueError("query 含非法控制字符。")


@dataclass(frozen=True, slots=True)
class EvolutionReviewItem:
    candidate_id: str
    finding_code: str
    kind: str
    scope: str
    risk: str
    hypothesis: str
    occurrence_count: int
    source_kinds: tuple[str, ...]
    providers: tuple[str, ...]
    models: tuple[str, ...]
    platforms: tuple[str, ...]
    first_observed_at: str
    last_observed_at: str
    revision: int
    status: str
    experiment_eligible: bool
    expected_metrics: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    eligibility: CandidateEligibilityAssessment
    aggregation: CandidateAggregation | None
    proposal: EvolutionProposalPreview | None


@dataclass(frozen=True, slots=True)
class EvolutionReviewSnapshot:
    mode: str
    items: tuple[EvolutionReviewItem, ...] = ()
    selected: EvolutionReviewItem | None = None
    events: tuple[EvolutionCandidateEvent, ...] = ()
    filters: EvolutionReviewFilter = EvolutionReviewFilter()


class EvolutionReviewService:
    """Expose verified Candidate Store state without mutation actions."""

    def __init__(self, store: EvolutionCandidateStore) -> None:
        self._store = store

    async def list_snapshot(
        self,
        workspace_root: str | Path,
        *,
        filters: EvolutionReviewFilter | None = None,
    ) -> EvolutionReviewSnapshot:
        active = filters or EvolutionReviewFilter()
        candidates = await self._store.list_candidates(workspace_root, limit=500)
        items = tuple(
            item
            for candidate in candidates
            if _matches(candidate, active)
            for item in (_review_item(candidate, include_refs=False),)
        )[: active.limit]
        return EvolutionReviewSnapshot(mode="list", items=items, filters=active)

    async def detail_snapshot(
        self,
        workspace_root: str | Path,
        candidate_id: str,
    ) -> EvolutionReviewSnapshot:
        stored = await self._store.get_candidate(workspace_root, candidate_id)
        if stored is None:
            return EvolutionReviewSnapshot(mode="detail")
        events = await self._store.list_events(workspace_root, candidate_id)
        return EvolutionReviewSnapshot(
            mode="detail",
            selected=_review_item(stored, include_refs=True),
            events=events[-100:],
        )


def render_evolution_review(snapshot: EvolutionReviewSnapshot) -> str:
    if snapshot.mode == "detail":
        return _render_detail(snapshot)
    lines = ["# Evolution Candidate 审查", ""]
    if not snapshot.items:
        lines.extend([
            "当前过滤条件下没有 Candidate。",
            "",
            "下一步：运行 `/self-review`、Harness 检查或 `/feedback` 产生真实证据。",
        ])
        return "\n".join(lines)
    lines.append(f"共显示 {len(snapshot.items)} 个不可执行候选：")
    lines.append("")
    for item in snapshot.items:
        sources = ", ".join(item.source_kinds)
        lines.extend([
            f"- `{item.candidate_id}` · **{item.risk}** · {item.finding_code}",
            (
                f"  - Scope：`{_escape(item.scope)}` · "
                f"证据：{item.occurrence_count} · Revision：{item.revision}"
            ),
            f"  - 来源：`{_escape(sources)}` · 最近：`{_escape(item.last_observed_at)}`",
        ])
    lines.extend(["", "详情：`/evolution detail <candidate-id>`"])
    return "\n".join(lines)


def _render_detail(snapshot: EvolutionReviewSnapshot) -> str:
    item = snapshot.selected
    if item is None:
        return "Candidate 不存在，或不属于当前工作区。"
    lines = [
        f"# Candidate `{item.candidate_id}`",
        "",
        f"- 状态：`{item.status}`（不可执行：{'否' if item.experiment_eligible else '是'}）",
        f"- Policy：`{item.eligibility.policy_version}` / `{item.eligibility.decision}`",
        f"- 可进入人工审阅：{'是' if item.eligibility.review_ready else '否'}",
        f"- 必须人工治理：{'是' if item.eligibility.human_review_required else '否'}",
        f"- 类型/风险：`{item.kind}` / **{item.risk}**",
        f"- Finding：`{item.finding_code}`",
        f"- Scope：`{_escape(item.scope)}`",
        f"- 唯一证据：{item.occurrence_count} · Revision：{item.revision}",
        f"- 时间：`{_escape(item.first_observed_at)}` → `{_escape(item.last_observed_at)}`",
        f"- 来源：`{_escape(', '.join(item.source_kinds))}`",
        f"- Provider：`{_escape(', '.join(item.providers) or '-')}`",
        f"- Model：`{_escape(', '.join(item.models) or '-')}`",
        f"- Platform：`{_escape(', '.join(item.platforms) or '-')}`",
        "",
        "## 假设",
        "",
        _escape(item.hypothesis),
        "",
        "## 机械指标",
        "",
    ]
    lines.extend(f"- `{_escape(metric)}`" for metric in item.expected_metrics)
    if item.aggregation is not None:
        aggregate = item.aggregation
        lines.extend([
            "",
            f"## 聚合趋势 · `{aggregate.policy_version}`",
            "",
            (
                f"- 趋势：`{aggregate.trend}` · 24h/7d/30d："
                f"{aggregate.count_24h}/{aggregate.count_7d}/{aggregate.count_30d}"
            ),
            f"- 前一 7d：{aggregate.previous_7d_count} · 时间跨度：{aggregate.span_seconds}s",
            (
                "- Provider："
                f"{_render_dimensions(aggregate.provider_counts, aggregate.provider_unique_count)}"
            ),
            f"- Model：{_render_dimensions(aggregate.model_counts, aggregate.model_unique_count)}",
            (
                "- Platform："
                f"{_render_dimensions(aggregate.platform_counts, aggregate.platform_unique_count)}"
            ),
            f"- 来源：{_render_dimensions(aggregate.source_counts, aggregate.source_unique_count)}",
        ])
    lines.extend(["", "## Eligibility Gates", ""])
    lines.extend(
        (
            f"- {'通过' if check.passed else '未通过'} `{check.code}`"
            f"{'（硬阻断）' if not check.passed and check.hard_block else ''}："
            f"{_escape(check.detail)}"
        )
        for check in item.eligibility.checks
    )
    if item.proposal is not None:
        proposal = item.proposal
        lines.extend([
            "",
            f"## Proposal Preview · `{proposal.proposal_kind}`",
            "",
            f"- ID：`{proposal.proposal_id}`",
            f"- 标题：{_escape(proposal.title)}",
            f"- 分类依据：`{proposal.classification_reason}`",
            f"- 影响范围：`{_escape(proposal.impact_scope)}`",
            f"- 风险：`{proposal.risk_level}`",
            "- 可执行：否 · 已入队：否 · 必须人工审阅：是",
        ])
        if proposal.intended_files:
            lines.append(
                "- 目标文件："
                + ", ".join(f"`{_escape(path)}`" for path in proposal.intended_files)
            )
        lines.extend(["", "### Proposal 验证计划", ""])
        lines.extend(
            (
                f"- `{step.metric_name}` {step.direction} {step.target:g} "
                f"via `{step.verifier}`：{_escape(step.procedure)}"
            )
            for step in proposal.validation_plan
        )
    elif item.eligibility.decision != "review_ready":
        lines.extend([
            "",
            "## Proposal Preview",
            "",
            "当前 Candidate 未达到 review_ready，未生成 Proposal。",
        ])
    lines.extend(["", "## Evidence 引用", ""])
    lines.extend(f"- `{_escape(ref)}`" for ref in item.evidence_refs[:20])
    if len(item.evidence_refs) > 20:
        lines.append(f"- … 另有 {len(item.evidence_refs) - 20} 条引用")
    lines.extend(["", "## 审计链", ""])
    lines.extend(
        (
            f"- r{event.revision} `{event.event_type}` · "
            f"+{len(event.added_evidence_ids)} evidence · `{event.occurred_at}`"
        )
        for event in snapshot.events
    )
    lines.extend([
        "",
        "> 当前仅供审查；完整实验 Eligibility、approve/reject/defer 尚未开放。",
    ])
    return "\n".join(lines)


def _review_item(
    stored: EvolutionStoredCandidate,
    *,
    include_refs: bool,
) -> EvolutionReviewItem:
    draft = stored.draft
    evidence = draft.evidence
    return EvolutionReviewItem(
        candidate_id=draft.candidate_id,
        finding_code=draft.finding_code,
        kind=draft.kind,
        scope=draft.scope,
        risk=draft.risk.level,
        hypothesis=draft.hypothesis.text,
        occurrence_count=draft.occurrence_count,
        source_kinds=draft.source_kinds,
        providers=_unique((item.provider for item in evidence), limit=50),
        models=_unique((item.model for item in evidence), limit=50),
        platforms=_unique((item.platform for item in evidence), limit=50),
        first_observed_at=draft.first_observed_at,
        last_observed_at=draft.last_observed_at,
        revision=stored.revision,
        status=draft.status,
        experiment_eligible=draft.experiment_eligible,
        expected_metrics=tuple(
            f"{metric.name} {metric.direction} {metric.target:g} via {metric.verifier}"
            for metric in draft.expected_metrics
        ),
        evidence_refs=(
            _unique(
                (
                    f"{ref.uri}#{ref.sha256[:12]}"
                    for item in evidence
                    for ref in item.refs
                ),
                limit=200,
            )
            if include_refs
            else ()
        ),
        eligibility=assess_candidate_eligibility(draft),
        aggregation=aggregate_candidate(draft) if include_refs else None,
        proposal=generate_proposal_preview(stored) if include_refs else None,
    )


def _matches(candidate: EvolutionStoredCandidate, filters: EvolutionReviewFilter) -> bool:
    draft = candidate.draft
    if filters.risk and draft.risk.level != filters.risk:
        return False
    if filters.source_kind and filters.source_kind not in draft.source_kinds:
        return False
    query = filters.query.strip().casefold()
    if not query:
        return True
    values = (draft.candidate_id, draft.finding_code, draft.kind, draft.scope)
    return any(query in value.casefold() for value in values)


def _unique(values, *, limit: int) -> tuple[str, ...]:
    unique: set[str] = set()
    for value in values:
        normalized = str(value).strip()
        if normalized:
            unique.add(normalized)
        if len(unique) >= limit:
            break
    return tuple(sorted(unique))


def _escape(value: str) -> str:
    return str(value).replace("`", "\\`").replace("\x00", "")[:2_048]


def _render_dimensions(values, unique_count: int) -> str:
    rendered = ", ".join(
        f"`{_escape(item.value)}` {item.count} ({item.percentage:g}%)"
        for item in values
    ) or "-"
    omitted = max(0, unique_count - len(values))
    return f"{rendered}，另有 {omitted} 项" if omitted else rendered


__all__ = [
    "EvolutionReviewFilter",
    "EvolutionReviewItem",
    "EvolutionReviewService",
    "EvolutionReviewSnapshot",
    "render_evolution_review",
]
