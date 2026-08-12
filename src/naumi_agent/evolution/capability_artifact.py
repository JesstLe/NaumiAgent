"""Sealed implementation artifacts and fail-closed Sandbox admission previews."""

from __future__ import annotations

import ast
import asyncio
import hashlib
import hmac
import json
import re
from collections.abc import Callable, Sequence
from pathlib import Path, PureWindowsPath
from typing import Any, Literal

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.capability_governance import (
    CapabilityGovernanceView,
    EvolutionCapabilityGovernanceService,
)
from naumi_agent.evolution.capability_specification import (
    EvolutionCapabilitySpecification,
    EvolutionCapabilitySpecificationService,
)

_POLICY_VERSION = "evolution-capability-artifact-v1"
_MAX_SOURCE_BYTES = 256 * 1024
_CLASS_RE = re.compile(r"^[A-Z][A-Za-z0-9]{0,127}$")


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class CapabilityArtifactCheck(_StrictModel):
    code: Literal[
        "approved_source_binding",
        "workspace_source",
        "tool_subclass",
        "interface_match",
        "entrypoint_shape",
        "import_time_safety",
        "builtin_conflict",
        "temporary_namespace",
    ]
    passed: bool
    hard_block: Literal[True] = True
    detail: str = Field(min_length=1, max_length=500)


class EvolutionCapabilityImplementationArtifact(_StrictModel):
    """Content-addressed source snapshot; this is not executable authority."""

    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-capability-artifact-v1"] = _POLICY_VERSION
    artifact_id: str = Field(pattern=r"^evcia_[0-9a-f]{24}$")
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_id: str = Field(pattern=r"^evc_[0-9a-f]{24}$")
    specification_id: str = Field(pattern=r"^evcs_[0-9a-f]{24}$")
    specification_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    governance_decision_id: str = Field(pattern=r"^evcgd_[0-9a-f]{24}$")
    governance_decision_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_path: str = Field(min_length=1, max_length=1_024)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_text: str = Field(min_length=1, max_length=_MAX_SOURCE_BYTES)
    class_name: str = Field(pattern=r"^[A-Z][A-Za-z0-9]{0,127}$")
    declared_tool_name: str = Field(pattern=r"^[a-z][a-z0-9_.:-]{0,127}$")
    temporary_tool_name: str = Field(
        pattern=r"^evolution_sandbox:[0-9a-f]{12}:[a-z][a-z0-9_.:-]{0,127}$"
    )
    parameters_schema_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    permission_specification_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    verification_specification_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    checks: tuple[CapabilityArtifactCheck, ...] = Field(min_length=8, max_length=8)
    admission_ready: bool
    registry_state: Literal["preview_only"] = "preview_only"
    registration_authorized: Literal[False] = False
    shadow_authorized: Literal[False] = False
    executable: Literal[False] = False
    created_at: str = Field(min_length=20, max_length=64)

    @model_validator(mode="after")
    def _identity_and_authority_are_exact(self) -> EvolutionCapabilityImplementationArtifact:
        codes = tuple(item.code for item in self.checks)
        expected = (
            "approved_source_binding",
            "workspace_source",
            "tool_subclass",
            "interface_match",
            "entrypoint_shape",
            "import_time_safety",
            "builtin_conflict",
            "temporary_namespace",
        )
        if codes != expected:
            raise ValueError("Capability artifact checks 顺序或集合无效。")
        if self.admission_ready != all(item.passed for item in self.checks):
            raise ValueError("admission_ready 与机械 checks 不一致。")
        if hashlib.sha256(self.source_text.encode()).hexdigest() != self.source_sha256:
            raise ValueError("Capability artifact 源码摘要不一致。")
        payload = self.model_dump(
            mode="json", exclude={"artifact_id", "artifact_sha256"}
        )
        digest = _digest(payload)
        if not hmac.compare_digest(self.artifact_sha256, digest):
            raise ValueError("Capability artifact 摘要不一致。")
        if self.artifact_id != f"evcia_{digest[:24]}":
            raise ValueError("Capability artifact identity 不一致。")
        return self

    def canonical_json(self) -> str:
        return _canonical(self.model_dump(mode="json"))


class CapabilityArtifactView(_StrictModel):
    schema_version: Literal[1] = 1
    artifact: EvolutionCapabilityImplementationArtifact | None
    state: Literal["missing", "preview_ready", "blocked", "revoked"]
    source_current: bool
    governance_current: bool
    registration_authorized: Literal[False] = False
    shadow_authorized: Literal[False] = False
    executable: Literal[False] = False


