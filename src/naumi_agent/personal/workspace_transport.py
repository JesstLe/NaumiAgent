"""Bounded capability-only HTTP reads; never discover, start or redirect runtimes."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
import time
from contextvars import ContextVar

import httpx

from naumi_agent.personal.artifact_models import strict_json_loads
from naumi_agent.personal.item_models import canonical
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.workspace_models import MAX_REQUEST_BYTES, endpoint_url, error

TRACE = ContextVar("personal_workspace_read_trace", default=())
PATH = "/api/v1/personal/workspaces/federation"


def request_signature(secret, nonce, stamp, arguments, trace):
    message = "\0".join(
        ("NaumiWorkspace.Request.v1", nonce, stamp, PATH, ",".join(trace), canonical(arguments))
    ).encode()
    return hmac.new(bytes.fromhex(secret), message, hashlib.sha256).hexdigest()


def response_signature(secret, nonce, body):
    message = ("NaumiWorkspace.Response.v1\0" + nonce + "\0" + canonical(body)).encode()
    return hmac.new(bytes.fromhex(secret), message, hashlib.sha256).hexdigest()


class WorkspaceTransport:
    def __init__(self):
        self._semaphore = asyncio.Semaphore(4)

    async def request(self, endpoint, secret, arguments, trace):
        endpoint = endpoint_url(endpoint)
        content = canonical(arguments).encode("utf-8")
        if len(content) > MAX_REQUEST_BYTES:
            raise error("invalid_input")
        nonce, stamp = secrets.token_hex(16), str(int(time.time()))
        signature = request_signature(secret, nonce, stamp, arguments, trace)
        try:
            async with asyncio.timeout(10):
                async with self._semaphore:
                    async with httpx.AsyncClient(
                        trust_env=False,
                        follow_redirects=False,
                        timeout=httpx.Timeout(5),
                        limits=httpx.Limits(max_connections=4),
                    ) as client:
                        async with client.stream(
                            "POST",
                            endpoint + PATH,
                            content=content,
                            headers={
                                "Authorization": f"NaumiWorkspace {nonce}:{stamp}:{signature}",
                                "Content-Type": "application/json",
                                "Accept-Encoding": "identity",
                                "X-Naumi-Workspace-Trace": ",".join(trace),
                            },
                        ) as response:
                            if response.status_code in {401, 403}:
                                raise error("grant_revoked")
                            if response.status_code == 409:
                                raise error("source_stale")
                            if response.status_code != 200:
                                raise error()
                            encodings = response.headers.get_list("content-encoding")
                            if encodings and encodings != ["identity"]:
                                raise error("input_limit")
                            if (
                                response.headers.get("content-type", "").split(";", 1)[0]
                                != "application/json"
                            ):
                                raise error()
                            chunks, length = [], 0
                            async for chunk in response.aiter_bytes():
                                length += len(chunk)
                                if length > 2 * 1024 * 1024:
                                    raise error("input_limit")
                                chunks.append(chunk)
            result = strict_json_loads(b"".join(chunks))
            if type(result) is not dict:
                raise error()
            proof = result.pop("proof", None)
            if (
                type(proof) is not dict
                or set(proof) != {"nonce", "signature"}
                or proof["nonce"] != nonce
                or type(proof["signature"]) is not str
                or not hmac.compare_digest(
                    response_signature(secret, nonce, result), proof["signature"]
                )
            ):
                raise error("source_stale")
            return result
        except PersonalAgentError:
            raise
        except (TimeoutError, httpx.HTTPError, ValueError, UnicodeError, RecursionError):
            raise error() from None
