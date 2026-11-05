"""Explicit workspace enrollment and scope-bound federation journal access."""

from __future__ import annotations

import asyncio
import os
import re
import stat
from pathlib import Path

from naumi_agent.personal.artifact_models import strict_json_loads
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.owner_journal import canonical, checked_path, integer, text
from naumi_agent.personal.workspace_store import KINDS, WorkspaceStore

SHA = re.compile(r"^[a-f0-9]{64}$")
INSTANCE = re.compile(r"^[a-f0-9]{32}$")


class WorkspacePersistence:
    """Reads never enroll; only ensure() creates metadata and its external witness."""

    def __init__(self, engine, *, store=None):
        self.engine, self.personal = engine, engine.personal_agents
        self.workspace_key = text(self.personal.workspace_key, "工作区标识", 64, pattern=SHA)
        self.owner_key = text(engine.personal_items.owner_key, "目录归属", 64, pattern=SHA)
        database = Path(engine.config.memory.session_db_path).expanduser().absolute()
        self.boundary = database.with_name(f".personal-workspaces-{self.workspace_key}-v1")
        self._observed_pin = None
        self.store = store or WorkspaceStore(
            self.personal.store.base_dir / "workspaces",
            owner_key=self.owner_key,
        )
        if self.store.owner_key != self.owner_key:
            raise self._broken()
        pinned = self._external_identity()
        if self.store.owner_key != self.owner_key or (
            pinned and self.store.expected_instance_id not in {None, pinned}
        ):
            raise self._broken()
        if pinned is not None:
            self.store.expected_instance_id = pinned
            self._observed_pin = pinned

    @staticmethod
    def _broken():
        return PersonalAgentError(
            "storage_error", "工作区连接目录与原实例不一致，请恢复原目录后继续"
        )

    def _external_identity(self):
        try:
            checked_path(self.boundary, self.store.spec)
            if not self.boundary.exists() and not self.boundary.is_symlink():
                return None
            if (
                self.boundary.is_symlink()
                or self.boundary.stat().st_nlink != 1
                or self.boundary.stat().st_size > 1024
            ):
                raise self._broken()
            with self.boundary.open("rb") as stream:
                before = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(before.st_mode)
                    or before.st_nlink != 1
                    or before.st_size > 1024
                    or not os.path.samestat(before, self.boundary.lstat())
                ):
                    raise self._broken()
                raw = stream.read(1025)
                after = os.fstat(stream.fileno())
                if (
                    len(raw) > 1024
                    or after.st_nlink != 1
                    or not os.path.samestat(before, after)
                    or not os.path.samestat(after, self.boundary.lstat())
                    or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns)
                ):
                    raise self._broken()
            checked_path(self.boundary, self.store.spec)
            value = strict_json_loads(raw)
            if (
                not isinstance(value, dict)
                or set(value) != {"version", "owner_key", "workspace_key", "instance_id"}
                or type(value["version"]) is not int
                or value["version"] != 1
                or value["owner_key"] != self.owner_key
                or value["workspace_key"] != self.workspace_key
            ):
                raise self._broken()
            return text(value["instance_id"], "原工作区目录实例", 32, pattern=INSTANCE)
        except (OSError, ValueError, TypeError, PersonalAgentError):
            raise self._broken() from None

    def _catalog(self, *, required):
        path = checked_path(self.personal.store.db_path, self.store.spec)
        try:
            info = path.lstat()
        except FileNotFoundError:
            if required:
                raise self._broken() from None
            return False
        if not stat.S_ISREG(info.st_mode):
            raise self._broken()
        return True

    def _guard(self):
        try:
            checked_path(self.boundary, self.store.spec)
            pinned = self._external_identity()
            if self._observed_pin is not None and pinned != self._observed_pin:
                raise self._broken()
            if pinned is not None:
                self.store.expected_instance_id = pinned
                self._catalog(required=True)
            instance = self.store.identity()
            if pinned is not None and instance != pinned:
                raise self._broken()
            if instance is not None:
                self._catalog(required=True)
            if pinned is not None:
                self._observed_pin = pinned
            return instance
        except (OSError, ValueError, TypeError):
            raise self._broken() from None

    def enrolled(self):
        """Return validated enrollment state without writing a pin or creating a catalog."""
        instance = self._guard()
        return instance is not None and self._external_identity() == instance

    async def _read(self, callback):
        await asyncio.to_thread(self._guard)
        result = await asyncio.to_thread(self.store.read, callback)
        await asyncio.to_thread(self._guard)
        return result

    async def _write(self, callback):
        if not await asyncio.to_thread(self.enrolled):
            raise PersonalAgentError("workspace_not_enrolled", "请先由用户登记这个工作区")
        # An enrolled catalog is required before its existing validation is used;
        # a deleted catalog must never be recreated by a read/model mutation.
        await asyncio.to_thread(self.personal.store.list)
        await asyncio.to_thread(self._guard)
        result = await asyncio.to_thread(self.store.write, callback)
        await asyncio.to_thread(self._guard)
        return result

    async def _record(self, kind, key, *, missing=False, revision=None):
        if not isinstance(kind, str) or kind not in KINDS:
            raise self._broken()
        text(key, "工作区记录编号", 2300)
        if type(missing) is not bool:
            raise PersonalAgentError("invalid_input", "记录缺失选项格式不正确")
        if revision is not None:
            integer(revision, "原工作区记录版本")

        def read(tx):
            if tx is None:
                if missing:
                    return None
                raise PersonalAgentError("not_found", "原工作区连接记录不存在")
            value = tx.get(kind, key, missing=missing, revision=revision)
            if value is not None and value["workspace_key"] != self.workspace_key:
                raise PersonalAgentError("not_found", "该记录不在当前工作区")
            return value

        return await self._read(read)

    def _publish_pin(self, instance):
        checked_path(self.boundary, self.store.spec)
        self._catalog(required=True)
        self.boundary.parent.mkdir(parents=True, exist_ok=True)
        value = {
            "version": 1,
            "owner_key": self.owner_key,
            "workspace_key": self.workspace_key,
            "instance_id": instance,
        }
        try:
            with self.boundary.open("xb") as stream:
                stream.write((canonical(value) + "\n").encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            pass
        checked_path(self.boundary, self.store.spec)
        if self._external_identity() != instance:
            raise self._broken()
        self._observed_pin = instance

    async def ensure(self):
        """Explicitly enroll only the existing owner and this original workspace route."""
        if await asyncio.to_thread(self.enrolled):
            return self.store.expected_instance_id
        await asyncio.to_thread(self._catalog, required=False)
        await asyncio.to_thread(self.personal.store.list)
        await asyncio.to_thread(self._catalog, required=True)
        await asyncio.to_thread(self._guard)
        await asyncio.to_thread(self.store.write, lambda tx: None)
        instance = await asyncio.to_thread(self._guard)
        if instance is None:
            raise self._broken()
        await asyncio.to_thread(self._publish_pin, instance)
        await asyncio.to_thread(self._guard)
        return instance
