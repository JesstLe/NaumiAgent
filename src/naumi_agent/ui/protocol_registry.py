"""Published governance registry for every terminal JSONL event."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from naumi_agent.ui.protocol import ClientEventType, ServerEventType

EventOwner = Literal[
    "protocol",
    "runtime",
    "harness",
    "inspector",
    "agents",
    "safety",
    "workbench",
    "evolution",
    "diagnostics",
    "sessions",
    "tasks",
    "ui",
]
EventStability = Literal["stable", "experimental", "deprecated"]
EventCriticality = Literal["informational", "control", "terminal"]
EventPersistence = Literal["never", "timeline", "snapshot", "audit"]
EventRedaction = Literal["none", "required"]
EventDirection = Literal["client", "server"]

_FIELD_PATH = re.compile(r"^payload(?:\.[a-z][a-z0-9_]*)+$")


class ProtocolRegistryError(ValueError):
    """The published registry is missing, malformed, or semantically unsafe."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EventPolicy(_StrictModel):
    owner: EventOwner
    stability: EventStability
    criticality: EventCriticality
    persistence: EventPersistence
    sensitive_fields: tuple[str, ...] = Field(max_length=32)
    redaction: EventRedaction

    @model_validator(mode="after")
    def _sensitivity_is_governed(self) -> EventPolicy:
        if len(self.sensitive_fields) != len(set(self.sensitive_fields)):
            raise ValueError("sensitive_fields 不得重复。")
        if any(not _FIELD_PATH.fullmatch(field) for field in self.sensitive_fields):
            raise ValueError("sensitive_fields 必须是 payload 开头的字段路径。")
        if self.sensitive_fields and self.redaction != "required":
            raise ValueError("敏感字段必须声明 required redaction。")
        if not self.sensitive_fields and self.redaction != "none":
            raise ValueError("无敏感字段的事件不得伪造 redaction requirement。")
        return self


class EventCapabilityBinding(_StrictModel):
    client_events: tuple[str, ...] = Field(default=(), max_length=128)
    server_events: tuple[str, ...] = Field(default=(), max_length=128)

    @model_validator(mode="after")
    def _events_are_unique(self) -> EventCapabilityBinding:
        for direction, events in (
            ("client", self.client_events),
            ("server", self.server_events),
        ):
            if len(events) != len(set(events)):
                raise ValueError(f"{direction}_events 不得重复。")
        if not self.client_events and not self.server_events:
            raise ValueError("能力绑定至少需要一个事件。")
        return self


class ProtocolEventRegistry(_StrictModel):
    contract_version: StrictInt = Field(ge=1)
    registry_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    client: Mapping[str, EventPolicy]
    server: Mapping[str, EventPolicy]
    event_capabilities: Mapping[str, EventCapabilityBinding]

    @model_validator(mode="after")
    def _freeze_policy_maps(self) -> ProtocolEventRegistry:
        object.__setattr__(self, "client", MappingProxyType(dict(self.client)))
        object.__setattr__(self, "server", MappingProxyType(dict(self.server)))
        object.__setattr__(
            self,
            "event_capabilities",
            MappingProxyType(dict(self.event_capabilities)),
        )
        return self

    def policy(self, direction: EventDirection, event_type: str) -> EventPolicy:
        if direction not in ("client", "server"):
            raise ProtocolRegistryError(f"未知事件方向：{direction}")
        policies = self.client if direction == "client" else self.server
        try:
            return policies[event_type]
        except KeyError as exc:
            raise ProtocolRegistryError(
                f"未注册 {direction} 事件：{event_type}"
            ) from exc

    def required_capability(
        self,
        direction: EventDirection,
        event_type: str,
    ) -> str | None:
        """Return the single negotiated capability governing an event, if any."""
        if direction not in ("client", "server"):
            raise ProtocolRegistryError(f"未知事件方向：{direction}")
        field = "client_events" if direction == "client" else "server_events"
        matches = [
            capability
            for capability, binding in self.event_capabilities.items()
            if event_type in getattr(binding, field)
        ]
        if len(matches) > 1:
            raise ProtocolRegistryError(
                f"{direction} 事件 {event_type} 被多个能力重复绑定。"
            )
        return matches[0] if matches else None


