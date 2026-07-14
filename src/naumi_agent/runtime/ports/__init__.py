"""Runtime-owned dependency ports."""

from naumi_agent.runtime.ports.model import ModelPort
from naumi_agent.runtime.ports.permission import PermissionPort
from naumi_agent.runtime.ports.session import SessionPort, SessionT

__all__ = ["ModelPort", "PermissionPort", "SessionPort", "SessionT"]
