"""Legacy-compatible connection journal specification and public store API."""

from naumi_agent.personal.connection_models import KINDS
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.owner_journal import (
    JournalSpec,
    JournalTransaction,
    OwnerJournal,
)
from naumi_agent.personal.owner_journal import (
    checked_path as _checked_path,
)

_SPEC = JournalSpec(
    namespace="connection",
    kinds=KINDS,
    parent_field="connection_id",
    label="账户连接",
    db_filename="personal_connections.db",
    witness_filename=".personal-connections-v1",
    lock_filename=".personal-connections-bootstrap.lock",
)
_SCHEMA = _SPEC.schema
_TABLES = _SPEC.tables
_TRIGGERS = _SPEC.triggers


def broken():
    return _SPEC.broken()


def busy():
    return _SPEC.busy()


def checked_path(path):
    return _checked_path(path, _SPEC)


class ConnectionTransaction(JournalTransaction):
    def __init__(self, db, spec=_SPEC, owner_key=""):
        super().__init__(db, spec, owner_key)

    def put(self, kind, value, *, expected_revision):
        if kind == "calendar_change" and expected_revision != 0:
            raise PersonalAgentError("immutable_calendar_change", "原日历变更已保存，请读取原凭据")
        return super().put(kind, value, expected_revision=expected_revision)


class ConnectionStore(OwnerJournal):
    transaction_type = ConnectionTransaction

    def __init__(self, base_dir, *, owner_key):
        super().__init__(base_dir, owner_key=owner_key, spec=_SPEC)
