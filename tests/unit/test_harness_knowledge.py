"""Repository Harness knowledge contracts and deterministic primitives."""

from __future__ import annotations

import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from naumi_agent.harness.knowledge import (
    KnowledgeBudget,
    KnowledgeCandidate,
    KnowledgeIndexSnapshot,
    KnowledgeKind,
    KnowledgeLevel,
    KnowledgeReadResult,
    KnowledgeSelection,
    KnowledgeWarning,
    RepositoryKnowledgeIndex,
    clip_text_to_token_budget,
    estimate_knowledge_tokens,
    knowledge_digest,
    knowledge_id_for_path,
)
from naumi_agent.harness.models import HarnessKnowledgeSpec, HarnessProfile


def _profile(
    *,
    entrypoints: tuple[str, ...] = (),
    include: tuple[str, ...] = ("**/*",),
    exclude: tuple[str, ...] = (),
    max_file_bytes: int = 131_072,
) -> HarnessProfile:
    return HarnessProfile(
        schema_version=1,
        knowledge=HarnessKnowledgeSpec(
            entrypoints=entrypoints,
            include=include,
            exclude=exclude,
            max_file_bytes=max_file_bytes,
        ),
    )


def _write(path: Path, content: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")


def _git(workspace: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    )


def _init_git(workspace: Path) -> None:
    _git(workspace, "init", "-q")
    _git(workspace, "config", "user.email", "harness@example.test")
    _git(workspace, "config", "user.name", "Harness Test")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", 0),
        ("a", 1),
        ("abc", 1),
        ("abcd", 2),
        ("知识", 2),
        ("def run() -> None:\n    pass", 9),
    ],
)
def test_token_estimate_is_conservative_and_unicode_stable(
    text: str,
    expected: int,
) -> None:
    assert estimate_knowledge_tokens(text) == expected


def test_budget_applies_profile_model_fraction_and_hard_limit() -> None:
    constrained = KnowledgeBudget.for_model(
        profile_l1=8_000,
        model_window=32_000,
    )
    large = KnowledgeBudget.for_model(
        profile_l1=12_000,
        model_window=200_000,
    )
    tiny = KnowledgeBudget.for_model(
        profile_l1=8_000,
        model_window=4_000,
    )
    unknown = KnowledgeBudget.for_model(
        profile_l1=8_000,
        model_window=None,
    )

    assert constrained.l0_tokens == 1_000
    assert constrained.l1_tokens == 3_800
    assert constrained.total_tokens == 4_800
    assert large.l0_tokens == 1_000
    assert large.l1_tokens == 11_000
    assert large.total_tokens == 12_000
    assert tiny.l0_tokens == 600
    assert tiny.l1_tokens == 0
    assert tiny.total_tokens == 600
    assert unknown.total_tokens == 9_000


@pytest.mark.parametrize("profile_l1", [0, -1, 12_001])
def test_budget_rejects_invalid_profile_limits(profile_l1: int) -> None:
    with pytest.raises(ValueError, match="Profile L1"):
        KnowledgeBudget.for_model(profile_l1=profile_l1, model_window=32_000)


@pytest.mark.parametrize("model_window", [0, -1])
def test_budget_rejects_non_positive_known_model_windows(model_window: int) -> None:
    with pytest.raises(ValueError, match="模型上下文窗口"):
        KnowledgeBudget.for_model(profile_l1=8_000, model_window=model_window)


def test_clipping_preserves_whole_unicode_lines_and_final_budget() -> None:
    text = "第一行知识\n第二行更长的知识\nthird line\nfourth line"

    clipped = clip_text_to_token_budget(text, 19)

    assert clipped.truncated
    assert clipped.text.startswith("第一行知识\n")
    assert clipped.text.endswith("…（内容已按知识预算截断）")
    assert "\ufffd" not in clipped.text
    assert clipped.estimated_tokens <= 19
    assert clipped.estimated_tokens == estimate_knowledge_tokens(clipped.text)


