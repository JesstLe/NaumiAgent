"""Owner-bound research facts with immutable evidence and exact revision histories."""

from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.owner_journal import (
    JournalSpec,
    JournalTransaction,
    OwnerJournal,
    integer,
)

KINDS = frozenset({"notebook", "source", "review", "read", "intent", "publication", "discovery"})
APPEND_ONLY = frozenset({"review", "read", "discovery"})
_SPEC = JournalSpec(
    namespace="research",
    kinds=KINDS,
    parent_field="notebook_id",
    label="研究",
    db_filename="personal_research.db",
    witness_filename=".personal-research-v1",
    lock_filename=".personal-research-bootstrap.lock",
    domain_hashes=True,
    lease_proofs=True,
)


class ResearchTransaction(JournalTransaction):
    """Preserve owner-domain facts and record bindings across every CAS revision."""

    def __init__(self, db, spec=_SPEC, owner_key=""):
        super().__init__(db, spec, owner_key)

    def put(self, kind, value, *, expected_revision):
        integer(expected_revision, "预期研究版本", minimum=0)
        if kind in APPEND_ONLY and expected_revision != 0:
            raise PersonalAgentError(f"immutable_{kind}", "原研究凭据已保存，请创建新的记录")
        if isinstance(value, dict) and kind in KINDS:
            existing = self.get(kind, value.get("id"), missing=True)
            if existing is not None:
                for key in ("actor_id", "workspace_key", "notebook_id"):
                    if key in existing and value.get(key) != existing[key]:
                        raise PersonalAgentError(
                            "binding_conflict", "研究记录的原身份和作用范围不能改变"
                        )
        return super().put(kind, value, expected_revision=expected_revision)


class ResearchStore(OwnerJournal):
    """Reuse journal initialization, owner seals, CAS, receipts and fenced leases.

    The service must supply its externally pinned instance ID on reopen and must
    authorize the requested actor/workspace before passing record contents here.
    The journal performs no provider calls and starts no execution worker.
    """

    transaction_type = ResearchTransaction

    def __init__(self, base_dir, *, owner_key, expected_instance_id=None):
        super().__init__(
            base_dir,
            owner_key=owner_key,
            spec=_SPEC,
            expected_instance_id=expected_instance_id,
        )
