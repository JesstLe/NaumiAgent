"""Real immutable private bytes, SQLite CAS, lineage and revocation contracts."""

from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from pypdf import PdfWriter

from naumi_agent.personal.artifact_models import strict_json_loads
from naumi_agent.personal.artifact_store import ArtifactStore
from naumi_agent.personal.models import PersonalAgentError

A, B, C, D, E = ("pa_" + letter * 32 for letter in "abcde")
WORK = "workspace-one"


@pytest.fixture
def store(tmp_path):
    return ArtifactStore(tmp_path / "private")


def publish(
    store, *, actor=A, request="publish-one", content=b"income,expense\n1200,375\n", **changes
):
    return store.publish(
        **{
            "workspace_key": WORK,
            "actor_id": actor,
            "request_id": request,
            "title": "真实预算资料",
            "kind": "material",
            "content": content,
            "suffix": ".csv",
            **changes,
        }
    )


def access(view, actor=A, **changes):
    return {
        "workspace_key": WORK,
        "actor_id": actor,
        "artifact_id": view["artifact_id"],
        "version": view["version"],
        **changes,
    }


def ref(view):
    return {
        key: view[key] for key in ("artifact_id", "version", "sha256", "grant_id", "grant_revision")
    }


def share(store, view, recipient=B, actor=A, request="share-one", **changes):
    return store.share(**access(view, actor), recipient_id=recipient, request_id=request, **changes)


def revoke(store, grant, actor=A, request="revoke-one", **changes):
    return store.revoke(
        **{
            "workspace_key": WORK,
            "actor_id": actor,
            "grant_id": grant["grant_id"],
            "expected_revision": grant["revision"],
            "request_id": request,
            **changes,
        }
    )


def team_scope(members=None):
    members = [B, C] if members is None else members
    return {
        "kind": "team",
        "team_id": D,
        "team_version": 1,
        "member_ids": members,
        "teams": [{"agent_id": D, "agent_version": 1, "member_ids": members}],
    }


def fail(code, operation, *args, **kwargs):
    with pytest.raises(PersonalAgentError) as caught:
        operation(*args, **kwargs)
    assert caught.value.code == code
    assert caught.value.message
    return caught.value


def test_publish_reads_actual_private_immutable_bytes_and_reopens(store):
    view = publish(store, evidence={"session_id": "session-one", "run_id": "real-run"})
    assert view["artifact_id"].startswith("art_") and len(view["artifact_id"]) == 36
    assert view["version"] == 1 and view["owner_id"] == A
    assert view["grant_id"] == "" and view["grant_revision"] == 0
    assert view["mime_type"] == "text/csv" and view["suffix"] == ".csv"
    expected = b"income,expense\n1200,375\n"
    assert view["sha256"] == hashlib.sha256(expected).hexdigest()
    assert view["bytes"] == len(expected) and "path" not in view
    assert store.read_content(**access(view)) == expected
    reopened = ArtifactStore(store.base_dir)
    assert reopened.get(**access(view)) == view
    assert reopened.read_content(**access(view)) == expected
    assert reopened.list(workspace_key=WORK, actor_id=A) == [view]


def test_main_principal_is_explicit_and_not_an_implicit_specialist_admin(store):
    own = publish(store, actor="main")
    other = publish(store, actor=A)
    fail("not_found", store.get, **access(other, "main"))
    fail("not_found", store.get, **access(own, A))
    share(store, own, actor="main")
    assert store.read_content(**access(own, B))


def test_read_only_methods_do_not_create_missing_database_or_directories(store):
    assert store.list(workspace_key=WORK, actor_id=A) == []
    assert store.session_refs(workspace_key=WORK, actor_id=A, session_id="session-one") == []
    assert store.authorize_refs(workspace_key=WORK, actor_id=A, refs=[]) == []
    ghost = {"workspace_key": WORK, "actor_id": A, "artifact_id": "art_" + "f" * 32}
    fail("not_found", store.get, **ghost)
    fail("not_found", store.versions, **ghost)
    assert not store.base_dir.exists()