def test_clipping_handles_empty_zero_and_exact_fit() -> None:
    text = "one line"
    exact_budget = estimate_knowledge_tokens(text)

    empty = clip_text_to_token_budget("", 0)
    zero = clip_text_to_token_budget(text, 0)
    exact = clip_text_to_token_budget(text, exact_budget)

    assert (empty.text, empty.truncated, empty.estimated_tokens) == ("", False, 0)
    assert (zero.text, zero.truncated, zero.estimated_tokens) == ("", True, 0)
    assert (exact.text, exact.truncated) == (text, False)


def test_knowledge_identity_and_digest_are_stable() -> None:
    assert knowledge_id_for_path("src/naumi_agent/harness/knowledge.py") == (
        "kn_55c996f02c330866"
    )
    assert knowledge_id_for_path("src\\naumi_agent\\harness\\knowledge.py") == (
        "kn_55c996f02c330866"
    )
    assert knowledge_digest(b"repository knowledge") == (
        "aad51024ba004c2e3700d233c1f786e65761dc108818639dbea306a0e3c0884f"
    )


def test_knowledge_contracts_are_immutable_and_deterministic(tmp_path: Path) -> None:
    candidate = KnowledgeCandidate(
        id="kn_55c996f02c330866",
        path="src/naumi_agent/harness/knowledge.py",
        kind=KnowledgeKind.SOURCE,
        digest="a" * 64,
        size_bytes=128,
        modified_ns=42,
        scope="src/naumi_agent/harness",
    )
    warning = KnowledgeWarning(
        code="git_unavailable",
        message="Git 状态不可用。",
        hint="确认 Git 已安装后重试。",
    )
    snapshot = KnowledgeIndexSnapshot(
        workspace_root=tmp_path.resolve(),
        profile_digest="b" * 64,
        fingerprint="c" * 64,
        git_head="deadbeef",
        changed_paths=("src/a.py",),
        candidates=(candidate,),
        warnings=(warning,),
    )
    selection = KnowledgeSelection(
        level=KnowledgeLevel.L1,
        content="bounded",
        source_ids=(candidate.id,),
        source_paths=(candidate.path,),
        reasons=((candidate.id, ("path:knowledge",)),),
        estimated_tokens=3,
        budget_tokens=8,
        truncated=False,
    )
    result = KnowledgeReadResult(
        status="ok",
        content="bounded",
        source=candidate,
        estimated_tokens=3,
        budget_tokens=8,
        truncated=False,
    )

    assert snapshot.candidates == (candidate,)
    assert selection.source_paths == (candidate.path,)
    assert result.source is candidate
    with pytest.raises(FrozenInstanceError):
        candidate.path = "changed.py"  # type: ignore[misc]


def test_discovery_applies_nested_agents_only_to_descendants(tmp_path: Path) -> None:
    _write(tmp_path / "AGENTS.md", "root rules")
    _write(tmp_path / "src/AGENTS.md", "src rules")
    _write(tmp_path / "src/pkg/AGENTS.md", "pkg rules")
    _write(tmp_path / "other/AGENTS.md", "other rules")
    _write(tmp_path / "src/pkg/api.py", "def api(): pass")

    snapshot = RepositoryKnowledgeIndex(tmp_path).build(
        _profile(include=("src/**/*.py",)),
        profile_digest="a" * 64,
    )

    assert [
        item.path for item in snapshot.instructions_for("src/pkg/api.py")
    ] == ["AGENTS.md", "src/AGENTS.md", "src/pkg/AGENTS.md"]
    assert [
        item.path for item in snapshot.instructions_for("other/view.py")
    ] == ["AGENTS.md", "other/AGENTS.md"]


