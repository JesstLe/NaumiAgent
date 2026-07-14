"""Focused contracts for the ARC-01.1 import graph scanner."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from naumi_agent.architecture import import_graph as import_graph_module
from naumi_agent.architecture.import_graph import (
    DependencyEdge,
    GraphScope,
    ImportGraphScanError,
    ImportKind,
    ImportScope,
    scan_import_graph,
)


def _write(path: Path, content: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def test_discovers_modules_deterministically_and_maps_package_initializers(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "src" / "demo"
    _write(source_root / "zeta.py")
    _write(source_root / "__init__.py")
    _write(source_root / "alpha" / "worker.py")
    _write(source_root / "alpha" / "__init__.py")

    report = scan_import_graph(source_root, repository_root=tmp_path)

    assert [(module.name, module.path) for module in report.modules] == [
        ("demo", "src/demo/__init__.py"),
        ("demo.alpha", "src/demo/alpha/__init__.py"),
        ("demo.alpha.worker", "src/demo/alpha/worker.py"),
        ("demo.zeta", "src/demo/zeta.py"),
    ]


@pytest.mark.parametrize("root_kind", ["missing", "file"])
def test_source_root_must_exist_and_be_a_directory(
    tmp_path: Path,
    root_kind: str,
) -> None:
    source_root = tmp_path / "src" / "demo"
    if root_kind == "file":
        _write(source_root, "not a package\n")

    with pytest.raises(ImportGraphScanError) as caught:
        scan_import_graph(source_root, repository_root=tmp_path)

    assert caught.value.path == "src/demo"
    assert caught.value.line == 0
    assert "源码根目录" in caught.value.message


def test_source_root_outside_repository_fails_with_scanner_error(
    tmp_path: Path,
) -> None:
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    source_root = tmp_path / "external" / "demo"
    _write(source_root / "__init__.py")

    with pytest.raises(ImportGraphScanError) as caught:
        scan_import_graph(source_root, repository_root=repository_root)

    assert caught.value.path == source_root.resolve().as_posix()
    assert caught.value.line == 0
    assert "仓库目录内" in caught.value.message


def test_source_symlink_cannot_escape_source_root(tmp_path: Path) -> None:
    source_root = tmp_path / "src" / "demo"
    _write(source_root / "__init__.py")
    outside_source = tmp_path / "outside.py"
    _write(outside_source, "value = 1\n")
    symlink = source_root / "escaped.py"
    try:
        symlink.symlink_to(outside_source)
    except OSError as exc:
        pytest.skip(f"当前平台不能创建符号链接：{exc}")

    with pytest.raises(ImportGraphScanError) as caught:
        scan_import_graph(source_root, repository_root=tmp_path)

    assert caught.value.path == "src/demo/escaped.py"
    assert caught.value.line == 0
    assert "源码根目录外" in caught.value.message


def test_duplicate_module_names_fail_with_both_source_paths(tmp_path: Path) -> None:
    source_root = tmp_path / "src" / "demo"
    _write(source_root / "__init__.py")
    _write(source_root / "foo.py")
    _write(source_root / "foo" / "__init__.py")

    with pytest.raises(ImportGraphScanError) as caught:
        scan_import_graph(source_root, repository_root=tmp_path)

    assert caught.value.path == "src/demo/foo/__init__.py"
    assert caught.value.line == 0
    assert "demo.foo" in caught.value.message
    assert "src/demo/foo.py" in caught.value.message


def test_classifies_absolute_relative_type_checking_and_local_imports(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "src" / "demo"
    for name in ("model.py", "contracts.py", "typing_dep.py", "local_dep.py"):
        _write(source_root / name)
    _write(source_root / "__init__.py")
    _write(
        source_root / "consumer.py",
        """\
import demo.model as model
from . import contracts
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .typing_dep import TypeOnly

def load() -> None:
    from . import local_dep
