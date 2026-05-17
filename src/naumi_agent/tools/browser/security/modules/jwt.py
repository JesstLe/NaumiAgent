# ruff: noqa: E501
"""JWT (JSON Web Token) analysis module."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC
from typing import Any


async def test_jwt(
    page: Any,
    add_finding: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    """Detect and analyze JWT tokens for common vulnerabilities."""
    url = page.url
    findings: list[dict[str, Any]] = []

    jwt_analysis = await page.evaluate(
        """() => {
            const results = [];
            const sources = [
                document.cookie,
                localStorage.getItem("token") || localStorage.getItem("jwt")
                    || localStorage.getItem("access_token") || "",
                sessionStorage.getItem("token") || sessionStorage.getItem("jwt")
                    || sessionStorage.getItem("access_token") || "",
            ];
            const allSources = sources.join(" ");
            const jwtPattern = /eyJ[A-Za-z0-9_-]+\\.eyJ[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+/g;
            const tokens = allSources.match(jwtPattern) || [];

            for (const token of tokens) {
                const parts = token.split(".");
                if (parts.length !== 3) continue;
                try {
                    const header = JSON.parse(atob(parts[0].replace(/-/g, "+").replace(/_/g, "/")));
                    const payload = JSON.parse(atob(parts[1].replace(/-/g, "+").replace(/_/g, "/")));
                    results.push({ header, payload, tokenPreview: token.slice(0, 50) + "..." });
                } catch {
                    results.push({ decodeError: true, tokenPreview: token.slice(0, 50) });
                }
            }

            const scripts = Array.from(document.querySelectorAll("script:not([src])"));
            for (const script of scripts) {
                const match = script.textContent?.match(jwtPattern);
                if (match) {
                    for (const t of match) {
                        try {
                            const parts = t.split(".");
                            const header = JSON.parse(atob(parts[0].replace(/-/g, "+").replace(/_/g, "/")));
                            results.push({
                                header,
                                payload: null,
                                source: "inline-script",
                                tokenPreview: t.slice(0, 50) + "...",
                            });
                        } catch {}
                    }
                }
            }
            return results;
        }"""
    )

    import time

    for analysis in jwt_analysis:
        if analysis.get("decodeError"):
            findings.append(
                add_finding(
                    {
                        "category": "jwt",
                        "severity": "medium",
                        "title": "JWT token found but malformed",
                        "description": "A JWT-like token was found but could not be decoded",
                        "url": url,
                        "evidence": {"tokenPreview": analysis.get("tokenPreview")},
                    }
                )
            )
            continue

        header = analysis.get("header")
        if header:
            alg = header.get("alg", "")
            if alg == "none":
                findings.append(
                    add_finding(
                        {
                            "category": "jwt",
                            "severity": "critical",
                            "title": "JWT with 'none' algorithm",
                            "description": "JWT token uses the 'none' algorithm, allowing signature bypass",
                            "url": url,
                            "evidence": {"header": header},
                        }
                    )
                )

            if alg.startswith("HS"):
                findings.append(
                    add_finding(
                        {
                            "category": "jwt",
                            "severity": "medium",
                            "title": "JWT uses symmetric signing",
                            "description": f"JWT uses {alg} — if secret is weak, token can be forged",
                            "url": url,
                            "evidence": {"algorithm": alg},
                        }
                    )
                )

            if alg.startswith("RS") and "kid" not in header:
                findings.append(
                    add_finding(
                        {
                            "category": "jwt",
                            "severity": "low",
                            "title": "JWT RSA without key ID",
                            "description": "RSA-signed JWT has no 'kid' field, key rotation may be difficult",
                            "url": url,
                            "evidence": {"header": header},
                        }
                    )
                )

            typ = header.get("typ")
            if typ and typ != "JWT":
                findings.append(
                    add_finding(
                        {
                            "category": "jwt",
                            "severity": "low",
                            "title": "JWT with unusual typ claim",
                            "description": f'JWT typ is "{typ}", expected "JWT"',
                            "url": url,
                            "evidence": {"typ": typ},
                        }
                    )
                )

        payload = analysis.get("payload")
        if payload:
            exp = payload.get("exp")
            if not exp:
                findings.append(
                    add_finding(
                        {
                            "category": "jwt",
                            "severity": "medium",
                            "title": "JWT without expiration",
                            "description": "JWT payload has no 'exp' claim — token never expires",
                            "url": url,
                            "evidence": {"hasExp": False},
                        }
                    )
                )
            elif exp < time.time():
                from datetime import datetime
                expired_at = datetime.fromtimestamp(exp, tz=UTC).isoformat()
                findings.append(
                    add_finding(
                        {
                            "category": "jwt",
                            "severity": "info",
                            "title": "Expired JWT token found",
                            "description": f"Token expired at {expired_at}",
                            "url": url,
                            "evidence": {"expiredAt": expired_at},
                        }
                    )
                )

            if "iat" not in payload:
                findings.append(
                    add_finding(
                        {
                            "category": "jwt",
                            "severity": "low",
                            "title": "JWT without issued-at",
                            "description": "JWT payload has no 'iat' claim — cannot determine token age",
                            "url": url,
                            "evidence": {},
                        }
                    )
                )

        if analysis.get("source") == "inline-script":
            findings.append(
                add_finding(
                    {
                        "category": "jwt",
                        "severity": "high",
                        "title": "JWT exposed in inline script",
                        "description": "JWT token found embedded in inline JavaScript, accessible to XSS",
                        "url": url,
                        "evidence": {"tokenPreview": analysis.get("tokenPreview")},
                    }
                )
            )

    if not jwt_analysis:
        return {"category": "jwt", "findings": findings, "message": "No JWT tokens found on page"}

    return {"category": "jwt", "findings": findings, "tokensAnalyzed": len(jwt_analysis)}
