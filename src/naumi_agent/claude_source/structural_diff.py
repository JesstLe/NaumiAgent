"""Deterministic Git-tree diff derived from an approved Claude source baseline."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import subprocess
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from naumi_agent.claude_source.baseline import (
    SourceBaselineObservation,
    observe_source_baseline,
)
from naumi_agent.claude_source.refresh import (
    SourceRefreshStore,
    resolve_claude_source_db_path,
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_GIT_OUTPUT_BYTES = 16 * 1024 * 1024
_MAX_CHANGES = 20_000
_MAX_MAPPING_BYTES = 4 * 1024 * 1024
_DEPENDENCY_FILES = frozenset(
    {
        "package.json",
        "package-lock.json",
        "npm-shrinkwrap.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "bun.lock",
        "bun.lockb",
        "deno.json",
        "deno.lock",
    }
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SourceTreeChange(_StrictModel):
    kind: Literal["added", "deleted", "modified", "renamed", "copied", "type_changed"]
    old_path: str = ""
    new_path: str = ""
    similarity: int | None = Field(default=None, ge=0, le=100)

    @field_validator("old_path", "new_path")
    @classmethod
    def _safe_path(cls, value: str) -> str:
        return _normalize_git_path(value) if value else ""

    @model_validator(mode="after")
    def _shape(self) -> SourceTreeChange:
        if self.kind in {"renamed", "copied"}:
            if not self.old_path or not self.new_path or self.similarity is None:
                raise ValueError("rename/copy change 缺少完整路径或 similarity。")
        elif not self.new_path or self.old_path or self.similarity is not None:
            raise ValueError("普通 tree change 字段组合无效。")
        return self


class SourceMappedImpact(_StrictModel):
    area: str = Field(min_length=1, max_length=128)
    source_paths: tuple[str, ...] = Field(min_length=1, max_length=512)
    naumi_paths: tuple[str, ...] = Field(max_length=512)
    change_kinds: tuple[str, ...] = Field(min_length=1, max_length=6)
    deleted: bool

    @field_validator("source_paths", "naumi_paths")
    @classmethod
    def _portable_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(_normalize_git_path(value) for value in values)
        if normalized != tuple(sorted(set(normalized))):
            raise ValueError("mapped impact paths 必须排序且不得重复。")
        return normalized


class SourceStructuralDiff(BaseModel):
    """Integrity-bound, read-only structural diff receipt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    diff_id: str
    baseline_entry_id: str
    observation_id: str
    baseline_commit: str
    current_commit: str
    includes_worktree: bool
    status: Literal["unchanged", "change_detected", "invalid"]
    changes: tuple[SourceTreeChange, ...] = Field(max_length=_MAX_CHANGES)
    counts: dict[str, int]
    mapped_impacts: tuple[SourceMappedImpact, ...] = Field(max_length=512)
    mapping_status: Literal["current", "stale", "unavailable"]
    risk_flags: tuple[
        Literal[
            "license_changed",
            "dependency_manifest_changed",
            "mapped_path_deleted",
            "mapping_changed",
            "uncommitted_source",
        ],
        ...,
    ]
    errors: tuple[str, ...] = Field(max_length=20)

    @field_validator("diff_id", "baseline_entry_id", "observation_id")
    @classmethod
    def _digest(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("structural diff digest 必须是完整小写 SHA-256。")
        return value

    @model_validator(mode="after")
    def _integrity(self) -> SourceStructuralDiff:
        change_order = tuple(
            sorted(
                self.changes,
                key=lambda item: (item.new_path, item.old_path, item.kind),
            )
        )
        if self.changes != change_order or len(self.changes) != len(set(self.changes)):
            raise ValueError("structural diff changes 必须稳定排序且不得重复。")
        expected_counts = dict(sorted(Counter(item.kind for item in self.changes).items()))
        if self.counts != expected_counts:
            raise ValueError("structural diff counts 与 changes 不一致。")
        if self.risk_flags != tuple(sorted(set(self.risk_flags))):
            raise ValueError("structural diff risk_flags 必须排序且不得重复。")
        if self.mapped_impacts != tuple(
            sorted(self.mapped_impacts, key=lambda item: item.area)
        ) or len({item.area for item in self.mapped_impacts}) != len(self.mapped_impacts):
            raise ValueError("structural diff mapped impacts 必须按唯一 area 排序。")
        if self.status == "unchanged" and (self.changes or self.risk_flags or self.errors):
            raise ValueError("unchanged structural diff 不得携带变化或错误。")
        if self.status == "invalid" and not self.errors:
            raise ValueError("invalid structural diff 必须说明错误。")
        if self.status != "invalid" and self.errors:
            raise ValueError("非 invalid structural diff 不得携带错误。")
        if self.diff_id != source_structural_diff_sha256(self):
            raise ValueError("structural diff digest 不一致。")
        return self


def build_source_structural_diff(
    store: SourceRefreshStore,
    *,
    manifest_path: str | Path,
    source_root: str | Path,
    project_root: str | Path,
) -> SourceStructuralDiff:
    """Compare the approved Git tree with current local facts without fetching."""
    observation = observe_source_baseline(
        store,
        manifest_path=manifest_path,
        source_root=source_root,
        project_root=project_root,
    )
    root = Path(source_root).expanduser().resolve()
    project = Path(project_root).expanduser().resolve()
    if observation.status == "invalid":
        return _build_receipt(
            observation,
            status="invalid",
            changes=(),
            mapped_impacts=(),
            mapping_status="unavailable",
            risk_flags=(),
            errors=tuple(observation.audit.findings[:20]) or ("source baseline 无法验证。",),
        )
    if observation.approved_git.dirty:
        return _build_receipt(
            observation,
            status="invalid",
            changes=(),
            mapped_impacts=(),
            mapping_status="unavailable",
            risk_flags=(),
            errors=(
                "已批准 baseline 包含未提交工作树，Git commit 无法重建该结构快照。",
            ),
        )

    try:
        includes_worktree = not _is_worktree_clean(root)
        changes = _git_tree_changes(
            root,
            baseline_commit=observation.approved_git.commit,
            includes_worktree=includes_worktree,
        )
    except ValueError as exc:
        return _build_receipt(
            observation,
            status="invalid",
            changes=(),
            mapped_impacts=(),
            mapping_status="unavailable",
            risk_flags=(),
            errors=(str(exc),),
        )
    mapping_status, mapped_impacts = _mapped_impacts(
        observation,
        changes,
        project_root=project,
    )
    changed_paths = {
        path
        for change in changes
        for path in (change.old_path, change.new_path)
        if path
    }
    risks: set[str] = set()
    if observation.license.path in changed_paths:
        risks.add("license_changed")
    if any(PurePosixPath(path).name in _DEPENDENCY_FILES for path in changed_paths):
        risks.add("dependency_manifest_changed")
    if any(item.deleted for item in mapped_impacts):
        risks.add("mapped_path_deleted")
    if mapping_status != "current":
        risks.add("mapping_changed")
    if includes_worktree:
        risks.add("uncommitted_source")
    status: Literal["unchanged", "change_detected"] = (
        "change_detected" if changes or risks else "unchanged"
    )
    return _build_receipt(
        observation,
        status=status,
        changes=changes,
        mapped_impacts=mapped_impacts,
        mapping_status=mapping_status,
        risk_flags=tuple(sorted(risks)),
        errors=(),
        includes_worktree=includes_worktree,
    )


def source_structural_diff_sha256(receipt: SourceStructuralDiff) -> str:
    payload = receipt.model_dump(mode="json")
    payload.pop("diff_id", None)
    return _sha256_json(payload)


def _build_receipt(
    observation: SourceBaselineObservation,
    *,
    status: Literal["unchanged", "change_detected", "invalid"],
    changes: tuple[SourceTreeChange, ...],
    mapped_impacts: tuple[SourceMappedImpact, ...],
    mapping_status: Literal["current", "stale", "unavailable"],
    risk_flags: tuple[str, ...],
    errors: tuple[str, ...],
    includes_worktree: bool = False,
) -> SourceStructuralDiff:
    values = {
        "schema_version": 1,
        "baseline_entry_id": observation.baseline_entry_id,
        "observation_id": observation.observation_id,
        "baseline_commit": observation.approved_git.commit,
        "current_commit": observation.audit.current_commit,
        "includes_worktree": includes_worktree,
        "status": status,
        "changes": changes,
        "counts": dict(sorted(Counter(item.kind for item in changes).items())),
        "mapped_impacts": mapped_impacts,
        "mapping_status": mapping_status,
        "risk_flags": risk_flags,
        "errors": errors,
    }
    return SourceStructuralDiff(diff_id=_sha256_json(values), **values)


def _git_tree_changes(
    root: Path,
    *,
    baseline_commit: str,
    includes_worktree: bool,
) -> tuple[SourceTreeChange, ...]:
    _git(root, "cat-file", "-e", f"{baseline_commit}^{{commit}}")
    args = ["diff", "--name-status", "-z", "--find-renames", "--find-copies", baseline_commit]
    if not includes_worktree:
        args.append("HEAD")
    args.append("--")
    raw = _git(root, *args)
    changes = list(_parse_name_status(raw))
    if includes_worktree:
        untracked = _git(root, "ls-files", "--others", "--exclude-standard", "-z")
        for path in _split_z(untracked):
            changes.append(SourceTreeChange(kind="added", new_path=path))
    deduped = {
        (item.kind, item.old_path, item.new_path, item.similarity): item
        for item in changes
    }
    ordered = tuple(
        sorted(
            deduped.values(),
            key=lambda item: (item.new_path, item.old_path, item.kind),
        )
    )
    if len(ordered) > _MAX_CHANGES:
        raise ValueError(f"source structural diff 超过 {_MAX_CHANGES} 条变化上限。")
    return ordered


def _parse_name_status(raw: bytes) -> tuple[SourceTreeChange, ...]:
    tokens = _split_z(raw)
    changes: list[SourceTreeChange] = []
    index = 0
    kinds = {"A": "added", "D": "deleted", "M": "modified", "T": "type_changed"}
    while index < len(tokens):
        status = tokens[index]
        index += 1
        code = status[:1]
        if code in {"R", "C"}:
            if index + 1 >= len(tokens) or not status[1:].isdigit():
                raise ValueError("Git rename/copy structural diff 记录不完整。")
            old_path, new_path = tokens[index], tokens[index + 1]
            index += 2
            changes.append(
                SourceTreeChange(
                    kind="renamed" if code == "R" else "copied",
                    old_path=old_path,
                    new_path=new_path,
                    similarity=int(status[1:]),
                )
            )
            continue
        if code not in kinds or index >= len(tokens):
            raise ValueError(f"Git structural diff 状态不受支持：{status}")
        changes.append(SourceTreeChange(kind=kinds[code], new_path=tokens[index]))
        index += 1
    return tuple(changes)


def _mapped_impacts(
    observation: SourceBaselineObservation,
    changes: tuple[SourceTreeChange, ...],
    *,
    project_root: Path,
) -> tuple[Literal["current", "stale", "unavailable"], tuple[SourceMappedImpact, ...]]:
    mapping_path = _safe_project_file(project_root, observation.mapping.path)
    try:
        raw = mapping_path.read_bytes()
    except OSError:
        return "unavailable", ()
    if len(raw) > _MAX_MAPPING_BYTES:
        return "unavailable", ()
    if hashlib.sha256(raw).hexdigest() != observation.mapping.sha256:
        return "stale", ()
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "unavailable", ()
    entries = document.get("mapping") if isinstance(document, dict) else None
    if not isinstance(entries, list):
        return "unavailable", ()
    changed = {
        path: change.kind
        for change in changes
        for path in (change.old_path, change.new_path)
        if path
    }
    impacts: list[SourceMappedImpact] = []
    for entry in entries:
        if not isinstance(entry, dict):
            return "unavailable", ()
        area = str(entry.get("area") or "").strip()
        source_paths = entry.get("claude_code")
        naumi_paths = entry.get("naumi_agent")
        if (
            not area
            or not isinstance(source_paths, list)
            or not isinstance(naumi_paths, list)
            or any(not isinstance(path, str) for path in (*source_paths, *naumi_paths))
        ):
            return "unavailable", ()
        affected = tuple(sorted(path for path in source_paths if path in changed))
        if not affected:
            continue
        impacts.append(
            SourceMappedImpact(
                area=area,
                source_paths=affected,
                naumi_paths=tuple(sorted(set(naumi_paths))),
                change_kinds=tuple(sorted({changed[path] for path in affected})),
                deleted=any(changed[path] in {"deleted", "renamed"} for path in affected),
            )
        )
    return "current", tuple(sorted(impacts, key=lambda item: item.area))


def _safe_project_file(root: Path, relative: str) -> Path:
    path = PurePosixPath(relative)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("source mapping path 必须是项目内的规范相对路径。")
    candidate = root.joinpath(*path.parts).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError("source mapping path 越过项目边界。")
    return candidate


def _normalize_git_path(value: str) -> str:
    if not value or "\x00" in value or any(ord(char) < 32 for char in value):
        raise ValueError("Git structural diff path 含非法控制字符。")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("Git structural diff path 必须是规范相对路径。")
    return path.as_posix()


def _split_z(raw: bytes) -> tuple[str, ...]:
    if len(raw) > _MAX_GIT_OUTPUT_BYTES:
        raise ValueError("Git structural diff 输出超过安全上限。")
    if raw and not raw.endswith(b"\0"):
        raise ValueError("Git structural diff 缺少 NUL 终止符。")
    try:
        return tuple(item.decode("utf-8") for item in raw.split(b"\0") if item)
    except UnicodeDecodeError as exc:
        raise ValueError("Git structural diff path 不是 UTF-8。") from exc


def _git(root: Path, *args: str) -> bytes:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            check=False,
            capture_output=True,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("Git structural diff 执行失败。") from exc
    if result.returncode != 0:
        raise ValueError("Git structural diff 无法读取已批准 commit。")
    if len(result.stdout) > _MAX_GIT_OUTPUT_BYTES:
        raise ValueError("Git structural diff 输出超过安全上限。")
    return result.stdout


def _is_worktree_clean(root: Path) -> bool:
    return not bool(_git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all"))


def _sha256_json(payload: object) -> str:
    encoded = json.dumps(
        payload,
        default=_json_default,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _json_default(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    raise TypeError(f"{type(value).__name__} 无法序列化。")


def _main() -> int:
    parser = argparse.ArgumentParser(
        description="比较已批准 Claude source baseline 与当前本地 Git 树"
    )
    parser.add_argument("--manifest", default="frontend/terminal-ui/cc-source-map.v2.json")
    parser.add_argument("--source", default="../claude-code")
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--store", default=str(resolve_claude_source_db_path()))
    args = parser.parse_args()
    try:
        receipt = build_source_structural_diff(
            SourceRefreshStore(args.store),
            manifest_path=args.manifest,
            source_root=args.source,
            project_root=args.project_root,
        )
    except (OSError, sqlite3.Error, UnicodeError, ValueError) as exc:
        print(json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False))
        return 1
    print(receipt.model_dump_json(indent=2))
    return 0 if receipt.status == "unchanged" else 1


if __name__ == "__main__":
    raise SystemExit(_main())


__all__ = [
    "SourceMappedImpact",
    "SourceStructuralDiff",
    "SourceTreeChange",
    "build_source_structural_diff",
    "source_structural_diff_sha256",
]
