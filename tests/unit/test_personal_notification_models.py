"""Strict user preferences and immutable, domain-separated notification identities."""

from __future__ import annotations

import copy

import pytest

from naumi_agent.personal import notification_models as models
from naumi_agent.personal.models import PersonalAgentError
from naumi_agent.personal.owner_journal import digest
from naumi_agent.personal.routine_schedule import _zone

WORKSPACE = "a" * 64
ACTOR = "pa_" + "b" * 32
RAW = {
    "enabled": True,
    "timezone": "Asia/Shanghai",
    "quiet_hours": [{"weekdays": [4, 0], "start": "22:00", "end": "08:00", "full_day": False}],
    "muted_actor_ids": [ACTOR, "main"],
    "debounce_seconds": 60,
    "max_delay_seconds": 300,
    "bypass_kinds": ["item_failure", "calendar_approval"],
    "include_existing": False,
}


def assert_error(callback):
    with pytest.raises(PersonalAgentError) as caught:
        callback()
    assert caught.value.code == "invalid_input"


def test_preferences_normalize_once_detach_sort_and_use_real_tzif_bytes():
    raw = copy.deepcopy(RAW)
    value = models.normalize_preferences(raw)
    assert value["timezone_sha256"] == _zone("Asia/Shanghai")[1]
    assert value["quiet_hours"][0]["weekdays"] == [0, 4]
    assert value["muted_actor_ids"] == sorted(RAW["muted_actor_ids"])
    assert value["bypass_kinds"] == ["calendar_approval", "item_failure"]
    raw["quiet_hours"][0]["weekdays"].append(1)
    raw["muted_actor_ids"].clear()
    assert value["quiet_hours"][0]["weekdays"] == [0, 4]
    assert len(value["muted_actor_ids"]) == 2
    assert models.validate_preferences(value) == value


@pytest.mark.parametrize(
    "changes",
    [
        {"enabled": 1},
        {"enabled": "true"},
        {"include_existing": 0},
        {"debounce_seconds": True},
        {"debounce_seconds": -1},
        {"debounce_seconds": 301},
        {"debounce_seconds": 0.0},
        {"max_delay_seconds": False},
        {"max_delay_seconds": "300"},
        {"max_delay_seconds": 0},
        {"max_delay_seconds": 3601},
        {"debounce_seconds": 300, "max_delay_seconds": 299},
        {"muted_actor_ids": ["main", "main"]},
        {"muted_actor_ids": ["team"]},
        {"muted_actor_ids": ["pa_" + "A" * 32]},
        {"muted_actor_ids": None},
        {"muted_actor_ids": [f"pa_{number:032x}" for number in range(1001)]},
        {"bypass_kinds": ["item_result"]},
        {"bypass_kinds": ["item_failure", "item_failure"]},
        {"bypass_kinds": [True]},
        {"bypass_kinds": [] * 3 + ["bad"]},
        {"quiet_hours": None},
        {"quiet_hours": [RAW["quiet_hours"][0]] * 15},
        {"quiet_hours": [RAW["quiet_hours"][0]] * 2},
        {"timezone_sha256": "f" * 64},
        {"trusted_channel": "web"},
        {"caller_actor": "main"},
        {"revision": 1},
        {"extra": float("nan")},
    ],
)
def test_preferences_reject_coercion_duplicates_limits_and_trusted_fields(changes):
    assert_error(lambda: models.normalize_preferences(RAW | changes))


@pytest.mark.parametrize("value", [None, [], "preferences", {}, {"enabled": True}])
def test_preferences_require_all_input_fields(value):
    assert_error(lambda: models.normalize_preferences(value))


@pytest.mark.parametrize("timezone", [None, 0, "", "../UTC", "/UTC", "UTC\\name", "Not/AZone"])
def test_invalid_timezone_is_not_resolved_or_hashed(timezone):
    with pytest.raises(PersonalAgentError, match="IANA") as caught:
        models.normalize_preferences(RAW | {"timezone": timezone})
    assert caught.value.code == "invalid_timezone"


@pytest.mark.parametrize(
    "changes",
    [
        {"weekdays": []},
        {"weekdays": [0, 0]},
        {"weekdays": [True]},
        {"weekdays": [0.0]},
        {"weekdays": [-1]},
        {"weekdays": [7]},
        {"weekdays": "0"},
        {"start": "24:00"},
        {"start": "8:00"},
        {"start": "08:00:00"},
        {"end": "08:60"},
        {"end": "08:00Z"},
        {"start": "08:00", "end": "08:00", "full_day": False},
        {"start": "08:00", "end": "09:00", "full_day": True},
        {"full_day": 0},
        {"extra": True},
    ],
)
def test_window_is_exact_nonempty_and_uses_explicit_full_day(changes):
    window = RAW["quiet_hours"][0] | changes
    assert_error(lambda: models.normalize_preferences(RAW | {"quiet_hours": [window]}))