class CapabilityArtifactError(RuntimeError):
    pass


class EvolutionCapabilityArtifactStore:
    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path).expanduser().resolve()
        self._schema_ready = False
        self._schema_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()

    async def latest(
        self, workspace_root: str | Path, specification_id: str
    ) -> EvolutionCapabilityImplementationArtifact | None:
        workspace = str(Path(workspace_root).expanduser().resolve(strict=True))
        if not self._db_path.is_file():
            return None
        await self._ensure_schema()
        try:
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                row = await (
                    await db.execute(
                        "SELECT payload_json, payload_sha256 FROM "
                        "evolution_capability_implementation_artifacts "
                        "WHERE workspace_root = ? AND specification_id = ? "
                        "ORDER BY rowid DESC LIMIT 1",
                        (workspace, specification_id),
                    )
                ).fetchone()
        except (aiosqlite.Error, OSError) as exc:
            raise CapabilityArtifactError("无法读取 Capability 实现制品。") from exc
        if row is None:
            return None
        return _restore_artifact(row["payload_json"], row["payload_sha256"])

    async def record(
        self,
        workspace_root: str | Path,
        artifact: EvolutionCapabilityImplementationArtifact,
    ) -> EvolutionCapabilityImplementationArtifact:
        workspace = str(Path(workspace_root).expanduser().resolve(strict=True))
        await self._ensure_schema()
        payload = artifact.canonical_json()
        payload_sha256 = hashlib.sha256(payload.encode()).hexdigest()
        try:
            async with self._write_lock, aiosqlite.connect(self._db_path) as db:
                await db.execute("BEGIN IMMEDIATE")
                await db.execute(
                    "INSERT OR IGNORE INTO evolution_capability_implementation_artifacts "
                    "(workspace_root, specification_id, artifact_id, source_sha256, "
                    "payload_json, payload_sha256, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        workspace,
                        artifact.specification_id,
                        artifact.artifact_id,
                        artifact.source_sha256,
                        payload,
                        payload_sha256,
                        artifact.created_at,
                    ),
                )
                row = await (
                    await db.execute(
                        "SELECT payload_json, payload_sha256 FROM "
                        "evolution_capability_implementation_artifacts "
                        "WHERE workspace_root = ? AND artifact_id = ?",
                        (workspace, artifact.artifact_id),
                    )
                ).fetchone()
                await db.commit()
        except (aiosqlite.Error, OSError) as exc:
            raise CapabilityArtifactError("无法保存 Capability 实现制品。") from exc
        if row is None:
            raise CapabilityArtifactError("Capability 实现制品未形成持久记录。")
        return _restore_artifact(row[0], row[1])

    async def _ensure_schema(self) -> None:
        if self._schema_ready:
            return
        async with self._schema_lock:
            if self._schema_ready:
                return
            self._db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                async with aiosqlite.connect(self._db_path) as db:
                    await db.executescript(_SCHEMA)
                    await db.commit()
                self._db_path.chmod(0o600)
            except (aiosqlite.Error, OSError) as exc:
                raise CapabilityArtifactError(
                    "无法初始化 Capability 实现制品 Store。"
                ) from exc
            self._schema_ready = True


