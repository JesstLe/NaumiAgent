"""Owner-bound day planning; original Item, Job, Run and delivery remain authoritative."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import uuid
from datetime import UTC, datetime

from naumi_agent.personal.artifact_models import strict_json_loads
from naumi_agent.personal.item_models import (
    canonical,
    digest,
    integer,
    keys,
    normalized_object,
    text,
)
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.owner_journal import checked_path
from naumi_agent.personal.today_authority import TodayAuthorityMixin
from naumi_agent.personal.today_models import (
    ACTOR,
    day_identity,
    day_window,
    deadline_status,
    item_ref,
    metadata_identity,
    normalize_deadline,
    normalize_item_ref,
    placement_identity,
    record_digest,
    semantic_item_ref,
    validate_deadline,
)
from naumi_agent.personal.today_store import TodayStore

_REQUEST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")
_DAY = re.compile(r"^td_[a-f0-9]{32}$")
_META = re.compile(r"^tm_[a-f0-9]{32}$")
_PLACEMENT = re.compile(r"^tp_[a-f0-9]{32}$")
_CHANNELS = {"web", "cli", "tui"}
MAX_PLACEMENTS = 500


def _error(code, message):
    return PersonalAgentError(code, message)


class PersonalTodayService(TodayAuthorityMixin):
    def __init__(self, engine, *, store=None, clock=None):
        self.engine, self.personal, self.items = (
            engine,
            engine.personal_agents,
            engine.personal_items,
        )
        self.workspace_key = self.personal.workspace_key
        self.owner_key = hashlib.sha256(
            os.path.normcase(str(self.personal.store.db_path.resolve())).encode()
        ).hexdigest()
        self.boundary = self.personal.store.base_dir / ".personal-today-instance-v1"
        self.clock = clock or (lambda: datetime.now(UTC))
        pinned = self._external_identity()
        self.store = store or TodayStore(
            self.personal.store.base_dir / "today",
            owner_key=self.owner_key,
            expected_instance_id=pinned,
        )
        if pinned is not None:
            if self.store.expected_instance_id not in {None, pinned}:
                raise self._broken()
            self.store.expected_instance_id = pinned

    @staticmethod
    def _broken():
        return _error("storage_error", "今日计划与原资料库不一致，请恢复原目录后继续")

    def now(self):
        value = self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise self._broken()
        return value.astimezone(UTC)

    def stamp(self):
        return self.now().isoformat(timespec="microseconds")

    def _external_identity(self):
        if not self.boundary.exists() and not self.boundary.is_symlink():
            return None
        try:
            if self.boundary.is_symlink() or self.boundary.stat().st_nlink != 1:
                raise ValueError()
            if self.boundary.stat().st_size > 1024:
                raise ValueError()
            value = strict_json_loads(self.boundary.read_bytes())
            keys(
                value,
                {"version", "owner_key", "instance_id"},
                {"version", "owner_key", "instance_id"},
                "原计划实例",
            )
            if value["version"] != 1 or value["owner_key"] != self.owner_key:
                raise ValueError()
            return text(value["instance_id"], "原实例", 32, pattern=re.compile("^[a-f0-9]{32}$"))
        except (OSError, ValueError, TypeError, PersonalAgentError):
            raise self._broken() from None

    def _guard(self):
        try:
            checked_path(self.boundary, self.store.spec)
            pinned = self._external_identity()
            if pinned and not self.personal.store.db_path.is_file():
                raise self._broken()
            if pinned:
                self.store.expected_instance_id = pinned
            identity = self.store.identity()
            if pinned and identity != pinned:
                raise self._broken()
            if identity is not None:
                self.boundary.parent.mkdir(parents=True, exist_ok=True)
                value = {"version": 1, "owner_key": self.owner_key, "instance_id": identity}
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

    async def _record(self, kind, identity, *, revision=None, missing=False):
        def read(tx):
            if tx is None:
                if missing:
                    return None
                raise _error("not_found", "原今日记录不存在")
            return tx.get(kind, identity, revision=revision, missing=missing)

        return await self._read(read)

    def _planner(self, planner_id=None, caller_actor=None, *, write=False):
        if caller_actor is not None:
            text(caller_actor, "当前 Agent", 35, pattern=ACTOR)
            if planner_id is not None and planner_id != caller_actor:
                raise _error("scope_denied", "请从该 Agent 自己的对话管理日计划")
        actor = planner_id or caller_actor or "main"
        text(actor, "计划 Agent", 35, pattern=ACTOR)
        if actor != "main":
            value = self.personal.get(actor)
            if write and value.status != "active":
                raise _error("agent_inactive", "该 Agent 已停用，请启用后再调整计划")
        if caller_actor is not None and caller_actor != "main":
            self.personal.require_active_profile(caller_actor)
        return actor

    def _writer(self, caller_actor, trusted_channel, *, human_only=False):
        if caller_actor is None:
            if trusted_channel not in _CHANNELS:
                raise _error("scope_denied", "请从已验证的用户界面调整计划")
        elif trusted_channel is not None or human_only:
            raise _error("scope_denied", "这项计划操作需要用户本人确认")
        self._planner(caller_actor=caller_actor, write=True)

    async def context(self, caller_actor=None):
        actor = self._planner(caller_actor=caller_actor)
        planners = [{"id": "main", "name": "主 Agent", "version": 0}]
        if caller_actor is None:
            planners.extend(
                {"id": p.id, "name": p.name, "version": p.version}
                for p in self.personal.list()
                if p.status == "active"
            )
        elif actor != "main":
            p = self.personal.get(actor)
            planners = [{"id": p.id, "name": p.name, "version": p.version}]
        return {
            "workspace_key": self.workspace_key,
            "default_planner": actor,
            "planners": planners,
            "limits": {"placements": MAX_PLACEMENTS, "page": 100},
            "server_now": self.stamp(),
        }

    def _day_scope(self, value, caller_actor=None):
        if value["workspace_key"] != self.workspace_key:
            raise _error("not_found", "该日计划不在当前工作区")
        self._planner(value["planner_id"], caller_actor)
        if value["id"] != day_identity(
            self.workspace_key, value["planner_id"], value["date"], value["timezone"]
        ):
            raise self._broken()
        rows = value["placements"]
        if not isinstance(rows, list) or len(rows) > MAX_PLACEMENTS:
            raise self._broken()
        seen = set()
        for row in rows:
            if row["id"] in seen or row["disposition"] not in {"include", "exclude"}:
                raise self._broken()
            seen.add(row["id"])
            ref = normalize_item_ref(row["source_ref"])
            meta = metadata_identity(self.workspace_key, ref["item_id"])
            if (
                ref["workspace_key"] != self.workspace_key
                or row["metadata_id"] != meta
                or row["id"] != placement_identity(value["id"], meta)
            ):
                raise self._broken()
        return value

    def _virtual_day(self, date, timezone, planner):
        window = day_window(date, timezone)
        return {
            "id": day_identity(self.workspace_key, planner, date, timezone),
            "workspace_key": self.workspace_key,
            "planner_id": planner,
            "date": window["date"],
            "timezone": window["timezone"],
            "timezone_sha256": window["timezone_sha256"],
            "placements": [],
            "provenance": [],
            "created_at": None,
            "updated_at": None,
            "revision": 0,
        }

    async def _day(self, date, timezone, planner, caller_actor=None):
        empty = self._virtual_day(date, timezone, planner)
        saved = await self._record("day", empty["id"], missing=True)
        return self._day_scope(saved or empty, caller_actor)

    def _metadata_scope(self, value):
        if value["workspace_key"] != self.workspace_key:
            raise _error("not_found", "该计划字段不在当前工作区")
        ref = normalize_item_ref(value["item_ref"])
        if (
            ref["workspace_key"] != self.workspace_key
            or value["id"] != metadata_identity(self.workspace_key, ref["item_id"])
            or value["priority"] not in {"high", "normal", "low"}
            or value["status"] not in {"active", "archived"}
        ):
            raise self._broken()
        validate_deadline(value["deadline"])
        return value

    def _virtual_metadata(self, ref):
        return {
            "id": metadata_identity(self.workspace_key, ref["item_id"]),
            "workspace_key": self.workspace_key,
            "item_ref": ref,
            "priority": "normal",
            "deadline": None,
            "human_check": None,
            "status": "active",
            "created_at": None,
            "updated_at": None,
            "created_by": None,
            "last_changed_by": None,
            "provenance": [],
            "revision": 0,
        }

    async def _metadata_for(self, ref):
        identity = metadata_identity(self.workspace_key, ref["item_id"])
        value = await self._record("metadata", identity, missing=True)
        return self._metadata_scope(value or self._virtual_metadata(ref))

    async def _entry(self, ref, caller_actor=None, *, metadata=None, placement=None):
        ref = normalize_item_ref(ref)
        metadata = metadata or await self._metadata_for(ref)
        base = {
            "id": placement["id"] if placement else metadata["id"],
            "placement_id": placement["id"] if placement else None,
            "metadata_id": metadata["id"],
            "metadata_revision": metadata["revision"],
            "item_ref": ref,
            "availability": "source_unavailable",
            "title": None,
            "assignee": None,
            "item_status": None,
            "execution_status": None,
            "due_at": None,
            "deadline": None
            if metadata["deadline"] is None
            else {
                **metadata["deadline"],
                "status": deadline_status(metadata["deadline"], self.now()),
            },
            "priority": metadata["priority"],
            "human_check": metadata["human_check"],
            "metadata_status": metadata["status"],
            "planned": placement is not None,
            "disposition": placement["disposition"] if placement else "include",
            "report": None,
        }
        metadata_available = False
        try:
            await self._authorize_metadata_sources(metadata, caller_actor)
            metadata_available = True
            original, current = await self._authorize_item(ref, caller_actor)
            same = semantic_item_ref(ref) == semantic_item_ref(item_ref(current))
            same_metadata = semantic_item_ref(ref) == semantic_item_ref(metadata["item_ref"])
            availability = "available" if same and same_metadata else "stale"
            base.update(
                availability=availability,
                title=original["definition"]["title"],
                assignee={
                    key: original["definition"]["assignee"][key]
                    for key in ("id", "name", "version")
                },
                item_status=current["state"],
                due_at=original["definition"]["schedule"]["due_at"],
            )
            intent = await self.items._call(
                "find_intent", item_id=ref["item_id"], once_key=ref["once_key"]
            )
            if intent:
                view = await self.items.executor.view(intent)
                if not view.get("source_available", True):
                    raise _error("source_unavailable", "原事项或报告资料授权已失效")
                base["execution_status"] = view["status"]
                if view.get("delivery_id") and (
                    caller_actor is None or caller_actor == intent["actor_id"]
                ):
                    base["report"] = self._delivery(ref, intent, view, original)
            elif current["once_key"] == ref["once_key"]:
                decision = await self.items._decision_view(current)
                if decision:
                    base["execution_status"] = decision["status"]
            metadata_available = False
            await self._authorize_metadata_sources(metadata, caller_actor)
            metadata_available = True
            await self._authorize_item(ref, caller_actor)
        except PersonalAgentError as exc:
            if exc.code in {"storage_error", "configuration_error"}:
                raise
            base.update(
                availability="source_unavailable",
                title=None,
                assignee=None,
                report=None,
                due_at=None,
                execution_status=None,
                item_status=None,
            )
            if caller_actor is not None or not metadata_available:
                base.update(
                    deadline=None, priority="normal", human_check=None, metadata_status="active"
                )
        if base["availability"] == "stale":
            base.update(
                title=None,
                assignee=None,
                report=None,
                due_at=None,
                execution_status=None,
                item_status=None,
            )
        return base

    async def _metadata_projection(self, value, caller_actor):
        try:
            await self._authorize_metadata_sources(value, caller_actor)
        except PersonalAgentError as exc:
            if exc.code in {"storage_error", "configuration_error"}:
                raise
            return None
        return value

    @staticmethod
    def _delivery(ref, intent, view, original):
        return {
            "item_id": ref["item_id"],
            "item_revision": ref["item_revision"],
            "item_ref": ref,
            "occurrence_id": intent["occurrence_id"],
            **{
                key: view.get(key)
                for key in (
                    "job_id",
                    "run_id",
                    "publication_id",
                    "delivery_id",
                    "delivery_sha256",
                    "delivered_at",
                    "read_receipt",
                )
            },
            "artifacts": view.get("artifacts", []),
            "title": original["definition"]["title"],
            "assignee_id": intent["actor_id"],
        }

    async def sources(self, limit=50, after="", caller_actor=None):
        self._planner(caller_actor=caller_actor)
        page = await self.items.source_page(limit=limit, after=after, caller_actor=caller_actor)
        entries = [await self._entry(item_ref(item), caller_actor) for item in page["items"]]
        return {
            "entries": entries,
            "next_after": page["next_after"],
            "partial": page["partial"],
            "consistency": "live_keyset",
        }

    async def metadata(self, item_id, caller_actor=None):
        self._planner(caller_actor=caller_actor)
        current = await self.items._item(item_id, caller_actor)
        value = await self._metadata_for(item_ref(current))
        return await self._metadata_view(value, item_ref(current), caller_actor)

    async def _metadata_view(self, value, ref, caller_actor):
        entry = await self._entry(ref, caller_actor, metadata=value)
        projected = await self._metadata_projection(value, caller_actor)
        if projected is None:
            entry.update(
                availability="source_unavailable",
                title=None,
                assignee=None,
                report=None,
                due_at=None,
                execution_status=None,
                item_status=None,
                deadline=None,
                priority="normal",
                human_check=None,
                metadata_status="active",
            )
        return {
            "metadata": projected,
            "entry": entry,
            "metadata_id": value["id"],
            "revision": value["revision"],
            "digest": record_digest(value),
        }

    def _cursor(self, cursor, day):
        expected = {
            "day_id": day["id"],
            "day_revision": day["revision"],
            "workspace_key": self.workspace_key,
            "planner_id": day["planner_id"],
            "date": day["date"],
            "timezone": day["timezone"],
        }
        if cursor is None:
            return {**expected, "item_after": "", "routine_after": "", "intent_after": ""}
        text(cursor, "今日分页游标", 4096)
        value = strict_json_loads(cursor)
        keys(
            value,
            set(expected) | {"item_after", "routine_after", "intent_after"},
            set(expected) | {"item_after", "routine_after", "intent_after"},
            "今日游标",
        )
        if any(value[key] != item for key, item in expected.items()):
            raise _error("version_conflict", "日计划或视图已改变，请刷新后继续分页")
        for key, pattern in (
            ("item_after", r"pi_[a-f0-9]{32}"),
            ("routine_after", r"pr_[a-f0-9]{32}"),
            ("intent_after", r"pio_[a-f0-9]{32}"),
        ):
            value_after = value[key]
            if value_after is not None and (
                not isinstance(value_after, str)
                or value_after
                and re.fullmatch(pattern, value_after) is None
            ):
                raise _error("invalid_input", "来源分页游标不正确")
        return value

    async def _routine_page(self, after, limit, caller_actor, window):
        service = getattr(self.engine, "personal_routines", None)
        if service is None:
            return [], None, "configuration_error"
        page = await service._read(
            lambda tx: (
                {"records": [], "next_after": None}
                if tx is None
                else tx.page("routine", limit=limit, after=after or None)
            )
        )
        rows = []
        for value in page["records"]:
            try:
                service._scope(value, caller_actor)
                await service.authorize_definition_sources(value["definition"], caller_actor)
                public = await service._view(value, caller_actor)
            except PersonalAgentError as exc:
                if exc.code == "not_found":
                    continue
                if exc.code == "storage_error":
                    raise
                rows.append(
                    {
                        "id": value["id"],
                        "title": None,
                        "availability": "source_unavailable",
                        "status": value["status"],
                        "attention_code": "source_unavailable",
                        "next_due_at": None,
                        "plan_ref": service._plan_reference(value),
                        "assignee_id": None,
                    }
                )
            else:
                if not public["attention_code"] and (
                    not public["next_due_at"] or not self._in_window(public["next_due_at"], window)
                ):
                    continue
                rows.append(
                    {
                        "id": value["id"],
                        "title": value["definition"]["title"],
                        "availability": "available",
                        "status": value["status"],
                        "attention_code": public["attention_code"],
                        "next_due_at": public["next_due_at"],
                        "plan_ref": service._plan_reference(value),
                        "assignee_id": value["definition"]["assignee"]["id"],
                    }
                )
        return rows, page["next_after"], None

    async def _deliveries(self, after, limit, caller_actor, window):
        page = await self.items._call(
            "list_intent_reference_page",
            workspace_key=self.workspace_key,
            after_occurrence_id=after or "",
            limit=limit,
        )
        rows = []
        for intent in page["intents"]:
            if caller_actor is not None and caller_actor != intent["actor_id"]:
                continue
            history = await self.items._call("history", item_id=intent["item_id"])
            original = next((v for v in history if v["revision"] == intent["item_revision"]), None)
            if original is None:
                raise self._broken()
            ref = item_ref(original)
            try:
                await self._authorize_item(ref, caller_actor)
                view = await self.items.executor.view(intent)
                if not view.get("source_available", True):
                    continue
            except PersonalAgentError as exc:
                if exc.code == "storage_error":
                    raise
                continue
            delivered = view.get("delivered_at")
            if delivered and self._in_window(delivered, window) and view.get("delivery_id"):
                rows.append(self._delivery(ref, intent, view, original))
        return rows, page["next_after"]

    @staticmethod
    def _in_window(stamp, window):
        instant = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        return (
            datetime.fromisoformat(window["start_at"])
            <= instant
            < datetime.fromisoformat(window["end_at"])
        )

    async def day(self, date, timezone, planner_id=None, limit=50, cursor=None, caller_actor=None):
        integer(limit, "每页来源数", 1, 100)
        planner = self._planner(planner_id, caller_actor)
        value = await self._day(date, timezone, planner, caller_actor)
        return await self._day_view(value, limit, cursor, caller_actor)

    async def get(self, day_id, revision=None, caller_actor=None):
        text(day_id, "日计划编号", 35, pattern=_DAY)
        if revision is not None:
            integer(revision, "原日计划版本")
        value = self._day_scope(await self._record("day", day_id, revision=revision), caller_actor)
        return await self._day_view(value, 50, None, caller_actor)

    async def _day_view(self, value, limit, cursor, caller_actor):
        window = day_window(value["date"], value["timezone"])
        try:
            await self._authorize_metadata_sources(value, caller_actor)
        except PersonalAgentError as exc:
            if caller_actor is not None or exc.code in {"storage_error", "configuration_error"}:
                raise
            return await self._opaque_day_view(value, window)
        state = self._cursor(cursor, value)
        entries, included = [], set()
        for placement in value["placements"]:
            metadata = await self._metadata_for(placement["source_ref"])
            entries.append(
                await self._entry(
                    placement["source_ref"], caller_actor, metadata=metadata, placement=placement
                )
            )
            included.add(placement["source_ref"]["item_id"])
        sources = {
            "items": {"next_after": None, "partial": False},
            "routines": {"next_after": None, "partial": False},
            "deliveries": {"next_after": None, "partial": False},
        }
        routines, deliveries = [], []
        if state["item_after"] is not None:
            try:
                page = await self.sources(limit, state["item_after"], caller_actor)
                for row in page["entries"]:
                    if (
                        row["item_ref"]["item_id"] in included
                        or row["metadata_status"] == "archived"
                    ):
                        continue
                    deadline = row["deadline"]
                    bound = (
                        None
                        if deadline is None
                        else deadline.get("boundary_at", deadline.get("resolved_at"))
                    )
                    due = row["due_at"]
                    cutoff = datetime.fromisoformat(window["end_at"])
                    outstanding = row["item_status"] not in {"closed", "cancelled"}
                    relevant = due and datetime.fromisoformat(due) < cutoff and outstanding
                    relevant = (
                        relevant
                        or bound
                        and datetime.fromisoformat(bound) <= cutoff
                        and outstanding
                    )
                    if (
                        relevant
                        or row["report"]
                        and self._in_window(row["report"]["delivered_at"], window)
                    ):
                        entries.append(row)
                state["item_after"] = page["next_after"]
                sources["items"].update(next_after=page["next_after"], partial=page["partial"])
            except PersonalAgentError as exc:
                sources["items"].update(error=exc.code, partial=True)
        if state["routine_after"] is not None:
            try:
                routines, state["routine_after"], error = await self._routine_page(
                    state["routine_after"], min(limit, 50), caller_actor, window
                )
                sources["routines"].update(
                    next_after=state["routine_after"],
                    partial=state["routine_after"] is not None,
                    **({"error": error} if error else {}),
                )
            except PersonalAgentError as exc:
                sources["routines"].update(error=exc.code, partial=True)
        if state["intent_after"] is not None:
            try:
                deliveries, state["intent_after"] = await self._deliveries(
                    state["intent_after"], limit, caller_actor, window
                )
                sources["deliveries"].update(
                    next_after=state["intent_after"], partial=state["intent_after"] is not None
                )
            except PersonalAgentError as exc:
                sources["deliveries"].update(error=exc.code, partial=True)
        next_cursor = (
            canonical(state)
            if any(state[k] is not None for k in ("item_after", "routine_after", "intent_after"))
            else None
        )
        try:
            await self._authorize_metadata_sources(value, caller_actor)
        except PersonalAgentError as exc:
            if caller_actor is not None or exc.code in {"storage_error", "configuration_error"}:
                raise
            return await self._opaque_day_view(value, window)
        return {
            "workspace_key": self.workspace_key,
            "day": {**value, "digest": record_digest(value)},
            "window": window,
            "entries": entries,
            "routines": routines,
            "deliveries": deliveries,
            "sources": sources,
            "next_cursor": next_cursor,
            "observed_at": self.stamp(),
            "consistency": "live_keyset",
            "timezone_data_changed": value["timezone_sha256"] != window["timezone_sha256"],
        }

    async def _opaque_day_view(self, value, window):
        placements = sorted(value["placements"], key=lambda row: row["id"])
        entries = []
        for placement in placements:
            metadata = await self._metadata_for(placement["source_ref"])
            entries.append(
                {
                    "id": placement["id"],
                    "placement_id": placement["id"],
                    "metadata_id": placement["metadata_id"],
                    "metadata_revision": metadata["revision"],
                    "item_ref": placement["source_ref"],
                    "availability": "source_unavailable",
                    "title": None,
                    "assignee": None,
                    "item_status": None,
                    "execution_status": None,
                    "due_at": None,
                    "deadline": None,
                    "priority": "normal",
                    "human_check": None,
                    "metadata_status": "active",
                    "planned": True,
                    "disposition": placement["disposition"],
                    "report": None,
                }
            )
        day = {key: item for key, item in value.items() if key != "provenance"}
        return {
            "workspace_key": self.workspace_key,
            "day": {**day, "placements": placements, "digest": record_digest(value)},
            "window": window,
            "entries": entries,
            "routines": [],
            "deliveries": [],
            "sources": {
                key: {"next_after": None, "partial": True, "error": "source_unavailable"}
                for key in ("items", "routines", "deliveries")
            },
            "next_cursor": None,
            "observed_at": self.stamp(),
            "consistency": "live_keyset",
            "projection_unavailable": True,
            "timezone_data_changed": value["timezone_sha256"] != window["timezone_sha256"],
        }

    @staticmethod
    def _payload(payload, allowed, required):
        value = normalized_object(payload, "今日计划请求", maximum=256 * 1024)
        keys(value, allowed, required | {"request_id"}, "今日计划请求")
        text(value["request_id"], "原请求编号", 128, pattern=_REQUEST)
        return value

    def _envelope(self, payload, caller_actor, trusted_channel):
        return {
            "workspace_key": self.workspace_key,
            "caller_actor": caller_actor,
            "channel": trusted_channel,
            "payload": payload,
        }

    async def _replay(self, operation, payload, caller_actor, trusted_channel):
        existing = await self._read(
            lambda tx: None if tx is None else tx.request(payload["request_id"])
        )
        if existing is None:
            return None
        expected = digest(
            {
                "operation": operation,
                "payload": self._envelope(payload, caller_actor, trusted_channel),
            }
        )
        if existing["operation"] != operation or existing["fingerprint"] != expected:
            raise _error("idempotency_conflict", "原请求编号已绑定不同内容，请核对原请求")
        return await self._finish(existing["result"], caller_actor)

    async def _mutate(self, operation, payload, caller_actor, trusted_channel, callback):
        self.personal.store.list()
        await asyncio.to_thread(self._guard)
        result = await asyncio.to_thread(
            self.store.mutate,
            payload["request_id"],
            operation,
            self._envelope(payload, caller_actor, trusted_channel),
            callback,
        )
        # The write may be accepted even if a later cross-store source check fails.
        try:
            await asyncio.to_thread(self._guard)
        except PersonalAgentError:
            return self._opaque_receipt(result)
        return await self._finish(result, caller_actor)

    @staticmethod
    def _opaque_receipt(receipt):
        return {key: value for key, value in receipt.items() if key != "_scope"} | {
            "projection_available": False,
        }

    async def _finish(self, receipt, caller_actor):
        result = self._opaque_receipt(receipt)
        scope = receipt["_scope"]
        if scope["workspace_key"] != self.workspace_key or (
            caller_actor is not None and scope["caller_actor"] != caller_actor
        ):
            raise _error("not_found", "原请求不在当前 Agent 范围")
        try:
            self._planner(caller_actor=caller_actor)
            for ref in scope["sources"]:
                await self._authorize_item(ref, caller_actor)
            identity = result["identity"]
            metadata = [
                await self._authorized_metadata_ref(ref, caller_actor)
                for ref in identity["metadata_refs"]
            ]
            if identity.get("day_id"):
                await self._authorized_day_ref(identity["day_ref"], caller_actor)
                view = await self.get(
                    identity["day_id"], revision=identity["day_revision"], caller_actor=caller_actor
                )
                result["view"] = view
            elif metadata:
                meta = metadata[0]
                projection = await self._metadata_view(meta, meta["item_ref"], caller_actor)
                if (
                    projection["metadata"] is None
                    or projection["entry"]["availability"] == "source_unavailable"
                ):
                    raise _error("source_unavailable", "原计划字段来源授权已失效")
                result["metadata"] = projection
            result["projection_available"] = True
        except PersonalAgentError:
            result.pop("view", None)
            result.pop("metadata", None)
        return result

    def _receipt(
        self,
        tx,
        operation,
        payload,
        caller_actor,
        trusted_channel,
        *,
        day=None,
        metadata=(),
        sources=(),
        extra_identity=None,
    ):
        change_id = "tc_" + uuid.uuid4().hex
        identity = {
            "metadata_ids": list(metadata),
            "metadata_refs": [
                self._metadata_reference(tx.get("metadata", key)) for key in metadata
            ],
            "change_id": change_id,
        }
        if day is not None:
            identity.update(
                day_id=day["id"], day_revision=day["revision"], day_ref=self._day_reference(day)
            )
        if extra_identity:
            identity.update(extra_identity)
        tx.put(
            "change",
            {
                "id": change_id,
                "day_id": "" if day is None else day["id"],
                "workspace_key": self.workspace_key,
                "operation": operation,
                "request_id": payload["request_id"],
                "identity": identity,
                "caller_actor": caller_actor,
                "channel": trusted_channel,
                "occurred_at": self.stamp(),
                "source_refs": list(sources),
            },
            expected_revision=0,
        )
        return {
            "accepted": True,
            "request_id": payload["request_id"],
            "operation": operation,
            "identity": identity,
            "_scope": {
                "workspace_key": self.workspace_key,
                "caller_actor": caller_actor,
                "sources": list(sources),
            },
        }

    def _tx_day(self, tx, identity, caller_actor=None, *, empty=None, expected_revision):
        value = tx.get("day", identity, missing=True) or empty
        if value is None:
            raise _error("not_found", "该日计划不存在")
        self._day_scope(value, caller_actor)
        if value["revision"] != expected_revision:
            raise _error("version_conflict", "日计划已更新，请核对后再保存")
        return value

    def _tx_meta(self, tx, ref, expected_revision):
        value = tx.get(
            "metadata", metadata_identity(self.workspace_key, ref["item_id"]), missing=True
        )
        value = self._metadata_scope(value or self._virtual_metadata(ref))
        if value["revision"] != expected_revision:
            raise _error("version_conflict", "截止或优先级已更新，请核对后再保存")
        if semantic_item_ref(value["item_ref"]) != semantic_item_ref(ref):
            raise _error("source_stale", "事项委托已改变，请明确重新绑定原计划")
        return value

    def _changed_metadata(self, value, caller_actor, trusted_channel, **fields):
        stamp = self.stamp()
        actor = {"actor_id": caller_actor, "channel": trusted_channel}
        return {
            **value,
            **fields,
            "created_at": value["created_at"] or stamp,
            "updated_at": stamp,
            "created_by": value["created_by"] or actor,
            "last_changed_by": actor,
        }

    def _changed_day(self, value, placements):
        stamp = self.stamp()
        return {
            **value,
            "placements": placements,
            "created_at": value["created_at"] or stamp,
            "updated_at": stamp,
        }

    async def set_metadata(self, payload, caller_actor=None, trusted_channel=None):
        self._writer(caller_actor, trusted_channel)
        allowed = {
            "request_id",
            "expected_revision",
            "source_ref",
            "deadline",
            "priority",
            "status",
        }
        p = self._payload(payload, allowed, allowed - {"status"})
        integer(p["expected_revision"], "预期字段版本", 0)
        ref = normalize_item_ref(p["source_ref"])
        priority, status = p["priority"], p.get("status", "active")
        if priority not in {"high", "normal", "low"} or status not in {"active", "archived"}:
            raise _error("invalid_input", "优先级或计划字段状态不正确")
        replay = await self._replay("set_metadata", p, caller_actor, trusted_channel)
        if replay is not None:
            return replay
        deadline = normalize_deadline(p["deadline"])
        await self._authorize_item(ref, caller_actor, require_current=True)
        provenance = await self._capture_provenance(caller_actor)
        previous_meta = await self._metadata_for(ref)
        if caller_actor is not None:
            await self._authorize_metadata_sources(previous_meta, caller_actor)

        def update(tx):
            previous = self._tx_meta(tx, ref, p["expected_revision"])
            current = tx.put(
                "metadata",
                self._changed_metadata(
                    previous,
                    caller_actor,
                    trusted_channel,
                    deadline=deadline,
                    priority=priority,
                    status=status,
                    provenance=self._merge_provenance(
                        previous, provenance, reset=caller_actor is None
                    ),
                ),
                expected_revision=previous["revision"],
            )
            return self._receipt(
                tx,
                "set_metadata",
                p,
                caller_actor,
                trusted_channel,
                metadata=[current["id"]],
                sources=[ref],
            )

        return await self._mutate("set_metadata", p, caller_actor, trusted_channel, update)

    async def place_from_followup(self, effect_id):
        followups = getattr(self.engine, "personal_followups", None)
        if followups is None:
            raise _error("source_unavailable", "原跟进授权组件不可用")
        effect = await followups.resolve_effect(effect_id, expected_kind="today_place")
        return await self.place(effect["payload"], trusted_channel=effect["channel"],
                                _followup_effect=effect)

    async def place(
        self, payload, caller_actor=None, trusted_channel=None, *, _followup_effect=None
    ):
        self._writer(caller_actor, trusted_channel)
        allowed = {
            "request_id",
            "date",
            "timezone",
            "planner_id",
            "expected_day_revision",
            "expected_metadata_revision",
            "source_ref",
        }
        p = self._payload(payload, allowed, allowed - {"planner_id"})
        for key in ("expected_day_revision", "expected_metadata_revision"):
            integer(p[key], "预期计划版本", 0)
        planner = self._planner(p.get("planner_id"), caller_actor, write=True)
        ref = normalize_item_ref(p["source_ref"])
        semantic = p
        if _followup_effect is not None:
            if (caller_actor is not None or _followup_effect["payload"] != p
                    or _followup_effect["channel"] != trusted_channel
                    or _followup_effect["actor_id"] != planner):
                raise _error("scope_denied", "跟进采纳与原规划输入不一致")
            semantic = {**p, "_followup_effect_id": _followup_effect["effect_id"]}
        replay = await self._replay("place", semantic, caller_actor, trusted_channel)
        if replay is not None:
            return replay
        empty = self._virtual_day(p["date"], p["timezone"], planner)
        await self._authorize_item(ref, caller_actor, require_current=True)
        provenance = await self._capture_provenance(caller_actor)
        if _followup_effect is not None:
            artifact = _followup_effect["artifact_ref"]
            await self._artifacts().authorize_refs(actor_id=planner, refs=[artifact])
            provenance.append({"actor_id": planner, "context": {
                "artifact_refs": [artifact], "memory_refs": [], "memory_item_id": None,
                "context_session_ids": [],
            }})
        previous_day = await self._day(p["date"], p["timezone"], planner, caller_actor)
        await self._authorize_metadata_sources(previous_day, caller_actor)
        await self._authorize_metadata_sources(await self._metadata_for(ref), caller_actor)

        def update(tx):
            if _followup_effect is not None:
                self.engine.personal_followups.check_effect_commit(
                    _followup_effect["effect_id"], _followup_effect["proof"], p)
            day = self._tx_day(
                tx,
                empty["id"],
                caller_actor,
                empty=empty,
                expected_revision=p["expected_day_revision"],
            )
            meta = self._tx_meta(tx, ref, p["expected_metadata_revision"])
            if meta["status"] == "archived":
                raise _error("source_stale", "请先恢复该事项的计划字段，再加入今日")
            if meta["revision"] == 0:
                meta = tx.put(
                    "metadata",
                    self._changed_metadata(meta, caller_actor, trusted_channel),
                    expected_revision=0,
                )
            identity = placement_identity(day["id"], meta["id"])
            placements = [dict(row) for row in day["placements"]]
            target = next((row for row in placements if row["id"] == identity), None)
            if target:
                if semantic_item_ref(target["source_ref"]) != semantic_item_ref(ref):
                    raise _error("source_stale", "原今日条目需要明确重新绑定")
                target["disposition"] = "include"
            else:
                if len(placements) >= MAX_PLACEMENTS:
                    raise _error("input_limit", "单日日计划已达 500 项上限，请选择其他日期")
                placements.append(
                    {
                        "id": identity,
                        "metadata_id": meta["id"],
                        "source_ref": ref,
                        "disposition": "include",
                    }
                )
            changed = self._changed_day(day, placements)
            changed["provenance"] = self._merge_provenance(day, provenance)
            day = tx.put("day", changed, expected_revision=day["revision"])
            return self._receipt(
                tx,
                "place",
                p,
                caller_actor,
                trusted_channel,
                day=day,
                metadata=[meta["id"]],
                sources=[ref],
            )

        return await self._mutate("place", semantic, caller_actor, trusted_channel, update)

    async def _edit_day(self, operation, payload, caller_actor, trusted_channel):
        self._writer(caller_actor, trusted_channel)
        selection = "placement_ids" if operation == "reorder" else "placement_id"
        allowed = {"request_id", "day_id", "expected_revision", selection}
        p = self._payload(payload, allowed, allowed)
        text(p["day_id"], "日计划编号", 35, pattern=_DAY)
        integer(p["expected_revision"], "原日计划版本", 0)
        if operation == "reorder":
            ids = p[selection]
            if (
                not isinstance(ids, list)
                or len(ids) > MAX_PLACEMENTS
                or any(not isinstance(v, str) or _PLACEMENT.fullmatch(v) is None for v in ids)
                or len(set(ids)) != len(ids)
            ):
                raise _error("invalid_input", "排序需要完整且不重复的今日条目")
        else:
            text(p[selection], "今日条目", 35, pattern=_PLACEMENT)
        replay = await self._replay(operation, p, caller_actor, trusted_channel)
        if replay is not None:
            return replay
        original = await self._read(
            lambda tx: self._tx_day(
                tx, p["day_id"], caller_actor, expected_revision=p["expected_revision"]
            )
        )
        sources = [
            row["source_ref"]
            for row in original["placements"]
            if row["disposition"] == "include"
            and (operation == "reorder" or row["id"] == p[selection])
        ]
        if caller_actor is not None:
            for ref in sources:
                await self._authorize_item(ref, caller_actor)
        if caller_actor is not None:
            await self._authorize_metadata_sources(original, caller_actor)
        provenance = await self._capture_provenance(caller_actor)

        def update(tx):
            day = self._tx_day(
                tx, p["day_id"], caller_actor, expected_revision=p["expected_revision"]
            )
            placements = [dict(row) for row in day["placements"]]
            if operation == "reorder":
                included = {row["id"]: row for row in placements if row["disposition"] == "include"}
                if set(p[selection]) != set(included):
                    raise _error("version_conflict", "排序条目已改变，请刷新后重新排序")
                placements = [included[identity] for identity in p[selection]] + [
                    row for row in placements if row["disposition"] == "exclude"
                ]
            else:
                target = next((row for row in placements if row["id"] == p[selection]), None)
                if target is None:
                    raise _error("not_found", "原今日条目不存在")
                target["disposition"] = "exclude"
            changed = self._changed_day(day, placements)
            changed["provenance"] = self._merge_provenance(
                day, provenance, reset=caller_actor is None and operation == "reorder"
            )
            day = tx.put("day", changed, expected_revision=day["revision"])
            return self._receipt(
                tx, operation, p, caller_actor, trusted_channel, day=day, sources=sources
            )

        return await self._mutate(operation, p, caller_actor, trusted_channel, update)

    async def reorder(self, payload, caller_actor=None, trusted_channel=None):
        return await self._edit_day("reorder", payload, caller_actor, trusted_channel)

    async def remove(self, payload, caller_actor=None, trusted_channel=None):
        return await self._edit_day("remove", payload, caller_actor, trusted_channel)

    async def check(self, payload, *, trusted_channel, caller_actor=None):
        self._writer(caller_actor, trusted_channel, human_only=True)
        allowed = {"request_id", "metadata_id", "expected_revision", "source_ref", "checked"}
        p = self._payload(payload, allowed, allowed)
        if type(p["checked"]) is not bool:
            raise _error("invalid_input", "人工勾选需要明确的是或否")
        integer(p["expected_revision"], "原计划字段版本", 0)
        ref = normalize_item_ref(p["source_ref"])
        if p["metadata_id"] != metadata_identity(self.workspace_key, ref["item_id"]):
            raise _error("invalid_input", "人工勾选与原计划字段不一致")
        replay = await self._replay("check", p, caller_actor, trusted_channel)
        if replay is not None:
            return replay
        await self._authorize_item(ref, caller_actor, require_current=True)

        def update(tx):
            value = self._tx_meta(tx, ref, p["expected_revision"])
            human_check = {
                "checked": p["checked"],
                "channel": trusted_channel,
                "checked_at": self.stamp(),
                "correction_revision": value["revision"] + 1,
            }
            saved = tx.put(
                "metadata",
                self._changed_metadata(
                    value, caller_actor, trusted_channel, human_check=human_check
                ),
                expected_revision=value["revision"],
            )
            return self._receipt(
                tx, "check", p, caller_actor, trusted_channel, metadata=[saved["id"]], sources=[ref]
            )

        return await self._mutate("check", p, caller_actor, trusted_channel, update)

    async def rebase(self, payload, caller_actor=None, trusted_channel=None):
        self._writer(caller_actor, trusted_channel)
        allowed = {
            "request_id",
            "day_id",
            "expected_day_revision",
            "metadata_id",
            "expected_metadata_revision",
            "old_source_ref",
            "new_source_ref",
            "preserve_deadline",
            "preserve_priority",
        }
        p = self._payload(payload, allowed, allowed)
        for key in ("expected_day_revision", "expected_metadata_revision"):
            integer(p[key], "原计划版本", 0)
        if any(type(p[key]) is not bool for key in ("preserve_deadline", "preserve_priority")):
            raise _error("invalid_input", "重新绑定需要明确保留哪些计划字段")
        old, new = normalize_item_ref(p["old_source_ref"]), normalize_item_ref(p["new_source_ref"])
        if old["item_id"] != new["item_id"] or p["metadata_id"] != metadata_identity(
            self.workspace_key, old["item_id"]
        ):
            raise _error("invalid_input", "重新绑定必须对应同一个原始事项")
        replay = await self._replay("rebase", p, caller_actor, trusted_channel)
        if replay is not None:
            return replay
        discard_derivatives = (
            caller_actor is None and not p["preserve_deadline"] and not p["preserve_priority"]
        )
        if old["workspace_key"] != self.workspace_key:
            raise _error("not_found", "原事项不在当前工作区")
        if not discard_derivatives:
            await self._authorize_item(old, caller_actor)
        await self._authorize_item(new, caller_actor, require_current=True)
        if not discard_derivatives:
            await self._authorize_metadata_sources(await self._metadata_for(old), caller_actor)
        provenance = await self._capture_provenance(caller_actor)

        def update(tx):
            day = tx.get("day", p["day_id"], missing=True)
            if day is None:
                if p["expected_day_revision"] != 0:
                    raise _error("version_conflict", "原日计划不存在，请核对原修订")
            else:
                day = self._tx_day(
                    tx, p["day_id"], caller_actor, expected_revision=p["expected_day_revision"]
                )
            value = self._metadata_scope(tx.get("metadata", p["metadata_id"]))
            if value["revision"] != p["expected_metadata_revision"]:
                raise _error("version_conflict", "原计划字段已改变，请重新核对")
            if semantic_item_ref(value["item_ref"]) not in (
                semantic_item_ref(old),
                semantic_item_ref(new),
            ):
                raise _error("source_stale", "原计划已绑定另一个委托，请核对原修订")
            placements = [] if day is None else [dict(row) for row in day["placements"]]
            target = next((row for row in placements if row["metadata_id"] == value["id"]), None)
            if target is not None and target["source_ref"] != old:
                raise _error("source_stale", "原今日条目与所选旧修订不一致")
            if target is None and value["item_ref"] != old:
                raise _error("source_stale", "原事项计划字段与旧修订不一致")
            changed_once = semantic_item_ref(value["item_ref"]) != semantic_item_ref(new)
            value = tx.put(
                "metadata",
                self._changed_metadata(
                    value,
                    caller_actor,
                    trusted_channel,
                    item_ref=new,
                    deadline=value["deadline"] if p["preserve_deadline"] else None,
                    priority=value["priority"] if p["preserve_priority"] else "normal",
                    human_check=None
                    if changed_once or discard_derivatives
                    else value["human_check"],
                    status="active" if discard_derivatives else value["status"],
                    provenance=self._merge_provenance(value, provenance, reset=discard_derivatives),
                ),
                expected_revision=value["revision"],
            )
            if target is not None:
                target["source_ref"] = new
                changed = self._changed_day(day, placements)
                changed["provenance"] = self._merge_provenance(day, provenance)
                day = tx.put("day", changed, expected_revision=day["revision"])
            else:
                day = None
            return self._receipt(
                tx,
                "rebase",
                p,
                caller_actor,
                trusted_channel,
                day=day,
                metadata=[value["id"]],
                sources=[old, new],
            )

        return await self._mutate("rebase", p, caller_actor, trusted_channel, update)

    async def move(self, payload, *, trusted_channel, caller_actor=None):
        self._writer(caller_actor, trusted_channel, human_only=True)
        allowed = {
            "request_id",
            "day_id",
            "expected_revision",
            "placement_id",
            "target_date",
            "target_timezone",
            "target_planner_id",
            "expected_target_revision",
        }
        p = self._payload(payload, allowed, allowed - {"target_planner_id"})
        for key in ("expected_revision", "expected_target_revision"):
            integer(p[key], "原日计划版本", 0)
        replay = await self._replay("move", p, caller_actor, trusted_channel)
        if replay is not None:
            return replay
        original = self._day_scope(await self._record("day", p["day_id"]), caller_actor)
        planner = self._planner(p.get("target_planner_id") or original["planner_id"], write=True)
        empty = self._virtual_day(p["target_date"], p["target_timezone"], planner)
        if empty["id"] == original["id"]:
            raise _error("invalid_input", "目标日期、时区和 Agent 与原计划相同")
        placement = next(
            (row for row in original["placements"] if row["id"] == p["placement_id"]), None
        )
        if placement is None or placement["disposition"] != "include":
            raise _error("not_found", "原计划没有这个已安排条目")
        await self._authorize_item(placement["source_ref"], caller_actor, require_current=True)
        metadata = await self._metadata_for(placement["source_ref"])
        if semantic_item_ref(metadata["item_ref"]) != semantic_item_ref(placement["source_ref"]):
            raise _error("source_stale", "原安排需要重新绑定后才能移动")
        await self._authorize_metadata_sources(metadata, caller_actor)

        def update(tx):
            origin = self._tx_day(tx, original["id"], expected_revision=p["expected_revision"])
            target = self._tx_day(
                tx, empty["id"], empty=empty, expected_revision=p["expected_target_revision"]
            )
            rows = [dict(v) for v in origin["placements"]]
            chosen = next((v for v in rows if v["id"] == p["placement_id"]), None)
            if chosen is None or chosen != placement:
                raise _error("version_conflict", "原今日条目已变更")
            chosen["disposition"] = "exclude"
            destination = [dict(v) for v in target["placements"]]
            target_id = placement_identity(target["id"], chosen["metadata_id"])
            existing = next((v for v in destination if v["id"] == target_id), None)
            if existing and semantic_item_ref(existing["source_ref"]) != semantic_item_ref(
                chosen["source_ref"]
            ):
                raise _error("source_stale", "目标已有其他委托修订，请先明确重新绑定")
            destination = [v for v in destination if v["id"] != target_id]
            if len(destination) >= MAX_PLACEMENTS:
                raise _error("input_limit", "目标日计划已达 500 项上限")
            destination.append({**chosen, "id": target_id, "disposition": "include"})
            saved_origin = tx.put(
                "day", self._changed_day(origin, rows), expected_revision=origin["revision"]
            )
            saved = tx.put(
                "day", self._changed_day(target, destination), expected_revision=target["revision"]
            )
            return self._receipt(
                tx,
                "move",
                p,
                caller_actor,
                trusted_channel,
                day=saved,
                metadata=[chosen["metadata_id"]],
                sources=[chosen["source_ref"]],
                extra_identity={
                    "source_day_id": saved_origin["id"],
                    "source_day_revision": saved_origin["revision"],
                    "source_day_ref": self._day_reference(saved_origin),
                },
            )

        return await self._mutate("move", p, caller_actor, trusted_channel, update)

    async def history(self, kind, identity, limit=20, after_revision=None, caller_actor=None):
        if kind not in {"day", "metadata"}:
            raise _error("invalid_input", "请选择日计划或事项计划字段历史")
        integer(limit, "每页历史数", 1, 100)
        text(identity, "原记录编号", 35, pattern=_DAY if kind == "day" else _META)
        current = await self._record(kind, identity)
        if kind == "day":
            self._day_scope(current, caller_actor)
        else:
            self._metadata_scope(current)
            await self.items._item(current["item_ref"]["item_id"], caller_actor)
        before = (
            current["revision"] + 1
            if after_revision is None
            else integer(after_revision, "原历史游标")
        )
        if before > current["revision"] + 1:
            raise _error("invalid_input", "历史游标超出原记录范围")
        revisions = list(range(before - 1, max(0, before - limit - 1), -1))
        records = await self._read(
            lambda tx: [tx.get(kind, identity, revision=v) for v in revisions]
        )
        output = []
        for record in records:
            if kind == "day":
                view = await self._day_view(
                    self._day_scope(record, caller_actor), 25, None, caller_actor
                )
            else:
                view = await self._metadata_view(record, record["item_ref"], caller_actor)
            output.append(
                {
                    "id": identity,
                    "revision": record["revision"],
                    "digest": record_digest(record),
                    "view": view,
                }
            )
        return {
            "history": output,
            "next_after_revision": revisions[-1] if revisions and revisions[-1] > 1 else None,
        }

    async def request_status(self, request_id, caller_actor=None):
        text(request_id, "原请求编号", 128, pattern=_REQUEST)
        value = await self._read(lambda tx: None if tx is None else tx.request(request_id))
        if value is None:
            return {"status": "unknown", "request_id": request_id}
        result = await self._finish(value["result"], caller_actor)
        return {"status": "accepted", **result}