@pytest.mark.parametrize(
    "mutation",
    [
        {"title": "另一个标题"},
        {"content": b"income,expense\n1,2\n"},
        {"kind": "output"},
        {"evidence": {"run_id": "changed"}},
        {"suffix": ".txt"},
    ],
)
def test_strict_publish_request_idempotence_rejects_changed_input(store, mutation):
    first = publish(store)
    assert publish(store) == first
    fail("idempotency_conflict", publish, store, **mutation)
    assert (
        len(store.versions(workspace_key=WORK, actor_id=A, artifact_id=first["artifact_id"])) == 1
    )


def test_same_content_creates_real_logical_versions_without_replacing_blob(store):
    first = publish(store)
    file = next(store.blob_dir.rglob("*.csv"))
    original_inode, modified_time = file.stat().st_ino, file.stat().st_mtime_ns
    second = publish(
        store, request="revise-two", artifact_id=first["artifact_id"], expected_version=1
    )
    assert second["version"] == 2 and second["sha256"] == first["sha256"]
    assert file.stat().st_ino == original_inode and file.stat().st_mtime_ns == modified_time
    assert len(list(store.blob_dir.rglob("*.csv"))) == 1
    assert store.versions(workspace_key=WORK, actor_id=A, artifact_id=first["artifact_id"]) == [
        second,
        first,
    ]
    assert publish(store) == first


def test_revision_requires_owner_cas_and_keeps_old_version_readable(store):
    first = publish(store)
    share(store, first)
    fail("not_found", publish, store, actor=B, artifact_id=first["artifact_id"], expected_version=1)
    second = publish(
        store,
        request="revision",
        content=b"income,expense\n2000,500\n",
        artifact_id=first["artifact_id"],
        expected_version=1,
        title="第二版",
    )
    fail(
        "version_conflict",
        publish,
        store,
        request="stale",
        artifact_id=first["artifact_id"],
        expected_version=1,
    )
    assert store.read_content(**access(first)) == b"income,expense\n1200,375\n"
    assert store.get(**access(second))["title"] == "第二版"


def test_recipient_cannot_discover_new_unshared_version_or_title(store):
    first = publish(store)
    grant = share(store, first)
    second = publish(
        store,
        request="revision",
        artifact_id=first["artifact_id"],
        expected_version=1,
        title="SECRET_NEW_VERSION",
        content=b"changed",
    )
    listed = store.list(workspace_key=WORK, actor_id=B)
    assert len(listed) == 1 and listed[0]["version"] == 1
    assert listed[0]["grant_id"] == grant["grant_id"]
    assert "SECRET" not in json.dumps(listed)
    assert (
        store.get(workspace_key=WORK, actor_id=B, artifact_id=first["artifact_id"])["version"] == 1
    )
    assert (
        store.versions(workspace_key=WORK, actor_id=B, artifact_id=first["artifact_id"]) == listed
    )
    fail("not_found", store.get, **access(second, B))


def test_share_is_explicit_per_version_revoke_cas_and_replay_does_not_resurrect(store):
    first = publish(store)
    grant = share(store, first, recipient_scope={})
    assert grant["revision"] == 1 and grant["revoked_at"] == ""
    assert share(store, first, recipient_scope={}) == grant
    assert share(store, first, request="same-grant", recipient_scope={}) == grant
    assert store.read_content(**access(first, B))
    gone = revoke(store, grant)
    assert gone["revision"] == 2 and gone["revoked_at"]
    assert revoke(store, grant) == gone
    assert share(store, first, recipient_scope={}) == gone
    fail("not_found", store.read_content, **access(first, B))
    assert store.list(workspace_key=WORK, actor_id=B) == []
    fresh = share(store, first, request="new-explicit-share", recipient_scope={})
    assert fresh["grant_id"] != grant["grant_id"]
    assert store.read_content(**access(first, B))
    fail("version_conflict", revoke, store, fresh, expected_revision=2, request="wrong-revision")
    assert store.grants(**access(first)) == [gone, fresh]


