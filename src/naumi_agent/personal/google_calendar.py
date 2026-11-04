"""Google Calendar primary-calendar protocol adapter and exact event projections."""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from naumi_agent.personal.google_oauth import (
    OPERATION_MARKER,
    GoogleProtocolError,
    _request_json,
    _text,
)

BASE_URI = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
_EVENT_ID = re.compile(r"^[A-Za-z0-9_-]{1,1024}$")
_INSERT_ID = re.compile(r"^[0-9a-v]{5,1024}$")
_PROJECTION = frozenset(
    {
        "summary",
        "description",
        "location",
        "start",
        "end",
        "transparency",
        "visibility",
        "extendedProperties",
    }
)


def _event_id(value):
    if not isinstance(value, str) or not _EVENT_ID.fullmatch(value):
        raise GoogleProtocolError("invalid_event_id")
    return value


def _timestamp(value):
    if not isinstance(value, str) or len(value) > 80:
        raise GoogleProtocolError("invalid_event")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError()
        return parsed.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    except (ValueError, OverflowError):
        raise GoogleProtocolError("invalid_event") from None


def _when(value):
    if not isinstance(value, dict) or set(value) - {"date", "dateTime", "timeZone"}:
        raise GoogleProtocolError("invalid_event")
    if ("date" in value) == ("dateTime" in value):
        raise GoogleProtocolError("invalid_event")
    zone = value.get("timeZone")
    if zone is not None:
        try:
            if not isinstance(zone, str) or len(zone) > 128:
                raise ValueError()
            ZoneInfo(zone)
        except (ValueError, ZoneInfoNotFoundError):
            raise GoogleProtocolError("invalid_event") from None
    if "date" in value:
        try:
            day = date.fromisoformat(value["date"])
            if day.isoformat() != value["date"]:
                raise ValueError()
        except (ValueError, TypeError):
            raise GoogleProtocolError("invalid_event") from None
        return {"date": day.isoformat(), **({"timeZone": zone} if zone is not None else {})}
    return {"dateTime": _timestamp(value["dateTime"]), "timeZone": zone}


def _plain(value, maximum=20000):
    if (
        not isinstance(value, str)
        or len(value) > maximum
        or any(ord(c) < 32 and c not in "\n\r\t" for c in value)
    ):
        raise GoogleProtocolError("invalid_event")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise GoogleProtocolError("invalid_event") from None
    return value


def _private(value):
    if not isinstance(value, dict) or len(value) > 50:
        raise GoogleProtocolError("invalid_event")
    result = {}
    for key, content in value.items():
        result[_plain(key, 44)] = _plain(content, 1024)
    return result


def normalize_event(event):
    if not isinstance(event, dict):
        raise GoogleProtocolError("invalid_event")
    identifier = _event_id(event.get("id"))
    status = event.get("status", "confirmed")
    if not isinstance(status, str) or status not in {"confirmed", "tentative", "cancelled"}:
        raise GoogleProtocolError("invalid_event")
    cancelled = status == "cancelled"
    private = event.get("extendedProperties", {})
    if not isinstance(private, dict):
        raise GoogleProtocolError("invalid_event")
    link = event.get("htmlLink")
    if link is not None:
        try:
            parts = urlsplit(_plain(link, 8192))
            if (
                parts.scheme != "https"
                or parts.hostname not in {"www.google.com", "calendar.google.com"}
                or parts.port not in {None, 443}
                or parts.username is not None
                or parts.password is not None
            ):
                raise ValueError()
        except ValueError:
            raise GoogleProtocolError("invalid_event") from None
    recurring = event.get("recurringEventId")
    if recurring is not None:
        _event_id(recurring)
    sequence = event.get("sequence")
    if sequence is not None and (type(sequence) is not int or not 0 <= sequence <= 2**63 - 1):
        raise GoogleProtocolError("invalid_event")
    recurrence = event.get("recurrence", [])
    if not isinstance(recurrence, list) or len(recurrence) > 100:
        raise GoogleProtocolError("invalid_event")
    transparency, visibility = (
        event.get("transparency", "opaque"),
        event.get("visibility", "default"),
    )
    if transparency not in ("opaque", "transparent") or visibility not in (
        "default",
        "public",
        "private",
        "confidential",
    ):
        raise GoogleProtocolError("invalid_event")
    result = {
        "id": identifier,
        "status": status,
        "etag": _plain(event.get("etag", ""), 512),
        "updated": _timestamp(event["updated"]) if event.get("updated") is not None else None,
        "summary": _plain(event.get("summary", "")),
        "description": _plain(event.get("description", "")),
        "location": _plain(event.get("location", "")),
        "transparency": transparency,
        "visibility": visibility,
        "htmlLink": link,
        "start": _when(event["start"]) if event.get("start") is not None else None,
        "end": _when(event["end"]) if event.get("end") is not None else None,
        "recurringEventId": recurring,
        "originalStartTime": _when(event["originalStartTime"])
        if event.get("originalStartTime") is not None
        else None,
        "iCalUID": _plain(event["iCalUID"], 1024) if event.get("iCalUID") is not None else None,
        "sequence": sequence,
        "recurrence": [_plain(value, 4096) for value in recurrence],
        "extendedProperties": {"private": _private(private.get("private", {}))},
    }
    if not cancelled and (result["start"] is None or result["end"] is None):
        raise GoogleProtocolError("invalid_event")
    if recurring is not None and result["originalStartTime"] is None:
        raise GoogleProtocolError("invalid_event")
    return result


