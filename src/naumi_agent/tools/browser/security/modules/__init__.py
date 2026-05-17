# ruff: noqa: E501
"""Individual audit modules for the browser security scanner.

Each module is an async function taking ``(page, add_finding)`` where
*page* is a Playwright ``Page`` and *add_finding* is a callback that
records a finding dict and returns the augmented entry.
"""

from naumi_agent.tools.browser.security.modules.accessibility import audit_accessibility
from naumi_agent.tools.browser.security.modules.api_fuzz import fuzz_api
from naumi_agent.tools.browser.security.modules.auth_bypass import test_auth_bypass
from naumi_agent.tools.browser.security.modules.clickjacking import test_clickjacking
from naumi_agent.tools.browser.security.modules.client_storage import scan_client_storage
from naumi_agent.tools.browser.security.modules.command_injection import test_command_injection
from naumi_agent.tools.browser.security.modules.cookies import audit_cookies
from naumi_agent.tools.browser.security.modules.cors import audit_cors
from naumi_agent.tools.browser.security.modules.csrf import detect_csrf
from naumi_agent.tools.browser.security.modules.dependency_vuln import scan_dependency_vulns
from naumi_agent.tools.browser.security.modules.file_upload import test_file_upload_bypass
from naumi_agent.tools.browser.security.modules.headers import audit_security_headers
from naumi_agent.tools.browser.security.modules.idor import test_idor
from naumi_agent.tools.browser.security.modules.info_leak import scan_info_leaks
from naumi_agent.tools.browser.security.modules.jwt import test_jwt
from naumi_agent.tools.browser.security.modules.open_redirect import test_open_redirect
from naumi_agent.tools.browser.security.modules.race_condition import test_race_condition
from naumi_agent.tools.browser.security.modules.sqli import test_sqli
from naumi_agent.tools.browser.security.modules.sri import check_subresource_integrity
from naumi_agent.tools.browser.security.modules.ssrf import test_ssrf
from naumi_agent.tools.browser.security.modules.ssti import test_ssti
from naumi_agent.tools.browser.security.modules.tls import audit_tls
from naumi_agent.tools.browser.security.modules.xss import test_xss

__all__ = [
    "audit_accessibility",
    "audit_cookies",
    "audit_cors",
    "audit_security_headers",
    "audit_tls",
    "check_subresource_integrity",
    "detect_csrf",
    "fuzz_api",
    "scan_client_storage",
    "scan_dependency_vulns",
    "scan_info_leaks",
    "test_auth_bypass",
    "test_clickjacking",
    "test_command_injection",
    "test_file_upload_bypass",
    "test_idor",
    "test_jwt",
    "test_open_redirect",
    "test_race_condition",
    "test_sqli",
    "test_ssrf",
    "test_ssti",
    "test_xss",
]
