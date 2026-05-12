"""输出护栏 — 敏感信息脱敏、危险内容检测."""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

_SECRET_PATTERNS: list[tuple[str, str]] = [
    (r'(api[_\-]?key["\s:=]+)["\']?[\w\-]{20,}["\']?', r'\1[REDACTED]'),
    (r'(password["\s:=]+)["\']?[\w\-]{8,}["\']?', r'\1[REDACTED]'),
    (r'(token["\s:=]+)["\']?[\w\-]{20,}["\']?', r'\1[REDACTED]'),
    (r'(secret["\s:=]+)["\']?[\w\-]{20,}["\']?', r'\1[REDACTED]'),
    (r'sk-[a-zA-Z0-9]{20,}', '[REDACTED_API_KEY]'),
    (r'ghp_[a-zA-Z0-9]{36}', '[REDACTED_GITHUB_TOKEN]'),
    (r'gho_[a-zA-Z0-9]{36}', '[REDACTED_GITHUB_TOKEN]'),
]

_DANGEROUS_PATTERNS = [
    r'rm\s+-rf\s+/',
    r'del\s+/[sS]\s+/[qQ]\s+[a-zA-Z]:\\',
    r'>\s*/dev/sd',
    r'mkfs\.',
    r'dd\s+if=.*of=/dev/',
]


class OutputGuardrail:
    """输出审计."""

    def validate(self, output: str) -> str:
        """审计输出：脱敏 + 安全检查."""
        output = self._redact_secrets(output)
        self._check_dangerous_content(output)
        return output

    def _redact_secrets(self, text: str) -> str:
        for pattern, replacement in _SECRET_PATTERNS:
            text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
        return text

    def _check_dangerous_content(self, text: str) -> None:
        for pattern in _DANGEROUS_PATTERNS:
            if re.search(pattern, text):
                raise SecurityError(f"输出包含潜在危险命令，已拦截。")

    @staticmethod
    def redact(text: str) -> str:
        """便捷方法：仅脱敏不拦截."""
        for pattern, replacement in _SECRET_PATTERNS:
            text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
        return text


class SecurityError(Exception):
    """安全违规异常."""
