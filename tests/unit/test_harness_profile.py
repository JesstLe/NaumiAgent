from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from naumi_agent.harness.models import HarnessProfileStatus
from naumi_agent.harness.profile import MAX_PROFILE_BYTES, load_harness_profile

VALID_PROFILE = """\
schema_version: 1
knowledge:
  entrypoints: [AGENTS.md, docs/harness/index.md]
  include: [src/**/*.py]
  exclude: [data/**]
  max_turn_tokens: 8000
  max_file_bytes: 131072
completion:
  require_todo_reconciliation: true
  require_change_evidence: true
  correction_attempts: 1
  unverified_status: completed_unverified
checks:
  - id: python_tests
    label: Python 定向测试
    argv: [uv, run, pytest, -q]
    timeout_seconds: 180
    when_changed: ['**/*.py']
    required_for: [change]
evals:
  suites: [docs/harness/evals/core.yaml]
  live_default: false
  max_cost_usd: 1.0
  max_duration_seconds: 1800
"""


def _write_profile(workspace: Path, content: str = VALID_PROFILE) -> Path:
    profile = workspace / ".naumi" / "harness.yaml"
    profile.parent.mkdir(parents=True, exist_ok=True)
    profile.write_text(content, encoding="utf-8")
    return profile


def test_load_valid_profile_returns_exact_digest_and_immutable_contract(
    tmp_path: Path,
) -> None:
    profile_path = _write_profile(tmp_path)

    snapshot = load_harness_profile(tmp_path)

    assert snapshot.status is HarnessProfileStatus.VALID
    assert snapshot.profile is not None
    assert snapshot.profile.checks[0].argv == ("uv", "run", "pytest", "-q")
    assert snapshot.profile.knowledge.entrypoints == (
        "AGENTS.md",
        "docs/harness/index.md",
    )
    assert snapshot.digest == hashlib.sha256(profile_path.read_bytes()).hexdigest()
    assert snapshot.errors == ()
    assert snapshot.workspace_root == tmp_path.resolve()
    assert snapshot.profile_path == profile_path.resolve()


def test_missing_profile_is_actionable_non_error_state(tmp_path: Path) -> None:
    snapshot = load_harness_profile(tmp_path)

    assert snapshot.status is HarnessProfileStatus.MISSING
    assert snapshot.profile is None
    assert snapshot.digest is None
    assert snapshot.errors == ()


@pytest.mark.parametrize(
    ("content", "error_code"),
    [
        ("", "empty_profile"),
        ("- not\n- a\n- mapping\n", "invalid_root"),
        ("schema_version: 2\n", "invalid_profile"),
        ("schema_version: 1\nunknown: true\n", "invalid_profile"),
        (
            "schema_version: 1\nvalue: !!python/object/apply:os.system ['echo bad']\n",
            "invalid_yaml",
        ),
        (
            "schema_version: 1\nchecks:\n  - id: tests\n    argv: uv run pytest\n",
            "invalid_profile",
        ),
        (
            "schema_version: 1\nchecks:\n  - id: tests\n    argv: [uv, '', pytest]\n",
            "invalid_profile",
        ),
        (
            "schema_version: 1\nchecks:\n"
            "  - {id: tests, argv: [uv, run, pytest]}\n"
            "  - {id: tests, argv: [uv, run, pytest]}\n",
            "invalid_profile",
        ),
        (
            "schema_version: 1\nchecks:\n"
            "  - {id: tests, argv: [uv], timeout_seconds: 0}\n",
            "invalid_profile",
        ),
        (
            "schema_version: 1\nknowledge:\n  entrypoints: [/etc/passwd]\n",
            "path_outside_workspace",
        ),
        (
            "schema_version: 1\nevals:\n  suites: [../outside.yaml]\n",
            "path_outside_workspace",
        ),
    ],
)
def test_invalid_profiles_return_stable_actionable_error(
    tmp_path: Path,
    content: str,
    error_code: str,
) -> None:
    _write_profile(tmp_path, content)

    snapshot = load_harness_profile(tmp_path)

    assert snapshot.status is HarnessProfileStatus.INVALID
    assert snapshot.profile is None
    assert snapshot.errors[0].code == error_code
    assert snapshot.errors[0].message
    assert snapshot.errors[0].hint.startswith("下一步：")
    assert "Traceback" not in snapshot.errors[0].message


def test_profile_larger_than_limit_is_rejected_before_file_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _write_profile(tmp_path, "x" * (MAX_PROFILE_BYTES + 1))

    def fail_unbounded_read(_path: Path) -> bytes:
        raise AssertionError("超大 Profile 不得调用 Path.read_bytes()")

    monkeypatch.setattr(Path, "read_bytes", fail_unbounded_read)

    snapshot = load_harness_profile(tmp_path)

    assert profile.stat().st_size > MAX_PROFILE_BYTES
    assert snapshot.status is HarnessProfileStatus.INVALID
    assert snapshot.errors[0].code == "profile_too_large"


def test_profile_digest_changes_for_one_byte_edit(tmp_path: Path) -> None:
    profile = _write_profile(tmp_path)
    before = load_harness_profile(tmp_path)

    profile.write_text(VALID_PROFILE + "\n", encoding="utf-8")
    after = load_harness_profile(tmp_path)

    assert before.digest != after.digest
    assert after.status is HarnessProfileStatus.VALID


def test_unicode_contained_paths_are_accepted(tmp_path: Path) -> None:
    docs = tmp_path / "docs" / "设计"
    docs.mkdir(parents=True)
    (docs / "说明.md").write_text("ok", encoding="utf-8")
    _write_profile(
        tmp_path,
        "schema_version: 1\nknowledge:\n  entrypoints: [docs/设计/说明.md]\n",
    )

    snapshot = load_harness_profile(tmp_path)

    assert snapshot.status is HarnessProfileStatus.VALID


def test_existing_symlink_escape_is_rejected(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (outside / "secret.md").write_text("secret", encoding="utf-8")
    (tmp_path / "linked").symlink_to(outside, target_is_directory=True)
    _write_profile(
        tmp_path,
        "schema_version: 1\nknowledge:\n  entrypoints: [linked/secret.md]\n",
    )

    snapshot = load_harness_profile(tmp_path)

    assert snapshot.status is HarnessProfileStatus.INVALID
    assert snapshot.errors[0].code == "path_outside_workspace"


def test_glob_prefix_symlink_escape_is_rejected(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-glob-outside"
    outside.mkdir()
    (tmp_path / "linked").symlink_to(outside, target_is_directory=True)
    _write_profile(
        tmp_path,
        "schema_version: 1\nknowledge:\n  include: [linked/**/*.py]\n",
    )

    snapshot = load_harness_profile(tmp_path)

    assert snapshot.status is HarnessProfileStatus.INVALID
    assert snapshot.errors[0].code == "path_outside_workspace"


def test_explicit_profile_path_must_remain_in_workspace(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-profile.yaml"
    outside.write_text("schema_version: 1\n", encoding="utf-8")

    snapshot = load_harness_profile(tmp_path, outside)

    assert snapshot.status is HarnessProfileStatus.INVALID
    assert snapshot.errors[0].code == "profile_outside_workspace"
