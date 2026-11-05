"""Mutual proofs reject port reuse, redirects and oversized or ambiguous responses."""

from __future__ import annotations

import gzip
import json
import re

import httpx
import pytest

from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.workspace_transport import (
    WorkspaceTransport,
    request_signature,
    response_signature,
)

SECRET = "a" * 64
ARGS = {"operation": "inspect", "grant_id": "wg_" + "1" * 32, "recipient": {"opaque": "unit"}}


@pytest.mark.parametrize(
    "case",
    [
        "valid",
        "impostor",
        "wrong_nonce",
        "redirect",
        "oversized",
        "duplicate",
        "wrong_media",
        "403",
        "409",
        "gzip",
    ],
)
async def test_transport_mutual_proof_and_response_limits(monkeypatch, case):
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.path == "/api/v1/personal/workspaces/federation"
        assert SECRET not in str(request.headers) and SECRET.encode() not in request.content
        proof = re.fullmatch(
            r"NaumiWorkspace ([a-f0-9]{32}):([0-9]+):([a-f0-9]{64})",
            request.headers["authorization"],
        )
        assert proof
        nonce, stamp, signature = proof.groups()
        assert signature == request_signature(SECRET, nonce, stamp, json.loads(request.content), [])
        body = {"source": {}, "grant": {}, "body": {}, "read_ref": None}
        signature = response_signature(SECRET, nonce, body)
        if case == "impostor":
            signature = "0" * 64
        if case == "wrong_nonce":
            nonce = "0" * 32
        result = {**body, "proof": {"nonce": nonce, "signature": signature}}
        if case == "redirect":
            return httpx.Response(307, headers={"location": "http://127.0.0.1:9999/private"})
        if case in {"403", "409"}:
            return httpx.Response(int(case), json={"secret": "must-not-be-exposed"})
        if case == "oversized":
            return httpx.Response(
                200,
                content=b"x" * (2 * 1024 * 1024 + 1),
                headers={"content-type": "application/json"},
            )
        if case == "gzip":
            assert request.headers["accept-encoding"] == "identity"
            return httpx.Response(
                200,
                content=gzip.compress(b"x" * 4096),
                headers={"content-type": "application/json", "content-encoding": "gzip"},
            )
        if case == "duplicate":
            return httpx.Response(
                200,
                content=b'{"source":{},"source":{}}',
                headers={"content-type": "application/json"},
            )
        if case == "wrong_media":
            return httpx.Response(
                200, content=json.dumps(result), headers={"content-type": "text/html"}
            )
        return httpx.Response(200, json=result)

    original = httpx.AsyncClient

    def client(*args, **kwargs):
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
        return original(*args, **kwargs, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(httpx, "AsyncClient", client)
    if case == "valid":
        assert (await WorkspaceTransport().request("http://127.0.0.1:9998", SECRET, ARGS, []))[
            "body"
        ] == {}
    else:
        with pytest.raises(PersonalAgentError) as exc:
            await WorkspaceTransport().request("http://127.0.0.1:9998", SECRET, ARGS, [])
        assert "must-not-be-exposed" not in str(exc.value)
        expected = {
            "impostor": "source_stale",
            "wrong_nonce": "source_stale",
            "oversized": "input_limit",
            "403": "grant_revoked",
            "409": "source_stale",
            "gzip": "input_limit",
        }
        if case in expected:
            assert exc.value.code == expected[case]
    assert len(requests) == 1
