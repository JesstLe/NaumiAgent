"""Exact human-approved calendar proposals over the shared connection authority."""

from __future__ import annotations

import json
import re
import uuid
from datetime import timedelta

from naumi_agent.personal.artifact_models import normalize_refs
from naumi_agent.personal.connection_models import (
    ID,
    REQUEST,
    SHA,
    action_body,
    canonical,
    digest,
    fields,
    integer,
    source_ref,
    text,
    timestamp,
)
from naumi_agent.personal.google_calendar import event_matches_approval
from naumi_agent.personal.google_oauth import OPERATION_MARKER, GoogleProtocolError
from naumi_agent.personal.memory_models import normalize_ref as memory_ref
from naumi_agent.personal.models import PersonalAgentError

_CORE = (
    "id",
    "draft_revision",
    "workspace_key",
    "connection_id",
    "issuer",
    "subject",
    "auth_epoch",
    "actor_id",
    "actor_version",
    "grant",
    "operation",
    "event_id",
    "base_etag",
    "body",
    "source_refs",
    "artifact_refs",
    "memory_refs",
    "memory_item_id",
    "context_session_ids",
    "expires_at",
)
_PUBLIC = (
    "id",
    "revision",
    "status",
    "digest",
    "connection_id",
    "auth_epoch",
    "actor_id",
    "account_email",
    "operation",
    "event_id",
    "base_etag",
    "created_at",
    "updated_at",
    "expires_at",
    "source_refs",
    "artifact_refs",
    "memory_refs",
    "memory_item_id",
    "approval",
    "result",
    "job_id",
    "run_id",
    "last_error_code",
)


def action_digest(value):
    return digest({key: value[key] for key in _CORE})


def _manual(channel):
    from naumi_agent.personal.connections import manual

    manual(channel)