""",
    )

    report = scan_import_graph(source_root, repository_root=tmp_path)

    assert [
        (item.source, item.target, item.scope, item.kind)
        for item in report.imports
    ] == [
        ("demo.consumer", "demo.contracts", ImportScope.IMPORT_TIME, ImportKind.FROM),
        ("demo.consumer", "demo.model", ImportScope.IMPORT_TIME, ImportKind.IMPORT),
        ("demo.consumer", "demo.local_dep", ImportScope.LOCAL, ImportKind.FROM),
        (
            "demo.consumer",
            "demo.typing_dep",
            ImportScope.TYPE_CHECKING,
            ImportKind.FROM,
        ),
    ]


def test_type_checking_guard_aliases_remain_type_only(tmp_path: Path) -> None:
    source_root = tmp_path / "src" / "demo"
    for name in ("standard.py", "aliased.py", "qualified.py", "combined.py"):
        _write(source_root / name)
    _write(source_root / "__init__.py")
    _write(
        source_root / "consumer.py",
        """\
import typing as type_api
from typing import TYPE_CHECKING, TYPE_CHECKING as TC

if TYPE_CHECKING:
    import demo.standard
if TC:
    import demo.aliased
if type_api.TYPE_CHECKING:
    import demo.qualified
if TC and feature_enabled:
    import demo.combined
""",
    )

    report = scan_import_graph(source_root, repository_root=tmp_path)

    assert [(item.target, item.scope) for item in report.imports] == [
        ("demo.aliased", ImportScope.TYPE_CHECKING),
        ("demo.combined", ImportScope.TYPE_CHECKING),
        ("demo.qualified", ImportScope.TYPE_CHECKING),
        ("demo.standard", ImportScope.TYPE_CHECKING),
    ]


def test_scope_classification_respects_function_evaluation_boundaries(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "src" / "demo"
    for name in ("decorator.py", "default.py", "body.py", "lambda_dep.py"):
        _write(source_root / name)
    _write(source_root / "__init__.py")
    _write(
        source_root / "consumer.py",
        """\
import importlib

@importlib.import_module("demo.decorator").decorate
def run(value=importlib.import_module("demo.default")) -> None:
    import demo.body

load_later = lambda: importlib.import_module("demo.lambda_dep")
""",
    )

    report = scan_import_graph(source_root, repository_root=tmp_path)

    assert [
        (item.target, item.scope)
        for item in report.imports
    ] == [
        ("demo.decorator", ImportScope.IMPORT_TIME),
        ("demo.default", ImportScope.IMPORT_TIME),
        ("demo.body", ImportScope.LOCAL),
        ("demo.lambda_dep", ImportScope.LOCAL),
    ]


def test_collects_literal_dynamic_imports_and_reports_non_literal_calls(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "src" / "demo"
    _write(source_root / "__init__.py")
    _write(source_root / "plugin.py")
    _write(
        source_root / "loader.py",
        """\
import importlib as loader
from importlib import import_module as load_module

loader.import_module("demo.plugin")
__import__("demo.plugin")
loader.import_module("demo.missing")
load_module(dynamic_name)

def load_later(name: str) -> None:
    __import__(name)
""",
    )

    report = scan_import_graph(source_root, repository_root=tmp_path)

    assert [
        (item.target, item.scope, item.kind)
        for item in report.imports
    ] == [
        ("demo.plugin", ImportScope.IMPORT_TIME, ImportKind.DYNAMIC),
        ("demo.plugin", ImportScope.IMPORT_TIME, ImportKind.DYNAMIC),
    ]
    assert [
        (item.operation, item.requested, item.scope, item.line, item.reason)
        for item in report.unresolved
    ] == [
        (
            "import_module",
            "demo.missing",
            ImportScope.IMPORT_TIME,
            6,
            "dynamic target does not resolve to a discovered module",
        ),
        (
            "import_module",
            "",
            ImportScope.IMPORT_TIME,
            7,
            "dynamic target is not a string literal",
        ),
        (
            "__import__",
            "",
            ImportScope.LOCAL,
            10,
            "dynamic target is not a string literal",
        ),
    ]


def test_dynamic_import_aliases_follow_python_lexical_scopes(tmp_path: Path) -> None:
    source_root = tmp_path / "src" / "demo"
    targets = (
        "class_visible",
        "function_direct_visible",
        "function_imported_visible",
        "module_method_visible",
        "module_visible",
        "function_direct_leak",
        "function_imported_leak",
        "class_method_leak",
        "class_leak",
        "unbound_importlib",
    )
    _write(source_root / "__init__.py")
    for target in targets:
        _write(source_root / f"{target}.py")
    _write(
        source_root / "consumer.py",
        """\
