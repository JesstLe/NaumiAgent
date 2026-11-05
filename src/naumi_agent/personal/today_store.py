"""Owner-bound planning metadata; Item, Job, Run and human-read schemas stay unchanged."""

from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.owner_journal import (
    JournalSpec,
    JournalTransaction,
    OwnerJournal,
    integer,
)

KINDS = frozenset({"metadata", "day", "change", "read"})
_SPEC = JournalSpec(
    namespace="today",
    kinds=KINDS,
    parent_field="day_id",
    label="今日计划",
    db_filename="personal_today.db",
    witness_filename=".personal-today-v1",
    lock_filename=".personal-today-bootstrap.lock",
    domain_hashes=True,
)


class TodayTransaction(JournalTransaction):
    """Keep all plan edits and their immutable receipt in one existing transaction."""

    def __init__(self, db, spec=_SPEC, owner_key=""):
        super().__init__(db, spec, owner_key)

    def put(self, kind, value, *, expected_revision):
        integer(expected_revision, "预期计划版本", minimum=0)
        if kind in {"change", "read"} and expected_revision != 0:
            code = "immutable_read" if kind == "read" else "immutable_change"
            raise PersonalAgentError(code, "原计划凭据已保存，请创建新的记录")
        return super().put(kind, value, expected_revision=expected_revision)


class TodayStore(OwnerJournal):
    """Reuse journal CAS/history/keysets/initialization, with no execution worker.

    The service pins ``identity()`` outside this directory and supplies that pin
    on cold reopen, so deleting a database and its local witness cannot silently
    create an unrelated empty installation.
    """

    transaction_type = TodayTransaction

    def __init__(self, base_dir, *, owner_key, expected_instance_id=None):
        super().__init__(
            base_dir,
            owner_key=owner_key,
            spec=_SPEC,
            expected_instance_id=expected_instance_id,
        )
