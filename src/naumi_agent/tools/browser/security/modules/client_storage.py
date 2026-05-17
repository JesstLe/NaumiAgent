# ruff: noqa: E501
"""Client-side storage security scan module."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


async def scan_client_storage(
    page: Any,
    add_finding: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    """Scan localStorage, sessionStorage, and cookies for sensitive data."""
    url = page.url
    findings: list[dict[str, Any]] = []

    storage_data = await page.evaluate(
        """() => {
            const sensitiveKeyPatterns = [
                "password", "secret", "token", "api_key", "apikey", "auth",
                "credential", "private", "session", "ssn", "credit", "card",
            ];
            const sensitiveValuePatterns = [
                /eyJ[A-Za-z0-9_-]*\\.eyJ/,
                /sk[_-]/,
                /pk[_-]/,
                /\\b\\d{16}\\b/,
                /\\b\\d{3}-\\d{2}-\\d{4}\\b/,
                /-----BEGIN (RSA |EC )?PRIVATE KEY-----/,
            ];

            const checkEntry = (key, value, store) => {
                if (!value || typeof value !== "string") return null;
                const keyLower = key.toLowerCase();
                const isSensitiveKey = sensitiveKeyPatterns.some((p) => keyLower.includes(p));
                const isSensitiveValue = sensitiveValuePatterns.some((p) => p.test(value));
                if (isSensitiveKey || isSensitiveValue) {
                    return {
                        key,
                        store,
                        reason: isSensitiveKey ? "sensitive-key" : "sensitive-value",
                        valuePreview: value.slice(0, 80),
                    };
                }
                return null;
            };

            const results = [];

            for (let i = 0; i < localStorage.length; i++) {
                const key = localStorage.key(i);
                const value = localStorage.getItem(key);
                const found = checkEntry(key, value, "localStorage");
                if (found) results.push(found);
            }

            for (let i = 0; i < sessionStorage.length; i++) {
                const key = sessionStorage.key(i);
                const value = sessionStorage.getItem(key);
                const found = checkEntry(key, value, "sessionStorage");
                if (found) results.push(found);
            }

            const cookies = document.cookie.split(";").map((c) => c.trim()).filter(Boolean);
            for (const cookie of cookies) {
                const eqIdx = cookie.indexOf("=");
                const key = cookie.slice(0, eqIdx).trim();
                const value = cookie.slice(eqIdx + 1);
                const found = checkEntry(key, value, "cookie");
                if (found) results.push(found);
            }

            const indexedDBCheck = typeof indexedDB !== "undefined"
                ? "available" : "unavailable";
            results.push({
                info: true,
                indexedDB: indexedDBCheck,
                localStorageEntries: localStorage.length,
                sessionStorageEntries: sessionStorage.length,
                cookieCount: cookies.length,
            });

            return results;
        }"""
    )

    sensitive_entries = [d for d in storage_data if not d.get("info")]
    for entry in sensitive_entries:
        key_lower = (entry.get("key") or "").lower()
        severity = (
            "critical"
            if "password" in key_lower or "private" in key_lower
            else "high"
        )
        findings.append(
            add_finding(
                {
                    "category": "client-storage",
                    "severity": severity,
                    "title": f"Sensitive data in {entry['store']}",
                    "description": f"\"{entry['key']}\" found in {entry['store']} ({entry['reason']})",
                    "url": url,
                    "evidence": {
                        "key": entry["key"],
                        "store": entry["store"],
                        "reason": entry["reason"],
                        "preview": entry.get("valuePreview"),
                    },
                }
            )
        )

    info = next((d for d in storage_data if d.get("info")), {})
    return {
        "category": "client-storage",
        "findings": findings,
        "storageStats": {
            "localStorage": info.get("localStorageEntries", 0),
            "sessionStorage": info.get("sessionStorageEntries", 0),
            "cookies": info.get("cookieCount", 0),
            "indexedDB": info.get("indexedDB", "unavailable"),
        },
    }