class EvolutionCapabilityArtifactService:
    """Seal source and preview admission without importing or registering it."""

    def __init__(
        self,
        *,
        review_service: Any,
        specification_service: EvolutionCapabilitySpecificationService,
        governance_service: EvolutionCapabilityGovernanceService,
        store: EvolutionCapabilityArtifactStore,
        registered_tool_names: Callable[[], Sequence[str]],
        now: Callable[[], str],
    ) -> None:
        self.review_service = review_service
        self.specification_service = specification_service
        self.governance_service = governance_service
        self.store = store
        self.registered_tool_names = registered_tool_names
        self.now = now

    async def create(
        self,
        workspace_root: str | Path,
        *,
        candidate_id: str,
        source_path: str,
        class_name: str,
    ) -> CapabilityArtifactView:
        workspace = Path(workspace_root).expanduser().resolve(strict=True)
        proposal = await self._proposal(workspace, candidate_id)
        spec_view = await self.specification_service.inspect_capability_specification(
            workspace, proposal
        )
        specification = spec_view.specification
        if specification is None or specification.state != "complete":
            raise CapabilityArtifactError("Capability Specification 尚未完成。")
        governance = await self.governance_service.inspect(workspace, candidate_id)
        if not _governance_approved(governance, specification):
            raise CapabilityArtifactError("当前治理批准无效，拒绝创建实现制品。")
        artifact = build_capability_implementation_artifact(
            workspace,
            specification=specification,
            governance=governance,
            source_path=source_path,
            class_name=class_name,
            registered_tool_names=self.registered_tool_names(),
            created_at=self.now(),
        )
        stored = await self.store.record(workspace, artifact)
        return _artifact_view(stored, source_current=True, governance_current=True)

    async def inspect(
        self, workspace_root: str | Path, candidate_id: str
    ) -> CapabilityArtifactView:
        workspace = Path(workspace_root).expanduser().resolve(strict=True)
        proposal = await self._proposal(workspace, candidate_id)
        spec_view = await self.specification_service.inspect_capability_specification(
            workspace, proposal
        )
        artifact = await self.store.latest(workspace, spec_view.specification_id)
        if artifact is None:
            return CapabilityArtifactView(
                artifact=None,
                state="missing",
                source_current=False,
                governance_current=False,
            )
        specification = spec_view.specification
        governance = await self.governance_service.inspect(workspace, candidate_id)
        governance_current = bool(
            specification is not None
            and _governance_approved(governance, specification)
            and artifact.governance_decision_id == governance.decision.decision_id
            and artifact.governance_decision_sha256 == governance.decision.digest()
            and artifact.specification_sha256 == specification.digest()
        )
        source_current = _source_is_current(workspace, artifact)
        return _artifact_view(
            artifact,
            source_current=source_current,
            governance_current=governance_current,
        )

    async def _proposal(self, workspace: Path, candidate_id: str) -> Any:
        snapshot = await self.review_service.detail_snapshot(
            workspace, str(candidate_id).strip(), include_capability_extensions=False
        )
        proposal = (
            snapshot.selected.capability_proposal
            if snapshot.selected is not None
            else None
        )
        if proposal is None:
            raise CapabilityArtifactError("Candidate 当前没有有效 Capability Proposal。")
        return proposal


def build_capability_implementation_artifact(
    workspace_root: str | Path,
    *,
    specification: EvolutionCapabilitySpecification,
    governance: CapabilityGovernanceView,
    source_path: str,
    class_name: str,
    registered_tool_names: Sequence[str],
    created_at: str,
) -> EvolutionCapabilityImplementationArtifact:
    workspace = Path(workspace_root).expanduser().resolve(strict=True)
    if not _governance_approved(governance, specification):
        raise CapabilityArtifactError("治理 Decision 未绑定当前完整规格。")
    if _CLASS_RE.fullmatch(class_name) is None:
        raise CapabilityArtifactError("Capability class_name 格式无效。")
    relative, source = _read_workspace_source(workspace, source_path)
    interface = specification.interface
    if interface is None or specification.permissions is None or specification.verification is None:
        raise CapabilityArtifactError("Capability Specification 缺少接口、权限或验证契约。")
    try:
        tree = ast.parse(source, filename=relative)
    except SyntaxError as exc:
        raise CapabilityArtifactError(
            f"Capability 源码语法无效：第 {exc.lineno or 0} 行。"
        ) from exc
    target = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == class_name
        ),
        None,
    )
    subclass_ok = target is not None and any(_base_is_tool(base) for base in target.bases)
    declared_name, declared_schema, entrypoint_ok = _inspect_tool_class(target)
    interface_ok = bool(
        declared_name == interface.tool_name
        and declared_schema == interface.parameters_schema
    )
    import_safe = _import_time_safe(tree)
    names = tuple(str(name) for name in registered_tool_names)
    aliases = {alias for name in names for alias in _aliases(name)}
    builtin_ok = not bool(_aliases(interface.tool_name) & aliases)
    temporary_name = (
        f"evolution_sandbox:{specification.specification_id[5:17]}:"
        f"{interface.tool_name}"
    )
    namespace_ok = temporary_name not in names and not bool(_aliases(temporary_name) & aliases)
    checks = (
        _check("approved_source_binding", True, "绑定当前有效 approved Decision 与完整规格摘要。"),
        _check("workspace_source", True, "源码是工作区内非符号链接 UTF-8 Python 文件。"),
        _check("tool_subclass", subclass_ok, "目标类直接声明 Tool 基类。"),
        _check("interface_match", interface_ok, "静态 name/schema 与规格完全一致。"),
        _check(
            "entrypoint_shape",
            entrypoint_ok,
            "存在 description、parameters_schema 与 async execute。",
        ),
        _check("import_time_safety", import_safe, "模块顶层不包含可执行调用或控制流。"),
        _check("builtin_conflict", builtin_ok, "声明名称不与当前 Registry 名称或旧别名冲突。"),
        _check("temporary_namespace", namespace_ok, "临时名称唯一且不触发旧 namespace 归一化。"),
    )
    source_sha256 = hashlib.sha256(source.encode()).hexdigest()
    payload = {
        "schema_version": 1,
        "policy_version": _POLICY_VERSION,
        "candidate_id": specification.candidate_id,
        "specification_id": specification.specification_id,
        "specification_sha256": specification.digest(),
        "governance_decision_id": governance.decision.decision_id,
        "governance_decision_sha256": governance.decision.digest(),
        "source_path": relative,
        "source_sha256": source_sha256,
        "source_text": source,
        "class_name": class_name,
        "declared_tool_name": interface.tool_name,
        "temporary_tool_name": temporary_name,
        "parameters_schema_sha256": _digest(interface.parameters_schema),
        "permission_specification_sha256": _digest(
            specification.permissions.model_dump(mode="json")
        ),
        "verification_specification_sha256": _digest(
            specification.verification.model_dump(mode="json")
        ),
        "checks": [item.model_dump(mode="json") for item in checks],
        "admission_ready": all(item.passed for item in checks),
        "registry_state": "preview_only",
        "registration_authorized": False,
        "shadow_authorized": False,
        "executable": False,
        "created_at": created_at,
    }
    digest = _digest(payload)
    return EvolutionCapabilityImplementationArtifact(
        **payload,
        artifact_id=f"evcia_{digest[:24]}",
        artifact_sha256=digest,
    )


