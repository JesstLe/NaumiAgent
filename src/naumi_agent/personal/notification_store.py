"""Owner-bound notification references; execution and original human reads stay separate."""

from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.owner_journal import (
    JournalSpec,
    JournalTransaction,
    OwnerJournal,
    integer,
)

KINDS = frozenset(
    {
        "preferences",
        "scan",
        "watch",
        "event",
        "outbox",
        "batch",
        "presentation",
        "notice_read",
        "read",
    }
)
APPEND_ONLY = frozenset({"event", "batch", "presentation", "notice_read", "read"})
_SPEC = JournalSpec(
    namespace="notification",
    kinds=KINDS,
    parent_field="workspace_key",
    label="通知",
    db_filename="personal_notifications.db",
    witness_filename=".personal-notifications-v1",
    lock_filename=".personal-notifications-bootstrap.lock",
    domain_hashes=True,
    lease_proofs=True,
)


class NotificationTransaction(JournalTransaction):
    """Commit source references and immutable receipts inside one fenced transaction."""

    def __init__(self, db, spec=_SPEC, owner_key=""):
        super().__init__(db, spec, owner_key)

    def put(self, kind, value, *, expected_revision):
        integer(expected_revision, "预期通知版本", minimum=0)
        if kind in APPEND_ONLY and expected_revision != 0:
            raise PersonalAgentError(f"immutable_{kind}", "原通知凭据已保存，请创建新的记录")
        return super().put(kind, value, expected_revision=expected_revision)


class NotificationStore(OwnerJournal):
    """Reuse CAS, immutable history, owner binding, durable requests and lease proofs.

    The runtime supplies its externally pinned expected_instance_id when reopening;
    deleting the database plus its local witness never resets that runtime's source
    identity. The store itself creates neither Job nor Run nor execution worker.
    """

    transaction_type = NotificationTransaction

    def __init__(self, base_dir, *, owner_key, expected_instance_id=None):
        super().__init__(
            base_dir,
            owner_key=owner_key,
            spec=_SPEC,
            expected_instance_id=expected_instance_id,
        )