import importlib as module_loader

def use_module_alias() -> None:
    module_loader.import_module("demo.module_visible")

def define_function_aliases() -> None:
    import importlib as function_loader
    from importlib import import_module as function_import
    function_loader.import_module("demo.function_direct_visible")
    function_import("demo.function_imported_visible")

function_loader.import_module("demo.function_direct_leak")
function_import("demo.function_imported_leak")

class Consumer:
    import importlib as class_loader
    class_loader.import_module("demo.class_visible")

    def load(self) -> None:
        module_loader.import_module("demo.module_method_visible")
        class_loader.import_module("demo.class_method_leak")

class_loader.import_module("demo.class_leak")
""",
    )
    _write(
        source_root / "unbound.py",
        'importlib.import_module("demo.unbound_importlib")\n',
    )

    report = scan_import_graph(source_root, repository_root=tmp_path)

    assert [
        (item.target, item.scope)
        for item in report.imports
        if item.kind is ImportKind.DYNAMIC
    ] == [
        ("demo.class_visible", ImportScope.IMPORT_TIME),
        ("demo.function_direct_visible", ImportScope.LOCAL),
        ("demo.function_imported_visible", ImportScope.LOCAL),
        ("demo.module_method_visible", ImportScope.LOCAL),
        ("demo.module_visible", ImportScope.LOCAL),
    ]


def test_dynamic_alias_binding_order_and_shadowing_match_python(tmp_path: Path) -> None:
    source_root = tmp_path / "src" / "demo"
    targets = (
        "after_module_rebind",
        "before_module_rebind",
        "function_after_assign",
        "function_before_assign",
        "late_global",
        "local_rebound",
        "local_visible",
        "nested_late",
        "parameter_shadow",
    )
    _write(source_root / "__init__.py")
    for target in targets:
        _write(source_root / f"{target}.py")
    _write(
        source_root / "consumer.py",
        """\
def defined_before_import() -> None:
    late_loader("demo.late_global")

from importlib import import_module as late_loader
import importlib as module_loader

module_loader.import_module("demo.before_module_rebind")
module_loader = object()
module_loader.import_module("demo.after_module_rebind")

def parameter_shadow(module_loader: object) -> None:
    module_loader.import_module("demo.parameter_shadow")

def assignment_shadow() -> None:
    module_loader.import_module("demo.function_before_assign")
    module_loader = object()
    module_loader.import_module("demo.function_after_assign")

def local_alias() -> None:
    import importlib as local_loader
    local_loader.import_module("demo.local_visible")
    local_loader = object()
    local_loader.import_module("demo.local_rebound")

def outer() -> object:
    def inner() -> None:
        nested_loader.import_module("demo.nested_late")
    import importlib as nested_loader
    return inner
""",
    )

    report = scan_import_graph(source_root, repository_root=tmp_path)

    assert [
        (item.target, item.scope)
        for item in report.imports
        if item.kind is ImportKind.DYNAMIC
    ] == [
        ("demo.before_module_rebind", ImportScope.IMPORT_TIME),
        ("demo.late_global", ImportScope.LOCAL),
        ("demo.local_visible", ImportScope.LOCAL),
        ("demo.nested_late", ImportScope.LOCAL),
    ]


def test_type_checking_branch_aliases_do_not_leak_and_rebinding_invalidates_guard(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "src" / "demo"
    for target in ("branch_leak", "inside_branch", "rebound_guard"):
        _write(source_root / f"{target}.py")
    _write(source_root / "__init__.py")
    _write(
        source_root / "consumer.py",
        """\
