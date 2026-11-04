"""Independent, owner-bound PA-04 routine facts; existing Item and Job schemas stay intact."""

from naumi_agent.personal.owner_journal import JournalSpec, JournalTransaction, OwnerJournal

KINDS = frozenset(
    {
        "routine",
        "occurrence",
        "budget",
        "calendar",
        "preview",
        "notification",
        "batch",
        "preferences",
        "followup",
        "workspace",
        "call",
        "item_binding",
    }
)
_SPEC = JournalSpec(
    namespace="routine",
    kinds=KINDS,
    parent_field="routine_id",
    label="周期事项",
    db_filename="personal_routines.db",
    witness_filename=".personal-routines-v1",
    lock_filename=".personal-routines-bootstrap.lock",
    lease_proofs=True,
    domain_hashes=True,
)


class RoutineTransaction(JournalTransaction):
    def __init__(self, db, spec=_SPEC, owner_key=""):
        super().__init__(db, spec, owner_key)


class RoutineStore(OwnerJournal):
    """One write callback atomically commits cursor, reservations, facts, and receipt.

    Parent projection uses ``routine_id``. Listing uses strict ``id > after``
    keysets. Lease transitions are independently append-only and hash chained.
    Supply the external service witness's expected_instance_id on cold reopen;
    a missing or replaced original database is then never treated as empty.
    """

    transaction_type = RoutineTransaction

    def __init__(self, base_dir, *, owner_key, expected_instance_id=None):
        super().__init__(
            base_dir,
            owner_key=owner_key,
            spec=_SPEC,
            expected_instance_id=expected_instance_id,
        )
