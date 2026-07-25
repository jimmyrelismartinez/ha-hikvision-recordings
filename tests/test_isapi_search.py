import re
import uuid
from datetime import datetime, timezone

import pytest

from hikvision_recordings.app.isapi import (
    DvrBadRequest,
    Recording,
    build_search_body,
    parse_search_response,
)

IDENTITY = lambda dt: dt.replace(tzinfo=timezone.utc)  # noqa: E731


def test_search_body_uses_a_real_uuid():
    body = build_search_body(101, "2026-07-24T13:00:00Z", "2026-07-24T14:00:00Z", 40)
    found = re.search(r"<searchID>([^<]+)</searchID>", body)
    assert found, "searchID element missing"
    uuid.UUID(found.group(1))  # raises ValueError if not a real UUID


def test_search_body_uuid_differs_per_call():
    a = build_search_body(101, "2026-07-24T13:00:00Z", "2026-07-24T14:00:00Z", 40)
    b = build_search_body(101, "2026-07-24T13:00:00Z", "2026-07-24T14:00:00Z", 40)
    assert a != b, "a frozen searchID breaks paging and risks firmware rejection"


def test_search_body_contains_required_elements():
    body = build_search_body(401, "2026-07-24T13:00:00Z", "2026-07-24T14:00:00Z", 25, position=25)
    assert "<trackID>401</trackID>" in body
    assert "<startTime>2026-07-24T13:00:00Z</startTime>" in body
    assert "<endTime>2026-07-24T14:00:00Z</endTime>" in body
    assert "<maxResults>25</maxResults>" in body
    assert "<searchResultPosition>25</searchResultPosition>" in body


def test_parse_returns_recordings(search_ok):
    recs = parse_search_response(search_ok, channel=101, to_utc=IDENTITY)
    assert len(recs) == 2
    first = recs[0]
    assert isinstance(first, Recording)
    assert first.channel == 101
    assert first.start == datetime(2026, 7, 24, 13, 20, 29, tzinfo=timezone.utc)
    assert first.duration_s == 73
    assert first.size_bytes == 32947084
    assert first.playback_uri.startswith("rtsp://10.10.11.56/Streaming/tracks/101/")
    assert "&amp;" not in first.playback_uri, "XML entities must be decoded"


def test_parse_no_matches_returns_empty(search_no_match):
    assert parse_search_response(search_no_match, channel=101, to_utc=IDENTITY) == []


def test_parse_bad_xml_content_raises_with_uuid_hint(bad_xml_content):
    with pytest.raises(DvrBadRequest) as exc:
        parse_search_response(bad_xml_content, channel=101, to_utc=IDENTITY)
    message = str(exc.value)
    assert "searchID" in message and "uuid" in message.lower()
