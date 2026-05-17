# ruff: noqa: E501
"""Browser security auditing — 25-module scanner + multi-agent coordinator.

Ported from browser-debugging-daemon/scripts/security/SecurityAuditor.js (3085 lines)
and AgentCoordinator.js (324 lines).
"""

from naumi_agent.tools.browser.security.auditor import SecurityAuditor, finding_fingerprint
from naumi_agent.tools.browser.security.coordinator import AgentCoordinator

__all__ = ["SecurityAuditor", "AgentCoordinator", "finding_fingerprint"]
