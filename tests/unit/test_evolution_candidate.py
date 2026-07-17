from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from naumi_agent.evolution.candidate import (
    CandidateHypothesis,
    EvolutionCandidateDraft,
    build_candidate_draft,
)
from naumi_agent.evolution.evidence import adapt_self_review_static_evidence
from naumi_agent.evolution.self_review import scan_self_review_files


def _secret_evidence(tmp_path: Path):  # type: ignore[no-untyped-def]
    source = tmp_path / "module.py"
    source.write_text('token = "private-secret-value"\n', encoding="utf-8")
    scan = scan_self_review_files([source], workspace_root=tmp_path)
    return adapt_self_review_static_evidence(scan)[0]


def _observations(tmp_path: Path, count: int):  # type: ignore[no-untyped-def]
    original = _secret_evidence(tmp_path)
    start = datetime(2026, 7, 18, 8, 0, tzinfo=UTC)
    return tuple(
        type(original).model_validate(
            {
                **original.model_dump(mode="json"),
                "evidence_id": f"eve_{index:024x}",
                "observed_at": (start + timedelta(minutes=index)).isoformat(),
            }
        )
        for index in range(count)
    )


def test_candidate_draft_is_stable_private_and_non_executable(tmp_path: Path) -> None:
    evidence = _observations(tmp_path, 3)

    candidate = build_candidate_draft(reversed(evidence))

    assert candidate.candidate_id == f"evc_{candidate.fingerprint[:24]}"
    assert candidate.finding_code == "hardcoded_secret"
    assert candidate.kind == "safety"
    assert candidate.risk.level == "high"
    assert candidate.scope == "module.py:token"
    assert candidate.occurrence_count == 3
    assert candidate.source_kinds == ("self_review_static",)
    assert candidate.expected_metrics[0].name == "self_review.hardcoded_secret.count"
    assert candidate.expected_metrics[0].target == 0
    assert candidate.status == "draft"
    assert candidate.experiment_eligible is False
    serialized = candidate.model_dump_json()
    assert "private-secret-value" not in serialized
    assert str(tmp_path) not in serialized


def test_candidate_id_is_stable_as_same_root_accumulates_100_observations(
    tmp_path: Path,
) -> None:
    evidence = _observations(tmp_path, 100)

    first = build_candidate_draft(evidence[:1])
    aggregate = build_candidate_draft((*evidence, evidence[0]))

    assert aggregate.candidate_id == first.candidate_id
    assert aggregate.fingerprint == first.fingerprint
    assert aggregate.occurrence_count == 100
    assert aggregate.first_observed_at == evidence[0].observed_at
    assert aggregate.last_observed_at == evidence[-1].observed_at


def test_candidate_rejects_mixed_roots_absolute_scope_and_forged_identity(
    tmp_path: Path,
) -> None:
    evidence = _observations(tmp_path, 2)
    mixed = evidence[1].model_copy(update={"root_fingerprint": "f" * 64})
    with pytest.raises(ValueError, match="同根"):
        build_candidate_draft((evidence[0], mixed))

    absolute = evidence[0].model_copy(update={"scope": "/private/workspace/module.py"})
    with pytest.raises(ValueError, match="相对 scope"):
        build_candidate_draft((absolute,))

    embedded_absolute = evidence[0].model_copy(
        update={"scope": "module:/Users/private/project.py"}
    )
    with pytest.raises(ValueError, match="绝对路径|相对 scope"):
        build_candidate_draft((embedded_absolute,))

    candidate = build_candidate_draft(evidence)
    forged = candidate.model_dump(mode="json")
    forged["candidate_id"] = "evc_" + "f" * 24
    with pytest.raises(ValueError, match="确定性生成"):
        EvolutionCandidateDraft.model_validate(forged)

    forged = candidate.model_dump(mode="json")
    forged["fingerprint"] = "f" * 64
    forged["candidate_id"] = "evc_" + "f" * 24
    with pytest.raises(ValueError, match="fingerprint 与 Evidence"):
        EvolutionCandidateDraft.model_validate(forged)

    forged = candidate.model_dump(mode="json")
    forged["risk"]["level"] = "low"
    with pytest.raises(ValueError, match="risk 与 draft policy"):
        EvolutionCandidateDraft.model_validate(forged)

    forged = candidate.model_dump(mode="json")
    forged["source_kinds"] = ["harness_failure"]
    with pytest.raises(ValueError, match="source_kinds"):
        EvolutionCandidateDraft.model_validate(forged)

    forged = candidate.model_dump(mode="json")
    forged["first_observed_at"] = "2027-01-01T00:00:00+00:00"
    with pytest.raises(ValueError, match="时间窗口"):
        EvolutionCandidateDraft.model_validate(forged)


def test_candidate_rejects_empty_and_semantically_different_evidence(
    tmp_path: Path,
) -> None:
    evidence = _observations(tmp_path, 2)

    with pytest.raises(ValueError, match="至少需要一个"):
        build_candidate_draft(())

    with pytest.raises(TypeError, match="EvolutionEvidence"):
        build_candidate_draft(({"evidence_id": "not-a-model"},))  # type: ignore[arg-type]

    different = evidence[1].model_copy(update={"finding_code": "syntax_error"})
    with pytest.raises(ValueError, match="同 finding"):
        build_candidate_draft((evidence[0], different))


def test_llm_hypothesis_contract_rejects_secrets_and_absolute_machine_paths() -> None:
    with pytest.raises(ValueError, match="凭据"):
        CandidateHypothesis(origin="llm", text="请使用 api_key=sk-private-value")
    with pytest.raises(ValueError, match="绝对路径"):
        CandidateHypothesis(origin="llm", text="修改 /Users/private/project.py")
