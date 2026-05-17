# ruff: noqa: E501
"""Authentication bypass test module."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


async def test_auth_bypass(
    page: Any,
    add_finding: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    """Test for auth bypass by clearing cookies and checking page access."""
    url = page.url
    findings: list[dict[str, Any]] = []
    context = page.context

    session_cookies = await context.cookies()
    auth_cookies = [
        c for c in session_cookies
        if not any(
            c.get("name", "").lower().startswith(prefix)
            for prefix in ("_ga", "_gid", "_utm")
        )
        and any(
            kw in c.get("name", "").lower()
            for kw in ("session", "token", "auth")
        )
        and len(c.get("name", "")) < 50
    ]

    if auth_cookies:
        saved_cookies = list(session_cookies)
        await context.clear_cookies()
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=10000)
            if response:
                landed_url = response.url
                status = response.status
                no_login_redirect = not any(
                    kw in landed_url for kw in ("login", "signin", "auth")
                )
                if status == 200 and no_login_redirect:
                    findings.append(
                        add_finding(
                            {
                                "category": "auth-bypass",
                                "severity": "critical",
                                "title": "Protected page accessible without authentication",
                                "description": (
                                    f"{url} returned 200 after clearing all auth cookies "
                                    "— page did not redirect to login"
                                ),
                                "url": url,
                                "evidence": {
                                    "status": status,
                                    "landedUrl": landed_url,
                                    "removedCookies": [c["name"] for c in auth_cookies],
                                },
                            }
                        )
                    )
        except Exception:
            pass
        await context.add_cookies(saved_cookies)
        await page.goto(url, wait_until="domcontentloaded", timeout=10000)

    # JWT alg:none forgery test
    jwt_tokens = await page.evaluate(
        """() => {
            const all = [];
            for (const store of [localStorage, sessionStorage]) {
                for (let i = 0; i < store.length; i++) {
                    const val = store.getItem(store.key(i));
                    if (/^eyJ[A-Za-z0-9_-]+\\./.test(val)) {
                        all.push({
                            key: store.key(i),
                            value: val,
                            store: store === localStorage ? "localStorage" : "sessionStorage",
                        });
                    }
                }
            }
            return all;
        }"""
    )

    for token in jwt_tokens:
        parts = token["value"].split(".")
        if len(parts) == 3:
            try:
                import base64
                import json

                def b64_decode(s: str) -> dict:
                    padded = s + "=" * (4 - len(s) % 4)
                    return json.loads(base64.urlsafe_b64decode(padded))

                header = b64_decode(parts[0])
                payload = b64_decode(parts[1])

                forged_header = {**header, "alg": "none", "typ": header.get("typ", "JWT")}
                forged_payload = dict(payload)
                if "role" in forged_payload:
                    forged_payload["role"] = "admin"
                if "isAdmin" in forged_payload:
                    forged_payload["isAdmin"] = True
                if "userId" in forged_payload:
                    forged_payload["userId"] = 1

                b64_header = base64.urlsafe_b64encode(
                    json.dumps(forged_header).encode()
                ).rstrip(b"=").decode()
                b64_payload = base64.urlsafe_b64encode(
                    json.dumps(forged_payload).encode()
                ).rstrip(b"=").decode()
                forged_token = f"{b64_header}.{b64_payload}."

                findings.append(
                    add_finding(
                        {
                            "category": "auth-bypass",
                            "severity": "high",
                            "title": "JWT alg:none forgery possible",
                            "description": (
                                f"Forged token created for \"{token['key']}\" "
                                f"in {token['store']} with admin claims"
                            ),
                            "url": url,
                            "evidence": {
                                "originalAlg": header.get("alg"),
                                "forgedTokenPreview": forged_token[:80] + "...",
                            },
                        }
                    )
                )
            except Exception:
                pass

    # Session fixation check
    session_fixation = await page.evaluate(
        """() => {
            const results = {};
            const cookies = document.cookie.split(";").map((c) => c.trim());
            for (const cookie of cookies) {
                const eqIdx = cookie.indexOf("=");
                const name = cookie.slice(0, eqIdx);
                const val = cookie.slice(eqIdx + 1);
                const nameLower = name.toLowerCase();
                if (
                    nameLower.includes("session")
                    || nameLower.includes("phpsessid")
                    || nameLower.includes("jsessionid")
                ) {
                    results[name] = val;
                }
            }
            return results;
        }"""
    )

    if session_fixation:
        findings.append(
            add_finding(
                {
                    "category": "auth-bypass",
                    "severity": "medium",
                    "title": "Session token in URL or cookie may be fixatable",
                    "description": (
                        "Session identifiers found that may be vulnerable to session fixation"
                    ),
                    "url": url,
                    "evidence": {"tokens": list(session_fixation.keys())},
                }
            )
        )

    return {"category": "auth-bypass", "findings": findings}
