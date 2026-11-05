"""Owner-bound research facts with immutable evidence and exact revision histories."""

from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.owner_journal import (
    JournalSpec,
    JournalTransaction,
    OwnerJournal,
    integer,
)
from naumi_agent.personal.research_egress_models import EGRESS_CORE, egress_digest

KINDS = frozenset(
    {
        "notebook",
        "source",
        "review",
        "read",
        "intent",
        "publication",
        "discovery",
        "egress",
        "egress_decision",
        "context_update",
        "context_result",
        "context_head",
    }
)
APPEND_ONLY = frozenset(
    {"review", "read", "discovery", "egress_decision", "context_update", "context_result"}
)
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
            if kind == "egress":
                if value.get("digest") != egress_digest(value):
                    raise PersonalAgentError("binding_conflict", "外发内容与原提案摘要不一致")
                if existing is None and (
                    value.get("status") != "proposed"
                    or value.get("approval") is not None
                    or value.get("execution") is not None
                ):
                    raise PersonalAgentError("binding_conflict", "外发提案需要从未批准状态创建")
                if existing is not None and any(
                    value.get(key) != existing.get(key) for key in EGRESS_CORE
                ):
                    raise PersonalAgentError("binding_conflict", "原外发提案的内容和来源不能改变")
                if existing is not None:
                    transitions = {
                        "proposed": {"proposed", "approved", "rejected", "revoked"},
                        "approved": {"approved", "started", "rejected", "revoked"},
                        "started": {"started", "completed"},
                        "completed": {"completed"},
                        "rejected": {"rejected"},
                        "revoked": {"revoked"},
                    }
                    if value.get("status") not in transitions.get(existing.get("status"), set()):
                        raise PersonalAgentError("binding_conflict", "原外发提案不能退回可发送状态")
                    if (
                        existing.get("approval") is not None
                        and value.get("approval") != existing["approval"]
                    ):
                        raise PersonalAgentError("binding_conflict", "原外发批准凭据不能被替换")
                    if existing.get("execution") is not None:
                        old = existing["execution"]
                        incoming = value.get("execution")
                        if not isinstance(incoming, dict) or any(
                            incoming.get(key) != item
                            for key, item in old.items()
                            if key not in {"status", "result"}
                        ):
                            raise PersonalAgentError("binding_conflict", "原外发执行身份不能被替换")
                        if old.get("status") == "completed" and incoming != old:
                            raise PersonalAgentError("binding_conflict", "原外发完成收据不能改变")
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