def load_protocol_event_registry(
    contract_path: str | Path | None = None,
) -> ProtocolEventRegistry:
    """Load and validate exact enum coverage from the shipped protocol contract."""
    path = Path(contract_path).expanduser() if contract_path else _contract_path()
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProtocolRegistryError(
            f"无法读取 protocol contract：{type(exc).__name__}"
        ) from exc
    if not isinstance(document, dict):
        raise ProtocolRegistryError("protocol contract 必须是对象。")
    raw_registry = document.get("event_registry")
    if not isinstance(raw_registry, dict):
        raise ProtocolRegistryError("protocol contract 缺少 event_registry。")
    if set(raw_registry) != {"client", "server"}:
        raise ProtocolRegistryError("event_registry 只能包含 client/server。")
    raw_client = raw_registry.get("client")
    raw_server = raw_registry.get("server")
    if not isinstance(raw_client, dict) or not isinstance(raw_server, dict):
        raise ProtocolRegistryError("event_registry 必须包含 client/server 对象。")
    _assert_exact_coverage(
        "client",
        set(raw_client),
        {str(event) for event in ClientEventType},
    )
    _assert_exact_coverage(
        "server",
        set(raw_server),
        {str(event) for event in ServerEventType},
    )
    raw_negotiation = document.get("negotiation")
    raw_capability_registry = document.get("event_capabilities")
    if not isinstance(raw_negotiation, dict) or not isinstance(
        raw_negotiation.get("capabilities"), list
    ):
        raise ProtocolRegistryError("protocol contract 缺少 negotiation capabilities。")
    if not isinstance(raw_capability_registry, dict):
        raise ProtocolRegistryError("protocol contract 缺少 event_capabilities。")
    published_capabilities = set(raw_negotiation["capabilities"])
    unknown_capabilities = sorted(set(raw_capability_registry) - published_capabilities)
    if unknown_capabilities:
        raise ProtocolRegistryError(
            f"event_capabilities 包含未发布能力：{unknown_capabilities}"
        )
    canonical = json.dumps(
        {
            "client": raw_client,
            "server": raw_server,
            "event_capabilities": raw_capability_registry,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    try:
        bindings = {
            name: EventCapabilityBinding.model_validate(value)
            for name, value in raw_capability_registry.items()
        }
        _validate_capability_bindings(bindings)
        return ProtocolEventRegistry(
            contract_version=document.get("version", 0),
            registry_sha256=hashlib.sha256(canonical).hexdigest(),
            client={name: EventPolicy.model_validate(value) for name, value in raw_client.items()},
            server={name: EventPolicy.model_validate(value) for name, value in raw_server.items()},
            event_capabilities=bindings,
        )
    except (TypeError, ValueError) as exc:
        raise ProtocolRegistryError(f"event_registry 无效：{exc}") from exc


def _assert_exact_coverage(
    direction: EventDirection,
    actual: set[str],
    expected: set[str],
) -> None:
    missing = sorted(expected - actual)
    unknown = sorted(actual - expected)
    if missing or unknown:
        raise ProtocolRegistryError(
            f"{direction} event_registry 覆盖不完整：missing={missing} unknown={unknown}"
        )


def _validate_capability_bindings(
    bindings: Mapping[str, EventCapabilityBinding],
) -> None:
    expected = {
        "client": {str(event) for event in ClientEventType},
        "server": {str(event) for event in ServerEventType},
    }
    for direction, field in (
        ("client", "client_events"),
        ("server", "server_events"),
    ):
        owners: dict[str, str] = {}
        for capability, binding in bindings.items():
            for event_type in getattr(binding, field):
                if event_type not in expected[direction]:
                    raise ProtocolRegistryError(
                        f"event_capabilities {capability} 引用未注册 {direction} 事件：{event_type}"
                    )
                previous = owners.setdefault(event_type, capability)
                if previous != capability:
                    raise ProtocolRegistryError(
                        f"{direction} 事件 {event_type} 被多个能力重复绑定。"
                    )


def _contract_path() -> Path:
    source = (
        Path(__file__).resolve().parents[3]
        / "frontend"
        / "terminal-ui"
        / "protocol-contract.json"
    )
    installed = (
        Path(__file__).resolve().parents[1]
        / "frontend"
        / "terminal-ui"
        / "protocol-contract.json"
    )
    for candidate in (source, installed):
        if candidate.is_file():
            return candidate
    raise ProtocolRegistryError("未找到随程序发布的 protocol-contract.json。")


__all__ = [
    "EventCapabilityBinding",
    "EventPolicy",
    "ProtocolEventRegistry",
    "ProtocolRegistryError",
    "load_protocol_event_registry",
]
