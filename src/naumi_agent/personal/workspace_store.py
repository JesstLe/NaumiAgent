"""Owner-bound federation facts; source runtimes retain all execution authority."""

from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.owner_journal import (
    JournalSpec,
    JournalTransaction,
    OwnerJournal,
    integer,
)

KINDS = frozenset({"grant", "connection", "source_read", "read"})
APPEND_ONLY = frozenset({"source_read", "read"})
_SPEC = JournalSpec(
    namespace="workspace",
    kinds=KINDS,
    parent_field="workspace_key",
    label="工作区连接",
    db_filename="personal_workspaces.db",
    witness_filename=".personal-workspaces-v1",
    lock_filename=".personal-workspaces-bootstrap.lock",
    domain_hashes=True,
    lease_proofs=True,
)


class WorkspaceTransaction(JournalTransaction):
    """Commit federation lifecycle changes and opaque receipts atomically."""

    def __init__(self, db, spec=_SPEC, owner_key=""):
        super().__init__(db, spec, owner_key)

    def put(self, kind, value, *, expected_revision):
        integer(expected_revision, "预期工作区记录版本", minimum=0)
        if kind in APPEND_ONLY and expected_revision != 0:
            raise PersonalAgentError(f"immutable_{kind}", "原读取凭据已保存，请创建新的记录")
        return super().put(kind, value, expected_revision=expected_revision)


class WorkspaceStore(OwnerJournal):
    """Reuse authenticated CAS/history/request/lease storage, without any worker."""

    transaction_type = WorkspaceTransaction

    def __init__(self, base_dir, *, owner_key, expected_instance_id=None):
        super().__init__(
            base_dir,
            owner_key=owner_key,
            spec=_SPEC,
            expected_instance_id=expected_instance_id,
        )
