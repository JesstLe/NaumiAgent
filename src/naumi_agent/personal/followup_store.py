"""Fenced proposal and human-decision journal, independent of all executors."""

from naumi_agent.personal.followup_models import error
from naumi_agent.personal.owner_journal import JournalSpec, JournalTransaction, OwnerJournal

KINDS = frozenset({"rule", "scan", "proposal", "decision", "effect", "read", "cursor"})
APPEND_ONLY = frozenset({"proposal", "read", "cursor"})
SPEC = JournalSpec(
    namespace="followup",
    kinds=KINDS,
    parent_field="workspace_key",
    label="跟进",
    db_filename="personal_followups.db",
    witness_filename=".personal-followups-v1",
    lock_filename=".personal-followups-bootstrap.lock",
    domain_hashes=True,
    lease_proofs=True,
)


class FollowupTransaction(JournalTransaction):
    def put(self, kind, value, *, expected_revision):
        if kind in APPEND_ONLY and expected_revision != 0:
            raise error("version_conflict")
        return super().put(kind, value, expected_revision=expected_revision)


class FollowupStore(OwnerJournal):
    transaction_type = FollowupTransaction

    def __init__(self, base_dir, *, owner_key, expected_instance_id=None):
        super().__init__(
            base_dir, owner_key=owner_key, spec=SPEC, expected_instance_id=expected_instance_id
        )