def test_grant_zero_or_another_recipients_grant_cannot_authorize_ref(store):
    first = publish(store)
    grant_b = share(store, first)
    grant_c = share(store, first, recipient=C, request="share-c")
    received = store.get(**access(first, B))
    assert received["grant_id"] == grant_b["grant_id"]
    fail("not_found", store.authorize_refs, workspace_key=WORK, actor_id=B, refs=[ref(first)])
    forged = ref(received) | {"grant_id": grant_c["grant_id"]}
    fail("not_found", store.authorize_refs, workspace_key=WORK, actor_id=B, refs=[forged])
    assert store.authorize_refs(workspace_key=WORK, actor_id=B, refs=[ref(received)]) == [
        ref(received)
    ]
    fail(
        "version_conflict",
        store.authorize_refs,
        workspace_key=WORK,
        actor_id=B,
        refs=[ref(received) | {"sha256": "0" * 64}],
    )


def test_frozen_team_grant_gives_only_historical_members_the_original_grant(store):
    first = publish(store)
    scope = team_scope()
    grant = share(store, first, recipient=D, recipient_scope=scope)
    for actor in (B, C, D):
        view = store.get(**access(first, actor))
        assert view["grant_id"] == grant["grant_id"]
        assert store.read_content(**access(first, actor))
        assert (
            store.get_grant(workspace_key=WORK, actor_id=actor, grant_id=grant["grant_id"]) == grant
        )
        assert store.access_grants(**access(first, actor)) == [grant]
        assert len(store.list(workspace_key=WORK, actor_id=actor)) == 1
    assert store.access_grants(**access(first)) == []
    fail("not_found", store.get, **access(first, E))
    fail("not_found", store.get_grant, workspace_key=WORK, actor_id=E, grant_id=grant["grant_id"])
    scope["member_ids"].append(E)
    fail("not_found", store.get, **access(first, E))
    fail(
        "version_conflict",
        share,
        store,
        first,
        recipient=D,
        request="changed-members",
        recipient_scope=team_scope([B, C, E]),
    )
    revoke(store, grant)
    for actor in (B, C, D):
        fail("not_found", store.read_content, **access(first, actor))


def test_nested_team_snapshot_and_derived_access_grants_preserve_ancestor_dependency(store):
    first = publish(store)
    scope = team_scope()
    scope["teams"] = [
        {"agent_id": D, "agent_version": 1, "member_ids": [E, B]},
        {"agent_id": E, "agent_version": 3, "member_ids": [C]},
    ]
    group = share(store, first, recipient=D, recipient_scope=scope)
    received = store.get(**access(first, B))
    derived = publish(store, actor=B, request="derived", source_refs=[ref(received)], kind="output")
    own = share(store, derived, actor=B, recipient=C, request="derived-c")
    collected = store.access_grants(**access(derived, C))
    assert {item["grant_id"] for item in collected} == {group["grant_id"], own["grant_id"]}
    assert store.get(**access(derived, C))
    fail("not_found", store.get, **access(first, E))
    revoke(store, group)
    fail("not_found", store.access_grants, **access(derived, B))
    fail("not_found", store.read_content, **access(derived, C))


def test_group_derivative_share_requires_each_frozen_leafs_upstream_access(store):
    source = publish(store)
    share(store, source)
    received = store.get(**access(source, B))
    derived = publish(store, actor=B, request="derived", kind="output", source_refs=[ref(received)])
    share(store, source, recipient=D, request="team-only-source")
    fail(
        "not_found",
        share,
        store,
        derived,
        actor=B,
        recipient=D,
        request="missing-leaf-source",
        recipient_scope=team_scope(),
    )
    share(store, source, recipient=C, request="source-c")
    group = share(
        store, derived, actor=B, recipient=D, request="valid-group", recipient_scope=team_scope()
    )
    for reader in (B, C, D):
        view = store.get(**access(derived, reader))
        if reader != B:
            assert view["grant_id"] == group["grant_id"]
        assert store.read_content(**access(derived, reader))


