"""Focused contracts for ARC-01.2 domain ownership."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from naumi_agent.architecture.import_graph import (
    ImportGraphReport,
    ModuleRecord,
    scan_import_graph,
)
from naumi_agent.architecture.ownership import (
    DEFAULT_OWNERSHIP_RULES,
    DOMAIN_DEFINITIONS,
    DomainDefinition,
    DomainOwner,
    DomainOwnershipError,
    OwnershipMatch,
    OwnershipRule,
    analyze_domain_ownership,
    require_complete_ownership,
    validate_ownership_contract,
)


def _definition(owner: DomainOwner) -> DomainDefinition:
    return DomainDefinition(
        owner=owner,
        summary=f"{owner.value} summary",
        owns=(f"{owner.value} responsibility",),
        excludes=(f"not {owner.value}",),
    )


def _import_report(*modules: str) -> ImportGraphReport:
    return ImportGraphReport(
        source_root="src/demo",
        modules=tuple(
            ModuleRecord(
                name=module,
                path=f"src/{module.replace('.', '/')}.py",
            )
            for module in modules
        ),
        digest="import-graph-digest",
    )


def test_domain_definitions_cover_each_owner_once_with_real_boundaries() -> None:
    assert {definition.owner for definition in DOMAIN_DEFINITIONS} == set(DomainOwner)
    assert len(DOMAIN_DEFINITIONS) == len(DomainOwner)
    for definition in DOMAIN_DEFINITIONS:
        assert definition.summary.strip()
        assert all(item.strip() for item in definition.owns)
        assert all(item.strip() for item in definition.excludes)


def test_contract_normalizes_definition_and_rule_input_order() -> None:
    definitions = tuple(reversed(tuple(_definition(owner) for owner in DomainOwner)))
    rules = (
        OwnershipRule(
            rule_id="ui-demo",
            owner=DomainOwner.UI,
            match=OwnershipMatch.PREFIX,
            module="demo.ui",
            rationale="UI surface",
        ),
        OwnershipRule(
            rule_id="runtime-root",
            owner=DomainOwner.RUNTIME,
            match=OwnershipMatch.EXACT,
            module="demo",
            rationale="Runtime root",
        ),
    )

    normalized_definitions, normalized_rules = validate_ownership_contract(
        definitions,
        rules,
    )

    assert [item.owner.value for item in normalized_definitions] == sorted(
        owner.value for owner in DomainOwner
    )
    assert [item.rule_id for item in normalized_rules] == [
        "runtime-root",
        "ui-demo",
    ]


def test_contract_rejects_duplicate_rule_ids() -> None:
    rules = (
        OwnershipRule(
            "same",
            DomainOwner.RUNTIME,
            OwnershipMatch.EXACT,
            "demo",
            "runtime",
        ),
        OwnershipRule(
            "same",
            DomainOwner.UI,
            OwnershipMatch.PREFIX,
            "demo.ui",
            "ui",
        ),
    )

    with pytest.raises(DomainOwnershipError, match="rule_id"):
        validate_ownership_contract(DOMAIN_DEFINITIONS, rules)


@pytest.mark.parametrize(
    ("field", "rule"),
    [
        (
            "rule_id",
            OwnershipRule(
                " ",
                DomainOwner.RUNTIME,
                OwnershipMatch.EXACT,
                "demo",
                "runtime",
            ),
        ),
        (
            "module",
            OwnershipRule(
                "runtime-root",
                DomainOwner.RUNTIME,
                OwnershipMatch.EXACT,
                " ",
                "runtime",
            ),
        ),
        (
            "rationale",
            OwnershipRule(
                "runtime-root",
                DomainOwner.RUNTIME,
                OwnershipMatch.EXACT,
                "demo",
                " ",
            ),
        ),
    ],
)
def test_contract_rejects_empty_rule_fields(
    field: str,
    rule: OwnershipRule,
) -> None:
    with pytest.raises(DomainOwnershipError, match=field):
        validate_ownership_contract(DOMAIN_DEFINITIONS, (rule,))


def test_contract_rejects_missing_or_duplicate_domain_definitions() -> None:
    missing = tuple(
        definition
        for definition in DOMAIN_DEFINITIONS
        if definition.owner is not DomainOwner.TASKS
    )
    duplicate = (*DOMAIN_DEFINITIONS, _definition(DomainOwner.RUNTIME))

    with pytest.raises(DomainOwnershipError, match="缺少 owner.*tasks"):
        validate_ownership_contract(missing, ())
    with pytest.raises(DomainOwnershipError, match="重复 owner.*runtime"):
        validate_ownership_contract(duplicate, ())


@pytest.mark.parametrize(
    ("field", "bad_definition"),
    [
        (
            "summary",
            DomainDefinition(DomainOwner.RUNTIME, " ", ("runtime",), ("ui",)),
        ),
        (
            "owns",
            DomainDefinition(DomainOwner.RUNTIME, "runtime", (), ("ui",)),
        ),
        (
            "excludes",
            DomainDefinition(DomainOwner.RUNTIME, "runtime", ("runtime",), ()),
        ),
    ],
)
def test_contract_rejects_empty_domain_boundaries(
    field: str,
    bad_definition: DomainDefinition,
) -> None:
    definitions = tuple(
        bad_definition if item.owner is DomainOwner.RUNTIME else item
        for item in DOMAIN_DEFINITIONS
    )

    with pytest.raises(DomainOwnershipError, match=field):
        validate_ownership_contract(definitions, ())


def test_analysis_assigns_exact_and_prefix_rules_deterministically() -> None:
    import_report = _import_report("demo", "demo.runtime", "demo.runtime.worker")
    rules = (
        OwnershipRule(
            "runtime-package",
            DomainOwner.RUNTIME,
            OwnershipMatch.PREFIX,
            "demo.runtime",
            "runtime package",
        ),
        OwnershipRule(
            "runtime-root",
            DomainOwner.RUNTIME,
            OwnershipMatch.EXACT,
            "demo",
            "runtime root",
        ),
    )

    report = analyze_domain_ownership(
        import_report,
        source_base="base-commit",
        rules=rules,
    )

    assert [
        (item.module, item.owner, item.rule_id) for item in report.assignments
    ] == [
        ("demo", DomainOwner.RUNTIME, "runtime-root"),
        ("demo.runtime", DomainOwner.RUNTIME, "runtime-package"),
        ("demo.runtime.worker", DomainOwner.RUNTIME, "runtime-package"),
    ]
    assert report.issues == ()
    assert report.import_graph_digest == "import-graph-digest"
    assert report.source_base == "base-commit"
    assert sum(item.module_count for item in report.summaries) == 3


def test_analysis_reports_unowned_and_ambiguous_modules() -> None:
    import_report = _import_report("demo", "demo.ui", "demo.unknown")
    rules = (
        OwnershipRule(
            "root",
            DomainOwner.RUNTIME,
            OwnershipMatch.EXACT,
            "demo",
            "root",
        ),
        OwnershipRule(
            "ui-a",
            DomainOwner.UI,
            OwnershipMatch.PREFIX,
            "demo.ui",
            "ui",
        ),
        OwnershipRule(
            "ui-b",
            DomainOwner.UI,
            OwnershipMatch.PREFIX,
            "demo.ui",
            "conflict",
        ),
    )

    report = analyze_domain_ownership(
        import_report,
        source_base="base",
        rules=rules,
    )

    assert [(issue.module, issue.code) for issue in report.issues] == [
        ("demo.ui", "ambiguous_owner"),
        ("demo.unknown", "unowned_module"),
    ]
    assert report.issues[0].matching_rule_ids == ("ui-a", "ui-b")
    assert report.issues[1].matching_rule_ids == ()
    with pytest.raises(DomainOwnershipError, match="2 个模块") as caught:
        require_complete_ownership(report)
    assert "demo.ui" in str(caught.value)
    assert "ui-a" in str(caught.value)


def test_report_json_and_digest_are_stable_across_rule_input_order() -> None:
    import_report = _import_report("demo", "demo.ui")
    rules = (
        OwnershipRule(
            "runtime-root",
            DomainOwner.RUNTIME,
            OwnershipMatch.EXACT,
            "demo",
            "root",
        ),
        OwnershipRule(
            "ui-package",
            DomainOwner.UI,
            OwnershipMatch.PREFIX,
            "demo.ui",
            "ui",
        ),
    )

    first = analyze_domain_ownership(
        import_report,
        source_base="base",
        rules=rules,
    )
    second = analyze_domain_ownership(
        import_report,
        source_base="base",
        rules=tuple(reversed(rules)),
    )

    assert first.digest == second.digest
    assert first.canonical_json() == second.canonical_json()
    payload = json.loads(first.canonical_json())
    digest = payload.pop("digest")
    expected = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    assert digest == expected
    assert "timestamp" not in payload
    assert "/Users/" not in first.canonical_json()


def test_analysis_rejects_non_relative_or_non_posix_module_paths() -> None:
    rules = (
        OwnershipRule(
            "runtime-root",
            DomainOwner.RUNTIME,
            OwnershipMatch.EXACT,
            "demo",
            "root",
        ),
    )
    absolute = ImportGraphReport(
        source_root="src/demo",
        modules=(ModuleRecord("demo", "/private/demo.py"),),
        digest="graph",
    )
    backslash = ImportGraphReport(
        source_root="src/demo",
        modules=(ModuleRecord("demo", "src\\demo.py"),),
        digest="graph",
    )

    with pytest.raises(DomainOwnershipError, match="仓库相对 POSIX"):
        analyze_domain_ownership(absolute, source_base="base", rules=rules)
    with pytest.raises(DomainOwnershipError, match="仓库相对 POSIX"):
        analyze_domain_ownership(backslash, source_base="base", rules=rules)


def test_default_policy_owns_every_real_naumi_module_exactly_once() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    import_report = scan_import_graph(
        repository_root / "src" / "naumi_agent",
        repository_root=repository_root,
    )

    report = analyze_domain_ownership(import_report, source_base="test-base")

    assert len(DEFAULT_OWNERSHIP_RULES) == 41
    assert len(report.assignments) == len(import_report.modules)
    assert report.issues == ()
    assert {summary.owner for summary in report.summaries if summary.module_count} == set(
        DomainOwner
    )
    assert sum(summary.module_count for summary in report.summaries) == len(
        import_report.modules
    )


def test_default_policy_assigns_daemon_contracts_to_runtime() -> None:
    report = analyze_domain_ownership(
        ImportGraphReport(
            source_root="src/naumi_agent",
            modules=(
                ModuleRecord(
                    "naumi_agent.daemons",
                    "src/naumi_agent/daemons/__init__.py",
                    True,
                ),
                ModuleRecord(
                    "naumi_agent.daemons.worker_registry",
                    "src/naumi_agent/daemons/worker_registry.py",
                ),
                ModuleRecord(
                    "naumi_agent.daemons.worker_authority_health",
                    "src/naumi_agent/daemons/worker_authority_health.py",
                ),
            ),
            digest="daemon-graph",
        ),
        source_base="test-base",
    )

    assert report.issues == ()
    assert {item.owner for item in report.assignments} == {DomainOwner.RUNTIME}
    assert {item.rule_id for item in report.assignments} == {"runtime-daemons"}


def test_default_policy_assigns_execution_grants_to_runtime() -> None:
    report = analyze_domain_ownership(
        ImportGraphReport(
            source_root="src/naumi_agent",
            modules=(
                ModuleRecord(
                    "naumi_agent.daemons.execution_grants",
                    "src/naumi_agent/daemons/execution_grants.py",
                ),
            ),
            digest="execution-grant-graph",
        ),
        source_base="test-base",
    )

    assert report.issues == ()
    assert {item.owner for item in report.assignments} == {DomainOwner.RUNTIME}
    assert {item.rule_id for item in report.assignments} == {"runtime-daemons"}


def test_default_policy_rejects_unknown_future_top_level_package() -> None:
    import_report = ImportGraphReport(
        source_root="src/naumi_agent",
        modules=(
            ModuleRecord("naumi_agent", "src/naumi_agent/__init__.py", True),
            ModuleRecord(
                "naumi_agent.future_unknown",
                "src/naumi_agent/future_unknown/__init__.py",
                True,
            ),
        ),
        digest="future-graph",
    )

    report = analyze_domain_ownership(import_report, source_base="test-base")

    assert [(item.module, item.owner) for item in report.assignments] == [
        ("naumi_agent", DomainOwner.RUNTIME)
    ]
    assert [(item.module, item.code) for item in report.issues] == [
        ("naumi_agent.future_unknown", "unowned_module")
    ]


def test_cli_real_repository_is_byte_stable_and_reports_all_owners(
    tmp_path: Path,
) -> None:
    repository_root = Path(__file__).resolve().parents[2]
    first_path = tmp_path / "ownership-a.json"
    second_path = tmp_path / "ownership-b.json"
    command = [
        sys.executable,
        "-m",
        "naumi_agent.architecture.ownership",
        "--source-root",
        "src/naumi_agent",
        "--source-base",
        "test-base",
    ]
    environment = {**os.environ, "PYTHONPATH": "src"}

    first = subprocess.run(
        [*command, "--output", str(first_path)],
        cwd=repository_root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    second = subprocess.run(
        [*command, "--output", str(second_path)],
        cwd=repository_root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert first_path.read_bytes() == second_path.read_bytes()
    assert "Domain ownership 已生成" in first.stdout
    for owner in DomainOwner:
        assert f"{owner.value}=" in first.stdout
    payload = json.loads(first_path.read_text(encoding="utf-8"))
    assert payload["source_base"] == "test-base"
    assert len(payload["assignments"]) > 300
    assert payload["issues"] == []
    assert "timestamp" not in payload
    assert all(not item["path"].startswith("/") for item in payload["assignments"])


def test_cli_writes_unowned_diagnostics_before_returning_two(tmp_path: Path) -> None:
    repository_root = Path(__file__).resolve().parents[2]
    source_root = tmp_path / "repository" / "src" / "demo"
    source_root.mkdir(parents=True)
    (source_root / "__init__.py").write_text("", encoding="utf-8")
    output_path = tmp_path / "unowned.json"

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "naumi_agent.architecture.ownership",
            "--source-root",
            str(source_root),
            "--output",
            str(output_path),
            "--source-base",
            "test-base",
        ],
        cwd=repository_root,
        env={**os.environ, "PYTHONPATH": "src"},
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "ownership 不完整" in completed.stderr
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert [(item["module"], item["code"]) for item in payload["issues"]] == [
        ("demo", "unowned_module")
    ]
