"""Crash recovery coordinator for durable evolution patch journals."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from naumi_agent.evolution.patch_journals import (
    EvolutionPatchJournal,
    EvolutionPatchJournalStore,
    PatchJournalState,
)
from naumi_agent.evolution.patch_sets import (
    EvolutionPatchSetStore,
    EvolutionPatchSetTransaction,
    PatchSetState,
)
from naumi_agent.evolution.patch_writers import (
    EvolutionPatchWriteError,
    _atomic_replace,
    _fsync_directory,
    _read_lock,
    _reclaim_stale_lock,
    _release_lock,
    _verify_target_path,
)


class EvolutionPatchRecoveryResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    journal_id: str = Field(pattern=r"^evj_[0-9a-f]{24}$")
    lease_id: str = Field(pattern=r"^evl_[0-9a-f]{24}$")
    status: Literal[
        "rolled_back",
        "already_baseline",
        "orphan_lock_removed",
        "deferred",
        "failed",
    ]
    failure_code: str = Field(default="", max_length=128)
    filesystem_changed: bool
    recovery_complete: bool


class EvolutionPatchRecoveryCoordinator:
    """Reconcile incomplete filesystem writes against durable before/after digests."""

    def __init__(
        self,
        *,
        journal_store: EvolutionPatchJournalStore,
        patch_set_store: EvolutionPatchSetStore | None = None,
        worktree_storage_dir: str | Path | None = None,
    ) -> None:
        self._journal_store = journal_store
        self._patch_set_store = patch_set_store
        self._worktree_storage_dir = (
            Path(worktree_storage_dir).expanduser().resolve()
            if worktree_storage_dir is not None
            else None
        )

    async def recover_pending(
        self,
        *,
        limit: int = 100,
    ) -> tuple[EvolutionPatchRecoveryResult, ...]:
        journals, failures = await asyncio.to_thread(
            self._journal_store.scan_recoverable,
            limit=limit,
        )
        outcomes = [
            EvolutionPatchRecoveryResult(
                journal_id=failure.journal_id,
                lease_id=failure.lease_id,
                status="failed",
                failure_code=failure.failure_code,
                filesystem_changed=False,
                recovery_complete=False,
            )
            for failure in failures
        ]
        for journal in journals:
            outcomes.append(await asyncio.to_thread(self._recover_one, journal))
        if self._worktree_storage_dir is not None:
            known_leases = {journal.lease_id for journal in journals}
            if self._patch_set_store is not None:
                patch_sets, patch_set_failures = await asyncio.to_thread(
                    self._patch_set_store.scan_recoverable,
                    limit=limit,
                )
                known_leases.update(item.lease_id for item in patch_sets)
                known_leases.update(item.lease_id for item in patch_set_failures)
            outcomes.extend(
                await asyncio.to_thread(
                    self._recover_orphan_locks,
                    known_leases,
                    limit,
                )
            )
        return tuple(outcomes)

    def _recover_orphan_locks(
        self,
        known_leases: set[str],
        limit: int,
    ) -> tuple[EvolutionPatchRecoveryResult, ...]:
        storage = self._worktree_storage_dir
        if storage is None or not storage.is_dir():
            return ()
        outcomes: list[EvolutionPatchRecoveryResult] = []
        for path in sorted(storage.glob(".*.patch.lock"))[:limit]:
            synthetic_journal_id = f"evj_{hashlib.sha256(str(path).encode()).hexdigest()[:24]}"
            lease_id = ""
            try:
                payload = _read_lock(path)
                lease_id = str(payload["lease_id"])
                worktree_name = str(payload["worktree_name"])
                expected_name = f".{worktree_name}.{lease_id}.patch.lock"
                if path.name != expected_name:
                    raise EvolutionPatchWriteError(
                        "stale_lock_binding",
                        "孤儿锁文件名与内容 binding 不一致。",
                    )
                if lease_id in known_leases:
                    continue
                binding = SimpleNamespace(
                    lease_id=lease_id,
                    worktree_name=worktree_name,
                )
                token = _reclaim_stale_lock(path, binding)
                _release_lock(path, token)
            except EvolutionPatchWriteError as exc:
                lease_id = _safe_lease_id(lease_id, path)
                status = (
                    "deferred"
                    if exc.code in {"writer_locked", "remote_lock_owner"}
                    else "failed"
                )
                outcomes.append(
                    EvolutionPatchRecoveryResult(
                        journal_id=synthetic_journal_id,
                        lease_id=lease_id,
                        status=status,
                        failure_code=exc.code,
                        filesystem_changed=False,
                        recovery_complete=False,
                    )
                )
                continue
            except OSError:
                outcomes.append(
                    EvolutionPatchRecoveryResult(
                        journal_id=synthetic_journal_id,
                        lease_id=_safe_lease_id(lease_id, path),
                        status="failed",
                        failure_code="lock_unreadable",
                        filesystem_changed=False,
                        recovery_complete=False,
                    )
                )
                continue
            outcomes.append(
                EvolutionPatchRecoveryResult(
                    journal_id=synthetic_journal_id,
                    lease_id=lease_id,
                    status="orphan_lock_removed",
                    filesystem_changed=False,
                    recovery_complete=True,
                )
            )
        return tuple(outcomes)

    def _recover_one(self, journal: EvolutionPatchJournal) -> EvolutionPatchRecoveryResult:
        try:
            root = Path(journal.worktree_path).resolve(strict=True)
        except OSError:
            return self._fail(journal, "worktree_unavailable")
        if not root.is_dir() or root.name != journal.worktree_name:
            return self._fail(journal, "worktree_binding")
        lock_path = root.parent / f".{journal.worktree_name}.{journal.lease_id}.patch.lock"
        try:
            token = _reclaim_stale_lock(lock_path, journal)
        except (EvolutionPatchWriteError, OSError) as exc:
            code = exc.code if isinstance(exc, EvolutionPatchWriteError) else "lock_unreadable"
            return EvolutionPatchRecoveryResult(
                journal_id=journal.journal_id,
                lease_id=journal.lease_id,
                status="deferred",
                failure_code=code,
                filesystem_changed=False,
                recovery_complete=False,
            )
        try:
            current = self._journal_store.get_by_lease(journal.lease_id)
            if current is None:
                return self._fail(journal, "journal_missing")
            if current.state not in {PatchJournalState.PREPARED, PatchJournalState.REPLACED}:
                return EvolutionPatchRecoveryResult(
                    journal_id=current.journal_id,
                    lease_id=current.lease_id,
                    status="deferred",
                    failure_code="journal_already_terminal",
                    filesystem_changed=False,
                    recovery_complete=False,
                )
            return self._restore(current, root)
        finally:
            _release_lock(lock_path, token)

    def _restore(
        self,
        journal: EvolutionPatchJournal,
        root: Path,
    ) -> EvolutionPatchRecoveryResult:
        target = root / journal.target_path
        try:
            _verify_target_path(root, target)
            backup = self._journal_store.load_backup(journal.journal_id)
        except (OSError, ValueError, EvolutionPatchWriteError):
            return self._fail(journal, "recovery_evidence_invalid")
        before_matches = _matches_before(target, journal)
        after_matches = _matches_file(target, journal.after_sha256)
        if before_matches:
            try:
                self._journal_store.mark_rolled_back(
                    journal.journal_id,
                    failure_code="recovered_before_replace",
                )
            except (KeyError, RuntimeError, ValueError):
                return self._fail(journal, "journal_finalize_failed")
            return EvolutionPatchRecoveryResult(
                journal_id=journal.journal_id,
                lease_id=journal.lease_id,
                status="already_baseline",
                filesystem_changed=False,
                recovery_complete=True,
            )
        if not after_matches:
            return self._fail(journal, "target_digest_unknown")
        try:
            if journal.operation == "modify":
                if backup is None:
                    return self._fail(journal, "backup_missing")
                _atomic_replace(target, backup, mode=journal.file_mode)
            else:
                target.unlink()
                _fsync_directory(target.parent)
            if not _matches_before(target, journal):
                return self._fail(journal, "rollback_digest_mismatch")
            self._journal_store.mark_rolled_back(
                journal.journal_id,
                failure_code="recovered_after_replace",
            )
        except (KeyError, OSError, RuntimeError, ValueError):
            return self._fail(journal, "rollback_failed")
        return EvolutionPatchRecoveryResult(
            journal_id=journal.journal_id,
            lease_id=journal.lease_id,
            status="rolled_back",
            filesystem_changed=True,
            recovery_complete=True,
        )

    def _fail(
        self,
        journal: EvolutionPatchJournal,
        code: str,
    ) -> EvolutionPatchRecoveryResult:
        try:
            current = self._journal_store.get_by_lease(journal.lease_id)
            if current is not None and current.state in {
                PatchJournalState.PREPARED,
                PatchJournalState.REPLACED,
            }:
                self._journal_store.mark_recovery_failed(
                    current.journal_id,
                    failure_code=code,
                )
        except (KeyError, RuntimeError, ValueError):
            pass
        return EvolutionPatchRecoveryResult(
            journal_id=journal.journal_id,
            lease_id=journal.lease_id,
            status="failed",
            failure_code=code,
            filesystem_changed=False,
            recovery_complete=False,
        )


class EvolutionPatchSetRecoveryResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    transaction_id: str = Field(pattern=r"^evset_[0-9a-f]{24}$")
    lease_id: str = Field(pattern=r"^evl_[0-9a-f]{24}$")
    status: Literal["rolled_back", "already_baseline", "deferred", "failed"]
    failure_code: str = Field(default="", max_length=128)
    filesystem_changed: bool
    recovery_complete: bool
    file_count: int = Field(ge=0, le=16)


class EvolutionPatchSetRecoveryCoordinator:
    """Restore an incomplete write-set only when every target has known state."""

    def __init__(
        self,
        *,
        patch_set_store: EvolutionPatchSetStore,
        journal_store: EvolutionPatchJournalStore,
    ) -> None:
        self._patch_set_store = patch_set_store
        self._journal_store = journal_store

    async def recover_pending(
        self,
        *,
        limit: int = 100,
    ) -> tuple[EvolutionPatchSetRecoveryResult, ...]:
        transactions, failures = await asyncio.to_thread(
            self._patch_set_store.scan_recoverable,
            limit=limit,
        )
        outcomes = [
            EvolutionPatchSetRecoveryResult(
                transaction_id=failure.transaction_id,
                lease_id=failure.lease_id,
                status="failed",
                failure_code=failure.failure_code,
                filesystem_changed=False,
                recovery_complete=False,
                file_count=0,
            )
            for failure in failures
        ]
        for transaction in transactions:
            outcomes.append(await asyncio.to_thread(self._recover_one, transaction))
        return tuple(outcomes)

    def _recover_one(
        self,
        transaction: EvolutionPatchSetTransaction,
    ) -> EvolutionPatchSetRecoveryResult:
        try:
            single_journal = self._journal_store.get_by_lease(transaction.lease_id)
        except ValueError:
            return self._fail(transaction, "single_journal_corrupt")
        if single_journal is not None:
            return self._fail(transaction, "single_journal_conflict")
        try:
            root = Path(transaction.worktree_path).resolve(strict=True)
        except OSError:
            return self._fail(transaction, "worktree_unavailable")
        if not root.is_dir() or root.name != transaction.worktree_name:
            return self._fail(transaction, "worktree_binding")
        lock_path = (
            root.parent
            / f".{transaction.worktree_name}.{transaction.lease_id}.patch.lock"
        )
        try:
            token = _reclaim_stale_lock(lock_path, transaction)
        except (EvolutionPatchWriteError, OSError) as exc:
            code = (
                exc.code
                if isinstance(exc, EvolutionPatchWriteError)
                else "lock_unreadable"
            )
            return EvolutionPatchSetRecoveryResult(
                transaction_id=transaction.transaction_id,
                lease_id=transaction.lease_id,
                status="deferred",
                failure_code=code,
                filesystem_changed=False,
                recovery_complete=False,
                file_count=len(transaction.files),
            )
        try:
            current = self._patch_set_store.get_by_lease(transaction.lease_id)
            if current is None:
                return self._fail(transaction, "write_set_missing")
            if current.state not in {
                PatchSetState.PREPARED,
                PatchSetState.APPLYING,
                PatchSetState.APPLIED,
                PatchSetState.ROLLING_BACK,
            }:
                return EvolutionPatchSetRecoveryResult(
                    transaction_id=current.transaction_id,
                    lease_id=current.lease_id,
                    status="deferred",
                    failure_code="write_set_already_terminal",
                    filesystem_changed=False,
                    recovery_complete=False,
                    file_count=len(current.files),
                )
            return self._restore(current, root)
        finally:
            _release_lock(lock_path, token)

    def _restore(
        self,
        transaction: EvolutionPatchSetTransaction,
        root: Path,
    ) -> EvolutionPatchSetRecoveryResult:
        try:
            backups = self._patch_set_store.load_backups(transaction.transaction_id)
            targets = tuple(root / item.path for item in transaction.files)
            for target in targets:
                _verify_target_path(root, target)
        except (KeyError, OSError, ValueError, EvolutionPatchWriteError):
            return self._fail(transaction, "recovery_evidence_invalid")

        states: list[Literal["before", "after", "unknown"]] = []
        for target, item in zip(targets, transaction.files, strict=True):
            if _matches_patch_set_before(target, item.operation, item.before_sha256):
                states.append("before")
            elif _matches_file(target, item.after_sha256):
                states.append("after")
            else:
                states.append("unknown")
        if "unknown" in states:
            return self._fail(transaction, "target_digest_unknown")
        if transaction.state is PatchSetState.ROLLING_BACK:
            for index in range(transaction.rollback_cursor + 1, len(states)):
                if states[index] != "before":
                    return self._fail(transaction, "rollback_progress_mismatch")

        changed = False
        try:
            current = transaction
            if current.state is not PatchSetState.ROLLING_BACK:
                current = self._patch_set_store.begin_rollback(
                    transaction.transaction_id
                )
            for index in range(current.rollback_cursor, -1, -1):
                item = current.files[index]
                target = targets[index]
                if states[index] == "after":
                    if item.operation == "modify":
                        backup = backups[index]
                        if backup is None:
                            return self._fail(transaction, "backup_missing")
                        _atomic_replace(target, backup, mode=item.file_mode)
                    else:
                        target.unlink()
                        _fsync_directory(target.parent)
                    changed = True
                if not _matches_patch_set_before(
                    target, item.operation, item.before_sha256
                ):
                    return self._fail(transaction, "rollback_digest_mismatch")
                self._patch_set_store.mark_file_rolled_back(
                    transaction.transaction_id,
                    file_index=index,
                )
            self._patch_set_store.mark_rolled_back(
                transaction.transaction_id,
                failure_code=(
                    "recovered_after_replace" if changed else "recovered_before_replace"
                ),
            )
        except (KeyError, OSError, RuntimeError, ValueError):
            return self._fail(transaction, "rollback_failed")
        return EvolutionPatchSetRecoveryResult(
            transaction_id=transaction.transaction_id,
            lease_id=transaction.lease_id,
            status="rolled_back" if changed else "already_baseline",
            filesystem_changed=changed,
            recovery_complete=True,
            file_count=len(transaction.files),
        )

    def _fail(
        self,
        transaction: EvolutionPatchSetTransaction,
        code: str,
    ) -> EvolutionPatchSetRecoveryResult:
        try:
            current = self._patch_set_store.get_by_lease(transaction.lease_id)
            if current is not None and current.state in {
                PatchSetState.PREPARED,
                PatchSetState.APPLYING,
                PatchSetState.APPLIED,
                PatchSetState.ROLLING_BACK,
            }:
                self._patch_set_store.mark_recovery_failed(
                    current.transaction_id,
                    failure_code=code,
                )
        except (KeyError, RuntimeError, ValueError):
            pass
        return EvolutionPatchSetRecoveryResult(
            transaction_id=transaction.transaction_id,
            lease_id=transaction.lease_id,
            status="failed",
            failure_code=code,
            filesystem_changed=False,
            recovery_complete=False,
            file_count=len(transaction.files),
        )


def _matches_before(target: Path, journal: EvolutionPatchJournal) -> bool:
    if journal.operation == "create":
        return not target.exists() and not target.is_symlink()
    if journal.before_sha256 is None:
        return False
    return _matches_file(target, journal.before_sha256)


def _matches_file(target: Path, expected_sha256: str) -> bool:
    try:
        metadata = target.lstat()
        if not stat.S_ISREG(metadata.st_mode) or target.is_symlink():
            return False
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
    except OSError:
        return False
    return hmac.compare_digest(digest, expected_sha256)


def _matches_patch_set_before(
    target: Path,
    operation: str,
    before_sha256: str | None,
) -> bool:
    if operation == "create":
        return not target.exists() and not target.is_symlink()
    if before_sha256 is None:
        return False
    return _matches_file(target, before_sha256)


def _safe_lease_id(value: object, path: Path) -> str:
    text = str(value or "")
    if len(text) == 28 and text.startswith("evl_") and all(
        char in "0123456789abcdef" for char in text[4:]
    ):
        return text
    digest = hashlib.sha256(str(path).encode()).hexdigest()
    return f"evl_{digest[:24]}"


__all__ = [
    "EvolutionPatchRecoveryCoordinator",
    "EvolutionPatchRecoveryResult",
    "EvolutionPatchSetRecoveryCoordinator",
    "EvolutionPatchSetRecoveryResult",
]