def test_discovery_combines_entrypoints_globs_build_files_and_excludes(
    tmp_path: Path,
) -> None:
    _write(tmp_path / "AGENTS.md", "root rules")
    _write(tmp_path / "README.md", "# Project")
    _write(tmp_path / "pyproject.toml", "[project]\nname='demo'")
    _write(tmp_path / "frontend/package.json", '{"name":"ui"}')
    _write(tmp_path / "src/app.py", "def app(): pass")
    _write(tmp_path / "tests/test_app.py", "def test_app(): pass")
    _write(tmp_path / "generated/skip.py", "generated = True")

    snapshot = RepositoryKnowledgeIndex(tmp_path).build(
        _profile(
            entrypoints=("README.md", "src/app.py"),
            include=("src/**/*.py", "tests/**/*.py", "generated/**/*.py"),
            exclude=("generated/**",),
        ),
        profile_digest="b" * 64,
    )
    by_path = {item.path: item for item in snapshot.candidates}

    assert set(by_path) == {
        "AGENTS.md",
        "README.md",
        "frontend/package.json",
        "pyproject.toml",
        "src/app.py",
        "tests/test_app.py",
    }
    assert by_path["AGENTS.md"].kind is KnowledgeKind.INSTRUCTION
    assert by_path["README.md"].kind is KnowledgeKind.ENTRYPOINT
    assert by_path["pyproject.toml"].kind is KnowledgeKind.BUILD
    assert by_path["src/app.py"].kind is KnowledgeKind.ENTRYPOINT
    assert by_path["tests/test_app.py"].kind is KnowledgeKind.TEST


def test_discovery_reports_real_git_changed_and_untracked_paths(tmp_path: Path) -> None:
    _init_git(tmp_path)
    _write(tmp_path / "src/tracked.py", "value = 1")
    _write(tmp_path / "AGENTS.md", "root")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "initial")
    _write(tmp_path / "src/tracked.py", "value = 2")
    _write(tmp_path / "src/untracked.py", "value = 3")

    snapshot = RepositoryKnowledgeIndex(tmp_path).build(
        _profile(include=("src/**/*.py",)),
        profile_digest="c" * 64,
    )
    by_path = {item.path: item for item in snapshot.candidates}

    assert snapshot.git_head
    assert snapshot.changed_paths == ("src/tracked.py", "src/untracked.py")
    assert by_path["src/tracked.py"].changed
    assert by_path["src/untracked.py"].changed


def test_discovery_degrades_cleanly_without_a_git_repository(tmp_path: Path) -> None:
    _write(tmp_path / "AGENTS.md", "root")

    snapshot = RepositoryKnowledgeIndex(tmp_path).build(
        _profile(include=()),
        profile_digest="d" * 64,
    )

    assert snapshot.git_head is None
    assert snapshot.changed_paths == ()
    assert "git_not_repository" in {warning.code for warning in snapshot.warnings}


def test_discovery_rejects_unsafe_binary_large_and_sensitive_candidates(
    tmp_path: Path,
) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
    outside.write_text("outside", encoding="utf-8")
    _write(tmp_path / "AGENTS.md", "root")
    _write(tmp_path / "src/safe.py", "safe = True")
    _write(tmp_path / "src/binary.py", b"abc\x00def")
    _write(tmp_path / "src/huge.py", "x" * 2_049)
    _write(tmp_path / "src/payload.txt", "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo=" * 10)
    _write(tmp_path / "debug/run.log", "long log")
    _write(tmp_path / "changes/full.diff", "diff --git a/a b/a")
    _write(tmp_path / "assets/screenshot.png", b"\x89PNG\r\n")
    _write(tmp_path / ".env", "API_KEY=secret")
    _write(tmp_path / "config/credentials.json", "{}")
    (tmp_path / "src/escape.py").symlink_to(outside)

    snapshot = RepositoryKnowledgeIndex(tmp_path).build(
        _profile(include=("**/*",), max_file_bytes=1_024),
        profile_digest="e" * 64,
    )
    paths = {item.path for item in snapshot.candidates}
    warning_codes = {warning.code for warning in snapshot.warnings}

    assert "src/safe.py" in paths
    assert not paths.intersection(
        {
            "src/binary.py",
            "src/huge.py",
            "src/payload.txt",
            "debug/run.log",
            "changes/full.diff",
            "assets/screenshot.png",
            ".env",
            "config/credentials.json",
            "src/escape.py",
        }
    )
    assert {
        "binary_file",
        "file_too_large",
        "base64_payload",
        "unsupported_artifact",
        "sensitive_path",
        "path_escape",
    } <= warning_codes


