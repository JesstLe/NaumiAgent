"""Real semantic review Jobs, Sessions, ChatRuns and exact Artifact/Memory ACLs.

Source capture and model tokens are explicitly controlled transports. Production
Engine, Tool, journal, peer9 authority, structured validation and runtime receipts
are exercised; this suite does not claim provider or public-network acceptance.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
import sqlite3

import pytest

from naumi_agent.daemons.agent_jobs import AgentJobState
from naumi_agent.model.router import ModelRouter, StreamChunk, TokenUsage
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.owner_journal import canonical
from naumi_agent.personal.research_semantic import ResearchSemanticMixin, semantic_read_covers
from naumi_agent.personal.research_semantic_models import SemanticQuote
from naumi_agent.personal.research_tools import execute_research_operation
from tests.integration import test_personal_research_provenance as provenance_base
from tests.integration import test_personal_research_service as base

pytestmark = pytest.mark.usefixtures("runtime_payload_key")
DAILY_PRIVATE = "UNRELATED_DAILY_PRIVATE_NOT_FOR_REVIEW_5820"


@pytest.fixture
async def semantic_system(tmp_path, monkeypatch):
    async for system in base.system.__wrapped__(tmp_path, monkeypatch):
        system.engine.config.safety.permission_mode = "bypass"
        system.engine.config.safety.max_turns = 12
        system.engine.personal_teams.start = lambda: None
        system.engine.personal_collaborations.start = lambda: None
        system.engine.personal_memory.index = provenance_base.UnavailableSemanticIndex()
        system.reviewer = system.engine.personal_agents.create(
            request_id="semantic-reviewer",
            name="Mira",
            model_tier="fast",
            instructions="独立核对用户指定的确切资料；只提交完整结构化结论。",
            tool_names=["personal_research", "file_read", "web_fetch"],
        )
        system.model_calls = []
        system.mode = "complete"
        system.entered, system.release = asyncio.Event(), asyncio.Event()

        async def stream(router, **kwargs):
            messages = copy.deepcopy(kwargs["messages"])
            system.model_calls.append(messages)
            assert DAILY_PRIVATE not in json.dumps(messages, ensure_ascii=False)
            names = {item["function"]["name"] for item in kwargs.get("tools", [])}
            assert names == {"personal_research"}
            task = next(item["content"] for item in messages if item["role"] == "user")
            review_id = re.search(r"rsv_[a-f0-9]{32}", task).group()
            outputs = []
            for item in messages:
                if item["role"] != "tool":
                    continue
                try:
                    outputs.append(json.loads(item["content"]))
                except (ValueError, TypeError):
                    outputs.append({"error": item["content"]})
            if system.mode == "prose":
                yield StreamChunk(token="自然语言声称全部复核通过，但没有任何结构化提交。")
                yield StreamChunk(
                    finish_reason="stop", usage=TokenUsage(input_tokens=11, output_tokens=5)
                )
                return
            operation = None
            if not outputs:
                operation = {"operation": "semantic_sources", "id": review_id}
            elif outputs[-1].get("status") == "submitted" or outputs[-1].get("error"):
                yield StreamChunk(token="确切资料复核流程结束。")
                yield StreamChunk(
                    finish_reason="stop", usage=TokenUsage(input_tokens=11, output_tokens=5)
                )
                return
            else:
                catalog = outputs[0]
                source = catalog["sources"][0]
                pages = [item for item in outputs if "next_offset" in item]
                if (
                    not pages or pages[-1]["next_offset"] is not None
                ) and system.mode != "skip_source":
                    if system.mode == "gate" and not system.entered.is_set():
                        system.entered.set()
                        await system.release.wait()
                    offset = pages[-1]["next_offset"] if pages else 0
                    operation = {
                        "operation": "semantic_source",
                        "id": review_id,
                        "source_id": source["id"],
                        "source_revision": source["revision"],
                        "offset": offset,
                        "limit": 20000,
                        "request_id": f"read-{offset}",
                    }
                else:
                    body = (
                        system.capture.body
                        if system.mode == "skip_source"
                        else "".join(item["body"] for item in pages)
                    )
                    verdicts = []
                    for claim in catalog["notebook"]["claims"]:
                        quote = claim["citations"][0]["quote"] if claim["citations"] else ""
                        evidence = (
                            []
                            if not quote
                            else [
                                {
                                    "source_id": source["id"],
                                    "source_revision": source["revision"],
                                    "body_sha256": source["body_sha256"],
                                    "quote": quote,
                                    "start": body.index(quote)
                                    + (1 if system.mode == "bad_quote" else 0),
                                }
                            ]
                        )
                        verdicts.append(
                            {
                                "claim_id": claim["id"],
                                "verdict": "insufficient"
                                if claim["id"] == "share"
                                else "supported",
                                "reason": "保留原统计口径，按确切原文核对该声明。",
                                "evidence": evidence,
                            }
                        )
                    operation = {
                        "operation": "semantic_submit",
                        "id": review_id,
                        "request_id": "submit-original",
                        "summary": "来源支持原始金额；比例保留待核实。",
                        "verdicts": verdicts,
                    }
            yield StreamChunk(
                tool_call={
                    0: {
                        "id": "semantic-call-" + str(len(outputs)),
                        "type": "function",
                        "function": {
                            "name": "personal_research",
                            "arguments": json.dumps(operation, ensure_ascii=False),
                        },
                    }
                },
                finish_reason="tool_calls",
                usage=TokenUsage(input_tokens=13, output_tokens=7),
            )

        monkeypatch.setattr(ModelRouter, "stream", stream)
        yield system


async def call(system, operation, **fields):
    return await execute_research_operation(
        system.service, {"operation": operation, **fields}, trusted_channel="web"
    )


async def notebook(system, *, request="semantic-source", claims=True):
    created = await base.create(system, request_id=request + "-create")
    captured = await base.capture(system, created, request_id=request + "-capture")
    if not claims:
        return captured
    return await base.manual(
        system, "revise", request_id=request + "-revise", **base.fields(captured)
    )


async def preview(system, view):
    return await call(
        system,
        "semantic_preview",
        id=view["notebook"]["id"],
        source_session_id=system.engine._session.id,
        reviewer_agent_id=system.reviewer.id,
    )


def start_args(value, request_id="semantic-original-start"):
    return {
        "id": value["notebook_id"],
        "source_session_id": value["source_session_id"],
        "reviewer_agent_id": value["reviewer"]["id"],
        "expected_reviewer_version": value["reviewer"]["version"],
        "expected_revision": value["notebook_revision"],
        "expected_input_digest": value["input_digest"],
        "request_id": request_id,
    }


async def finish(system, review):
    collaboration = system.engine.personal_collaborations
    for _ in range(12):
        await collaboration.tick()
        if collaboration._active:
            await asyncio.wait_for(
                asyncio.gather(*tuple(collaboration._active.values()), return_exceptions=True),
                timeout=90,
            )
        value = await call(system, "semantic_get", id=review["id"])
        if value["execution_status"] in {"completed", "failed", "cancelled", "unknown"}:
            return value
    raise AssertionError("Original semantic review did not settle")


async def test_real_engine_semantic_review_has_original_job_run_and_exact_result(semantic_system):
    system = semantic_system
    view = await notebook(system)
    daily = await system.engine.personal_artifacts.publish(
        actor_id="main",
        request_id="daily-private",
        title="其他日常资料",
        kind="material",
        filename="daily.txt",
        content=DAILY_PRIVATE.encode(),
    )
    source_session = system.engine._session.id
    daily_ref = system.engine.personal_artifacts.reference(daily)
    await system.engine.personal_artifacts.bind_refs(source_session, "main", [daily_ref])
    initial_messages = copy.deepcopy(system.engine._session.messages)
    planned = await preview(system, view)
    assert planned["can_start"] and planned["memory_count"] == 0
    assert all(item["artifact_id"] != daily_ref["artifact_id"] for item in planned["materials"])
    review = await call(system, "semantic_start", **start_args(planned))
    replay = await call(system, "semantic_start", **start_args(planned))
    assert review["id"] == replay["id"] and review["session_id"] == replay["session_id"]
    final = await finish(system, review)
    assert final["execution_status"] == "completed", final
    assert final["basis_status"] == "current"
    result = final["result"]
    assert result["verification_scope"] == "provided_sources" and result["independently_reviewed"]
    assert len(result["verdicts"]) == len(view["notebook"]["claims"])
    income = next(item for item in result["verdicts"] if item["claim_id"] == "income")
    anchor = income["evidence"][0]
    assert anchor["start"] == base.BODY.index(base.QUOTE_INCOME)
    assert anchor["start_byte"] == len(base.BODY[: anchor["start"]].encode())
    assert anchor["end_byte"] - anchor["start_byte"] == len(anchor["quote"].encode())
    assert all(final[key] for key in ("collaboration_id", "job_id", "session_id", "run_id"))
    job = await system.engine.personal_collaborations.jobs.get(final["job_id"])
    assert job.state is AgentJobState.COMPLETED and job.request.tool_scope == ("personal_research",)
    run = await system.engine.chat_run_store.get_run(final["session_id"], final["run_id"])
    assert run is not None and run.status == "completed"
    assert (
        system.engine._session.id == source_session
        and system.engine._session.messages == initial_messages
    )
    assert await system.engine.personal_artifacts.session_refs(source_session, "main") == [
        daily_ref
    ]
    with pytest.raises(PersonalAgentError):
        await system.engine.personal_artifacts.get(
            actor_id=system.reviewer.id, artifact_id=daily_ref["artifact_id"], version=1
        )
    assert (await system.service.get(view["notebook"]["id"]))["review"][
        "semantic_verification"
    ] == "not_performed"
    response = await call(system, "semantic_request", request_id="semantic-original-start")
    assert response["status"] == "completed" and response["review"]["job_id"] == final["job_id"]
    assert (await call(system, "semantic_list", id=view["notebook"]["id"]))["reviews"] == [final]
    stored_review = await system.service._semantic_record(final["id"])
    packet = stored_review["packet_ref"]
    await system.engine.personal_artifacts.revoke(
        actor_id="main",
        grant_id=packet["grant_id"],
        expected_revision=packet["grant_revision"],
        request_id="revoke-completed-review-packet",
    )
    revoked = await call(system, "semantic_get", id=final["id"])
    assert revoked["execution_status"] == "completed"
    assert revoked["basis_status"] == "unavailable" and "result" not in revoked
    with pytest.raises(PersonalAgentError):
        await system.engine.personal_artifacts.get(
            actor_id="main",
            artifact_id=result["artifact_ref"]["artifact_id"],
            version=result["artifact_ref"]["version"],
        )


@pytest.mark.parametrize("mode", ["prose", "bad_quote", "skip_source"])
async def test_final_prose_and_invalid_quote_never_create_verified_result(semantic_system, mode):
    system = semantic_system
    system.mode = mode
    value = await preview(system, await notebook(system))
    started = await call(system, "semantic_start", **start_args(value))
    final = await finish(system, started)
    assert final["execution_status"] == "failed", final
    assert "result" not in final
    assert (
        await system.service._record("semantic_submission", "rss_" + final["id"][4:], missing=True)
        is None
    )


async def test_source_pages_keep_exact_late_unicode_quote_without_prefix_truncation(
    semantic_system,
):
    system = semantic_system
    system.capture.body = "前置正文😀。" * 4000 + base.BODY
    view = await notebook(system)
    started = await call(system, "semantic_start", **start_args(await preview(system, view)))
    final = await finish(system, started)
    assert final["execution_status"] == "completed", final
    income = next(item for item in final["result"]["verdicts"] if item["claim_id"] == "income")
    assert income["evidence"][0]["start"] > 20000
    calls = [
        json.loads(call["function"]["arguments"])
        for messages in system.model_calls
        for message in messages
        if message["role"] == "assistant"
        for call in message.get("tool_calls", [])
    ]
    assert any(
        item.get("operation") == "semantic_source" and item.get("offset", 0) >= 20000
        for item in calls
    )


async def test_preview_grant_revocation_changes_digest_and_does_not_reshare(semantic_system):
    system = semantic_system
    view = await notebook(system)
    source = view["sources"][0]["artifact_ref"]
    grant = await system.engine.personal_artifacts.share(
        actor_id="main",
        artifact_id=source["artifact_id"],
        version=source["version"],
        recipient_id=system.reviewer.id,
        request_id="existing-review-grant",
    )
    before = await preview(system, view)
    await system.engine.personal_artifacts.revoke(
        actor_id="main",
        grant_id=grant["grant_id"],
        expected_revision=grant["revision"],
        request_id="revoke-existing",
    )
    after = await preview(system, view)
    assert before["input_digest"] != after["input_digest"]
    with pytest.raises(PersonalAgentError) as error:
        await call(system, "semantic_start", **start_args(before))
    assert error.value.code == "source_stale"
    assert system.model_calls == []


async def test_new_source_after_admission_fences_original_job_without_replacement(semantic_system):
    system = semantic_system
    view = await notebook(system)
    started = await call(system, "semantic_start", **start_args(await preview(system, view)))
    await base.capture(system, view, request_id="new-source-after-admission")
    final = await finish(system, started)
    assert final["basis_status"] == "stale"
    assert final["execution_status"] in {"cancelled", "failed"}
    assert system.model_calls == []
    assert len(await system.engine.personal_collaborations.list()) == 1
    original = await call(system, "semantic_request", request_id="semantic-original-start")
    assert original["review"]["id"] == started["id"]


async def test_original_grant_revocation_cancels_or_fences_running_review(semantic_system):
    system = semantic_system
    system.mode = "gate"
    view = await notebook(system)
    started = await call(system, "semantic_start", **start_args(await preview(system, view)))
    await system.engine.personal_collaborations.tick()
    await asyncio.wait_for(system.entered.wait(), timeout=60)
    value = await system.service._semantic_record(started["id"])
    frozen = await system.service._semantic_input(value)
    target = frozen["recipient_refs"][0]
    await system.engine.personal_artifacts.revoke(
        actor_id="main",
        grant_id=target["grant_id"],
        expected_revision=target["grant_revision"],
        request_id="revoke-running-review",
    )
    system.release.set()
    final = await finish(system, started)
    assert final["basis_status"] == "unavailable"
    assert final["execution_status"] in {"cancelled", "failed"}
    assert "result" not in final
    assert len(await system.engine.personal_collaborations.list()) == 1


async def test_cancel_request_and_replay_preserve_original_collaboration_identity(semantic_system):
    system = semantic_system
    started = await call(
        system, "semantic_start", **start_args(await preview(system, await notebook(system)))
    )
    cancelled = await call(
        system, "semantic_cancel", id=started["id"], request_id="cancel-original"
    )
    await system.engine.personal_collaborations.tick()
    final = await call(system, "semantic_get", id=started["id"])
    assert final["execution_status"] == "cancelled"
    assert cancelled["collaboration_id"] == final["collaboration_id"] == started["collaboration_id"]
    assert system.model_calls == []
    receipt = await call(system, "semantic_request", request_id="cancel-original")
    assert receipt["status"] == "completed" and receipt["review"]["id"] == started["id"]
    reconciled = await call(
        system, "semantic_reconcile", id=started["id"], request_id="reconcile-original"
    )
    assert (
        reconciled["id"] == started["id"]
        and len(await system.engine.personal_collaborations.list()) == 1
    )


async def test_authoring_memory_is_frozen_shared_and_retains_live_grant(semantic_system):
    system = semantic_system
    note = await provenance_base.memory(system)
    await provenance_base.select_memories(system)
    view = await notebook(system)
    revised = await base.model(
        system, "revise", request_id="memory-grounded-revise", **provenance_base.empty_fields(view)
    )
    planned = await preview(system, revised)
    assert planned["memory_count"] == 1 and planned["can_start"]
    started = await call(system, "semantic_start", **start_args(planned))
    frozen = await system.service._semantic_input(
        await system.service._semantic_record(started["id"])
    )
    assert frozen["memory_pairs"][0]["original_ref"] == note["ref"]
    assert frozen["memory_pairs"][0]["recipient_ref"]["grant_id"]
    final = await finish(system, started)
    assert final["execution_status"] == "completed", final
    target = frozen["memory_pairs"][0]["recipient_ref"]
    await system.engine.personal_memory.dispatch(
        "revoke",
        {
            "request_id": "revoke-review-memory",
            "grant_id": target["grant_id"],
            "expected_revision": target["grant_revision"],
        },
        actor_id="main",
        trusted_channel="management_web",
    )
    removed = await call(system, "semantic_get", id=started["id"])
    assert removed["basis_status"] == "unavailable" and "result" not in removed


@pytest.mark.parametrize(
    "operation",
    [
        "semantic_preview",
        "semantic_get",
        "semantic_list",
        "semantic_start",
        "semantic_submit",
        "semantic_source",
    ],
)
async def test_unbound_model_cannot_use_human_or_reviewer_authority(semantic_system, operation):
    system = semantic_system
    notebook_id, review_id, source_id = "rq_" + "1" * 32, "rsv_" + "2" * 32, "rs_" + "3" * 32
    valid = {
        "semantic_preview": {
            "id": notebook_id,
            "source_session_id": system.engine._session.id,
            "reviewer_agent_id": system.reviewer.id,
        },
        "semantic_start": {
            "id": notebook_id,
            "source_session_id": system.engine._session.id,
            "reviewer_agent_id": system.reviewer.id,
            "expected_reviewer_version": 1,
            "expected_revision": 1,
            "expected_input_digest": "4" * 64,
            "request_id": "start",
        },
        "semantic_get": {"id": review_id},
        "semantic_list": {"id": notebook_id},
        "semantic_submit": {
            "id": review_id,
            "request_id": "submit",
            "summary": "原资料不足。",
            "verdicts": [
                {
                    "claim_id": "claim",
                    "verdict": "insufficient",
                    "reason": "原资料不足。",
                    "evidence": [],
                }
            ],
        },
        "semantic_source": {
            "id": review_id,
            "source_id": source_id,
            "source_revision": 1,
            "request_id": "read",
        },
    }
    with pytest.raises(PersonalAgentError) as caught:
        await execute_research_operation(
            system.service, {"operation": operation, **valid[operation]}, caller_actor="main"
        )
    assert caught.value.code == "scope_denied"
    assert system.model_calls == []


async def test_no_claims_preview_blocks_without_creating_agents_or_execution(semantic_system):
    system = semantic_system
    view = await notebook(system, claims=False)
    planned = await preview(system, view)
    assert not planned["can_start"] and "no_claims" in planned["blocking_codes"]
    assert len(system.engine.personal_agents.list()) == 1
    assert await system.engine.personal_collaborations.list() == []
    assert system.model_calls == []


def receipt(**updates):
    return {
        "review_id": "original-review",
        "input_digest": "1" * 64,
        "collaboration_id": "original-collaboration",
        "job_id": "original-job",
        "session_id": "original-session",
        "run_id": "original-run",
        "actor_id": "original-reviewer",
        "workspace_key": "original-workspace",
        "source_id": "original-source",
        "source_revision": 1,
        "body_sha256": "2" * 64,
        "offset": 0,
        "end": 10,
        "model_turn": 2,
        **updates,
    }


def covered(receipts, **updates):
    original = receipt()
    binding = {
        key: original[key]
        for key in (
            "review_id",
            "input_digest",
            "collaboration_id",
            "job_id",
            "session_id",
            "run_id",
            "actor_id",
            "workspace_key",
        )
    }
    return semantic_read_covers(
        receipts,
        **{
            "binding": binding,
            "source_id": "original-source",
            "source_revision": 1,
            "body_sha256": "2" * 64,
            "start": 5,
            "end": 15,
            "model_turn": 4,
            **updates,
        },
    )


@pytest.mark.parametrize(
    "mutation",
    [
        {"review_id": "other-review"},
        {"input_digest": "3" * 64},
        {"collaboration_id": "other-collaboration"},
        {"job_id": "other-job"},
        {"session_id": "other-session"},
        {"run_id": "other-run"},
        {"actor_id": "other-reviewer"},
        {"workspace_key": "other-workspace"},
        {"source_id": "other-source"},
        {"source_revision": 2},
        {"body_sha256": "4" * 64},
        {"model_turn": 4},
        {"model_turn": 5},
        {"model_turn": True},
    ],
)
def test_read_receipt_cannot_cross_identity_source_or_unseen_model_turn(mutation):
    assert not covered([receipt(offset=0, end=20, **mutation)])


def test_read_receipt_requires_prior_pages_to_cover_full_quote_without_gap():
    assert not covered([])
    assert not covered([receipt()])
    assert not covered([receipt(), receipt(offset=11, end=20, model_turn=3)])
    assert covered([receipt(), receipt(offset=10, end=20, model_turn=3)])
    assert covered([receipt(offset=0, end=20)])


def test_source_candidate_anchors_are_exact_unicode_locations_not_verdicts():
    source = {"id": "rs_" + "1" * 32, "revision": 1, "body_sha256": "2" * 64, "title": "确切原文"}
    value = {
        "notebook": {
            "claims": [
                {
                    "id": "claim",
                    "citations": [
                        {"source_id": source["id"], "source_revision": 1, "quote": "金额 USD 100"}
                    ],
                }
            ]
        }
    }
    body = "😀原文：金额 USD 100；金额 USD 100。"
    anchors = ResearchSemanticMixin._semantic_candidates(value, source, body, 0, len(body))
    assert len(anchors) == 2 and all(row["candidate"] is True for row in anchors)
    assert all("verdict" not in row for row in anchors)
    for anchor in anchors:
        assert body[anchor["start"] : anchor["end"]] == anchor["quote"]
        assert anchor["end_byte"] - anchor["start_byte"] == len(anchor["quote"].encode())
        evidence = anchor["submission_evidence"]
        assert set(evidence) == {"source_id", "source_revision", "body_sha256", "quote", "start"}
        assert SemanticQuote.model_validate(evidence).model_dump() == evidence
    assert ResearchSemanticMixin._semantic_candidates(value, source, body, 0, 6) == []
    value["notebook"]["claims"][0]["citations"][0]["quote"] = "金额\nUSD 100"
    body = "😀原文：金额\nUSD 100。"
    page = ResearchSemanticMixin._semantic_source_page(
        value, {"id": "review", "input_digest": "3" * 64}, source, body, 0, 20000
    )
    assert len(json.dumps(page, ensure_ascii=False)) < 12000
    evidence = page["citation_anchors"][0]["submission_evidence"]
    assert SemanticQuote.model_validate(evidence).quote == "金额\nUSD 100"


def test_large_catalog_pages_reassemble_exact_canonical_json_and_keep_end_boundaries():
    packet = {
        "claims": [{"id": str(index), "text": '😀原文"\\\n' * 400} for index in range(18)],
        "memories": [{"content": "明确保留的完整记忆" * 400}],
    }
    content = canonical(packet).encode("utf-8")
    review = {"id": "review", "input_digest": "1" * 64}
    text = content.decode("utf-8")
    offset, pieces = 0, []
    while True:
        page = ResearchSemanticMixin._semantic_catalog_page(review, content, offset, 10000)
        assert page["format"] == "canonical_json_page"
        assert len(json.dumps(page, ensure_ascii=False)) < 12000
        assert (
            page["offset"] == offset and page["body_sha256"] == hashlib.sha256(content).hexdigest()
        )
        assert page["end_byte"] - page["start_byte"] == len(page["packet_json"].encode())
        pieces.append(page["packet_json"])
        if page["next_offset"] is None:
            break
        assert page["next_offset"] > offset
        offset = page["next_offset"]
    assert "".join(pieces) == text and json.loads("".join(pieces)) == packet
    end = ResearchSemanticMixin._semantic_catalog_page(review, content, len(text), 10000)
    assert end["packet_json"] == "" and end["next_offset"] is None
    with pytest.raises(PersonalAgentError) as caught:
        ResearchSemanticMixin._semantic_catalog_page(review, content, len(text) + 1, 10000)
    assert caught.value.code == "invalid_input"
    small = {"notebook": {"claims": []}, "memories": []}
    assert (
        ResearchSemanticMixin._semantic_catalog_page(review, canonical(small).encode(), 0, 10000)
        == small
    )
    small_content = canonical(small).encode()
    first = ResearchSemanticMixin._semantic_catalog_page(review, small_content, 0, 1)
    assert first["format"] == "canonical_json_page" and len(first["packet_json"]) == 1
    assert first["next_offset"] == 1
    last = ResearchSemanticMixin._semantic_catalog_page(
        review, small_content, len(small_content) - 1, 1
    )
    assert len(last["packet_json"]) == 1 and last["next_offset"] is None
    empty = ResearchSemanticMixin._semantic_catalog_page(
        review, small_content, len(small_content), 1
    )
    assert empty["packet_json"] == "" and empty["next_offset"] is None


def test_source_json_pages_remain_inline_without_losing_escaped_text_or_offsets():
    value = {"notebook": {"claims": []}}
    review = {"id": "review", "input_digest": "1" * 64}
    source = {"id": "source", "revision": 1, "body_sha256": "2" * 64, "title": "确切原文"}
    text = '😀"\\\n' * 9000
    offset, pieces = 0, []
    while True:
        page = ResearchSemanticMixin._semantic_source_page(
            value, review, source, text, offset, 20000
        )
        assert len(json.dumps(page, ensure_ascii=False)) < 12000
        assert page["offset"] == offset
        assert page["end_byte"] - page["start_byte"] == len(page["body"].encode())
        pieces.append(page["body"])
        if page["next_offset"] is None:
            break
        assert page["next_offset"] > offset
        offset = page["next_offset"]
    assert "".join(pieces) == text


def test_model_request_identity_is_stable_within_review_and_separate_between_reviews():
    original = {"operation": "semantic_source", "id": "first-review", "request_id": "read-0"}
    first = ResearchSemanticMixin._semantic_tool_fields(original)
    assert ResearchSemanticMixin._semantic_tool_fields(original) == first
    second = ResearchSemanticMixin._semantic_tool_fields({**original, "id": "second-review"})
    assert first["request_id"] != second["request_id"]
    assert first["client_request_id"] == second["client_request_id"] == "read-0"
    assert len(first["request_id"]) <= 128


@pytest.mark.parametrize("kind", ["artifact", "memory"])
async def test_interrupted_prepare_never_replaces_revoked_original_share_with_new_grant(
    semantic_system, monkeypatch, kind
):
    system = semantic_system
    note = None
    if kind == "memory":
        note = await provenance_base.memory(system)
        await provenance_base.select_memories(system)
    view = await notebook(system)
    if note is not None:
        view = await base.model(
            system,
            "revise",
            request_id="interrupted-memory-authoring",
            **provenance_base.empty_fields(view),
        )
    planned = await preview(system, view)
    arguments = start_args(planned)
    original = system.service._semantic_put_once

    async def interrupted(record_kind, record):
        if record_kind == "semantic_input":
            raise OSError("controlled interruption after original share before input freeze")
        return await original(record_kind, record)

    monkeypatch.setattr(system.service, "_semantic_put_once", interrupted)
    with pytest.raises(OSError):
        await call(system, "semantic_start", **arguments)
    monkeypatch.setattr(system.service, "_semantic_put_once", original)
    if kind == "artifact":
        ref = view["sources"][0]["artifact_ref"]
        grants = await system.engine.personal_artifacts.grants(
            actor_id="main", artifact_id=ref["artifact_id"], version=ref["version"]
        )
        assert len(grants) == 1
        grant = grants[0]
        await system.engine.personal_artifacts.revoke(
            actor_id="main",
            grant_id=grant["grant_id"],
            expected_revision=grant["revision"],
            request_id="revoke-interrupted-artifact",
        )
        replacement = await system.engine.personal_artifacts.share(
            actor_id="main",
            artifact_id=ref["artifact_id"],
            version=ref["version"],
            recipient_id=system.reviewer.id,
            request_id="replacement-artifact-grant",
        )
    else:
        grants = (await system.engine.personal_memory.grants(note["memory_id"]))["grants"]
        assert len(grants) == 1
        grant = grants[0]
        await system.engine.personal_memory.dispatch(
            "revoke",
            {
                "request_id": "revoke-interrupted-memory",
                "grant_id": grant["grant_id"],
                "expected_revision": grant["revision"],
            },
            actor_id="main",
            trusted_channel="management_web",
        )
        replacement = await system.engine.personal_memory.dispatch(
            "share",
            {
                "request_id": "replacement-memory-grant",
                "memory_id": note["memory_id"],
                "expected_revision": note["revision"],
                "expected_content_sha256": note["content_sha256"],
                "recipient_actor_id": system.reviewer.id,
                "recipient_version": system.reviewer.version,
            },
            actor_id="main",
            trusted_channel="management_web",
        )
    assert replacement["grant_id"] != grant["grant_id"]
    with pytest.raises(PersonalAgentError) as caught:
        await call(system, "semantic_start", **arguments)
    assert caught.value.code in {"artifact_access_revoked", "memory_access_revoked", "not_found"}
    assert system.model_calls == [] and await system.engine.personal_collaborations.list() == []
    request = await call(system, "semantic_request", request_id=arguments["request_id"])
    assert request["status"] == "pending"


def business_snapshot(system):
    snapshot = {}
    for path in sorted(system.root.rglob("*.db")):
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
            tables = [
                row[0]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
            ]
            snapshot[str(path.relative_to(system.root))] = {
                table: sorted(
                    db.execute('SELECT * FROM "' + table.replace('"', '""') + '"').fetchall(),
                    key=repr,
                )
                for table in tables
            }
    return snapshot


async def interrupted_admission(system, monkeypatch):
    view = await notebook(system)
    planned = await preview(system, view)
    original = system.service._complete

    async def lost_acknowledgement(intent, callback):
        if intent["operation"] == "semantic_start":
            raise OSError(
                "controlled crash after exact collaboration admission before start receipt"
            )
        return await original(intent, callback)

    monkeypatch.setattr(system.service, "_complete", lost_acknowledgement)
    with pytest.raises(OSError):
        await call(system, "semantic_start", **start_args(planned))
    review_id = (
        "rsv_"
        + hashlib.sha256(
            canonical([system.service.workspace_key, "semantic-original-start"]).encode()
        ).hexdigest()[:32]
    )
    review = await system.service._semantic_record(review_id)
    plan = system.engine.personal_collaborations.store.find_request(
        system.service.workspace_key,
        review["source_session_id"],
        review["collaboration_request_id"],
    )
    assert plan is not None and system.model_calls == []
    return view, review, plan


@pytest.mark.parametrize("basis", ["stale", "unavailable"])
async def test_lost_start_acknowledgement_projects_original_admission_without_get_writes(
    semantic_system, monkeypatch, basis
):
    system = semantic_system
    view, review, plan = await interrupted_admission(system, monkeypatch)
    if basis == "stale":
        await base.manual(
            system,
            "revise",
            request_id="change-after-lost-ack",
            **provenance_base.empty_fields(view),
        )
    else:
        system.engine.personal_agents.update(
            system.reviewer.id, system.reviewer.version, status="paused"
        )

    async def forbidden_dispatch(*args, **kwargs):
        raise AssertionError("GET must not repair an admission by creating or dispatching work")

    monkeypatch.setattr(system.engine.personal_collaborations, "create", forbidden_dispatch)
    monkeypatch.setattr(system.engine.personal_collaborations, "tick", forbidden_dispatch)
    monkeypatch.setattr(system.engine.session_store, "bind_personal_execution", forbidden_dispatch)
    before = business_snapshot(system)
    result = await call(system, "semantic_request", request_id="semantic-original-start")
    assert result["status"] == "completed" and result["review"]["id"] == review["id"]
    assert result["review"]["collaboration_id"] == plan["id"]
    assert result["review"]["session_id"] == plan["steps"][0]["session_id"]
    assert result["review"]["job_id"] == result["review"]["run_id"] == ""
    assert result["review"]["basis_status"] == basis
    assert business_snapshot(system) == before and system.model_calls == []
    intent = await system.service._record(
        "intent", system.service._request_identity("main", "semantic-original-start")
    )
    assert intent["status"] == "pending", "Admission projection must not complete the intent"


@pytest.mark.parametrize("field", ["context", "step_task"])
async def test_pending_admission_projection_requires_the_exact_original_plan(
    semantic_system, monkeypatch, field
):
    system = semantic_system
    _view, _review, plan = await interrupted_admission(system, monkeypatch)
    with sqlite3.connect(system.engine.personal_collaborations.store.db_path) as db:
        raw = json.loads(
            db.execute("SELECT plan_json FROM collaborations WHERE id=?", (plan["id"],)).fetchone()[
                0
            ]
        )
        if field == "context":
            raw["context"] = "Not the accepted exact context"
        else:
            raw["steps"][0]["task"] = "Not the accepted exact task"
        db.execute(
            "UPDATE collaborations SET plan_json=? WHERE id=?", (json.dumps(raw), plan["id"])
        )
    before = business_snapshot(system)
    with pytest.raises(PersonalAgentError) as caught:
        await call(system, "semantic_request", request_id="semantic-original-start")
    assert caught.value.code == "binding_conflict"
    assert business_snapshot(system) == before and system.model_calls == []
