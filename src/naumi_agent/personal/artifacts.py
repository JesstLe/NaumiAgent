"""One artifact broker shared by authenticated UI and bound agent tools."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from contextvars import ContextVar
from pathlib import Path
from typing import Any
from weakref import WeakKeyDictionary

from naumi_agent.personal.artifact_content import page_refs, preview
from naumi_agent.personal.artifact_models import (
    PRINCIPAL,
    REF_FIELDS,
    is_text_artifact,
    normalize_refs,
    validate_content,
)
from naumi_agent.personal.artifact_store import ArtifactStore
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.tools.output_publish import MAX_ASSET_BYTES

_INITIALIZERS = WeakKeyDictionary()
_PUBLISH_CONTEXT = ContextVar("artifact_publish_context", default=frozenset())


class PersonalArtifactService:
    def __init__(self, engine: Any, *, store: ArtifactStore | None = None):
        self.engine = engine
        self.personal = engine.personal_agents
        self.workspace_key = self.personal.workspace_key
        self.store = store or ArtifactStore(self.personal.store.base_dir / "artifacts")
        self.boundary_marker = self.personal.store.base_dir / ".artifact-boundary-v1"
        # This independent witness survives loss or replacement of personal.data_dir.
        session_db = Path(engine.config.memory.session_db_path).resolve()
        self.runtime_boundary_marker = session_db.with_name(
            f".artifact-boundary-{self.workspace_key}-v1"
        )
        self._boundary_required = False

    def _initialization_lock(self):
        loop = asyncio.get_running_loop()
        locks = _INITIALIZERS.setdefault(loop, {})
        return locks.setdefault(str(self.store.base_dir), asyncio.Lock())

    async def wait_initialized(self):
        if str(self.store.base_dir) not in _PUBLISH_CONTEXT.get():
            async with self._initialization_lock():
                pass

    @property
    def enabled(self):
        try:
            required = (
                self._boundary_required
                or self.runtime_boundary_marker.exists()
                or self.boundary_marker.exists()
                or (
                self.store.base_dir.exists() and any(self.store.base_dir.iterdir())
                )
            )
            self._boundary_required = required
            return required
        except OSError as exc:
            raise PersonalAgentError(
                "storage_error", "私有资料目录状态暂不可核验，已停止执行"
            ) from exc

    def _enable(self):
        fresh = not self.enabled
        expected = b"naumi-private-artifact-boundary-v1\n"
        for marker in (self.runtime_boundary_marker, self.boundary_marker):
            marker.parent.mkdir(parents=True, exist_ok=True)
            try:
                with marker.open("xb") as stream:
                    stream.write(expected)
                    stream.flush()
                    import os
                    os.fsync(stream.fileno())
            except FileExistsError:
                if marker.is_symlink() or marker.read_bytes() != expected:
                    raise PersonalAgentError(
                        "storage_error", "私有资料执行边界凭据不完整"
                    ) from None
        return fresh

    def require_catalog(self):
        if self.enabled:
            if not self.store.db_path.is_file() or not self.store.db_path.stat().st_size:
                raise PersonalAgentError("storage_error", "私有资料目录缺失，请恢复原目录后再继续")
            self._enable()

    async def _call(self, method: str, *, actor_id: str, _initializing=False, **kwargs):
        await self.wait_initialized()
        if not _initializing:
            self.require_catalog()
        return await asyncio.to_thread(
            getattr(self.store, method),
            workspace_key=self.workspace_key,
            actor_id=actor_id,
            **kwargs,
        )

    def actor(self, actor_id: str, *, write=False):
        if not isinstance(actor_id, str) or not PRINCIPAL.fullmatch(actor_id):
            raise PersonalAgentError("invalid_input", "请选择当前资料库所属 Agent")
        if actor_id != "main":
            profile = self.personal.get(actor_id)
            if write and profile.status != "active":
                raise PersonalAgentError("agent_inactive", "该 Agent 已停用，请恢复后再修改资料")
        return actor_id

    def engine_actor(self):
        from naumi_agent.personal.runtime import require_current_agent

        agent = require_current_agent(self.engine)
        return agent.id if agent is not None else "main"

    async def session_actor(self, session_id: str, *, write=False, allow_execution=False):
        session = await self.engine.session_store.load(session_id)
        if session is None:
            raise PersonalAgentError("not_found", "来源对话不存在")
        profile = self.personal.current(session_id)
        if session.personal_agent_id and (
            profile is None or profile.id != session.personal_agent_id
        ):
            raise PersonalAgentError("binding_conflict", "资料与原对话的 Agent 归属不一致")
        trusted = (
            allow_execution
            and getattr(self.engine, "_session", None) is not None
            and self.engine._session.id == session_id
            and getattr(self.engine, "_personal_execution_authorizer", None) is not None
            and session.personal_execution_id
            == getattr(self.engine, "_personal_collaboration_task_id", "")
        )
        if write and (
            (session.personal_execution_id and not trusted)
            or not self.personal._session_valid(session)
        ):
            raise PersonalAgentError("execution_session_readonly", "请从 Agent 日常对话添加资料")
        return self.actor(profile.id if profile is not None else "main", write=write)

    @staticmethod
    def reference(snapshot):
        return {key: snapshot[key] for key in REF_FIELDS}

    def _scope_current(self, grant: dict):
        scope = grant.get("recipient_scope", {})
        if not scope:
            self.actor(grant["recipient_id"], write=True)
            return
        for item in scope["teams"]:
            profile = self.personal.require_active_profile(item["agent_id"])
            if (
                profile.kind != "team"
                or profile.version != item["agent_version"]
                or list(profile.member_ids) != item["member_ids"]
            ):
                raise PersonalAgentError(
                    "version_conflict", "资料分享的团队配置已改变，请由资料所有者重新确认分享"
                )
        for identity in scope["member_ids"]:
            self.actor(identity, write=True)

    async def _checked(self, actor_id: str, snapshot: dict):
        grants = await self._call(
            "access_grants",
            actor_id=actor_id,
            artifact_id=snapshot["artifact_id"],
            version=snapshot["version"],
        )
        for grant in grants:
            self._scope_current(grant)
        routines = getattr(self.engine, "personal_routines", None)
        if routines is not None:
            await routines.authorize_artifact(snapshot, actor_id)
        else:
            stack, seen = [snapshot], set()
            while stack:
                value = stack.pop()
                identity = (value["artifact_id"], value["version"])
                if identity in seen:
                    continue
                seen.add(identity)
                if len(seen) > 1024:
                    raise PersonalAgentError("storage_error", "周期产物来源链超过校验上限")
                if (value.get("evidence", {}).get("routine_refs")
                        or value.get("evidence", {}).get("routine_plan_refs")):
                    raise PersonalAgentError(
                        "routine_source_unavailable", "周期资料需要原执行授权组件"
                    )
                for ref in value.get("source_refs", []):
                    stack.append(await self._call(
                        "get", actor_id=actor_id, artifact_id=ref["artifact_id"],
                        version=ref["version"],
                    ))
        today = getattr(self.engine, "personal_today", None)
        if today is not None:
            await today.authorize_artifact(snapshot, actor_id)
        else:
            stack, seen = [snapshot], set()
            while stack:
                value = stack.pop()
                identity = (value["artifact_id"], value["version"])
                if identity in seen:
                    continue
                seen.add(identity)
                if len(seen) > 4096:
                    raise PersonalAgentError("storage_error", "今日资料来源超过校验上限")
                if value.get("evidence", {}).get("today_read_refs"):
                    raise PersonalAgentError("source_unavailable", "今日资料需要原计划授权组件")
                for ref in value.get("source_refs", []):
                    stack.append(await self._call("get", actor_id=actor_id,
                        artifact_id=ref["artifact_id"], version=ref["version"]))
        notifications = getattr(self.engine, "personal_notifications", None)
        if notifications is not None:
            await notifications.authorize_artifact(snapshot, actor_id)
        else:
            stack, seen = [snapshot], set()
            while stack:
                value = stack.pop()
                identity = (value["artifact_id"], value["version"])
                if identity in seen:
                    continue
                seen.add(identity)
                if len(seen) > 4096:
                    raise PersonalAgentError("storage_error", "通知资料来源超过校验上限")
                if value.get("evidence", {}).get("notification_read_refs"):
                    raise PersonalAgentError("source_unavailable", "通知资料需要原通知授权组件")
                for ref in value.get("source_refs", []):
                    stack.append(await self._call("get", actor_id=actor_id,
                        artifact_id=ref["artifact_id"], version=ref["version"]))
        workspaces = getattr(self.engine, "personal_workspaces", None)
        if workspaces is not None:
            await workspaces.authorize_artifact(snapshot, actor_id)
        else:
            stack, seen = [snapshot], set()
            while stack:
                value = stack.pop()
                identity = (value["artifact_id"], value["version"])
                if identity in seen:
                    continue
                seen.add(identity)
                if len(seen) > 4096:
                    raise PersonalAgentError("storage_error", "工作区资料来源超过校验上限")
                if value.get("evidence", {}).get("workspace_read_refs"):
                    raise PersonalAgentError("source_unavailable", "工作区资料需要原连接授权组件")
                for ref in value.get("source_refs", []):
                    stack.append(await self._call("get", actor_id=actor_id,
                        artifact_id=ref["artifact_id"], version=ref["version"]))
        followups = getattr(self.engine, "personal_followups", None)
        if followups is not None:
            await followups.authorize_artifact(snapshot, actor_id)
        else:
            stack, seen = [snapshot], set()
            while stack:
                value = stack.pop()
                identity = (value["artifact_id"], value["version"])
                if identity in seen:
                    continue
                seen.add(identity)
                if len(seen) > 4096:
                    raise PersonalAgentError("source_unavailable", "跟进资料来源超过校验上限")
                if (value.get("evidence", {}).get("followup_read_refs")
                        or value.get("evidence", {}).get("followup_effect_refs")):
                    raise PersonalAgentError("source_unavailable", "跟进资料需要原授权组件")
                for ref in value.get("source_refs", []):
                    parent = await self._call("get", actor_id=actor_id,
                        artifact_id=ref["artifact_id"], version=ref["version"])
                    if parent["sha256"] != ref["sha256"]:
                        raise PersonalAgentError("source_unavailable", "原跟进资料摘要已变化")
                    stack.append(parent)
        research = getattr(self.engine, "personal_research", None)
        if research is not None:
            await research.authorize_artifact(snapshot, actor_id)
        else:
            stack, seen = [snapshot], set()
            while stack:
                value = stack.pop()
                identity = (value["artifact_id"], value["version"])
                if identity in seen:
                    continue
                seen.add(identity)
                if len(seen) > 4096:
                    raise PersonalAgentError("storage_error", "研究资料来源超过校验上限")
                if (value.get("evidence", {}).get("research_read_refs")
                        or value.get("evidence", {}).get("research_egress_read_refs")):
                    raise PersonalAgentError("source_unavailable", "研究资料需要原授权组件")
                for ref in value.get("source_refs", []):
                    stack.append(await self._call("get", actor_id=actor_id,
                        artifact_id=ref["artifact_id"], version=ref["version"]))
        connections = getattr(self.engine, "personal_connections", None)
        if connections is not None:
            await connections.authorize_artifact(snapshot, actor_id)
        else:
            stack, seen = [snapshot], set()
            while stack:
                value = stack.pop()
                identity = (value["artifact_id"], value["version"])
                if identity in seen:
                    continue
                seen.add(identity)
                if len(seen) > 1024:
                    raise PersonalAgentError("storage_error", "产物来源链超过校验上限")
                if (value.get("evidence", {}).get("connection_sources")
                        or value.get("evidence", {}).get("connection_action_refs")):
                    raise PersonalAgentError(
                        "connection_source_revoked", "日历资料需要当前账户授权组件"
                    )
                for ref in value.get("source_refs", []):
                    stack.append(await self._call("get", actor_id=actor_id,
                        artifact_id=ref["artifact_id"], version=ref["version"]))
        return {
            **snapshot,
            "can_revise": snapshot["owner_id"] == actor_id,
            "can_share": snapshot["owner_id"] == actor_id,
        }

    async def get(self, *, actor_id: str, artifact_id: str, version: int | None = None):
        self.actor(actor_id)
        snapshot = await self._call(
            "get", actor_id=actor_id, artifact_id=artifact_id, version=version
        )
        return await self._checked(actor_id, snapshot)

    async def list(self, *, actor_id: str, kind=None, query="", limit=50, offset=0):
        self.actor(actor_id)
        # Scope-invalid shares are not silently treated as a valid current list.
        values = await self._call(
            "list", actor_id=actor_id, kind=kind, query=query, limit=limit, offset=offset
        )
        result = []
        for value in values:
            try:
                result.append(await self._checked(actor_id, value))
            except PersonalAgentError as exc:
                if exc.code not in {
                    "version_conflict", "agent_inactive", "not_found",
                    "connection_source_revoked", "connection_revoked",
                }:
                    raise
        return result

    async def versions(self, *, actor_id: str, artifact_id: str):
        self.actor(actor_id)
        values = await self._call("versions", actor_id=actor_id, artifact_id=artifact_id)
        return [await self._checked(actor_id, value) for value in values]

    async def read(self, *, actor_id: str, artifact_id: str, version: int | None = None):
        snapshot = await self.get(actor_id=actor_id, artifact_id=artifact_id, version=version)
        content = await self._call(
            "read_content", actor_id=actor_id, artifact_id=artifact_id, version=snapshot["version"]
        )
        await self._checked(actor_id, snapshot)
        return snapshot, content

    async def preview(self, *, actor_id: str, artifact_id: str, version: int | None = None):
        snapshot, content = await self.read(
            actor_id=actor_id, artifact_id=artifact_id, version=version
        )
        return await asyncio.to_thread(preview, content, snapshot["suffix"])

    async def authorize_refs(self, *, actor_id: str, refs: list[dict]):
        normalized = await self._call("authorize_refs", actor_id=actor_id, refs=refs)
        for ref in normalized:
            snapshot = await self.get(
                actor_id=actor_id, artifact_id=ref["artifact_id"], version=ref["version"]
            )
            if snapshot["sha256"] != ref["sha256"]:
                # No latest-version or replacement-grant substitution.
                raise PersonalAgentError(
                    "artifact_access_revoked", "原资料的版本或授权已失效，请重新选择资料"
                )
            if ref["grant_id"]:
                self._scope_current(
                    await self._call("get_grant", actor_id=actor_id, grant_id=ref["grant_id"])
                )
        return normalized

    async def session_refs(self, session_id: str, actor_id: str):
        try:
            refs = await self._call("session_refs", actor_id=actor_id, session_id=session_id)
            return await self.authorize_refs(actor_id=actor_id, refs=refs)
        except PersonalAgentError as exc:
            if exc.code in {"not_found", "version_conflict", "agent_inactive"}:
                raise PersonalAgentError(
                    "artifact_access_revoked",
                    "本对话引用的资料授权已失效，请新开对话并重新选择资料",
                ) from exc
            raise

    async def bind_refs(self, session_id: str, actor_id: str, refs: list[dict]):
        await self.authorize_refs(actor_id=actor_id, refs=refs)
        result = await self._call(
            "bind_session_refs", actor_id=actor_id, session_id=session_id, refs=refs
        )
        session = getattr(self.engine, "_session", None)
        if result and session is not None and session.id == session_id:
            self.engine._personal_artifact_refs = result
            self.engine._compactor._long_term_memory = None
        return result

    async def publish(self, **kwargs):
        async with self._initialization_lock():
            token = _PUBLISH_CONTEXT.set(_PUBLISH_CONTEXT.get() | {str(self.store.base_dir)})
            try:
                return await self._publish(**kwargs)
            finally:
                _PUBLISH_CONTEXT.reset(token)

    async def _publish(
        self,
        *,
        actor_id: str,
        request_id: str,
        title: str,
        kind: str,
        filename: str,
        content: bytes,
        source_refs=None,
        evidence=None,
        artifact_id=None,
        expected_version=None,
        session_id=None,
        _trusted_execution=False,
    ):
        self.actor(actor_id, write=True)
        if (
            not isinstance(filename, str)
            or len(filename) > 240
            or not filename.strip()
            or any(c in filename for c in "/\\\0")
        ):
            raise PersonalAgentError("invalid_input", "文件名需要明确的名称和扩展名，不包含路径")
        suffix = Path(filename).suffix.lower()
        suffix = ".jpg" if suffix == ".jpeg" else suffix
        if not isinstance(content, bytes) or not 0 < len(content) <= MAX_ASSET_BYTES:
            raise PersonalAgentError("invalid_input", "文件不能为空，且不能超过 20 MB")
        await asyncio.to_thread(validate_content, content, suffix, kind=kind)
        refs = [*normalize_refs(source_refs or []), *page_refs(content, suffix)]
        if session_id:
            owner = await self.session_actor(
                session_id, write=True, allow_execution=_trusted_execution
            )
            if owner != actor_id:
                raise PersonalAgentError("binding_conflict", "资料发布与对话身份不一致")
            refs.extend(await self.session_refs(session_id, actor_id))
        refs = list({json.dumps(ref, sort_keys=True): ref for ref in refs}.values())
        refs = await self.authorize_refs(actor_id=actor_id, refs=refs)
        initialize = self._enable()
        snapshot = await self._call(
            "publish",
            _initializing=initialize,
            actor_id=actor_id,
            request_id=request_id,
            title=title,
            kind=kind,
            content=content,
            suffix=suffix,
            artifact_id=artifact_id,
            expected_version=expected_version,
            source_refs=refs,
            evidence=evidence or {},
        )
        try:
            result = await self._checked(actor_id, snapshot)
            if session_id:
                await self.attach_source(
                    session_id, actor_id, result, allow_execution=_trusted_execution
                )
            return result
        except Exception as exc:
            # The immutable version already exists; response failure is not
            # evidence of non-admission. Preserve stable replay identity.
            if isinstance(exc, PersonalAgentError):
                exc.accepted, exc.artifact_id, exc.version = (
                    True,
                    snapshot["artifact_id"],
                    snapshot["version"],
                )
            raise

    async def attach_source(self, session_id, actor_id, snapshot, *, allow_execution=False):
        if (
            await self.session_actor(session_id, write=True, allow_execution=allow_execution)
            != actor_id
        ):
            raise PersonalAgentError("binding_conflict", "来源对话不属于当前 Agent")
        # Stable content identity makes version replay independent of old chat.
        identity = (
            "artifact-"
            + hashlib.sha256(
                (
                    session_id + ":" + snapshot["artifact_id"] + ":" + str(snapshot["version"])
                ).encode()
            ).hexdigest()[:32]
        )
        path = f"artifact://{snapshot['artifact_id']}/{snapshot['version']}"
        expected = {"artifact_ref": self.reference(snapshot)}

        async def existing():
            for source in await self.engine.chat_run_store.list_sources(session_id):
                if source.id == identity:
                    if (
                        source.path != path
                        or source.metadata != expected
                        or source.kind != "artifact"
                    ):
                        raise PersonalAgentError("binding_conflict", "原资料卡与确切版本不一致")
                    return source
            return None

        source = await existing()
        if source is not None:
            return source
        try:
            return await self.engine.chat_run_store.add_source(
                session_id=session_id,
                source_id=identity,
                kind="artifact",
                title=snapshot["title"],
                path=path,
                metadata=expected,
            )
        except sqlite3.IntegrityError:
            source = await existing()
            if source is None:
                raise
            return source

    async def source_text(self, *, session_id: str, source: Any):
        actor = await self.session_actor(session_id)
        ref = source.metadata.get("artifact_ref")
        if not isinstance(ref, dict):
            raise PersonalAgentError("storage_error", "资料来源缺少确切版本凭据")
        await self.authorize_refs(actor_id=actor, refs=[ref])
        await self.bind_refs(session_id, actor, [ref])
        snapshot, content = await self.read(
            actor_id=actor, artifact_id=ref["artifact_id"], version=ref["version"]
        )
        if not is_text_artifact(snapshot):
            raise PersonalAgentError(
                "invalid_input", "该资料需要图像或文档读取工具，未将二进制内容伪装为文本"
            )
        return content.decode("utf-8-sig")[:20000]

    def team_scope(self, recipient_id):
        profile = self.personal.require_active_profile(recipient_id)
        if profile.kind != "team":
            return {}
        from naumi_agent.personal.team_models import capture_roster

        roster = capture_roster(self.personal, profile, self.engine.tool_registry)
        return {
            "kind": "team",
            "team_id": profile.id,
            "team_version": profile.version,
            "member_ids": [item["agent_id"] for item in roster["agents"]],
            "teams": roster["teams"],
        }

    @staticmethod
    def grant_view(grant):
        return {
            **grant,
            "id": grant["grant_id"],
            "status": "revoked" if grant["revoked_at"] else "active",
        }

    async def share(self, *, actor_id, artifact_id, version, recipient_id, request_id):
        self.actor(actor_id, write=True)
        self.actor(recipient_id, write=True)
        await self.get(actor_id=actor_id, artifact_id=artifact_id, version=version)
        scope = {} if recipient_id == "main" else self.team_scope(recipient_id)
        grant = await self._call(
            "share",
            actor_id=actor_id,
            artifact_id=artifact_id,
            version=version,
            recipient_id=recipient_id,
            request_id=request_id,
            recipient_scope=scope,
        )
        return self.grant_view(grant)

    async def grants(self, *, actor_id, artifact_id, version):
        self.actor(actor_id)
        await self.get(actor_id=actor_id, artifact_id=artifact_id, version=version)
        return [
            self.grant_view(grant)
            for grant in await self._call(
                "grants", actor_id=actor_id, artifact_id=artifact_id, version=version
            )
        ]

    async def revoke(self, *, actor_id, grant_id, expected_revision, request_id):
        self.actor(actor_id, write=True)
        return self.grant_view(
            await self._call(
                "revoke",
                actor_id=actor_id,
                grant_id=grant_id,
                expected_revision=expected_revision,
                request_id=request_id,
            )
        )

    async def authorize_engine(self):
        await self.wait_initialized()
        session = getattr(self.engine, "_session", None)
        self.require_catalog()
        if session is not None and self.enabled:
            self.engine._personal_artifact_refs = await self.session_refs(
                session.id, self.engine_actor()
            )
            if self.engine._personal_artifact_refs:
                self.engine._compactor._long_term_memory = None

    async def propagate(self, source_session_id, recipient_session_id, recipient_actor):
        self.require_catalog()
        if not self.enabled or source_session_id == recipient_session_id:
            return
        source_actor = await self.session_actor(source_session_id)
        refs = await self.session_refs(source_session_id, source_actor)
        target = []
        for ref in refs:
            snapshot = await self.get(
                actor_id=recipient_actor, artifact_id=ref["artifact_id"], version=ref["version"]
            )
            target.append(self.reference(snapshot))
        if target:
            await self.bind_refs(recipient_session_id, recipient_actor, target)
