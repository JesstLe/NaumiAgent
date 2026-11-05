"""Durable personal delegation over the existing scheduler and execution authorities."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from naumi_agent.personal.item_inputs import (
    capture_inputs,
    fingerprint,
    normalize_directories,
    resolve_policy,
    resolve_schedule,
)
from naumi_agent.personal.item_models import (
    PRINCIPAL,
    REQUEST_ID,
    canonical,
    integer,
    keys,
    normalize_definition,
    normalized_object,
    text,
)
from naumi_agent.personal.item_store import PersonalItemStore
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.scheduler.models import ScheduleTarget

logger = logging.getLogger(__name__)
_KNOWN_TERMINAL = frozenset({"completed", "error", "timeout", "max_turns", "cancelled"})
_BUSY = frozenset({"queued", "admitted", "claimed", "running"})
_INPUT_FIELDS = {
    "request_id",
    "title",
    "instructions",
    "tags",
    "assignee_id",
    "assignee_version",
    "schedule",
    "input_refs",
    "directories",
    "policy",
    "origin_session_id",
}


class PersonalItemService:
    """Own definitions/dispatch intents, never a shadow Job/result/read state machine."""

    def __init__(self, engine: Any, *, store=None, executor=None, clock=None):
        self.engine = engine
        self.personal = engine.personal_agents
        self.workspace_key = self.personal.workspace_key
        self.owner_key = hashlib.sha256(
            os.path.normcase(str(self.personal.store.db_path.resolve())).encode("utf-8")
        ).hexdigest()
        self.store = store or PersonalItemStore(
            self.personal.store.base_dir / "items",
            owner_key=self.owner_key,
        )
        self.clock = clock or (lambda: datetime.now(UTC))
        self.scheduler = engine.scheduler_runner
        if executor is None:
            from naumi_agent.personal.item_execution import PersonalItemExecutionService

            executor = PersonalItemExecutionService(
                engine,
                self.store,
                self.owner_key,
                validate_intent=self.validate_intent,
            )
        self.executor = executor
        self._worker = None
        self._wake = asyncio.Event()
        self._tick_lock = asyncio.Lock()
        self._mutation_lock = asyncio.Lock()
        self._required_identity = None
        self._closing = False
        session_db = Path(engine.config.memory.session_db_path).resolve()
        self.witness = session_db.with_name(f".personal-items-{self.workspace_key}-v1")

    def _guard(self):
        identity = self.store.identity(owner_key=self.owner_key)
        try:
            witness_exists = self.witness.exists() or self.witness.is_symlink()
            if witness_exists:
                if self.witness.is_symlink():
                    raise ValueError("witness alias")
                value = json.loads(self.witness.read_text(encoding="utf-8"))
                expected = {
                    "version": 1,
                    "owner_key": self.owner_key,
                    "workspace_key": self.workspace_key,
                    "instance_id": identity,
                }
                if value != expected or not identity:
                    raise ValueError("store instance changed")
            if self._required_identity and identity != self._required_identity:
                raise ValueError("store disappeared")
            if identity and not witness_exists:
                self.witness.parent.mkdir(parents=True, exist_ok=True)
                value = {
                    "version": 1,
                    "owner_key": self.owner_key,
                    "workspace_key": self.workspace_key,
                    "instance_id": identity,
                }
                try:
                    with self.witness.open("xb") as stream:
                        stream.write((canonical(value) + "\n").encode("utf-8"))
                        stream.flush()
                        os.fsync(stream.fileno())
                except FileExistsError:
                    if (
                        self.witness.is_symlink()
                        or json.loads(
                            self.witness.read_text(encoding="utf-8"),
                        )
                        != value
                    ):
                        raise ValueError("witness changed") from None
            if identity:
                self._required_identity = identity
        except (OSError, ValueError) as exc:
            raise PersonalAgentError(
                "storage_error",
                "个人事项资料库与原执行凭据不一致，请恢复原目录后继续",
            ) from exc

    async def _call(self, method, **kwargs):
        await asyncio.to_thread(self._guard)
        result = await asyncio.to_thread(
            getattr(self.store, method),
            owner_key=self.owner_key,
            **kwargs,
        )
        await asyncio.to_thread(self._guard)
        return result

    def _caller(self, actor):
        if actor is not None and (not isinstance(actor, str) or not PRINCIPAL.fullmatch(actor)):
            raise PersonalAgentError("invalid_input", "事项调用身份无效")
        return actor

    def _scope(self, item, caller_actor=None):
        caller = self._caller(caller_actor)
        if item["definition"]["route"] != self._route():
            raise PersonalAgentError("not_found", "该个人事项不存在")
        definition = item["definition"]
        if (
            caller is not None
            and caller != definition["assignee"]["id"]
            and not (caller == "main" and definition["origin"]["actor_id"] == "main")
        ):
            raise PersonalAgentError("not_found", "该个人事项不存在")
        return item

    def _route(self):
        return {
            "workspace_key": self.workspace_key,
            "workspace_root": self.personal.workspace_root,
            "session_db_key": self.personal.session_db_key,
        }

    async def _item(self, item_id, caller_actor=None):
        return self._scope(await self._call("get", item_id=item_id), caller_actor)

    @staticmethod
    def schedule_identity(item):
        binding = {
            "item_id": item["item_id"],
            "once_key": item["once_key"],
            "definition_sha256": item["definition_sha256"],
        }
        sha = fingerprint(binding)
        return "sch_pi_" + sha, sha, canonical(binding)

    async def _sync_schedule(self, item):
        schedule_id, binding, prompt = self.schedule_identity(item)
        existing = self.scheduler.store.get_job(schedule_id)
        if existing is None:
            self.scheduler.create_personal_item(
                schedule_id=schedule_id,
                due_at=item["definition"]["schedule"]["due_at"],
                prompt=prompt,
                scope_key=self.workspace_key,
                binding_sha256=binding,
            )
        action = {
            "active": "resume",
            "paused": "pause",
            "cancelled": "cancel",
            "closed": "cancel",
        }[item["state"]]
        self.scheduler.store.transition_managed(
            schedule_id,
            scope_key=self.workspace_key,
            binding_sha256=binding,
            action=action,
        )

    async def _retire_schedule(self, item):
        schedule_id, binding, _ = self.schedule_identity(item)
        if self.scheduler.store.get_job(schedule_id) is not None:
            self.scheduler.store.transition_managed(
                schedule_id,
                scope_key=self.workspace_key,
                binding_sha256=binding,
                action="cancel",
            )

    async def _current_intent(self, item):
        return await self._call(
            "find_intent",
            item_id=item["item_id"],
            once_key=item["once_key"],
        )

    async def _decision_view(self, item):
        values = await self._call("list_decisions", item_id=item["item_id"])
        values = [v for v in values if v["once_key"] == item["once_key"]]
        if not values:
            return None
        decision = values[-1]
        return {
            "occurrence_id": "pio_" + fingerprint(decision)[:32],
            "status": "error" if decision["decision"] == "input-error" else "skipped",
            "job_id": "",
            "run_id": "",
            "execution_session_id": "",
            "artifacts": [],
            "error": decision["detail"],
            "receipt": decision,
            "delivery_id": None,
            "read_receipt": None,
        }

    async def _view(self, item, caller_actor=None):
        intent = await self._current_intent(item)
        occurrence = await self.executor.view(intent) if intent else await self._decision_view(item)
        if (
            occurrence
            and caller_actor is not None
            and (caller_actor != item["definition"]["assignee"]["id"])
        ):
            occurrence = {
                k: v
                for k, v in occurrence.items()
                if k in {"occurrence_id", "status", "job_id", "run_id", "execution_session_id"}
            }
            occurrence["artifacts"] = []
        return {**item, "occurrence": occurrence}

    async def get(self, item_id, caller_actor=None):
        return await self._view(await self._item(item_id, caller_actor), caller_actor)

    async def _all_items(self, *, state=None, query="", caller_actor=None):
        values, offset = [], 0
        while True:
            page = await self._call(
                "list",
                workspace_key=self.workspace_key,
                state=state,
                query=query,
                limit=500,
                offset=offset,
            )
            for item in page:
                try:
                    self._scope(item, caller_actor)
                except PersonalAgentError as exc:
                    if exc.code != "not_found":
                        raise
                else:
                    values.append(item)
            if len(page) < 500:
                return values
            offset += 500

    async def list(self, state=None, query="", limit=50, offset=0, caller_actor=None):
        integer(limit, "每页事项数", 1, 100)
        integer(offset, "事项位置", 0)
        self._caller(caller_actor)
        values = await self._all_items(state=state, query=query, caller_actor=caller_actor)
        page = values[offset : offset + limit]
        return {
            "items": [await self._view(v, caller_actor) for v in page],
            "limit": limit,
            "offset": offset,
            "next_offset": offset + limit if len(values) > offset + limit else None,
        }

    async def source_page(self, *, limit=50, after="", caller_actor=None):
        """Expose a bounded authorized source page without execution/report bodies."""
        integer(limit, "每页事项数", 1, 100)
        self._caller(caller_actor)
        page = await self._call(
            "list_reference_page", workspace_key=self.workspace_key,
            after_item_id=after, limit=limit,
        )
        values = []
        for value in page["items"]:
            try:
                self._scope(value, caller_actor)
            except PersonalAgentError as exc:
                if exc.code != "not_found":
                    raise
            else:
                values.append(value)
        return {"items": values, "next_after": page["next_after"],
                "partial": page["next_after"] is not None, "consistency": "live_keyset"}

    @staticmethod
    def _payload(payload, *, fields, required):
        value = normalized_object(payload, "事项请求")
        keys(value, fields, required, "事项请求")
        text(value.get("request_id"), "请求编号", 128, pattern=REQUEST_ID)
        return value

    def _fingerprint(self, operation, item_id, payload, caller):
        return fingerprint(
            {
                "operation": operation,
                "item_id": item_id,
                "payload": payload,
                "caller_actor": caller,
                "workspace_key": self.workspace_key,
            }
        )

    async def _replay(self, payload, operation, item_id, caller_actor):
        existing = await self._call("find_request", request_id=payload["request_id"])
        sha = self._fingerprint(operation, item_id, payload, caller_actor)
        if existing is not None:
            if existing.get("request_fingerprint") != sha:
                raise PersonalAgentError("idempotency_conflict", "该请求编号已用于另一项操作")
            self._scope(existing["item"], caller_actor)
        return existing, sha

    async def _definition(self, payload, caller_actor=None):
        actor = payload.get("assignee_id", "main")
        version = payload.get("assignee_version", 0)
        if caller_actor not in {None, "main", actor}:
            raise PersonalAgentError("scope_denied", "请在自己的 Agent 范围内安排事项")
        if actor == "main":
            if type(version) is not int or version != 0:
                raise PersonalAgentError("version_conflict", "主 Agent 不使用个人 Agent 版本")
            profile = None
            assignee = {"id": "main", "version": 0, "name": "主 Agent", "kind": "main"}
        else:
            profile = self.personal._executable(actor)
            if type(version) is not int or profile.version != version:
                raise PersonalAgentError("version_conflict", "Agent 配置已变更，请刷新后再委托")
            assignee = {"id": actor, "version": version, "name": profile.name, "kind": profile.kind}
        origin_id = payload.get("origin_session_id", "")
        origin_actor = caller_actor or actor
        if origin_id:
            origin_actor = await self.engine.personal_artifacts.session_actor(origin_id, write=True)
            if caller_actor is not None:
                session = getattr(self.engine, "_session", None)
                if session is None or session.id != origin_id or origin_actor != caller_actor:
                    raise PersonalAgentError("scope_denied", "事项来源与当前对话不一致")
        refs = await self.engine.personal_artifacts.authorize_refs(
            actor_id=actor,
            refs=payload.get("input_refs", []),
        )
        return normalize_definition(
            {
                "title": payload.get("title"),
                "instructions": payload.get("instructions"),
                "tags": payload.get("tags", []),
                "assignee": assignee,
                "origin": {"actor_id": origin_actor, "session_id": origin_id},
                "route": self._route(),
                "schedule": resolve_schedule(payload.get("schedule"), self.clock()),
                "inputs": {
                    "artifact_refs": refs,
                    "directories": normalize_directories(
                        self.engine, payload.get("directories", [])
                    ),
                },
                "policy": resolve_policy(self.engine, payload.get("policy"), profile),
                "output": {"destination": "personal_library", "format": "report"},
            }
        )

    async def create(self, payload, caller_actor=None):
        self._caller(caller_actor)
        payload = self._payload(
            payload,
            fields=_INPUT_FIELDS,
            required={"request_id", "title", "instructions"},
        )
        async with self._mutation_lock:
            replay, sha = await self._replay(payload, "create", "", caller_actor)
            if replay is None:
                definition = await self._definition(payload, caller_actor)
                item = await self._call(
                    "create",
                    request_id=payload["request_id"],
                    definition=definition,
                    request_fingerprint=sha,
                )
            else:
                item = await self._item(replay["item"]["item_id"], caller_actor)
            await self._sync_schedule(item)
            self._wake.set()
            return await self._view(item, caller_actor)

    async def update(self, item_id, payload, caller_actor=None):
        payload = self._payload(
            payload,
            fields=_INPUT_FIELDS | {"expected_revision"},
            required={"request_id", "expected_revision", "title", "instructions"},
        )
        async with self._mutation_lock:
            item = await self._item(item_id, caller_actor)
            replay, sha = await self._replay(payload, "update", item_id, caller_actor)
            if replay is None:
                definition = await self._definition(payload, caller_actor)
                previous = item
                item = await self._call(
                    "update_definition",
                    item_id=item_id,
                    expected_revision=payload["expected_revision"],
                    request_id=payload["request_id"],
                    definition=definition,
                    request_fingerprint=sha,
                )
                await self._retire_schedule(previous)
            await self._sync_schedule(item)
            self._wake.set()
            return await self._view(item, caller_actor)

    async def control(self, item_id, action, payload, caller_actor=None):
        if action not in {"pause", "resume", "cancel", "run-now", "restart", "close"}:
            raise PersonalAgentError("invalid_input", "事项操作无效")
        payload = self._payload(
            payload,
            fields={"request_id", "expected_revision", "schedule"},
            required={"request_id", "expected_revision"},
        )
        if "schedule" in payload and action != "restart":
            raise PersonalAgentError("invalid_input", "只有重新执行时设置新的触发时间")
        async with self._mutation_lock:
            item = await self._item(item_id, caller_actor)
            replay, sha = await self._replay(payload, action, item_id, caller_actor)
            if replay is not None:
                if action == "run-now":
                    await self._fire_now(replay["committed_item"])
                elif action == "cancel":
                    intent = await self._current_intent(replay["committed_item"])
                    if intent:
                        await self.executor.cancel(intent)
                await self._sync_schedule(item)
                self._wake.set()
                return await self._view(item, caller_actor)
            integer(payload["expected_revision"], "事项版本")
            if item["revision"] != payload["expected_revision"]:
                raise PersonalAgentError("version_conflict", "事项已更新，请刷新后再操作")
            intent = await self._current_intent(item)
            view = await self.executor.view(intent) if intent else None
            status = view["status"] if view else None
            if action == "pause" and status in _BUSY:
                raise PersonalAgentError(
                    "lifecycle_conflict", "事项已进入执行队列，请使用取消结束本次执行"
                )
            if action == "close" and intent and status not in _KNOWN_TERMINAL:
                raise PersonalAgentError(
                    "lifecycle_conflict", "原执行尚未得到明确终态，请先核对执行记录"
                )
            if action == "restart":
                if intent and status not in _KNOWN_TERMINAL:
                    raise PersonalAgentError(
                        "lifecycle_conflict",
                        "原执行尚未明确结束，请先核对原记录，不重复执行",
                    )
                if item["state"] not in {"closed", "cancelled"}:
                    raise PersonalAgentError("lifecycle_conflict", "请先结束原事项，再明确重新执行")
                definition = json.loads(canonical(item["definition"]))
                definition["schedule"] = resolve_schedule(payload.get("schedule"), self.clock())
                # Revalidate the frozen Agent, policy and grants without changing their identity.
                await self._validate_definition(definition)
                item = await self._call(
                    "restart",
                    item_id=item_id,
                    expected_revision=payload["expected_revision"],
                    request_id=payload["request_id"],
                    definition=definition,
                    request_fingerprint=sha,
                )
            else:
                item = await self._call(
                    "control",
                    item_id=item_id,
                    expected_revision=payload["expected_revision"],
                    request_id=payload["request_id"],
                    action=action,
                    request_fingerprint=sha,
                )
                if action == "cancel" and intent:
                    await self.executor.cancel(intent)
            await self._sync_schedule(item)
            if action == "run-now":
                await self._fire_now(item)
            self._wake.set()
            return await self._view(item, caller_actor)

    async def _fire_now(self, item):
        await self._sync_schedule(item)
        schedule_id, binding, _ = self.schedule_identity(item)
        self.scheduler.fire_personal_item_now(
            schedule_id,
            scope_key=self.workspace_key,
            binding_sha256=binding,
        )

    async def history(self, item_id, caller_actor=None):
        await self._item(item_id, caller_actor)
        values = await self._call("history", item_id=item_id)
        return {"history": [self._scope(v, caller_actor) for v in values]}

    async def occurrences(self, item_id, caller_actor=None):
        item = await self._item(item_id, caller_actor)
        intents = await self._intents(item_id=item_id)
        values = []
        for intent in intents:
            view = await self.executor.view(intent)
            if caller_actor is not None and caller_actor != intent["actor_id"]:
                view = {
                    k: v
                    for k, v in view.items()
                    if k
                    in {
                        "occurrence_id",
                        "status",
                        "job_id",
                        "run_id",
                        "execution_session_id",
                    }
                }
                view["artifacts"] = []
            values.append(view)
        decision = await self._decision_view(item)
        if decision:
            values.append(decision)
        return {"occurrences": values}

    async def _intents(self, item_id=None):
        result, offset = [], 0
        while True:
            page = await self._call(
                "list_intents",
                item_id=item_id,
                workspace_key=self.workspace_key,
                limit=100,
                offset=offset,
            )
            result.extend(page)
            if len(page) < 100:
                return result
            offset += 100

    async def _result(self, intent, caller_actor=None):
        item = await self._item(intent["item_id"], caller_actor)
        if caller_actor is not None and caller_actor != intent["actor_id"]:
            raise PersonalAgentError("not_found", "该结果不存在于当前 Agent 的收件箱")
        history = await self._call("history", item_id=item["item_id"])
        original = next((v for v in history if v["revision"] == intent["item_revision"]), None)
        if original is None or original["definition_sha256"] != intent["definition_sha256"]:
            raise PersonalAgentError("storage_error", "事项结果缺少原始委托修订")
        view = await self.executor.view(intent)
        if not view.get("delivery_id"):
            raise PersonalAgentError("not_found", "该执行尚未产生已送达的结果")
        definition = original["definition"]
        return {
            **view,
            "item_id": item["item_id"],
            "item_revision": intent["item_revision"],
            "title": definition["title"],
            "assignee_id": intent["actor_id"],
            "assignee_name": definition["assignee"]["name"],
            "created_at": intent["issued_at"],
        }

    async def result(self, occurrence_id, caller_actor=None):
        intent = await self._call("get_intent", occurrence_id=occurrence_id)
        if intent["workspace_key"] != self.workspace_key:
            raise PersonalAgentError("not_found", "该结果不存在")
        return await self._result(intent, caller_actor)

    async def authorize_session_read(self, session_id):
        """Historical item Runs remain tied to their exact original material grants."""
        if not isinstance(session_id, str) or not session_id.startswith("pie_"):
            return
        intents = [i for i in await self._intents() if i["execution_session_id"] == session_id]
        if len(intents) != 1:
            raise PersonalAgentError("storage_error", "事项执行记录缺少唯一原始委托")
        await self.engine.personal_artifacts.authorize_refs(
            actor_id=intents[0]["actor_id"], refs=intents[0]["input_refs"],
        )

    async def results(self, unread=False, limit=50, offset=0, caller_actor=None):
        if type(unread) is not bool:
            raise PersonalAgentError("invalid_input", "收件箱筛选无效")
        integer(limit, "每页结果数", 1, 100)
        integer(offset, "结果位置", 0)
        self._caller(caller_actor)
        values = []
        for intent in await self._intents():
            if caller_actor is not None and intent["actor_id"] != caller_actor:
                continue
            try:
                row = await self._result(intent, caller_actor)
            except PersonalAgentError as exc:
                if exc.code != "not_found":
                    raise
            else:
                if not unread or row.get("read_receipt") is None:
                    values.append(row)
        values.sort(key=lambda v: (v["created_at"], v["occurrence_id"]), reverse=True)
        return {
            "results": values[offset : offset + limit],
            "limit": limit,
            "offset": offset,
            "next_offset": offset + limit if len(values) > offset + limit else None,
        }

    async def read_result(self, occurrence_id, payload, *, trusted_channel, caller_actor=None):
        channels = {"web": "management_web", "cli": "manual_cli", "tui": "manual_cli"}
        if trusted_channel not in channels:
            raise PersonalAgentError("scope_denied", "请通过人工收件箱入口确认已读")
        payload = self._payload(
            payload,
            fields={"request_id", "delivery_id", "expected_delivery_sha256"},
            required={"request_id", "delivery_id", "expected_delivery_sha256"},
        )
        # A repeated request only acknowledges this immutable delivery/hash. No mutable read flag.
        row = await self.result(occurrence_id, caller_actor)
        if (row["delivery_id"], row["delivery_sha256"]) != (
            payload["delivery_id"],
            payload["expected_delivery_sha256"],
        ):
            raise PersonalAgentError("binding_conflict", "结果版本已变化，请核对原送达凭据")
        intent = await self._call("get_intent", occurrence_id=occurrence_id)
        receipt = await self.executor.ack(
            intent,
            delivery_id=payload["delivery_id"],
            expected_delivery_sha256=payload["expected_delivery_sha256"],
            trusted_channel=channels[trusted_channel],
        )
        if isinstance(receipt, dict) and "read_receipt" in receipt:
            receipt = receipt["read_receipt"]
        if not isinstance(receipt, dict) or not receipt:
            raise PersonalAgentError("storage_error", "人工已读尚未得到持久回执，请核对原结果")
        return {
            "read_receipt": receipt,
            "occurrence": await self.result(occurrence_id, caller_actor),
        }

    async def _validate_definition(self, definition):
        assignee = definition["assignee"]
        profile = None
        if assignee["id"] != "main":
            profile = self.personal._executable(assignee["id"])
            if profile.version != assignee["version"]:
                raise PersonalAgentError(
                    "version_conflict", "执行 Agent 的配置已变化，请重新确认委托"
                )
        if resolve_policy(self.engine, definition["policy"], profile) != definition["policy"]:
            raise PersonalAgentError("version_conflict", "本次执行策略与当前系统设置不一致")
        await self.engine.personal_artifacts.authorize_refs(
            actor_id=assignee["id"],
            refs=definition["inputs"]["artifact_refs"],
        )

    async def validate_intent(self, intent):
        item = await self._item(intent["item_id"])
        if item["once_key"] != intent["once_key"] or (
            item["definition_sha256"] != intent["definition_sha256"]
        ):
            raise PersonalAgentError("binding_conflict", "原单次委托已变化，本次执行已停止")
        if item["state"] != "active":
            raise PersonalAgentError(
                "occurrence_paused" if item["state"] == "paused" else "occurrence_cancelled",
                "事项已暂停" if item["state"] == "paused" else "事项已结束",
            )
        await self._validate_definition(item["definition"])
        if intent["actor_id"] == "main":
            from naumi_agent.personal.item_execution import main_policy_sha256

            if main_policy_sha256(self.engine) != intent["main_policy_sha256"]:
                raise PersonalAgentError(
                    "version_conflict", "主 Agent 执行配置已变化，请核对原委托"
                )
        await self.engine.personal_artifacts.authorize_refs(
            actor_id=intent["actor_id"],
            refs=intent["input_refs"],
        )
        routines = getattr(self.engine, "personal_routines", None)
        if routines is not None:
            occurrence = await routines.occurrence_for_item(intent["item_id"])
            if occurrence is not None:
                await routines.authorize_occurrence_execution(occurrence, intent)

    async def _close_terminal(self, item):
        if item["state"] not in {"active", "paused"}:
            return item
        try:
            return await self._call(
                "control",
                item_id=item["item_id"],
                expected_revision=item["revision"],
                request_id=f"auto-close-{item['once_key']}-{item['revision']}",
                action="close",
            )
        except PersonalAgentError as exc:
            if exc.code != "version_conflict":
                raise
            return await self._item(item["item_id"])

    async def _consume_event(self, event):
        try:
            binding = json.loads(event.prompt)
        except (ValueError, TypeError) as exc:
            raise PersonalAgentError("storage_error", "事项触发记录缺少原始委托绑定") from exc
        if not isinstance(binding, dict) or fingerprint(binding) != event.binding_sha256:
            raise PersonalAgentError("storage_error", "事项触发记录绑定校验未通过")
        item = await self._item(binding.get("item_id"))
        decisions = await self._call("list_decisions", item_id=item["item_id"])
        previous = next((d for d in decisions if d["event_id"] == event.id), None)
        if previous is not None:
            if (
                previous["once_key"] == item["once_key"]
                and previous["decision"]
                in {
                    "input-error",
                    "expired",
                    "skip",
                }
                and binding["definition_sha256"] == item["definition_sha256"]
            ):
                await self._close_terminal(item)
            self._consume(event)
            return
        schedule_id, sha, prompt = self.schedule_identity(item)
        if (event.schedule_id, event.binding_sha256, event.prompt) != (schedule_id, sha, prompt):
            await self._call(
                "record_decision",
                item_id=item["item_id"],
                once_key=binding["once_key"],
                event_id=event.id,
                decision="skip",
                detail="该原始触发已被用户的新委托修订替代",
            )
            self._consume(event)
            return
        if item["state"] == "paused":
            return
        intent = await self._current_intent(item)
        if intent:
            self._consume(event)
            return
        if item["state"] in {"cancelled", "closed"}:
            await self._call(
                "record_decision",
                item_id=item["item_id"],
                once_key=item["once_key"],
                event_id=event.id,
                decision="cancel-before-admission",
                detail="委托在执行受理前结束",
            )
            self._consume(event)
            return
        schedule = item["definition"]["schedule"]
        late = self.clock().astimezone(UTC) - datetime.fromisoformat(schedule["due_at"])
        if late > timedelta(seconds=schedule["grace_seconds"]):
            decision = "skip" if schedule["misfire_policy"] == "skip" else "expired"
            await self._call(
                "record_decision",
                item_id=item["item_id"],
                once_key=item["once_key"],
                event_id=event.id,
                decision=decision,
                detail="服务恢复时已超过确认的补执行窗口",
            )
            await self._close_terminal(item)
            self._consume(event)
            return
        occurrence = "pio_" + fingerprint({"item": item["item_id"], "once": item["once_key"]})[:32]
        execution_session = "pie_" + occurrence[4:]
        try:
            await self._validate_definition(item["definition"])
            refs, manifest, context = await capture_inputs(
                self.engine, item["definition"], occurrence
            )
            prepared = self.executor.prepare(
                item,
                occurrence_id=occurrence,
                execution_session_id=execution_session,
                task=item["definition"]["instructions"],
                context=context,
                input_refs=refs,
                input_manifest=manifest,
                schedule_id=event.schedule_id,
                event_id=event.id,
            )
            if hasattr(prepared, "__await__"):
                prepared = await prepared
            intent = await self._call(
                "prepare",
                item_id=item["item_id"],
                expected_definition_sha256=item["definition_sha256"],
                intent=prepared,
            )
        except PersonalAgentError as exc:
            if exc.code in {"storage_error", "binding_conflict", "version_conflict"}:
                # Races and storage UNKNOWN keep the original event for mechanical recovery.
                current = await self._item(item["item_id"])
                if (current["once_key"], current["definition_sha256"], current["state"]) != (
                    item["once_key"],
                    item["definition_sha256"],
                    "active",
                ):
                    return
                if exc.code == "storage_error":
                    raise
            winner = await self._current_intent(item)
            if winner is not None:
                self._consume(event)
                return
            await self._call(
                "record_decision",
                item_id=item["item_id"],
                once_key=item["once_key"],
                event_id=event.id,
                decision="input-error",
                detail=f"{exc.code}: {exc.message}",
            )
            await self._close_terminal(item)
            self._consume(event)
            return
        self._consume(event)
        await self.executor.submit(intent)

    def _consume(self, event):
        if not self.scheduler.store.mark_event_consumed(
            event.id,
            target=ScheduleTarget.PERSONAL_ITEM,
            scope_key=self.workspace_key,
        ):
            raise PersonalAgentError("storage_error", "原事项触发记录尚未确认消费")

    async def tick(self):
        async with self._tick_lock:
            await asyncio.to_thread(self._guard)
            for item in await self._all_items():
                await self._sync_schedule(item)
            # SchedulerRunner alone creates time events; this service only consumes its target.
            cursor = None
            while True:
                events = self.scheduler.store.pending_events(
                    limit=1000, target=ScheduleTarget.PERSONAL_ITEM,
                    scope_key=self.workspace_key, after=cursor,
                )
                for event in events:
                    await self._consume_event(event)
                if len(events) < 1000:
                    break
                cursor = (events[-1].fired_at, events[-1].id)
            for intent in await self._intents():
                item = await self._item(intent["item_id"])
                if item["state"] == "cancelled" and item["once_key"] == intent["once_key"]:
                    view = await self.executor.cancel(intent)
                elif item["state"] == "paused" and item["once_key"] == intent["once_key"]:
                    view = await self.executor.view(intent)
                else:
                    view = await self.executor.reconcile(intent)
                if view.get("status") in _KNOWN_TERMINAL and item["once_key"] == intent["once_key"]:
                    await self._close_terminal(item)

    def start(self):
        if self._worker is not None and not self._worker.done():
            return
        self._closing = False
        self._worker = asyncio.create_task(self._loop())

    async def _loop(self):
        starter = self.executor.start()
        if hasattr(starter, "__await__"):
            await starter
        while not self._closing:
            self._wake.clear()
            try:
                await self.tick()
            except Exception as exc:
                logger.error("Personal item recovery stopped for this pass: %s", type(exc).__name__)
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=1)
            except TimeoutError:
                pass

    async def stop(self):
        self._closing = True
        self._wake.set()
        if self._worker is not None:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
            self._worker = None
        result = self.executor.stop()
        if hasattr(result, "__await__"):
            await result