from typing import TYPE_CHECKING as TC

if TC:
    import importlib as guarded_loader
    guarded_loader.import_module("demo.inside_branch")

guarded_loader.import_module("demo.branch_leak")
TC = False
if TC:
    import demo.rebound_guard
""",
    )

    report = scan_import_graph(source_root, repository_root=tmp_path)

    assert [
        (item.target, item.scope)
        for item in report.imports
    ] == [
        ("demo.rebound_guard", ImportScope.IMPORT_TIME),
        ("demo.inside_branch", ImportScope.TYPE_CHECKING),
    ]


def test_unknown_internal_static_targets_are_diagnostics_not_ancestor_edges(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "src" / "demo"
    _write(source_root / "__init__.py")
    _write(source_root / "existing.py")
    _write(
        source_root / "consumer.py",
        """\
import demo.existing
import demo.missing
from demo.existing import Symbol
from demo import existing
from demo.missing import Missing
""",
    )

    report = scan_import_graph(source_root, repository_root=tmp_path)

    assert [(item.target, item.requested) for item in report.imports] == [
        ("demo.existing", "demo.existing.Symbol"),
        ("demo.existing", "demo.existing"),
        ("demo.existing", "demo.existing"),
    ]
    assert [
        (item.operation, item.requested, item.line, item.reason)
        for item in report.unresolved
    ] == [
        (
            "import",
            "demo.missing",
            2,
            "static target does not resolve to a discovered module",
        ),
        (
            "from",
            "demo.missing",
            5,
            "static target does not resolve to a discovered module",
        ),
    ]
    assert report.graph(GraphScope.ALL_STATIC).edges == (
        DependencyEdge("demo.consumer", "demo.existing"),
    )


def test_scc_views_separate_import_time_typing_and_all_static_dependencies(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "src" / "demo"
    _write(source_root / "__init__.py")
    _write(source_root / "a.py", "import demo.b\nimport demo.b\n")
    _write(
        source_root / "b.py",
        """\
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    import demo.a
def load() -> None:
    import demo.c