def render_capability_artifact(view: CapabilityArtifactView) -> str:
    if view.artifact is None:
        return "# Capability Sandbox 准入预检\n\n尚未创建实现制品；Registry、Shadow 与执行均关闭。"
    artifact = view.artifact
    lines = [
        "# Capability Sandbox 准入预检",
        "",
        f"- Artifact：`{artifact.artifact_id}`",
        f"- 状态：`{view.state}`",
        f"- 源码：`{artifact.source_path}` · `{artifact.source_sha256[:12]}`",
        f"- 临时名称：`{artifact.temporary_tool_name}`",
        f"- 当前源码：{'是' if view.source_current else '否'}",
        f"- 当前治理：{'是' if view.governance_current else '否'}",
        "- Registry 注册：否 · Shadow：否 · 可执行：否",
        "",
        "## 机械检查",
        "",
    ]
    lines.extend(
        f"- {'通过' if item.passed else '阻断'} · `{item.code}` · {item.detail}"
        for item in artifact.checks
    )
    lines.extend([
        "",
        "> 本结果只封存源码并预检准入；没有 import、注册或执行该代码。",
    ])
    return "\n".join(lines)


def _read_workspace_source(workspace: Path, raw: str) -> tuple[str, str]:
    if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
        raise CapabilityArtifactError("source_path 必须是有效工作区相对路径。")
    if Path(raw).is_absolute() or PureWindowsPath(raw).is_absolute():
        raise CapabilityArtifactError("source_path 不得是绝对路径。")
    native_parts = Path(raw).parts
    windows_parts = PureWindowsPath(raw).parts
    if ".." in native_parts or ".." in windows_parts:
        raise CapabilityArtifactError("source_path 不得包含父目录跳转。")
    lexical = workspace
    for part in native_parts:
        lexical = lexical / part
        if lexical.is_symlink():
            raise CapabilityArtifactError("源码路径不得经过符号链接。")
    candidate = workspace.joinpath(raw).resolve(strict=True)
    try:
        relative = candidate.relative_to(workspace).as_posix()
    except ValueError as exc:
        raise CapabilityArtifactError("source_path 逃逸工作区。") from exc
    if not candidate.is_file() or candidate.suffix != ".py":
        raise CapabilityArtifactError("源码必须是工作区内非符号链接 .py 文件。")
    size = candidate.stat().st_size
    if size < 1 or size > _MAX_SOURCE_BYTES:
        raise CapabilityArtifactError("Capability 源码大小必须在 1..256 KiB。")
    try:
        source = candidate.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise CapabilityArtifactError("Capability 源码必须是可读 UTF-8。") from exc
    return relative, source


