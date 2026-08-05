"""Human-attested approval principals and Ed25519 public-key lifecycle."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import re
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Self

import aiosqlite
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, Field, model_validator

from naumi_agent.evolution.approval_requirements import EvolutionPromotionApprovalRole
from naumi_agent.harness.interaction import HarnessInteractionRecord
from naumi_agent.harness.store import HarnessStore, HarnessStoreError
from naumi_agent.user_interaction import (
    UserInteractionRequest,
    UserInteractionUnavailableError,
    normalize_interaction_request,
)

EVOLUTION_APPROVAL_PRINCIPAL_POLICY = "evolution-approval-principal-v1"
_SHA256_RE = r"^[0-9a-f]{64}$"
_PRINCIPAL_NAME_RE = re.compile(r"^[a-z][a-z0-9._-]{1,63}$")
_INTERACTION_RE = re.compile(
    r"^ask-evprincipal-([0-9a-f]{24})-"
    r"(register|rotate_key|update_roles|revoke)-([1-9][0-9]{0,3})$"
)
_MAX_EVENT_BYTES = 256 * 1_024
_ROLE_ORDER = {
    role: index
    for index, role in enumerate(
        (
            EvolutionPromotionApprovalRole.USER,
            EvolutionPromotionApprovalRole.INDEPENDENT_REVIEWER,
            EvolutionPromotionApprovalRole.SECURITY_REVIEWER,
            EvolutionPromotionApprovalRole.DATA_OWNER,
            EvolutionPromotionApprovalRole.RELEASE_MANAGER,
        )
    )
}

RequestUserInputCallback = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class EvolutionApprovalPrincipalAction(StrEnum):
    REGISTER = "register"
    ROTATE_KEY = "rotate_key"
    UPDATE_ROLES = "update_roles"
    REVOKE = "revoke"


class EvolutionApprovalPrincipalState(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvolutionApprovalPrincipalEvent(_StrictModel):
    schema_version: Literal[1] = 1
    policy_version: Literal["evolution-approval-principal-v1"] = (
        EVOLUTION_APPROVAL_PRINCIPAL_POLICY
    )
    event_id: str = Field(pattern=r"^evprincipalevent_[0-9a-f]{24}$")
    event_sha256: str = Field(pattern=_SHA256_RE)
    workspace_root: str = Field(min_length=1, max_length=4_096)
    principal_id: str = Field(pattern=r"^evprincipal_[0-9a-f]{24}$")
    principal_name: str = Field(pattern=r"^[a-z][a-z0-9._-]{1,63}$")
    sequence: int = Field(ge=1, le=10_000)
    action: EvolutionApprovalPrincipalAction
    state: EvolutionApprovalPrincipalState
    roles: tuple[EvolutionPromotionApprovalRole, ...] = Field(
        min_length=1,
        max_length=5,
    )
    key_id: str = Field(pattern=r"^evapprovalkey_[0-9a-f]{24}$")
    key_generation: int = Field(ge=1, le=10_000)
    signature_algorithm: Literal["ed25519"] = "ed25519"
    public_key_base64: str = Field(min_length=44, max_length=44)
    public_key_sha256: str = Field(pattern=_SHA256_RE)
    previous_event_sha256: str = Field(pattern=r"^(?:|[0-9a-f]{64})$")
    interaction: HarnessInteractionRecord
    registered_at: str = Field(min_length=1, max_length=100)
    updated_at: str = Field(min_length=1, max_length=100)
    revoked_at: str = Field(max_length=100)
    trust_source: Literal["local_user_attestation"] = "local_user_attestation"
    role_binding_verified: Literal[True] = True
    private_key_stored: Literal[False] = False
    private_key_requested: Literal[False] = False
    automatic_role_assignment: Literal[False] = False
    approval_decision_authority: Literal[False] = False
    promotion_authority: Literal[False] = False
    git_write_executed: Literal[False] = False
    contains_secret_material: Literal[False] = False
    llm_generated: Literal[False] = False

    @model_validator(mode="after")
    def _event_is_exact(self) -> Self:
        if self.workspace_root != str(Path(self.workspace_root).expanduser().resolve()):
            raise ValueError("Approval Principal workspace 必须是 canonical 路径。")
        if self.principal_id != _principal_id(self.workspace_root, self.principal_name):
            raise ValueError("Approval Principal identity 不一致。")
        if self.roles != _normalize_roles(self.roles):
            raise ValueError("Approval Principal roles 必须按策略排序且不得重复。")
        public_key = _decode_public_key(self.public_key_base64)
        if not hmac.compare_digest(self.public_key_sha256, hashlib.sha256(public_key).hexdigest()):
            raise ValueError("Approval Principal public key 摘要不一致。")
        expected_key_id = _key_id(
            principal_id=self.principal_id,
            generation=self.key_generation,
            public_key_sha256=self.public_key_sha256,
        )
        if self.key_id != expected_key_id:
            raise ValueError("Approval Principal key identity 不一致。")
        if self.sequence == 1:
            if (
                self.action is not EvolutionApprovalPrincipalAction.REGISTER
                or self.previous_event_sha256
                or self.key_generation != 1
            ):
                raise ValueError("Approval Principal 首事件必须是 register。")
        elif not self.previous_event_sha256:
            raise ValueError("Approval Principal 后续事件必须链接前序摘要。")
        if self.state is EvolutionApprovalPrincipalState.REVOKED:
            if self.action is not EvolutionApprovalPrincipalAction.REVOKE or not self.revoked_at:
                raise ValueError("Approval Principal revoked 状态投影不一致。")
            if _aware(self.revoked_at) != _aware(self.updated_at):
                raise ValueError("Approval Principal revoked_at 必须等于 updated_at。")
        elif self.revoked_at or self.action is EvolutionApprovalPrincipalAction.REVOKE:
            raise ValueError("Approval Principal active 状态不能携带 revoke 事实。")
        if _aware(self.updated_at) != _aware(self.interaction.answered_at):
            raise ValueError("Approval Principal updated_at 必须来自 interaction。")
        if _aware(self.registered_at) > _aware(self.updated_at):
            raise ValueError("Approval Principal registered_at 不能晚于 updated_at。")
        _require_approved_interaction(self)
        digest = _sha256_payload(
            self.model_dump(mode="json", exclude={"event_id", "event_sha256"})
        )
        if not hmac.compare_digest(self.event_sha256, digest):
            raise ValueError("Approval Principal Event 摘要不一致。")
        if self.event_id != f"evprincipalevent_{digest[:24]}":
            raise ValueError("Approval Principal Event identity 不一致。")
        return self


class EvolutionApprovalPrincipalView(_StrictModel):
    principal: EvolutionApprovalPrincipalEvent
    active: bool
    signature_verification_eligible: bool

    @model_validator(mode="after")
    def _view_is_exact(self) -> Self:
        expected = self.principal.state is EvolutionApprovalPrincipalState.ACTIVE
        if self.active is not expected or self.signature_verification_eligible is not expected:
            raise ValueError("Approval Principal view 投影不一致。")
        return self


class EvolutionApprovalPrincipalGovernanceResult(_StrictModel):
    action: EvolutionApprovalPrincipalAction
    interaction: HarnessInteractionRecord
    approved: bool
    principal: EvolutionApprovalPrincipalView | None = None
    authority_changed: bool
    private_key_stored: Literal[False] = False
    promotion_authority: Literal[False] = False

    @model_validator(mode="after")
    def _result_is_exact(self) -> Self:
        approved = bool(
            self.interaction.state == "answered"
            and self.interaction.answer_kind == "option"
            and self.interaction.answer_value == "approve"
        )
        if self.approved is not approved:
            raise ValueError("Approval Principal governance result 投影不一致。")
        if approved != (self.principal is not None):
            raise ValueError("Approval Principal authority 投影不一致。")
        if self.authority_changed and not approved:
            raise ValueError("未批准的 Principal proposal 不能改变 authority。")
        return self


class EvolutionApprovalPrincipalError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionApprovalPrincipalEventBuilder:
    def build(
        self,
        *,
        workspace_root: str | Path,
        principal_name: str,
        action: EvolutionApprovalPrincipalAction,
        roles: Sequence[EvolutionPromotionApprovalRole | str],
        public_key_base64: str,
        interaction: HarnessInteractionRecord,
        previous: EvolutionApprovalPrincipalEvent | None = None,
    ) -> EvolutionApprovalPrincipalEvent:
        try:
            workspace = str(Path(workspace_root).expanduser().resolve(strict=True))
            name = _normalize_principal_name(principal_name)
            normalized_roles = _normalize_roles(roles)
            normalized_key = _normalize_public_key(public_key_base64)
            answered = HarnessInteractionRecord.model_validate_json(interaction.model_dump_json())
            _validate_transition_inputs(
                workspace_root=workspace,
                principal_name=name,
                action=action,
                roles=normalized_roles,
                public_key_base64=normalized_key,
                previous=previous,
            )
            principal_id = _principal_id(workspace, name)
            request = _governance_request(
                action=action,
                principal_id=principal_id,
                principal_name=name,
                roles=normalized_roles,
                public_key_base64=normalized_key,
                timeout_seconds=None,
            )
            if not _interaction_matches(
                answered,
                request=request,
                principal_id=principal_id,
                action=action,
            ):
                raise ValueError("Governance interaction 未绑定 exact proposal。")
            if answered.answer_value != "approve":
                raise ValueError("只有用户明确 approve 才能写入 Principal authority。")
        except EvolutionApprovalPrincipalError:
            raise
        except (AttributeError, OSError, TypeError, ValueError) as exc:
            raise EvolutionApprovalPrincipalError(
                "approval_principal_event_input_invalid",
                "Approval Principal Event 输入无效。",
            ) from exc
        public_key = _decode_public_key(normalized_key)
        key_sha256 = hashlib.sha256(public_key).hexdigest()
        sequence = 1 if previous is None else previous.sequence + 1
        generation = (
            1
            if previous is None
            else previous.key_generation
            + (1 if action is EvolutionApprovalPrincipalAction.ROTATE_KEY else 0)
        )
        state = (
            EvolutionApprovalPrincipalState.REVOKED
            if action is EvolutionApprovalPrincipalAction.REVOKE
            else EvolutionApprovalPrincipalState.ACTIVE
        )
        registered_at = (
            answered.answered_at if previous is None else previous.registered_at
        )
        core = {
            "schema_version": 1,
            "policy_version": EVOLUTION_APPROVAL_PRINCIPAL_POLICY,
            "workspace_root": workspace,
            "principal_id": principal_id,
            "principal_name": name,
            "sequence": sequence,
            "action": action.value,
            "state": state.value,
            "roles": [role.value for role in normalized_roles],
            "key_id": _key_id(
                principal_id=principal_id,
                generation=generation,
                public_key_sha256=key_sha256,
            ),
            "key_generation": generation,
            "signature_algorithm": "ed25519",
            "public_key_base64": normalized_key,
            "public_key_sha256": key_sha256,
            "previous_event_sha256": "" if previous is None else previous.event_sha256,
            "interaction": answered.model_dump(mode="json"),
            "registered_at": registered_at,
            "updated_at": answered.answered_at,
            "revoked_at": (
                answered.answered_at
                if action is EvolutionApprovalPrincipalAction.REVOKE
                else ""
            ),
            "trust_source": "local_user_attestation",
            "role_binding_verified": True,
            "private_key_stored": False,
            "private_key_requested": False,
            "automatic_role_assignment": False,
            "approval_decision_authority": False,
            "promotion_authority": False,
            "git_write_executed": False,
            "contains_secret_material": False,
            "llm_generated": False,
        }
        digest = _sha256_payload(core)
        try:
            return EvolutionApprovalPrincipalEvent.model_validate_json(
                json.dumps(
                    {
                        **core,
                        "event_id": f"evprincipalevent_{digest[:24]}",
                        "event_sha256": digest,
                    },
                    ensure_ascii=False,
                )
            )
        except ValueError as exc:
            raise EvolutionApprovalPrincipalError(
                "approval_principal_event_invalid",
                "Approval Principal Event 无法验证。",
            ) from exc


class EvolutionApprovalPrincipalStore:
    def __init__(self, db_path: str | Path, *, interaction_store: HarnessStore) -> None:
        self._db_path = Path(db_path).expanduser().resolve()
        if not isinstance(interaction_store, HarnessStore):
            raise TypeError("Approval Principal Store 需要 Harness Store。")
        self._interaction_store = interaction_store

    async def record(
        self,
        event: EvolutionApprovalPrincipalEvent,
    ) -> EvolutionApprovalPrincipalEvent:
        try:
            item = EvolutionApprovalPrincipalEvent.model_validate_json(event.model_dump_json())
            authoritative = await self._interaction_store.get_interaction(
                workspace_root=item.workspace_root,
                interaction_id=item.interaction.interaction_id,
            )
        except (HarnessStoreError, OSError, TypeError, ValueError) as exc:
            raise EvolutionApprovalPrincipalError(
                "approval_principal_interaction_read_failed",
                "无法重读 Principal governance interaction authority。",
            ) from exc
        if authoritative is None or authoritative != item.interaction:
            raise EvolutionApprovalPrincipalError(
                "approval_principal_interaction_mismatch",
                "Principal Event 未绑定 exact Harness interaction authority。",
            )
        encoded = item.model_dump_json()
        if len(encoded.encode("utf-8")) > _MAX_EVENT_BYTES:
            raise EvolutionApprovalPrincipalError(
                "approval_principal_event_oversized",
                "Approval Principal Event 超过 256 KiB 上限。",
            )
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                await db.execute("BEGIN IMMEDIATE")
                existing = await (
                    await db.execute(
                        "SELECT event_json FROM evolution_approval_principal_events "
                        "WHERE event_id = ?",
                        (item.event_id,),
                    )
                ).fetchone()
                if existing is not None:
                    restored = EvolutionApprovalPrincipalEvent.model_validate_json(
                        str(existing["event_json"])
                    )
                    if restored != item:
                        await db.rollback()
                        raise EvolutionApprovalPrincipalError(
                            "approval_principal_event_conflict",
                            "Principal Event ID 已绑定不同内容。",
                        )
                    await db.rollback()
                    return restored
                previous = await _latest_with_connection(
                    db,
                    workspace_root=item.workspace_root,
                    principal_id=item.principal_id,
                )
                _validate_event_transition(item, previous)
                await db.execute(
                    "INSERT INTO evolution_approval_principal_events "
                    "(event_id, event_sha256, workspace_root, principal_id, sequence, "
                    "action, state, key_id, interaction_id, event_json, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        item.event_id,
                        item.event_sha256,
                        item.workspace_root,
                        item.principal_id,
                        item.sequence,
                        item.action.value,
                        item.state.value,
                        item.key_id,
                        item.interaction.interaction_id,
                        encoded,
                        item.updated_at,
                    ),
                )
                await db.execute(
                    "INSERT INTO evolution_approval_principals "
                    "(workspace_root, principal_id, principal_name, latest_sequence, state, "
                    "latest_event_id, latest_event_sha256, key_id, key_generation, "
                    "public_key_sha256, roles_json, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(workspace_root, principal_id) DO UPDATE SET "
                    "latest_sequence=excluded.latest_sequence, state=excluded.state, "
                    "latest_event_id=excluded.latest_event_id, "
                    "latest_event_sha256=excluded.latest_event_sha256, "
                    "key_id=excluded.key_id, key_generation=excluded.key_generation, "
                    "public_key_sha256=excluded.public_key_sha256, "
                    "roles_json=excluded.roles_json, updated_at=excluded.updated_at",
                    (
                        item.workspace_root,
                        item.principal_id,
                        item.principal_name,
                        item.sequence,
                        item.state.value,
                        item.event_id,
                        item.event_sha256,
                        item.key_id,
                        item.key_generation,
                        item.public_key_sha256,
                        json.dumps([role.value for role in item.roles]),
                        item.updated_at,
                    ),
                )
                await db.commit()
        except EvolutionApprovalPrincipalError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionApprovalPrincipalError(
                "approval_principal_store_error",
                "Approval Principal Event 无法持久化。",
            ) from exc
        restored = await self.get(item.workspace_root, item.principal_id)
        assert restored is not None
        return restored

    async def get(
        self,
        workspace_root: str | Path,
        principal_id: str,
    ) -> EvolutionApprovalPrincipalEvent | None:
        workspace = str(Path(workspace_root).expanduser().resolve())
        if re.fullmatch(r"evprincipal_[0-9a-f]{24}", str(principal_id)) is None:
            raise ValueError("approval principal id 格式无效。")
        if not self._db_path.is_file():
            return None
        try:
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                await _ensure_schema(db)
                return await _latest_with_connection(
                    db,
                    workspace_root=workspace,
                    principal_id=principal_id,
                )
        except EvolutionApprovalPrincipalError:
            raise
        except (aiosqlite.Error, OSError, TypeError, ValueError) as exc:
            raise EvolutionApprovalPrincipalError(
                "approval_principal_store_corrupt",
                "Approval Principal authority 损坏或无法读取。",
            ) from exc

    async def get_by_name(
        self,
        workspace_root: str | Path,
        principal_name: str,
    ) -> EvolutionApprovalPrincipalEvent | None:
        workspace = str(Path(workspace_root).expanduser().resolve())
        name = _normalize_principal_name(principal_name)
        return await self.get(workspace, _principal_id(workspace, name))


class EvolutionApprovalPrincipalService:
    """Require one durable human attestation before changing principal authority."""

    def __init__(
        self,
        *,
        store: EvolutionApprovalPrincipalStore,
        interaction_store: HarnessStore,
        request_user_input: RequestUserInputCallback,
        builder: EvolutionApprovalPrincipalEventBuilder | None = None,
        interaction_timeout_seconds: int = 3_600,
    ) -> None:
        if not isinstance(store, EvolutionApprovalPrincipalStore):
            raise TypeError("Approval Principal service 需要 Principal Store。")
        if not isinstance(interaction_store, HarnessStore):
            raise TypeError("Approval Principal service 需要 Harness Store。")
        if not callable(request_user_input):
            raise TypeError("Approval Principal service 需要用户交互 callback。")
        if not 3 <= interaction_timeout_seconds <= 86_400:
            raise ValueError("Principal governance timeout 必须在 3..86400 秒之间。")
        self._store = store
        self._interaction_store = interaction_store
        self._request_user_input = request_user_input
        self._builder = builder or EvolutionApprovalPrincipalEventBuilder()
        self._interaction_timeout_seconds = interaction_timeout_seconds
        self._locks: dict[str, asyncio.Lock] = {}

    async def register(
        self,
        *,
        workspace_root: str | Path,
        principal_name: str,
        roles: Sequence[EvolutionPromotionApprovalRole | str],
        public_key_base64: str,
    ) -> EvolutionApprovalPrincipalGovernanceResult:
        workspace = _workspace(workspace_root)
        name = _normalize_principal_name(principal_name)
        return await self._govern(
            workspace_root=workspace,
            principal_id=_principal_id(workspace, name),
            principal_name=name,
            action=EvolutionApprovalPrincipalAction.REGISTER,
            roles=_normalize_roles(roles),
            public_key_base64=_normalize_public_key(public_key_base64),
        )

    async def rotate_key(
        self,
        *,
        workspace_root: str | Path,
        principal_id: str,
        public_key_base64: str,
    ) -> EvolutionApprovalPrincipalGovernanceResult:
        current = await self._require_active(workspace_root, principal_id)
        return await self._govern(
            workspace_root=current.workspace_root,
            principal_id=current.principal_id,
            principal_name=current.principal_name,
            action=EvolutionApprovalPrincipalAction.ROTATE_KEY,
            roles=current.roles,
            public_key_base64=_normalize_public_key(public_key_base64),
        )

    async def update_roles(
        self,
        *,
        workspace_root: str | Path,
        principal_id: str,
        roles: Sequence[EvolutionPromotionApprovalRole | str],
    ) -> EvolutionApprovalPrincipalGovernanceResult:
        current = await self._require_active(workspace_root, principal_id)
        return await self._govern(
            workspace_root=current.workspace_root,
            principal_id=current.principal_id,
            principal_name=current.principal_name,
            action=EvolutionApprovalPrincipalAction.UPDATE_ROLES,
            roles=_normalize_roles(roles),
            public_key_base64=current.public_key_base64,
        )

    async def revoke(
        self,
        *,
        workspace_root: str | Path,
        principal_id: str,
    ) -> EvolutionApprovalPrincipalGovernanceResult:
        current = await self.inspect(workspace_root=workspace_root, principal_id=principal_id)
        if not current.active:
            return _idempotent_governance_result(
                current.principal,
                requested_action=EvolutionApprovalPrincipalAction.REVOKE,
            )
        return await self._govern(
            workspace_root=current.principal.workspace_root,
            principal_id=current.principal.principal_id,
            principal_name=current.principal.principal_name,
            action=EvolutionApprovalPrincipalAction.REVOKE,
            roles=current.principal.roles,
            public_key_base64=current.principal.public_key_base64,
        )

    async def inspect(
        self,
        *,
        workspace_root: str | Path,
        principal_id: str,
    ) -> EvolutionApprovalPrincipalView:
        try:
            current = await self._store.get(workspace_root, principal_id)
        except (EvolutionApprovalPrincipalError, OSError, TypeError, ValueError) as exc:
            raise EvolutionApprovalPrincipalError(
                "approval_principal_read_failed",
                "无法读取 Approval Principal authority。",
            ) from exc
        if current is None:
            raise EvolutionApprovalPrincipalError(
                "approval_principal_missing",
                "Approval Principal 不存在。",
            )
        return _view(current)

    async def _require_active(
        self,
        workspace_root: str | Path,
        principal_id: str,
    ) -> EvolutionApprovalPrincipalEvent:
        view = await self.inspect(workspace_root=workspace_root, principal_id=principal_id)
        if not view.active:
            raise EvolutionApprovalPrincipalError(
                "approval_principal_revoked",
                "Approval Principal 已撤销，不能继续变更。",
            )
        return view.principal

    async def _govern(
        self,
        *,
        workspace_root: str,
        principal_id: str,
        principal_name: str,
        action: EvolutionApprovalPrincipalAction,
        roles: tuple[EvolutionPromotionApprovalRole, ...],
        public_key_base64: str,
    ) -> EvolutionApprovalPrincipalGovernanceResult:
        lock_key = f"{workspace_root}::{principal_id}"
        lock = self._locks.setdefault(lock_key, asyncio.Lock())
        async with lock:
            return await self._govern_locked(
                workspace_root=workspace_root,
                principal_id=principal_id,
                principal_name=principal_name,
                action=action,
                roles=roles,
                public_key_base64=public_key_base64,
            )

    async def _govern_locked(
        self,
        *,
        workspace_root: str,
        principal_id: str,
        principal_name: str,
        action: EvolutionApprovalPrincipalAction,
        roles: tuple[EvolutionPromotionApprovalRole, ...],
        public_key_base64: str,
    ) -> EvolutionApprovalPrincipalGovernanceResult:
        current = await self._store.get(workspace_root, principal_id)
        idempotent = _idempotent_current(
            current,
            action=action,
            roles=roles,
            public_key_base64=public_key_base64,
        )
        if idempotent is not None:
            return _idempotent_governance_result(
                idempotent,
                requested_action=action,
            )
        _validate_transition_inputs(
            workspace_root=workspace_root,
            principal_name=principal_name,
            action=action,
            roles=roles,
            public_key_base64=public_key_base64,
            previous=current,
        )
        request = _governance_request(
            action=action,
            principal_id=principal_id,
            principal_name=principal_name,
            roles=roles,
            public_key_base64=public_key_base64,
            timeout_seconds=self._interaction_timeout_seconds,
        )
        history = await self._interaction_history(
            workspace_root=workspace_root,
            principal_id=principal_id,
            action=action,
            request=request,
        )
        answered = tuple(item for item in history if item.state == "answered")
        if len(answered) > 1:
            raise EvolutionApprovalPrincipalError(
                "approval_principal_answer_ambiguous",
                "同一 Principal proposal 存在多个已回答交互。",
            )
        if answered:
            return await self._consume_answer(
                workspace_root=workspace_root,
                principal_name=principal_name,
                action=action,
                roles=roles,
                public_key_base64=public_key_base64,
                current=current,
                interaction=answered[0],
            )
        pending = next((item for item in history if item.state == "pending"), None)
        if pending is not None:
            raise EvolutionApprovalPrincipalError(
                "approval_principal_interaction_pending",
                f"Principal governance 交互 {pending.interaction_id} 仍待回答。",
            )
        interaction_id = _next_interaction_id(principal_id, action, history)
        payload = {
            **request.to_public_dict(),
            "priority": "high",
            "_interaction_id": interaction_id,
            "_durable_subject_kind": "tool",
            "_durable_subject_id": principal_id,
        }
        try:
            await self._request_user_input(payload)
        except (HarnessStoreError, UserInteractionUnavailableError) as exc:
            raise EvolutionApprovalPrincipalError(
                "approval_principal_interaction_unavailable",
                "当前界面无法创建持久 Principal governance 交互。",
            ) from exc
        except ValueError as exc:
            raise EvolutionApprovalPrincipalError(
                "approval_principal_interaction_invalid",
                "Principal governance 交互未通过运行时协议校验。",
            ) from exc
        try:
            interaction = await self._interaction_store.get_interaction(
                workspace_root=workspace_root,
                interaction_id=interaction_id,
            )
        except (HarnessStoreError, OSError, TypeError, ValueError) as exc:
            raise EvolutionApprovalPrincipalError(
                "approval_principal_interaction_read_failed",
                "无法重读 Principal governance interaction authority。",
            ) from exc
        if interaction is None or interaction.state != "answered":
            raise EvolutionApprovalPrincipalError(
                "approval_principal_answer_not_committed",
                "用户答案尚未提交到 Harness authority。",
            )
        refreshed = await self._store.get(workspace_root, principal_id)
        if refreshed != current:
            raise EvolutionApprovalPrincipalError(
                "approval_principal_authority_changed",
                "Principal authority 在用户回答期间已变化，请重新发起治理。",
            )
        return await self._consume_answer(
            workspace_root=workspace_root,
            principal_name=principal_name,
            action=action,
            roles=roles,
            public_key_base64=public_key_base64,
            current=current,
            interaction=interaction,
        )

    async def _consume_answer(
        self,
        *,
        workspace_root: str,
        principal_name: str,
        action: EvolutionApprovalPrincipalAction,
        roles: tuple[EvolutionPromotionApprovalRole, ...],
        public_key_base64: str,
        current: EvolutionApprovalPrincipalEvent | None,
        interaction: HarnessInteractionRecord,
    ) -> EvolutionApprovalPrincipalGovernanceResult:
        if interaction.answer_value != "approve":
            return EvolutionApprovalPrincipalGovernanceResult(
                action=action,
                interaction=interaction,
                approved=False,
                principal=None,
                authority_changed=False,
            )
        event = self._builder.build(
            workspace_root=workspace_root,
            principal_name=principal_name,
            action=action,
            roles=roles,
            public_key_base64=public_key_base64,
            interaction=interaction,
            previous=current,
        )
        stored = await self._store.record(event)
        return EvolutionApprovalPrincipalGovernanceResult(
            action=action,
            interaction=interaction,
            approved=True,
            principal=_view(stored),
            authority_changed=True,
        )

    async def _interaction_history(
        self,
        *,
        workspace_root: str,
        principal_id: str,
        action: EvolutionApprovalPrincipalAction,
        request: UserInteractionRequest,
    ) -> tuple[HarnessInteractionRecord, ...]:
        try:
            records = await self._interaction_store.list_interactions(
                workspace_root=workspace_root,
                subject_kind="tool",
                subject_ids=(principal_id,),
                limit=100,
            )
        except (HarnessStoreError, OSError, TypeError, ValueError) as exc:
            raise EvolutionApprovalPrincipalError(
                "approval_principal_interaction_history_failed",
                "无法读取 Principal governance 交互历史。",
            ) from exc
        return tuple(
            item
            for item in records
            if _interaction_matches(
                item,
                request=request,
                principal_id=principal_id,
                action=action,
            )
        )


def render_evolution_approval_principal(
    value: EvolutionApprovalPrincipalGovernanceResult | EvolutionApprovalPrincipalView,
) -> str:
    if isinstance(value, EvolutionApprovalPrincipalGovernanceResult):
        result = EvolutionApprovalPrincipalGovernanceResult.model_validate(
            value.model_dump(mode="python")
        )
        if not result.approved:
            return "\n".join(
                [
                    "# Evolution Approval Principal",
                    "",
                    "**用户已拒绝本次 Principal governance proposal；authority 未变化。**",
                    "",
                    f"- Action：`{result.action.value}`",
                    f"- Interaction：`{result.interaction.interaction_id}`",
                    "- Private key stored：`false`",
                    "- Promotion authority：`false`",
                ]
            )
        assert result.principal is not None
        view = result.principal
        headline = (
            "用户已确认 Principal authority 变更。"
            if result.authority_changed
            else "该 Principal proposal 已生效，authority 未重复写入。"
        )
    else:
        view = EvolutionApprovalPrincipalView.model_validate(value.model_dump(mode="python"))
        headline = "当前 Principal authority。"
    item = view.principal
    return "\n".join(
        [
            f"# Evolution Approval Principal `{item.principal_id}`",
            "",
            f"**{headline}私钥从未进入 Naumi。**",
            "",
            f"- Name：`{item.principal_name}`",
            f"- State：`{item.state.value}` · sequence {item.sequence}",
            f"- Roles：{', '.join(f'`{role.value}`' for role in item.roles)}",
            f"- Key：`{item.key_id}` · generation {item.key_generation}",
            f"- Algorithm：`{item.signature_algorithm}`",
            f"- Public key SHA-256：`{item.public_key_sha256}`",
            f"- Trust source：`{item.trust_source}`",
            "- Eligible for future signature verification："
            f"{'是' if view.signature_verification_eligible else '否'}",
            "- Private key stored/requested：`false`",
            "- Approval decision/Promotion/Git：`false`",
            f"- Latest Event SHA-256：`{item.event_sha256}`",
            "",
            "下一步：EVO-05.2c2 只接受由 active Principal 当前 key "
            "对 exact domain payload 的 Ed25519 签名。",
        ]
    )


def parse_approval_roles(value: str | Sequence[str]) -> tuple[EvolutionPromotionApprovalRole, ...]:
    raw = value.split(",") if isinstance(value, str) else value
    return _normalize_roles(tuple(item.strip() for item in raw if str(item).strip()))


def _governance_request(
    *,
    action: EvolutionApprovalPrincipalAction,
    principal_id: str,
    principal_name: str,
    roles: tuple[EvolutionPromotionApprovalRole, ...],
    public_key_base64: str,
    timeout_seconds: int | None,
) -> UserInteractionRequest:
    fingerprint = hashlib.sha256(_decode_public_key(public_key_base64)).hexdigest()
    labels = {
        EvolutionApprovalPrincipalAction.REGISTER: "注册审批身份",
        EvolutionApprovalPrincipalAction.ROTATE_KEY: "轮换审批公钥",
        EvolutionApprovalPrincipalAction.UPDATE_ROLES: "更新审批角色",
        EvolutionApprovalPrincipalAction.REVOKE: "撤销审批身份",
    }
    question = (
        f"是否{labels[action]} {principal_name}（{principal_id}）？"
        f"角色：{','.join(role.value for role in roles)}；"
        f"Ed25519 公钥指纹：{fingerprint}。"
        "只保存公钥与审计事实，Naumi 不会请求或保存私钥。"
    )
    return normalize_interaction_request(
        {
            "header": f"Principal 治理 · {action.value}",
            "question": question,
            "options": [
                {
                    "value": "approve",
                    "label": "确认本次治理变更",
                    "description": "写入 append-only Principal authority；不产生 Promotion 权限。",
                },
                {
                    "value": "reject",
                    "label": "拒绝本次治理变更",
                    "description": "保留 HAR 回答审计，Principal authority 不变化。",
                },
            ],
            "allow_custom": False,
            "custom_label": "不允许自定义治理文本",
            "timeout_seconds": timeout_seconds,
            "priority": "high",
        }
    )


def _interaction_matches(
    interaction: HarnessInteractionRecord,
    *,
    request: UserInteractionRequest,
    principal_id: str,
    action: EvolutionApprovalPrincipalAction,
) -> bool:
    match = _INTERACTION_RE.fullmatch(interaction.interaction_id)
    return bool(
        match is not None
        and interaction.subject_kind == "tool"
        and interaction.subject_id == principal_id
        and match.group(1) == principal_id.removeprefix("evprincipal_")
        and match.group(2) == action.value
        and _static_request_payload(
            interaction.request(),
            include_priority=interaction.schema_version != 1,
        ) == _static_request_payload(
            request,
            include_priority=interaction.schema_version != 1,
        )
        and interaction.request().timeout_seconds is not None
        and interaction.request().timeout_seconds <= 86_400
    )


def _require_approved_interaction(event: EvolutionApprovalPrincipalEvent) -> None:
    request = _governance_request(
        action=event.action,
        principal_id=event.principal_id,
        principal_name=event.principal_name,
        roles=event.roles,
        public_key_base64=event.public_key_base64,
        timeout_seconds=None,
    )
    if not _interaction_matches(
        event.interaction,
        request=request,
        principal_id=event.principal_id,
        action=event.action,
    ):
        raise ValueError("Principal Event interaction proposal 不一致。")
    if (
        event.interaction.state != "answered"
        or event.interaction.answer_kind != "option"
        or event.interaction.answer_value != "approve"
        or event.interaction.answered_by != "user"
        or not event.interaction.session_id
    ):
        raise ValueError("Principal Event 必须来自有会话的真实用户 approve。")


def _validate_transition_inputs(
    *,
    workspace_root: str,
    principal_name: str,
    action: EvolutionApprovalPrincipalAction,
    roles: tuple[EvolutionPromotionApprovalRole, ...],
    public_key_base64: str,
    previous: EvolutionApprovalPrincipalEvent | None,
) -> None:
    _workspace(workspace_root)
    _normalize_principal_name(principal_name)
    _normalize_roles(roles)
    _normalize_public_key(public_key_base64)
    if action is EvolutionApprovalPrincipalAction.REGISTER:
        if previous is not None:
            raise EvolutionApprovalPrincipalError(
                "approval_principal_already_exists",
                "Principal 已存在；请使用 rotate_key、update_roles 或 revoke。",
            )
        return
    if previous is None:
        raise EvolutionApprovalPrincipalError(
            "approval_principal_missing",
            "Principal 不存在，不能执行后续治理动作。",
        )
    if previous.workspace_root != workspace_root or previous.principal_name != principal_name:
        raise EvolutionApprovalPrincipalError(
            "approval_principal_identity_mismatch",
            "Principal previous authority identity 不一致。",
        )
    if previous.state is not EvolutionApprovalPrincipalState.ACTIVE:
        raise EvolutionApprovalPrincipalError(
            "approval_principal_revoked",
            "已撤销 Principal 不能继续变更。",
        )
    if action is EvolutionApprovalPrincipalAction.ROTATE_KEY:
        if roles != previous.roles or public_key_base64 == previous.public_key_base64:
            raise EvolutionApprovalPrincipalError(
                "approval_principal_rotation_invalid",
                "轮换必须保留角色并提供不同的 Ed25519 公钥。",
            )
    elif action is EvolutionApprovalPrincipalAction.UPDATE_ROLES:
        if public_key_base64 != previous.public_key_base64 or roles == previous.roles:
            raise EvolutionApprovalPrincipalError(
                "approval_principal_roles_update_invalid",
                "角色更新必须保留当前公钥并提供不同的角色集合。",
            )
    elif action is EvolutionApprovalPrincipalAction.REVOKE:
        if roles != previous.roles or public_key_base64 != previous.public_key_base64:
            raise EvolutionApprovalPrincipalError(
                "approval_principal_revoke_invalid",
                "撤销必须绑定当前角色与公钥。",
            )


def _validate_event_transition(
    event: EvolutionApprovalPrincipalEvent,
    previous: EvolutionApprovalPrincipalEvent | None,
) -> None:
    _validate_transition_inputs(
        workspace_root=event.workspace_root,
        principal_name=event.principal_name,
        action=event.action,
        roles=event.roles,
        public_key_base64=event.public_key_base64,
        previous=previous,
    )
    expected_sequence = 1 if previous is None else previous.sequence + 1
    expected_previous = "" if previous is None else previous.event_sha256
    expected_generation = (
        1
        if previous is None
        else previous.key_generation
        + (1 if event.action is EvolutionApprovalPrincipalAction.ROTATE_KEY else 0)
    )
    registered_at = event.updated_at if previous is None else previous.registered_at
    if not (
        event.sequence == expected_sequence
        and event.previous_event_sha256 == expected_previous
        and event.key_generation == expected_generation
        and event.registered_at == registered_at
    ):
        raise EvolutionApprovalPrincipalError(
            "approval_principal_transition_invalid",
            "Principal Event sequence、key generation 或 hash chain 不一致。",
        )


def _idempotent_current(
    current: EvolutionApprovalPrincipalEvent | None,
    *,
    action: EvolutionApprovalPrincipalAction,
    roles: tuple[EvolutionPromotionApprovalRole, ...],
    public_key_base64: str,
) -> EvolutionApprovalPrincipalEvent | None:
    if current is None:
        return None
    if action is EvolutionApprovalPrincipalAction.REGISTER and (
        current.sequence == 1
        and current.roles == roles
        and current.public_key_base64 == public_key_base64
    ):
        return current
    if action is EvolutionApprovalPrincipalAction.ROTATE_KEY and (
        current.action is EvolutionApprovalPrincipalAction.ROTATE_KEY
        and current.roles == roles
        and current.public_key_base64 == public_key_base64
    ):
        return current
    if action is EvolutionApprovalPrincipalAction.UPDATE_ROLES and (
        current.action is EvolutionApprovalPrincipalAction.UPDATE_ROLES
        and current.roles == roles
        and current.public_key_base64 == public_key_base64
    ):
        return current
    if action is EvolutionApprovalPrincipalAction.REVOKE and (
        current.state is EvolutionApprovalPrincipalState.REVOKED
    ):
        return current
    return None


def _idempotent_governance_result(
    current: EvolutionApprovalPrincipalEvent,
    *,
    requested_action: EvolutionApprovalPrincipalAction,
) -> EvolutionApprovalPrincipalGovernanceResult:
    return EvolutionApprovalPrincipalGovernanceResult(
        action=requested_action,
        interaction=current.interaction,
        approved=True,
        principal=_view(current),
        authority_changed=False,
    )


def _next_interaction_id(
    principal_id: str,
    action: EvolutionApprovalPrincipalAction,
    history: tuple[HarnessInteractionRecord, ...],
) -> str:
    attempts = []
    for item in history:
        match = _INTERACTION_RE.fullmatch(item.interaction_id)
        if match is not None:
            attempts.append(int(match.group(3)))
    attempt = max(attempts, default=0) + 1
    if attempt > 9_999:
        raise EvolutionApprovalPrincipalError(
            "approval_principal_attempts_exhausted",
            "Principal governance 交互尝试次数已达上限。",
        )
    suffix = principal_id.removeprefix("evprincipal_")
    return f"ask-evprincipal-{suffix}-{action.value}-{attempt}"


def _principal_id(workspace_root: str, principal_name: str) -> str:
    digest = _sha256_payload(
        {
            "policy_version": EVOLUTION_APPROVAL_PRINCIPAL_POLICY,
            "workspace_root": workspace_root,
            "principal_name": principal_name,
        }
    )
    return f"evprincipal_{digest[:24]}"


def _key_id(*, principal_id: str, generation: int, public_key_sha256: str) -> str:
    digest = _sha256_payload(
        {
            "domain": "naumi.evolution.approval-principal.key.v1",
            "principal_id": principal_id,
            "generation": generation,
            "public_key_sha256": public_key_sha256,
        }
    )
    return f"evapprovalkey_{digest[:24]}"


def _normalize_principal_name(value: str) -> str:
    name = str(value or "").strip().lower()
    if _PRINCIPAL_NAME_RE.fullmatch(name) is None:
        raise ValueError("principal name 必须是 2..64 位小写字母、数字、点、下划线或连字符。")
    return name


def _normalize_roles(
    values: Sequence[EvolutionPromotionApprovalRole | str],
) -> tuple[EvolutionPromotionApprovalRole, ...]:
    try:
        roles = tuple(EvolutionPromotionApprovalRole(value) for value in values)
    except (TypeError, ValueError) as exc:
        raise ValueError("Approval Principal role 无效。") from exc
    if not roles:
        raise ValueError("Approval Principal 至少需要一个角色。")
    return tuple(sorted(set(roles), key=_ROLE_ORDER.__getitem__))


def _normalize_public_key(value: str) -> str:
    encoded = str(value or "").strip()
    raw = _decode_public_key(encoded)
    canonical = base64.b64encode(raw).decode("ascii")
    if not hmac.compare_digest(canonical, encoded):
        raise ValueError("Ed25519 public key 必须使用 canonical standard Base64。")
    return canonical


def _decode_public_key(value: str) -> bytes:
    try:
        raw = base64.b64decode(str(value or ""), validate=True)
        if len(raw) != 32:
            raise ValueError("Ed25519 public key 必须是 32 bytes。")
        Ed25519PublicKey.from_public_bytes(raw)
        return raw
    except (TypeError, ValueError) as exc:
        raise ValueError("Ed25519 public key 无效。") from exc


def _static_request_payload(
    request: UserInteractionRequest,
    *,
    include_priority: bool = True,
) -> dict[str, object]:
    payload = {**request.to_public_dict(), "timeout_seconds": None}
    if not include_priority:
        payload.pop("priority", None)
    return payload


def _view(event: EvolutionApprovalPrincipalEvent) -> EvolutionApprovalPrincipalView:
    active = event.state is EvolutionApprovalPrincipalState.ACTIVE
    return EvolutionApprovalPrincipalView(
        principal=event,
        active=active,
        signature_verification_eligible=active,
    )


def _workspace(value: str | Path) -> str:
    try:
        path = Path(value).expanduser().resolve(strict=True)
    except (OSError, TypeError, ValueError) as exc:
        raise EvolutionApprovalPrincipalError(
            "approval_principal_workspace_invalid",
            "Approval Principal workspace 不存在或不可解析。",
        ) from exc
    if not path.is_dir():
        raise EvolutionApprovalPrincipalError(
            "approval_principal_workspace_invalid",
            "Approval Principal workspace 必须是目录。",
        )
    return str(path)


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS evolution_approval_principals (
            workspace_root TEXT NOT NULL,
            principal_id TEXT NOT NULL,
            principal_name TEXT NOT NULL,
            latest_sequence INTEGER NOT NULL,
            state TEXT NOT NULL,
            latest_event_id TEXT NOT NULL,
            latest_event_sha256 TEXT NOT NULL,
            key_id TEXT NOT NULL,
            key_generation INTEGER NOT NULL,
            public_key_sha256 TEXT NOT NULL,
            roles_json TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(workspace_root, principal_id),
            UNIQUE(workspace_root, principal_name)
        );
        CREATE TABLE IF NOT EXISTS evolution_approval_principal_events (
            event_id TEXT PRIMARY KEY,
            event_sha256 TEXT NOT NULL,
            workspace_root TEXT NOT NULL,
            principal_id TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            action TEXT NOT NULL,
            state TEXT NOT NULL,
            key_id TEXT NOT NULL,
            interaction_id TEXT NOT NULL UNIQUE,
            event_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(workspace_root, principal_id, sequence)
        );
        """
    )


