"""Structured validation command and working-directory policy."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


class ValidationPolicyError(ValueError):
    """Raised when a validation request crosses a mechanical safety boundary."""


@dataclass(frozen=True)
class ApprovedValidationCommand:
    argv: tuple[str, ...]
    cwd: Path


class ValidationCommandPolicy:
    """Approve argv-only commands inside explicitly managed roots."""

    def __init__(
        self,
        *,
        allowed_commands: Sequence[Sequence[str]],
        allowed_roots: Sequence[str | Path] = (),
    ) -> None:
        self._allowed_commands = tuple(
            self._normalize_argv(prefix, field="allowed_commands")
            for prefix in allowed_commands
        )
        if not self._allowed_commands:
            raise ValidationPolicyError("验证命令允许列表不能为空。")
        self._allowed_roots = tuple(
            Path(root).expanduser().resolve(strict=True) for root in allowed_roots
        )
        if any(not root.is_dir() for root in self._allowed_roots):
            raise ValidationPolicyError("允许的工作区必须是现有目录。")

    def approve(
        self,
        *,
        argv: Sequence[str],
        cwd: str | Path,
    ) -> ApprovedValidationCommand:
        normalized = self._normalize_argv(argv, field="argv")
        if not any(
            normalized[: len(prefix)] == prefix for prefix in self._allowed_commands
        ):
            raise ValidationPolicyError(
                f"验证命令不在允许列表：{normalized[0]}"
            )

        try:
            resolved_cwd = Path(cwd).expanduser().resolve(strict=True)
        except OSError as exc:
            raise ValidationPolicyError(f"验证工作目录不存在或不可读取：{cwd}") from exc
        if not resolved_cwd.is_dir():
            raise ValidationPolicyError(f"验证工作目录不是目录：{cwd}")
        if self._allowed_roots and not any(
            _is_relative_to(resolved_cwd, root) for root in self._allowed_roots
        ):
            raise ValidationPolicyError(
                f"验证工作目录必须位于允许的工作区内：{resolved_cwd}"
            )
        return ApprovedValidationCommand(argv=normalized, cwd=resolved_cwd)

    @staticmethod
    def _normalize_argv(
        argv: Sequence[str],
        *,
        field: str,
    ) -> tuple[str, ...]:
        if isinstance(argv, (str, bytes)) or not isinstance(argv, Sequence) or not argv:
            raise ValidationPolicyError(f"{field} 必须是非空 argv 字符串数组。")
        normalized: list[str] = []
        for argument in argv:
            if not isinstance(argument, str) or not argument:
                raise ValidationPolicyError(f"{field} 不能包含空值或非字符串。")
            if "\x00" in argument:
                raise ValidationPolicyError(f"{field} 不能包含 NUL 字符。")
            normalized.append(argument)
        return tuple(normalized)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
