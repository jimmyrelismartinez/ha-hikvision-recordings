"""Decode Reolink SD-card filename event flags.

Bit positions and the reverse-then-mask scheme come from Home Assistant's
`reolink_aio/typings.py` (`FLAGS_CAM_V2`, `decode_hex_to_flags`) — this module is
deliberately a tiny copy, not a dependency. A wrong "person" label is worse than
none, so unknown versions / shapes return () instead of falling back to a newer
layout the way reolink_aio does.

Restricted (design §10.8): only basenames with 7 underscore-separated fields,
version hex `09` or `0A` (chars 4–5 of the first field), and a 14-hex-digit
flags word in the 6th field.
"""
from __future__ import annotations

import re

# Closed vocabulary, fixed output order (design §3.4).
EVENT_ORDER = (
    "motion",
    "person",
    "vehicle",
    "animal",
    "face",
    "doorbell",
    "scheduled",
    "alarm",
)

# (bit_position, event_name) — 1-bit flags after reverse, per FLAGS_CAM_V2.
_FLAG_BITS = (
    (17, "person"),
    (18, "face"),
    (19, "vehicle"),
    (20, "animal"),
    (23, "scheduled"),
    (24, "motion"),
    (26, "doorbell"),
)

_ALLOWED_VERSIONS = frozenset({"09", "0A", "0a"})
_BASENAME_RE = re.compile(
    r"^(Rec[MS][0-9A-Fa-f]{2})"
    r"_((?:DST)?\d{8})"
    r"_(\d{6})"
    r"_(\d{6})"
    r"_([0-9A-Za-z]+)"
    r"_([0-9A-Fa-f]{14})"
    r"_([0-9A-Fa-f]+)$"
)


def decode_hex_flags(hex_flags: str) -> tuple[str, ...]:
    """Decode a 14-hex-digit flags word into the closed event vocabulary.

    Follows reolink_aio: reverse the bit string of the whole word, then read the
    named bit positions. For 1-bit fields the per-segment re-reverse is a no-op.
    """
    try:
        hex_int = int(hex_flags, 16)
    except (TypeError, ValueError):
        return ()
    width = len(hex_flags) * 4
    hex_int_rev = int(bin(hex_int)[2:].zfill(width)[::-1], 2)
    found: set[str] = set()
    for bit, name in _FLAG_BITS:
        if (hex_int_rev >> bit) & 1:
            found.add(name)
    return tuple(e for e in EVENT_ORDER if e in found)


def events_from_filename(name: str) -> tuple[str, ...]:
    """Event labels for a Reolink recording path or basename, or ()."""
    if not isinstance(name, str) or not name:
        return ()
    basename = name.rsplit("/", 1)[-1]
    # Strip extension if present.
    if basename.lower().endswith(".mp4"):
        basename = basename[:-4]
    match = _BASENAME_RE.fullmatch(basename)
    if not match:
        return ()
    head, _date, _start, _end, _animal, flags, _size = match.groups()
    version = head[4:6]
    if version not in _ALLOWED_VERSIONS:
        return ()
    return decode_hex_flags(flags)
