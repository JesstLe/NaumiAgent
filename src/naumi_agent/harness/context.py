"""Budgeted L0/L1 rendering for trusted repository knowledge."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from naumi_agent.harness.knowledge import (
    KnowledgeBudget,
    KnowledgeCandidate,
    KnowledgeIndexSnapshot,
    KnowledgeKind,
    KnowledgeLevel,
    KnowledgeSelection,
    KnowledgeWarning,
    RankedKnowledgeCandidate,
    RepositoryKnowledgeIndex,
    clip_text_to_token_budget,
    estimate_knowledge_tokens,
)
from naumi_agent.harness.models import HarnessProfile


@dataclass(frozen=True)
class KnowledgeContextBundle:
    budget: KnowledgeBudget
    l0: KnowledgeSelection
    l1: KnowledgeSelection
    ranked_paths: tuple[str, ...]
    source_paths: tuple[str, ...]
    warnings: tuple[KnowledgeWarning, ...]

    @property
    def total_tokens(self) -> int:
        return self.l0.estimated_tokens + self.l1.estimated_tokens

    @property
    def rendered(self) -> str:
        return "\n\n".join(
            section for section in (self.l0.content, self.l1.content) if section
        )


class HarnessKnowledgeContextComposer:
    """Compose trusted repository knowledge under deterministic budgets."""

    def __init__(self, index: RepositoryKnowledgeIndex) -> None:
        self._index = index

    def compose(
        self,
        task: str,
        snapshot: KnowledgeIndexSnapshot,
        profile: HarnessProfile,
        *,
        model_window: int | None,
    ) -> KnowledgeContextBundle:
        budget = KnowledgeBudget.for_model(
            profile_l1=profile.knowledge.max_turn_tokens,
            model_window=model_window,
        )
        ranked = self._index.rank(snapshot, task, limit=8)
        instructions = _applicable_instructions(snapshot, ranked)
        l0 = _render_l0(snapshot, profile, ranked, instructions, budget.l0_tokens)
        l1 = self._render_l1(
            snapshot,
            ranked,
            instructions,
            budget.l1_tokens,
        )
        return KnowledgeContextBundle(
            budget=budget,
            l0=l0,
            l1=l1,
            ranked_paths=tuple(item.candidate.path for item in ranked),
            source_paths=l1.source_paths,
            warnings=snapshot.warnings,
        )

    def _render_l1(
        self,
        snapshot: KnowledgeIndexSnapshot,
        ranked: tuple[RankedKnowledgeCandidate, ...],
        instructions: tuple[KnowledgeCandidate, ...],
        budget_tokens: int,
    ) -> KnowledgeSelection:
        if budget_tokens <= 0:
            return _empty_selection(KnowledgeLevel.L1, budget_tokens)

        ranked_reasons = {
            item.candidate.id: item.reasons
            for item in ranked
        }
        ordered: list[KnowledgeCandidate] = []
        seen: set[str] = set()
        for candidate in (*instructions, *(item.candidate for item in ranked)):
            if candidate.id not in seen:
                seen.add(candidate.id)
                ordered.append(candidate)

        title = (
            "## Repository Knowledge（受信任仓库内容）\n"
            "以下内容来自当前已信任 Profile 的只读知识索引；"
            "引用时保留路径与 digest。"
        )
        title_tokens = estimate_knowledge_tokens(title)
        if title_tokens >= budget_tokens:
            clipped = clip_text_to_token_budget(title, budget_tokens)
            return KnowledgeSelection(
                level=KnowledgeLevel.L1,
                content=clipped.text,
                source_ids=(),
                source_paths=(),
                reasons=(),
                estimated_tokens=clipped.estimated_tokens,
                budget_tokens=budget_tokens,
                truncated=True,
            )

        content = title
        included: list[KnowledgeCandidate] = []
        reasons: list[tuple[str, tuple[str, ...]]] = []
        truncated = False
        for candidate in ordered:
            separator = "\n\n" if content else ""
            remaining = budget_tokens - estimate_knowledge_tokens(
                f"{content}{separator}"
            )
            block = self._render_source_block(
                snapshot,
                candidate,
                ranked_reasons.get(candidate.id, ("applicable_instruction",)),
                remaining,
            )
            if not block:
                truncated = True
                break
            content = f"{content}{separator}{block}"
            included.append(candidate)
            reasons.append((
                candidate.id,
                ranked_reasons.get(candidate.id, ("applicable_instruction",)),
            ))
        estimated = estimate_knowledge_tokens(content)
        return KnowledgeSelection(
            level=KnowledgeLevel.L1,
            content=content,
            source_ids=tuple(item.id for item in included),
            source_paths=tuple(item.path for item in included),
            reasons=tuple(reasons),
            estimated_tokens=estimated,
            budget_tokens=budget_tokens,
            truncated=truncated or len(included) < len(ordered),
        )

    def _render_source_block(
        self,
        snapshot: KnowledgeIndexSnapshot,
        candidate: KnowledgeCandidate,
        reasons: tuple[str, ...],
        remaining_tokens: int,
    ) -> str:
        language = _fence_language(candidate.path)
        metadata = (
            f"### `{candidate.path}` · `{candidate.id}`\n"
            f"digest `{candidate.digest[:16]}` · 相关性 `{', '.join(reasons)}`"
        )
        minimum_fence = "```"
        overhead = estimate_knowledge_tokens(
            f"{metadata}\n{minimum_fence}{language}\n\n{minimum_fence}"
        )
        if remaining_tokens <= overhead:
            return ""
        body_budget = min(1_200, remaining_tokens - overhead)
        while body_budget > 0:
            result = self._index.read(
                snapshot,
                path=candidate.path,
                max_tokens=body_budget,
            )
            if result.status != "ok":
                return ""
            fence = safe_markdown_fence(result.content)
            header = f"{metadata}\n{fence}{language}\n"
            footer = f"\n{fence}"
            block = f"{header}{result.content}{footer}"
            excess = estimate_knowledge_tokens(block) - remaining_tokens
            if excess <= 0:
                return block
            body_budget -= max(1, excess)
        return ""


def _applicable_instructions(
    snapshot: KnowledgeIndexSnapshot,
    ranked: tuple[RankedKnowledgeCandidate, ...],
) -> tuple[KnowledgeCandidate, ...]:
    selected: dict[str, KnowledgeCandidate] = {
        item.id: item
        for item in snapshot.candidates
        if item.kind is KnowledgeKind.INSTRUCTION and not item.scope
    }
    for ranked_item in ranked[:4]:
        for instruction in snapshot.instructions_for(ranked_item.candidate.path):
            selected[instruction.id] = instruction
    return tuple(sorted(
        selected.values(),
        key=lambda item: (
            len(PurePosixPath(item.scope).parts) if item.scope else 0,
            item.path,
        ),
    ))


def _render_l0(
    snapshot: KnowledgeIndexSnapshot,
    profile: HarnessProfile,
    ranked: tuple[RankedKnowledgeCandidate, ...],
    instructions: tuple[KnowledgeCandidate, ...],
    budget_tokens: int,
) -> KnowledgeSelection:
    if budget_tokens <= 0:
        return _empty_selection(KnowledgeLevel.L0, budget_tokens)
    checks = ", ".join(check.id for check in profile.checks) or "无"
    lines = [
        "## Repository Knowledge Manifest (L0)",
        f"- 项目：{snapshot.workspace_root.name}",
        f"- Profile：{snapshot.profile_digest[:12]}",
        f"- Git HEAD：{snapshot.git_head or '不可用'}",
        f"- 可用检查名称：{checks}（H2 不执行）",
        "- 适用规则：" + (", ".join(item.path for item in instructions) or "无"),
        "- 本轮候选：",
    ]
    manifest_candidates: list[KnowledgeCandidate] = []
    seen: set[str] = set()
    for candidate in (*instructions, *(item.candidate for item in ranked)):
        if candidate.id not in seen:
            seen.add(candidate.id)
            manifest_candidates.append(candidate)
            lines.append(f"  - {candidate.id} · {candidate.path} · {candidate.kind.value}")
    clipped = clip_text_to_token_budget("\n".join(lines), budget_tokens)
    return KnowledgeSelection(
        level=KnowledgeLevel.L0,
        content=clipped.text,
        source_ids=tuple(item.id for item in manifest_candidates),
        source_paths=tuple(item.path for item in manifest_candidates),
        reasons=(),
        estimated_tokens=clipped.estimated_tokens,
        budget_tokens=budget_tokens,
        truncated=clipped.truncated,
    )


def _empty_selection(level: KnowledgeLevel, budget_tokens: int) -> KnowledgeSelection:
    return KnowledgeSelection(
        level=level,
        content="",
        source_ids=(),
        source_paths=(),
        reasons=(),
        estimated_tokens=0,
        budget_tokens=budget_tokens,
        truncated=False,
    )


def safe_markdown_fence(content: str) -> str:
    """Return a Markdown fence longer than every backtick run in content."""
    longest = max((len(item) for item in re.findall(r"`+", content)), default=0)
    return "`" * max(3, longest + 1)


def _fence_language(path: str) -> str:
    return {
        ".js": "javascript",
        ".json": "json",
        ".md": "markdown",
        ".py": "python",
        ".swift": "swift",
        ".toml": "toml",
        ".ts": "typescript",
        ".yaml": "yaml",
        ".yml": "yaml",
    }.get(PurePosixPath(path).suffix.lower(), "text")
