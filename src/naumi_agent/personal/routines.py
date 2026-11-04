"""Durable routine definitions over the existing once-Item execution authority."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import uuid
from datetime import UTC, datetime, timedelta

from naumi_agent.personal.artifact_models import normalize_refs, strict_json_loads
from naumi_agent.personal.item_inputs import resolve_policy
from naumi_agent.personal.memory_models import normalize_ref as memory_ref
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.owner_journal import checked_path
from naumi_agent.personal.routine_models import (
    ACTOR,
    SHA,
    canonical,
    detached,
    digest,
    fields,
    integer,
    normalize_limits,
    normalize_recovery,
    raw_definition,
    request_id,
    routine_id,
    text,
    timestamp,
)
from naumi_agent.personal.routine_schedule import (
    available_calendars,
    next_occurrences,
    normalize_schedule,
    validate_schedule,
)
from naumi_agent.personal.routine_store import RoutineStore

logger = logging.getLogger(__name__)


class RoutineCore:
    def __init__(self, engine, *, store=None, clock=None):
        self.engine = engine
        self.personal = engine.personal_agents
        self.items = engine.personal_items
        self.workspace_key = self.personal.workspace_key
        self.owner_key = hashlib.sha256(
            os.path.normcase(str(self.personal.store.db_path.resolve())).encode()
        ).hexdigest()
        self.boundary = self.personal.store.base_dir / ".personal-routines-instance-v1"
        self.clock = clock or (lambda: datetime.now(UTC))
        self.holder = "routines-" + uuid.uuid4().hex
        self._worker = None
        self._closing = False
        self._wake = asyncio.Event()
        self._tick_lock = asyncio.Lock()
        self._last_worker_error = None
        pinned = self._external_identity()
        self.store = store or RoutineStore(
            self.personal.store.base_dir / "routines",
            owner_key=self.owner_key,
            expected_instance_id=pinned,
        )
        if pinned is not None:
            if self.store.expected_instance_id not in {None, pinned}:
                raise self._broken()
            self.store.expected_instance_id = pinned

    @staticmethod
    def _broken():
        return PersonalAgentError("storage_error", "周期资料与原实例不一致，请恢复原资料库")

    def now(self):
        value = self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise self._broken()
        return value.astimezone(UTC)

    def stamp(self):
        return self.now().isoformat(timespec="microseconds")

    def _external_identity(self):
        # The pin is outside the journal directory: deleting DB plus its local
        # witness must not turn an existing routine install into a new empty one.
        if not self.boundary.exists() and not self.boundary.is_symlink():
            return None
        try:
            if self.boundary.is_symlink() or self.boundary.stat().st_nlink != 1:
                raise ValueError()
            if self.boundary.stat().st_size > 1024:
                raise ValueError()
            value = strict_json_loads(self.boundary.read_bytes())
            fields(
                value,
                {"version", "owner_key", "instance_id"},
                {"version", "owner_key", "instance_id"},
            )
            if value["version"] != 1 or value["owner_key"] != self.owner_key:
                raise ValueError()
            return text(
                value["instance_id"],
                "原周期库身份",
                32,
                pattern=re.compile(r"^[a-f0-9]{32}$"),
            )
        except (OSError, ValueError, KeyError, TypeError, PersonalAgentError):
            raise self._broken() from None

    def _guard(self):
        try:
            checked_path(self.boundary, self.store.spec)
            pinned = self._external_identity()
            if pinned:
                self.store.expected_instance_id = pinned
            identity = self.store.identity()
            if pinned and identity != pinned:
                raise self._broken()
            if identity is not None:
                value = {"version": 1, "owner_key": self.owner_key, "instance_id": identity}
                self.boundary.parent.mkdir(parents=True, exist_ok=True)
                if not self.boundary.exists():
                    try:
                        with self.boundary.open("xb") as stream:
                            stream.write((canonical(value) + "\n").encode())
                            stream.flush()
                            os.fsync(stream.fileno())
                    except FileExistsError:
                        pass
                if self._external_identity() != identity:
                    raise self._broken()
                self.store.expected_instance_id = identity
        except (OSError, ValueError, TypeError):
            raise self._broken() from None

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

    async def _record(self, kind, identity, *, revision=None, missing=False):
        def read(tx):
            if tx is None:
                if missing:
                    return None
                raise PersonalAgentError("not_found", "原周期记录不存在")
            return tx.get(kind, identity, revision=revision, missing=missing)

        return await self._read(read)

    def actor_version(self, actor):
        text(actor, "当前 Agent", 35, pattern=ACTOR)
        return 0 if actor == "main" else self.personal._executable(actor).version

    def _scope(self, value, caller_actor=None):
        if value.get("workspace_key") != self.workspace_key:
            raise PersonalAgentError("not_found", "该周期计划不在当前工作区")
        if caller_actor is not None:
            self.actor_version(caller_actor)
            assignee = value["definition"]["assignee"]["id"]
            if caller_actor != assignee and not (
                caller_actor == "main" and value["origin_actor_id"] == "main"
            ):
                raise PersonalAgentError("not_found", "该周期计划不在当前 Agent 范围")
        definition = value.get("definition")
        if not isinstance(definition, dict) or digest(definition) != value["definition_sha256"]:
            raise self._broken()
        if definition["route"] != self.items._route():
            raise self._broken()
        return value

    async def _routine(self, identity, caller_actor=None, *, revision=None):
        routine_id(identity)
        return self._scope(await self._record("routine", identity, revision=revision), caller_actor)

    def _operation_payload(self, payload, caller_actor):
        return {
            "workspace_key": self.workspace_key,
            "caller_actor": caller_actor,
            "payload": detached(payload),
        }

    async def _known(self, identity, operation, payload, caller_actor):
        request_id(identity)
        value = await self._read(lambda tx: None if tx is None else tx.request(identity))
        if value is not None and (
            value["operation"] != operation
            or value["fingerprint"]
            != digest(
                {
                    "operation": operation,
                    "payload": self._operation_payload(payload, caller_actor),
                }
            )
        ):
            raise PersonalAgentError("idempotency_conflict", "同一原请求对应不同周期操作")
        return value

    async def _mutate(self, identity, operation, payload, caller_actor, callback):
        request_id(identity)
        await asyncio.to_thread(self._guard)
        result = await asyncio.to_thread(
            self.store.mutate,
            identity,
            operation,
            self._operation_payload(payload, caller_actor),
            callback,
        )
        await asyncio.to_thread(self._guard)
        self._wake.set()
        return result

    async def context(self):
        return {
            "owner_key": self.owner_key,
            "workspace_key": self.workspace_key,
            "calendars": await asyncio.to_thread(available_calendars, self.now()),
            "default_policy": resolve_policy(self.engine, {}, None),
        }

    @staticmethod
    def _public_definition(definition):
        value = detached(definition)
        value["schedule"].pop("calendar_sessions", None)
        value.pop("source_context", None)
        return value

    async def _view(self, value, caller_actor=None):
        self._scope(value, caller_actor)
        budget, cursor = await self._read(
            lambda tx: (
                self.budget_view(tx, tx.get("routine", value["id"])),
                tx.get("budget", "cursor:" + value["id"], missing=True),
            )
        )
        public_budget = {
            "reserved_slots": budget["reserved_executions"],
            "started_executions": budget["started_executions"],
            "settled_cost_microusd": budget["settled_cost_microusd"],
            "reserved_cost_microusd": budget["reserved_cost_microusd"],
            "unknown_count": budget["unknown_reservations"],
            "remaining_cost_microusd": budget["remaining_cost_microusd"],
            "remaining_slots": budget["remaining_executions"],
            "accounting": "rate_card_estimate_not_provider_billing",
        }
        return {
            **{
                key: value[key]
                for key in (
                    "id",
                    "revision",
                    "status",
                    "epoch",
                    "definition_sha256",
                    "created_at",
                    "updated_at",
                )
            },
            "definition": self._public_definition(value["definition"]),
            "budget": public_budget,
            "next_due_at": None if cursor is None else cursor.get("next_due_at"),
            "attention_code": None if cursor is None else cursor.get("attention_code"),
        }

    async def get(self, identity, caller_actor=None):
        value = await self._routine(identity, caller_actor)
        await self.authorize_definition_sources(value["definition"], caller_actor)
        return await self._view(value, caller_actor)

    async def list(self, *, status=None, query="", limit=50, after=None, caller_actor=None):
        integer(limit, "每页计划数量", maximum=100)
        text(query, "周期查询", 200, empty=True)
        if status is not None and (
            type(status) is not str or status not in {"active", "paused", "cancelled", "closed"}
        ):
            raise PersonalAgentError("invalid_input", "周期状态筛选不正确")
        if after is not None:
            routine_id(after)
        if caller_actor is not None:
            self.actor_version(caller_actor)
        result, cursor = [], after
        while len(result) < limit + 1:
            page = await self._read(
                lambda tx: (
                    {"records": [], "next_after": None}
                    if tx is None
                    else tx.page(
                        "routine",
                        limit=100,
                        after=cursor,
                        statuses=None if status is None else [status],
                    )
                )
            )
            for row in page["records"]:
                cursor = row["id"]
                try:
                    self._scope(row, caller_actor)
                except PersonalAgentError as exc:
                    if exc.code == "not_found":
                        continue
                    raise
                if query.casefold() not in row["definition"]["title"].casefold():
                    continue
                result.append(row)
                if len(result) == limit + 1:
                    break
            if len(result) == limit + 1 or page["next_after"] is None:
                break
        next_after = result[limit - 1]["id"] if len(result) > limit else None
        # Model lists carry no unpinned instruction/title/source body.
        if caller_actor is not None:
            values = [
                {key: row[key] for key in ("id", "revision", "status", "epoch")}
                for row in result[:limit]
            ]
        else:
            values = [await self._view(row) for row in result[:limit]]
        return {"routines": values, "next_after": next_after}

    async def _model_sources(self, caller_actor, previous=None):
        value = {
            "artifact_refs": [],
            "memory_refs": [],
            "memory_item_id": None,
            "context_session_ids": [],
        }
        if previous:
            value.update(detached(previous))
        if caller_actor is None:
            return value
        session = getattr(self.engine, "_session", None)
        if session is None or session.personal_agent_id != (
            "" if caller_actor == "main" else caller_actor
        ):
            raise PersonalAgentError("binding_conflict", "周期提案需要当前 Agent 的有效对话")
        artifacts = self.engine.personal_artifacts
        refs = [
            *value["artifact_refs"],
            *getattr(self.engine, "_personal_artifact_refs", []),
            *getattr(self.engine, "_personal_memory_artifact_refs", []),
            *await artifacts.session_refs(session.id, caller_actor),
        ]
        value["artifact_refs"] = normalize_refs(list({canonical(r): r for r in refs}.values()))
        memories = [
            *value["memory_refs"],
            *getattr(self.engine, "_personal_memory_runtime_refs", []),
        ]
        value["memory_refs"] = list({canonical(r): memory_ref(r) for r in memories}.values())
        runtime = getattr(self.engine, "personal_memory_runtime", None)
        scope = None if runtime is None else runtime.item_id()
        if (
            value["memory_refs"]
            and previous
            and previous["memory_refs"]
            and scope != value["memory_item_id"]
        ):
            raise PersonalAgentError("scope_denied", "周期提案引用了不同事项的记忆范围")
        if memories:
            value["memory_item_id"] = scope
        value["context_session_ids"] = list(
            dict.fromkeys(
                [
                    *value["context_session_ids"],
                    session.id,
                ]
            )
        )
        if len(value["memory_refs"]) > 64 or len(value["context_session_ids"]) > 32:
            raise PersonalAgentError("input_limit", "周期来源超过上限，请拆分委托")
        return value

    async def _compile(self, raw, caller_actor, previous=None):
        raw = raw_definition(raw)
        if caller_actor not in {None, "main", raw["assignee_id"]}:
            raise PersonalAgentError("scope_denied", "只能为当前 Agent 建立周期委托")
        schedule_input = detached(raw["schedule"], "周期时间")
        if schedule_input.get("starts_at") is None:
            schedule_input["starts_at"] = (
                self.stamp() if previous is None else previous["schedule"]["starts_at"]
            )
        if previous is not None:
            old_schedule = previous["schedule"]
            input_keys = set(schedule_input)
            old_input = {key: old_schedule.get(key) for key in input_keys}
            if canonical(old_input) == canonical(schedule_input):
                schedule = validate_schedule(old_schedule)
            else:
                schedule = normalize_schedule(schedule_input, self.now())
        else:
            schedule = normalize_schedule(schedule_input, self.now())
        template = await self.items._definition(
            {
                **{
                    key: raw[key]
                    for key in (
                        "title",
                        "instructions",
                        "tags",
                        "assignee_id",
                        "assignee_version",
                        "policy",
                        "input_refs",
                        "directories",
                    )
                    if key in raw
                },
                "schedule": {"mode": "now", "timezone": schedule["timezone"]},
            },
            caller_actor,
        )
        policy = template["policy"]
        if policy["max_cost_microusd"] is None:
            raise PersonalAgentError("invalid_policy", "周期执行需要有限的单次估算额度")
        sources = await self._model_sources(
            caller_actor, None if previous is None else previous["source_context"]
        )
        result = {
            "schema_version": 1,
            "title": template["title"],
            "instructions": template["instructions"],
            "tags": template["tags"],
            "assignee": template["assignee"],
            "route": template["route"],
            "inputs": template["inputs"],
            "policy": policy,
            "schedule": schedule,
            "limits": normalize_limits(raw["limits"]),
            "recovery": normalize_recovery(raw["recovery"]),
            "source_context": sources,
        }
        await self.authorize_definition_sources(result, result["assignee"]["id"])
        return detached(result)

    async def preview(self, payload, caller_actor=None):
        fields(
            payload,
            {"request_id", "definition", "routine_id", "expected_revision"},
            {"request_id", "definition"},
        )
        known = await self._known(payload["request_id"], "preview", payload, caller_actor)
        if known:
            return await self._preview_view(
                await self._record("preview", known["result"]["id"]), caller_actor
            )
        base = None
        if (payload.get("routine_id") is None) != (payload.get("expected_revision") is None):
            raise PersonalAgentError("invalid_input", "修改预览需要原计划和确切版本")
        if payload.get("routine_id"):
            base = await self._routine(payload["routine_id"], caller_actor)
            if base["revision"] != integer(payload["expected_revision"], "原计划版本"):
                raise PersonalAgentError("version_conflict", "周期计划已变化，请刷新后预览")
        definition = await self._compile(
            payload["definition"], caller_actor, None if base is None else base["definition"]
        )
        first_after = max(self.now(), timestamp(definition["schedule"]["starts_at"])) - timedelta(
            microseconds=1
        )
        slots = next_occurrences(definition["schedule"], first_after, 5)
        if not slots:
            raise PersonalAgentError("schedule_exhausted", "规则在确认范围内没有下一次发生")
        identity, stamp = "prp_" + uuid.uuid4().hex, self.stamp()
        value = {
            "id": identity,
            "status": "ready",
            "routine_id": "" if base is None else base["id"],
            "workspace_key": self.workspace_key,
            "caller_actor": caller_actor,
            "definition": definition,
            "definition_sha256": digest(definition),
            "base_revision": None if base is None else base["revision"],
            "expires_at": (self.now() + timedelta(minutes=30)).isoformat(),
            "slots": slots,
            "created_at": stamp,
        }
        ref = await self._mutate(
            payload["request_id"],
            "preview",
            payload,
            caller_actor,
            lambda tx: self._put_ref(tx, "preview", value),
        )
        return await self._preview_view(await self._record("preview", ref["id"]), caller_actor)

    @staticmethod
    def _put_ref(tx, kind, value):
        tx.put(kind, value, expected_revision=0)
        return {"kind": kind, "id": value["id"]}

    async def _preview_view(self, value, caller_actor=None):
        if value["workspace_key"] != self.workspace_key or value["caller_actor"] != caller_actor:
            raise PersonalAgentError("not_found", "原预览不在当前身份范围")
        await self.authorize_definition_sources(value["definition"], caller_actor)
        return {
            "preview_id": value["id"],
            "definition_digest": value["definition_sha256"],
            "definition": self._public_definition(value["definition"]),
            "expires_at": value["expires_at"],
            "schedule_preview": value["slots"],
            "base_routine_id": value["routine_id"] or None,
            "base_revision": value["base_revision"],
        }

    async def create(self, payload, caller_actor=None):
        fields(
            payload,
            {"request_id", "preview_id", "expected_digest", "enabled"},
            {"request_id", "preview_id", "expected_digest", "enabled"},
        )
        if type(payload["enabled"]) is not bool:
            raise PersonalAgentError("invalid_input", "请选择是否启用计划")
        known = await self._known(payload["request_id"], "create", payload, caller_actor)
        if known:
            return await self.get(known["result"]["id"], caller_actor)
        preview = await self._record("preview", payload["preview_id"])
        await self._preview_view(preview, caller_actor)
        await self.authorize_definition_sources(
            preview["definition"], preview["definition"]["assignee"]["id"]
        )

        def create(tx):
            original = self._confirm_preview(tx, payload, caller_actor)
            if original["routine_id"]:
                raise PersonalAgentError("version_conflict", "修改预览不能建立另一个计划")
            identity, stamp = "pr_" + uuid.uuid4().hex, self.stamp()
            value = {
                "id": identity,
                "status": "active" if payload["enabled"] else "paused",
                "workspace_key": self.workspace_key,
                "origin_actor_id": caller_actor or "main",
                "epoch": uuid.uuid4().hex,
                "definition": original["definition"],
                "definition_sha256": original["definition_sha256"],
                "created_at": stamp,
                "updated_at": stamp,
            }
            value = tx.put("routine", value, expected_revision=0)
            self.budget_initialize(tx, value)
            self._new_cursor(tx, value)
            return {"kind": "routine", "id": identity}

        ref = await self._mutate(payload["request_id"], "create", payload, caller_actor, create)
        return await self.get(ref["id"], caller_actor)

    def _confirm_preview(self, tx, payload, caller_actor):
        value = tx.get("preview", payload["preview_id"])
        if value["workspace_key"] != self.workspace_key or value["caller_actor"] != caller_actor:
            raise PersonalAgentError("not_found", "原预览不在当前身份范围")
        if (
            value["definition_sha256"]
            != text(payload["expected_digest"], "预览摘要", 64, pattern=SHA)
            or digest(value["definition"]) != value["definition_sha256"]
        ):
            raise PersonalAgentError("version_conflict", "周期正文与核对的预览不一致")
        if timestamp(value["expires_at"]) <= self.now():
            raise PersonalAgentError("preview_expired", "预览已过期，请重新预览后确认")
        assignee = value["definition"]["assignee"]
        if self.actor_version(assignee["id"]) != assignee["version"]:
            raise PersonalAgentError("version_conflict", "Agent 配置已变化，请重新预览后确认")
        profile = None if assignee["id"] == "main" else self.personal._executable(assignee["id"])
        if (
            resolve_policy(self.engine, value["definition"]["policy"], profile)
            != value["definition"]["policy"]
        ):
            raise PersonalAgentError("version_conflict", "执行策略已变化，请重新预览后确认")
        return value

    def _new_cursor(self, tx, routine):
        identity = "cursor:" + routine["id"]
        previous = tx.get("budget", identity, missing=True)
        schedule = routine["definition"]["schedule"]
        after = max(self.now(), timestamp(schedule["starts_at"])) - timedelta(microseconds=1)
        slots = next_occurrences(schedule, after, 1)
        return tx.put(
            "budget",
            {
                "id": identity,
                "routine_id": routine["id"],
                "status": "ready",
                "epoch": routine["epoch"],
                "cursor_after": after.isoformat(),
                "next_due_at": None if not slots else slots[0]["due_at"],
                "attention_code": None,
                "updated_at": self.stamp(),
            },
            expected_revision=0 if previous is None else previous["revision"],
        )

    async def revise(self, identity, payload, caller_actor=None):
        fields(
            payload,
            {"request_id", "preview_id", "expected_digest", "expected_revision"},
            {"request_id", "preview_id", "expected_digest", "expected_revision"},
        )
        semantic = {"routine_id": identity, **payload}
        known = await self._known(payload["request_id"], "revise", semantic, caller_actor)
        if known:
            return await self.get(known["result"]["id"], caller_actor)
        old = await self._routine(identity, caller_actor)
        preview = await self._record("preview", payload["preview_id"])
        await self._preview_view(preview, caller_actor)
        await self.authorize_definition_sources(
            preview["definition"], preview["definition"]["assignee"]["id"]
        )

        def revise(tx):
            current = self._scope(tx.get("routine", identity), caller_actor)
            original = self._confirm_preview(tx, payload, caller_actor)
            revision = integer(payload["expected_revision"], "原计划版本")
            if (
                current["revision"] != revision
                or original["routine_id"] != identity
                or original["base_revision"] != revision
                or current["status"] in {"cancelled", "closed"}
            ):
                raise PersonalAgentError("version_conflict", "周期计划已变化或结束，请核对原记录")
            changed = digest(current["definition"]["schedule"]) != digest(
                original["definition"]["schedule"]
            )
            value = {
                **current,
                "definition": original["definition"],
                "definition_sha256": original["definition_sha256"],
                "updated_at": self.stamp(),
                "epoch": uuid.uuid4().hex if changed else current["epoch"],
            }
            # The budget owner validates reductions against all original holds.
            self.budget_view(tx, value)
            saved = tx.put("routine", value, expected_revision=revision)
            if changed:
                self._new_cursor(tx, saved)
            return {"kind": "routine", "id": identity}

        del old
        ref = await self._mutate(payload["request_id"], "revise", semantic, caller_actor, revise)
        return await self.get(ref["id"], caller_actor)

    async def control(self, identity, action, payload, caller_actor=None):
        fields(payload, {"request_id", "expected_revision"}, {"request_id", "expected_revision"})
        if action not in {"pause", "resume", "cancel", "extra-run"}:
            raise PersonalAgentError("invalid_input", "周期操作不正确")
        semantic = {"routine_id": identity, **payload}
        known = await self._known(payload["request_id"], action, semantic, caller_actor)
        if known:
            return await self._request_result(known["result"], caller_actor)
        value = await self._routine(identity, caller_actor)
        if action == "extra-run":
            return await self.extra_run(value, payload, caller_actor)
        if action == "resume":
            await self.authorize_definition_sources(
                value["definition"], value["definition"]["assignee"]["id"]
            )

        def control(tx):
            current = self._scope(tx.get("routine", identity), caller_actor)
            if current["revision"] != integer(payload["expected_revision"], "原计划版本"):
                raise PersonalAgentError("version_conflict", "周期状态已变化，请刷新后操作")
            if current["status"] in {"cancelled", "closed"}:
                raise PersonalAgentError("routine_closed", "原周期已经结束，不自动恢复旧计划")
            status = {"pause": "paused", "resume": "active", "cancel": "cancelled"}[action]
            tx.put(
                "routine",
                {**current, "status": status, "updated_at": self.stamp()},
                expected_revision=current["revision"],
            )
            return {"kind": "routine", "id": identity}

        ref = await self._mutate(payload["request_id"], action, semantic, caller_actor, control)
        return await self.get(ref["id"], caller_actor)

    async def history(self, identity, caller_actor=None, *, limit=50, after=None):
        value = await self._routine(identity, caller_actor)
        integer(limit, "历史页大小", maximum=100)
        start = 0 if after is None else integer(after, "历史续读版本", minimum=0)
        stop = min(value["revision"], start + limit)
        rows = [
            await self._routine(identity, caller_actor, revision=number)
            for number in range(start + 1, stop + 1)
        ]
        return {
            "history": [await self._view(row, caller_actor) for row in rows],
            "next_after": stop if stop < value["revision"] else None,
        }

    async def _request_result(self, ref, caller_actor):
        if ref["kind"] == "routine":
            return await self.get(ref["id"], caller_actor)
        if ref["kind"] == "preview":
            return await self._preview_view(await self._record("preview", ref["id"]), caller_actor)
        if ref["kind"] == "occurrence":
            return await self.occurrence(ref["id"], caller_actor)
        raise self._broken()

    async def request_status(self, identity, caller_actor=None):
        request_id(identity)
        value = await self._read(lambda tx: None if tx is None else tx.request(identity))
        if value is None:
            return {
                "request_id": identity,
                "operation": None,
                "status": "unknown",
                "result_kind": None,
                "result_id": None,
                "result": None,
            }
        result = await self._request_result(value["result"], caller_actor)
        return {
            "request_id": identity,
            "operation": value["operation"],
            "status": "applied",
            "result_kind": value["result"]["kind"],
            "result_id": value["result"]["id"],
            "result": result,
        }

    def start(self):
        if self._worker is not None and not self._worker.done():
            return
        self._closing = False
        self._worker = asyncio.create_task(self._loop(), name="personal-routines")

    async def _loop(self):
        while not self._closing:
            self._wake.clear()
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_worker_error = (
                    exc.code if isinstance(exc, PersonalAgentError) else "routine_worker_error"
                )
                logger.error("周期恢复本轮停止：%s", self._last_worker_error)
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


from naumi_agent.personal.routine_authority import RoutineAuthorityMixin  # noqa: E402
from naumi_agent.personal.routine_budget import RoutineBudgetMixin  # noqa: E402
from naumi_agent.personal.routine_dispatch import RoutineDispatchMixin  # noqa: E402


class PersonalRoutineService(
    RoutineDispatchMixin, RoutineBudgetMixin, RoutineAuthorityMixin, RoutineCore
):
    """One definition/occurrence authority; Item/Job/Run remain execution owners."""
