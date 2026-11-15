"""In-memory map of opaque clip ids → Clip.

This is the trust boundary. The browser never sees a playbackURI and therefore
cannot ask the backend to fetch an arbitrary URL — it can only name an id that
this process minted from a real search result. Nothing here touches disk.

Every ref is re-validated by the rules of the device family that produced it:
a Hikvision playbackURI must point at the configured DVR, a Reolink ref must be a
file name of the exact shape that camera's SD card uses.

A secondary index (kind, camera_key, start, main_ref) reuses the id when the same
clip is listed again (infinite scroll must not mint a fresh id and FIFO-evict
Today's clips).
"""
from __future__ import annotations

import re
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass
from urllib.parse import urlparse

import logging

from .clips import KIND_DVR, KIND_REOLINK, Clip
from .reolink import InvalidReolinkRef, validate_reolink_ref

LOG = logging.getLogger(__name__)

DEFAULT_TTL_S = 3600.0
DEFAULT_CAPACITY = 2000


class ClipExpired(KeyError):
    """The clip id is unknown or older than the TTL — the client must search again."""


class InvalidPlaybackUri(ValueError):
    """A ref that does not point at a configured device. Never fetched."""


@dataclass
class _Entry:
    clip: Clip
    expires_at: float


def _identity(clip: Clip) -> tuple:
    return (clip.kind, clip.camera_key, clip.start, clip.main_ref)


class ClipRegistry:
    def __init__(
        self,
        dvr_host: str | None = None,
        ttl_s: float = DEFAULT_TTL_S,
        capacity: int = DEFAULT_CAPACITY,
    ) -> None:
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self._by_identity: dict[tuple, str] = {}
        self._ttl_s = ttl_s
        self._capacity = capacity
        self._dvr_host = dvr_host
        # Defence in depth: even though every URI here came from the DVR's own
        # search response, re-validate it before it can ever be sent back.
        # Use fullmatch to anchor the entire string (prefix AND suffix).
        # Query string is restricted to conservative allowlist to reject CR/LF/NUL.
        # No DVR configured (Reolink-only install) -> nothing can match.
        self._pattern = re.compile(
            rf"^rtsp://{re.escape(dvr_host)}(?::\d+)?/Streaming/tracks/\d+/\?[A-Za-z0-9=&%._:+-]*\Z"
            if dvr_host else r"(?!)"
        )

    def __len__(self) -> int:
        return len(self._entries)

    def validate_uri(self, uri: str) -> None:
        """Raise InvalidPlaybackUri unless `uri` points at the configured DVR.

        Public because a caller may need to USE a playbackURI without minting a
        clip id for it — the substream lookup behind /api/stream does exactly
        that. It runs the same check here rather than growing a second copy that
        can drift away from this one.
        """
        # Reject CR, LF, NUL, and other dangerous characters outright
        if any(c in uri for c in '\r\n\x00'):
            raise InvalidPlaybackUri(
                "playbackURI contains forbidden characters — refusing to use it"
            )

        # Use fullmatch (not match) to validate the entire URI
        if not self._pattern.fullmatch(uri):
            # Extract the actual host from the URI for a diagnostic message
            try:
                parsed = urlparse(uri)
                actual_host = parsed.hostname or "unknown"
            except Exception:
                actual_host = "unknown"

            raise InvalidPlaybackUri(
                f"playbackURI host '{actual_host}' does not match the configured "
                f"dvr_host '{self._dvr_host}' — if your DVR reports its IP here, "
                f"set dvr_host to that IP"
            )

    def validate_ref(self, kind: str, ref: str) -> None:
        """Raise InvalidPlaybackUri unless `ref` is valid for its device family."""
        if kind == KIND_DVR:
            self.validate_uri(ref)
        elif kind == KIND_REOLINK:
            try:
                validate_reolink_ref(ref)
            except InvalidReolinkRef as exc:
                raise InvalidPlaybackUri(str(exc)) from exc
        else:
            raise InvalidPlaybackUri(f"unknown device kind {kind!r}")

    def put(self, clip: Clip) -> str:
        self.validate_ref(clip.kind, clip.main_ref)
        if clip.sub_ref is not None:
            try:
                self.validate_ref(clip.kind, clip.sub_ref)
            except InvalidPlaybackUri:
                # A bad substream must not cost the user the clip — it just plays
                # (and previews) from the mainstream instead.
                LOG.warning("dropping an invalid substream ref; using the mainstream")
                clip = clip.with_sub(None, None)
        self._purge()
        identity = _identity(clip)
        existing_id = self._by_identity.get(identity)
        if existing_id is not None and existing_id in self._entries:
            # Same clip again (e.g. infinite-scroll re-list): reuse the id,
            # refresh TTL, move to MRU end, update the Clip (newly paired sub).
            self._entries[existing_id] = _Entry(clip, time.monotonic() + self._ttl_s)
            self._entries.move_to_end(existing_id)
            return existing_id

        clip_id = secrets.token_urlsafe(12)
        self._entries[clip_id] = _Entry(clip, time.monotonic() + self._ttl_s)
        self._by_identity[identity] = clip_id
        while len(self._entries) > self._capacity:
            evicted_id, evicted = self._entries.popitem(last=False)
            self._by_identity.pop(_identity(evicted.clip), None)
        return clip_id

    def get(self, clip_id: str) -> Clip:
        entry = self._entries.get(clip_id)
        if entry is None:
            raise ClipExpired(clip_id)
        if entry.expires_at <= time.monotonic():
            self._entries.pop(clip_id, None)
            self._by_identity.pop(_identity(entry.clip), None)
            raise ClipExpired(clip_id)
        return entry.clip

    def _purge(self) -> None:
        now = time.monotonic()
        for clip_id in [k for k, v in self._entries.items() if v.expires_at <= now]:
            entry = self._entries.pop(clip_id, None)
            if entry is not None:
                self._by_identity.pop(_identity(entry.clip), None)
