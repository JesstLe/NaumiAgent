"""Fail-closed composition for dynamic Evolution Evidence authorities."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping
from types import MappingProxyType

from naumi_agent.evolution.candidate import EvolutionCandidateDraft
from naumi_agent.evolution.evidence import (
    EVOLUTION_DYNAMIC_EVIDENCE_SOURCE_KINDS,
)
from naumi_agent.evolution.review import CandidateSourceAuthorityReader

_SOURCE_KIND_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_MAX_DYNAMIC_SOURCE_KINDS = 16


class EvolutionCandidateSourceAuthorityRouter:
    """Route each dynamic Evidence kind to all of its exact authorities."""

    def __init__(
        self,
        readers_by_source_kind: Mapping[str, CandidateSourceAuthorityReader],
    ) -> None:
        readers = dict(readers_by_source_kind)
        expected = EVOLUTION_DYNAMIC_EVIDENCE_SOURCE_KINDS
        actual = frozenset(readers)
        if actual != expected:
            missing = ", ".join(sorted(expected - actual)) or "无"
            unknown = ", ".join(sorted(actual - expected)) or "无"
            raise ValueError(
                "Evolution 动态来源 authority 注册不完整："
                f"缺失 [{missing}]，未知 [{unknown}]。"
            )
        if not 1 <= len(readers) <= _MAX_DYNAMIC_SOURCE_KINDS:
            raise ValueError("Evolution 动态来源 authority 数量必须在 1..16。")
        if any(_SOURCE_KIND_RE.fullmatch(kind) is None for kind in readers):
            raise ValueError("Evolution 动态来源 kind 格式无效。")
        if any(
            reader is None
            or not callable(getattr(reader, "validate_candidate_sources", None))
            for reader in readers.values()
        ):
            raise TypeError("Evolution 动态来源 reader 必须实现 validate_candidate_sources。")
        self._readers_by_source_kind = MappingProxyType(
            {kind: readers[kind] for kind in sorted(readers)}
        )

    @property
    def source_kinds(self) -> tuple[str, ...]:
        return tuple(self._readers_by_source_kind)

    async def validate_candidate_sources(
        self,
        candidate: EvolutionCandidateDraft,
    ) -> bool:
        """Validate every distinct reader required by one Candidate exactly once."""
        if not isinstance(candidate, EvolutionCandidateDraft):
            raise TypeError("source authority router 只能校验 EvolutionCandidateDraft。")
        readers: list[CandidateSourceAuthorityReader] = []
        seen: set[int] = set()
        for source_kind in candidate.source_kinds:
            reader = self._readers_by_source_kind.get(source_kind)
            if reader is None or id(reader) in seen:
                continue
            seen.add(id(reader))
            readers.append(reader)
        if not readers:
            return True

        async def validate(reader: CandidateSourceAuthorityReader) -> bool:
            try:
                result = await reader.validate_candidate_sources(candidate)
            except (OSError, RuntimeError, TypeError, ValueError):
                return False
            return result is True

        return all(await asyncio.gather(*(validate(reader) for reader in readers)))


__all__ = ["EvolutionCandidateSourceAuthorityRouter"]
