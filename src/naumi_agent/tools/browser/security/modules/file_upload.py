# ruff: noqa: E501
"""File upload security bypass test module."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


async def test_file_upload_bypass(
    page: Any,
    add_finding: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    """Test file upload fields for missing type restrictions and dangerous accept values."""
    url = page.url
    findings: list[dict[str, Any]] = []

    upload_fields = await page.evaluate(
        """() => {
            return Array.from(document.querySelectorAll("input[type='file']")).map((el) => ({
                id: el.id || "",
                name: el.name || "",
                accept: el.accept || "",
                multiple: el.multiple,
            }));
        }"""
    )

    if not upload_fields:
        return {"category": "file-upload", "findings": findings, "message": "No file upload fields found"}

    for field in upload_fields:
        accept = field.get("accept", "")
        if not accept:
            findings.append(
                add_finding(
                    {
                        "category": "file-upload",
                        "severity": "medium",
                        "title": "File upload without type restriction",
                        "description": (
                            f"Upload field \"{field.get('id') or field.get('name')}\" "
                            "has no accept attribute, may allow any file type"
                        ),
                        "url": url,
                        "evidence": {"field": field},
                    }
                )
            )
        else:
            allowed = accept.lower()
            dangerous_types = [".php", ".jsp", ".asp", ".aspx", ".exe", ".sh", ".bat", ".cmd", ".ps1"]
            for ext in dangerous_types:
                if ext in allowed:
                    findings.append(
                        add_finding(
                            {
                                "category": "file-upload",
                                "severity": "high",
                                "title": "Dangerous file type allowed",
                                "description": f"Upload field accepts {ext} files",
                                "url": url,
                                "evidence": {
                                    "field": field,
                                    "allowed": accept,
                                    "dangerous": ext,
                                },
                            }
                        )
                    )

        selector = f"#{field['id']}" if field.get("id") else f"[name=\"{field['name']}\"]"
        form = await page.evaluate(
            """(selector) => {
                const el = document.querySelector(selector);
                const form = el?.closest("form");
                return form
                    ? { action: form.action, method: form.method, enctype: form.enctype }
                    : null;
            }""",
            selector,
        )

        if form and "urlencoded" in (form.get("enctype") or ""):
            findings.append(
                add_finding(
                    {
                        "category": "file-upload",
                        "severity": "low",
                        "title": "File upload with wrong enctype",
                        "description": (
                            "Form with file input uses application/x-www-form-urlencoded "
                            "instead of multipart/form-data"
                        ),
                        "url": url,
                        "evidence": {"enctype": form["enctype"]},
                    }
                )
            )

    return {"category": "file-upload", "findings": findings}
