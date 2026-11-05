"""Durable source references, merged offers and separate presentation/read facts."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

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
from naumi_agent.personal.notification_authority import NotificationAuthorityMixin
from naumi_agent.personal.notification_models import (
    default_preferences,
    identity,
    normalize_event_ref,
    normalize_preferences,
    record_digest,
)
from naumi_agent.personal.notification_schedule import quiet_state
from naumi_agent.personal.notification_store import NotificationStore
from naumi_agent.personal.owner_journal import checked_path

ACTOR = re.compile(r"^(main|pa_[a-f0-9]{32})$")
REQUEST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")
SHA = re.compile(r"^[a-f0-9]{64}$")
PAGE = 50
VIEW_LIMIT = 10000


def error(code, message):
    return PersonalAgentError(code, message)


class PersonalNotificationService(NotificationAuthorityMixin):
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
        self.boundary = (
            Path(engine.config.memory.session_db_path)
            .resolve()
            .with_name(f".personal-notifications-{self.workspace_key}-v1")
        )
        self.clock = clock or (lambda: datetime.now(UTC))
        pinned = self._external_identity()
        self.store = store or NotificationStore(
            self.personal.store.base_dir / "notifications",
            owner_key=self.owner_key,
            expected_instance_id=pinned,
        )
        if pinned:
            if self.store.expected_instance_id not in {None, pinned}:
                raise self._broken()
            self.store.expected_instance_id = pinned
        self.holder = uuid.uuid4().hex
        self._worker = None
        self._closing = False
        self._tick_lock = asyncio.Lock()
        self._last_error = None
        self._prompt_after = None

    @staticmethod
    def _broken():
        return error("storage_error", "通知与原资料库不一致，请恢复原目录后继续")

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
            if (
                self.boundary.is_symlink()
                or self.boundary.stat().st_nlink != 1
                or self.boundary.stat().st_size > 1024
            ):
                raise ValueError()
            value = strict_json_loads(self.boundary.read_bytes())
            keys(
                value,
                {"version", "owner_key", "workspace_key", "instance_id"},
                {"version", "owner_key", "workspace_key", "instance_id"},
                "原通知实例",
            )
            if (
                type(value["version"]) is not int
                or value["version"] != 1
                or value["owner_key"] != self.owner_key
                or value["workspace_key"] != self.workspace_key
            ):
                raise ValueError()
            return text(value["instance_id"], "原实例", 32, pattern=re.compile("^[a-f0-9]{32}$"))
        except (OSError, ValueError, TypeError, PersonalAgentError):
            raise self._broken() from None

    def _guard(self):
        try:
            checked_path(self.boundary, self.store.spec)
            pinned = self._external_identity()
            if pinned:
                if not self.personal.store.db_path.is_file():
                    raise self._broken()
                self.store.expected_instance_id = pinned
            instance = self.store.identity()
            if pinned and instance != pinned:
                raise self._broken()
            if instance is not None:
                self.boundary.parent.mkdir(parents=True, exist_ok=True)
                if not self.boundary.exists():
                    value = {
                        "version": 1,
                        "owner_key": self.owner_key,
                        "workspace_key": self.workspace_key,
                        "instance_id": instance,
                    }
                    try:
                        with self.boundary.open("xb") as stream:
                            stream.write((canonical(value) + "\n").encode())
                            stream.flush()
                            os.fsync(stream.fileno())
                    except FileExistsError:
                        pass
                if self._external_identity() != instance:
                    raise self._broken()
                self.store.expected_instance_id = instance
        except (OSError, ValueError, TypeError):
            raise self._broken() from None

    async def _read(self, callback):
        await asyncio.to_thread(self._guard)
        result = await asyncio.to_thread(self.store.read, callback)
        await asyncio.to_thread(self._guard)
        return result

    async def _write(self, callback):
        self.personal.store.list()
        await asyncio.to_thread(self._guard)
        result = await asyncio.to_thread(self.store.write, callback)
        await asyncio.to_thread(self._guard)
        return result

    async def _record(self, kind, key, *, missing=False, revision=None):
        def read(tx):
            if tx is None:
                if missing:
                    return None
                raise error("not_found", "原通知记录不存在")
            value = tx.get(kind, key, missing=missing, revision=revision)
            if value is not None and value["workspace_key"] != self.workspace_key:
                raise error("not_found", "通知不在当前工作区")
            return value

        return await self._read(read)

    def _actor(self, caller):
        if caller is not None:
            text(caller, "当前 Agent", 35, pattern=ACTOR)
            if caller != "main":
                self.personal.require_active_profile(caller)
        return caller

    def _manual(self, caller, channel):
        if caller is not None or channel not in {"web", "cli", "tui"}:
            raise error("scope_denied", "请由用户在通知界面确认此操作")

    async def _prefs(self):
        default = default_preferences(self.workspace_key)
        return await self._record("preferences", default["id"], missing=True) or default

    async def _scan(self):
        return await self._record("scan", "scan", missing=True) or {
            "id": "scan",
            "workspace_key": self.workspace_key,
            "revision": 0,
            "intent_after": None,
            "watch_after": None,
            "action_after": None,
            "followup_after": None,
            "outbox_after": None,
            "max_wall": None,
            "last_tick_at": None,
            "partial": False,
            "last_error_code": None,
        }

    async def _quiet(self, preferences=None):
        prefs = preferences or await self._prefs()
        state = quiet_state(prefs, self.now())
        scan = await self._scan()
        rollback = bool(scan["max_wall"] and self.now() < datetime.fromisoformat(scan["max_wall"]))
        return {
            **state,
            "clock_rollback": rollback,
            "hold_reason": "clock_rollback"
            if rollback
            else "timezone_data_changed"
            if state["timezone_data_changed"]
            else None,
        }

    async def preferences(self, caller_actor=None):
        self._actor(caller_actor)
        value = await self._prefs()
        return {
            "preferences": value,
            "revision": value["revision"],
            "digest": record_digest(value),
            "quiet": await self._quiet(value),
        }

    async def context(self, caller_actor=None):
        self._actor(caller_actor)
        summary = await self._summary(caller_actor)
        actors = [{"id": "main", "name": "主 Agent", "version": 0}]
        if caller_actor is None:
            actors += [
                {"id": p.id, "name": p.name, "version": p.version}
                for p in self.personal.list()
                if p.status == "active"
            ]
        elif caller_actor != "main":
            p = self.personal.get(caller_actor)
            actors = [{"id": p.id, "name": p.name, "version": p.version}]
        prefs, scan = await self.preferences(caller_actor), await self._scan()
        return {
            "workspace_key": self.workspace_key,
            "owner_key": self.owner_key,
            "configured": prefs["revision"] > 0,
            "preferences": prefs,
            "actors": actors,
            "unread_count": summary["unread_count"],
            "attention_count": summary["attention_count"],
            "counts_consistency": "journal_snapshot",
            "partial": summary["partial"] or scan["partial"],
            "worker": {
                "last_error_code": self._last_error or scan["last_error_code"],
                "last_tick_at": scan["last_tick_at"],
            },
            "quiet": prefs["quiet"],
        }

    @staticmethod
    def _counts(rows):
        return {
            "unread_count": sum(r["unread"] for r in rows),
            "attention_count": sum(
                r["unread"] and r["priority"] == "high" and r["availability"] == "available"
                for r in rows
            ),
        }

    async def _summary(self, caller):
        # Unread includes opaque, unavailable notifications by design. Its badge
        # needs this journal's authenticated facts, not private source bodies.
        # Attention is different: only currently available high-priority sources
        # may contribute. Never treat these counters as whole-source health.
        def snapshot(tx):
            if tx is None:
                return {"unread_count": 0, "high": [], "partial": False}
            page = tx.page("event", parent=self.workspace_key, limit=VIEW_LIMIT)
            unread, high = 0, []
            for event in page["records"]:
                if event["workspace_key"] != self.workspace_key:
                    raise self._broken()
                if caller is not None and event["actor_id"] != caller:
                    continue
                read = tx.get(
                    "notice_read",
                    identity("nr_", {"event_id": event["id"]}),
                    missing=True,
                )
                if read is not None:
                    if read["workspace_key"] != self.workspace_key or read["event_ref"] != {
                        "event_id": event["id"],
                        "event_digest": record_digest(event),
                    }:
                        raise self._broken()
                    continue
                unread += 1
                if event["kind"] in {"item_failure", "calendar_approval"}:
                    high.append(event)
            return {"unread_count": unread, "high": high, "partial": bool(page["next_after"])}

        value = await self._read(snapshot)
        # Preserve the final mutable-authority check for actionable counters.
        for event in value["high"]:
            await self._entry(event, caller)
        rows = [await self._entry(event, caller) for event in value["high"]]
        return {
            "unread_count": value["unread_count"],
            "attention_count": self._counts(rows)["attention_count"],
            "partial": value["partial"],
        }

    async def _page(self, kind, after=None, limit=PAGE, statuses=None):
        return await self._read(
            lambda tx: (
                {"records": [], "next_after": None}
                if tx is None
                else tx.page(
                    kind, parent=self.workspace_key, after=after, limit=limit, statuses=statuses
                )
            )
        )

    def _entry_metadata(self, tx, event, *, batches=None):
        """Read detached local facts within one authenticated, non-awaiting snapshot."""
        if event["workspace_key"] != self.workspace_key:
            raise self._broken()
        ref = {"event_id": event["id"], "event_digest": record_digest(event)}
        read = tx.get("notice_read", identity("nr_", {"event_id": event["id"]}), missing=True)
        if read is not None and (
            read["workspace_key"] != self.workspace_key or read["event_ref"] != ref
        ):
            raise self._broken()
        outbox = tx.get("outbox", identity("no_", {"event_id": event["id"]}), missing=True)
        if outbox is not None:
            if (
                outbox["workspace_key"] != self.workspace_key
                or outbox["event_id"] != event["id"]
                or outbox["actor_id"] != event["actor_id"]
                or outbox["kind"] != event["kind"]
                or outbox["status"] not in {"pending", "sealed"}
                or (outbox["status"] == "pending") != (outbox["batch_id"] is None)
            ):
                raise self._broken()
            if outbox["batch_id"] is not None:
                batches = batches if batches is not None else {}
                batch_id = outbox["batch_id"]
                if batch_id not in batches:
                    batches[batch_id] = tx.get("batch", batch_id)
                batch = batches[batch_id]
                if (
                    batch["workspace_key"] != self.workspace_key
                    or batch["actor_id"] != event["actor_id"]
                    or batch["kind"] != event["kind"]
                    or ref not in batch["member_refs"]
                ):
                    raise self._broken()
        return {"event": event, "notice_read": read, "outbox": outbox}

    async def _refresh_entry_metadata(self, events):
        def snapshot(tx):
            if tx is None:
                if events:
                    raise self._broken()
                return []
            result, batches = [], {}
            for original in events:
                current = tx.get("event", original["id"])
                if record_digest(current) != record_digest(original):
                    raise self._broken()
                result.append(self._entry_metadata(tx, current, batches=batches))
            return result

        return await self._read(snapshot)

    async def _entry(self, event, caller=None):
        if event["workspace_key"] != self.workspace_key:
            raise self._broken()
        if caller is not None and event["actor_id"] != caller:
            raise error("not_found", "通知不在当前 Agent 范围")
        metadata = (await self._refresh_entry_metadata([event]))[0]
        return await self._project_entry(event, caller, metadata)

    async def _project_entry(self, event, caller, metadata):
        if event["workspace_key"] != self.workspace_key:
            raise self._broken()
        if caller is not None and event["actor_id"] != caller:
            raise error("not_found", "通知不在当前 Agent 范围")
        if record_digest(metadata["event"]) != record_digest(event):
            raise self._broken()
        read, outbox = metadata["notice_read"], metadata["outbox"]
        view = {
            "id": event["id"],
            "event_digest": record_digest(event),
            "kind": event["kind"],
            "actor_id": event["actor_id"],
            "actor_name": None,
            "title": None,
            "occurred_at": event["occurred_at"],
            "availability": "source_unavailable",
            "priority": (
                "high" if event["kind"] in {"item_failure", "calendar_approval"} else "normal"
            ),
            "unread": read is None,
            "notice_read": read,
            "source_ref": {"source_kind": event["kind"], "source_id": digest(event["source_ref"])},
            "jump": None,
            "batch_id": outbox.get("batch_id") if outbox else None,
        }
        try:
            source = await self._authorize_event(event, caller)
            view.update(source)
        except PersonalAgentError as exc:
            # The notification journal remains readable even if an original source
            # store is missing or fails its own authentication. No old body survives.
            if exc.code == "notification_busy":
                raise
        return view

    async def _entries(self, caller):
        page = await self._page("event", limit=VIEW_LIMIT)
        rows = []
        for event in page["records"]:
            if caller is None or event["actor_id"] == caller:
                rows.append(await self._entry(event, caller))
        # Recheck after all awaits; never return a title from an earlier authorization epoch.
        for row in rows:
            if row["availability"] != "source_unavailable":
                event = await self._record("event", row["id"])
                latest = await self._entry(event, caller)
                row.clear()
                row.update(latest)
        return rows, page["next_after"] is not None

    async def list(self, filter="unread", actor_id=None, limit=50, after=None, caller_actor=None):
        self._actor(caller_actor)
        if filter not in {"unread", "attention", "all"}:
            raise error("invalid_input", "请选择未读、待处理或全部通知")
        if actor_id is not None:
            text(actor_id, "Agent", 35, pattern=ACTOR)
            if caller_actor is not None:
                raise error("scope_denied", "模型通知只能读取当前 Agent")
        integer(limit, "每页通知数", 1, 100)
        if after is not None:
            text(after, "续读位置", 35, pattern=re.compile(r"^ne_[a-f0-9]{32}$"))
        # Counts contain opaque local identities; only actionable high-priority
        # counters need source bodies. Never project every normal private result
        # just to count it. Summary is bounded; raw continuation is not capped.
        summary = await self._summary(caller_actor)

        def snapshot(tx):
            if tx is None:
                return {"metadata": [], "next_after": None}
            page = tx.page("event", parent=self.workspace_key, after=after, limit=limit)
            values, batches = [], {}
            for event in page["records"]:
                if event["workspace_key"] != self.workspace_key:
                    raise self._broken()
                if (
                    caller_actor is not None
                    and event["actor_id"] != caller_actor
                    or actor_id is not None
                    and event["actor_id"] != actor_id
                ):
                    continue
                metadata = self._entry_metadata(tx, event, batches=batches)
                if filter in {"unread", "attention"} and metadata["notice_read"] is not None:
                    continue
                if (filter == "attention"
                        and event["kind"] not in {"item_failure", "calendar_approval"}):
                    continue
                values.append(metadata)
            return {"metadata": values, "next_after": page["next_after"]}

        page = await self._read(snapshot)
        scan = await self._scan()
        events = [value["event"] for value in page["metadata"]]
        for event, metadata in zip(events, page["metadata"], strict=True):
            await self._project_entry(event, caller_actor, metadata)
        # Refresh local metadata before the final source pass, not after it.
        # No SQLite lock spans an await and no post-authorization DB await can
        # make a previously authorized title bypass the final source boundary.
        final = await self._refresh_entry_metadata(events)
        rows = []
        for event, metadata in zip(events, final, strict=True):
            row = await self._project_entry(event, caller_actor, metadata)
            if filter == "unread" and not row["unread"]:
                continue
            if filter == "attention" and not (
                row["unread"] and row["priority"] == "high" and row["availability"] == "available"
            ):
                continue
            rows.append(row)
        return {
            "entries": rows,
            "next_after": page["next_after"],
            "partial": summary["partial"] or scan["partial"],
            "consistency": "live_keyset",
            "counts_consistency": "journal_snapshot",
            "unread_count": summary["unread_count"],
            "attention_count": summary["attention_count"],
        }

    async def get_batch(self, batch_id, caller_actor=None):
        self._actor(caller_actor)
        text(batch_id, "通知批次", 35, pattern=re.compile(r"^nb_[a-f0-9]{32}$"))
        batch = await self._record("batch", batch_id)
        if caller_actor is not None and caller_actor != batch["actor_id"]:
            raise error("not_found", "通知批次不在当前 Agent 范围")
        entries = [
            await self._entry(await self._event(ref), caller_actor) for ref in batch["member_refs"]
        ]
        offer = await self._record("outbox", identity("no_", {"batch_id": batch_id}), missing=True)
        quiet, prefs = await self._quiet(), await self._prefs()
        allowed = (
            prefs["enabled"]
            and not quiet["hold_reason"]
            and batch["actor_id"] not in prefs["muted_actor_ids"]
            and (not quiet["quiet"] or batch["kind"] in prefs["bypass_kinds"])
            and all(r["unread"] and r["availability"] == "available" for r in entries)
        )
        return {
            **batch,
            "digest": record_digest(batch),
            "entries": entries,
            "presentation_status": offer["status"] if offer else "ready",
            "quiet": quiet,
            "prompt_available": bool(allowed and (offer is None or offer["status"] == "ready")),
        }

    async def prompt(self, caller_actor=None):
        if caller_actor is not None:
            raise error("scope_denied", "主动提示由用户通知界面领取")
        # A read-only round-robin cursor is local to this runtime, not a delivery,
        # presentation or human-read fact. Claimed batches leave this ready index.
        page = await self._page("outbox", self._prompt_after, limit=100, statuses={"ready"})
        self._prompt_after = page["next_after"]
        for offer in page["records"]:
            view = await self.get_batch(offer["batch_id"])
            if view["prompt_available"]:
                return {"batch": view, "quiet": view["quiet"], "partial": bool(page["next_after"])}
        return {"batch": None, "quiet": await self._quiet(), "partial": bool(page["next_after"])}

    async def _event(self, ref):
        ref = normalize_event_ref(ref)
        value = await self._record("event", ref["event_id"])
        if record_digest(value) != ref["event_digest"]:
            raise error("version_conflict", "通知内容与原批次不一致")
        return value

    @staticmethod
    def _payload(payload, fields):
        value = normalized_object(payload, "通知请求", maximum=256 * 1024)
        keys(value, fields | {"request_id"}, fields | {"request_id"}, "通知请求")
        text(value["request_id"], "原请求编号", 128, pattern=REQUEST)
        return value

    @staticmethod
    def _refs(value):
        if not isinstance(value, list) or not 1 <= len(value) <= 100:
            raise error("invalid_input", "请选择 1 至 100 条确切通知")
        result = [normalize_event_ref(r) for r in value]
        if len({r["event_id"] for r in result}) != len(result):
            raise error("invalid_input", "通知不能重复")
        return sorted(result, key=lambda r: r["event_id"])

    def _envelope(self, payload, channel):
        return {"workspace_key": self.workspace_key, "channel": channel, "payload": payload}

    async def _replay(self, operation, payload, channel):
        row = await self._read(lambda tx: None if tx is None else tx.request(payload["request_id"]))
        if row is None:
            return None
        if row["operation"] != operation or row["fingerprint"] != digest(
            {"operation": operation, "payload": self._envelope(payload, channel)}
        ):
            raise error("idempotency_conflict", "原请求编号已绑定不同内容，请核对原请求")
        return await self._finish(row["result"])

    def _receipt(self, operation, payload, identity_value):
        return {
            "accepted": True,
            "request_id": payload["request_id"],
            "operation": operation,
            "identity": identity_value,
            "projection_available": False,
        }

    async def _mutate(self, operation, payload, channel, callback):
        self.personal.store.list()
        await asyncio.to_thread(self._guard)
        result = await asyncio.to_thread(
            self.store.mutate,
            payload["request_id"],
            operation,
            self._envelope(payload, channel),
            callback,
        )
        try:
            await asyncio.to_thread(self._guard)
            return await self._finish(result)
        except PersonalAgentError:
            return {**result, "projection_available": False}

    async def _finish(self, receipt):
        result = {**receipt, "projection_available": False}
        try:
            if receipt["operation"] == "set_preferences":
                result["preferences"] = await self.preferences()
            elif receipt["identity"].get("batch_id"):
                result["batch"] = await self.get_batch(receipt["identity"]["batch_id"])
                if any(
                    e["availability"] == "source_unavailable" for e in result["batch"]["entries"]
                ):
                    result.pop("batch")
                    return result
            result["projection_available"] = True
        except PersonalAgentError:
            result.pop("batch", None)
            result.pop("preferences", None)
        return result

    async def request_status(self, request_id, caller_actor=None):
        self._actor(caller_actor)
        text(request_id, "原请求编号", 128, pattern=REQUEST)
        row = await self._read(lambda tx: None if tx is None else tx.request(request_id))
        if row is None or caller_actor is not None:
            return {"status": "unknown", "request_id": request_id}
        receipt = row["result"]
        if not await self._receipt_workspace(receipt):
            return {"status": "unknown", "request_id": request_id}
        return {
            "status": "accepted",
            "request_id": request_id,
            "operation": receipt["operation"],
            "identity": receipt["identity"],
            "projection_available": (await self._finish(receipt))["projection_available"],
        }

    async def _receipt_workspace(self, receipt):
        # Requests share an owner journal, but lookup must not expose another
        # workspace's opaque identities. Keep old receipts compatible: derive the
        # scope from their immutable local identities, not a new optional field.
        operation, ident = receipt["operation"], receipt["identity"]
        if operation == "set_preferences":
            if ident["preferences_id"] != default_preferences(self.workspace_key)["id"]:
                return False
            await self._record("preferences", ident["preferences_id"])
            return True
        if operation in {"claim", "present"}:
            refs = [("batch", ident["batch_id"], None)]
        elif operation == "mark_read":
            refs = [
                ("event", ref["event_id"], ref["event_digest"])
                for ref in self._refs(ident["event_refs"])
            ]
        else:
            raise self._broken()

        def check(tx):
            if tx is None:
                raise self._broken()
            for kind, key, checksum in refs:
                record = tx.get(kind, key)
                if record["workspace_key"] != self.workspace_key:
                    return False
                if checksum is not None and record_digest(record) != checksum:
                    raise self._broken()
            return True

        return await self._read(check)

    async def set_preferences(self, payload, caller_actor=None, trusted_channel=None):
        self._manual(caller_actor, trusted_channel)
        pref_fields = {
            "enabled",
            "timezone",
            "quiet_hours",
            "muted_actor_ids",
            "debounce_seconds",
            "max_delay_seconds",
            "bypass_kinds",
            "include_existing",
        }
        p = self._payload(payload, pref_fields | {"expected_revision"})
        integer(p["expected_revision"], "预期设置版本", 0)
        normalized = normalize_preferences({k: p[k] for k in pref_fields})
        replay = await self._replay("set_preferences", p, trusted_channel)
        if replay:
            return replay
        stamp = self.stamp()

        def update(tx):
            base = default_preferences(self.workspace_key)
            old = tx.get("preferences", base["id"], missing=True) or base
            if old["revision"] != p["expected_revision"]:
                raise error("version_conflict", "通知设置已更新，请刷新后核对")
            value = {
                **base,
                **normalized,
                "created_at": old["created_at"] or stamp,
                "updated_at": stamp,
                "cutover_at": old["cutover_at"]
                if old["revision"]
                else None
                if normalized["include_existing"]
                else stamp,
            }
            saved = tx.put("preferences", value, expected_revision=p["expected_revision"])
            return self._receipt(
                "set_preferences",
                p,
                {"preferences_id": saved["id"], "preferences_revision": saved["revision"]},
            )

        return await self._mutate("set_preferences", p, trusted_channel, update)

    async def claim(self, payload, caller_actor=None, trusted_channel=None):
        self._manual(caller_actor, trusted_channel)
        p = self._payload(payload, {"batch_id", "expected_digest", "client_id"})
        text(p["client_id"], "通知客户端", 35, pattern=re.compile(r"^nc_[a-f0-9]{32}$"))
        text(p["expected_digest"], "通知摘要", 64, pattern=SHA)
        replay = await self._replay("claim", p, trusted_channel)
        if replay:
            return replay
        view = await self.get_batch(p["batch_id"])
        prefs = await self._prefs()
        if view["digest"] != p["expected_digest"]:
            raise error("version_conflict", "通知批次已改变，请刷新")
        if not view["prompt_available"]:
            raise error("notification_unavailable", "该通知已领取、已处理或正在勿扰时段")
        for ref in view["member_refs"]:
            await self._authorize_event(await self._event(ref), None)
        claim_id = identity("no_", {"batch_id": view["id"]})

        def update(tx):
            current = tx.get("preferences", prefs["id"])
            if record_digest(current) != record_digest(prefs):
                raise error("version_conflict", "通知设置已改变，请重新核对")
            batch = tx.get("batch", view["id"])
            if record_digest(batch) != view["digest"]:
                raise self._broken()
            offer = tx.get("outbox", claim_id, missing=True)
            if offer and offer["status"] != "ready":
                raise error("notification_claimed", "该通知已由另一个窗口领取")
            quiet = quiet_state(current, self.now())
            scan = tx.get("scan", "scan", missing=True)
            rollback = bool(
                scan and scan["max_wall"] and self.now() < datetime.fromisoformat(scan["max_wall"])
            )
            if (
                not current["enabled"]
                or rollback
                or quiet["timezone_data_changed"]
                or batch["actor_id"] in current["muted_actor_ids"]
                or quiet["quiet"]
                and batch["kind"] not in current["bypass_kinds"]
            ):
                raise error("notification_unavailable", "当前处于勿扰或暂停提示状态")
            for ref in batch["member_refs"]:
                if tx.get(
                    "notice_read", identity("nr_", {"event_id": ref["event_id"]}), missing=True
                ):
                    raise error("version_conflict", "批次成员已处理，请从通知列表查看")
            ident = {
                "claim_id": claim_id,
                "batch_id": batch["id"],
                "batch_digest": view["digest"],
                "client_id": p["client_id"],
                "event_refs": batch["member_refs"],
            }
            tx.put(
                "outbox",
                {
                    "id": claim_id,
                    "workspace_key": self.workspace_key,
                    "batch_id": batch["id"],
                    "status": "claimed",
                    "identity": ident,
                    "claimed_at": self.stamp(),
                },
                expected_revision=offer["revision"] if offer else 0,
            )
            return self._receipt("claim", p, ident)

        return await self._mutate("claim", p, trusted_channel, update)

    async def present(self, payload, caller_actor=None, trusted_channel=None):
        self._manual(caller_actor, trusted_channel)
        p = self._payload(
            payload, {"batch_id", "batch_digest", "client_id", "claim_id", "event_refs"}
        )
        p["event_refs"] = self._refs(p["event_refs"])
        replay = await self._replay("present", p, trusted_channel)
        if replay:
            return replay
        view = await self.get_batch(p["batch_id"])
        expected = {
            "claim_id": identity("no_", {"batch_id": view["id"]}),
            "batch_id": view["id"],
            "batch_digest": view["digest"],
            "client_id": p["client_id"],
            "event_refs": view["member_refs"],
        }
        if {k: p[k] for k in expected} != expected:
            raise error("version_conflict", "呈现确认与原领取批次不一致")
        for ref in view["member_refs"]:
            await self._authorize_event(await self._event(ref), None)

        def update(tx):
            offer = tx.get("outbox", expected["claim_id"])
            if offer["identity"] != expected or offer["status"] != "claimed":
                raise error("notification_claimed", "原领取状态已改变，请核对原请求")
            key = identity("ns_", expected)
            tx.put(
                "presentation",
                {
                    "id": key,
                    "workspace_key": self.workspace_key,
                    **expected,
                    "channel": trusted_channel,
                    "presented_at": self.stamp(),
                },
                expected_revision=0,
            )
            tx.put(
                "outbox",
                {**offer, "status": "presented", "presentation_id": key},
                expected_revision=offer["revision"],
            )
            return self._receipt("present", p, {**expected, "presentation_id": key})

        return await self._mutate("present", p, trusted_channel, update)

    async def mark_read(self, payload, caller_actor=None, trusted_channel=None):
        self._manual(caller_actor, trusted_channel)
        p = self._payload(payload, {"event_refs"})
        p["event_refs"] = self._refs(p["event_refs"])
        replay = await self._replay("mark_read", p, trusted_channel)
        if replay:
            return replay
        entries = [await self._entry(await self._event(ref)) for ref in p["event_refs"]]

        def update(tx):
            ids = []
            for ref, entry in zip(p["event_refs"], entries, strict=True):
                event = tx.get("event", ref["event_id"])
                if record_digest(event) != ref["event_digest"]:
                    raise error("version_conflict", "通知内容已改变")
                key = identity("nr_", {"event_id": ref["event_id"]})
                if not tx.get("notice_read", key, missing=True):
                    tx.put(
                        "notice_read",
                        {
                            "id": key,
                            "workspace_key": self.workspace_key,
                            "event_ref": ref,
                            "channel": trusted_channel,
                            "read_at": self.stamp(),
                            "availability": entry["availability"],
                        },
                        expected_revision=0,
                    )
                ids.append(key)
            return self._receipt(
                "mark_read", p, {"event_refs": p["event_refs"], "notice_read_ids": ids}
            )

        return await self._mutate("mark_read", p, trusted_channel, update)

    def start(self):
        if self._worker is None or self._worker.done():
            self._closing = False
            self._worker = asyncio.create_task(self._loop(), name="personal-notifications")

    async def stop(self):
        self._closing = True
        if self._worker:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
            self._worker = None

    async def _loop(self):
        while not self._closing:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                code = getattr(exc, "code", "notification_error")
                self._last_error = (
                    code if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code) else "notification_error"
                )
            await asyncio.sleep(5)

    async def tick(self):
        async with self._tick_lock:
            prefs = await self._prefs()
            if not prefs["revision"]:
                return
            scan = await self._scan()
            stamp = self.stamp()
            wall = max(stamp, scan["max_wall"] or stamp)
            lease = await self._write(
                lambda tx: tx.claim(
                    "worker",
                    self.workspace_key,
                    self.holder,
                    now=datetime.fromisoformat(wall).timestamp(),
                    ttl=90,
                )
            )
            try:
                # A different runtime may have advanced the durable cursor/high-water
                # after our initial read but before this new fence was acquired.
                scan, prefs = await self._scan(), await self._prefs()
                stamp = self.stamp()
                wall = max(stamp, scan["max_wall"] or stamp)
                candidates, watches, progress = await self._scan_sources(scan, prefs)

                def save(tx):
                    tx.require_lease(
                        "worker",
                        self.workspace_key,
                        self.holder,
                        lease["fence"],
                        now=max(self.now().timestamp(), datetime.fromisoformat(wall).timestamp()),
                    )
                    current = tx.get("preferences", prefs["id"])
                    if record_digest(current) != record_digest(prefs):
                        raise error("version_conflict", "扫描期间通知设置已改变")
                    for event in candidates:
                        if prefs["cutover_at"] and datetime.fromisoformat(
                            event["occurred_at"]
                        ) < datetime.fromisoformat(prefs["cutover_at"]):
                            continue
                        old = tx.get("event", event["id"], missing=True)
                        if old is None:
                            tx.put("event", event, expected_revision=0)
                            tx.put(
                                "outbox",
                                {
                                    "id": identity("no_", {"event_id": event["id"]}),
                                    "workspace_key": self.workspace_key,
                                    "event_id": event["id"],
                                    "actor_id": event["actor_id"],
                                    "kind": event["kind"],
                                    "status": "pending",
                                    "first_seen_at": stamp,
                                    "batch_id": None,
                                },
                                expected_revision=0,
                            )
                        elif {k: v for k, v in old.items() if k != "revision"} != event:
                            raise self._broken()
                    for watch in watches:
                        old = tx.get("watch", watch["id"], missing=True)
                        if (
                            old is None
                            or {k: v for k, v in old.items() if k != "revision"} != watch
                        ):
                            tx.put("watch", watch, expected_revision=old["revision"] if old else 0)
                    old = tx.get("scan", "scan", missing=True)
                    if (old["revision"] if old else 0) != scan["revision"]:
                        raise error("version_conflict", "扫描进度已更新，请核对下一轮")
                    saved = {
                        **scan,
                        **progress,
                        "max_wall": wall,
                        "last_tick_at": stamp,
                        "last_error_code": None,
                    }
                    tx.put("scan", saved, expected_revision=old["revision"] if old else 0)

                await self._write(save)
                await self._seal(prefs, lease, wall)
                self._last_error = None
            finally:
                try:
                    await self._write(
                        lambda tx: tx.require_lease(
                            "worker",
                            self.workspace_key,
                            self.holder,
                            lease["fence"],
                            now=max(
                                self.now().timestamp(), datetime.fromisoformat(wall).timestamp()
                            ),
                            phase="finished",
                        )
                    )
                except PersonalAgentError as exc:
                    if exc.code != "lease_lost":
                        raise

    async def _seal(self, prefs, lease, wall):
        scan = await self._scan()
        page = await self._page("outbox", scan["outbox_after"], limit=500, statuses={"pending"})
        groups = {}
        for outbox in page["records"]:
            event = await self._record("event", outbox["event_id"])
            groups.setdefault((event["actor_id"], event["kind"]), []).append((event, outbox))
        ready = []
        for (actor, kind), rows in groups.items():
            rows.sort(key=lambda p: (p[1]["first_seen_at"], p[0]["id"]))
            for offset in range(0, len(rows), 100):
                chunk = rows[offset : offset + 100]
                age = (
                    self.now() - datetime.fromisoformat(chunk[0][1]["first_seen_at"])
                ).total_seconds()
                quiet_age = (
                    self.now() - datetime.fromisoformat(chunk[-1][1]["first_seen_at"])
                ).total_seconds()
                if (
                    kind in {"item_failure", "calendar_approval"}
                    or len(chunk) == 100
                    or age >= prefs["max_delay_seconds"]
                    or quiet_age >= prefs["debounce_seconds"]
                ):
                    refs = sorted(
                        [{"event_id": e["id"], "event_digest": record_digest(e)} for e, _ in chunk],
                        key=lambda r: r["event_id"],
                    )
                    batch = {
                        "id": identity(
                            "nb_", {"workspace_key": self.workspace_key, "members": refs}
                        ),
                        "workspace_key": self.workspace_key,
                        "actor_id": actor,
                        "kind": kind,
                        "member_refs": refs,
                        "created_at": self.stamp(),
                    }
                    ready.append((batch, chunk))

        def seal(tx):
            tx.require_lease(
                "worker",
                self.workspace_key,
                self.holder,
                lease["fence"],
                now=max(self.now().timestamp(), datetime.fromisoformat(wall).timestamp()),
            )
            if record_digest(tx.get("preferences", prefs["id"])) != record_digest(prefs):
                raise error("version_conflict", "合并期间通知设置已改变")
            for batch, chunk in ready:
                if not tx.get("batch", batch["id"], missing=True):
                    tx.put("batch", batch, expected_revision=0)
                    tx.put(
                        "outbox",
                        {
                            "id": identity("no_", {"batch_id": batch["id"]}),
                            "workspace_key": self.workspace_key,
                            "batch_id": batch["id"],
                            "status": "ready",
                        },
                        expected_revision=0,
                    )
                for _, box in chunk:
                    current = tx.get("outbox", box["id"])
                    tx.put(
                        "outbox",
                        {**current, "status": "sealed", "batch_id": batch["id"]},
                        expected_revision=box["revision"],
                    )
            current = tx.get("scan", "scan")
            tx.put(
                "scan",
                {**current, "outbox_after": page["next_after"]},
                expected_revision=current["revision"],
            )

        await self._write(seal)