def occurrence_key(event):
    normalized = normalize_event(event)
    if normalized["recurringEventId"] is None:
        return "event:" + normalized["id"]
    original = normalized["originalStartTime"]
    point = {"date": original["date"]} if "date" in original else {"dateTime": original["dateTime"]}
    return (
        "instance:"
        + normalized["recurringEventId"]
        + ":"
        + json.dumps(point, sort_keys=True, separators=(",", ":"))
    )


def approved_event_projection(body):
    if not isinstance(body, dict) or not body or set(body) - _PROJECTION:
        raise GoogleProtocolError("invalid_approved_event")
    result = {}
    for key, value in body.items():
        if key in {"start", "end"}:
            result[key] = _when(value)
        elif key == "extendedProperties":
            if not isinstance(value, dict) or set(value) != {"private"}:
                raise GoogleProtocolError("invalid_approved_event")
            result[key] = {"private": _private(value["private"])}
        else:
            result[key] = _plain(value)
    if "start" in result and "end" in result:
        start, end = result["start"], result["end"]
        kind = "date" if "date" in start else "dateTime"
        if kind not in end or end[kind] <= start[kind]:
            raise GoogleProtocolError("invalid_approved_event")
    for key, choices in (
        ("transparency", ("opaque", "transparent")),
        ("visibility", ("default", "public", "private", "confidential")),
    ):
        if key in result and result[key] not in choices:
            raise GoogleProtocolError("invalid_approved_event")
    return result


def event_matches_approval(event, approved, *, operation_marker):
    """Compare approved fields only; timestamps compare as instants, not offset text."""
    try:
        normalized = normalize_event(event)
        projected = approved_event_projection(approved)
        _text(operation_marker, 1024)
        if (
            normalized["status"] == "cancelled"
            or normalized["extendedProperties"]["private"].get(OPERATION_MARKER) != operation_marker
        ):
            return False
        if any(
            event.get(field)
            for field in (
                "attendees",
                "attachments",
                "conferenceData",
                "recurrence",
                "recurringEventId",
            )
        ):
            return False
        if (
            projected.get("extendedProperties", {}).get("private", {}).get(OPERATION_MARKER)
            != operation_marker
        ):
            return False
        for key, expected in projected.items():
            actual = normalized.get(key, event.get(key))
            if key in {"start", "end"}:
                if actual is None or ("date" in actual) != ("date" in expected):
                    return False
                # A nonrecurring event's approved start/end are exact instants.
                # Keep provider timeZone in snapshots, but accept canonical UTC/aliases.
                field = "date" if "date" in expected else "dateTime"
                if actual[field] != expected[field]:
                    return False
            elif key == "extendedProperties":
                if any(
                    actual["private"].get(name) != value
                    for name, value in expected["private"].items()
                ):
                    return False
            elif actual != expected:
                return False
        return True
    except (GoogleProtocolError, ValueError, TypeError):
        return False


def _write_body(body):
    projected = approved_event_projection(body)
    marker = projected.get("extendedProperties", {}).get("private", {}).get(OPERATION_MARKER)
    try:
        _text(marker, 1024)
        raw = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(raw) > 256 * 1024:
            raise ValueError()
        return json.loads(raw)
    except (ValueError, TypeError, UnicodeError, GoogleProtocolError):
        raise GoogleProtocolError("invalid_approved_event") from None


class GoogleCalendarAdapter:
    def __init__(self, *, http_client=None):
        self._client = http_client

    async def _call(self, method, access_token, *, suffix="", params=None, body=None, etag=None):
        _text(access_token, 65536)
        headers = {"Authorization": "Bearer " + access_token, "Accept": "application/json"}
        if etag is not None:
            _text(etag, 512)
            headers["If-Match"] = etag
        return await _request_json(
            self._client,
            method,
            BASE_URI + suffix,
            headers=headers,
            params=params,
            body=body,
            mutation=method in {"POST", "PATCH"},
            maximum=2 * 1024 * 1024,
        )

    async def list_events(self, access_token, *, page_token=None, sync_token=None):
        params = {"singleEvents": "false", "showDeleted": "true", "maxResults": "250"}
        for name, value in (("pageToken", page_token), ("syncToken", sync_token)):
            if value is not None:
                params[name] = _text(value, 8192)
        return await self._call("GET", access_token, params=params)

    async def instances(
        self, access_token, event_id, *, page_token=None, time_min=None, time_max=None
    ):
        params = {"showDeleted": "true", "maxResults": "250"}
        if page_token is not None:
            params["pageToken"] = _text(page_token, 8192)
        for name, value in (("timeMin", time_min), ("timeMax", time_max)):
            if value is not None:
                params[name] = _timestamp(value)
        return await self._call(
            "GET",
            access_token,
            suffix="/" + quote(_event_id(event_id), safe="") + "/instances",
            params=params,
        )

    async def get_event(self, access_token, event_id):
        return await self._call(
            "GET", access_token, suffix="/" + quote(_event_id(event_id), safe="")
        )

    async def insert_event(self, access_token, event_id, body):
        if not isinstance(event_id, str) or not _INSERT_ID.fullmatch(event_id):
            raise GoogleProtocolError("invalid_event_id")
        body = _write_body(body)
        if "start" not in body or "end" not in body:
            raise GoogleProtocolError("invalid_approved_event")
        return await self._call(
            "POST", access_token, params={"sendUpdates": "none"}, body={**body, "id": event_id}
        )

    async def update_event(self, access_token, event_id, body, *, etag):
        _text(etag, 512)
        body = _write_body(body)
        return await self._call(
            "PATCH",
            access_token,
            suffix="/" + quote(_event_id(event_id), safe=""),
            params={"sendUpdates": "none"},
            body=body,
            etag=etag,
        )
