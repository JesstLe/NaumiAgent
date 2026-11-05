"""Strict enrollment, capability and read envelopes for existing local runtimes."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from naumi_agent.personal.item_models import canonical, digest
from naumi_agent.personal.models import PersonalAgentError

SHA = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Instance = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
Actor = Annotated[str, Field(pattern=r"^(main|pa_[a-f0-9]{32})$")]
GrantId = Annotated[str, Field(pattern=r"^wg_[a-f0-9]{32}$")]
ConnectionId = Annotated[str, Field(pattern=r"^wc_[a-f0-9]{32}$")]
ItemId = Annotated[str, Field(pattern=r"^pi_[a-f0-9]{32}$")]
RequestId = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")]
Revision = Annotated[int, Field(strict=True, ge=1, le=2**53 - 1)]
Scope = Literal["items", "results"]
MAX_REQUEST_BYTES = 256 * 1024
MAX_BODY_BYTES = 1024 * 1024


def error(code="source_unavailable"):
    messages = {
        "invalid_input": "工作区连接字段不完整或格式不正确",
        "scope_denied": "请由用户确认工作区读取授权",
        "source_unavailable": "原工作区当前不可读，请核对服务与资料授权",
        "source_stale": "原工作区身份或授权版本已变化，请重新核对连接",
        "grant_revoked": "工作区读取授权已撤销或已到期",
        "version_conflict": "连接或 Agent 配置已变化，请刷新后核对",
        "not_found": "这条工作区连接或原结果不在当前范围",
        "storage_error": "工作区连接与原资料库不一致，请恢复原目录后继续",
        "input_limit": "原结果超过本次读取容量，请在原工作区查看",
        "workspace_busy": "工作区连接正在更新，请稍后核对原请求",
        "idempotency_conflict": "同一请求编号已经绑定其他内容，请核对原请求",
        "workspace_unknown": "操作结果待核对，请查询原请求编号",
        "credential_error": "系统凭据库暂未就绪，请核对系统凭据服务",
        "credential_missing": "工作区连接凭据已缺失，请重新连接",
        "vault_corrupt": "工作区连接凭据校验未通过，请重新连接",
        "configuration_error": "工作区汇总尚未就绪，请核对运行时配置",
    }
    return PersonalAgentError(
        code if code in messages else "workspace_unknown",
        messages.get(code, messages["workspace_unknown"]),
    )


class Strict(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class Recipient(Strict):
    version: Literal[1]
    owner_key: SHA
    workspace_key: SHA
    session_db_key: SHA
    directory_instance_id: Instance
    actor_id: Actor
    actor_version: Revision


class Source(Strict):
    version: Literal[1]
    owner_key: SHA
    workspace_key: SHA
    session_db_key: SHA
    directory_instance_id: Instance
    item_instance_id: Instance


class Ticket(Strict):
    version: Literal[1]
    grant_id: GrantId
    grant_revision: Literal[1]
    source: Source
    recipient: Recipient
    source_actor_id: Actor
    source_actor_version: Revision
    scopes: list[Scope] = Field(min_length=1, max_length=2)
    expires_at: str = Field(min_length=1, max_length=40)
    secret: SHA

    @model_validator(mode="after")
    def unique_scopes(self):
        if len(self.scopes) != len(set(self.scopes)) or "items" not in self.scopes:
            raise ValueError("读取范围重复或缺少事项")
        return self


class IssuePayload(Strict):
    request_id: RequestId
    recipient: Recipient
    source_actor_id: Actor
    source_actor_version: Revision
    scopes: list[Scope] = Field(min_length=1, max_length=2)
    expires_at: str = Field(min_length=1, max_length=40)

    @model_validator(mode="after")
    def unique_scopes(self):
        if len(self.scopes) != len(set(self.scopes)) or "items" not in self.scopes:
            raise ValueError("读取范围重复或缺少事项")
        return self


class RevokePayload(Strict):
    request_id: RequestId
    grant_id: GrantId
    expected_revision: Revision


class ConnectPayload(Strict):
    request_id: RequestId
    endpoint: str = Field(min_length=1, max_length=128)
    label: str = Field(min_length=1, max_length=80)
    ticket: Ticket


class DisconnectPayload(Strict):
    request_id: RequestId
    connection_id: ConnectionId
    expected_revision: Revision


class ReadRef(Strict):
    read_id: Annotated[str, Field(pattern=r"^ws_[a-f0-9]{32}$")]
    revision: Literal[1]
    digest: SHA
    body_sha256: SHA


class WorkspaceReadRef(Strict):
    read_id: Annotated[str, Field(pattern=r"^wx_[a-f0-9]{32}$")]
    revision: Literal[1]
    digest: SHA
    workspace_key: SHA


class FederationRequest(Strict):
    operation: Literal["inspect", "page", "result", "validate"]
    grant_id: GrantId
    recipient: Recipient
    after: str | None = Field(default=None, max_length=40)
    limit: Annotated[int, Field(strict=True, ge=1, le=100)] = 50
    item_id: ItemId | None = None
    read_ref: ReadRef | None = None

    @model_validator(mode="after")
    def exact_fields(self):
        allowed = {
            "inspect": set(),
            "page": {"after", "limit"},
            "result": {"item_id"},
            "validate": {"read_ref"},
        }[self.operation]
        if self.model_fields_set - {"operation", "grant_id", "recipient"} - allowed:
            raise ValueError("读取字段不匹配")
        if self.operation == "result" and self.item_id is None:
            raise ValueError("缺少原事项")
        if self.operation == "validate" and self.read_ref is None:
            raise ValueError("缺少原读取凭据")
        if self.after is not None and not re.fullmatch(r"pi_[a-f0-9]{32}", self.after):
            raise ValueError("分页编号无效")
        return self


def normalize(model, value):
    try:
        if len(canonical(value).encode("utf-8")) > MAX_REQUEST_BYTES:
            raise ValueError()
        result = model.model_validate(value).model_dump(exclude_unset=True)

        # Literal[1] in pydantic accepts bool and 1.0; wire versions never do.
        def strict_versions(node):
            if isinstance(node, dict):
                for key, child in node.items():
                    if key in {"version", "grant_revision", "revision"} and type(child) is not int:
                        raise ValueError()
                    strict_versions(child)
            elif isinstance(node, list):
                for child in node:
                    strict_versions(child)

        strict_versions(value)
        return result
    except (ValidationError, TypeError, ValueError, UnicodeError, RecursionError):
        raise error("invalid_input") from None


def endpoint_url(value):
    if type(value) is not str or len(value) > 128:
        raise error("invalid_input")
    try:
        parts = urlsplit(value)
        port = parts.port
        if (
            parts.scheme != "http"
            or parts.hostname not in {"127.0.0.1", "::1"}
            or parts.username is not None
            or parts.password is not None
            or not port
            or not 1 <= port <= 65535
            or parts.path not in {"", "/"}
            or parts.query
            or parts.fragment
        ):
            raise ValueError()
        host = "[::1]" if parts.hostname == "::1" else "127.0.0.1"
        canonical_url = f"http://{host}:{port}"
        if value not in {canonical_url, canonical_url + "/"}:
            raise ValueError()
        return canonical_url
    except ValueError:
        raise error("invalid_input") from None


def timestamp(value):
    try:
        if type(value) is not str or len(value) > 40:
            raise ValueError()
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
            raise ValueError()
        return parsed.astimezone(UTC)
    except (ValueError, TypeError, AttributeError, OverflowError):
        raise error("invalid_input") from None


def identity(prefix, value):
    return prefix + digest(value)[:32]


def grant_view(value):
    return {
        key: value[key]
        for key in (
            "id",
            "revision",
            "status",
            "source",
            "recipient",
            "source_actor_id",
            "source_actor_version",
            "scopes",
            "expires_at",
            "created_at",
        )
    }


def connection_view(value):
    return {
        key: value[key]
        for key in (
            "id",
            "revision",
            "status",
            "label",
            "endpoint",
            "source",
            "recipient",
            "source_actor_id",
            "source_actor_version",
            "scopes",
            "expires_at",
        )
    }