def test_discovery_uses_bounded_stream_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write(tmp_path / "AGENTS.md", "root")
    _write(tmp_path / "src/safe.py", "safe = True")

    def fail_read_bytes(path: Path) -> bytes:
        raise AssertionError(f"知识发现不得使用无界 read_bytes：{path}")

    monkeypatch.setattr(Path, "read_bytes", fail_read_bytes)

    snapshot = RepositoryKnowledgeIndex(tmp_path).build(
        _profile(include=("src/**/*.py",), max_file_bytes=1_024),
        profile_digest="0" * 64,
    )

    assert {item.path for item in snapshot.candidates} == {
        "AGENTS.md",
        "src/safe.py",
    }


def test_discovery_handles_unicode_unreadable_and_duplicate_aliases(
    tmp_path: Path,
) -> None:
    _write(tmp_path / "AGENTS.md", "root")
    _write(tmp_path / "文档/架构.md", "架构说明")
    unreadable = tmp_path / "src/private.py"
    _write(unreadable, "private = True")
    unreadable.chmod(0)
    try:
        snapshot = RepositoryKnowledgeIndex(tmp_path).build(
            _profile(
                entrypoints=("文档/架构.md",),
                include=("文档/**/*.md", "src/**/*.py"),
            ),
            profile_digest="f" * 64,
        )
    finally:
        unreadable.chmod(0o600)

    paths = [item.path for item in snapshot.candidates]
    assert paths.count("文档/架构.md") == 1
    assert "src/private.py" not in paths
    assert "file_unreadable" in {warning.code for warning in snapshot.warnings}


def test_discovery_is_deterministic_under_concurrent_builds(tmp_path: Path) -> None:
    _write(tmp_path / "AGENTS.md", "root")
    for index in range(20):
        _write(tmp_path / f"src/module_{index}.py", f"VALUE = {index}")
    knowledge_index = RepositoryKnowledgeIndex(tmp_path)
    profile = _profile(include=("src/**/*.py",))

    with ThreadPoolExecutor(max_workers=8) as pool:
        snapshots = list(
            pool.map(
                lambda _: knowledge_index.build(profile, profile_digest="1" * 64),
                range(24),
            )
        )

    assert len({snapshot.fingerprint for snapshot in snapshots}) == 1
    assert len({snapshot.candidates for snapshot in snapshots}) == 1


def test_discovery_reports_git_timeout_without_breaking_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write(tmp_path / "AGENTS.md", "root")

    def timeout(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.TimeoutExpired(cmd="git", timeout=0.01)

    monkeypatch.setattr(subprocess, "run", timeout)
    snapshot = RepositoryKnowledgeIndex(tmp_path).build(
        _profile(include=()),
        profile_digest="2" * 64,
    )

    assert [item.path for item in snapshot.candidates] == ["AGENTS.md"]
    assert "git_timeout" in {warning.code for warning in snapshot.warnings}


def test_warm_rank_and_freshness_use_cached_text_and_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write(tmp_path / "AGENTS.md", "root")
    _write(tmp_path / "src/engine.py", "class AgentEngine: pass")
    index = RepositoryKnowledgeIndex(tmp_path)
    snapshot = index.build(
        _profile(include=("src/**/*.py",)),
        profile_digest="3" * 64,
    )

    def fail_read_bytes(path: Path) -> bytes:
        raise AssertionError(f"暖缓存不得重读所有文件 bytes：{path}")

    monkeypatch.setattr(Path, "read_bytes", fail_read_bytes)

    ranked = index.rank(snapshot, "修改 AgentEngine", limit=4)

    assert ranked[0].candidate.path == "src/engine.py"
    assert index.is_current(snapshot)
