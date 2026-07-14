"""Runtime-owned dependency ports."""

from naumi_agent.runtime.ports.permission import PermissionPort
from naumi_agent.runtime.ports.session import SessionPort, SessionT

__all__ = ["PermissionPort", "SessionPort", "SessionT"]