def _inspect_tool_class(target: ast.ClassDef | None) -> tuple[str, Any, bool]:
    if target is None:
        return "", None, False
    methods = {
        node.name: node
        for node in target.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    name = _literal_return(methods.get("name"))
    schema = _literal_return(methods.get("parameters_schema"))
    description = _literal_return(methods.get("description"))
    execute = methods.get("execute")
    return (
        name if isinstance(name, str) else "",
        schema,
        bool(
            isinstance(description, str)
            and description.strip()
            and all(
                _is_property(methods.get(method))
                for method in ("name", "description", "parameters_schema")
            )
            and isinstance(execute, ast.AsyncFunctionDef)
        ),
    )


def _is_property(node: ast.AST | None) -> bool:
    return isinstance(node, ast.FunctionDef) and any(
        isinstance(decorator, ast.Name) and decorator.id == "property"
        for decorator in node.decorator_list
    )


def _literal_return(node: ast.AST | None) -> Any:
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return None
    returns = [item for item in node.body if isinstance(item, ast.Return)]
    if len(returns) != 1:
        return None
    try:
        return ast.literal_eval(returns[0].value)
    except (TypeError, ValueError):
        return None


def _base_is_tool(node: ast.expr) -> bool:
    return (isinstance(node, ast.Name) and node.id == "Tool") or (
        isinstance(node, ast.Attribute) and node.attr == "Tool"
    )


def _import_time_safe(tree: ast.Module) -> bool:
    safe = (ast.Import, ast.ImportFrom, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    for node in tree.body:
        if isinstance(node, safe):
            continue
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            if value is None:
                continue
            try:
                ast.literal_eval(value)
                continue
            except (TypeError, ValueError):
                return False
        return False
    return True


def _governance_approved(
    governance: CapabilityGovernanceView,
    specification: EvolutionCapabilitySpecification,
) -> bool:
    decision = governance.decision
    return bool(
        governance.state == "approved"
        and governance.decision_effective
        and governance.sandbox_design_eligible
        and not governance.registration_authorized
        and not governance.shadow_authorized
        and not governance.executable
        and decision is not None
        and decision.outcome == "approved"
        and decision.specification_id == specification.specification_id
        and decision.specification_sha256 == specification.digest()
    )


def _source_is_current(
    workspace: Path, artifact: EvolutionCapabilityImplementationArtifact
) -> bool:
    try:
        relative, source = _read_workspace_source(workspace, artifact.source_path)
    except CapabilityArtifactError:
        return False
    return relative == artifact.source_path and hmac.compare_digest(
        hashlib.sha256(source.encode()).hexdigest(), artifact.source_sha256
    )


def _artifact_view(
    artifact: EvolutionCapabilityImplementationArtifact,
    *,
    source_current: bool,
    governance_current: bool,
) -> CapabilityArtifactView:
    if not source_current or not governance_current:
        state = "revoked"
    elif artifact.admission_ready:
        state = "preview_ready"
    else:
        state = "blocked"
    return CapabilityArtifactView(
        artifact=artifact,
        state=state,
        source_current=source_current,
        governance_current=governance_current,
    )


def _check(code: Any, passed: bool, detail: str) -> CapabilityArtifactCheck:
    return CapabilityArtifactCheck(code=code, passed=passed, detail=detail)


def _aliases(name: str) -> set[str]:
    aliases = {name}
    if "." in name:
        aliases.add(name.rsplit(".", 1)[-1])
    if "__" in name:
        aliases.add(name.rsplit("__", 1)[-1])
    return aliases


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _restore_artifact(payload: str, digest: str) -> EvolutionCapabilityImplementationArtifact:
    if not hmac.compare_digest(hashlib.sha256(payload.encode()).hexdigest(), digest):
        raise CapabilityArtifactError("Capability 实现制品持久摘要不一致。")
    try:
        return EvolutionCapabilityImplementationArtifact.model_validate_json(payload)
    except (TypeError, ValueError) as exc:
        raise CapabilityArtifactError("Capability 实现制品 JSON 损坏。") from exc


_SCHEMA = """
CREATE TABLE IF NOT EXISTS evolution_capability_implementation_artifacts (
    workspace_root TEXT NOT NULL,
    specification_id TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (workspace_root, artifact_id),
    UNIQUE (workspace_root, specification_id, source_sha256)
);
CREATE INDEX IF NOT EXISTS idx_evolution_capability_artifacts_spec
ON evolution_capability_implementation_artifacts(workspace_root, specification_id);
"""


__all__ = [
    "CapabilityArtifactCheck",
    "CapabilityArtifactError",
    "CapabilityArtifactView",
    "EvolutionCapabilityArtifactService",
    "EvolutionCapabilityArtifactStore",
    "EvolutionCapabilityImplementationArtifact",
    "build_capability_implementation_artifact",
    "render_capability_artifact",
]
