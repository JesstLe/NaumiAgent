"""Paged calendar mirrors, exact source authority and recoverable event-to-item outboxes."""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

from naumi_agent.personal.connection_models import (
    canonical,
    digest,
    fields,
    integer,
    source_ref,
    text,
)
from naumi_agent.personal.google_calendar import normalize_event, occurrence_key
from naumi_agent.personal.google_oauth import GoogleProtocolError
from naumi_agent.personal.models import PersonalAgentError


class CalendarSyncMixin:
    def _event_key(self, connection_id, event_id):
        return connection_id + ":" + text(event_id, "日程编号", 1024)

    async def sync(self, identity, payload, *, trusted_channel=None, caller_actor=None):
        from naumi_agent.personal.connections import manual

        if trusted_channel is not None:
            manual(trusted_channel)
        actor = caller_actor or "main"
        fields(payload, {"request_id", "expected_revision"}, {"request_id", "expected_revision"})

        def request(tx):
            current = tx.get("connection", identity)
            self._grant(current, actor)
            if current["revision"] != integer(payload["expected_revision"], "连接版本"):
                raise PersonalAgentError("version_conflict", "连接状态已更新，请刷新后同步")
            tx.put(
                "connection",
                {
                    **current,
                    "sync_requested": True,
                    "sync_status": "syncing",
                    "updated_at": self.stamp(),
                },
                expected_revision=current["revision"],
            )
            return {"kind": "connection", "id": identity}

        await self._mutate(
            payload["request_id"],
            "sync",
            {"connection_id": identity, "actor_id": actor, **payload},
            request,
        )
        current = await self._record("connection", identity)
        if not current.get("next_sync_at") or self.now() >= self._parse_sync_time(
            current["next_sync_at"]
        ):
            self._kick_sync(identity)
        return self.connection_view(current)

    @staticmethod
    def _parse_sync_time(value):
        from naumi_agent.personal.connection_models import timestamp

        return timestamp(value)

    def _kick_sync(self, identity):
        tasks = getattr(self, "_sync_tasks", None)
        if tasks is None:
            self._sync_tasks = tasks = {}
        if self._closing or identity in tasks:
            return
        task = asyncio.create_task(self._run_sync(identity), name="calendar-sync:" + identity)
        tasks[identity] = task
        task.add_done_callback(lambda done, key=identity: self._background_done(done, key, "sync"))

    async def _run_sync(self, identity):
        lease = None
        original = await self._connection(identity)
        if original.get("next_sync_at") and self.now() < self._parse_sync_time(
            original["next_sync_at"]
        ):
            return
        try:
            lease = await self._write(
                lambda tx: tx.claim(
                    "sync", identity, self.holder, now=self.now().timestamp(), ttl=900
                )
            )
            credential = await self.token_snapshot(identity)
            access, original = credential.access_token, credential.connection
            query_sha = digest(
                {
                    "provider": "google_calendar",
                    "calendar": "primary",
                    "singleEvents": False,
                    "showDeleted": True,
                }
            )

            def begin(tx):
                self._fence(tx, "sync", identity, lease)
                old = tx.get("sync", identity, missing=True)
                if (
                    old
                    and old["auth_epoch"] == original["auth_epoch"]
                    and old["query_sha256"] == query_sha
                    and old["status"] == "reading"
                ):
                    return old
                token = (
                    old.get("committed_token")
                    if old
                    and old["auth_epoch"] == original["auth_epoch"]
                    and old["query_sha256"] == query_sha
                    else None
                )
                return tx.put(
                    "sync",
                    {
                        "id": identity,
                        "connection_id": identity,
                        "status": "reading",
                        "auth_epoch": original["auth_epoch"],
                        "query_sha256": query_sha,
                        "committed_token": token,
                        "generation": uuid.uuid4().hex,
                        "full": not bool(token),
                        "page_token": None,
                        "page_count": 0,
                        "event_count": 0,
                        "masters_done": [],
                        "seen_page_tokens": [],
                        "seen_instance_tokens": {},
                        "window_start": (self.now() - timedelta(days=1)).isoformat(),
                        "window_end": (self.now() + timedelta(days=30)).isoformat(),
                        "started_at": self.stamp(),
                    },
                    expected_revision=0 if old is None else old["revision"],
                )

            checkpoint = await self._write(begin)
            calls = 0
            if not checkpoint.get("next_sync_token"):
                for _ in range(32):
                    try:
                        response = await self.adapter.list_events(
                            access,
                            page_token=checkpoint["page_token"],
                            sync_token=checkpoint["committed_token"],
                        )
                        calls += 1
                    except GoogleProtocolError as exc:
                        if exc.code == "sync_token_expired" and checkpoint["committed_token"]:

                            def reset(tx):
                                self._fence(tx, "sync", identity, lease)
                                current = tx.get("sync", identity)
                                return tx.put(
                                    "sync",
                                    {
                                        **current,
                                        "generation": uuid.uuid4().hex,
                                        "full": True,
                                        "committed_token": None,
                                        "page_token": None,
                                        "page_count": 0,
                                        "event_count": 0,
                                        "masters_done": [],
                                        "seen_page_tokens": [],
                                        "seen_instance_tokens": {},
                                        "next_sync_token": None,
                                    },
                                    expected_revision=current["revision"],
                                )

                            checkpoint = await self._write(reset)
                            continue
                        raise
                    checkpoint = await self._stage_page(
                        identity, checkpoint, response, original, lease
                    )
                    if checkpoint.get("next_sync_token"):
                        break
                if not checkpoint.get("next_sync_token"):
                    return
            pages = await self._sync_pages(identity, checkpoint)
            staged = {event["id"]: event for page in pages for event in page["events"]}
            previous = await self._read(lambda tx: tx.list("event", parent=identity, limit=10001))
            if len(previous) > 10000:
                raise PersonalAgentError("sync_limit", "日历镜像超过本机一万条记录上限")
            merged = (
                {}
                if checkpoint["full"]
                else {v["event_id"]: v["event"] for v in previous if v.get("available", True)}
            )
            merged.update(staged)
            masters = [
                v for v in merged.values() if v.get("recurrence") and v["status"] != "cancelled"
            ]
            remaining_calls = max(0, 32 - calls)
            for master in masters:
                if master["id"] in checkpoint["masters_done"]:
                    continue
                page_token = (
                    checkpoint.get("instance_page_token")
                    if checkpoint.get("instance_master") == master["id"]
                    else None
                )
                while remaining_calls:
                    response = await self.adapter.instances(
                        access,
                        master["id"],
                        page_token=page_token,
                        time_min=checkpoint["window_start"],
                        time_max=checkpoint["window_end"],
                    )
                    checkpoint = await self._stage_page(
                        identity, checkpoint, response, original, lease, master=master["id"]
                    )
                    remaining_calls -= 1
                    page_token = checkpoint.get("instance_page_token")
                    if not page_token:
                        break
                if page_token or not remaining_calls:
                    return
            # Reopen all staged pages after instance expansion. No cursor is
            # committed before every page and bounded instance window is durable.
            pages = await self._sync_pages(identity, checkpoint)
            staged = {event["id"]: event for page in pages for event in page["events"]}

            def publish(tx):
                self._fence(tx, "sync", identity, lease)
                current_connection = tx.get("connection", identity)
                self._grant(current_connection, "main")
                if current_connection["auth_epoch"] != original["auth_epoch"]:
                    raise PersonalAgentError("lease_lost", "账户授权已更新，未发布旧同步结果")
                current = tx.get("sync", identity)
                if current["generation"] != checkpoint["generation"]:
                    raise PersonalAgentError("lease_lost", "同步执行权已更新")
                if checkpoint["full"]:
                    for old in tx.list("event", parent=identity, limit=10001):
                        if old["event_id"] not in staged and old.get("available", True):
                            tx.put(
                                "event",
                                {**old, "available": False},
                                expected_revision=old["revision"],
                            )
                for event in staged.values():
                    key = self._event_key(identity, event["id"])
                    old = tx.get("event", key, missing=True)
                    event_sha = digest(event)
                    if (
                        old
                        and old["payload_sha256"] == event_sha
                        and old["auth_epoch"] == original["auth_epoch"]
                        and old.get("available", True)
                    ):
                        continue
                    value = {
                        "id": key,
                        "connection_id": identity,
                        "status": event["status"],
                        "event_id": event["id"],
                        "event": event,
                        "auth_epoch": original["auth_epoch"],
                        "occurrence_key": occurrence_key(event),
                        "payload_sha256": event_sha,
                        "fetched_at": self.stamp(),
                        "available": True,
                    }
                    tx.put("event", value, expected_revision=0 if old is None else old["revision"])
                cancelled_masters = {
                    event["id"] for event in staged.values() if event["status"] == "cancelled"
                }
                cancelled_masters.update(
                    old["event_id"]
                    for old in previous
                    if old["event"].get("recurrence")
                    and old["event_id"] in staged
                    and not staged[old["event_id"]].get("recurrence")
                )
                for instance in tx.list("event", parent=identity, limit=10001):
                    if instance["event"].get(
                        "recurringEventId"
                    ) in cancelled_masters and instance.get("available", True):
                        tx.put(
                            "event",
                            {**instance, "available": False},
                            expected_revision=instance["revision"],
                        )
                tx.put(
                    "sync",
                    {
                        **current,
                        "status": "ready",
                        "committed_token": current["next_sync_token"],
                        "page_token": None,
                        "committed_at": self.stamp(),
                    },
                    expected_revision=current["revision"],
                )
                tx.put(
                    "connection",
                    {
                        **current_connection,
                        "sync_status": "ready",
                        "sync_requested": False,
                        "last_sync_at": self.stamp(),
                        "last_error_code": None,
                        "sync_retry_count": 0,
                        "next_sync_at": None,
                        "updated_at": self.stamp(),
                    },
                    expected_revision=current_connection["revision"],
                )

            await self._write(publish)
        except asyncio.CancelledError:
            raise
        except GoogleProtocolError as exc:
            if exc.code in {
                "invalid_grant",
                "authentication_required",
                "access_denied",
                "scope_denied",
            }:
                await self._credential_failure(original, "reauth_required", exc.code)
            await self._sync_failure(
                identity,
                exc.code,
                original["auth_epoch"],
                expected_token_generation=original["token_generation"],
                retryable=exc.retryable,
                delay=exc.retry_after_seconds,
            )
        except PersonalAgentError as exc:
            if exc.code != "connection_busy":
                await self._sync_failure(
                    identity,
                    exc.code,
                    original["auth_epoch"],
                    expected_token_generation=original["token_generation"],
                )
        except Exception:
            await self._sync_failure(
                identity,
                "sync_unknown",
                original["auth_epoch"],
                expected_token_generation=original["token_generation"],
            )
        finally:
            if lease is not None:
                try:
                    await self._write(
                        lambda tx: self._fence(tx, "sync", identity, lease, phase="finished")
                    )
                except PersonalAgentError:
                    pass

    async def _sync_pages(self, identity, checkpoint):
        return await self._read(
            lambda tx: [
                tx.get(
                    "sync", "page:" + identity + ":" + checkpoint["generation"] + ":" + str(index)
                )
                for index in range(checkpoint["page_count"])
            ]
        )

    async def _stage_page(self, identity, checkpoint, response, connection, lease, master=None):
        if (
            not isinstance(response, dict)
            or not isinstance(response.get("items", []), list)
            or len(response.get("items", [])) > 2500
        ):
            raise PersonalAgentError("invalid_event", "日历分页响应格式不正确")
        events = [normalize_event(value) for value in response.get("items", [])]
        if len({event["id"] for event in events}) != len(events):
            raise PersonalAgentError("invalid_event", "同一日历分页包含重复对象")
        next_page, next_sync = response.get("nextPageToken"), response.get("nextSyncToken")
        for token in (next_page, next_sync):
            if token is not None:
                text(token, "日历分页标识", 8192)
        if next_page and next_sync or master is None and not next_page and not next_sync:
            raise PersonalAgentError("invalid_event", "日历分页缺少完整同步凭据")

        def stage(tx):
            self._fence(tx, "sync", identity, lease)
            current_connection = tx.get("connection", identity)
            if (
                current_connection["auth_epoch"] != connection["auth_epoch"]
                or current_connection["status"] != "connected"
            ):
                raise PersonalAgentError("lease_lost", "账户授权已更新，未保存旧分页")
            current = tx.get("sync", identity)
            if (
                current["generation"] != checkpoint["generation"]
                or current["revision"] != checkpoint["revision"]
            ):
                raise PersonalAgentError("lease_lost", "日历分页执行权已更新")
            if current["event_count"] + len(events) > 10000:
                raise PersonalAgentError(
                    "sync_limit", "单次同步超过本机一万条记录上限，尚未提交游标"
                )
            if current["page_count"] >= 1000:
                raise PersonalAgentError("sync_limit", "同步分页超过本机上限，尚未提交游标")
            seen = (
                current["seen_page_tokens"]
                if master is None
                else current["seen_instance_tokens"].get(master, [])
            )
            if next_page and next_page in seen:
                raise PersonalAgentError("invalid_event", "日历分页标识出现循环，尚未提交游标")
            page_id = (
                "page:" + identity + ":" + current["generation"] + ":" + str(current["page_count"])
            )
            tx.put(
                "sync",
                {
                    "id": page_id,
                    "connection_id": identity,
                    "status": "page",
                    "generation": current["generation"],
                    "events": events,
                },
                expected_revision=0,
            )
            changes = {
                "page_count": current["page_count"] + 1,
                "event_count": current["event_count"] + len(events),
            }
            if master is None:
                changes.update(
                    page_token=next_page,
                    next_sync_token=next_sync,
                    seen_page_tokens=[*seen, next_page] if next_page else seen,
                )
            else:
                changes.update(instance_master=master, instance_page_token=next_page)
                changes["seen_instance_tokens"] = {
                    **current["seen_instance_tokens"],
                    master: [*seen, next_page] if next_page else seen,
                }
                if not next_page:
                    changes["masters_done"] = [*current["masters_done"], master]
            return tx.put("sync", {**current, **changes}, expected_revision=current["revision"])

        return await self._write(stage)

    async def _sync_failure(
        self,
        identity,
        code,
        expected_epoch,
        *,
        expected_token_generation=None,
        retryable=False,
        delay=None,
    ):
        def fail(tx):
            current = tx.get("connection", identity)
            if current["auth_epoch"] != expected_epoch or (
                expected_token_generation is not None
                and current["token_generation"] != expected_token_generation
            ):
                return current
            count = min(10, current.get("sync_retry_count", 0) + 1)
            spread = int(digest({"connection": identity, "attempt": count})[:8], 16) % 101 / 1000
            backoff = min(3600, 30 * 2 ** min(count - 1, 7)) * (1 + spread)
            if delay is not None:
                backoff = max(backoff, delay)
            return tx.put(
                "connection",
                {
                    **current,
                    "sync_status": "error",
                    "sync_requested": False,
                    "last_error_code": code,
                    "sync_retry_count": count,
                    "next_sync_at": (self.now() + timedelta(seconds=backoff)).isoformat()
                    if retryable
                    else None,
                    "updated_at": self.stamp(),
                },
                expected_revision=current["revision"],
            )

        await self._write(fail)

    def _reference(self, record, connection, actor):
        grant = self._grant(connection, actor)
        return {
            "connection_id": connection["id"],
            "auth_epoch": record["auth_epoch"],
            "event_id": record["event_id"],
            "revision": record["revision"],
            "payload_sha256": record["payload_sha256"],
            "occurrence_key": record["occurrence_key"],
            "grant_id": grant["id"],
            "grant_revision": grant["revision"],
        }

    async def capture_source(self, ref, actor_id, *, allow_manual_rebind=False):
        original = source_ref(ref)
        connection = await self._connection(original["connection_id"], actor_id)
        await self._credential(connection)
        current = await self._record(
            "event", self._event_key(connection["id"], original["event_id"])
        )
        historical = await self._record("event", current["id"], revision=original["revision"])
        if (
            not current.get("available", True)
            or current["status"] == "cancelled"
            or historical["auth_epoch"] != connection["auth_epoch"]
            or original["auth_epoch"] != connection["auth_epoch"]
            or any(historical[key] != original[key] for key in ("payload_sha256", "occurrence_key"))
        ):
            raise PersonalAgentError(
                "connection_source_revoked", "原日历资料的授权或来源版本已失效"
            )
        expected = self._reference(historical, connection, actor_id)
        master_id = current["event"].get("recurringEventId")
        if master_id:
            master = await self._record("event", self._event_key(connection["id"], master_id))
            if master["status"] == "cancelled" or not master.get("available", True):
                raise PersonalAgentError(
                    "connection_source_revoked", "原周期日程已取消或失去访问权限"
                )
        if not allow_manual_rebind and (expected["grant_id"], expected["grant_revision"]) != (
            original["grant_id"],
            original["grant_revision"],
        ):
            raise PersonalAgentError(
                "connection_source_revoked", "原日历分享已失效，新授权不替代旧引用"
            )
        return expected

    async def authorize_sources(self, refs, actor_id):
        if not isinstance(refs, list) or len(refs) > 64:
            raise PersonalAgentError("invalid_input", "日历来源引用超过上限")
        return [await self.capture_source(ref, actor_id) for ref in refs]

    async def events(self, identity, actor_id="main", limit=50, offset=0):
        integer(limit, "日程分页数量", maximum=100)
        integer(offset, "日程分页位置", minimum=0)
        connection = await self._connection(identity, actor_id)
        values = await self._read(lambda tx: tx.list("event", parent=identity, limit=10001))
        visible = [
            v
            for v in values
            if v.get("available", True)
            and v["auth_epoch"] == connection["auth_epoch"]
            and v["status"] != "cancelled"
            and not v["event"].get("recurrence")
        ]
        visible.sort(
            key=lambda v: (
                v["event"]
                .get("start", {})
                .get("dateTime", v["event"].get("start", {}).get("date", "")),
                v["event_id"],
            )
        )
        result = []
        for record in visible[offset : offset + limit]:
            event = record["event"]
            result.append(
                {
                    "connection_id": identity,
                    "event_id": record["event_id"],
                    "occurrence_key": record["occurrence_key"],
                    "revision": record["revision"],
                    "etag": event["etag"],
                    "status": event["status"],
                    "summary": event["summary"],
                    "description": event["description"],
                    "start": event["start"],
                    "end": event["end"],
                    "html_link": event["htmlLink"],
                    "recurring_event_id": event["recurringEventId"],
                    "original_start_time": event["originalStartTime"],
                    "fetched_at": record["fetched_at"],
                    "source_ref": self._reference(record, connection, actor_id),
                }
            )
        final = await self._connection(identity, actor_id)
        if final["auth_epoch"] != connection["auth_epoch"] or self._grant(
            final, actor_id
        ) != self._grant(connection, actor_id):
            raise PersonalAgentError(
                "connection_source_revoked", "读取期间账户或分享权限已更新，请刷新后继续"
            )
        return {
            "events": result,
            "limit": limit,
            "offset": offset,
            "next_offset": offset + limit if offset + limit < len(visible) else None,
        }

    async def _source_artifact(self, ref, actor_id):
        exact = await self.capture_source(ref, actor_id)
        event = await self._record(
            "event",
            self._event_key(exact["connection_id"], exact["event_id"]),
            revision=exact["revision"],
        )
        value = await self.engine.personal_artifacts.publish(
            actor_id=actor_id,
            request_id="calendar-source:" + digest({"ref": exact, "actor": actor_id}),
            title=event["event"]["summary"] or "日历事件",
            kind="material",
            filename="calendar-event.txt",
            content=canonical(
                {"source": exact, "event": event["event"], "fetched_at": event["fetched_at"]}
            ).encode(),
            evidence={"source_kind": "calendar_event", "connection_sources": [exact]},
        )
        await self.capture_source(exact, actor_id)
        return self.engine.personal_artifacts.reference(value)

    async def tool_events(self, identity, actor_id, limit=20):
        page = await self.events(identity, actor_id, limit=limit)
        session = getattr(self.engine, "_session", None)
        if session is None:
            raise PersonalAgentError("binding_conflict", "请在有效对话中读取日历资料")
        result = []
        for event in page["events"]:
            ref = await self._source_artifact(event["source_ref"], actor_id)
            await self.engine.personal_artifacts.bind_refs(session.id, actor_id, [ref])
            result.append(
                {
                    "event_id": event["event_id"],
                    "source_ref": event["source_ref"],
                    "artifact_ref": ref,
                }
            )
        await self.engine.personal_artifacts.authorize_engine()
        return {
            "events": result,
            "next_offset": page["next_offset"],
            "content_transport": "exact_private_artifact",
        }

    async def event_to_item(
        self, identity, event_id, payload, *, trusted_channel=None, caller_actor=None
    ):
        from naumi_agent.personal.connections import manual

        if trusted_channel is not None:
            manual(trusted_channel)
        if caller_actor is not None and "actor_id" not in payload:
            payload = {**payload, "actor_id": caller_actor}
        fields(
            payload,
            {"request_id", "event_revision", "actor_id", "title", "instructions", "schedule"},
            {"request_id", "event_revision", "actor_id", "title", "instructions", "schedule"},
        )
        actor = payload["actor_id"]
        if caller_actor is not None and caller_actor != actor:
            raise PersonalAgentError("scope_denied", "只能为当前 Agent 创建日历事项")
        semantic = {"connection_id": identity, "event_id": event_id, **payload}
        saved = await self._read(
            lambda tx: None if tx is None else tx.request(payload["request_id"])
        )
        if saved is not None:
            if saved["operation"] != "event_to_item" or saved["fingerprint"] != digest(
                {"operation": "event_to_item", "payload": semantic}
            ):
                raise PersonalAgentError("idempotency_conflict", "原日历事项请求对应不同内容")
            return await self._complete_item_binding(
                saved["result"]["id"], caller_actor=caller_actor
            )
        connection = await self._connection(identity, actor)
        record = await self._record("event", self._event_key(identity, event_id))
        if record["revision"] != integer(payload["event_revision"], "日程版本"):
            raise PersonalAgentError("version_conflict", "日程已更新，请刷新后创建事项")
        ref = self._reference(record, connection, actor)
        await self.capture_source(ref, actor)
        purpose = "prepare"
        binding_id = "binding:" + digest(
            {"connection": identity, "occurrence": record["occurrence_key"], "purpose": purpose}
        )
        frozen = {
            "title": text(payload["title"], "事项标题", 200),
            "instructions": text(payload["instructions"], "事项要求", 20000),
            "schedule": payload["schedule"],
            "assignee_id": actor,
            "assignee_version": self.actor_version(actor),
        }

        def reserve(tx):
            old = tx.get("binding", binding_id, missing=True)
            if old is None:
                tx.put(
                    "binding",
                    {
                        "id": binding_id,
                        "connection_id": identity,
                        "status": "pending",
                        "source_ref": ref,
                        "frozen": frozen,
                        "item_request_id": "calendar-item:" + binding_id[8:],
                        "item_id": None,
                        "artifact_ref": None,
                        "created_at": self.stamp(),
                    },
                    expected_revision=0,
                )
            elif old["frozen"]["assignee_id"] != actor:
                raise PersonalAgentError("binding_conflict", "原日历实例已生成事项，请查看原事项")
            return {"kind": "item", "id": binding_id}

        await self._mutate(
            payload["request_id"],
            "event_to_item",
            {"connection_id": identity, "event_id": event_id, **payload},
            reserve,
        )
        return await self._complete_item_binding(binding_id, caller_actor=caller_actor)

    async def _complete_item_binding(self, binding_id, *, caller_actor=None):
        binding = await self._record("binding", binding_id)
        if caller_actor is not None:
            await self.capture_source(binding["source_ref"], caller_actor)
        if binding["item_id"]:
            return await self.engine.personal_items.get(
                binding["item_id"], caller_actor=caller_actor
            )
        actor = binding["frozen"]["assignee_id"]
        await self.capture_source(binding["source_ref"], actor)
        artifact = binding["artifact_ref"] or await self._source_artifact(
            binding["source_ref"], actor
        )
        if binding["artifact_ref"] is None:
            binding = await self._write(
                lambda tx: self._change(tx, "binding", binding_id, {"artifact_ref": artifact})
            )
        item = await self.engine.personal_items.create(
            {
                **binding["frozen"],
                "request_id": binding["item_request_id"],
                "input_refs": [artifact],
            },
            caller_actor=caller_actor,
        )

        def commit(tx):
            current = tx.get("binding", binding_id)
            if current["item_id"] and current["item_id"] != item["item_id"]:
                raise PersonalAgentError("binding_conflict", "原日历事项绑定与回执不一致")
            if current["item_id"] is None:
                tx.put(
                    "binding",
                    {**current, "item_id": item["item_id"], "status": "bound"},
                    expected_revision=current["revision"],
                )

        await self._write(commit)
        return item