""",
    )
    _write(source_root / "c.py", "import demo.b\n")
    _write(source_root / "d.py", "import demo.e\n")
    _write(source_root / "e.py", "import demo.d\n")

    report = scan_import_graph(source_root, repository_root=tmp_path)

    assert {
        scope: [component.modules for component in report.graph(scope).sccs]
        for scope in GraphScope
    } == {
        GraphScope.IMPORT_TIME: [("demo.d", "demo.e")],
        GraphScope.TYPING: [("demo.a", "demo.b"), ("demo.d", "demo.e")],
        GraphScope.ALL_STATIC: [
            ("demo.a", "demo.b", "demo.c"),
            ("demo.d", "demo.e"),
        ],
    }
    all_static = report.graph(GraphScope.ALL_STATIC)
    assert len(all_static.edges) == 6
    assert all_static.hotspots[0].module == "demo.b"
    assert (all_static.hotspots[0].incoming, all_static.hotspots[0].outgoing) == (2, 2)


def test_malformed_python_raises_path_and_line_aware_scanner_error(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "src" / "demo"
    _write(source_root / "__init__.py")
    _write(source_root / "broken.py", "value = (\n")

    with pytest.raises(ImportGraphScanError) as caught:
        scan_import_graph(source_root, repository_root=tmp_path)

    assert caught.value.path == "src/demo/broken.py"
    assert caught.value.line == 1
    assert caught.value.column > 0
    assert "src/demo/broken.py:1" in str(caught.value)


def test_scanner_honors_python_source_encoding_cookie(tmp_path: Path) -> None:
    source_root = tmp_path / "src" / "demo"
    _write(source_root / "__init__.py")
    _write(source_root / "target.py")
    _write_bytes(
        source_root / "latin1.py",
        "# -*- coding: latin-1 -*-\nlabel = 'café'\nimport demo.target\n".encode(
            "latin-1"
        ),
    )

    report = scan_import_graph(source_root, repository_root=tmp_path)

    assert DependencyEdge("demo.latin1", "demo.target") in report.graph(
        GraphScope.IMPORT_TIME
    ).edges


def test_canonical_json_is_stable_and_digest_excludes_digest_field(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "src" / "demo"
    _write(source_root / "__init__.py")
    _write(source_root / "b.py")
    _write(source_root / "a.py", "import demo.b\n")

    first = scan_import_graph(source_root, repository_root=tmp_path)
    second = scan_import_graph(source_root, repository_root=tmp_path)

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
    assert payload["source_root"] == "src/demo"
    assert payload["modules"][0]["path"] == "src/demo/__init__.py"
    assert "timestamp" not in payload


def test_real_repository_generations_have_identical_digest_and_json() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    source_root = repository_root / "src" / "naumi_agent"

    first = scan_import_graph(source_root, repository_root=repository_root)
    second = scan_import_graph(source_root, repository_root=repository_root)

    assert first.digest == second.digest
    assert first.canonical_json() == second.canonical_json()
    assert len(first.modules) > 300
    assert first.graph(GraphScope.ALL_STATIC).edges
    assert any(
        hotspot.module == "naumi_agent.orchestrator.engine"
        for hotspot in first.graph(GraphScope.ALL_STATIC).hotspots[:10]
    )


def test_cli_real_smoke_writes_full_report_and_compact_baseline(
    tmp_path: Path,
) -> None:
    repository_root = Path(__file__).resolve().parents[2]
    report_path = tmp_path / "report.json"
    baseline_path = tmp_path / "baseline.json"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "naumi_agent.architecture.import_graph",
            "--source-root",
            "src/naumi_agent",
            "--output",
            str(report_path),
            "--baseline-output",
            str(baseline_path),
            "--source-base",
            "test-base",
        ],
        cwd=repository_root,
        env={**os.environ, "PYTHONPATH": "src"},
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert "导入图扫描完成" in completed.stdout
    assert "模块" in completed.stdout
    assert "import_time SCC" in completed.stdout
    assert "typing SCC" in completed.stdout
    assert "all_static SCC" in completed.stdout
    report = json.loads(report_path.read_text(encoding="utf-8"))
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    assert report["digest"] == baseline["report_digest"]
    assert baseline["source_base"] == "test-base"
    assert baseline["source_root"] == "src/naumi_agent"
    assert baseline["summaries"]
    assert baseline["sccs"]
    assert baseline["top_hotspots"]
    assert "edges" not in baseline
    assert "imports" not in baseline
    assert "timestamp" not in baseline


def test_cli_rejects_using_one_path_for_full_report_and_baseline(
    tmp_path: Path,
) -> None:
    repository_root = Path(__file__).resolve().parents[2]
    source_root = tmp_path / "src" / "demo"
    _write(source_root / "__init__.py")
    output_path = tmp_path / "same.json"

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "naumi_agent.architecture.import_graph",
            "--source-root",
            str(source_root),
            "--output",
            str(output_path),
            "--baseline-output",
            str(output_path),
        ],
        cwd=repository_root,
        env={**os.environ, "PYTHONPATH": "src"},
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "不能相同" in completed.stderr
    assert not output_path.exists()


def test_git_source_base_returns_unknown_when_git_cannot_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing_git(*_args: object, **_kwargs: object) -> None:
        raise FileNotFoundError("git")

    monkeypatch.setattr(import_graph_module.subprocess, "run", missing_git)

    assert import_graph_module._git_source_base(tmp_path) == "unknown"
