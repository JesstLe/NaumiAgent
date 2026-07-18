"""Post-write diff and public API guard for isolated evolution patches."""

from __future__ import annotations

import ast
import difflib
import hashlib
import hmac
import json
import os
import stat
import subprocess
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.experiment_leases import ExperimentWorktreeLease
from naumi_agent.evolution.experiment_snapshots import EvolutionExperimentSourceSnapshot
from naumi_agent.evolution.experiments import EvolutionExperimentContract
from naumi_agent.evolution.mutation_plans import EvolutionMutationPlan, MutationFileFact
from naumi_agent.evolution.static_guards import (
    EvolutionStaticGuardReceipt,
    StaticGuardChangeFact,
)

POSTFLIGHT_GUARD_POLICY = "evolution-postflight-diff-api-v1"
_SHA256_RE = r"^[0-9a-f]{64}$"
_MAX_SOURCE_BYTES = 2 * 1024 * 1024
_MAX_DIFF_LINES = 20_000
_NON_API_FILE_KINDS = frozenset({"markdown", "yaml", "json", "toml", "text"})


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class PostflightDiffFact(_StrictModel):
    path: str = Field(min_length=1, max_length=1_024)
    file_kind: str = Field(min_length=1, max_length=32)
    operation: Literal["modify", "create"]
    before_sha256: str | None = Field(default=None, pattern=_SHA256_RE)
    after_sha256: str = Field(pattern=_SHA256_RE)
    unified_diff_sha256: str = Field(pattern=_SHA256_RE)
    added_lines: int = Field(ge=0, le=4_194_304)
    deleted_lines: int = Field(ge=0, le=4_194_304)
    before_mode: int | None = Field(default=None, ge=0, le=0o777)
    after_mode: int = Field(ge=0, le=0o777)
    api_change: Literal["not_applicable", "unchanged", "additive"]
    api_before_sha256: str | None = Field(default=None, pattern=_SHA256_RE)
    api_after_sha256: str | None = Field(default=None, pattern=_SHA256_RE)
    public_symbols_before: int = Field(ge=0, le=100_000)
    public_symbols_after: int = Field(ge=0, le=100_000)


class EvolutionPostflightGuardReceipt(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-postflight-diff-api-v1"] = (
        POSTFLIGHT_GUARD_POLICY
    )
    postflight_guard_id: str = Field(pattern=r"^evpg_[0-9a-f]{24}$")
    receipt_sha256: str = Field(pattern=_SHA256_RE)
    contract_id: str = Field(pattern=r"^evx_[0-9a-f]{24}$")
    lease_id: str = Field(pattern=r"^evl_[0-9a-f]{24}$")
    source_snapshot_id: str = Field(pattern=r"^evs_[0-9a-f]{24}$")
    mutation_plan_id: str = Field(pattern=r"^evpplan_[0-9a-f]{24}$")
    static_guard_id: str = Field(pattern=r"^evg_[0-9a-f]{24}$")
    facts: tuple[PostflightDiffFact, ...] = Field(min_length=1, max_length=16)
    facts_sha256: str = Field(pattern=_SHA256_RE)
    total_changed_files: int = Field(ge=1, le=16)
    total_added_lines: int = Field(ge=0, le=67_108_864)
    total_deleted_lines: int = Field(ge=0, le=67_108_864)
    breaking_api_changes: Literal[0] = 0
    postflight_passed: Literal[True] = True
    execution_ready: Literal[False] = False

    @model_validator(mode="after")
    def _receipt_is_tamper_evident(self) -> Self:
        if self.total_changed_files != len(self.facts):
            raise ValueError("Postflight Guard 文件总数不一致。")
        if self.total_added_lines != sum(item.added_lines for item in self.facts):
            raise ValueError("Postflight Guard added lines 不一致。")
        if self.total_deleted_lines != sum(item.deleted_lines for item in self.facts):
            raise ValueError("Postflight Guard deleted lines 不一致。")
        expected_facts = _sha256_payload(
            [item.model_dump(mode="json") for item in self.facts]
        )
        if not hmac.compare_digest(self.facts_sha256, expected_facts):
            raise ValueError("Postflight Guard facts 摘要不一致。")
        expected = _sha256_payload(
            self.model_dump(
                mode="json",
                exclude={"postflight_guard_id", "receipt_sha256"},
            )
        )
        if not hmac.compare_digest(self.receipt_sha256, expected):
            raise ValueError("Postflight Guard Receipt 摘要不一致。")
        if self.postflight_guard_id != f"evpg_{expected[:24]}":
            raise ValueError("Postflight Guard identity 不一致。")
        return self


