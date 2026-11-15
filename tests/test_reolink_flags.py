"""Reolink filename flag decoding — vectors from design §10.8."""
from __future__ import annotations

import pytest

from hikvision_recordings.app.reolink_flags import decode_hex_flags, events_from_filename

# Live-probe vectors (design §10.8). Output order follows §3.4.
VECTORS = [
    ("9B288080000000", ("motion",)),
    ("9B28C080000000", ("motion", "person")),
    ("9B28C000000000", ("person",)),
    ("9B28C880000000", ("motion", "person", "animal")),
    ("9B288800000000", ("animal",)),
    ("9B28C800000000", ("person", "animal")),
    ("9B288880000000", ("motion", "animal")),
]


@pytest.mark.parametrize("flags,expected", VECTORS)
def test_live_probe_flag_vectors(flags, expected):
    assert decode_hex_flags(flags) == expected


@pytest.mark.parametrize("flags,expected", VECTORS)
def test_events_from_filename_version_0a(flags, expected):
    name = (
        f"Mp4Record/2026-11-14/RecM0A_20261114_040923_040948_0_{flags}_109CF6A.mp4"
    )
    assert events_from_filename(name) == expected


def test_dst_name_variant_still_decodes():
    name = (
        "Mp4Record/2026-11-01/RecM0A_DST20261101_003727_003751_0_"
        "9B28C080000000_1A2B.mp4"
    )
    assert events_from_filename(name) == ("motion", "person")


def test_unknown_version_returns_empty():
    name = (
        "Mp4Record/2026-11-14/RecM02_20261114_040923_040948_0_"
        "9B288080000000_109CF6A.mp4"
    )
    assert events_from_filename(name) == ()


def test_malformed_hex_returns_empty():
    assert decode_hex_flags("not-hex!!!!!") == ()
    assert decode_hex_flags("ZZZZZZZZZZZZZZ") == ()


def test_hub_nine_field_name_returns_empty():
    name = (
        "Mp4Record/2026-08-27/RecM02_DST20240827_090302_090334_0_800_800_"
        "033C820000_61B6F0.mp4"
    )
    assert events_from_filename(name) == ()


def test_wrong_hex_length_returns_empty():
    name = (
        "Mp4Record/2026-11-14/RecM0A_20261114_040923_040948_0_6D28808_109CF6A.mp4"
    )
    assert events_from_filename(name) == ()
