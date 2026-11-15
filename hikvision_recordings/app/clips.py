"""The device-neutral clip record the API, registry and frontend all deal in.

A Hikvision DVR and a Reolink camera describe a recording completely differently
(an rtsp:// playbackURI vs an SD-card file name), so everything above the device
clients carries a Clip whose `main_ref` / `sub_ref` are opaque to all but the
backend that minted them. The browser never sees either — only a registry id.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta

KIND_DVR = "dvr"
KIND_REOLINK = "reolink"

# Main and sub recordings of the same event start/stop at slightly different
# instants (a second or two on both device families), so a sub search window is
# padded before matching by overlap.
SUB_WINDOW_PAD = timedelta(seconds=30)


@dataclass(frozen=True)
class Clip:
    camera_key: str          # "dvr-101", "reolink-0"
    kind: str                # KIND_DVR | KIND_REOLINK
    channel: int             # DVR channel (101) / Reolink channel (0)
    start: datetime          # tz-aware UTC
    end: datetime            # tz-aware UTC
    size_bytes: int
    main_ref: str            # Hikvision playbackURI / Reolink file name
    sub_ref: str | None = None
    sub_size_bytes: int | None = None

    @property
    def duration_s(self) -> int:
        return int((self.end - self.start).total_seconds())

    def with_sub(self, ref: str | None, size: int | None) -> "Clip":
        return replace(self, sub_ref=ref, sub_size_bytes=size)


def best_overlap(start: datetime, end: datetime, candidates, key=lambda c: (c.start, c.end)):
    """The candidate whose span overlaps [start, end] the most, or None.

    A padded window can legitimately catch the neighbouring event too, so the
    first candidate is not good enough — the same rule /api/stream has always used.
    """
    best = None
    best_overlap_td = timedelta(0)
    for candidate in candidates:
        c_start, c_end = key(candidate)
        overlap = min(c_end, end) - max(c_start, start)
        if overlap > best_overlap_td:
            best, best_overlap_td = candidate, overlap
    return best
