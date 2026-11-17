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
    assert first.playback_uri.startswith("rtsp://192.0.2.56/Streaming/tracks/101/")
    assert "&amp;" not in first.playback_uri, "XML entities must be decoded"


def test_parse_no_matches_returns_empty(search_no_match):
    assert parse_search_response(search_no_match, channel=101, to_utc=IDENTITY) == []


def test_parse_bad_xml_content_raises_with_uuid_hint(bad_xml_content):
    with pytest.raises(DvrBadRequest) as exc:
        parse_search_response(bad_xml_content, channel=101, to_utc=IDENTITY)
    message = str(exc.value)
    assert "searchID" in message and "uuid" in message.lower()


SEARCH_WITH_EVENTS = """<?xml version="1.0" encoding="UTF-8"?>
<CMSearchResult xmlns="http://www.hikvision.com/ver20/XMLSchema">
<responseStatus>true</responseStatus>
<responseStatusStrg>OK</responseStatusStrg>
<numOfMatches>2</numOfMatches>
<matchList>
<searchMatchItem>
<trackID>101</trackID>
<timeSpan><startTime>2026-07-24T13:20:29Z</startTime><endTime>2026-07-24T13:21:42Z</endTime></timeSpan>
<mediaSegmentDescriptor>
<playbackURI>rtsp://192.0.2.10/Streaming/tracks/101/?starttime=20260724T132029Z&amp;size=100</playbackURI>
</mediaSegmentDescriptor>
<metadataMatches>
<metadataDescriptor>recordType.meta.std-cgi.com/motion</metadataDescriptor>
</metadataMatches>
</searchMatchItem>
<searchMatchItem>
<trackID>101</trackID>
<timeSpan><startTime>2026-07-24T13:25:00Z</startTime><endTime>2026-07-24T13:26:00Z</endTime></timeSpan>
<mediaSegmentDescriptor>
<playbackURI>rtsp://192.0.2.10/Streaming/tracks/101/?starttime=20260724T132500Z&amp;size=200</playbackURI>
</mediaSegmentDescriptor>
<metadataMatches>
<metadataDescriptor>recordType.meta.std-cgi.com/CMR</metadataDescriptor>
<metadataDescriptor>recordType.meta.std-cgi.com/unknownThing</metadataDescriptor>
</metadataMatches>
</searchMatchItem>
</matchList>
</CMSearchResult>"""


def test_parse_reads_metadata_matches_into_events():
    recs = parse_search_response(SEARCH_WITH_EVENTS, channel=101, to_utc=IDENTITY)
    assert recs[0].events == ("motion",)
    assert recs[1].events == ("scheduled",)


def test_parse_without_metadata_defaults_to_empty_events(search_ok):
    recs = parse_search_response(search_ok, channel=101, to_utc=IDENTITY)
    assert all(r.events == () for r in recs)


def test_search_body_still_has_no_metadata_list():
    """Do not send metadataList — firmware rejects some descriptors (design §2)."""
    body = build_search_body(101, "2026-07-24T13:00:00Z", "2026-07-24T14:00:00Z", 40)
    assert "metadataList" not in body
    assert "metadataDescriptor" not in body
