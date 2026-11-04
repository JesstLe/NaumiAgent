"""输出护栏 — 敏感信息脱敏、危险内容检测."""

from __future__ import annotations

import logging
import re
from collections import deque
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _SecretSpec:
    minimum: int
    replacement: str = "[REDACTED]"
    direct: bool = False


_PREFIX_SPECS = {
    "apikey": _SecretSpec(20),
    "api_key": _SecretSpec(20),
    "api-key": _SecretSpec(20),
    "password": _SecretSpec(8),
    "token": _SecretSpec(20),
    "secret": _SecretSpec(20),
    "sk-": _SecretSpec(20, "[REDACTED_API_KEY]", True),
    "ghp_": _SecretSpec(36, "[REDACTED_GITHUB_TOKEN]", True),
    "gho_": _SecretSpec(36, "[REDACTED_GITHUB_TOKEN]", True),
}


class IncrementalRedactor:
    """Redact across arbitrary chunk boundaries with bounded candidate storage.

    Safe prose is emitted immediately. Once a value reaches its threshold, its
    placeholder is emitted and the remaining value is consumed, not buffered.
    The largest undecided candidate is a GitHub prefix plus 35 characters.
    """

    def __init__(self) -> None:
        self._phase = "normal"
        self._prefix = ""
        self._value = ""
        self._spec: _SecretSpec | None = None

    @property
    def buffered_chars(self) -> int:
        return len(self._prefix) + len(self._value)

    def _reset(self) -> None:
        self._phase = "normal"
        self._prefix = ""
        self._value = ""
        self._spec = None

    @staticmethod
    def _separator(char: str) -> bool:
        return char.isspace() or char in '":='

    def feed(self, text: str) -> str:
        if self._phase == "finished":
            raise ValueError("脱敏流已结束，请为新响应创建独立实例。")
        output: list[str] = []
        replay: deque[str] = deque()
        for char in text:
            replay.append(char)
            while replay:
                self._consume(replay.popleft(), output, replay)
        return "".join(output)

    def _consume(self, char: str, output: list[str], replay: deque[str]) -> None:
        if self._phase == "normal":
            self._prefix += char
            while self._prefix:
                candidate = self._prefix.lower()
                spec = _PREFIX_SPECS.get(candidate)
                if spec is not None:
                    self._spec = spec
                    self._phase = "value" if spec.direct else "await_separator"
                    return
                if any(prefix.startswith(candidate) for prefix in _PREFIX_SPECS):
                    return
                output.append(self._prefix[0])
                self._prefix = self._prefix[1:]
            return

        if self._phase == "await_separator":
            if self._separator(char):
                output.append(self._prefix + char)
                self._prefix = ""
                self._phase = "separator"
            else:
                # This start failed; re-scan its suffix for a nested start.
                failed = self._prefix
                self._reset()
                output.append(failed[0])
                replay.extendleft(reversed(failed[1:] + char))
            return

        if self._phase == "separator":
            if self._separator(char):
                output.append(char)
                return
            self._phase = "value"
            if char == "'":
                output.append(char)
                return

        assert self._spec is not None
        is_value = (
            char.isascii() and char.isalnum()
            if self._spec.direct
            else char.isalnum() or char in "_-"
        )
        if self._phase == "masked":
            if not is_value:
                self._reset()
                replay.appendleft(char)
            return

        if is_value:
            self._value += char
            if len(self._value) >= self._spec.minimum:
                output.append(self._spec.replacement)
                self._prefix = ""
                self._value = ""
                self._phase = "masked"
            return

        failed = self._prefix + self._value
        direct = self._spec.direct
        self._reset()
        if direct:
            output.append(failed[0])
            failed = failed[1:]
        replay.extendleft(reversed(failed + char))

    def finish(self) -> str:
        if self._phase == "finished":
            return ""
        tail = self._prefix + self._value
        self._reset()
        self._phase = "finished"
        return tail


_DANGEROUS_PATTERNS = [
    r"rm\s+-rf\s+/",
    r"del\s+/[sS]\s+/[qQ]\s+[a-zA-Z]:\\",
    r">\s*/dev/sd",
    r"mkfs\.",
    r"dd\s+if=.*of=/dev/",
]


class OutputGuardrail:
    """输出审计."""

    def validate(self, output: str) -> str:
        """审计输出：脱敏 + 安全检查."""
        output = self._redact_secrets(output)
        self._check_dangerous_content(output)
        return output

    def _redact_secrets(self, text: str) -> str:
        return self.redact(text)

    def _check_dangerous_content(self, text: str) -> None:
        for pattern in _DANGEROUS_PATTERNS:
            if re.search(pattern, text):
                raise SecurityError("输出包含潜在危险命令，已拦截。")

    @staticmethod
    def redact(text: str) -> str:
        """便捷方法：仅脱敏不拦截."""
        stream = IncrementalRedactor()
        return stream.feed(text) + stream.finish()


class SecurityError(Exception):
    """安全违规异常."""
