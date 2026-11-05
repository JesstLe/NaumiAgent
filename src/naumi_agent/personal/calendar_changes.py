"""Enrolled, transactional calendar-change facts with original mirror authority."""

from __future__ import annotations

import copy
import re

from naumi_agent.personal.connection_models import digest, fields, integer, text, timestamp
from naumi_agent.personal.google_calendar import normalize_event, occurrence_key
from naumi_agent.personal.google_oauth import GoogleProtocolError
from naumi_agent.personal.models import PersonalAgentError

_CONNECTION = re.compile(r"^pc_[a-f0-9]{32}$")
_INSTANCE = re.compile(r"^[a-f0-9]{32}$")
_SHA = re.compile(r"^[a-f0-9]{64}$")
_CHANGE = re.compile(r"^cch_[a-f0-9]{32}_[0-9]{20}$")
_REF_FIELDS = frozenset(
    {
        "connection_id",
        "connection_instance",
        "auth_epoch",
        "change_id",
        "sequence",
        "change_digest",
        "grant_id",
        "grant_revision",
    }
)
_EVENT_REF_FIELDS = frozenset(
    {
        "event_id",
        "event_revision",
        "payload_sha256",
        "occurrence_key",
        "available",
        "status",
        "auth_epoch",
    }
)
_CHANGE_FIELDS = frozenset(
    {
        "id",
        "connection_id",
        "sequence",
        "change_kind",
        "reason",
        "sync_generation",
        "before_ref",
        "after_ref",
        "changed_fields",
        "observed_at",
        "revision",
    }
)
_KINDS = frozenset({"baseline", "created", "updated", "cancelled", "unavailable", "restored"})
_REASONS = frozenset(
    {
        "enrollment_snapshot",
        "provider_event",
        "missing_in_full_snapshot",
        "master_cancelled",
        "series_replaced",
    }
)
_MIRROR_FIELDS = frozenset(
    {
        "id",
        "connection_id",
        "status",
        "event_id",
        "event",
        "auth_epoch",
        "occurrence_key",
        "payload_sha256",
        "fetched_at",
        "available",
        "revision",
    }
)


def _unavailable():
    return PersonalAgentError("connection_source_revoked", "原日历变更或授权已失效，请重新核对来源")


def _broken():
    return PersonalAgentError("storage_error", "原日历变更凭据校验未通过，请恢复原资料库")


def _change_id(connection_id, sequence):
    return "cch_" + connection_id[3:] + "_" + f"{sequence:020d}"


def normalize_change_ref(value):
    fields(value, _REF_FIELDS, _REF_FIELDS)
    result = dict(value)
    text(result["connection_id"], "原日历连接", 35, pattern=_CONNECTION)
    text(result["connection_instance"], "原连接实例", 32, pattern=_INSTANCE)
    integer(result["auth_epoch"], "原账户授权版本")
    integer(result["sequence"], "原变更序号")
    text(result["change_id"], "原变更编号", 57, pattern=_CHANGE)
    if result["change_id"] != _change_id(result["connection_id"], result["sequence"]):
        raise _unavailable()
    text(result["change_digest"], "原变更摘要", 64, pattern=_SHA)
    text(result["grant_id"], "原分享编号", 64, empty=True)
    integer(result["grant_revision"], "原分享版本", minimum=0)
    if bool(result["grant_id"]) != bool(result["grant_revision"]):
        raise _unavailable()
    return result