@pytest.mark.parametrize(
    "mutation",
    [
        {"kind": "all"},
        {"team_id": E},
        {"team_version": True},
        {"member_ids": [B, B]},
        {"member_ids": [B, C, "main"]},
        {"member_ids": [B]},
        {"unknown": True},
        {"teams": []},
        {"teams": [{"agent_id": D, "agent_version": 2, "member_ids": [B, C]}]},
        {"teams": [{"agent_id": D, "agent_version": 1, "member_ids": [D]}]},
    ],
)
def test_invalid_team_scope_cannot_grant_access(store, mutation):
    first = publish(store)
    fail("invalid_input", share, store, first, recipient=D, recipient_scope=team_scope() | mutation)
    assert store.grants(**access(first)) == []
    for actor in (B, C, D):
        fail("not_found", store.get, **access(first, actor))


def test_cannot_use_read_only_grant_to_share_an_original_version(store):
    first = publish(store)
    share(store, first)
    fail("not_found", share, store, first, actor=B, recipient=C)
    fail("not_found", store.grants, **access(first, B))


def test_derived_publication_share_requires_upstream_and_ancestor_revoke_fences_all_readers(store):
    source = publish(store)
    original_grant = share(store, source)
    received = store.get(**access(source, B))
    derived = publish(
        store,
        actor=B,
        request="derived",
        kind="output",
        suffix=".md",
        content="预算结余：825 元".encode(),
        source_refs=[ref(received)],
    )
    assert derived["source_refs"] == [ref(received)]
    assert "reader_id" not in json.dumps(derived)
    fail("not_found", share, store, derived, actor=B, recipient=C, request="derived-c-denied")
    share(store, source, recipient=C, request="source-c")
    downstream = share(store, derived, actor=B, recipient=C, request="derived-c")
    assert store.read_content(**access(derived, C)) == "预算结余：825 元".encode()
    revoke(store, original_grant)
    for actor in (B, C):
        fail("not_found", store.read_content, **access(derived, actor))
        assert store.list(workspace_key=WORK, actor_id=actor, kind="output") == []
    fail(
        "not_found",
        publish,
        store,
        actor=B,
        request="washed-revision",
        kind="output",
        suffix=".md",
        content=b"new report",
        artifact_id=derived["artifact_id"],
        expected_version=1,
        source_refs=[],
    )
    assert revoke(store, downstream, actor=B)["revision"] == 2


def test_revisions_and_bound_sessions_cannot_drop_prior_lineage(store):
    source = publish(store)
    grant = share(store, source)
    received = store.get(**access(source, B))
    derived = publish(
        store,
        actor=B,
        request="derived",
        kind="output",
        suffix=".md",
        content=b"825",
        source_refs=[ref(received)],
    )
    revised = publish(
        store,
        actor=B,
        request="revision",
        kind="output",
        suffix=".md",
        content=b"825 checked",
        artifact_id=derived["artifact_id"],
        expected_version=1,
        source_refs=[],
    )
    assert revised["source_refs"] == derived["source_refs"]
    binding = {"workspace_key": WORK, "session_id": "receiver-session", "actor_id": B}
    assert store.bind_session_refs(**binding, refs=[ref(received)]) == [ref(received)]
    assert store.bind_session_refs(**binding, refs=[]) == [ref(received)]
    assert ArtifactStore(store.base_dir).session_refs(**binding) == [ref(received)]
    fail("binding_conflict", store.session_refs, **(binding | {"actor_id": C}))
    revoke(store, grant)
    fail("not_found", store.session_refs, **binding)
    fail("not_found", store.bind_session_refs, **binding, refs=[])
    share(store, source, request="new-share")
    fail("not_found", store.session_refs, **binding)


def test_cross_workspace_is_hidden_even_when_id_and_grant_are_known(store):
    first = publish(store)
    grant = share(store, first)
    for actor in (A, B):
        fail("not_found", store.get, **access(first, actor, workspace_key="different"))
        assert store.list(workspace_key="different", actor_id=actor) == []
    fail(
        "not_found",
        store.revoke,
        workspace_key="different",
        actor_id=A,
        grant_id=grant["grant_id"],
        expected_revision=1,
        request_id="forged",
    )
    fail("not_found", publish, store, actor=C, request="cross-source", source_refs=[ref(first)])