class CalendarActionMixin:
    """Draft/approval facts live here; AgentJob/Run remain execution owners."""

    async def _action_context(self, actor, caller_actor):
        context = {
            "artifact_refs": [],
            "memory_refs": [],
            "memory_item_id": None,
            "context_session_ids": [],
        }
        if caller_actor is None:
            return context
        session = getattr(self.engine, "_session", None)
        session_id = getattr(session, "id", None)
        if session_id:
            marker = "" if actor == "main" else actor
            if getattr(session, "personal_agent_id", "") != marker:
                raise PersonalAgentError("binding_conflict", "行动来源会话与当前 Agent 身份不一致")
            context["context_session_ids"] = [session_id]
        refs = [
            *getattr(self.engine, "_personal_artifact_refs", []),
            *getattr(self.engine, "_personal_memory_artifact_refs", []),
        ]
        artifacts = getattr(self.engine, "personal_artifacts", None)
        if artifacts is not None and session_id and artifacts.enabled:
            refs.extend(await artifacts.session_refs(session_id, actor))
        context["artifact_refs"] = normalize_refs(
            list({canonical(ref): ref for ref in refs}.values())
        )
        memories = getattr(self.engine, "_personal_memory_runtime_refs", [])
        if not isinstance(memories, list) or len(memories) > 64:
            raise PersonalAgentError("invalid_input", "本轮记忆来源超过行动上限")
        context["memory_refs"] = list(
            {canonical(ref): memory_ref(ref) for ref in memories}.values()
        )
        runtime = getattr(self.engine, "personal_memory_runtime", None)
        if runtime is not None:
            context["memory_item_id"] = runtime.item_id()
        await self._authorize_action_dependencies({**context, "actor_id": actor})
        return context

    async def _authorize_action_dependencies(self, value):
        if value["artifact_refs"]:
            artifacts = getattr(self.engine, "personal_artifacts", None)
            if artifacts is None:
                raise PersonalAgentError("source_unavailable", "行动的原私有资料边界不可用")
            await artifacts.authorize_refs(actor_id=value["actor_id"], refs=value["artifact_refs"])
        if value["memory_refs"]:
            memory = getattr(self.engine, "personal_memory", None)
            if memory is None:
                raise PersonalAgentError("source_unavailable", "行动的原记忆来源边界不可用")
            for ref in value["memory_refs"]:
                await memory.resolve_ref(ref, value["actor_id"], item_id=value["memory_item_id"])

    @staticmethod
    def _merge_action_context(previous, incoming):
        if (
            previous["memory_refs"]
            and incoming["memory_refs"]
            and previous["memory_item_id"] != incoming["memory_item_id"]
        ):
            raise PersonalAgentError(
                "action_context_conflict", "行动引用了不同事项的记忆，请分别提出草稿"
            )
        artifacts = [*previous["artifact_refs"], *incoming["artifact_refs"]]
        memories = [*previous["memory_refs"], *incoming["memory_refs"]]
        sessions = list(
            dict.fromkeys([*previous["context_session_ids"], *incoming["context_session_ids"]])
        )
        if len(sessions) > 32 or len(memories) > 128:
            raise PersonalAgentError("invalid_input", "行动来源合并超过上限")
        return {
            "artifact_refs": normalize_refs(
                list({canonical(ref): ref for ref in artifacts}.values())
            ),
            "memory_refs": list({canonical(ref): ref for ref in memories}.values()),
            "memory_item_id": previous["memory_item_id"]
            if previous["memory_refs"]
            else incoming["memory_item_id"],
            "context_session_ids": sessions,
        }

    async def _action_replay(self, request_id, operation, payload, caller_actor=None):
        text(request_id, "请求编号", 128, pattern=REQUEST)
        previous = await self._read(lambda tx: None if tx is None else tx.request(request_id))
        if previous is None:
            return None
        if previous["operation"] != operation or previous["fingerprint"] != digest(
            {"operation": operation, "payload": payload}
        ):
            raise PersonalAgentError(
                "idempotency_conflict", "同一请求编号对应不同内容，请核对原请求"
            )
        return await self.get_action(previous["result"]["id"], caller_actor)

    def _action_actor(self, payload, caller_actor):
        actor = payload.get("actor_id", caller_actor or "main")
        self.actor_version(actor)
        if caller_actor is not None and actor != caller_actor:
            raise PersonalAgentError("scope_denied", "只能为当前 Agent 提出日历行动")
        return actor

    def _action_scope(self, value, caller_actor=None):
        if value["workspace_key"] != self.workspace_key:
            raise PersonalAgentError("not_found", "该日历行动不存在")
        if caller_actor is not None:
            version = self.actor_version(caller_actor)
            if value["actor_id"] != caller_actor or value["actor_version"] != version:
                raise PersonalAgentError("not_found", "该日历行动不在当前 Agent 范围")
        if value["digest"] != action_digest(value):
            raise PersonalAgentError("storage_error", "日历行动原始正文校验未通过")
        return value

    async def _action_record(self, identity, caller_actor=None):
        text(identity, "行动编号", 35, pattern=ID)
        value = self._action_scope(await self._record("action", identity), caller_actor)
        if caller_actor is not None:
            await self._connection(value["connection_id"], caller_actor)
            await self.authorize_sources(value["source_refs"], caller_actor)
            await self._authorize_action_dependencies(value)
        return value

    @staticmethod
    def action_view(value):
        public = {key: value.get(key) for key in _PUBLIC}
        error = value.get("last_error_code")
        public["last_error_code"] = (
            error
            if isinstance(error, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", error)
            else None
            if error is None
            else "action_error"
        )
        public["body"] = {
            key: value["body"][key] for key in ("summary", "description", "start", "end")
        }
        if value.get("approval"):
            public["approval"] = {
                key: value["approval"][key]
                for key in ("revision", "digest", "expires_at", "receipt_id")
            }
        # Internal dispatch/identity/credential facts never become a tool response.
        return json.loads(canonical(public))

    async def list_actions(self, caller_actor=None):
        if caller_actor is not None:
            self.actor_version(caller_actor)
        records = await self._read(lambda tx: [] if tx is None else tx.list("action", limit=1001))
        if len(records) > 1000:
            raise PersonalAgentError("invalid_input", "日历行动超过本机管理上限")
        result = []
        for value in records:
            if value["workspace_key"] != self.workspace_key:
                continue
            if caller_actor is not None and value["actor_id"] != caller_actor:
                continue
            try:
                value = await self._action_record(value["id"], caller_actor)
            except PersonalAgentError as exc:
                if caller_actor is None or exc.code == "storage_error":
                    raise
                continue
            result.append(self.action_view(value))
        return {"actions": result}

    async def get_action(self, identity, caller_actor=None):
        return self.action_view(await self._action_record(identity, caller_actor))

    def _action_expiry(self, raw):
        expiry = timestamp(raw, "行动有效期")
        if not self.now() < expiry <= self.now() + timedelta(hours=24):
            raise PersonalAgentError("invalid_input", "行动有效期需要在未来 24 小时内")
        return expiry.isoformat(timespec="microseconds")

    async def _action_sources(self, refs, actor, *, manual=False):
        if not isinstance(refs, list) or len(refs) > 32:
            raise PersonalAgentError("invalid_input", "请选择不超过 32 个确切日程来源")
        result = []
        for raw in refs:
            ref = source_ref(raw)
            ref = await self.capture_source(ref, actor, allow_manual_rebind=manual)
            result.append(source_ref(ref))
        if len({canonical(ref) for ref in result}) != len(result):
            raise PersonalAgentError("invalid_input", "日程来源不能重复")
        await self.authorize_sources(result, actor)
        return result

    async def prepare_action(self, payload, caller_actor=None):
        fields(
            payload,
            {
                "request_id",
                "connection_id",
                "actor_id",
                "source_refs",
                "operation",
                "target_event_id",
                "base_etag",
                "body",
                "expires_at",
            },
            {"request_id", "connection_id", "source_refs", "operation", "body", "expires_at"},
        )
        operation_payload = {
            "workspace_key": self.workspace_key,
            "caller_actor": caller_actor,
            **payload,
        }
        replay = await self._action_replay(
            payload["request_id"], "prepare_action", operation_payload, caller_actor
        )
        if replay is not None:
            return replay
        actor = self._action_actor(payload, caller_actor)
        context = await self._action_context(actor, caller_actor)
        connection = await self._connection(payload["connection_id"], actor)
        refs = await self._action_sources(
            payload["source_refs"], actor, manual=caller_actor is None
        )
        body, expiry = action_body(payload["body"]), self._action_expiry(payload["expires_at"])
        operation = payload["operation"]
        if operation not in {"insert", "update"}:
            raise PersonalAgentError("invalid_input", "请选择创建或修改日程")
        if operation == "update":
            event_id = text(payload.get("target_event_id"), "原日程编号", 1024)
            etag = text(payload.get("base_etag"), "原日程版本", 512)
            # An update is limited to an exact captured object, never a guessed target.
            if not any(
                ref["connection_id"] == connection["id"] and ref["event_id"] == event_id
                for ref in refs
            ):
                raise PersonalAgentError("scope_denied", "修改日程需要包含该原对象的确切来源")
        else:
            if payload.get("target_event_id") is not None or payload.get("base_etag") is not None:
                raise PersonalAgentError("invalid_input", "创建日程不填写已有对象版本")
            event_id, etag = uuid.uuid4().hex, None

        def create(tx):
            current = tx.get("connection", connection["id"])
            self._grant(current, actor)
            if current["auth_epoch"] != connection["auth_epoch"]:
                raise PersonalAgentError("version_conflict", "账户授权已变化，请重新核对草稿")
            identity = "ca_" + uuid.uuid4().hex
            value = {
                "id": identity,
                "workspace_key": self.workspace_key,
                "status": "draft",
                "draft_revision": 1,
                "connection_id": current["id"],
                "issuer": current["issuer"],
                "subject": current["subject"],
                "auth_epoch": current["auth_epoch"],
                "account_email": current["email"],
                "actor_id": actor,
                "actor_version": self.actor_version(actor),
                "grant": self._grant(current, actor),
                "operation": operation,
                "event_id": event_id,
                "base_etag": etag,
                "body": {
                    **body,
                    "extendedProperties": {"private": {OPERATION_MARKER: identity + ":1"}},
                },
                "source_refs": refs,
                **context,
                "expires_at": expiry,
                "created_at": self.stamp(),
                "updated_at": self.stamp(),
                "approval": None,
                "result": None,
                "execution": None,
                "job_id": None,
                "run_id": None,
                "last_error_code": None,
            }
            value["digest"] = action_digest(value)
            tx.put("action", value, expected_revision=0)
            return {"kind": "action", "id": identity}

        ref = await self._mutate(
            payload["request_id"],
            "prepare_action",
            {"workspace_key": self.workspace_key, "caller_actor": caller_actor, **payload},
            create,
        )
        return await self.get_action(ref["id"], caller_actor)

    async def revise_action(self, identity, payload, caller_actor=None):
        fields(
            payload,
            {"request_id", "expected_revision", "body", "expires_at"},
            {"request_id", "expected_revision", "body", "expires_at"},
        )
        replay = await self._action_replay(
            payload["request_id"],
            "revise_action",
            {"action_id": identity, "caller_actor": caller_actor, **payload},
            caller_actor,
        )
        if replay is not None:
            return replay
        original = await self._action_record(identity, caller_actor)
        actor = original["actor_id"]
        await self._authorize_action_dependencies(original)
        context = self._merge_action_context(
            original, await self._action_context(actor, caller_actor)
        )
        await self._connection(original["connection_id"], actor)
        await self.authorize_sources(original["source_refs"], actor)
        body, expiry = action_body(payload["body"]), self._action_expiry(payload["expires_at"])

        def revise(tx):
            value = self._action_scope(tx.get("action", identity), caller_actor)
            self._check_revision(value, payload)
            if value["status"] != "draft" or value["execution"] is not None:
                raise PersonalAgentError("action_locked", "行动已批准或已提交，请核对原行动")
            self._action_current(tx, value, write=False)
            revision = value["revision"] + 1
            value = {
                **value,
                **context,
                "draft_revision": revision,
                "body": {
                    **body,
                    "extendedProperties": {
                        "private": {OPERATION_MARKER: identity + ":" + str(revision)}
                    },
                },
                "expires_at": expiry,
                "approval": None,
                "updated_at": self.stamp(),
            }
            value["digest"] = action_digest(value)
            tx.put("action", value, expected_revision=revision - 1)
            return {"kind": "action", "id": identity}

        ref = await self._mutate(
            payload["request_id"],
            "revise_action",
            {"action_id": identity, "caller_actor": caller_actor, **payload},
            revise,
        )
        return await self.get_action(ref["id"], caller_actor)

    @staticmethod
    def _check_revision(value, payload):
        if value["revision"] != integer(payload["expected_revision"], "行动版本"):
            raise PersonalAgentError("version_conflict", "日历行动已变化，请重新核对完整草稿")

    def _action_current(self, tx, value, *, write=True, expiry=True, approved=False):
        self._action_scope(value)
        if approved and (
            value["status"] not in {"approved", "dispatching", "submitted"}
            or not value.get("approval")
            or value["approval"]["digest"] != value["digest"]
            or value["approval"]["revision"] != value["draft_revision"]
            or value["approval"]["identity_sha256"] != action_digest(value)
        ):
            raise PersonalAgentError("approval_invalidated", "原行动没有当前有效的确切人工批准")
        connection = tx.get("connection", value["connection_id"])
        grant = self._grant(connection, value["actor_id"], write=write)
        if (
            connection["issuer"] != value["issuer"]
            or connection["subject"] != value["subject"]
            or connection["auth_epoch"] != value["auth_epoch"]
            or self.actor_version(value["actor_id"]) != value["actor_version"]
            or grant != value["grant"]
        ):
            raise PersonalAgentError(
                "approval_invalidated", "账户、Agent 或分享授权已变化，原批准已失效"
            )
        if expiry and timestamp(value["expires_at"]) <= self.now():
            raise PersonalAgentError("action_expired", "日历行动已过期，未重新提交")
        return connection

    async def _authorize_action(self, value, *, write=True, expiry=True):
        await self._read(
            lambda tx: self._action_current(
                tx, tx.get("action", value["id"]), write=write, expiry=expiry, approved=write
            )
        )
        await self.authorize_sources(value["source_refs"], value["actor_id"])
        await self._authorize_action_dependencies(value)

    async def approve_action(self, identity, payload, *, trusted_channel):
        _manual(trusted_channel)
        fields(
            payload,
            {"request_id", "expected_revision", "expected_digest"},
            {"request_id", "expected_revision", "expected_digest"},
        )
        text(payload["expected_digest"], "已核对草稿摘要", 64, pattern=SHA)
        replay = await self._action_replay(
            payload["request_id"],
            "approve_action",
            {"action_id": identity, "channel": trusted_channel, **payload},
        )
        if replay is not None:
            return replay
        value = await self._action_record(identity)
        await self.authorize_sources(value["source_refs"], value["actor_id"])
        await self._authorize_action_dependencies(value)
        from naumi_agent.personal.calendar_action_execution import build_action_intent

        def approve(tx):
            current = self._action_scope(tx.get("action", identity))
            self._check_revision(current, payload)
            if current["digest"] != payload["expected_digest"]:
                raise PersonalAgentError("version_conflict", "您核对的日历正文已变化，请重新确认")
            if current["status"] != "draft" or current["execution"] is not None:
                raise PersonalAgentError("action_locked", "原行动已处理，未重新签发批准")
            self._action_current(tx, current)
            approval = {
                "revision": current["draft_revision"],
                "digest": current["digest"],
                "expires_at": current["expires_at"],
                "receipt_id": "caa_" + uuid.uuid4().hex,
                "channel": trusted_channel,
                "approved_at": self.stamp(),
                "identity_sha256": digest({key: current[key] for key in _CORE}),
            }
            current = {
                **current,
                "status": "approved",
                "approval": approval,
                "updated_at": self.stamp(),
            }
            current["execution"] = build_action_intent(self, current)
            tx.put("action", current, expected_revision=current["revision"])
            return {"kind": "action", "id": identity}

        ref = await self._mutate(
            payload["request_id"],
            "approve_action",
            {"action_id": identity, "channel": trusted_channel, **payload},
            approve,
        )
        await self.action_tick()
        return await self.get_action(ref["id"])

    async def reject_action(self, identity, payload, *, trusted_channel):
        _manual(trusted_channel)
        fields(payload, {"request_id", "expected_revision"}, {"request_id", "expected_revision"})

        replay = await self._action_replay(
            payload["request_id"],
            "reject_action",
            {"action_id": identity, "channel": trusted_channel, **payload},
        )
        if replay is not None:
            return replay

        def reject(tx):
            value = self._action_scope(tx.get("action", identity))
            self._check_revision(value, payload)
            if value["status"] not in {"draft", "approved"}:
                raise PersonalAgentError("action_locked", "行动已提交，请先核对原日历对象")
            tx.put(
                "action",
                {**value, "status": "rejected", "updated_at": self.stamp()},
                expected_revision=value["revision"],
            )
            return {"kind": "action", "id": identity}

        ref = await self._mutate(
            payload["request_id"],
            "reject_action",
            {"action_id": identity, "channel": trusted_channel, **payload},
            reject,
        )
        return await self.get_action(ref["id"])

    async def _readback_action(self, value, *, actor, reauthorization=False):
        connection = await self._connection(value["connection_id"], actor)
        if connection["issuer"] != value["issuer"] or connection["subject"] != value["subject"]:
            raise PersonalAgentError("binding_conflict", "请重新验证原日历账户后核对")
        if not reauthorization:
            await self._authorize_action(value, write=False, expiry=False)
        credential = await self.token_snapshot(value["connection_id"], actor, write=False)
        current = await self.assert_token_snapshot(credential, actor)
        if (
            current["issuer"] != value["issuer"]
            or current["subject"] != value["subject"]
            or current["auth_epoch"] != connection["auth_epoch"]
        ):
            raise PersonalAgentError("approval_invalidated", "对账期间账户授权已变化")
        remote = await self.adapter.get_event(credential.access_token, value["event_id"])
        latest = await self._connection(value["connection_id"], actor)
        if (
            latest["issuer"] != value["issuer"]
            or latest["subject"] != value["subject"]
            or latest["auth_epoch"] != current["auth_epoch"]
        ):
            raise PersonalAgentError("approval_invalidated", "对账结果返回前账户授权已变化")
        if remote.get("id") != value["event_id"] or not event_matches_approval(
            remote,
            value["body"],
            operation_marker=value["body"]["extendedProperties"]["private"][OPERATION_MARKER],
        ):
            return "conflict", None
        evidence = {
            "action_id": value["id"],
            "digest": value["digest"],
            "event_id": value["event_id"],
            "etag": remote.get("etag"),
            "readback_at": self.stamp(),
            "operation_marker": value["body"]["extendedProperties"]["private"][OPERATION_MARKER],
        }
        return "verified", {
            "html_link": remote.get("htmlLink"),
            "etag": remote.get("etag"),
            "readback_at": evidence["readback_at"],
            "receipt_sha256": digest(evidence),
        }

    async def reconcile_action(self, identity, payload, caller_actor=None):
        fields(payload, {"request_id", "expected_revision"}, {"request_id", "expected_revision"})
        replay = await self._action_replay(
            payload["request_id"],
            "reconcile_action",
            {"action_id": identity, "caller_actor": caller_actor, **payload},
            caller_actor,
        )
        if replay is not None:
            return replay
        value = await self._action_record(identity, caller_actor)
        if value["status"] == "verified":
            return self.action_view(value)
        if value["status"] not in {"unknown", "submitted", "conflict", "verified"}:
            raise PersonalAgentError("action_locked", "请等待原行动结束后再核对")
        actor = "main" if caller_actor is None else caller_actor
        lease = await self._claim("action_reconcile", identity)

        def reserve(tx):
            current = self._action_scope(tx.get("action", identity), caller_actor)
            self._check_revision(current, payload)
            attempt = {
                "id": "car_" + uuid.uuid4().hex,
                "request_id": payload["request_id"],
                "status": "pending",
                "started_at": self.stamp(),
            }
            tx.put(
                "action",
                {**current, "reconciliation": attempt},
                expected_revision=current["revision"],
            )
            return {"kind": "action", "id": identity, "attempt_id": attempt["id"]}

        try:
            ref = await self._mutate(
                payload["request_id"],
                "reconcile_action",
                {"action_id": identity, "caller_actor": caller_actor, **payload},
                reserve,
            )
            value = await self._record("action", identity)
            if (
                value.get("reconciliation", {}).get("id") != ref["attempt_id"]
                or value["reconciliation"]["status"] != "pending"
            ):
                return self.action_view(value)
            status, result, error = "unknown", None, None
            try:
                status, result = await self._readback_action(
                    value, actor=actor, reauthorization=caller_actor is None
                )
            except GoogleProtocolError as exc:
                error = exc.code
            except PersonalAgentError as exc:
                error = exc.code
            except Exception:
                error = "reconciliation_error"
            if status == "conflict" and error is None:
                error = "readback_mismatch"

            def finish(tx):
                self._fence(tx, "action_reconcile", identity, lease, phase="finished")
                current = tx.get("action", identity)
                if current.get("reconciliation", {}).get("id") != ref["attempt_id"]:
                    raise PersonalAgentError("version_conflict", "原对账请求已变化")
                return tx.put(
                    "action",
                    {
                        **current,
                        "status": status,
                        "result": result,
                        "last_error_code": error,
                        "updated_at": self.stamp(),
                        "reconciliation": {
                            **current["reconciliation"],
                            "status": "done",
                            "completed_at": self.stamp(),
                            "error_code": error,
                        },
                    },
                    expected_revision=current["revision"],
                )

            return self.action_view(await self._write(finish))
        finally:

            def release(tx):
                current = tx.lease("action_reconcile", identity)
                if current and current["phase"] != "finished":
                    self._fence(tx, "action_reconcile", identity, lease, phase="finished")

            await self._write(release)

    def _calendar_action_executor(self):
        from naumi_agent.personal.calendar_action_execution import CalendarActionExecution

        if not hasattr(self, "_calendar_actions"):
            self._calendar_actions = CalendarActionExecution(self)
        return self._calendar_actions

    async def action_tick(self):
        if not self._closing:
            await self._calendar_action_executor().tick()

    async def stop_actions(self):
        if hasattr(self, "_calendar_actions"):
            executor = self._calendar_actions
            await executor.stop()
            if getattr(self, "_calendar_actions", None) is executor:
                del self._calendar_actions
