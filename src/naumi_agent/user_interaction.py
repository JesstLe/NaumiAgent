"""Validated domain objects for model-initiated user interaction."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


class UserInteractionUnavailableError(RuntimeError):
    """Raised when the active host cannot collect structured user input."""


@dataclass(frozen=True, slots=True)
class UserInteractionOption:
    value: str
    label: str
    description: str = ""

    def to_public_dict(self) -> dict[str, str]:
        return {
            "value": self.value,
            "label": self.label,
            "description": self.description,
        }


@dataclass(frozen=True, slots=True)
class UserInteractionRequest:
    header: str
    question: str
    options: tuple[UserInteractionOption, ...]
    allow_custom: bool = True
    custom_label: str = "其他"
    timeout_seconds: int | None = None

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "header": self.header,
            "question": self.question,
            "options": [option.to_public_dict() for option in self.options],
            "allow_custom": self.allow_custom,
            "custom_label": self.custom_label,
            "timeout_seconds": self.timeout_seconds,
        }


def normalize_interaction_request(payload: Mapping[str, Any]) -> UserInteractionRequest:
    """Validate and sanitize one model-authored interaction request."""
    header = _bounded_text(payload.get("header"), field="标题", maximum=40)
    question = _bounded_text(
        payload.get("question"),
        field="问题",
        maximum=2_000,
        multiline=True,
    )
    raw_options = payload.get("options")
    if not isinstance(raw_options, Sequence) or isinstance(raw_options, (str, bytes)):
        raise ValueError("必须提供 2 到 3 个选项")
    if not 2 <= len(raw_options) <= 3:
        raise ValueError("必须提供 2 到 3 个选项")

    options: list[UserInteractionOption] = []
    for raw_option in raw_options:
        if not isinstance(raw_option, Mapping):
            raise ValueError("每个选项都必须是对象")
        options.append(
            UserInteractionOption(
                value=_bounded_text(raw_option.get("value"), field="选项 value", maximum=80),
                label=_bounded_text(raw_option.get("label"), field="选项标签", maximum=80),
                description=_bounded_text(
                    raw_option.get("description", ""),
                    field="选项说明",
                    maximum=300,
                    multiline=True,
                    required=False,
                ),
            )
        )
    if len({option.value for option in options}) != len(options):
        raise ValueError("选项 value 不能重复")

    raw_allow_custom = payload.get("allow_custom", True)
    if not isinstance(raw_allow_custom, bool):
        raise ValueError("allow_custom 必须是布尔值")
    allow_custom = raw_allow_custom
    custom_label = _bounded_text(
        payload.get("custom_label", "其他"),
        field="自定义选项标签",
        maximum=80,
    )
    raw_timeout = payload.get("timeout_seconds")
    timeout_seconds: int | None = None
    if raw_timeout is not None:
        if isinstance(raw_timeout, bool) or not isinstance(raw_timeout, int):
            raise ValueError("交互超时必须是整数秒")
        if not 3 <= raw_timeout <= 604_800:
            raise ValueError("交互超时必须在 3..604800 秒之间")
        timeout_seconds = raw_timeout
    return UserInteractionRequest(
        header=header,
        question=question,
        options=tuple(options),
        allow_custom=allow_custom,
        custom_label=custom_label,
        timeout_seconds=timeout_seconds,
    )


def normalize_interaction_response(
    request: UserInteractionRequest,
    response: Mapping[str, Any],
) -> dict[str, str]:
    """Validate a host response against the exact pending request."""
    kind = str(response.get("kind") or "option").strip().lower()
    if kind == "option":
        value = _bounded_text(response.get("value"), field="选择值", maximum=80)
        option = next((item for item in request.options if item.value == value), None)
        if option is None:
            raise ValueError("选择值不属于当前问题")
        return {
            "kind": "option",
            "value": option.value,
            "label": option.label,
            "custom_text": "",
        }
    if kind == "custom":
        if not request.allow_custom:
            raise ValueError("当前问题不允许自定义输入")
        custom_text = _bounded_text(
            response.get("custom_text"),
            field="自定义输入",
            maximum=4_000,
            multiline=True,
        )
        return {
            "kind": "custom",
            "value": "",
            "label": request.custom_label,
            "custom_text": custom_text,
        }
    raise ValueError("交互响应 kind 只能是 option 或 custom")


def public_interaction_request_payload(
    request: UserInteractionRequest,
    *,
    request_id: str,
    session_id: str = "",
    run_id: str = "",
    agent_name: str = "main",
    expires_at: str = "",
) -> dict[str, Any]:
    """Build the exact pending-question shape shown by every frontend."""
    if not re.fullmatch(r"ask-[A-Za-z0-9._:-]{1,128}", request_id):
        raise ValueError("交互 request_id 格式无效")
    return {
        "request_id": request_id,
        "session_id": _bounded_text(
            session_id,
            field="会话 ID",
            maximum=128,
            required=False,
        ),
        "run_id": _bounded_text(
            run_id,
            field="运行 ID",
            maximum=128,
            required=False,
        ),
        "agent_name": _bounded_text(
            agent_name or "main",
            field="Agent 名称",
            maximum=80,
        ),
        **request.to_public_dict(),
        "expires_at": _bounded_text(
            expires_at,
            field="过期时间",
            maximum=64,
            required=False,
        ),
        "status": "needs_input",
    }


def _bounded_text(
    value: Any,
    *,
    field: str,
    maximum: int,
    multiline: bool = False,
    required: bool = True,
) -> str:
    text = _CONTROL_RE.sub("", str(value or ""))
    if not multiline:
        text = " ".join(text.split())
    text = text.strip()
    if required and not text:
        raise ValueError(f"{field}不能为空")
    if len(text) > maximum:
        raise ValueError(f"{field}最多 {maximum} 个字符")
    return text