def test_search_and_pagination_are_real_and_shared_titles_are_version_scoped(store):
    first = publish(store, title="Alpha 预算")
    second = publish(store, request="publish-two", kind="output", title="Beta 结果")
    share(store, first)
    all_items = store.list(workspace_key=WORK, actor_id=A)
    assert all_items == [second, first]
    assert store.list(workspace_key=WORK, actor_id=A, query="ALPHA") == [first]
    assert store.list(workspace_key=WORK, actor_id=A, kind="output") == [second]
    assert store.list(workspace_key=WORK, actor_id=A, offset=1, limit=1) == [first]
    assert store.list(workspace_key=WORK, actor_id=B, query="Beta") == []


def test_concurrent_identical_publish_has_one_version_and_one_identity(store):
    def write(_):
        return publish(ArtifactStore(store.base_dir))

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(write, range(12)))
    assert all(result == results[0] for result in results)
    assert len(store.list(workspace_key=WORK, actor_id=A)) == 1
    assert len(list(store.blob_dir.rglob("*.csv"))) == 1


@pytest.mark.skipif(os.name != "nt", reason="Win32 extended path namespace")
def test_equivalent_win32_extended_path_during_creation_is_not_a_storage_alias(store, monkeypatch):
    resolve = Path.resolve

    def temporary_extended_path(path, *args, **kwargs):
        result = resolve(path, *args, **kwargs)
        if path.name == "artifacts.db" and not str(result).startswith("\\\\?\\"):
            return Path("\\\\?\\" + str(result))
        return result

    monkeypatch.setattr(Path, "resolve", temporary_extended_path)
    view = publish(store)
    assert store.read_content(**access(view)) == b"income,expense\n1200,375\n"


def test_concurrent_cas_revisions_have_exactly_one_winner(store):
    first = publish(store)

    def revise(number):
        try:
            return publish(
                ArtifactStore(store.base_dir),
                request=f"revision-{number}",
                artifact_id=first["artifact_id"],
                expected_version=1,
                content=str(number).encode(),
            )
        except PersonalAgentError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(revise, range(8)))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert results.count("version_conflict") == 7
    assert (
        len(store.versions(workspace_key=WORK, actor_id=A, artifact_id=first["artifact_id"])) == 2
    )


def test_concurrent_share_and_revoke_never_resurrect_the_original_grant(store):
    first = publish(store)
    grant = share(store, first)

    def cancel(number):
        try:
            return revoke(ArtifactStore(store.base_dir), grant, request=f"cancel-{number}")
        except PersonalAgentError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(cancel, range(4)))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert results.count("version_conflict") == 3
    assert share(store, first)["revision"] == 2
    fail("not_found", store.read_content, **access(first, B))


def test_blob_written_before_metadata_failure_is_reused_on_original_retry(store, monkeypatch):
    publish_blob = store._publish_blob
    interrupted = True

    def fail_after_blob(*args):
        publish_blob(*args)
        if interrupted:
            raise OSError("controlled metadata transaction interruption")

    monkeypatch.setattr(store, "_publish_blob", fail_after_blob)
    fail("storage_error", publish, store)
    assert store.list(workspace_key=WORK, actor_id=A) == []
    assert len(list(store.blob_dir.rglob("*.csv"))) == 1
    interrupted = False
    first = publish(store)
    assert store.read_content(**access(first))
    assert len(store.list(workspace_key=WORK, actor_id=A)) == 1