async def _latest_with_connection(
    db: aiosqlite.Connection,
    *,
    workspace_root: str,
    principal_id: str,
) -> EvolutionApprovalPrincipalEvent | None:
    snapshot = await (
        await db.execute(
            "SELECT * FROM evolution_approval_principals "
            "WHERE workspace_root = ? AND principal_id = ?",
            (workspace_root, principal_id),
        )
    ).fetchone()
    if snapshot is None:
        orphan = await (
            await db.execute(
                "SELECT 1 FROM evolution_approval_principal_events "
                "WHERE workspace_root = ? AND principal_id = ? LIMIT 1",
                (workspace_root, principal_id),
            )
        ).fetchone()
        if orphan is not None:
            raise ValueError("Approval Principal Event chain 缺少 latest snapshot。")
        return None
    rows = await (
        await db.execute(
            "SELECT * FROM evolution_approval_principal_events "
            "WHERE workspace_root = ? AND principal_id = ? ORDER BY sequence ASC",
            (workspace_root, principal_id),
        )
    ).fetchall()
    previous: EvolutionApprovalPrincipalEvent | None = None
    for expected, row in enumerate(rows, start=1):
        encoded = str(row["event_json"])
        if len(encoded.encode("utf-8")) > _MAX_EVENT_BYTES:
            raise ValueError("Approval Principal Event 过大。")
        item = EvolutionApprovalPrincipalEvent.model_validate_json(encoded)
        if not (
            row["event_id"] == item.event_id
            and row["event_sha256"] == item.event_sha256
            and row["workspace_root"] == item.workspace_root
            and row["principal_id"] == item.principal_id
            and int(row["sequence"]) == item.sequence == expected
            and row["action"] == item.action.value
            and row["state"] == item.state.value
            and row["key_id"] == item.key_id
            and row["interaction_id"] == item.interaction.interaction_id
            and row["created_at"] == item.updated_at
        ):
            raise ValueError("Approval Principal Event Store index 不一致。")
        _validate_event_transition(item, previous)
        previous = item
    if previous is None:
        raise ValueError("Approval Principal snapshot 缺少 event chain。")
    if not (
        snapshot["principal_id"] == previous.principal_id
        and snapshot["principal_name"] == previous.principal_name
        and int(snapshot["latest_sequence"]) == previous.sequence
        and snapshot["state"] == previous.state.value
        and snapshot["latest_event_id"] == previous.event_id
        and snapshot["latest_event_sha256"] == previous.event_sha256
        and snapshot["key_id"] == previous.key_id
        and int(snapshot["key_generation"]) == previous.key_generation
        and snapshot["public_key_sha256"] == previous.public_key_sha256
        and json.loads(str(snapshot["roles_json"]))
        == [role.value for role in previous.roles]
        and snapshot["updated_at"] == previous.updated_at
    ):
        raise ValueError("Approval Principal latest snapshot 不一致。")
    return previous


def _aware(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError("timestamp 必须包含时区。")
    return parsed.astimezone(UTC)


def _sha256_payload(payload: object) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "EVOLUTION_APPROVAL_PRINCIPAL_POLICY",
    "EvolutionApprovalPrincipalAction",
    "EvolutionApprovalPrincipalError",
    "EvolutionApprovalPrincipalEvent",
    "EvolutionApprovalPrincipalEventBuilder",
    "EvolutionApprovalPrincipalGovernanceResult",
    "EvolutionApprovalPrincipalService",
    "EvolutionApprovalPrincipalState",
    "EvolutionApprovalPrincipalStore",
    "EvolutionApprovalPrincipalView",
    "parse_approval_roles",
    "render_evolution_approval_principal",
]