class CalendarChangeMixin:
    """No provider calls, worker creation, or inferred changes from staged pages.

    The feed begins at explicit enrollment. Earlier committed mirrors yield labeled
    baseline facts, never invented historical provider transitions.
    """

    @staticmethod
    def _event_change_ref(record):
        return {
            "event_id": record["event_id"],
            "event_revision": record["revision"],
            "payload_sha256": record["payload_sha256"],
            "occurrence_key": record["occurrence_key"],
            "available": record.get("available", True),
            "status": record["status"],
            "auth_epoch": record["auth_epoch"],
        }

    @staticmethod
    def _change_fields(before, after):
        result = []
        for key in ("summary", "description", "start", "end", "status"):
            if before is None or before["event"].get(key) != after["event"].get(key):
                result.append(key)
        if before is None or before.get("available", True) != after.get("available", True):
            result.append("available")
        return result

    def _validate_change(self, value, connection_id, sequence):
        try:
            integer(sequence, "原变更序号")
            if (
                set(value) != _CHANGE_FIELDS
                or value["id"] != _change_id(connection_id, sequence)
                or value["connection_id"] != connection_id
                or value["sequence"] != sequence
                or type(value["revision"]) is not int
                or value["revision"] != 1
                or value["change_kind"] not in _KINDS
                or value["reason"] not in _REASONS
            ):
                raise _broken()
            text(value["sync_generation"], "同步批次", 32, pattern=_INSTANCE)
            timestamp(value["observed_at"])
            for ref in (value["before_ref"], value["after_ref"]):
                if ref is None:
                    continue
                if set(ref) != _EVENT_REF_FIELDS or type(ref["available"]) is not bool:
                    raise _broken()
                text(ref["event_id"], "原日程编号", 1024)
                integer(ref["event_revision"], "原日程修订")
                text(ref["payload_sha256"], "原日程摘要", 64, pattern=_SHA)
                text(ref["occurrence_key"], "原日程实例", 2048)
                integer(ref["auth_epoch"], "原账户版本")
                if ref["status"] not in {"confirmed", "tentative", "cancelled"}:
                    raise _broken()
            if value["after_ref"] is None or (
                value["before_ref"] is not None
                and value["before_ref"]["event_id"] != value["after_ref"]["event_id"]
            ):
                raise _broken()
            changed = value["changed_fields"]
            if (
                not isinstance(changed, list)
                or len(changed) != len(set(changed))
                or any(
                    k not in {"summary", "description", "start", "end", "status", "available"}
                    for k in changed
                )
            ):
                raise _broken()
            return value
        except (KeyError, TypeError, ValueError, PersonalAgentError):
            raise _broken() from None

    def _feed_head(self, tx, connection_id):
        head = tx.get("calendar_change_head", connection_id, missing=True)
        if head is None:
            # A lost head must not silently reenroll and reset the existing feed.
            orphan = tx.db.execute(
                "SELECT 1 FROM connection_history "
                "WHERE kind='calendar_change' AND id LIKE ? LIMIT 1",
                (_change_id(connection_id, 0)[:-20] + "%",),
            ).fetchone()
            if orphan:
                raise _broken()
            return None
        if (
            set(head)
            != {"id", "connection_id", "connection_instance", "head", "enrolled_at", "revision"}
            or head["id"] != connection_id
            or head["connection_id"] != connection_id
            or head["connection_instance"] != self._required_identity
        ):
            raise _broken()
        integer(head["head"], "日历变更游标", minimum=0)
        timestamp(head["enrolled_at"])
        prefix = _change_id(connection_id, 0)[:-20]
        rows = tx.db.execute(
            "SELECT count(*),min(id),max(id),sum(revision=1),"
            "sum(length(id)=? AND substr(id,1,?)=? AND substr(id,?) NOT GLOB '*[^0-9]*') "
            "FROM connection_records WHERE kind='calendar_change' AND parent_id=?",
            (len(prefix) + 20, len(prefix), prefix, len(prefix) + 1, connection_id),
        ).fetchone()
        count = head["head"]
        expected = (
            count,
            _change_id(connection_id, 1) if count else None,
            _change_id(connection_id, count) if count else None,
            count if count else None,
            count if count else None,
        )
        if tuple(rows) != expected:
            raise _broken()
        orphan = tx.db.execute(
            "SELECT 1 FROM connection_history h LEFT JOIN connection_records r "
            "ON h.kind=r.kind AND h.id=r.id WHERE h.kind='calendar_change' "
            "AND h.id LIKE ? AND r.id IS NULL LIMIT 1",
            (prefix + "%",),
        ).fetchone()
        if orphan:
            raise _broken()
        return head

    def _append_calendar_change(self, tx, before, after, *, reason, sync_generation):
        connection_id = after["connection_id"]
        cache = getattr(tx, "_calendar_change_heads", None)
        if cache is None:
            tx._calendar_change_heads = cache = {}
        if connection_id not in cache:
            cache[connection_id] = self._feed_head(tx, connection_id)
        head = cache[connection_id]
        if head is None:
            return
        if reason == "enrollment_snapshot":
            kind = "baseline"
        elif not after.get("available", True):
            kind = "unavailable"
        elif after["status"] == "cancelled":
            kind = "cancelled"
        elif before is None:
            kind = "created"
        elif not before.get("available", True) or before["status"] == "cancelled":
            kind = "restored"
        else:
            kind = "updated"
        sequence = head["head"] + 1
        integer(sequence, "日历变更序号")
        value = {
            "id": _change_id(connection_id, sequence),
            "connection_id": connection_id,
            "sequence": sequence,
            "change_kind": kind,
            "reason": reason,
            "sync_generation": sync_generation,
            "before_ref": None if before is None else self._event_change_ref(before),
            "after_ref": self._event_change_ref(after),
            "changed_fields": self._change_fields(before, after),
            "observed_at": self.stamp(),
        }
        self._validate_change({**value, "revision": 1}, connection_id, sequence)
        tx.put("calendar_change", value, expected_revision=0)
        cache[connection_id] = tx.put(
            "calendar_change_head", {**head, "head": sequence}, expected_revision=head["revision"]
        )

    def _publish_calendar_event(self, tx, value, expected_revision, reason, sync_generation):
        """Commit the original mirror and its feed fact together, or neither."""
        if reason not in _REASONS - {"enrollment_snapshot"}:
            raise _broken()
        before = tx.get("event", value["id"], missing=True)
        if (before["revision"] if before else 0) != expected_revision:
            raise PersonalAgentError("version_conflict", "原日程镜像已更新，请核对同步批次")
        if before is not None:
            semantic = ("payload_sha256", "occurrence_key", "status", "auth_epoch")
            if all(before[k] == value[k] for k in semantic) and before.get(
                "available", True
            ) == value.get("available", True):
                return before
        after = tx.put("event", value, expected_revision=expected_revision)
        self._append_calendar_change(
            tx, before, after, reason=reason, sync_generation=sync_generation
        )
        return after

    def _change_connection(self, tx, original, actor):
        current = tx.get("connection", original["id"])
        grant, expected = self._grant(current, actor), self._grant(original, actor)
        if current["auth_epoch"] != original["auth_epoch"] or (grant["id"], grant["revision"]) != (
            expected["id"],
            expected["revision"],
        ):
            raise _unavailable()
        return current

    def _head_view(self, connection, actor, head):
        grant = self._grant(connection, actor)
        if connection.get("last_sync_at") is not None:
            timestamp(connection["last_sync_at"])
        return {
            "head": 0 if head is None else head["head"],
            "connection_instance": self._required_identity,
            "auth_epoch": connection["auth_epoch"],
            "grant": {"id": grant["id"], "revision": grant["revision"]},
            "last_sync_at": connection.get("last_sync_at"),
        }

    async def enroll_changes(self, connection_id, actor_id):
        original = await self._connection(connection_id, actor_id)
        await self._credential(original)

        def enroll(tx):
            connection = self._change_connection(tx, original, actor_id)
            head = self._feed_head(tx, connection_id)
            if head is None:
                tx.put(
                    "calendar_change_head",
                    {
                        "id": connection_id,
                        "connection_id": connection_id,
                        "connection_instance": self._required_identity,
                        "head": 0,
                        "enrolled_at": self.stamp(),
                    },
                    expected_revision=0,
                )
                records = tx.list("event", parent=connection_id, limit=10001)
                if len(records) > 10000:
                    raise PersonalAgentError("sync_limit", "日历镜像超过变更登记容量")
                for record in records:
                    if record["auth_epoch"] == connection["auth_epoch"]:
                        self._append_calendar_change(
                            tx, None, record, reason="enrollment_snapshot", sync_generation="0" * 32
                        )
                head = self._feed_head(tx, connection_id)
            return self._head_view(connection, actor_id, head)

        await self._write(enroll)
        return await self.change_head(connection_id, actor_id)

    async def change_head(self, connection_id, actor_id):
        original = await self._connection(connection_id, actor_id)
        await self._credential(original)
        return await self._read(
            lambda tx: self._head_view(
                self._change_connection(tx, original, actor_id),
                actor_id,
                self._feed_head(tx, connection_id),
            )
        )

    async def change_page(self, connection_id, actor_id, after_sequence=0, limit=50):
        integer(after_sequence, "原日历变更游标", minimum=0)
        integer(limit, "变更页大小", maximum=100)
        original = await self._connection(connection_id, actor_id)
        await self._credential(original)

        def read(tx):
            self._change_connection(tx, original, actor_id)
            record = self._feed_head(tx, connection_id)
            head = 0 if record is None else record["head"]
            if after_sequence > head:
                raise PersonalAgentError("source_stale", "原变更游标不属于当前来源")
            end = min(head, after_sequence + limit)
            changes = [
                self._validate_change(
                    tx.get("calendar_change", _change_id(connection_id, n)), connection_id, n
                )
                for n in range(after_sequence + 1, end + 1)
            ]
            return {"changes": changes, "next_sequence": end, "head": head, "partial": end < head}

        return await self._read(read)

    def change_reference(self, record, connection, actor):
        self._validate_change(record, connection["id"], record["sequence"])
        grant = self._grant(connection, actor)
        return normalize_change_ref(
            {
                "connection_id": connection["id"],
                "connection_instance": self._required_identity,
                "auth_epoch": record["after_ref"]["auth_epoch"],
                "change_id": record["id"],
                "sequence": record["sequence"],
                "change_digest": digest(record),
                "grant_id": grant["id"],
                "grant_revision": grant["revision"],
            }
        )

    def _historical_change_event(self, tx, connection_id, ref):
        if ref is None:
            return None
        try:
            key = self._event_key(connection_id, ref["event_id"])
            cache = getattr(tx, "_calendar_event_histories", None)
            if cache is None:
                tx._calendar_event_histories = cache = {}
            if key not in cache:
                current = tx.get("event", key)
                history = {}
                rows = tx.db.execute(
                    "SELECT * FROM connection_history "
                    "WHERE kind='event' AND id=? ORDER BY revision",
                    (key,),
                ).fetchall()
                for index, row in enumerate(rows, start=1):
                    value = tx._decode(row)
                    if (
                        set(value) - _MIRROR_FIELDS
                        or _MIRROR_FIELDS - {"available"} - set(value)
                        or value["revision"] != index
                        or value["id"] != key
                        or value["connection_id"] != connection_id
                        or value["event_id"] != ref["event_id"]
                        or value["event"]["id"] != value["event_id"]
                        or value["status"] != value["event"]["status"]
                        or type(value.get("available", True)) is not bool
                        or normalize_event(value["event"]) != value["event"]
                        or digest(value["event"]) != value["payload_sha256"]
                        or occurrence_key(value["event"]) != value["occurrence_key"]
                    ):
                        raise _broken()
                    integer(value["auth_epoch"], "原账户授权版本")
                    timestamp(value["fetched_at"])
                    history[index] = value
                if len(history) != current["revision"] or history[current["revision"]] != current:
                    raise _broken()
                cache[key] = history
            original = cache[key].get(ref["event_revision"])
            if original is None or self._event_change_ref(original) != ref:
                raise _broken()
            return original
        except (KeyError, ValueError, TypeError, GoogleProtocolError):
            raise _broken() from None

    async def capture_change(self, ref, actor_id, require_current=False):
        ref = normalize_change_ref(ref)
        if type(require_current) is not bool:
            raise PersonalAgentError("invalid_input", "日历当前版本检查需要明确选择")
        original = await self._connection(ref["connection_id"], actor_id)
        await self._credential(original)

        def read(tx):
            connection = self._change_connection(tx, original, actor_id)
            head = self._feed_head(tx, connection["id"])
            if head is None or ref["sequence"] > head["head"]:
                raise _unavailable()
            value = self._validate_change(
                tx.get("calendar_change", ref["change_id"]), connection["id"], ref["sequence"]
            )
            actual = self.change_reference(value, connection, actor_id)
            if actual != ref or ref["auth_epoch"] != connection["auth_epoch"]:
                raise _unavailable()
            before = self._historical_change_event(tx, connection["id"], value["before_ref"])
            after = self._historical_change_event(tx, connection["id"], value["after_ref"])
            if value["reason"] == "enrollment_snapshot":
                expected_kind = "baseline"
                if before is not None:
                    raise _broken()
            else:
                if (
                    before is None
                    and after["revision"] != 1
                    or before is not None
                    and after["revision"] != before["revision"] + 1
                ):
                    raise _broken()
                expected_kind = (
                    "unavailable"
                    if not after.get("available", True)
                    else "cancelled"
                    if after["status"] == "cancelled"
                    else "created"
                    if before is None
                    else "restored"
                    if not before.get("available", True) or before["status"] == "cancelled"
                    else "updated"
                )
            if value["change_kind"] != expected_kind or value[
                "changed_fields"
            ] != self._change_fields(before, after):
                raise _broken()
            current = tx.get("event", self._event_key(connection["id"], after["event_id"]))
            self._historical_change_event(tx, connection["id"], self._event_change_ref(current))
            if current["auth_epoch"] != connection["auth_epoch"]:
                raise _unavailable()
            if require_current and self._event_change_ref(current) != value["after_ref"]:
                raise PersonalAgentError("source_stale", "日程已再次变化，请核对最新建议")
            tombstone = not current.get("available", True) or current["status"] == "cancelled"
            master_id = current["event"].get("recurringEventId")
            if master_id:
                master = tx.get("event", self._event_key(connection["id"], master_id))
                self._historical_change_event(tx, connection["id"], self._event_change_ref(master))
                if master["auth_epoch"] != connection["auth_epoch"]:
                    raise _unavailable()
                if not master.get("available", True) or master["status"] == "cancelled":
                    if require_current and not tombstone:
                        raise PersonalAgentError("source_stale", "原周期日程已变化，请核对最新建议")
                    tombstone = True

            def project(record):
                if record is None:
                    return None
                if record["auth_epoch"] != connection["auth_epoch"]:
                    return None
                if tombstone:
                    return {"status": record["status"], "available": record.get("available", True)}
                return copy.deepcopy(record["event"])

            safe_current = copy.deepcopy(current)
            if tombstone:
                safe_current["event"] = {
                    "status": current["status"],
                    "available": current.get("available", True),
                }
            return {
                "change": value,
                "before": project(before),
                "after": project(after),
                "current": safe_current,
                "reference": actual,
                "last_sync_at": connection.get("last_sync_at"),
            }

        await self._read(read)
        # Credential access is outside journal locks, followed by the final complete
        # snapshot. Token refresh generations do not replace epoch or grant identity.
        await self._credential(await self._connection(ref["connection_id"], actor_id))
        return await self._read(read)