@pytest.mark.parametrize(
    "mutation", ["bytes", "missing", "head", "grant", "session", "version_json", "version_delete"]
)
def test_corruption_is_never_treated_as_empty_or_fresh_data(store, mutation):
    first = publish(store)
    grant = share(store, first)
    binding = {"workspace_key": WORK, "session_id": "bound-session", "actor_id": A}
    store.bind_session_refs(**binding, refs=[ref(first)])
    blob = next(store.blob_dir.rglob("*.csv"))
    if mutation == "bytes":
        blob.write_bytes(b"x" * first["bytes"])
    elif mutation == "missing":
        blob.unlink()
    else:
        with sqlite3.connect(store.db_path) as conn:
            if mutation == "head":
                conn.execute("UPDATE artifacts SET head_sha256=?", ("0" * 64,))
            elif mutation == "grant":
                conn.execute(
                    "UPDATE artifact_grants SET recipient_id=? WHERE grant_id=?",
                    (C, grant["grant_id"]),
                )
            elif mutation == "session":
                conn.execute("UPDATE artifact_session_refs SET refs_json='[]'")
            elif mutation == "version_json":
                conn.execute("DROP TRIGGER artifact_version_no_update")
                conn.execute("UPDATE artifact_versions SET record_json='{}'")
            else:
                conn.execute("DROP TRIGGER artifact_version_no_delete")
                conn.execute("DELETE FROM artifact_versions")
    if mutation == "grant":
        fail("storage_error", store.get, **access(first, C))
    elif mutation == "session":
        fail("storage_error", store.session_refs, **binding)
    else:
        fail("storage_error", store.read_content, **access(first))


def test_version_records_are_immutable_at_the_sqlite_boundary(store):
    publish(store)
    with sqlite3.connect(store.db_path) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="immutable version"):
            conn.execute("UPDATE artifact_versions SET record_json='{}'")
        with pytest.raises(sqlite3.IntegrityError, match="immutable version"):
            conn.execute("DELETE FROM artifact_versions")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"content": b""},
        {"content": "not bytes"},
        {"suffix": ".exe"},
        {"kind": "other"},
        {"kind": {}},
        {"title": ""},
        {"title": "\ud800"},
        {"title": "bad\x00title"},
        {"expected_version": 1},
        {"artifact_id": "../../escape", "expected_version": 1},
        {"actor": "user-supplied-admin"},
        {"evidence": {"value": float("nan")}},
        {"evidence": {"value": object()}},
        {"evidence": []},
        {"source_refs": [{"artifact_id": "art_" + "f" * 32}]},
    ],
)
def test_invalid_publication_parameters_have_no_content_side_effects(store, kwargs):
    fail("invalid_input", publish, store, **kwargs)
    assert not store.blob_dir.exists()


@pytest.mark.parametrize(
    "content",
    [
        b'{"same":1,"same":2}',
        b'{"nested":{"x":1,"x":2}}',
        b'{"v":NaN}',
        b'{"v":Infinity}',
        b'{"v":1e999}',
        b"not json",
        b'"\xff"',
    ],
)
def test_json_content_rejects_duplicate_keys_nonfinite_numbers_and_invalid_utf8(store, content):
    fail("invalid_input", publish, store, content=content, suffix=".json")
    assert not store.blob_dir.exists()


def test_strict_json_supports_bom_and_valid_unicode(store):
    payload = '\ufeff{"名称":"真实资料","值":825}'.encode()
    first = publish(store, content=payload, suffix=".json")
    assert strict_json_loads(store.read_content(**access(first))) == {"名称": "真实资料", "值": 825}


def pdf_bytes(pages=1, *, encrypted=False):
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=72, height=72)
    if encrypted:
        writer.encrypt("fixture-password-not-real")
    target = io.BytesIO()
    writer.write(target)
    return target.getvalue()


@pytest.mark.parametrize("pages", [1, 1000])
def test_pdf_structure_uses_real_parser_and_keeps_exact_original_bytes(store, pages):
    content = pdf_bytes(pages)
    view = publish(store, content=content, suffix=".pdf")
    assert view["mime_type"] == "application/pdf"
    assert view["sha256"] == hashlib.sha256(content).hexdigest()
    assert store.read_content(**access(view)) == content