def test_full_day_overlapping_windows_and_numeric_bounds_are_supported():
    windows = [
        {"weekdays": [6, 0], "start": "08:00", "end": "08:00", "full_day": True},
        {"weekdays": [0], "start": "07:00", "end": "10:00", "full_day": False},
    ]
    for debounce, maximum in ((0, 1), (300, 300), (300, 3600)):
        value = models.normalize_preferences(
            RAW
            | {"quiet_hours": windows, "debounce_seconds": debounce, "max_delay_seconds": maximum}
        )
        assert len(value["quiet_hours"]) == 2
        assert value["debounce_seconds"] == debounce


def test_virtual_default_is_disabled_and_has_no_cutover_or_fake_saved_revision():
    value = models.default_preferences(WORKSPACE)
    assert value["id"] == models.identity("np_", {"workspace_key": WORKSPACE})
    assert value["workspace_key"] == WORKSPACE and value["revision"] == 0
    assert value["enabled"] is value["include_existing"] is False
    assert value["timezone"] == "UTC" and value["timezone_sha256"] == _zone("UTC")[1]
    assert value["quiet_hours"] == value["muted_actor_ids"] == value["bypass_kinds"] == []
    assert value["debounce_seconds"] == 60 and value["max_delay_seconds"] == 300
    assert value["cutover_at"] is value["created_at"] is value["updated_at"] is None
    assert models.validate_preferences(value) == value
    assert_error(lambda: models.default_preferences("A" * 64))
    assert_error(lambda: models.default_preferences("workspace"))


def test_persisted_validation_preserves_original_tzif_and_canonical_ordering():
    original = models.normalize_preferences(RAW)
    changed = original | {"timezone_sha256": "f" * 64}
    assert models.validate_preferences(changed) == changed
    assert_error(lambda: models.validate_preferences(original | {"timezone_sha256": "bad"}))
    assert_error(lambda: models.validate_preferences(original | {"owner_key": WORKSPACE}))
    assert_error(
        lambda: models.validate_preferences(
            original | {"muted_actor_ids": list(reversed(original["muted_actor_ids"]))}
        )
    )
    assert_error(lambda: models.validate_preferences(RAW))


def test_identities_are_domain_separated_and_every_document_field_is_bound():
    document = {"workspace_key": WORKSPACE, "field": "value"}
    first = models.identity("ne_", document)
    assert len(first) == 35 and first.startswith("ne_")
    assert first == models.identity("ne_", dict(reversed(list(document.items()))))
    assert first != models.identity("no_", document)
    assert first != models.identity("ne_", document | {"field": "changed"})
    assert first != models.identity("ne_", document | {"domain": "personal_today_v1"})
    for prefix in models.PREFIXES:
        assert len(models.identity(prefix, {})) == 35
    for prefix in ("pi_", "NE_", "", True, None, []):
        assert_error(lambda p=prefix: models.identity(p, document))
    assert_error(lambda: models.identity("ne_", []))
    assert_error(lambda: models.identity("ne_", {"field": float("inf")}))


def test_record_digest_covers_complete_record_and_omits_only_external_digest():
    document = {"id": "ne_" + "1" * 32, "revision": 1, "workspace_key": WORKSPACE}
    assert models.record_digest(document) == digest(document)
    assert models.record_digest(document | {"digest": "f" * 64}) == digest(document)
    assert models.record_digest(document | {"revision": 2}) != digest(document)
    assert_error(lambda: models.record_digest([]))
    assert_error(lambda: models.record_digest({"field": float("nan")}))


@pytest.mark.parametrize(
    "changes",
    [
        {"event_id": "pi_" + "1" * 32},
        {"event_id": "ne_" + "A" * 32},
        {"event_id": "ne_" + "1" * 31},
        {"event_digest": "A" * 64},
        {"event_digest": None},
        {"revision": 1},
    ],
)
def test_event_refs_reject_changed_shapes_and_non_notification_ids(changes):
    reference = {"event_id": "ne_" + "1" * 32, "event_digest": "2" * 64}
    assert models.normalize_event_ref(reference) == reference
    assert models.normalize_event_ref(reference) is not reference
    assert_error(lambda: models.normalize_event_ref(reference | changes))
