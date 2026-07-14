"""Deterministic, side-effect-free Python dependency scanning."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import subprocess
import tempfile
import tokenize
from dataclasses import asdict, dataclass, field, replace
from enum import StrEnum
from pathlib import Path


class ImportGraphScanError(RuntimeError):
    """A source file could not be analyzed completely."""

    def __init__(self, *, path: str, line: int, column: int, message: str) -> None:
        self.path = path
        self.line = line
        self.column = column
        self.message = message
        super().__init__(f"无法扫描 Python 源码：{path}:{line}:{column}: {message}")


class ImportScope(StrEnum):
    """When an import can execute."""

    IMPORT_TIME = "import_time"
    LOCAL = "local"
    TYPE_CHECKING = "type_checking"


class ImportKind(StrEnum):
    """Syntax used to request a dependency."""

    IMPORT = "import"
    FROM = "from"
    DYNAMIC = "dynamic"


class GraphScope(StrEnum):
    """Dependency views with progressively broader execution scopes."""

    IMPORT_TIME = "import_time"
    TYPING = "typing"
    ALL_STATIC = "all_static"


@dataclass(frozen=True, slots=True)
class ModuleRecord:
    """One discovered Python module."""

    name: str
    path: str
    is_package: bool = False


@dataclass(frozen=True, slots=True)
class ImportRecord:
    """One statically located internal import occurrence."""

    source: str
    target: str
    requested: str
    scope: ImportScope
    kind: ImportKind
    path: str
    line: int
    column: int


@dataclass(frozen=True, slots=True)
class UnresolvedImport:
    """A recognizable import whose internal target cannot be inferred safely."""

    source: str
    operation: str
    requested: str
    scope: ImportScope
    path: str
    line: int
    column: int
    reason: str


@dataclass(frozen=True, slots=True, order=True)
class DependencyEdge:
    """One unique directed module dependency."""

    source: str
    target: str


@dataclass(frozen=True, slots=True)
class StronglyConnectedComponent:
    """One cyclic strongly connected component."""

    modules: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ModuleHotspot:
    """In/out degree for one module in a graph view."""

    module: str
    incoming: int
    outgoing: int

    @property
    def total(self) -> int:
        return self.incoming + self.outgoing


@dataclass(frozen=True, slots=True)
class DependencyGraph:
    """Unique edges and derived metrics for one dependency scope."""

    scope: GraphScope
    edges: tuple[DependencyEdge, ...]
    sccs: tuple[StronglyConnectedComponent, ...]
    hotspots: tuple[ModuleHotspot, ...]


@dataclass(frozen=True, slots=True)
class BaselineGraphSummary:
    """Compact counts for one graph scope."""

    scope: GraphScope
    module_count: int
    edge_count: int
    scc_count: int


@dataclass(frozen=True, slots=True)
class BaselineScc:
    """One cyclic component retained in the compact baseline."""

    scope: GraphScope
    modules: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BaselineHotspot:
    """One top-ranked hotspot retained in the compact baseline."""

    scope: GraphScope
    module: str
    incoming: int
    outgoing: int


@dataclass(frozen=True, slots=True)
class ImportGraphBaseline:
    """Reviewable summary tied to a source base and full report digest."""

    source_base: str
    source_root: str
    report_digest: str
    summaries: tuple[BaselineGraphSummary, ...]
    sccs: tuple[BaselineScc, ...]
    top_hotspots: tuple[BaselineHotspot, ...]
    schema_version: int = 1

    def canonical_json(self) -> str:
        return json.dumps(
            asdict(self),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )


@dataclass(frozen=True, slots=True)
class ImportGraphReport:
    """Typed result of one source-tree scan."""

    source_root: str
    modules: tuple[ModuleRecord, ...]
    imports: tuple[ImportRecord, ...] = ()
    unresolved: tuple[UnresolvedImport, ...] = ()
    graphs: tuple[DependencyGraph, ...] = ()
    schema_version: int = 1
    digest: str = ""

    def graph(self, scope: GraphScope) -> DependencyGraph:
        """Return the graph view for an exact scope."""
        for graph in self.graphs:
            if graph.scope is scope:
                return graph
        raise KeyError(scope)

    def canonical_json(self) -> str:
        """Serialize this report deterministically, including its digest."""
        return _canonical_json(self, include_digest=True)


_SCOPE_ORDER = {
    ImportScope.IMPORT_TIME: 0,
    ImportScope.LOCAL: 1,
    ImportScope.TYPE_CHECKING: 2,
}


@dataclass(slots=True)
class _ImportAliasState:
    importlib_names: set[str] = field(default_factory=set)
    import_module_names: set[str] = field(default_factory=set)
    typing_names: set[str] = field(default_factory=set)
    type_checking_names: set[str] = field(default_factory=set)

    def copy(self) -> _ImportAliasState:
        return _ImportAliasState(
            importlib_names=set(self.importlib_names),
            import_module_names=set(self.import_module_names),
            typing_names=set(self.typing_names),
            type_checking_names=set(self.type_checking_names),
        )

    def discard_bound_names(self, names: set[str]) -> None:
        """Forget aliases hidden by a Python binding operation."""
        self.importlib_names.difference_update(names)
        self.import_module_names.difference_update(names)
        self.typing_names.difference_update(names)
        self.type_checking_names.difference_update(names)


def _import_bound_name(alias: ast.alias, *, from_import: bool) -> str:
    if alias.asname:
        return alias.asname
    return alias.name if from_import else alias.name.partition(".")[0]


def _update_import_alias_state(
    state: _ImportAliasState,
    node: ast.Import | ast.ImportFrom,
) -> None:
    from_import = isinstance(node, ast.ImportFrom)
    bound_names = {
        _import_bound_name(alias, from_import=from_import)
        for alias in node.names
        if alias.name != "*"
    }
    state.discard_bound_names(bound_names)
    if isinstance(node, ast.Import):
        for alias in node.names:
            bound_name = _import_bound_name(alias, from_import=False)
            if alias.name == "importlib":
                state.importlib_names.add(bound_name)
            elif alias.name == "typing":
                state.typing_names.add(bound_name)
        return
    if node.level != 0:
        return
    if node.module == "importlib":
        for alias in node.names:
            if alias.name == "import_module":
                state.import_module_names.add(
                    _import_bound_name(alias, from_import=True)
                )
    elif node.module == "typing":
        for alias in node.names:
            if alias.name == "TYPE_CHECKING":
                state.type_checking_names.add(
                    _import_bound_name(alias, from_import=True)
                )


def _target_bound_names(target: ast.AST | None) -> set[str]:
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, (ast.List, ast.Tuple)):
        return {
            name
            for element in target.elts
            for name in _target_bound_names(element)
        }
    if isinstance(target, ast.Starred):
        return _target_bound_names(target.value)
    return set()


class _LocalBindingCollector(ast.NodeVisitor):
    """Collect names that Python makes local to one function scope."""

    def __init__(self) -> None:
        self.bound: set[str] = set()
        self.global_names: set[str] = set()
        self.nonlocal_names: set[str] = set()

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.bound.add(node.id)

    def visit_Import(self, node: ast.Import) -> None:
        self.bound.update(
            _import_bound_name(alias, from_import=False) for alias in node.names
        )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self.bound.update(
            _import_bound_name(alias, from_import=True)
            for alias in node.names
            if alias.name != "*"
        )

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.bound.add(node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.bound.add(node.name)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.bound.add(node.name)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return

    def visit_Global(self, node: ast.Global) -> None:
        self.global_names.update(node.names)

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        self.nonlocal_names.update(node.names)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name:
            self.bound.add(node.name)
        if node.type is not None:
            self.visit(node.type)
        for child in node.body:
            self.visit(child)

    def _visit_comprehension(
        self,
        generators: list[ast.comprehension],
        values: tuple[ast.expr, ...],
    ) -> None:
        for generator in generators:
            self.visit(generator.iter)
            for condition in generator.ifs:
                self.visit(condition)
        for value in values:
            self.visit(value)

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._visit_comprehension(node.generators, (node.elt,))

    def visit_SetComp(self, node: ast.SetComp) -> None:
        self._visit_comprehension(node.generators, (node.elt,))

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self._visit_comprehension(node.generators, (node.elt,))

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self._visit_comprehension(node.generators, (node.key, node.value))


def _function_local_bindings(
    arguments: ast.arguments,
    body: tuple[ast.expr] | list[ast.stmt],
) -> set[str]:
    collector = _LocalBindingCollector()
    collector.bound.update(argument.arg for argument in arguments.posonlyargs)
    collector.bound.update(argument.arg for argument in arguments.args)
    collector.bound.update(argument.arg for argument in arguments.kwonlyargs)
    if arguments.vararg is not None:
        collector.bound.add(arguments.vararg.arg)
    if arguments.kwarg is not None:
        collector.bound.add(arguments.kwarg.arg)
    for child in body:
        collector.visit(child)
    return collector.bound - collector.global_names - collector.nonlocal_names


def _final_aliases_after_scope(
    body: list[ast.stmt],
    initial: _ImportAliasState,
) -> _ImportAliasState:
    """Approximate aliases visible after one module or function scope finishes."""
    state = initial.copy()
    for statement in body:
        if isinstance(statement, (ast.Import, ast.ImportFrom)):
            _update_import_alias_state(state, statement)
            continue
        collector = _LocalBindingCollector()
        collector.visit(statement)
        state.discard_bound_names(
            collector.bound - collector.global_names - collector.nonlocal_names
        )
    return state


def _module_final_aliases(body: list[ast.stmt]) -> _ImportAliasState:
    """Approximate globals available when a defined function is later called."""
    return _final_aliases_after_scope(body, _ImportAliasState())


class _ImportCollector(ast.NodeVisitor):
    def __init__(
        self,
        *,
        module: ModuleRecord,
        package_name: str,
        known_modules: frozenset[str],
        lexical_aliases: _ImportAliasState,
    ) -> None:
        self.module = module
        self.package_name = package_name
        self.known_modules = known_modules
        self.imports: list[ImportRecord] = []
        self.unresolved: list[UnresolvedImport] = []
        self._function_depth = 0
        self._type_checking_depth = 0
        self._aliases = _ImportAliasState()
        self._lexical_aliases = lexical_aliases

    @property
    def _scope(self) -> ImportScope:
        if self._type_checking_depth:
            return ImportScope.TYPE_CHECKING
        if self._function_depth:
            return ImportScope.LOCAL
        return ImportScope.IMPORT_TIME

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)
        self._aliases.discard_bound_names({node.name})

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)
        self._aliases.discard_bound_names({node.name})

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        self.visit(node.args)
        if node.returns is not None:
            self.visit(node.returns)
        for type_parameter in getattr(node, "type_params", ()):
            self.visit(type_parameter)
        self._visit_function_body(
            node.body,
            local_bindings=_function_local_bindings(node.args, node.body),
        )

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self.visit(node.args)
        self._visit_function_body(
            (node.body,),
            local_bindings=_function_local_bindings(node.args, (node.body,)),
        )

    def _visit_function_body(
        self,
        body: tuple[ast.expr] | list[ast.stmt],
        *,
        local_bindings: set[str],
    ) -> None:
        previous_aliases = self._aliases
        previous_lexical_aliases = self._lexical_aliases
        function_aliases = self._lexical_aliases.copy()
        function_aliases.discard_bound_names(local_bindings)
        self._aliases = function_aliases
        self._lexical_aliases = _final_aliases_after_scope(
            list(body),
            function_aliases,
        )
        self._function_depth += 1
        try:
            for child in body:
                self.visit(child)
        finally:
            self._function_depth -= 1
            self._aliases = previous_aliases
            self._lexical_aliases = previous_lexical_aliases

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        for base in node.bases:
            self.visit(base)
        for keyword in node.keywords:
            self.visit(keyword)
        for type_parameter in getattr(node, "type_params", ()):
            self.visit(type_parameter)

        previous_aliases = self._aliases
        self._aliases = previous_aliases.copy()
        try:
            for child in node.body:
                self.visit(child)
        finally:
            self._aliases = previous_aliases
        self._aliases.discard_bound_names({node.name})

    def visit_If(self, node: ast.If) -> None:
        if self._is_type_checking_guard(node.test):
            self.visit(node.test)
            previous_aliases = self._aliases
            previous_lexical_aliases = self._lexical_aliases
            branch_aliases = previous_aliases.copy()
            self._aliases = branch_aliases
            self._lexical_aliases = branch_aliases
            self._type_checking_depth += 1
            try:
                for child in node.body:
                    self.visit(child)
            finally:
                self._type_checking_depth -= 1
                self._aliases = previous_aliases
                self._lexical_aliases = previous_lexical_aliases
            for child in node.orelse:
                self.visit(child)
            return
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        _update_import_alias_state(self._aliases, node)
        for alias in node.names:
            if alias.name in self.known_modules:
                self._append(node, alias.name, alias.name, ImportKind.IMPORT)
            elif self._is_internal_name(alias.name):
                self._append_unresolved(
                    node,
                    operation="import",
                    requested=alias.name,
                    reason="static target does not resolve to a discovered module",
                )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        _update_import_alias_state(self._aliases, node)
        base = self._resolve_from_base(node)
        if not base:
            return
        for alias in node.names:
            candidate = base if alias.name == "*" else f"{base}.{alias.name}"
            if candidate in self.known_modules:
                self._append(node, candidate, candidate, ImportKind.FROM)
            elif base in self.known_modules:
                self._append(node, base, candidate, ImportKind.FROM)
            elif self._is_internal_name(base):
                self._append_unresolved(
                    node,
                    operation="from",
                    requested=base,
                    reason="static target does not resolve to a discovered module",
                )

    def visit_Assign(self, node: ast.Assign) -> None:
        self.visit(node.value)
        for target in node.targets:
            self.visit(target)
        self._aliases.discard_bound_names(
            {name for target in node.targets for name in _target_bound_names(target)}
        )

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self.visit(node.annotation)
        if node.value is not None:
            self.visit(node.value)
        self.visit(node.target)
        self._aliases.discard_bound_names(_target_bound_names(node.target))

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self.visit(node.target)
        self.visit(node.value)
        self._aliases.discard_bound_names(_target_bound_names(node.target))

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self.visit(node.value)
        self.visit(node.target)
        self._aliases.discard_bound_names(_target_bound_names(node.target))

    def visit_Delete(self, node: ast.Delete) -> None:
        for target in node.targets:
            self.visit(target)
        self._aliases.discard_bound_names(
            {name for target in node.targets for name in _target_bound_names(target)}
        )

    def visit_For(self, node: ast.For) -> None:
        self.visit(node.iter)
        self.visit(node.target)
        self._aliases.discard_bound_names(_target_bound_names(node.target))
        for child in (*node.body, *node.orelse):
            self.visit(child)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self.visit_For(node)

    def visit_With(self, node: ast.With) -> None:
        for item in node.items:
            self.visit(item.context_expr)
            if item.optional_vars is not None:
                self.visit(item.optional_vars)
                self._aliases.discard_bound_names(
                    _target_bound_names(item.optional_vars)
                )
        for child in node.body:
            self.visit(child)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        self.visit_With(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.type is not None:
            self.visit(node.type)
        if node.name:
            self._aliases.discard_bound_names({node.name})
        for child in node.body:
            self.visit(child)

    def visit_Call(self, node: ast.Call) -> None:
        operation = self._dynamic_import_operation(node.func)
        if operation is not None:
            target_node = node.args[0] if node.args else None
            if target_node is None:
                for keyword in node.keywords:
                    if keyword.arg == "name":
                        target_node = keyword.value
                        break
            if isinstance(target_node, ast.Constant) and isinstance(target_node.value, str):
                requested = target_node.value
                if requested in self.known_modules:
                    self._append(node, requested, requested, ImportKind.DYNAMIC)
                elif requested == self.package_name or requested.startswith(
                    f"{self.package_name}."
                ):
                    self._append_unresolved(
                        node,
                        operation=operation,
                        requested=requested,
                        reason=(
                            "dynamic target does not resolve to a discovered module"
                        ),
                    )
            else:
                reason = (
                    "dynamic target is missing"
                    if target_node is None
                    else "dynamic target is not a string literal"
                )
                self._append_unresolved(
                    node,
                    operation=operation,
                    reason=reason,
                )
        self.generic_visit(node)

    def _append_unresolved(
        self,
        node: ast.Import | ast.ImportFrom | ast.Call,
        *,
        operation: str,
        reason: str,
        requested: str = "",
    ) -> None:
        self.unresolved.append(
            UnresolvedImport(
                source=self.module.name,
                operation=operation,
                requested=requested,
                scope=self._scope,
                path=self.module.path,
                line=node.lineno,
                column=node.col_offset + 1,
                reason=reason,
            )
        )

    def _is_internal_name(self, requested: str) -> bool:
        return requested == self.package_name or requested.startswith(
            f"{self.package_name}."
        )

    def _dynamic_import_operation(self, function: ast.expr) -> str | None:
        if isinstance(function, ast.Name):
            if function.id == "__import__":
                return "__import__"
            if function.id in self._aliases.import_module_names:
                return "import_module"
        if (
            isinstance(function, ast.Attribute)
            and function.attr == "import_module"
            and isinstance(function.value, ast.Name)
            and function.value.id in self._aliases.importlib_names
        ):
            return "import_module"
        return None

    def _is_type_checking_guard(self, node: ast.expr) -> bool:
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.And):
            return any(self._is_type_checking_guard(value) for value in node.values)
        if isinstance(node, ast.Name):
            return node.id in self._aliases.type_checking_names
        return (
            isinstance(node, ast.Attribute)
            and node.attr == "TYPE_CHECKING"
            and isinstance(node.value, ast.Name)
            and node.value.id in self._aliases.typing_names
        )

    def _resolve_from_base(self, node: ast.ImportFrom) -> str:
        if node.level == 0:
            return node.module or ""
        package = (
            self.module.name
            if self.module.is_package
            else self.module.name.rpartition(".")[0]
        )
        parts = package.split(".") if package else []
        remove = node.level - 1
        if remove > len(parts):
            return ""
        base_parts = parts[: len(parts) - remove]
        if node.module:
            base_parts.extend(node.module.split("."))
        return ".".join(base_parts)

    def _append(
        self,
        node: ast.Import | ast.ImportFrom | ast.Call,
        target: str,
        requested: str,
        kind: ImportKind,
    ) -> None:
        if not target.startswith(f"{self.package_name}.") and target != self.package_name:
            return
        self.imports.append(
            ImportRecord(
                source=self.module.name,
                target=target,
                requested=requested,
                scope=self._scope,
                kind=kind,
                path=self.module.path,
                line=node.lineno,
                column=node.col_offset + 1,
            )
        )


def _build_graphs(
    modules: tuple[ModuleRecord, ...],
    imports: tuple[ImportRecord, ...],
) -> tuple[DependencyGraph, ...]:
    accepted_scopes = {
        GraphScope.IMPORT_TIME: frozenset({ImportScope.IMPORT_TIME}),
        GraphScope.TYPING: frozenset(
            {ImportScope.IMPORT_TIME, ImportScope.TYPE_CHECKING}
        ),
        GraphScope.ALL_STATIC: frozenset(ImportScope),
    }
    module_names = tuple(module.name for module in modules)
    graphs: list[DependencyGraph] = []
    for graph_scope in GraphScope:
        edges = tuple(
            sorted(
                {
                    DependencyEdge(item.source, item.target)
                    for item in imports
                    if item.scope in accepted_scopes[graph_scope]
                }
            )
        )
        graphs.append(
            DependencyGraph(
                scope=graph_scope,
                edges=edges,
                sccs=_find_cyclic_components(module_names, edges),
                hotspots=_build_hotspots(module_names, edges),
            )
        )
    return tuple(graphs)


def _find_cyclic_components(
    modules: tuple[str, ...],
    edges: tuple[DependencyEdge, ...],
) -> tuple[StronglyConnectedComponent, ...]:
    adjacency = {module: set() for module in modules}
    for edge in edges:
        adjacency.setdefault(edge.source, set()).add(edge.target)
        adjacency.setdefault(edge.target, set())

    next_index = 0
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    components: list[tuple[str, ...]] = []

    def visit(module: str) -> None:
        nonlocal next_index
        indices[module] = next_index
        lowlinks[module] = next_index
        next_index += 1
        stack.append(module)
        on_stack.add(module)

        for target in sorted(adjacency[module]):
            if target not in indices:
                visit(target)
                lowlinks[module] = min(lowlinks[module], lowlinks[target])
            elif target in on_stack:
                lowlinks[module] = min(lowlinks[module], indices[target])

        if lowlinks[module] != indices[module]:
            return
        component: list[str] = []
        while True:
            member = stack.pop()
            on_stack.remove(member)
            component.append(member)
            if member == module:
                break
        ordered = tuple(sorted(component))
        if len(ordered) > 1 or (
            len(ordered) == 1 and ordered[0] in adjacency[ordered[0]]
        ):
            components.append(ordered)

    for module in sorted(adjacency):
        if module not in indices:
            visit(module)
    return tuple(
        StronglyConnectedComponent(modules=component)
        for component in sorted(components)
    )


def _build_hotspots(
    modules: tuple[str, ...],
    edges: tuple[DependencyEdge, ...],
) -> tuple[ModuleHotspot, ...]:
    incoming = dict.fromkeys(modules, 0)
    outgoing = dict.fromkeys(modules, 0)
    for edge in edges:
        outgoing[edge.source] = outgoing.get(edge.source, 0) + 1
        incoming[edge.target] = incoming.get(edge.target, 0) + 1
    hotspots = [
        ModuleHotspot(
            module=module,
            incoming=incoming.get(module, 0),
            outgoing=outgoing.get(module, 0),
        )
        for module in sorted(set(incoming) | set(outgoing))
    ]
    hotspots.sort(key=lambda item: (-item.total, -item.incoming, item.module))
    return tuple(hotspots)


def _canonical_json(report: ImportGraphReport, *, include_digest: bool) -> str:
    payload = asdict(report)
    if not include_digest:
        payload.pop("digest", None)
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _report_digest(report: ImportGraphReport) -> str:
    content = _canonical_json(report, include_digest=False).encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def build_baseline_summary(
    report: ImportGraphReport,
    *,
    source_base: str,
    hotspot_limit: int = 10,
) -> ImportGraphBaseline:
    """Build a compact baseline without copying import occurrences or edges."""
    if hotspot_limit < 0:
        raise ValueError("hotspot_limit 不能小于 0")
    summaries = tuple(
        BaselineGraphSummary(
            scope=graph.scope,
            module_count=len(report.modules),
            edge_count=len(graph.edges),
            scc_count=len(graph.sccs),
        )
        for graph in report.graphs
    )
    sccs = tuple(
        BaselineScc(scope=graph.scope, modules=component.modules)
        for graph in report.graphs
        for component in graph.sccs
    )
    top_hotspots = tuple(
        BaselineHotspot(
            scope=graph.scope,
            module=hotspot.module,
            incoming=hotspot.incoming,
            outgoing=hotspot.outgoing,
        )
        for graph in report.graphs
        for hotspot in graph.hotspots[:hotspot_limit]
    )
    return ImportGraphBaseline(
        source_base=source_base,
        source_root=report.source_root,
        report_digest=report.digest,
        summaries=summaries,
        sccs=sccs,
        top_hotspots=top_hotspots,
    )


def scan_import_graph(
    source_root: str | Path,
    *,
    repository_root: str | Path | None = None,
) -> ImportGraphReport:
    """Discover Python modules without importing application code."""
    root = Path(source_root).resolve()
    repo = (
        Path(repository_root).resolve()
        if repository_root is not None
        else _infer_repository_root(root)
    )
    root_display = _display_path(root, repo)
    if not root.exists():
        raise ImportGraphScanError(
            path=root_display,
            line=0,
            column=0,
            message="源码根目录不存在",
        )
    if not root.is_dir():
        raise ImportGraphScanError(
            path=root_display,
            line=0,
            column=0,
            message="源码根目录不是目录",
        )
    try:
        root.relative_to(repo)
    except ValueError as exc:
        raise ImportGraphScanError(
            path=root_display,
            line=0,
            column=0,
            message="源码根目录必须位于仓库目录内",
        ) from exc
    package_name = root.name
    modules: list[ModuleRecord] = []
    module_paths: dict[str, str] = {}
    for path in sorted(root.rglob("*.py"), key=lambda item: item.as_posix()):
        module_path = path.relative_to(repo).as_posix()
        try:
            path.resolve().relative_to(root)
        except (OSError, ValueError) as exc:
            raise ImportGraphScanError(
                path=module_path,
                line=0,
                column=0,
                message="源码文件解析后位于源码根目录外",
            ) from exc
        relative_module = path.relative_to(root).with_suffix("")
        parts = list(relative_module.parts)
        if parts[-1] == "__init__":
            parts.pop()
        name_parts = [package_name, *parts]
        module_name = ".".join(name_parts)
        previous_path = module_paths.get(module_name)
        if previous_path is not None:
            raise ImportGraphScanError(
                path=module_path,
                line=0,
                column=0,
                message=(
                    f"模块名 {module_name} 同时映射到 {previous_path} 和 {module_path}"
                ),
            )
        module_paths[module_name] = module_path
        modules.append(
            ModuleRecord(
                name=module_name,
                path=module_path,
                is_package=path.name == "__init__.py",
            )
        )
    modules.sort(key=lambda module: (module.name, module.path))
    known_modules = frozenset(module.name for module in modules)
    imports: list[ImportRecord] = []
    unresolved: list[UnresolvedImport] = []
    for module in modules:
        path = repo / module.path
        try:
            with tokenize.open(path) as source_file:
                source = source_file.read()
            tree = ast.parse(source, filename=module.path)
        except (SyntaxError, UnicodeDecodeError) as exc:
            raise ImportGraphScanError(
                path=module.path,
                line=getattr(exc, "lineno", 0) or 0,
                column=getattr(exc, "offset", 0) or 0,
                message=getattr(exc, "msg", str(exc)),
            ) from exc
        collector = _ImportCollector(
            module=module,
            package_name=package_name,
            known_modules=known_modules,
            lexical_aliases=_module_final_aliases(tree.body),
        )
        collector.visit(tree)
        imports.extend(collector.imports)
        unresolved.extend(collector.unresolved)
    imports.sort(
        key=lambda item: (
            item.source,
            _SCOPE_ORDER[item.scope],
            item.target,
            item.kind,
            item.path,
            item.line,
            item.column,
        )
    )
    unresolved.sort(
        key=lambda item: (
            item.source,
            _SCOPE_ORDER[item.scope],
            item.path,
            item.line,
            item.column,
            item.operation,
        )
    )
    module_tuple = tuple(modules)
    import_tuple = tuple(imports)
    report = ImportGraphReport(
        source_root=root.relative_to(repo).as_posix(),
        modules=module_tuple,
        imports=import_tuple,
        unresolved=tuple(unresolved),
        graphs=_build_graphs(module_tuple, import_tuple),
    )
    return replace(report, digest=_report_digest(report))


def _display_path(path: Path, repository_root: Path) -> str:
    try:
        return path.relative_to(repository_root).as_posix()
    except ValueError:
        return path.as_posix()


def _infer_repository_root(source_root: Path) -> Path:
    if source_root.parent.name == "src":
        return source_root.parent.parent
    return source_root.parent


def _git_source_base(repository_root: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repository_root), "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return "unknown"
    value = completed.stdout.strip()
    return value if completed.returncode == 0 and value else "unknown"


def write_utf8_json(path: Path, content: str) -> None:
    """Atomically write canonical UTF-8 JSON with a trailing newline."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(f"{content}\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    """Run a real source-tree scan and optionally emit a compact baseline."""
    parser = argparse.ArgumentParser(description="生成 NaumiAgent Python 导入依赖图")
    parser.add_argument("--source-root", required=True, help="Python 包源码根目录")
    parser.add_argument("--output", required=True, help="完整 JSON 报告输出路径")
    parser.add_argument("--baseline-output", help="紧凑 baseline JSON 输出路径")
    parser.add_argument("--source-base", help="baseline 对应的源代码 commit/base")
    args = parser.parse_args(argv)

    source_root = Path(args.source_root).resolve()
    repository_root = _infer_repository_root(source_root)
    output_path = Path(args.output).expanduser().resolve()
    baseline_output_path = (
        Path(args.baseline_output).expanduser().resolve()
        if args.baseline_output
        else None
    )
    if baseline_output_path is not None and baseline_output_path == output_path:
        parser.error("完整报告与 baseline 的输出路径不能相同")
    try:
        report = scan_import_graph(source_root, repository_root=repository_root)
    except ImportGraphScanError as exc:
        parser.exit(2, f"{exc}\n")
    write_utf8_json(output_path, report.canonical_json())

    if baseline_output_path is not None:
        source_base = args.source_base or _git_source_base(repository_root)
        baseline = build_baseline_summary(report, source_base=source_base)
        write_utf8_json(baseline_output_path, baseline.canonical_json())

    all_static = report.graph(GraphScope.ALL_STATIC)
    import_time = report.graph(GraphScope.IMPORT_TIME)
    typing_graph = report.graph(GraphScope.TYPING)
    print(
        "导入图扫描完成："
        f"{len(report.modules)} 个模块，"
        f"{len(all_static.edges)} 条唯一依赖，"
        f"{len(import_time.sccs)} 个 import_time SCC，"
        f"{len(typing_graph.sccs)} 个 typing SCC，"
        f"{len(all_static.sccs)} 个 all_static SCC。"
    )
    return 0


__all__ = [
    "BaselineGraphSummary",
    "BaselineHotspot",
    "BaselineScc",
    "DependencyEdge",
    "DependencyGraph",
    "GraphScope",
    "ImportGraphBaseline",
    "ImportGraphReport",
    "ImportGraphScanError",
    "ImportKind",
    "ImportRecord",
    "ImportScope",
    "ModuleHotspot",
    "ModuleRecord",
    "StronglyConnectedComponent",
    "UnresolvedImport",
    "build_baseline_summary",
    "main",
    "scan_import_graph",
    "write_utf8_json",
]


if __name__ == "__main__":
    raise SystemExit(main())