@pytest.mark.parametrize(
    "case",
    [
        "header_only",
        "truncated",
        "encrypted",
        "no_pages",
        "over_limit",
        "wrong_count",
        "wrong_catalog",
    ],
)
def test_pdf_rejects_real_structural_errors_encryption_and_page_limits(store, case):
    content = pdf_bytes()
    if case == "header_only":
        content = b"%PDF-1.7\npretend content\n%%EOF\n"
    elif case == "truncated":
        content = content[:-40]
    elif case == "encrypted":
        content = pdf_bytes(encrypted=True)
    elif case == "no_pages":
        content = pdf_bytes(0)
    elif case == "over_limit":
        content = pdf_bytes(1001)
    elif case == "wrong_count":
        assert b"/Count 1" in content
        content = content.replace(b"/Count 1", b"/Count 2", 1)
    else:
        assert b"/Type /Catalog" in content
        content = content.replace(b"/Type /Catalog", b"/Type /Invalid", 1)
    fail("invalid_input", publish, store, content=content, suffix=".pdf")
    assert not store.blob_dir.exists()


@pytest.mark.parametrize("exception", [AssertionError, IndexError])
def test_pdf_parser_aborts_remain_a_chinese_validation_error(store, monkeypatch, exception):
    import naumi_agent.personal.artifact_models as models

    def malformed_structure(*args, **kwargs):
        raise exception("malformed PDF structure")

    monkeypatch.setattr(models, "PdfReader", malformed_structure)
    fail("invalid_input", publish, store, content=pdf_bytes(), suffix=".pdf")
    assert not store.blob_dir.exists()


@pytest.mark.parametrize("kind", ["blob", "database", "base"])
def test_symlink_or_junction_storage_aliases_are_not_followed(store, tmp_path, kind):
    first = publish(store)
    outside = tmp_path / "outside"
    outside.mkdir()
    if kind == "blob":
        target = next(store.blob_dir.rglob("*.csv"))
        other = outside / target.name
        other.write_bytes(b"private outside content")
        target.unlink()
    elif kind == "database":
        target = store.db_path
        other = outside / "other.db"
        target.replace(other)
    else:
        target = store.base_dir
        other = outside / "actual"
        assert target.resolve().is_relative_to(tmp_path.resolve())
        assert other.resolve().is_relative_to(tmp_path.resolve())
        target.rename(other)
    try:
        os.symlink(other, target, target_is_directory=kind == "base")
    except (OSError, NotImplementedError):
        if kind != "base" or os.name != "nt":
            pytest.skip("This environment does not support unprivileged file symlinks")
        import subprocess

        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(target), str(other)], check=True, capture_output=True
        )
    if kind == "base":
        fail("storage_error", ArtifactStore, target)
    else:
        fail("storage_error", store.read_content, **access(first))


def test_unknown_future_schema_is_not_opened_for_writes(store):
    first = publish(store)
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("PRAGMA user_version=999")
    fail("unsupported_schema", store.get, **access(first))
    fail("unsupported_schema", publish, store, request="write-new")


def test_twenty_megabyte_actual_byte_limit_is_not_silently_truncated(store):
    allowed = b"x" * (20 * 1024 * 1024)
    first = publish(store, content=allowed, suffix=".txt")
    assert first["bytes"] == len(allowed)
    assert store.read_content(**access(first)) == allowed
    fail(
        "invalid_input", publish, store, request="too-large", content=allowed + b"x", suffix=".txt"
    )
    assert len(store.list(workspace_key=WORK, actor_id=A)) == 1


def test_base_replaced_by_junction_is_not_silently_returned_as_empty(store, tmp_path):
    publish(store)
    target = store.base_dir
    moved = tmp_path / "moved-private"
    empty = tmp_path / "empty-outside"
    empty.mkdir()
    assert target.resolve().is_relative_to(tmp_path.resolve())
    assert moved.resolve().is_relative_to(tmp_path.resolve())
    target.rename(moved)
    try:
        os.symlink(empty, target, target_is_directory=True)
    except (OSError, NotImplementedError):
        if os.name != "nt":
            pytest.skip("Directory aliases are not available")
        import subprocess

        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(target), str(empty)], check=True, capture_output=True
        )
    fail("storage_error", store.list, workspace_key=WORK, actor_id=A)
