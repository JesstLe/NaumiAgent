"""Real research journals/artifacts/identity with explicit fixture transport receipts."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import keyring
import pytest

from naumi_agent.config.settings import AppConfig, ModelConfig
from naumi_agent.model.router import ModelRouter
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.research import PersonalResearchService
from naumi_agent.runtime.composition import create_agent_engine

pytestmark = pytest.mark.usefixtures("runtime_payload_key")
BODY = "😀收入 USD 100；成本 USD 25。PRIVATE_SOURCE_7391"
QUOTE_INCOME = "收入 USD 100"
QUOTE_COST = "成本 USD 25"
URL = "https://research.example.test/report"


class Clock:
    def __init__(self):
        self.value = datetime(2026, 11, 6, 10, tzinfo=UTC)

    def __call__(self):
        return self.value


class CaptureFixture:
    """Inject only transport results; service/artifact/authority code remains real."""

    def __init__(self, clock):
        self.clock, self.calls, self.body = clock, [], BODY
        self.error = None
        self.entered = asyncio.Event()
        self.release = None

    async def __call__(self, url):
        self.calls.append(url)
        self.entered.set()
        if self.release is not None:
            await self.release.wait()
        if self.error is not None:
            raise self.error
        checksum = hashlib.sha256(self.body.encode()).hexdigest()
        return {
            "requested_url": url,
            "final_url": url,
            "title": "原网页资料",
            "body": self.body,
            "raw_sha256": checksum,
            "body_sha256": checksum,
            "http_status": 200,
            "captured_at": self.clock().isoformat(),
            "truncated": False,
            "content_type": "text/plain; charset=utf-8",
            "extractor": "fixture-transport-v1",
            "redirects": [],
        }


class DiscoveryFixture:
    def __init__(self, clock):
        self.clock, self.calls = clock, []

    async def __call__(self, tool, query, max_results=5):
        self.calls.append((tool.name, query, max_results))
        return {
            "query": query,
            "provider": "fixture-only",
            "status": "success",
            "code": "",
            "items": [{"title": "发现候选", "url": URL, "snippet": "这只是检索摘要，不是正文证据"}],
            "observed_at": self.clock().isoformat(),
            "attempts": ["fixture-only"],
        }


def configuration(root, personal=None):
    workspace = root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    return AppConfig(
        workspace_root=str(workspace),
        personal={"data_dir": str(personal or root / "personal")},
        memory={
            "session_db_path": str(root / "runtime/sessions.db"),
            "vector_db_path": str(root / "runtime/vectors"),
            "long_term_enabled": False,
        },
        models=ModelConfig(
            default_model="openai/gpt-4o-mini",
            fast_model="openai/gpt-4o-mini",
            reasoning_model="openai/gpt-4o-mini",
            api_key="research-fixture-only",
            api_base="http://127.0.0.1:1/v1",
            max_tokens=64,
        ),
        safety={"permission_mode": "strict", "allowed_dirs": [str(workspace)]},
        skills={"search_paths": []},
    )


@pytest.fixture
async def system(tmp_path, monkeypatch):
    monkeypatch.setenv("NAUMI_STATE_HOME", str(tmp_path / "state-home"))
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    provider_calls, credential_calls = [], []

    async def no_call(*_args, **_kwargs):
        provider_calls.append("call")
        raise AssertionError("Research service fixtures never call a provider")

    async def no_stream(*_args, **_kwargs):
        provider_calls.append("stream")
        raise AssertionError("Research service fixtures never call a provider")
        yield

    def no_credential(*_args, **_kwargs):
        credential_calls.append("native credential access")
        raise AssertionError("Research fixtures never read or write real credentials")

    monkeypatch.setattr(ModelRouter, "call", no_call)
    monkeypatch.setattr(ModelRouter, "stream", no_stream)
    for method in ("get_password", "set_password", "delete_password"):
        monkeypatch.setattr(keyring, method, no_credential)
    engine = create_agent_engine(configuration(tmp_path))
    service = engine.personal_research
    assert isinstance(service, PersonalResearchService)
    clock = Clock()
    capture, discovery = CaptureFixture(clock), DiscoveryFixture(clock)
    service.clock = clock
    service.capture_transport, service.discovery_transport = capture, discovery
    await engine.get_or_create_session(title="研究服务真实存储验收")
    try:
        yield SimpleNamespace(
            engine=engine,
            service=service,
            clock=clock,
            capture=capture,
            discovery=discovery,
            root=tmp_path,
        )
    finally:
        await engine.shutdown()
        assert provider_calls == [] and credential_calls == []


async def manual(system, operation, **fields):
    return await system.service.execute_operation(
        {"operation": operation, **fields}, trusted_channel="web"
    )


async def model(system, operation, *, actor="main", **fields):
    return await system.service.execute_operation(
        {"operation": operation, **fields}, caller_actor=actor
    )


async def create(system, request_id="research-original-create", title="原比例研究"):
    return await manual(
        system,
        "create",
        request_id=request_id,
        title=title,
        question="原资料的成本占收入比例是否正确？",
        domain="operations",
    )


async def capture(system, view, request_id="research-original-capture", **fields):
    return await manual(
        system,
        "capture",
        request_id=request_id,
        id=view["notebook"]["id"],
        url=URL,
        max_age_days=1,
        **fields,
    )


def fields(view, *, expected="25", title=None):
    notebook = view["notebook"]
    original = view["sources"][0]
    citations = [
        {
            "source_id": original["id"],
            "source_revision": original["revision"],
            "quote": quote,
            "occurrence": 0,
        }
        for quote in (QUOTE_INCOME, QUOTE_COST)
    ]
    base = {"unit": "currency", "currency": "USD", "period": "2026Q3"}
    claims = [
        {
            "id": "income",
            "kind": "fact",
            "text": "原收入100美元",
            "citations": citations[:1],
            "quantity": {"value": "100", **base},
        },
        {
            "id": "cost",
            "kind": "fact",
            "text": "原成本25美元",
            "citations": citations[1:],
            "quantity": {"value": "25", **base},
        },
        {
            "id": "share",
            "kind": "fact",
            "text": "成本占收入比例",
            "citations": citations,
            "quantity": {
                "value": expected,
                "unit": "percent",
                "currency": None,
                "period": "2026Q3",
            },
            "calculation": {
                "operation": "percent",
                "inputs": ["cost", "income"],
                "expected": expected,
                "tolerance": "0.000001",
            },
        },
    ]
    return {
        "id": notebook["id"],
        "expected_revision": notebook["revision"],
        "title": title or notebook["title"],
        "question": notebook["question"],
        "domain": notebook["domain"],
        "claims": claims,
        "conclusion": {
            "stance": "watch",
            "premises": ["原统计口径一致"],
            "invalidators": ["原来源内容或统计时段变化时重新检查"],
        },
    }


async def checked(system, view, request_id="research-original-review"):
    return await manual(
        system,
        "review",
        id=view["notebook"]["id"],
        expected_revision=view["notebook"]["revision"],
        request_id=request_id,
    )


async def published(system, view, request_id="research-original-publication"):
    return await manual(
        system,
        "publish",
        id=view["notebook"]["id"],
        expected_revision=view["notebook"]["revision"],
        expected_review_digest=view["review"]["digest"],
        request_id=request_id,
    )


async def report_body(system, reference):
    return await system.engine.personal_artifacts.read(
        actor_id="main", artifact_id=reference["artifact_id"], version=reference["version"]
    )


def finding_codes(view):
    return {finding["code"] for finding in view["review"]["findings"]}


async def pinned_material(system, arguments, body):
    artifacts = system.engine.personal_artifacts
    session_id = system.engine._session.id
    before = await artifacts.session_refs(session_id, "main")
    result = await system.service.model_result(arguments, body, "main")
    after = await artifacts.session_refs(session_id, "main")
    added = [reference for reference in after if reference not in before]
    assert len(added) == 1
    snapshot, content = await report_body(system, added[0])
    records = [
        await system.service._record("read", ref["read_id"], revision=ref["revision"])
        for ref in snapshot["evidence"]["research_read_refs"]
    ]
    return result, snapshot, json.loads(content), records


async def test_actual_service_wrong_ratio_correct_revision_and_two_report_versions(system):
    created = await create(system)
    assert created["notebook"]["revision"] == 1 and created["review"]["status"] == "not_reviewed"
    captured = await capture(system, created)
    source = captured["sources"][0]
    source_view = await manual(system, "source", id=source["id"])
    assert source_view["body"] == BODY and source["origin"] == "url"
    assert source["body_sha256"] == hashlib.sha256(BODY.encode()).hexdigest()
    wrong = await manual(
        system, "revise", request_id="wrong-ratio", **fields(captured, expected="20")
    )
    wrong = await checked(system, wrong, "wrong-ratio-review")
    assert wrong["review"]["status"] == "issues_found"
    assert "calculation_mismatch" in finding_codes(wrong)
    assert wrong["review"]["calculations"][0]["computed"] == "25"
    corrected = await manual(system, "revise", request_id="correct-ratio", **fields(wrong))
    corrected = await checked(system, corrected, "correct-ratio-review")
    assert corrected["review"]["status"] == "mechanically_checked"
    assert corrected["review"]["semantic_verification"] == "not_performed"
    first = await published(system, corrected)
    first_ref = first["publication"]
    assert first_ref["version"] == 1
    first_snapshot, first_content = await report_body(system, first_ref)
    assert "25" in first_content.decode() and "语义独立核实：未执行" in first_content.decode()
    with sqlite3.connect(system.engine.personal_artifacts.store.db_path) as db:
        raw = json.loads(
            db.execute(
                "SELECT record_json FROM artifact_versions WHERE artifact_id=? AND version=1",
                (first_ref["artifact_id"],),
            ).fetchone()[0]
        )
    assert raw["authority_version"] == 6
    assert first_snapshot["evidence"]["research_read_refs"]
    revised = await manual(
        system,
        "revise",
        request_id="revise-report-title",
        **fields(first, title="修订后的比例研究"),
    )
    revised = await checked(system, revised, "review-revised-report")
    second = await published(system, revised, "publish-revised-report")
    second_ref = second["publication"]
    assert second_ref["artifact_id"] == first_ref["artifact_id"] and second_ref["version"] == 2
    _, second_content = await report_body(system, second_ref)
    _, reopened_first = await report_body(system, first_ref)
    assert reopened_first == first_content and second_content != first_content
    assert "修订后的比例研究" in second_content.decode()
    replayed_publication = await published(system, corrected)
    assert replayed_publication["publication"] == first_ref
    original_publication = await manual(
        system, "request", request_id="research-original-publication"
    )
    assert original_publication["view"]["publication"] == first_ref
    historical = await manual(
        system, "get", id=created["notebook"]["id"], revision=wrong["notebook"]["revision"]
    )
    assert historical["historical"] is True
    assert historical["notebook"]["claims"][-1]["calculation"]["expected"] == "20"
    history = await manual(system, "history", id=created["notebook"]["id"])
    assert [row["revision"] for row in history["history"]] == [4, 3, 2, 1]
    assert system.capture.calls == [URL]


async def test_reports_with_findings_are_explicit_drafts_not_truth_passes(system):
    captured = await capture(system, await create(system))
    wrong = await manual(
        system, "revise", request_id="wrong-publication", **fields(captured, expected="20")
    )
    wrong = await checked(system, wrong)
    published_view = await published(system, wrong)
    _, body = await report_body(system, published_view["publication"])
    text = body.decode()
    assert "issues_found" in text and "calculation_mismatch" in text
    assert "语义独立核实：未执行" in text


async def test_request_replay_reads_original_id_and_never_refetches_capture(system):
    original = await create(system)
    repeated = await create(system)
    assert repeated["notebook"] == original["notebook"]
    captured = await capture(system, original)
    replayed = await capture(system, original)
    assert replayed["sources"] == captured["sources"] and system.capture.calls == [URL]
    request = await manual(system, "request", request_id="research-original-capture")
    assert request["status"] == "completed"
    assert request["view"]["notebook"]["id"] == original["notebook"]["id"]
    unknown = await manual(system, "request", request_id="not-observed-request")
    assert unknown == {"status": "unknown", "request_id": "not-observed-request"}
    with pytest.raises(PersonalAgentError) as caught:
        await create(system, title="原请求的不同标题")
    assert caught.value.code == "idempotency_conflict"


async def test_exact_revision_and_review_digest_are_required_before_publish(system):
    captured = await capture(system, await create(system))
    revised = await manual(system, "revise", request_id="first-revision", **fields(captured))
    with pytest.raises(PersonalAgentError) as caught:
        await manual(system, "revise", request_id="stale-revision", **fields(captured))
    assert caught.value.code == "version_conflict"
    with pytest.raises(PersonalAgentError) as caught:
        await published(system, revised, "no-current-review")
    assert caught.value.code == "source_stale"
    reviewed = await checked(system, revised)
    with pytest.raises(PersonalAgentError) as caught:
        await manual(
            system,
            "publish",
            id=revised["notebook"]["id"],
            expected_revision=revised["notebook"]["revision"],
            expected_review_digest="f" * 64,
            request_id="wrong-review-digest",
        )
    assert caught.value.code == "source_stale"
    valid = await published(system, reviewed, "correct-review-digest")
    assert valid["publication"]["version"] == 1


async def test_discovery_is_actual_transport_output_not_captured_evidence(system):
    created = await create(system)
    result = await manual(
        system,
        "search",
        id=created["notebook"]["id"],
        request_id="search-original",
        query="公开报告成本比例",
        max_results=3,
    )
    assert result["discovery"]["evidence_kind"] == "discovery_only"
    assert result["discovery"]["items"][0]["snippet"] == "这只是检索摘要，不是正文证据"
    assert result["view"]["sources"] == []
    repeated = await manual(
        system,
        "search",
        id=created["notebook"]["id"],
        request_id="search-original",
        query="公开报告成本比例",
        max_results=3,
    )
    assert repeated["discovery"] == result["discovery"] and len(system.discovery.calls) == 1


async def test_pending_duplicate_capture_has_one_transport_and_original_result(system):
    created = await create(system)
    system.capture.release = asyncio.Event()
    task = asyncio.create_task(capture(system, created))
    try:
        await asyncio.wait_for(system.capture.entered.wait(), 5)
        with pytest.raises(PersonalAgentError) as caught:
            await capture(system, created)
        assert caught.value.code == "request_pending"
        assert system.capture.calls == [URL]
        pending = await manual(system, "request", request_id="research-original-capture")
        assert pending["status"] == "unknown"
        system.capture.release.set()
        original = await asyncio.wait_for(task, 10)
        replay = await capture(system, created)
        assert replay["sources"] == original["sources"] and system.capture.calls == [URL]
    finally:
        system.capture.release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_cancelled_capture_is_unknown_and_never_automatically_refetched(system):
    created = await create(system)
    system.capture.release = asyncio.Event()
    task = asyncio.create_task(capture(system, created))
    try:
        await asyncio.wait_for(system.capture.entered.wait(), 10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        system.capture.release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    with pytest.raises(PersonalAgentError) as caught:
        await capture(system, created)
    assert caught.value.code == "request_pending"
    assert system.capture.calls == [URL]
    original_request = await manual(system, "request", request_id="research-original-capture")
    assert original_request["status"] == "unknown"


async def test_failed_capture_and_404_recheck_preserve_original_source_and_flags(system):
    captured = await capture(system, await create(system))
    revised = await manual(system, "revise", request_id="before-failed-recheck", **fields(captured))
    revised = await checked(system, revised)
    source = revised["sources"][0]
    system.capture.error = PersonalAgentError("source_unavailable", "本机传输夹具返回失效链接")
    failed = await manual(
        system,
        "recheck",
        source_id=source["id"],
        expected_revision=1,
        request_id="recheck-original-failure",
    )
    assert failed["sources"][0]["id"] == source["id"]
    assert failed["sources"][0]["revision"] == 2 and failed["sources"][0]["status"] == "unavailable"
    assert "source_unavailable" in finding_codes(failed)
    assert failed["review"]["status"] == "not_reviewed"
    historical = await manual(system, "source", id=source["id"], revision=1)
    assert historical["body"] == BODY and historical["source"]["revision"] == 1
    repeated = await manual(
        system,
        "recheck",
        source_id=source["id"],
        expected_revision=1,
        request_id="recheck-original-failure",
    )
    assert repeated["sources"][0]["revision"] == 2 and system.capture.calls == [URL, URL]
    created = await create(system, "new-unavailable-research")
    unavailable = await capture(system, created, "first-capture-failure")
    assert unavailable["sources"][0]["artifact_ref"] is None
    assert unavailable["sources"][0]["status"] == "unavailable"
    unavailable_source = await manual(system, "source", id=unavailable["sources"][0]["id"])
    assert unavailable_source["availability"] == "available"
    assert unavailable_source["body"] is None
    assert unavailable_source["source"]["status"] == "unavailable"
    assert unavailable_source["source"]["url"] == URL


async def test_expiry_invalidates_old_review_without_inventing_a_new_capture(system):
    captured = await capture(system, await create(system))
    revised = await manual(system, "revise", request_id="expiring-research", **fields(captured))
    reviewed = await checked(system, revised)
    original_digest = reviewed["review"]["digest"]
    published_view = await published(system, reviewed)
    _, original_report = await report_body(system, published_view["publication"])
    await system.service.model_result(
        {"operation": "get", "id": reviewed["notebook"]["id"]}, published_view, "main"
    )
    await system.engine.personal_artifacts.authorize_engine()
    system.clock.value += timedelta(days=2)
    current = await manual(system, "get", id=reviewed["notebook"]["id"])
    assert current["sources"][0]["expired"] is True
    assert "source_expired" in finding_codes(current)
    assert current["review"]["status"] == "not_reviewed"
    assert current["review"]["digest"] != original_digest
    assert system.capture.calls == [URL]
    with pytest.raises(PersonalAgentError) as caught:
        await system.engine.personal_artifacts.authorize_engine()
    assert caught.value.code in {"source_stale", "source_unavailable"}
    _, historical_report = await report_body(system, published_view["publication"])
    assert historical_report == original_report


async def test_two_revision_writers_have_one_real_service_cas_winner(system):
    captured = await capture(system, await create(system))
    first = fields(captured, title="并发修订一")
    second = fields(captured, title="并发修订二")
    results = await asyncio.gather(
        manual(system, "revise", request_id="concurrent-revision-one", **first),
        manual(system, "revise", request_id="concurrent-revision-two", **second),
        return_exceptions=True,
    )
    successes = [value for value in results if isinstance(value, dict)]
    errors = [value for value in results if isinstance(value, PersonalAgentError)]
    assert len(successes) == len(errors) == 1 and errors[0].code == "version_conflict"
    current = await manual(system, "get", id=captured["notebook"]["id"])
    assert current["notebook"]["revision"] == 2
    assert current["notebook"]["title"] == successes[0]["notebook"]["title"]
    history = await manual(system, "history", id=captured["notebook"]["id"])
    assert [row["revision"] for row in history["history"]] == [2, 1]


async def test_actor_workspace_and_opaque_request_scopes_do_not_overlap(system):
    created = await create(system)
    profile = system.engine.personal_agents.create(
        request_id="explicit-research-agent",
        name="Mira",
        instructions="仅处理用户明确交付的资料。",
        tool_names=[],
    )
    with pytest.raises(PersonalAgentError) as caught:
        await model(system, "get", actor=profile.id, id=created["notebook"]["id"])
    assert caught.value.code == "not_found"
    other_request = await model(
        system, "request", actor=profile.id, request_id="research-original-create"
    )
    assert other_request["status"] == "unknown"
    other_config = configuration(system.root / "other-workspace", personal=system.root / "personal")
    other_engine = create_agent_engine(other_config)
    try:
        other = other_engine.personal_research
        with pytest.raises(PersonalAgentError) as caught:
            await other.execute_operation(
                {"operation": "get", "id": created["notebook"]["id"]}, trusted_channel="web"
            )
        assert caught.value.code == "not_found"
        assert (await other.list(caller_actor="main"))["notebooks"] == []
        different = await other.execute_operation(
            {
                "operation": "create",
                "request_id": "research-original-create",
                "title": "另一个工作区",
                "question": "不同上下文",
                "domain": "general",
            },
            trusted_channel="web",
        )
        assert different["notebook"]["id"] != created["notebook"]["id"]
        original_list = await system.service.list(caller_actor="main")
        assert original_list["notebooks"][0]["id"] == created["notebook"]["id"]
    finally:
        await other_engine.shutdown()


async def test_private_model_context_cannot_send_new_queries_or_urls_or_forge_human_channel(system):
    created = await create(system)
    private = await system.engine.personal_artifacts.publish(
        actor_id="main",
        request_id="private-material",
        title="私有资料",
        kind="material",
        filename="private.txt",
        content=b"PRIVATE_NETWORK_SECRET",
    )
    await system.engine.personal_artifacts.bind_refs(
        system.engine._session.id, "main", [system.engine.personal_artifacts.reference(private)]
    )
    for operation, arguments in (
        ("capture", {"id": created["notebook"]["id"], "url": URL, "max_age_days": 1}),
        (
            "search",
            {"id": created["notebook"]["id"], "query": "PRIVATE_NETWORK_SECRET", "max_results": 1},
        ),
    ):
        with pytest.raises(PersonalAgentError) as caught:
            await model(system, operation, request_id="private-network-" + operation, **arguments)
        assert caught.value.code == "artifact_network_denied"
    with pytest.raises(PersonalAgentError) as caught:
        await system.service.execute_operation(
            {
                "operation": "capture",
                "request_id": "forged-human",
                "id": created["notebook"]["id"],
                "url": URL,
                "max_age_days": 1,
            },
            caller_actor="main",
            trusted_channel="web",
        )
    assert caught.value.code == "scope_denied"
    assert system.capture.calls == [] and system.discovery.calls == []
    explicit = await capture(system, created, "explicit-human-network")
    assert explicit["sources"] and system.capture.calls == [URL]


async def test_model_snapshot_can_revise_same_notebook_without_self_bricking(system):
    created = await model(
        system,
        "create",
        request_id="model-original-create",
        title="模型研究",
        question="检查原比例",
        domain="general",
    )
    await system.service.model_result(
        {"operation": "create", "request_id": "model-original-create"}, created, "main"
    )
    # A prior empty-source snapshot must not become a mutable self-head dependency.
    captured = await manual(
        system,
        "capture",
        request_id="human-first-source",
        id=created["notebook"]["id"],
        url=URL,
        max_age_days=1,
    )
    await system.service.model_result(
        {"operation": "get", "id": created["notebook"]["id"]}, captured, "main"
    )
    revised = await model(
        system, "revise", request_id="model-revise-own-notebook", **fields(captured)
    )
    assert revised["availability"] == "available" and revised["notebook"]["claims"]
    await system.service.model_result(
        {"operation": "revise", "id": revised["notebook"]["id"]}, revised, "main"
    )
    await system.engine.personal_artifacts.authorize_engine()
    original_refs = await system.engine.personal_artifacts.session_refs(
        system.engine._session.id, "main"
    )
    assert original_refs
    # Adding another explicit source cannot silently rewrite an old body/as-of snapshot.
    await manual(
        system,
        "capture",
        id=revised["notebook"]["id"],
        request_id="human-add-source",
        url="https://research.example.test/second",
        max_age_days=1,
    )
    await system.engine.personal_artifacts.authorize_engine()
    source = captured["sources"][0]
    await manual(
        system,
        "recheck",
        source_id=source["id"],
        expected_revision=1,
        request_id="change-original-source-observation",
    )
    with pytest.raises(PersonalAgentError) as caught:
        await system.engine.personal_artifacts.authorize_engine()
    assert caught.value.code in {"source_stale", "source_unavailable"}


async def test_original_source_grant_revocation_masks_views_requests_history_and_derivatives(
    system,
):
    owner = system.engine.personal_agents.create(
        request_id="explicit-private-source-owner",
        name="Atlas",
        instructions="保留原资料权限。",
        tool_names=[],
    )
    original = await system.engine.personal_artifacts.publish(
        actor_id=owner.id,
        request_id="owner-private-original",
        title="PRIVATE_TITLE_7391",
        kind="material",
        filename="private-source.txt",
        content=BODY.encode(),
    )
    grant = await system.engine.personal_artifacts.share(
        actor_id=owner.id,
        artifact_id=original["artifact_id"],
        version=original["version"],
        recipient_id="main",
        request_id="share-exact-original",
    )
    granted = await system.engine.personal_artifacts.get(
        actor_id="main", artifact_id=original["artifact_id"], version=original["version"]
    )
    created = await create(system, title="PRIVATE_NOTEBOOK_TITLE_7391")
    imported = await manual(
        system,
        "import",
        id=created["notebook"]["id"],
        request_id="import-private-original",
        artifact_ref=system.engine.personal_artifacts.reference(granted),
        max_age_days=1,
    )
    assert imported["sources"][0]["upstream_unverified"] is True
    revised = await manual(
        system, "revise", request_id="private-source-revision", **fields(imported)
    )
    reviewed = await checked(system, revised)
    assert "upstream_unverified" in finding_codes(reviewed)
    published_view = await published(system, reviewed)
    report_ref = published_view["publication"]
    _, report = await report_body(system, report_ref)
    report_text = report.decode()
    assert QUOTE_INCOME in report_text and QUOTE_COST in report_text
    assert "PRIVATE_NOTEBOOK_TITLE_7391" in report_text
    # Publication exports chosen anchors, not the uncited remainder of a source.
    assert "PRIVATE_SOURCE_7391" not in report_text
    await system.service.model_result(
        {"operation": "get", "id": revised["notebook"]["id"]}, published_view, "main"
    )
    derived = await system.engine.personal_artifacts.publish(
        actor_id="main",
        request_id="private-derived-original",
        title="派生结论",
        kind="output",
        filename="derived.md",
        content=b"# Derived private result\n",
        source_refs=[report_ref],
    )
    await system.engine.personal_artifacts.authorize_engine()
    await system.engine.personal_artifacts.revoke(
        actor_id=owner.id,
        grant_id=grant["grant_id"],
        expected_revision=grant["revision"],
        request_id="revoke-private-original",
    )
    current = await manual(system, "get", id=revised["notebook"]["id"])
    replay = await create(system, title="PRIVATE_NOTEBOOK_TITLE_7391")
    request = await manual(system, "request", request_id="import-private-original")
    history = await manual(system, "history", id=revised["notebook"]["id"])
    raw = json.dumps([current, replay, request, history], ensure_ascii=False)
    assert all(
        marker not in raw
        for marker in (
            "PRIVATE_TITLE_7391",
            "PRIVATE_NOTEBOOK_TITLE_7391",
            "PRIVATE_SOURCE_7391",
            QUOTE_INCOME,
            QUOTE_COST,
        )
    )
    assert current["availability"] == "source_unavailable" and current["notebook"]["claims"] == []
    source_view = await manual(system, "source", id=imported["sources"][0]["id"])
    assert source_view["body"] is None
    for reference in (report_ref, system.engine.personal_artifacts.reference(derived)):
        with pytest.raises(PersonalAgentError):
            await report_body(system, reference)
    with pytest.raises(PersonalAgentError):
        await system.engine.personal_artifacts.authorize_engine()


async def test_cross_notebook_upstream_unknown_never_acquires_an_independent_verification(system):
    captured = await capture(system, await create(system, "upstream-create"))
    upstream = fields(captured)
    upstream["claims"][0]["kind"] = "unknown"
    first = await manual(system, "revise", request_id="upstream-unknown", **upstream)
    second = await create(system, "downstream-create", "下游研究")
    downstream = {
        "id": second["notebook"]["id"],
        "expected_revision": 1,
        "request_id": "downstream-original",
        "title": "下游研究",
        "question": "上游证据是否独立？",
        "domain": "general",
        "claims": [
            {
                "id": "transferred",
                "text": "仅转述上游未知",
                "kind": "unknown",
                "upstream_claims": [
                    {
                        "notebook_id": first["notebook"]["id"],
                        "revision": first["notebook"]["revision"],
                        "claim_id": "income",
                    }
                ],
            }
        ],
        "conclusion": {"stance": "undecided", "premises": [], "invalidators": []},
    }
    revised = await manual(system, "revise", **downstream)
    reviewed = await checked(system, revised)
    assert {"unknown_claim", "upstream_unverified"} <= finding_codes(reviewed)
    assert reviewed["review"]["claim_states"][0]["kind"] == "unknown"
    assert reviewed["review"]["claim_states"][0]["independently_verified"] is False


async def test_model_historical_source_read_pins_original_body_and_exact_artifact(system):
    captured = await capture(system, await create(system))
    original = captured["sources"][0]
    system.capture.body = BODY.replace("100", "101")
    rechecked = await manual(
        system,
        "recheck",
        source_id=original["id"],
        expected_revision=1,
        request_id="source-new-observation",
    )
    assert rechecked["sources"][0]["revision"] == 2
    arguments = {"operation": "source", "id": original["id"], "revision": 1}
    historical = await model(system, "source", id=original["id"], revision=1)
    assert historical["source"]["revision"] == 1 and historical["body"] == BODY
    result, material, content, records = await pinned_material(system, arguments, historical)
    assert result == historical and content == historical
    assert content["source"]["revision"] == 1 and content["body"] == BODY
    assert original["artifact_ref"] in material["source_refs"]
    assert len(records) == 1 and original["artifact_ref"] in records[0]["source_refs"]
    assert any(
        source["id"] == original["id"] and source["revision"] == 1
        for source in records[0]["basis"]["sources"]
    )
    await system.engine.personal_artifacts.authorize_engine()
    system.capture.body = BODY.replace("100", "102")
    await manual(
        system,
        "recheck",
        source_id=original["id"],
        expected_revision=2,
        request_id="source-third-observation",
    )
    with pytest.raises(PersonalAgentError) as caught:
        await system.engine.personal_artifacts.authorize_engine()
    assert caught.value.code in {"source_stale", "source_unavailable"}


async def test_model_completed_revise_replay_and_request_pin_original_notebook_revision(system):
    captured = await capture(system, await create(system))
    original_fields = fields(captured, expected="20")
    original_args = {
        "operation": "revise",
        "request_id": "original-wrong-revise",
        **original_fields,
    }
    wrong = await manual(system, "revise", request_id="original-wrong-revise", **original_fields)
    current = await manual(system, "revise", request_id="later-correct-revise", **fields(wrong))
    assert current["notebook"]["revision"] == 3
    replay = await model(system, "revise", request_id="original-wrong-revise", **original_fields)
    assert replay["notebook"]["revision"] == 2
    _, _, content, records = await pinned_material(system, original_args, replay)
    assert len(records) == 1 and records[0]["notebook_revision"] == 2
    assert content["notebook"]["revision"] == 2
    assert content["notebook"]["claims"][-1]["calculation"]["expected"] == "20"
    assert content["notebook"]["claims"][0]["citations"][0]["source_revision"] == 1
    request_args = {"operation": "request", "request_id": "original-wrong-revise"}
    request = await model(system, "request", request_id="original-wrong-revise")
    assert request["status"] == "completed" and request["view"]["notebook"]["revision"] == 2
    _, _, content, records = await pinned_material(system, request_args, request)
    assert len(records) == 1 and records[0]["notebook_revision"] == 2
    assert content["view"]["notebook"]["revision"] == 2
    assert content["view"]["notebook"]["claims"][0]["citations"][0]["source_revision"] == 1
    await system.engine.personal_artifacts.authorize_engine()


async def test_model_history_material_proves_each_returned_revision_not_latest_substitutes(system):
    captured = await capture(system, await create(system))
    wrong = await manual(
        system, "revise", request_id="history-wrong-revise", **fields(captured, expected="20")
    )
    corrected = await manual(system, "revise", request_id="history-correct-revise", **fields(wrong))
    arguments = {"operation": "history", "id": corrected["notebook"]["id"]}
    history = await model(system, "history", id=corrected["notebook"]["id"])
    returned_revisions = [entry["revision"] for entry in history["history"]]
    assert returned_revisions == [3, 2, 1]
    _, material, content, records = await pinned_material(system, arguments, history)
    assert content == history
    assert len(material["evidence"]["research_read_refs"]) == len(returned_revisions)
    assert sorted(record["notebook_revision"] for record in records) == sorted(returned_revisions)
    assert {record["notebook_id"] for record in records} == {corrected["notebook"]["id"]}
    await system.engine.personal_artifacts.authorize_engine()


async def test_actor_filtered_keyset_list_scans_beyond_one_thousand_foreign_records(system):
    foreign = system.engine.personal_agents.create(
        request_id="explicit-page-fixture-agent",
        name="Mira",
        instructions="仅处理用户明确交付的资料。",
        tool_names=[],
    )
    stamp = system.service.stamp()

    def insert(tx):
        for index in range(1, 1008):
            identity = f"rq_{index:032x}"
            tx.put(
                "notebook",
                {
                    "id": identity,
                    "revision": 1,
                    "notebook_id": identity,
                    "workspace_key": system.service.workspace_key,
                    "actor_id": foreign.id if index <= 1005 else "main",
                    "status": "active",
                    "title": f"分页夹具 {index}",
                    "question": "只按原身份分页",
                    "domain": "general",
                    "claims": [],
                    "conclusion": {"stance": "undecided", "premises": [], "invalidators": []},
                    "created_at": stamp,
                    "updated_at": stamp,
                    "context_refs": [],
                },
                expected_revision=0,
            )

    # One authenticated journal transaction seeds the pagination boundary.
    await system.service._write(insert)
    first = await system.service.list(limit=1, caller_actor="main")
    assert [item["id"] for item in first["notebooks"]] == [f"rq_{1006:032x}"]
    assert first["next_after"] == f"rq_{1006:032x}"
    second = await system.service.list(limit=1, after=first["next_after"], caller_actor="main")
    assert [item["id"] for item in second["notebooks"]] == [f"rq_{1007:032x}"]
    assert second["next_after"] is None


async def test_late_quantity_conflict_propagates_through_nested_calculation_inputs(system):
    system.capture.body = BODY + "；收入 USD 101；基准 25%。"
    captured = await capture(system, await create(system))
    values = fields(captured)
    income = values["claims"][0]
    income["subject"] = "营业收入"
    original = captured["sources"][0]
    values["claims"].extend(
        [
            {
                "id": "income_alternate",
                "kind": "fact",
                "text": "同期间另一处收入101美元",
                "subject": "营业收入",
                "citations": [
                    {
                        "source_id": original["id"],
                        "source_revision": 1,
                        "quote": "收入 USD 101",
                        "occurrence": 0,
                    }
                ],
                "quantity": {**income["quantity"], "value": "101"},
            },
            {
                "id": "benchmark",
                "kind": "fact",
                "text": "原基准25%",
                "citations": [
                    {
                        "source_id": original["id"],
                        "source_revision": 1,
                        "quote": "基准 25%",
                        "occurrence": 0,
                    }
                ],
                "quantity": {
                    "value": "25",
                    "unit": "percent",
                    "currency": None,
                    "period": "2026Q3",
                },
            },
            {
                "id": "nested_total",
                "kind": "fact",
                "text": "比例与基准之和",
                "citations": values["claims"][2]["citations"],
                "quantity": {
                    "value": "50",
                    "unit": "percent",
                    "currency": None,
                    "period": "2026Q3",
                },
                "calculation": {
                    "operation": "sum",
                    "inputs": ["share", "benchmark"],
                    "expected": "50",
                    "tolerance": "0",
                },
            },
        ]
    )
    revised = await manual(system, "revise", request_id="conflicting-input-revise", **values)
    reviewed = await checked(system, revised)
    assert "quantity_conflict" in finding_codes(reviewed)
    calculations = {row["claim_id"]: row for row in reviewed["review"]["calculations"]}
    states = {row["claim_id"]: row for row in reviewed["review"]["claim_states"]}
    unverified = {
        row["claim_id"]
        for row in reviewed["review"]["findings"]
        if row["code"] == "calculation_inputs_unverified"
    }
    for identity in ("share", "nested_total"):
        assert calculations[identity]["inputs_anchored"] is False
        assert identity in unverified and states[identity]["upstream_unverified"] is True


async def test_source_change_between_review_inputs_and_basis_never_persists_stale_anchors(
    system, monkeypatch
):
    captured = await capture(system, await create(system))
    revised = await manual(system, "revise", request_id="review-race-revise", **fields(captured))
    source = revised["sources"][0]
    original_basis = system.service._basis
    injected = False

    async def changing_basis(*args, **kwargs):
        nonlocal injected
        if not injected:
            injected = True
            system.capture.body = BODY.replace("100", "101")
            await manual(
                system,
                "recheck",
                source_id=source["id"],
                expected_revision=source["revision"],
                request_id="review-race-source-observation",
            )
        return await original_basis(*args, **kwargs)

    monkeypatch.setattr(system.service, "_basis", changing_basis)
    with pytest.raises(PersonalAgentError) as caught:
        await checked(system, revised, "review-race-original")
    assert injected and caught.value.code in {"research_busy", "source_stale"}
    records = await system.service._read(
        lambda tx: tx.list("review", parent=revised["notebook"]["id"], limit=50)
    )
    assert records == []
    request = await manual(system, "request", request_id="review-race-original")
    assert request["status"] != "completed"
    current = await manual(system, "source", id=source["id"])
    assert current["source"]["revision"] == 2 and current["body"] != BODY


async def test_source_change_between_publication_view_and_read_never_publishes_old_review(
    system, monkeypatch
):
    captured = await capture(system, await create(system))
    revised = await manual(system, "revise", request_id="publish-race-revise", **fields(captured))
    reviewed = await checked(system, revised, "publish-race-review")
    assert reviewed["review"]["status"] == "mechanically_checked"
    source = reviewed["sources"][0]
    original_new_read = system.service._new_read
    injected = False
    assert await system.engine.personal_artifacts.list(actor_id="main", kind="output") == []

    async def changing_new_read(*args, **kwargs):
        nonlocal injected
        if not injected:
            injected = True
            system.capture.body = BODY.replace("100", "101")
            await manual(
                system,
                "recheck",
                source_id=source["id"],
                expected_revision=source["revision"],
                request_id="publish-race-source-observation",
            )
        return await original_new_read(*args, **kwargs)

    monkeypatch.setattr(system.service, "_new_read", changing_new_read)
    with pytest.raises(PersonalAgentError) as caught:
        await published(system, reviewed, "publish-race-original")
    assert injected and caught.value.code in {"research_busy", "source_stale"}
    assert await system.engine.personal_artifacts.list(actor_id="main", kind="output") == []
    publications = await system.service._read(
        lambda tx: tx.list("publication", parent=reviewed["notebook"]["id"], limit=50)
    )
    assert publications == []
    request = await manual(system, "request", request_id="publish-race-original")
    assert request["status"] != "completed"
    current = await manual(system, "source", id=source["id"])
    assert current["source"]["revision"] == 2 and current["body"] != BODY
