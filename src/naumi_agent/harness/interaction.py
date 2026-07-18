"""Strict durable interaction records and fenced state transitions."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.safety.guardrails import OutputGuardrail
from naumi_agent.user_interaction import (
    UserInteractionOption,
    UserInteractionRequest,
    normalize_interaction_response,
)

InteractionState = Literal["pending", "answered", "expired", "cancelled"]
InteractionSubjectKind = Literal["pursuit", "tool", "browser", "agent", "runtime"]
_SECRET_ASSIGNMENT_RE = re.compile(
    r"\b(api[_-]?key|token|secret|password|passwd|authorization|cookie|credential)"
    r"\s*([:=])\s*([^\s,;]+)",
    re.IGNORECASE,
)
_BEARER_RE = re.compile(r"\bbearer\s+\S+", re.IGNORECASE)


class _StrictInteractionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class HarnessInteractionOption(_StrictInteractionModel):
    value: str = Field(min_length=1, max_length=80)
    label: str = Field(min_length=1, max_length=80)
    description: str = Field(max_length=300)


class HarnessInteractionRecord(_StrictInteractionModel):
    """Authenticated latest state; Store also preserves every transition."""

    schema_version: Literal[1] = 1
    interaction_id: str = Field(pattern=r"^ask-[A-Za-z0-9._:-]{1,128}$")
    subject_kind: InteractionSubjectKind
    subject_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    )
    session_id: str = Field(max_length=128)
    agent_name: str = Field(min_length=1, max_length=128)
    sequence: int = Field(ge=1)
    state: InteractionState
    header: str = Field(min_length=1, max_length=40)
    question: str = Field(min_length=1, max_length=2_000)
    options: tuple[HarnessInteractionOption, ...] = Field(min_length=2, max_length=3)
    allow_custom: bool
    custom_label: str = Field(min_length=1, max_length=80)
    created_at: str = Field(min_length=1, max_length=64)
    expires_at: str = Field(max_length=64)
    owner_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    )
    owner_epoch: int = Field(ge=1)
    owner_lease_expires_at: str = Field(min_length=1, max_length=64)
    answer_kind: Literal["", "option", "custom"] = ""
    answer_value: str = Field(max_length=80)
    answer_label: str = Field(max_length=80)
    custom_text: str = Field(max_length=4_000)
    answered_by: str = Field(max_length=128)
    answered_at: str = Field(max_length=64)
    updated_at: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def _validate_state(self) -> HarnessInteractionRecord:
        _aware(self.created_at, "created_at")
        _aware(self.owner_lease_expires_at, "owner_lease_expires_at")
        _aware(self.updated_at, "updated_at")
        if _parsed(self.updated_at) < _parsed(self.created_at):
            raise ValueError("updated_at 不能早于 created_at。")
        if self.state == "pending" and (
            _parsed(self.owner_lease_expires_at) <= _parsed(self.updated_at)
        ):
            raise ValueError("pending interaction 必须持有尚未过期的 owner lease。")
        if self.expires_at:
            _aware(self.expires_at, "expires_at")
            if _parsed(self.expires_at) <= _parsed(self.created_at):
                raise ValueError("expires_at 必须晚于 created_at。")
            timeout_seconds = (
                _parsed(self.expires_at) - _parsed(self.created_at)
            ).total_seconds()
            if (
                not timeout_seconds.is_integer()
                or not 3 <= timeout_seconds <= 604_800
            ):
                raise ValueError("interaction timeout 必须是 3..604800 的整数秒。")
        if len({option.value for option in self.options}) != len(self.options):
            raise ValueError("interaction 选项 value 不能重复。")
        durable_text = (
            self.header,
            self.question,
            self.custom_label,
            self.answer_value,
            self.answer_label,
            self.custom_text,
            self.answered_by,
            *(field for option in self.options for field in (
                option.value, option.label, option.description,
            )),
        )
        if any(_safe(value) != value for value in durable_text):
            raise ValueError("interaction 持久文本必须先完成控制字符清理与脱敏。")
        answered = self.state == "answered"
        answer_fields = bool(
            self.answer_kind or self.answer_label or self.answered_by or self.answered_at
        )
        if answered != answer_fields:
            raise ValueError("answered 状态与答案字段不一致。")
        if answered:
            _aware(self.answered_at, "answered_at")
            if _parsed(self.answered_at) != _parsed(self.updated_at):
                raise ValueError("answered_at 必须等于 terminal updated_at。")
            if self.answer_kind == "option" and (
                not self.answer_value or self.custom_text
            ):
                raise ValueError("option 答案字段组合无效。")
            if self.answer_kind == "custom" and (
                self.answer_value or not self.custom_text
            ):
                raise ValueError("custom 答案字段组合无效。")
        elif self.answer_value or self.custom_text:
            raise ValueError("未回答记录不能携带答案正文。")
        return self

    def canonical_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    def digest(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    def request(self) -> UserInteractionRequest:
        timeout_seconds = (
            int((_parsed(self.expires_at) - _parsed(self.created_at)).total_seconds())
            if self.expires_at
            else None
        )
        return UserInteractionRequest(
            header=self.header,
            question=self.question,
            options=tuple(
                UserInteractionOption(
                    value=item.value,
                    label=item.label,
                    description=item.description,
                )
                for item in self.options
            ),
            allow_custom=self.allow_custom,
            custom_label=self.custom_label,
            timeout_seconds=timeout_seconds,
        )


def new_interaction_record(
    *,
    request: UserInteractionRequest,
    subject_kind: InteractionSubjectKind,
    subject_id: str,
    session_id: str,
    agent_name: str,
    owner_id: str,
    created_at: str,
    owner_lease_seconds: int,
    timeout_seconds: int | None = None,
    interaction_id: str | None = None,
) -> HarnessInteractionRecord:
    created = _aware(created_at, "created_at")
    if not 3 <= owner_lease_seconds <= 86_400:
        raise ValueError("owner_lease_seconds 必须在 3..86400 之间。")
    if timeout_seconds is not None and not 3 <= timeout_seconds <= 604_800:
        raise ValueError("timeout_seconds 必须在 3..604800 之间。")
    return HarnessInteractionRecord(
        interaction_id=interaction_id or f"ask-{uuid4().hex}",
        subject_kind=subject_kind,
        subject_id=_safe(subject_id),
        session_id=_safe(session_id),
        agent_name=_safe(agent_name or "main"),
        sequence=1,
        state="pending",
        header=_safe(request.header),
        question=_safe(request.question),
        options=tuple(
            HarnessInteractionOption(
                value=_safe(option.value),
                label=_safe(option.label),
                description=_safe(option.description),
            )
            for option in request.options
        ),
        allow_custom=request.allow_custom,
        custom_label=_safe(request.custom_label),
        created_at=created.isoformat(),
        expires_at=(
            (created + timedelta(seconds=timeout_seconds)).isoformat()
            if timeout_seconds is not None
            else ""
        ),
        owner_id=_safe(owner_id),
        owner_epoch=1,
        owner_lease_expires_at=(
            created + timedelta(seconds=owner_lease_seconds)
        ).isoformat(),
        answer_kind="",
        answer_value="",
        answer_label="",
        custom_text="",
        answered_by="",
        answered_at="",
        updated_at=created.isoformat(),
    )


def takeover_interaction(
    record: HarnessInteractionRecord,
    *,
    owner_id: str,
    now: str,
    owner_lease_seconds: int,
) -> HarnessInteractionRecord:
    current = _aware(now, "now")
    if record.state != "pending":
        raise ValueError("只有 pending interaction 可以 takeover。")
    if not 3 <= owner_lease_seconds <= 86_400:
        raise ValueError("owner_lease_seconds 必须在 3..86400 之间。")
    normalized_owner = _safe(owner_id)
    same_owner = normalized_owner == record.owner_id
    if not same_owner and current < _parsed(record.owner_lease_expires_at):
        raise ValueError("当前 interaction owner lease 仍有效，拒绝 takeover。")
    if record.expires_at and current >= _parsed(record.expires_at):
        raise ValueError("interaction 已超时，不能 takeover。")
    return _replace(record, {
        "sequence": record.sequence + 1,
        "owner_id": normalized_owner,
        "owner_epoch": record.owner_epoch if same_owner else record.owner_epoch + 1,
        "owner_lease_expires_at": (
            current + timedelta(seconds=owner_lease_seconds)
        ).isoformat(),
        "updated_at": current.isoformat(),
    })


def answer_interaction(
    record: HarnessInteractionRecord,
    *,
    owner_id: str,
    owner_epoch: int,
    response: dict[str, object],
    answered_by: str,
    now: str,
) -> HarnessInteractionRecord:
    current = _aware(now, "now")
    if record.state != "pending":
        raise ValueError("interaction 已结束，不能重复回答。")
    if owner_id != record.owner_id or owner_epoch != record.owner_epoch:
        raise ValueError("interaction owner/epoch 已失效。")
    if current >= _parsed(record.owner_lease_expires_at):
        raise ValueError("interaction owner lease 已过期。")
    if record.expires_at and current >= _parsed(record.expires_at):
        raise ValueError("interaction 已超时，不能继续回答。")
    normalized = normalize_interaction_response(record.request(), response)
    return _replace(record, {
        "sequence": record.sequence + 1,
        "state": "answered",
        "answer_kind": normalized["kind"],
        "answer_value": _safe(normalized["value"]),
        "answer_label": _safe(normalized["label"]),
        "custom_text": _safe(normalized["custom_text"]),
        "answered_by": _safe(answered_by),
        "answered_at": current.isoformat(),
        "updated_at": current.isoformat(),
    })


def expire_interaction(
    record: HarnessInteractionRecord,
    *,
    now: str,
) -> HarnessInteractionRecord:
    current = _aware(now, "now")
    if record.state != "pending":
        raise ValueError("只有 pending interaction 可以标记超时。")
    if not record.expires_at or current < _parsed(record.expires_at):
        raise ValueError("interaction 尚未达到超时时间。")
    return _replace(record, {
        "sequence": record.sequence + 1,
        "state": "expired",
        "updated_at": current.isoformat(),
    })


def cancel_interaction(
    record: HarnessInteractionRecord,
    *,
    now: str,
) -> HarnessInteractionRecord:
    """Cancel exactly one pending interaction at an explicit user boundary."""
    current = _aware(now, "now")
    if record.state != "pending":
        raise ValueError("只有 pending interaction 可以取消。")
    if current < _parsed(record.updated_at):
        raise ValueError("interaction 取消时间不能早于最近更新时间。")
    return _replace(record, {
        "sequence": record.sequence + 1,
        "state": "cancelled",
        "updated_at": current.isoformat(),
    })


def _safe(value: object) -> str:
    text = str(value or "").replace("\x00", "�")
    text = "".join(
        char if ord(char) >= 32 or char in {"\n", "\t"} else "�"
        for char in text
    )
    text = OutputGuardrail.redact(text)
    text = _SECRET_ASSIGNMENT_RE.sub(
        lambda match: f"{match.group(1)}{match.group(2)}<redacted>",
        text,
    )
    return _BEARER_RE.sub("Bearer <redacted>", text).strip()


def _replace(
    record: HarnessInteractionRecord,
    updates: dict[str, object],
) -> HarnessInteractionRecord:
    payload = record.model_dump(mode="python")
    payload.update(updates)
    return HarnessInteractionRecord.model_validate(payload)


def _aware(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} 必须是 ISO 8601 时间。") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} 必须包含时区偏移。")
    return parsed


def _parsed(value: str) -> datetime:
    return datetime.fromisoformat(value)


__all__ = [
    "HarnessInteractionRecord",
    "answer_interaction",
    "cancel_interaction",
    "expire_interaction",
    "new_interaction_record",
    "takeover_interaction",
]
