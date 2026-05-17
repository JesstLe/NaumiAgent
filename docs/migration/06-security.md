# Phase 6: Security Auditor (25 Modules)

## Source Files
- `scripts/security/SecurityAuditor.js` (3085 lines)
- `scripts/security/AgentCoordinator.js` (324 lines)
- `scripts/security/scanner_cli.js` (CLI tool, port as Python entry point)

## Objective

Port the full 25-module security auditor with OSV.dev CVE lookup, multi-agent parallel scanning, SARIF/HTML/JSON report export, scan profiles, baseline comparison, and false positive suppression.

## Files to Create

```
src/naumi_agent/tools/browser/
├── security/
│   ├── __init__.py
│   ├── auditor.py            # SecurityAuditor class
│   ├── coordinator.py        # AgentCoordinator class
│   ├── modules/              # Individual audit modules
│   │   ├── __init__.py
│   │   ├── security_headers.py
│   │   ├── cookies.py
│   │   ├── cors.py
│   │   ├── info_leak.py
│   │   ├── path_discovery.py
│   │   ├── client_storage.py
│   │   ├── sri.py
│   │   ├── xss.py
│   │   ├── sqli.py
│   │   ├── command_injection.py
│   │   ├── ssti.py
│   │   ├── file_upload.py
│   │   ├── csrf.py
│   │   ├── tls.py
│   │   ├── ssrf.py
│   │   ├── open_redirect.py
│   │   ├── clickjacking.py
│   │   ├── jwt.py
│   │   ├── auth_bypass.py
│   │   ├── api_fuzzing.py
│   │   ├── dependency_vuln.py  # OSV.dev lookup
│   │   ├── race_condition.py
│   │   ├── idor.py
│   │   ├── performance.py      # Core Web Vitals
│   │   └── accessibility.py    # WCAG
```

## 25 Audit Modules

### Recon (7)
1. **security-headers** — Check CSP, X-Frame-Options, X-Content-Type-Options, Strict-Transport-Security, Referrer-Policy, Permissions-Policy
2. **cookies** — Analyze cookie flags: Secure, HttpOnly, SameSite
3. **cors** — Test CORS configuration, wildcard origins, credential leakage
4. **info-leak** — Check for exposed .git, .env, server headers, stack traces, debug endpoints
5. **path-discovery** — Probe common paths: /admin, /api, /.well-known, /swagger, etc.
6. **client-storage** — Inspect localStorage/sessionStorage for sensitive data
7. **sri** — Check Subresource Integrity on external scripts/styles

### Attack (6)
8. **xss** — Test reflected/stored XSS vectors in forms and URL parameters
9. **sqli** — Test SQL injection in input fields and URL parameters
10. **command-injection** — Test OS command injection patterns
11. **ssti** — Test Server-Side Template Injection (Jinja2, ERB, etc.)
12. **file-upload** — Test file upload for unrestricted types, path traversal
13. **csrf** — Check for CSRF token presence on state-changing forms

### Infra (6)
14. **tls** — Check TLS version, certificate validity, cipher suites
15. **ssrf** — Test for SSRF via URL parameters and webhooks
16. **open-redirect** — Test open redirect via redirect parameters
17. **clickjacking** — Test X-Frame-Options and frame-busting
18. **jwt** — Analyze JWT implementation: algorithm confusion, weak secrets
19. **auth-bypass** — Test authentication bypass patterns

### Deep (4)
20. **api-fuzzing** — Fuzz API endpoints with unexpected inputs
21. **dependency-vuln** — OSV.dev CVE lookup for frontend dependencies
22. **race-condition** — Test concurrent request race conditions
23. **idor** — Test Insecure Direct Object Reference

### Quality (2)
24. **performance** — Measure Core Web Vitals (LCP, FID, CLS)
25. **accessibility** — WCAG compliance checks

## Scan Profiles

```python
PROFILES = {
    "quick": {  # 8 passive modules
        "modules": ["security_headers", "cookies", "cors", "info_leak",
                     "client_storage", "sri", "tls", "clickjacking"]
    },
    "standard": {  # 15 modules
        "modules": [... all recon + key attack + key infra]
    },
    "full": {  # all 25
        "modules": [... all]
    }
}
```

## Report Export Formats

- **JSON** — structured findings
- **HTML** — styled report with severity indicators
- **SARIF** — GitHub Advanced Security compatible format

## `AgentCoordinator`

Multi-agent parallel scanner with 5 roles:
- `recon` — reconnaissance modules
- `attack` — attack modules
- `infra` — infrastructure modules
- `deep` — deep analysis modules
- `quality` — quality modules

Features:
- Configurable concurrency (default 3)
- Results merging and deduplication
- Each agent runs in separate browser context

## Baseline & False Positive Management

- `save_baseline(path)` — save current scan results as baseline
- `compare_baseline(path)` — compare new scan against baseline, report only new findings
- `apply_ignores(ignores)` — suppress findings matching category, severity, or titleContains

## Testing

- `tests/unit/test_security_auditor.py` — module registration, profile selection
- `tests/unit/test_security_modules.py` — individual module tests with mock responses
- `tests/unit/test_agent_coordinator.py` — parallel scan coordination
- Test SARIF export format
- Test baseline comparison

## Checklist

- [ ] All 25 modules implemented
- [ ] Scan profiles (quick/standard/full)
- [ ] SARIF/HTML/JSON report export
- [ ] Multi-agent parallel scanning
- [ ] Baseline comparison
- [ ] False positive suppression
- [ ] OSV.dev dependency vulnerability lookup
- [ ] All tests passing
- [ ] `ruff check` clean
