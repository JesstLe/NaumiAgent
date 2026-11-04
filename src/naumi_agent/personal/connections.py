"""Personal calendar authority, encrypted credential lifecycle and shared service composition."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import secrets
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from naumi_agent.personal.artifact_models import strict_json_loads
from naumi_agent.personal.connection_models import (
    ACTOR,
    CALENDAR_READ,
    CALENDAR_WRITE,
    EMAIL,
    REQUEST,
    canonical,
    digest,
    fields,
    integer,
    scopes,
    text,
    timestamp,
)
from naumi_agent.personal.connection_store import ConnectionStore, broken, checked_path
from naumi_agent.personal.connection_vault import ConnectionVault
from naumi_agent.personal.google_calendar import GoogleCalendarAdapter
from naumi_agent.personal.google_oauth import GoogleOAuthClient, GoogleProtocolError, OAuthLoopback
from naumi_agent.personal.models import PersonalAgentError

_MANUAL = frozenset({"manual_api", "manual_cli", "manual_tui"})
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CalendarTokenSnapshot:
    """An internal request credential bound to its exact original authority generation."""

    access_token: str = field(repr=False)
    connection: dict = field(repr=False)


_SECRET_FIELDS = frozenset(
    {
        "credential_envelope",
        "key_slot",
        "secret_envelope",
        "subject",
        "issuer",
        "token_generation",
        "client_id_digest",
    }
)


def manual(channel):
    if channel not in _MANUAL:
        raise PersonalAgentError("scope_denied", "该操作需要由用户在账户管理入口确认")


def provider_error(error):
    messages = {
        "invalid_grant": "账户授权已失效，请重新连接原账户",
        "authentication_required": "账户需要重新验证",
        "access_denied": "当前账户已失去这份日历的访问权限",
        "scope_denied": "当前账户没有这项日历权限",
        "quota_exceeded": "日历服务额度暂时不足，请稍后核对原请求",
        "rate_limited": "日历服务暂时繁忙，请稍后核对原请求",
        "sync_token_expired": "日历同步游标已过期，正在重新读取镜像",
    }
    return PersonalAgentError(
        error.code, messages.get(error.code, "日历服务暂未返回可核验结果，请核对原请求")
    )


class ConnectionAuthority:
    """No model-facing operation can manufacture a human account grant or approval."""

    def __init__(
        self, engine, *, store=None, vault=None, adapter=None, oauth_factory=None, clock=None
    ):
        self.engine = engine
        self.personal = engine.personal_agents
        self.workspace_key = self.personal.workspace_key
        self.owner_key = hashlib.sha256(
            os.path.normcase(str(self.personal.store.db_path.resolve())).encode()
        ).hexdigest()
        self.store = store or ConnectionStore(
            self.personal.store.base_dir / "connections", owner_key=self.owner_key
        )
        self.vault = vault or ConnectionVault()
        self.adapter = adapter or GoogleCalendarAdapter()
        self.oauth_factory = oauth_factory or GoogleOAuthClient.from_json
        self.clock = clock or (lambda: datetime.now(UTC))
        self.holder = "connections-" + uuid.uuid4().hex
        session_db = Path(engine.config.memory.session_db_path).resolve()
        self.boundary = session_db.with_name(f".personal-connections-{self.workspace_key}-v1")
        self._required_identity = None
        self._oauth_tasks = {}
        self._worker = None
        self._closing = False
        self._tick_lock = asyncio.Lock()
        self._last_worker_error = None

    def now(self):
        value = self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise broken()
        return value.astimezone(UTC)

    def stamp(self):
        return self.now().isoformat(timespec="microseconds")

    def _guard(self):
        try:
            return self._guard_impl()
        except (OSError, ValueError, TypeError, KeyError):
            raise broken() from None

    def _guard_impl(self):
        identity = self.store.read(
            lambda tx: (
                None
                if tx is None
                else tx.db.execute("SELECT instance_id FROM connection_meta").fetchone()[0]
            )
        )
        checked_path(self.boundary)
        expected = {
            "version": 1,
            "owner_key": self.owner_key,
            "workspace_key": self.workspace_key,
            "instance_id": identity,
        }
        if self.boundary.exists():
            if (
                self.boundary.stat().st_size > 1024
                or strict_json_loads(self.boundary.read_bytes()) != expected
                or not identity
            ):
                raise broken()
        if self._required_identity and identity != self._required_identity:
            raise broken()
        if identity:
            self.boundary.parent.mkdir(parents=True, exist_ok=True)
            if not self.boundary.exists():
                try:
                    with self.boundary.open("xb") as stream:
                        stream.write((canonical(expected) + "\n").encode())
                        stream.flush()
                        os.fsync(stream.fileno())
                except FileExistsError:
                    if strict_json_loads(self.boundary.read_bytes()) != expected:
                        raise broken() from None
            self._required_identity = identity

    async def _read(self, callback):
        await asyncio.to_thread(self._guard)
        result = await asyncio.to_thread(self.store.read, callback)
        await asyncio.to_thread(self._guard)
        return result

    async def _write(self, callback):
        await asyncio.to_thread(self._guard)
        result = await asyncio.to_thread(self.store.write, callback)
        await asyncio.to_thread(self._guard)
        return result

    async def _mutate(self, request_id, operation, payload, callback):
        text(request_id, "请求编号", 128, pattern=REQUEST)
        await asyncio.to_thread(self._guard)
        result = await asyncio.to_thread(
            self.store.mutate, request_id, operation, payload, callback
        )
        await asyncio.to_thread(self._guard)
        return result

    async def _record(self, kind, identity, *, revision=None, missing=False):
        return await self._read(
            lambda tx: (
                None
                if tx is None and missing
                else self._required_tx(tx).get(kind, identity, revision=revision, missing=missing)
            )
        )

    @staticmethod
    def _required_tx(tx):
        if tx is None:
            raise PersonalAgentError("not_found", "账户连接资料尚未建立")
        return tx

    def actor_version(self, actor_id):
        text(actor_id, "Agent 编号", 35, pattern=ACTOR)
        return 0 if actor_id == "main" else self.personal._executable(actor_id).version

    def _grant(self, connection, actor_id, *, write=False):
        if connection["status"] != "connected":
            raise PersonalAgentError("connection_revoked", "账户已断开或需要重新验证")
        version = self.actor_version(actor_id)
        if actor_id == "main":
            grant = {"id": "", "revision": 0, "actor_version": 0}
        else:
            grant = connection.get("grants", {}).get(self.workspace_key + ":" + actor_id)
            if (
                not grant
                or grant["state"] != "active"
                or grant["actor_version"] != version
                or grant["auth_epoch"] != connection["auth_epoch"]
            ):
                raise PersonalAgentError(
                    "scope_denied", "请先为这个 Agent 的当前版本分享主日历只读权限"
                )
        if write and not connection["can_write"]:
            raise PersonalAgentError("scope_denied", "请先由用户为原账户确认日历写入权限")
        return grant

    async def _connection(self, identity, actor_id="main", *, write=False):
        connection = await self._record("connection", identity)
        self._grant(connection, actor_id, write=write)
        return connection

    def connection_view(self, value):
        public = {key: item for key, item in value.items() if key not in _SECRET_FIELDS}
        public["grants"] = [
            {key: grant[key] for key in ("actor_id", "actor_version", "revision", "state")}
            for grant in value.get("grants", {}).values()
            if grant["workspace_key"] == self.workspace_key
        ]
        return public

    async def context(self):
        client = await self._record("client", "google_calendar", missing=True)
        return {
            "owner_key": self.owner_key,
            "workspace_key": self.workspace_key,
            "client_configured": bool(client and client["status"] == "configured"),
            "provider": "google_calendar",
        }

    async def list_connections(self, actor_id="main"):
        self.actor_version(actor_id)
        values = await self._read(
            lambda tx: [] if tx is None else tx.list("connection", limit=1001)
        )
        if len(values) > 1000:
            raise PersonalAgentError("invalid_input", "连接数量超过本机管理上限")
        result = []
        for value in values:
            if actor_id != "main":
                try:
                    self._grant(value, actor_id)
                except PersonalAgentError:
                    continue
            result.append(self.connection_view(value))
        return {"connections": result}

    async def _claim(self, scope, identity, *, once=False):
        return await self._write(
            lambda tx: tx.claim(
                scope, identity, self.holder, now=self.now().timestamp(), ttl=90, once=once
            )
        )

    def _fence(self, tx, scope, identity, lease, *, phase=None):
        return tx.require_lease(
            scope, identity, self.holder, lease["fence"], now=self.now().timestamp(), phase=phase
        )

    async def _known_request(self, request_id, operation, payload):
        text(request_id, "请求编号", 128, pattern=REQUEST)
        found = await self._read(lambda tx: None if tx is None else tx.request(request_id))
        if found and (
            found["operation"] != operation
            or found["fingerprint"] != digest({"operation": operation, "payload": payload})
        ):
            raise PersonalAgentError(
                "idempotency_conflict", "同一请求编号对应不同内容，请核对原请求"
            )
        return found

    async def _release_lease(self, scope, identity, lease):
        try:
            await self._write(lambda tx: self._fence(tx, scope, identity, lease, phase="finished"))
        except PersonalAgentError as exc:
            if exc.code != "lease_lost":
                raise

    @asynccontextmanager
    async def _account(self, identity):
        lease = await self._claim("account", identity)
        try:
            yield lease
        finally:
            await self._release_lease("account", identity, lease)

    async def configure_client(self, payload, *, trusted_channel):
        manual(trusted_channel)
        fields(payload, {"request_id", "client_json"}, {"request_id", "client_json"})
        raw = text(payload["client_json"], "桌面 OAuth 配置", 16384)
        try:
            parsed = strict_json_loads(raw)
            self.oauth_factory(parsed)
        except GoogleProtocolError as exc:
            raise provider_error(exc) from None
        semantic = {"client_sha256": digest(parsed)}
        if await self._known_request(payload["request_id"], "configure_client", semantic):
            return await self.context()
        slot = "client_" + self.workspace_key
        lease = await self._claim("vault", slot)
        try:
            await asyncio.to_thread(self.vault.provision, owner_key=self.owner_key, slot=slot)
            encrypted = await asyncio.to_thread(
                self.vault.seal,
                owner_key=self.owner_key,
                slot=slot,
                purpose="oauth_client",
                value=parsed,
            )

            def save(tx):
                self._fence(tx, "vault", slot, lease)
                previous = tx.get("client", "google_calendar", missing=True)
                if not previous or previous["client_sha256"] != digest(parsed):
                    tx.put(
                        "client",
                        {
                            "id": "google_calendar",
                            "status": "configured",
                            "client_sha256": digest(parsed),
                            "key_slot": slot,
                            "credential_envelope": encrypted,
                            "updated_at": self.stamp(),
                        },
                        expected_revision=0 if previous is None else previous["revision"],
                    )
                return {"kind": "context", "id": "google_calendar"}

            await self._mutate(payload["request_id"], "configure_client", semantic, save)
            return await self.context()
        finally:
            await self._release_lease("vault", slot, lease)

    async def _client_data(self):
        value = await self._record("client", "google_calendar", missing=True)
        if not value or value["status"] != "configured":
            raise PersonalAgentError(
                "client_not_configured", "请先导入 Google 桌面应用的 OAuth 配置"
            )
        return await asyncio.to_thread(
            self.vault.open,
            owner_key=self.owner_key,
            slot=value["key_slot"],
            purpose="oauth_client",
            envelope=value["credential_envelope"],
        )

    async def start_oauth(self, payload, *, trusted_channel):
        manual(trusted_channel)
        fields(
            payload,
            {"request_id", "connection_id", "expected_revision", "write"},
            {"request_id", "write"},
        )
        if type(payload["write"]) is not bool:
            raise PersonalAgentError("invalid_input", "账户授权模式格式不正确")
        known = await self._known_request(payload["request_id"], "start_oauth", payload)
        if known:
            return await self.oauth_status(known["result"]["id"])
        previous = None
        if payload.get("connection_id"):
            previous = await self._record("connection", payload["connection_id"])
            integer(payload.get("expected_revision"), "连接版本")
            if previous["revision"] != payload["expected_revision"]:
                raise PersonalAgentError("version_conflict", "账户资料已更新，请刷新后连接")
        client_data = await self._client_data()

        def reserve(tx):
            identity = "co_" + uuid.uuid4().hex
            connection_id = previous["id"] if previous else "pc_" + uuid.uuid4().hex
            tx.put(
                "oauth",
                {
                    "id": identity,
                    "connection_id": connection_id,
                    "status": "pending",
                    "write": payload["write"],
                    "expected_connection_revision": previous["revision"] if previous else 0,
                    "expected_subject": None
                    if previous is None
                    else {"issuer": previous["issuer"], "sub": previous["subject"]},
                    "expires_at": (self.now() + timedelta(minutes=10)).isoformat(),
                    "authorization_url": None,
                    "secret_envelope": None,
                    "key_slot": "oauth_" + identity[3:],
                    "credential_slot": "conn_" + uuid.uuid4().hex,
                    "error_code": None,
                    "created_at": self.stamp(),
                },
                expected_revision=0,
            )
            return {"kind": "oauth", "id": identity}

        ref = await self._mutate(payload["request_id"], "start_oauth", payload, reserve)
        intent = await self._record("oauth", ref["id"])
        if intent["authorization_url"] or intent["status"] != "pending":
            return self.oauth_view(intent)
        lease = await self._claim("oauth", intent["id"], once=True)
        state, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(48)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        listener = OAuthLoopback(state=state, expires_at=timestamp(intent["expires_at"]))
        try:
            await listener.start()
            await asyncio.to_thread(
                self.vault.provision, owner_key=self.owner_key, slot=intent["key_slot"]
            )
            encrypted = await asyncio.to_thread(
                self.vault.seal,
                owner_key=self.owner_key,
                slot=intent["key_slot"],
                purpose="oauth_intent",
                value={
                    "client": client_data,
                    "state": state,
                    "nonce": nonce,
                    "verifier": verifier,
                    "redirect_uri": listener.redirect_uri,
                },
            )
            url = self.oauth_factory(client_data).authorization_url(
                redirect_uri=listener.redirect_uri,
                state=state,
                nonce=nonce,
                code_challenge=challenge,
                write=intent["write"],
            )

            def ready(tx):
                self._fence(tx, "oauth", intent["id"], lease)
                current = tx.get("oauth", intent["id"])
                return tx.put(
                    "oauth",
                    {**current, "authorization_url": url, "secret_envelope": encrypted},
                    expected_revision=current["revision"],
                )

            intent = await self._write(ready)
            task = asyncio.create_task(
                self._finish_oauth(intent, listener), name="oauth:" + intent["id"]
            )
            self._oauth_tasks[intent["id"]] = task
            task.add_done_callback(
                lambda done, key=intent["id"]: self._background_done(done, key, "oauth")
            )
            return self.oauth_view(intent)
        except BaseException:
            await listener.close()
            raise

    def oauth_view(self, value):
        return {
            key: value.get(key)
            for key in ("status", "authorization_url", "expires_at", "connection_id", "error_code")
        } | {"intent_id": value["id"]}

    async def oauth_status(self, identity):
        value = await self._record("oauth", identity)
        if value["status"] == "pending" and timestamp(value["expires_at"]) <= self.now():

            def expire(tx):
                current = tx.get("oauth", identity)
                if current["status"] != "pending" or timestamp(current["expires_at"]) > self.now():
                    return current
                return tx.put(
                    "oauth",
                    {**current, "status": "expired", "authorization_url": None},
                    expected_revision=current["revision"],
                )

            value = await self._write(expire)
        return self.oauth_view(value)

    @staticmethod
    def _change(tx, kind, identity, changes):
        current = tx.get(kind, identity)
        return tx.put(kind, {**current, **changes}, expected_revision=current["revision"])

    def _tokens(self, response, *, write, previous=None):
        if not isinstance(response, dict) or len(canonical(response).encode()) > 65536:
            raise PersonalAgentError("invalid_token", "账户凭据响应格式不正确")
        access = text(response.get("access_token"), "账户访问凭据", 16384)
        refresh = response.get(
            "refresh_token", None if previous is None else previous["refresh_token"]
        )
        refresh = text(refresh, "账户离线凭据", 16384)
        if (
            any(ord(c) < 33 or ord(c) > 126 for c in access + refresh)
            or str(response.get("token_type", "")).casefold() != "bearer"
        ):
            raise PersonalAgentError("invalid_token", "账户凭据响应格式不正确")
        lifetime = integer(response.get("expires_in"), "账户凭据有效期", maximum=86400)
        granted = scopes(response["scope"], write=write) if "scope" in response else None
        if granted is None:
            if previous is None:
                raise PersonalAgentError("scope_denied", "账户未返回可核验的授权范围")
            granted = previous["scopes"]
        return {
            "access_token": access,
            "refresh_token": refresh,
            "scopes": granted,
            "expires_at": (self.now() + timedelta(seconds=lifetime)).isoformat(),
        }

    @staticmethod
    def _continuous_write_upgrade(old, intent, subject, claims, client_digest, bundle):
        """Preserve read authority only for a verified, monotone, healthy RO -> RW change."""
        if (
            old is None
            or old["status"] != "connected"
            or old["provider"] != "google_calendar"
            or old["can_write"] is not False
            or intent["write"] is not True
            or old["issuer"] != subject["issuer"]
            or old["subject"] != subject["sub"]
            or old["email"] != claims["email"]
            or old["email_verified"] is not True
            or old["client_id_digest"] != client_digest
            or old.get("last_error_code") is not None
            or not old.get("credential_envelope")
            or not old.get("key_slot")
            or old.get("revoke_status") not in {None, "not_requested"}
        ):
            return False
        previous, current = set(old["scopes"]), set(bundle["scopes"])
        # The owned RW scope semantically includes owned.readonly, not its URI string.
        if previous != {"openid", EMAIL, CALENDAR_READ}:
            return False
        if CALENDAR_WRITE not in current:
            return False
        covered = current | {CALENDAR_READ}
        return previous <= covered and current <= {"openid", EMAIL, CALENDAR_READ, CALENDAR_WRITE}

    async def _finish_oauth(self, intent, listener):
        try:
            code = await listener.wait()

            async with self._account(intent["connection_id"]) as account_lease:

                def consume(tx):
                    current = tx.get("oauth", intent["id"])
                    self._fence(tx, "account", intent["connection_id"], account_lease)
                    prior = tx.get("connection", intent["connection_id"], missing=True)
                    if (0 if prior is None else prior["revision"]) != current[
                        "expected_connection_revision"
                    ]:
                        raise PersonalAgentError(
                            "version_conflict", "原账户状态已变化，未交换旧授权请求"
                        )
                    if (
                        current["status"] != "pending"
                        or timestamp(current["expires_at"]) <= self.now()
                    ):
                        raise PersonalAgentError("version_conflict", "本次账户连接已结束")
                    return tx.put(
                        "oauth",
                        {**current, "status": "exchanging", "authorization_url": None},
                        expected_revision=current["revision"],
                    )

                current = await self._write(consume)
                secret = await asyncio.to_thread(
                    self.vault.open,
                    owner_key=self.owner_key,
                    slot=current["key_slot"],
                    purpose="oauth_intent",
                    envelope=current["secret_envelope"],
                )
                client = self.oauth_factory(secret["client"])
                response = await client.exchange_code(
                    code, redirect_uri=secret["redirect_uri"], code_verifier=secret["verifier"]
                )
                claims = await client.verify_id_token(
                    response.get("id_token"), nonce=secret["nonce"]
                )
                subject = {"issuer": "https://accounts.google.com", "sub": claims["sub"]}
                if (
                    current["expected_subject"] is not None
                    and current["expected_subject"] != subject
                ):
                    raise PersonalAgentError(
                        "account_mismatch", "请选择原来连接的 Google 账户，不覆盖其他账户"
                    )
                bundle = self._tokens(response, write=current["write"])
                # A signed identity and token response do not establish Calendar ACL.
                await self.adapter.list_events(bundle["access_token"])
                slot = current["credential_slot"]
                await asyncio.to_thread(self.vault.provision, owner_key=self.owner_key, slot=slot)
                encrypted = await asyncio.to_thread(
                    self.vault.seal,
                    owner_key=self.owner_key,
                    slot=slot,
                    purpose="calendar_tokens",
                    value={**bundle, "client": secret["client"]},
                )

                def activate(tx):
                    self._fence(tx, "account", intent["connection_id"], account_lease)
                    active_intent = tx.get("oauth", current["id"])
                    if active_intent["status"] != "exchanging":
                        raise PersonalAgentError(
                            "version_conflict", "账户连接已取消，未启用返回的凭据"
                        )
                    old = tx.get("connection", current["connection_id"], missing=True)
                    if (0 if old is None else old["revision"]) != current[
                        "expected_connection_revision"
                    ]:
                        raise PersonalAgentError("version_conflict", "原账户状态已变化，请重新连接")
                    client_digest = digest(client.client_id)
                    for other in tx.list("connection"):
                        if (
                            other["id"] != current["connection_id"]
                            and other["subject"] == subject["sub"]
                            and other["issuer"] == subject["issuer"]
                            and other["client_id_digest"] == client_digest
                            and other["status"] != "disconnected"
                        ):
                            raise PersonalAgentError(
                                "connection_exists", "这个账户已连接，请在原连接中重新授权"
                            )
                    continuous = self._continuous_write_upgrade(
                        old, current, subject, claims, client_digest, bundle
                    )
                    if continuous:
                        actions = tx.list("action", parent=old["id"], limit=1001)
                        # Legacy/inconsistent RO records must never resurrect a write authority.
                        continuous = len(actions) <= 1000 and not any(
                            action["auth_epoch"] == old["auth_epoch"]
                            and (
                                action["status"]
                                in {"approved", "dispatching", "submitted", "unknown"}
                                or action["status"] == "draft"
                                and (action.get("approval") or action.get("execution"))
                            )
                            for action in actions
                        )
                    connection = {
                        **(old if continuous else {}),
                        "id": current["connection_id"],
                        "provider": "google_calendar",
                        "status": "connected",
                        "issuer": subject["issuer"],
                        "subject": subject["sub"],
                        "email": claims["email"],
                        "email_verified": True,
                        "auth_epoch": 1
                        if old is None
                        else old["auth_epoch"] + (0 if continuous else 1),
                        "can_write": current["write"],
                        "scopes": bundle["scopes"],
                        "credential_envelope": encrypted,
                        "key_slot": slot,
                        "token_generation": 1 if old is None else old["token_generation"] + 1,
                        "access_expires_at": bundle["expires_at"],
                        "client_id_digest": client_digest,
                        "grants": old["grants"] if continuous else {},
                        "sync_status": old["sync_status"] if continuous else "not_synced",
                        "last_sync_at": old["last_sync_at"] if continuous else None,
                        "last_error_code": None,
                        "revoke_status": None,
                        "updated_at": self.stamp(),
                    }
                    saved = tx.put(
                        "connection",
                        connection,
                        expected_revision=0 if old is None else old["revision"],
                    )
                    tx.put(
                        "oauth",
                        {
                            **active_intent,
                            "status": "connected",
                            "secret_envelope": None,
                            "connection_id": saved["id"],
                            "read_authority_preserved": continuous,
                        },
                        expected_revision=active_intent["revision"],
                    )
                    return saved

                await self._write(activate)
        except asyncio.CancelledError:
            await self._oauth_failure(intent["id"], "interrupted", "authorization_interrupted")
            raise
        except GoogleProtocolError as exc:
            state = (
                "exchange_unknown"
                if exc.unknown
                else "expired"
                if exc.code in {"loopback_timeout", "loopback_closed"}
                else "denied"
            )
            await self._oauth_failure(intent["id"], state, exc.code)
        except PersonalAgentError as exc:
            await self._oauth_failure(intent["id"], "denied", exc.code)
        except Exception:
            await self._oauth_failure(intent["id"], "exchange_unknown", "authorization_unknown")
        finally:
            await listener.close()
            await asyncio.to_thread(
                self.vault.delete, owner_key=self.owner_key, slot=intent["key_slot"]
            )
            try:
                latest = await self._record("oauth", intent["id"])
                if latest["status"] != "connected":
                    await asyncio.to_thread(
                        self.vault.delete, owner_key=self.owner_key, slot=intent["credential_slot"]
                    )
            except PersonalAgentError:
                # A lost ledger cannot prove that the new credential is unused.
                self._last_worker_error = "credential_cleanup_unknown"

    async def _oauth_failure(self, identity, status, code):
        def fail(tx):
            current = tx.get("oauth", identity)
            if current["status"] not in {"pending", "exchanging"}:
                return current
            return tx.put(
                "oauth",
                {
                    **current,
                    "status": status,
                    "authorization_url": None,
                    "secret_envelope": None,
                    "error_code": code,
                },
                expected_revision=current["revision"],
            )

        await self._write(fail)

    async def cancel_oauth(self, identity, payload, *, trusted_channel):
        manual(trusted_channel)
        fields(payload, {"request_id"}, {"request_id"})

        def cancel(tx):
            current = tx.get("oauth", identity)
            if current["status"] in {"pending", "exchanging"}:
                tx.put(
                    "oauth",
                    {**current, "status": "cancelled", "authorization_url": None},
                    expected_revision=current["revision"],
                )
            return {"kind": "oauth", "id": identity}

        await self._mutate(payload["request_id"], "cancel_oauth", {"intent_id": identity}, cancel)
        task = self._oauth_tasks.get(identity)
        if task:
            task.cancel()
        return await self.oauth_status(identity)

    async def grant(self, identity, payload, *, trusted_channel, revoke=False):
        manual(trusted_channel)
        fields(
            payload,
            {"request_id", "expected_revision", "actor_id", "actor_version"},
            {"request_id", "expected_revision", "actor_id"},
        )
        actor = payload["actor_id"]
        version = self.actor_version(actor)
        if actor == "main" or not revoke and payload.get("actor_version") != version:
            raise PersonalAgentError("version_conflict", "请选择已有 Agent 的当前版本")

        def change(tx):
            current = tx.get("connection", identity)
            if current["revision"] != integer(payload["expected_revision"], "连接版本"):
                raise PersonalAgentError("version_conflict", "连接状态已更新，请刷新后分享")
            self._grant(current, "main")
            key = self.workspace_key + ":" + actor
            old = current["grants"].get(key)
            grant = {
                "id": "cg_" + uuid.uuid4().hex,
                "revision": 1 if old is None else old["revision"] + 1,
                "state": "revoked" if revoke else "active",
                "actor_id": actor,
                "actor_version": version,
                "workspace_key": self.workspace_key,
                "auth_epoch": current["auth_epoch"],
            }
            tx.put(
                "connection",
                {
                    **current,
                    "grants": {**current["grants"], key: grant},
                    "updated_at": self.stamp(),
                },
                expected_revision=current["revision"],
            )
            return {"kind": "connection", "id": identity}

        await self._mutate(
            payload["request_id"],
            "revoke_grant" if revoke else "grant",
            {"connection_id": identity, **payload},
            change,
        )
        return self.connection_view(await self._record("connection", identity))

    async def request_status(self, request_id):
        text(request_id, "请求编号", 128, pattern=REQUEST)
        saved = await self._read(lambda tx: None if tx is None else tx.request(request_id))
        if saved is None:
            return {
                "request_id": request_id,
                "operation": None,
                "status": "unknown",
                "result_kind": None,
                "result_id": None,
                "result": None,
                "fingerprint": None,
            }
        ref = saved["result"]
        kind, identity = ref["kind"], ref["id"]
        status = "applied"
        if kind == "context":
            result = await self.context()
            identity = None
        elif kind == "connection":
            result = self.connection_view(await self._record("connection", identity))
        elif kind == "oauth":
            result = await self.oauth_status(identity)
        elif kind == "action":
            result = await self.get_action(identity, caller_actor=None)
        elif kind == "item":
            binding = await self._record("binding", identity)
            if binding["item_id"]:
                identity = binding["item_id"]
                result = await self.engine.personal_items.get(identity)
            else:
                result, status = None, "pending" if binding["status"] == "pending" else "unknown"
        else:
            raise broken()
        return {
            "request_id": request_id,
            "operation": saved["operation"],
            "status": status,
            "result_kind": kind,
            "result_id": identity,
            "result": result,
            "fingerprint": saved["fingerprint"],
        }

    async def authorize_artifact(self, snapshot, actor_id):
        """Walk immutable artifact ancestry; derived copies cannot launder a source."""
        seen = set()

        async def visit(value, depth=0):
            key = (value["artifact_id"], value["version"])
            if key in seen:
                return
            if depth > 32 or len(seen) >= 1024:
                raise broken()
            seen.add(key)
            refs = value.get("evidence", {}).get("connection_sources", [])
            if refs:
                await self.authorize_sources(refs, actor_id)
            for ref in value.get("evidence", {}).get("connection_action_refs", []):
                await self.authorize_action_reference(ref, actor_id)
            for ref in value.get("source_refs", []):
                source = await self.engine.personal_artifacts._call(
                    "get", actor_id=actor_id, artifact_id=ref["artifact_id"], version=ref["version"]
                )
                await visit(source, depth + 1)

        await visit(snapshot)

    async def authorize_action_reference(self, raw, actor_id):
        from naumi_agent.personal.connection_models import action_source_ref

        ref = action_source_ref(raw)
        value = await self._record("action", ref["action_id"], revision=ref["revision"])
        if (
            value["actor_id"] != actor_id
            or ref["actor_id"] != actor_id
            or value["connection_id"] != ref["connection_id"]
            or value["auth_epoch"] != ref["auth_epoch"]
            or value["digest"] != ref["digest"]
            or value["grant"]["id"] != ref["grant_id"]
            or value["grant"]["revision"] != ref["grant_revision"]
        ):
            raise PersonalAgentError("connection_source_revoked", "原行动资料与当前读取身份不一致")
        await self._authorize_action(value, write=False, expiry=False)
        return value

    async def pin_action_read(self, result, actor_id):
        session = getattr(self.engine, "_session", None)
        if session is None:
            raise PersonalAgentError("binding_conflict", "请在有效对话中读取行动资料")
        value = await self._record("action", result["id"], revision=result["revision"])
        ref = {
            "connection_id": value["connection_id"],
            "auth_epoch": value["auth_epoch"],
            "action_id": value["id"],
            "revision": value["revision"],
            "digest": value["digest"],
            "actor_id": actor_id,
            "grant_id": value["grant"]["id"],
            "grant_revision": value["grant"]["revision"],
        }
        if result["digest"] != value["digest"]:
            raise PersonalAgentError("binding_conflict", "行动正文与原版本摘要不一致")
        await self.authorize_action_reference(ref, actor_id)
        snapshot = await self.engine.personal_artifacts.publish(
            actor_id=actor_id,
            request_id="calendar-action-read:" + digest(ref),
            title="日历行动",
            kind="material",
            filename="calendar-action.txt",
            content=canonical(result).encode(),
            source_refs=value["artifact_refs"],
            evidence={"source_kind": "calendar_action", "connection_action_refs": [ref]},
        )
        artifact_ref = self.engine.personal_artifacts.reference(snapshot)
        await self.engine.personal_artifacts.bind_refs(session.id, actor_id, [artifact_ref])
        if getattr(self.engine, "_session", None) is not session:
            raise PersonalAgentError("binding_conflict", "读取期间对话已切换，行动正文未返回")
        await self.authorize_action_reference(ref, actor_id)
        return result

    def _background_done(self, task, identity, kind):
        tasks = self._oauth_tasks if kind == "oauth" else getattr(self, "_sync_tasks", {})
        tasks.pop(identity, None)
        if task.cancelled():
            return
        failure = task.exception()
        if failure is not None:
            code = (
                failure.code
                if isinstance(failure, PersonalAgentError)
                else "connection_worker_error"
            )
            self._last_worker_error = code
            logger.warning("账户后台操作未完成：%s", code)

    def start(self):
        if self._worker is not None and not self._worker.done():
            return
        self._closing = False
        self._worker = asyncio.create_task(self._loop(), name="personal-connections")

    async def tick(self):
        if self._closing:
            return
        async with self._tick_lock:
            values = await self._read(
                lambda tx: [] if tx is None else tx.list("connection", limit=1001)
            )
            for value in values:
                if value["status"] != "connected":
                    continue
                retry_at = value.get("next_sync_at")
                if retry_at and self.now() < timestamp(retry_at):
                    continue
                if (
                    value["sync_status"] == "error"
                    and not value.get("sync_requested")
                    and not retry_at
                ):
                    continue
                last = value.get("last_sync_at")
                if (
                    value.get("sync_requested")
                    or last is None
                    or self.now() - timestamp(last) >= timedelta(minutes=5)
                ):
                    self._kick_sync(value["id"])
            bindings = await self._read(
                lambda tx: [] if tx is None else tx.list("binding", statuses=["pending"], limit=20)
            )
            for value in bindings:
                try:
                    await self._complete_item_binding(value["id"])
                except PersonalAgentError as exc:
                    await self._write(
                        lambda tx, identity=value["id"], code=exc.code: self._change(
                            tx, "binding", identity, {"status": "blocked", "error_code": code}
                        )
                    )
            await self.action_tick()

    async def _loop(self):
        while not self._closing:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_worker_error = (
                    exc.code if isinstance(exc, PersonalAgentError) else "connection_worker_error"
                )
                logger.warning("账户连接轮询暂未完成：%s", self._last_worker_error)
            await asyncio.sleep(30)

    async def stop(self):
        self._closing = True
        tasks = [*self._oauth_tasks.values(), *getattr(self, "_sync_tasks", {}).values()]
        if self._worker is not None:
            tasks.append(self._worker)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.stop_actions()

    async def _credential(self, connection):
        try:
            return await asyncio.to_thread(
                self.vault.open,
                owner_key=self.owner_key,
                slot=connection["key_slot"],
                purpose="calendar_tokens",
                envelope=connection["credential_envelope"],
            )
        except PersonalAgentError as exc:
            if exc.code in {"credential_missing", "credential_error", "vault_corrupt"}:
                await self._credential_failure(connection, "credential_error", exc.code)
            raise

    async def assert_token_snapshot(self, snapshot, actor_id="main", *, write=False):
        """Check the exact credential generation without relabeling an old HTTP request."""
        original = snapshot.connection
        current = await self._connection(original["id"], actor_id, write=write)
        if any(
            current[key] != original[key]
            for key in ("issuer", "subject", "client_id_digest", "auth_epoch", "token_generation")
        ) or self._grant(current, actor_id, write=write) != self._grant(
            original, actor_id, write=write
        ):
            raise PersonalAgentError("version_conflict", "账户凭据已变化，请核对原操作")
        return current

    async def _bind_token(self, access_token, connection, actor_id, *, write=False):
        snapshot = CalendarTokenSnapshot(access_token, connection)
        await self.assert_token_snapshot(snapshot, actor_id, write=write)
        return snapshot

    async def token(self, identity, actor_id="main", *, write=False):
        return (await self.token_snapshot(identity, actor_id, write=write)).access_token

    async def token_snapshot(self, identity, actor_id="main", *, write=False):
        """Return one token and its own connection snapshot, including refreshed tokens."""
        connection = await self._connection(identity, actor_id, write=write)
        bundle = await self._credential(connection)
        if timestamp(bundle["expires_at"]) > self.now() + timedelta(seconds=90):
            return await self._bind_token(bundle["access_token"], connection, actor_id, write=write)
        async with self._account(identity):
            connection = await self._connection(identity, actor_id, write=write)
            bundle = await self._credential(connection)
            if timestamp(bundle["expires_at"]) > self.now() + timedelta(seconds=90):
                return await self._bind_token(
                    bundle["access_token"], connection, actor_id, write=write
                )
            return await self._refresh_token(connection, bundle, actor_id, write)

    async def _refresh_token(self, connection, bundle, actor_id, write):
        identity = connection["id"]
        try:
            lease = await self._claim("refresh", identity, once=True)
        except PersonalAgentError as exc:
            if exc.code == "connection_unknown":
                await self._credential_failure(connection, "refresh_unknown", "refresh_unknown")
            raise

        def sent(tx):
            self._fence(tx, "refresh", identity, lease, phase="sent")
            current = tx.get("connection", identity)
            self._grant(current, actor_id, write=write)
            if (
                current["auth_epoch"] != connection["auth_epoch"]
                or current["token_generation"] != connection["token_generation"]
            ):
                raise PersonalAgentError("version_conflict", "账户续期版本已变化")

        await self._write(sent)
        try:
            response = await self.oauth_factory(bundle["client"]).refresh(bundle["refresh_token"])
            renewed = self._tokens(response, write=connection["can_write"], previous=bundle)
            encrypted = await asyncio.to_thread(
                self.vault.seal,
                owner_key=self.owner_key,
                slot=connection["key_slot"],
                purpose="calendar_tokens",
                value={**renewed, "client": bundle["client"]},
            )

            def commit(tx):
                self._fence(tx, "refresh", identity, lease)
                current = tx.get("connection", identity)
                self._grant(current, actor_id, write=write)
                if (
                    current["auth_epoch"] != connection["auth_epoch"]
                    or current["token_generation"] != connection["token_generation"]
                ):
                    raise PersonalAgentError("lease_lost", "账户已断开或更新，未接纳旧续期结果")
                saved = tx.put(
                    "connection",
                    {
                        **current,
                        "credential_envelope": encrypted,
                        "token_generation": current["token_generation"] + 1,
                        "scopes": renewed["scopes"],
                        "access_expires_at": renewed["expires_at"],
                        "updated_at": self.stamp(),
                    },
                    expected_revision=current["revision"],
                )
                self._fence(tx, "refresh", identity, lease, phase="finished")
                return saved

            saved = await self._write(commit)
            return await self._bind_token(renewed["access_token"], saved, actor_id, write=write)
        except GoogleProtocolError as exc:
            await self._credential_failure(
                connection, "refresh_unknown" if exc.unknown else "reauth_required", exc.code
            )
            raise provider_error(exc) from None
        except PersonalAgentError as exc:
            await self._credential_failure(connection, "refresh_unknown", exc.code)
            raise
        except BaseException:
            await self._credential_failure(connection, "refresh_unknown", "refresh_unknown")
            raise

    async def _credential_failure(self, original, status, code):
        def fail(tx):
            current = tx.get("connection", original["id"])
            if (
                current["auth_epoch"] != original["auth_epoch"]
                or current["token_generation"] != original["token_generation"]
                or current["status"] != "connected"
            ):
                return
            tx.put(
                "connection",
                {
                    **current,
                    "status": status,
                    "auth_epoch": current["auth_epoch"] + 1,
                    "last_error_code": code,
                    "updated_at": self.stamp(),
                },
                expected_revision=current["revision"],
            )

        await self._write(fail)

    async def disconnect(self, identity, payload, *, trusted_channel):
        manual(trusted_channel)
        fields(
            payload,
            {"request_id", "expected_revision", "remote_revoke"},
            {"request_id", "expected_revision", "remote_revoke"},
        )
        if type(payload["remote_revoke"]) is not bool:
            raise PersonalAgentError("invalid_input", "远端撤销选项格式不正确")
        semantic = {"connection_id": identity, **payload}
        known = await self._known_request(payload["request_id"], "disconnect", semantic)
        if known:
            return self.connection_view(await self._record("connection", identity))
        async with self._account(identity) as lease:

            def stop(tx):
                self._fence(tx, "account", identity, lease)
                current = tx.get("connection", identity)
                if current["revision"] != integer(payload["expected_revision"], "连接版本"):
                    raise PersonalAgentError("version_conflict", "账户状态已更新，请刷新后断开")
                if current["status"] == "disconnected":
                    return {"kind": "connection", "id": identity, "old": None}
                tx.put(
                    "connection",
                    {
                        **current,
                        "status": "disconnected",
                        "auth_epoch": current["auth_epoch"] + 1,
                        "grants": {},
                        "revoke_status": "unknown" if payload["remote_revoke"] else "not_requested",
                        "revoke_attempted": True,
                        "updated_at": self.stamp(),
                    },
                    expected_revision=current["revision"],
                )
                return {"kind": "connection", "id": identity, "old": current}

            ref = await self._mutate(payload["request_id"], "disconnect", semantic, stop)
            if ref["old"] is None:
                return self.connection_view(await self._record("connection", identity))
            original = ref["old"]
            remote = "unknown" if payload["remote_revoke"] else "not_requested"
            try:
                if payload["remote_revoke"]:
                    bundle = await self._credential(original)
                    await self._write(lambda tx: self._fence(tx, "account", identity, lease))
                    await self.oauth_factory(bundle["client"]).revoke(bundle["refresh_token"])
                    remote = "confirmed"
            except GoogleProtocolError as exc:
                remote = "unknown" if exc.unknown else "error"
            except PersonalAgentError:
                remote = "error"
            finally:
                # Each authorization generation has a different key slot.
                # Even a stale cleanup never deletes a newer connection's key.
                deletion = await asyncio.to_thread(
                    self.vault.delete, owner_key=self.owner_key, slot=original["key_slot"]
                )

                def cleared(tx):
                    latest = tx.get("connection", identity)
                    if (
                        latest["status"] != "disconnected"
                        or latest["auth_epoch"] != original["auth_epoch"] + 1
                    ):
                        return latest
                    return tx.put(
                        "connection",
                        {
                            **latest,
                            "credential_envelope": None,
                            "credential_deleted": deletion["status"],
                            "revoke_status": remote,
                            "updated_at": self.stamp(),
                        },
                        expected_revision=latest["revision"],
                    )

                await self._write(cleared)
            return self.connection_view(await self._record("connection", identity))


from naumi_agent.personal.calendar_actions import CalendarActionMixin  # noqa: E402
from naumi_agent.personal.calendar_sync import CalendarSyncMixin  # noqa: E402


class PersonalConnectionService(CalendarActionMixin, CalendarSyncMixin, ConnectionAuthority):
    """One authority shared by authenticated management and identity-bound tools."""
