from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import yaml

import naumi_agent.harness.eval as eval_module
from naumi_agent.harness.eval import evaluate_declared_suites, render_harness_eval
from naumi_agent.harness.eval_models import EvalRunStatus


def _git(workspace: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=workspace,
        check=True,
        capture_output=True,
    )


def _workspace(tmp_path: Path, *, git: bool = True) -> tuple[Path, str]:
    workspace = tmp_path / "workspace"
    suite_root = workspace / "evals"
    fixture = suite_root / "fixtures" / "hello.json"
    fixture.parent.mkdir(parents=True)
    raw = json.dumps(
        {
            "type": "hello",
            "version": 1,
            "payload": {"client": "baseline-surface"},
        },
        sort_keys=True,
    ).encode("utf-8")
    fixture.write_bytes(raw)
    suite = suite_root / "protocol.yaml"
    suite.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "id": "baseline-protocol",
                "title": "Baseline 协议回归",
                "cases": [
                    {
                        "id": "legacy",
                        "runner": "protocol_hello",
                        "input": {"transport": "jsonl"},
                        "fixture": {
                            "path": "fixtures/hello.json",
                            "sha256": hashlib.sha256(raw).hexdigest(),
                        },
                        "expected": {
                            "outcome": "accepted",
                            "selected_version": 1,
                            "capabilities": [
                                "goal_snapshot",
                                "heartbeat",
                                "task_snapshot",
                                "typed_ui_messages",
                                "workbench_proposal_actions",
                                "workbench_snapshot",
                            ],
                        },
                        "metrics": {
                            "primary": "protocol_outcome_match",
                            "guardrails": ["no_model", "no_side_effect"],
                        },
                    }
                ],
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    if git:
        _git(workspace, "init", "-q")
        _git(workspace, "config", "user.email", "tests@naumi.local")
        _git(workspace, "config", "user.name", "Naumi Tests")
        _git(workspace, "add", ".")
        _git(workspace, "commit", "-qm", "eval fixture")
    return workspace, suite.relative_to(workspace).as_posix()


def test_static_eval_attaches_and_renders_clean_no_model_identity(
    tmp_path: Path,
) -> None:
    workspace, declared = _workspace(tmp_path)

    report = evaluate_declared_suites(
        workspace,
        (declared,),
        None,
        profile_digest="a" * 64,
        profile_trusted=True,
    )
    suite = report.suites[0]
    identity = suite.baseline_identity

    assert report.status is EvalRunStatus.PASSED
    assert suite.suite_sha256 == hashlib.sha256(
        (workspace / declared).read_bytes()
    ).hexdigest()
    assert identity is not None
    assert identity.model is None
    assert identity.profile_trusted is True
    assert identity.baseline_eligible is True
    assert identity.configuration.runner_version == "protocol_hello@1"
    assert identity.configuration.suite_sha256 == suite.suite_sha256
    assert identity.configuration.profile_sha256 == "a" * 64
    assert suite.baseline_identity_code == ""
    assert identity.identity_sha256 in json.dumps(report.canonical_payload())

    rendered = render_harness_eval(report)
    assert "Baseline" in rendered
    assert "可晋升" in rendered
    assert identity.identity_sha256[:12] in rendered


def test_untrusted_profile_identity_is_visible_but_not_promotable(
    tmp_path: Path,
) -> None:
    workspace, declared = _workspace(tmp_path)

    report = evaluate_declared_suites(
        workspace,
        (declared,),
        None,
        profile_digest="b" * 64,
        profile_trusted=False,
    )
    identity = report.suites[0].baseline_identity

    assert identity is not None
    assert identity.baseline_eligible is False
    assert "不可晋升" in render_harness_eval(report)


def test_non_git_workspace_keeps_eval_result_and_marks_baseline_unavailable(
    tmp_path: Path,
) -> None:
    workspace, declared = _workspace(tmp_path, git=False)

    report = evaluate_declared_suites(
        workspace,
        (declared,),
        None,
        profile_digest="c" * 64,
        profile_trusted=True,
    )
    suite = report.suites[0]

    assert report.status is EvalRunStatus.PASSED
    assert suite.baseline_identity is None
    assert suite.baseline_identity_code == "baseline_source_unavailable"
    assert "Baseline：不可用" in render_harness_eval(report)


def test_source_change_during_eval_refuses_stale_baseline_identity(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace, declared = _workspace(tmp_path)
    original = eval_module._evaluate_suite_file_raw

    def mutate_after_eval(*args, **kwargs):
        result = original(*args, **kwargs)
        (workspace / "changed-during-eval.txt").write_text("changed", encoding="utf-8")
        return result

    monkeypatch.setattr(eval_module, "_evaluate_suite_file_raw", mutate_after_eval)

    report = evaluate_declared_suites(
        workspace,
        (declared,),
        None,
        profile_digest="d" * 64,
        profile_trusted=True,
    )
    suite = report.suites[0]

    assert report.status is EvalRunStatus.PASSED
    assert suite.baseline_identity is None
    assert suite.baseline_identity_code == "baseline_source_changed"
    assert "源码状态在评测期间发生变化" in render_harness_eval(report)