class EvolutionPostflightGuardError(RuntimeError):
    """Fail-closed postflight result without source or diff content."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionPostflightGuard:
    """Rebuild diff/API facts from Git baseline and written worktree bytes."""

    def inspect(
        self,
        *,
        contract: EvolutionExperimentContract,
        lease: ExperimentWorktreeLease,
        source_snapshot: EvolutionExperimentSourceSnapshot,
        mutation_plan: EvolutionMutationPlan,
        static_guard: EvolutionStaticGuardReceipt,
    ) -> EvolutionPostflightGuardReceipt:
        _require_bindings(contract, lease, source_snapshot, mutation_plan, static_guard)
        root = Path(lease.worktree_path).resolve(strict=True)
        planned = {item.path: item for item in mutation_plan.planned_files}
        facts = tuple(
            self._inspect_file(
                root=root,
                baseline_commit=contract.baseline.commit,
                planned=planned[change.path],
                change=change,
            )
            for change in static_guard.changes
        )
        if tuple(item.path for item in facts) != tuple(sorted(planned)):
            raise EvolutionPostflightGuardError(
                "postflight_scope_mismatch",
                "写后事实未覆盖完整 Mutation Plan 文件范围。",
            )
        payload = {
            "schema_version": 1,
            "policy_version": POSTFLIGHT_GUARD_POLICY,
            "contract_id": contract.contract_id,
            "lease_id": lease.lease_id,
            "source_snapshot_id": source_snapshot.snapshot_id,
            "mutation_plan_id": mutation_plan.plan_id,
            "static_guard_id": static_guard.guard_id,
            "facts": [item.model_dump(mode="json") for item in facts],
            "facts_sha256": _sha256_payload(
                [item.model_dump(mode="json") for item in facts]
            ),
            "total_changed_files": len(facts),
            "total_added_lines": sum(item.added_lines for item in facts),
            "total_deleted_lines": sum(item.deleted_lines for item in facts),
            "breaking_api_changes": 0,
            "postflight_passed": True,
            "execution_ready": False,
        }
        digest = _sha256_payload(payload)
        return EvolutionPostflightGuardReceipt.model_validate({
            **payload,
            "postflight_guard_id": f"evpg_{digest[:24]}",
            "receipt_sha256": digest,
        })

    def _inspect_file(
        self,
        *,
        root: Path,
        baseline_commit: str,
        planned: MutationFileFact,
        change: StaticGuardChangeFact,
    ) -> PostflightDiffFact:
        if planned.path != change.path or planned.change_mode != change.operation:
            raise EvolutionPostflightGuardError(
                "postflight_binding_mismatch",
                "写后文件事实与 Mutation Plan 绑定不一致。",
        )
        target = root / change.path
        after, metadata = _read_regular_file(target)
        after_digest = hashlib.sha256(after).hexdigest()
        if not hmac.compare_digest(after_digest, change.after_sha256):
            raise EvolutionPostflightGuardError(
                "postflight_digest_mismatch", "写后摘要与 Static Guard 不一致。"
            )
        before, before_mode = _read_git_baseline(
            root, baseline_commit, planned,
        )
        before_digest = hashlib.sha256(before).hexdigest() if before is not None else None
        if before_digest != change.before_sha256:
            raise EvolutionPostflightGuardError(
                "postflight_baseline_mismatch", "Git baseline 与 Static Guard 不一致。"
            )
        after_mode = stat.S_IMODE(metadata.st_mode)
        if before_mode is not None and before_mode != after_mode:
            raise EvolutionPostflightGuardError(
                "postflight_mode_changed", "补丁意外改变了文件执行权限。"
            )
        if before_mode is None and after_mode != 0o644:
            raise EvolutionPostflightGuardError(
                "postflight_mode_changed", "新文件必须使用 0644 权限。"
            )
        before_lines = _decode_lines(before)
        after_lines = _decode_lines(after)
        added, deleted = _line_delta(before_lines, after_lines)
        if (added, deleted) != (change.added_lines, change.deleted_lines):
            raise EvolutionPostflightGuardError(
                "postflight_diff_mismatch", "写后 diff 行数与 Static Guard 不一致。"
            )
        api = _compare_public_api(planned.file_kind, before, after, change.path)
        unified = tuple(difflib.unified_diff(
            before_lines,
            after_lines,
            fromfile=f"a/{change.path}",
            tofile=f"b/{change.path}",
            lineterm="",
        ))
        if len(unified) > _MAX_DIFF_LINES:
            raise EvolutionPostflightGuardError(
                "postflight_diff_oversized", "写后 unified diff 超过安全上限。"
            )
        return PostflightDiffFact(
            path=change.path,
            file_kind=planned.file_kind,
            operation=change.operation,
            before_sha256=before_digest,
            after_sha256=after_digest,
            unified_diff_sha256=_sha256_payload(unified),
            added_lines=added,
            deleted_lines=deleted,
            before_mode=before_mode,
            after_mode=after_mode,
            api_change=api[0],
            api_before_sha256=api[1],
            api_after_sha256=api[2],
            public_symbols_before=api[3],
            public_symbols_after=api[4],
        )


def _require_bindings(
    contract: EvolutionExperimentContract,
    lease: ExperimentWorktreeLease,
    snapshot: EvolutionExperimentSourceSnapshot,
    plan: EvolutionMutationPlan,
    guard: EvolutionStaticGuardReceipt,
) -> None:
    if (
        lease.contract_id != contract.contract_id
        or snapshot.contract_id != contract.contract_id
        or snapshot.lease_id != lease.lease_id
        or plan.contract_id != contract.contract_id
        or plan.lease_id != lease.lease_id
        or plan.source_snapshot_id != snapshot.snapshot_id
        or guard.contract_id != contract.contract_id
        or guard.lease_id != lease.lease_id
        or guard.source_snapshot_id != snapshot.snapshot_id
        or guard.mutation_plan_id != plan.plan_id
        or not guard.preflight_passed
    ):
        raise EvolutionPostflightGuardError(
            "postflight_binding_mismatch", "Postflight Guard 权威绑定不一致。"
        )


def _read_git_baseline(
    root: Path,
    commit: str,
    planned: MutationFileFact,
) -> tuple[bytes | None, int | None]:
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
    if planned.change_mode == "create":
        try:
            tree = subprocess.run(
                ["git", "-C", str(root), "ls-tree", commit, "--", planned.path],
                check=True,
                capture_output=True,
                timeout=10,
                env=env,
            ).stdout
        except (OSError, subprocess.SubprocessError) as exc:
            raise EvolutionPostflightGuardError(
                "postflight_git_failed", "无法核对 Git baseline。"
            ) from exc
        if tree.strip():
            raise EvolutionPostflightGuardError(
                "postflight_baseline_mismatch", "计划创建的文件已存在于 Git baseline。"
            )
        return None, None
    try:
        content = subprocess.run(
            ["git", "-C", str(root), "show", f"{commit}:{planned.path}"],
            check=True,
            capture_output=True,
            timeout=10,
            env=env,
        ).stdout
        tree = subprocess.run(
            ["git", "-C", str(root), "ls-tree", commit, "--", planned.path],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise EvolutionPostflightGuardError(
            "postflight_git_failed", "无法读取 Git baseline。"
        ) from exc
    if len(content) > _MAX_SOURCE_BYTES or not tree:
        raise EvolutionPostflightGuardError(
            "postflight_baseline_mismatch", "Git baseline 文件缺失或超过上限。"
        )
    mode = tree.split(maxsplit=1)[0]
    if mode not in {"100644", "100755"}:
        raise EvolutionPostflightGuardError(
            "postflight_baseline_mismatch", "Git baseline 目标不是普通源码文件。"
        )
    return content, 0o755 if mode == "100755" else 0o644


def _read_regular_file(target: Path) -> tuple[bytes, os.stat_result]:
    """Read one stable regular file without following symlinks where supported."""
    try:
        before = target.lstat()
        if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode):
            raise EvolutionPostflightGuardError(
                "postflight_file_type", "写后目标不是普通文件。"
            )
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(target, flags)
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
            ):
                raise EvolutionPostflightGuardError(
                    "postflight_file_changed", "写后目标在审查期间发生替换。"
                )
            chunks: list[bytes] = []
            remaining = _MAX_SOURCE_BYTES + 1
            while remaining:
                chunk = os.read(descriptor, min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            content = b"".join(chunks)
        finally:
            os.close(descriptor)
    except EvolutionPostflightGuardError:
        raise
    except OSError as exc:
        raise EvolutionPostflightGuardError(
            "postflight_read_failed", "写后目标文件不可读取。"
        ) from exc
    if len(content) > _MAX_SOURCE_BYTES:
        raise EvolutionPostflightGuardError(
            "postflight_file_oversized", "写后目标超过 2 MiB 上限。"
        )
    return content, opened


def _compare_public_api(
    file_kind: str,
    before: bytes | None,
    after: bytes,
    path: str,
) -> tuple[str, str | None, str | None, int, int]:
    if file_kind in _NON_API_FILE_KINDS:
        return "not_applicable", None, None, 0, 0
    if file_kind != "python":
        raise EvolutionPostflightGuardError(
            "postflight_api_unsupported",
            f"{file_kind} 暂无可靠公共 API 解析器，拒绝提交 {path}。",
        )
    before_api = _python_public_api(before or b"", path) if before is not None else {}
    after_api = _python_public_api(after, path)
    breaking = {
        name
        for name, signature in before_api.items()
        if after_api.get(name) != signature
    }
    if breaking:
        raise EvolutionPostflightGuardError(
            "postflight_breaking_api",
            f"补丁删除或改变了 {len(breaking)} 个既有 Python 公共 API。",
        )
    change = "additive" if set(after_api) - set(before_api) else "unchanged"
    return (
        change,
        _sha256_payload(before_api),
        _sha256_payload(after_api),
        len(before_api),
        len(after_api),
    )


def _python_public_api(content: bytes, path: str) -> dict[str, str]:
    try:
        tree = ast.parse(content.decode("utf-8"), filename=path)
    except (SyntaxError, UnicodeDecodeError) as exc:
        raise EvolutionPostflightGuardError(
            "postflight_api_parse_failed", "Python 公共 API AST 解析失败。"
        ) from exc
    public: dict[str, object] = {}
    declared_all: tuple[str, ...] | None = None
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _public(node.name):
            public[f"function:{node.name}"] = _function_signature(node)
        elif isinstance(node, ast.ClassDef) and _public(node.name):
            public[f"class:{node.name}"] = {
                "bases": [ast.dump(item, include_attributes=False) for item in node.bases],
                "keywords": [
                    (item.arg, ast.dump(item.value, include_attributes=False))
                    for item in node.keywords
                ],
                "decorators": _decorator_names(node.decorator_list),
            }
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and _public(item.name):
                    public[f"class:{node.name}.method:{item.name}"] = _function_signature(item)
                elif isinstance(item, (ast.Assign, ast.AnnAssign)):
                    for name in _assignment_names(item):
                        if _public(name):
                            annotation = getattr(item, "annotation", None)
                            value = getattr(item, "value", None)
                            public[f"class:{node.name}.value:{name}"] = {
                                "annotation": (
                                    ast.dump(annotation, include_attributes=False)
                                    if annotation is not None else None
                                ),
                                "value": (
                                    ast.dump(value, include_attributes=False)
                                    if value is not None else None
                                ),
                            }
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            names = _assignment_names(node)
            if "__all__" in names:
                declared_all = _literal_string_sequence(getattr(node, "value", None))
            for name in names:
                if _public(name):
                    annotation = getattr(node, "annotation", None)
                    value = getattr(node, "value", None)
                    public[f"value:{name}"] = {
                        "annotation": (
                            ast.dump(annotation, include_attributes=False)
                            if annotation is not None else None
                        ),
                        "value": (
                            ast.dump(value, include_attributes=False)
                            if value is not None else None
                        ),
                    }
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                name = alias.asname or alias.name.split(".", 1)[0]
                if _public(name):
                    public[f"import:{name}"] = (
                        type(node).__name__, getattr(node, "module", None), alias.name
                    )
    if declared_all is not None:
        allowed = set(declared_all)
        public = {
            key: value
            for key, value in public.items()
            if _api_top_level_name(key) in allowed
        }
        for name in allowed:
            public[f"export:{name}"] = "public"
    return {key: _sha256_payload(value) for key, value in sorted(public.items())}


def _function_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> object:
    arguments = node.args
    return {
        "async": isinstance(node, ast.AsyncFunctionDef),
        "posonly": [_argument(item) for item in arguments.posonlyargs],
        "args": [_argument(item) for item in arguments.args],
        "vararg": _argument(arguments.vararg) if arguments.vararg else None,
        "kwonly": [_argument(item) for item in arguments.kwonlyargs],
        "kwarg": _argument(arguments.kwarg) if arguments.kwarg else None,
        "defaults": [
            ast.dump(item, include_attributes=False) for item in arguments.defaults
        ],
        "kw_defaults": [
            ast.dump(item, include_attributes=False) if item is not None else None
            for item in arguments.kw_defaults
        ],
        "returns": (
            ast.dump(node.returns, include_attributes=False)
            if node.returns is not None else None
        ),
        "decorators": _decorator_names(node.decorator_list),
        "type_params": [
            ast.dump(item, include_attributes=False)
            for item in getattr(node, "type_params", ())
        ],
    }


def _argument(value: ast.arg) -> tuple[str, str | None]:
    return (
        value.arg,
        ast.dump(value.annotation, include_attributes=False)
        if value.annotation is not None else None,
    )


def _decorator_names(values: list[ast.expr]) -> tuple[str, ...]:
    return tuple(ast.dump(value, include_attributes=False) for value in values)


def _assignment_names(node: ast.Assign | ast.AnnAssign) -> tuple[str, ...]:
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    return tuple(target.id for target in targets if isinstance(target, ast.Name))


def _literal_string_sequence(value: ast.expr | None) -> tuple[str, ...] | None:
    if not isinstance(value, (ast.List, ast.Tuple)):
        return None
    items: list[str] = []
    for item in value.elts:
        if not isinstance(item, ast.Constant) or not isinstance(item.value, str):
            return None
        items.append(item.value)
    return tuple(items)


def _public(name: str) -> bool:
    return not name.startswith("_")


def _api_top_level_name(key: str) -> str:
    parts = key.split(":", 2)
    if len(parts) < 2:
        return ""
    return parts[1].split(".", 1)[0]


def _decode_lines(content: bytes | None) -> tuple[str, ...]:
    if content is None:
        return ()
    try:
        return tuple(content.decode("utf-8").splitlines())
    except UnicodeDecodeError as exc:
        raise EvolutionPostflightGuardError(
            "postflight_invalid_encoding", "写后 diff 仅接受 UTF-8 文本。"
        ) from exc


def _line_delta(before: tuple[str, ...], after: tuple[str, ...]) -> tuple[int, int]:
    if len(before) + len(after) > 10_000:
        return len(after), len(before)
    matcher = difflib.SequenceMatcher(a=before, b=after, autojunk=False)
    added = deleted = 0
    for tag, left_start, left_end, right_start, right_end in matcher.get_opcodes():
        if tag in {"replace", "delete"}:
            deleted += left_end - left_start
        if tag in {"replace", "insert"}:
            added += right_end - right_start
    return added, deleted


def _sha256_payload(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "EvolutionPostflightGuard",
    "EvolutionPostflightGuardError",
    "EvolutionPostflightGuardReceipt",
    "PostflightDiffFact",
]
