from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from naumi_agent.evolution.proposal import (
    EvolutionProposalPreview,
    classify_proposal_kind,
    generate_proposal_preview,
    parse_proposal_scope_files,
)
from naumi_agent.evolution.store import EvolutionCandidateStore
from naumi_agent.harness.feedback import FeedbackIntakeService, build_direct_user_feedback

NOW = datetime(2026, 7, 18, 20, 0, tzinfo=UTC)


async def _stored_candidate(
    root: Path,
    *,
    repeats: int,
    scope: str = "src/naumi_agent/ui/footer.py:render_footer",
):
    root.mkdir(parents=True, exist_ok=True)
    store = EvolutionCandidateStore(root / "evolution.db")
    intake = FeedbackIntakeService(store)
    result = None
    for offset in range(repeats):
        result = await intake.ingest(
            root,
            build_direct_user_feedback(
                session_id="proposal-preview",
                category="defect",
                scope=scope,
                topic="footer_truncation",
                summary=f"底栏截断 {offset} secret=never-persist",
                now=NOW + timedelta(minutes=offset),
            ),
        )
    assert result is not None
    stored = await store.get_candidate(root, result.candidate_id)
    assert stored is not None
    return store, stored


@pytest.mark.parametrize(
    ("finding_code", "scope", "expected", "reason"),
    [
        ("knowledge_gap", "knowledge:python-runtime", "knowledge", "scope_prefix:knowledge"),
        ("environment_error", "runtime:provider", "profile", "finding:environment_error"),
        ("agent_repetition", "orchestrator:loop", "prompt", "finding:agent_repetition"),
        ("user_reported_defect", "tool:file_read", "tool", "scope_prefix:tool"),
        ("long_function", "tests/unit/test_ui.py", "test", "scope_path:tests"),
        ("long_function", "src/naumi_agent/core.py:run", "code", "fallback:code"),
        (
            "user_reported_defect",
            "files:src/naumi_agent/ui/footer.py,src/naumi_agent/ui/header.py",
            "code",
            "scope_prefix:files",
        ),
    ],
)
def test_classifier_covers_all_six_har_09_proposal_types(
    finding_code: str,
    scope: str,
    expected: str,
    reason: str,
) -> None:
    assert classify_proposal_kind(finding_code, scope) == (expected, reason)


@pytest.mark.parametrize(
    ("finding_code", "scope"),
    [("Bad-Code", "ui:footer"), ("long_function", "../secret"), ("long_function", "/tmp/x")],
)
def test_classifier_rejects_untrusted_identifiers_and_scopes(
    finding_code: str,
    scope: str,
) -> None:
    with pytest.raises(ValueError):
        classify_proposal_kind(finding_code, scope)


@pytest.mark.asyncio
async def test_review_ready_candidate_generates_stable_non_executable_preview(
    tmp_path: Path,
) -> None:
    _store, stored = await _stored_candidate(tmp_path, repeats=2)

    first = generate_proposal_preview(stored)
    second = generate_proposal_preview(stored)

    assert isinstance(first, EvolutionProposalPreview)
    assert first == second
    assert first.proposal_id.startswith("evp_")
    assert first.proposal_kind == "code"
    assert first.classification_reason == "fallback:code"
    assert first.intended_files == ("src/naumi_agent/ui/footer.py",)
    assert first.source.candidate_id == stored.draft.candidate_id
    assert first.source.candidate_revision == 2
    assert first.source.candidate_sha256 == stored.draft_sha256
    assert first.validation_plan[0].verifier == "feedback_recurrence"
    assert first.requires_human_review is True
    assert first.executable is False
    assert first.experiment_eligible is False
    assert first.state == "preview"
    assert "never-persist" not in first.model_dump_json()


@pytest.mark.asyncio
async def test_explicit_multi_file_scope_generates_ordered_intended_files(
    tmp_path: Path,
) -> None:
    scope = "files:src/naumi_agent/ui/footer.py,src/naumi_agent/ui/header.py"
    _store, stored = await _stored_candidate(tmp_path, repeats=2, scope=scope)

    preview = generate_proposal_preview(stored)

    assert preview is not None
    assert preview.proposal_kind == "code"
    assert preview.classification_reason == "scope_prefix:files"
    assert preview.impact_scope == scope
    assert preview.intended_files == (
        "src/naumi_agent/ui/footer.py",
        "src/naumi_agent/ui/header.py",
    )


@pytest.mark.parametrize(
    "scope",
    [
        "files:src/one.py",
        "files:src/one.py,src/one.py",
        "files:src/one.py,../secret.py",
        "files:src/one.py,/tmp/two.py",
        "files:src/one.py,src/two.py:run",
        "files:src/one.py,",
        "files:" + ",".join(f"src/file_{index}.py" for index in range(17)),
    ],
)
def test_multi_file_scope_rejects_malformed_or_unsafe_paths(scope: str) -> None:
    with pytest.raises(ValueError):
        parse_proposal_scope_files(scope)


@pytest.mark.asyncio
async def test_new_candidate_revision_gets_new_proposal_identity(tmp_path: Path) -> None:
    store, stored = await _stored_candidate(tmp_path, repeats=2)
    first = generate_proposal_preview(stored)
    intake = FeedbackIntakeService(store)
    await intake.ingest(
        tmp_path,
        build_direct_user_feedback(
            session_id="proposal-preview",
            category="defect",
            scope="src/naumi_agent/ui/footer.py:render_footer",
            topic="footer_truncation",
            summary="底栏第三次截断",
            now=NOW + timedelta(minutes=3),
        ),
    )
    updated = await store.get_candidate(tmp_path, stored.draft.candidate_id)
    assert updated is not None
    second = generate_proposal_preview(updated)

    assert first is not None and second is not None
    assert first.proposal_id != second.proposal_id
    assert second.source.candidate_revision == 3


@pytest.mark.asyncio
async def test_unready_or_protected_candidate_does_not_generate_preview(
    tmp_path: Path,
) -> None:
    _store, single = await _stored_candidate(tmp_path / "single", repeats=1)
    _protected_store, protected = await _stored_candidate(
        tmp_path / "protected",
        repeats=2,
        scope="safety:permissions",
    )

    assert generate_proposal_preview(single) is None
    assert generate_proposal_preview(protected) is None


@pytest.mark.asyncio
async def test_generator_rejects_forged_stored_digest(tmp_path: Path) -> None:
    _store, stored = await _stored_candidate(tmp_path, repeats=2)

    with pytest.raises(ValueError, match="摘要不可信"):
        generate_proposal_preview(replace(stored, draft_sha256="0" * 64))
