"""Task-local durable permission context for explicitly delegating tools."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from naumi_agent.daemons.permission_decisions import PermissionDecisionReceipt


@dataclass(slots=True)
class _PermissionInvocationCapability:
    receipt: PermissionDecisionReceipt | None
    active: bool = True


_CURRENT_CAPABILITY: ContextVar[_PermissionInvocationCapability | None] = ContextVar(
    "naumi_current_permission_receipt",
    default=None,
)


def current_permission_receipt() -> PermissionDecisionReceipt | None:
    capability = _CURRENT_CAPABILITY.get()
    if capability is None or not capability.active:
        return None
    return capability.receipt


@contextmanager
def bind_permission_receipt(
    receipt: PermissionDecisionReceipt | None,
) -> Iterator[None]:
    capability = _PermissionInvocationCapability(receipt=receipt)
    token = _CURRENT_CAPABILITY.set(capability)
    try:
        yield
    finally:
        capability.active = False
        _CURRENT_CAPABILITY.reset(token)


__all__ = ["bind_permission_receipt", "current_permission_receipt"]
