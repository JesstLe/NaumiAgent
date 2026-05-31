"""Network event recording from Playwright context.

Ported from browser-debugging-daemon/scripts/runtime/NetworkRecorder.js.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_MAX_ENTRIES = 500

_SENSITIVE_HEADERS = frozenset(["authorization", "cookie", "set-cookie"])


class NetworkRecorder:
    def __init__(self, *, max_entries: int = DEFAULT_MAX_ENTRIES) -> None:
        self.entries: list[dict[str, Any]] = []
        self.max_entries = max_entries
        self.enabled = False

    def attach(self, context: Any) -> None:
        if context is None:
            return
        self.detach()
        self.enabled = True

        context.on("request", self._on_request)
        context.on("response", self._on_response)
        context.on("requestfailed", self._on_request_failed)

    def detach(self) -> None:
        self.enabled = False

    def clear(self) -> None:
        self.entries.clear()

    def get_summary(self) -> dict[str, Any]:
        by_type: dict[str, int] = {}
        status_groups = {"2xx": 0, "3xx": 0, "4xx": 0, "5xx": 0, "other": 0}
        failed = 0

        for entry in self.entries:
            if entry.get("type") == "requestFailed":
                failed += 1
                continue
            if entry.get("type") == "response":
                code = entry.get("status", 0) or 0
                if 200 <= code < 300:
                    status_groups["2xx"] += 1
                elif 300 <= code < 400:
                    status_groups["3xx"] += 1
                elif 400 <= code < 500:
                    status_groups["4xx"] += 1
                elif 500 <= code < 600:
                    status_groups["5xx"] += 1
                else:
                    status_groups["other"] += 1
            rt = entry.get("resourceType", "unknown")
            by_type[rt] = by_type.get(rt, 0) + 1

        return {
            "totalRequests": sum(
                1 for e in self.entries if e.get("type") == "request"
            ),
            "totalResponses": sum(
                1 for e in self.entries if e.get("type") == "response"
            ),
            "failed": failed,
            "statusGroups": status_groups,
            "byResourceType": by_type,
        }

    # -- Event handlers --

    def _on_request(self, request: Any) -> None:
        if not self.enabled:
            return
        self._push({
            "type": "request",
            "url": request.url,
            "method": request.method,
            "resourceType": request.resource_type,
            "headers": _sanitize_headers(request.headers),
            "postData": request.post_data or None,
            "timestamp": datetime.now().isoformat(),
        })

    def _on_response(self, response: Any) -> None:
        if not self.enabled:
            return
        request = response.request
        self._push({
            "type": "response",
            "url": request.url,
            "status": response.status,
            "statusText": response.status_text,
            "headers": _sanitize_headers(response.headers),
            "timestamp": datetime.now().isoformat(),
        })

    def _on_request_failed(self, request: Any) -> None:
        if not self.enabled:
            return
        failure = request.failure
        failure_text = failure.error_text if failure else "Unknown error"
        self._push({
            "type": "requestFailed",
            "url": request.url,
            "failure": failure_text,
            "resourceType": request.resource_type,
            "timestamp": datetime.now().isoformat(),
        })

    def _push(self, entry: dict[str, Any]) -> None:
        self.entries.append(entry)
        while len(self.entries) > self.max_entries:
            self.entries.pop(0)


def _sanitize_headers(headers: dict[str, str] | None) -> dict[str, str]:
    if not headers:
        return {}
    result: dict[str, str] = {}
    for key, value in headers.items():
        if key.lower() in _SENSITIVE_HEADERS:
            result[key] = "[redacted]"
        else:
            result[key] = value
    return result
