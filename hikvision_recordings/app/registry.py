"""In-memory map of opaque clip ids → Recording.

This is the trust boundary. The browser never sees a playbackURI and therefore
cannot ask the backend to fetch an arbitrary URL — it can only name an id that
this process minted from a real search result. Nothing here touches disk.
"""
from __future__ import annotations

import re
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass
from urllib.parse import urlparse

from .isapi import Recording

DEFAULT_TTL_S = 3600.0
DEFAULT_CAPACITY = 500


class ClipExpired(KeyError):
    """The clip id is unknown or older than the TTL — the client must search again."""


class InvalidPlaybackUri(ValueError):
    """A playbackURI that does not point at the configured DVR. Never fetched."""


@dataclass
class _Entry:
    recording: Recording
    expires_at: float


class ClipRegistry:
    def __init__(
        self,
        dvr_host: str,
        ttl_s: float = DEFAULT_TTL_S,
        capacity: int = DEFAULT_CAPACITY,
    ) -> None:
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self._ttl_s = ttl_s
        self._capacity = capacity
        self._dvr_host = dvr_host
        # Defence in depth: even though every URI here came from the DVR's own
        # search response, re-validate it before it can ever be sent back.
        # Use fullmatch to anchor the entire string (prefix AND suffix).
        # Query string is restricted to conservative allowlist to reject CR/LF/NUL.
        self._pattern = re.compile(
            rf"^rtsp://{re.escape(dvr_host)}(?::\d+)?/Streaming/tracks/\d+/\?[A-Za-z0-9=&%._:+-]*\Z"
        )

    def __len__(self) -> int:
        return len(self._entries)

    def put(self, recording: Recording) -> str:
        uri = recording.playback_uri

        # Reject CR, LF, NUL, and other dangerous characters outright
        if any(c in uri for c in '\r\n\x00'):
            raise InvalidPlaybackUri(
                f"playbackURI contains forbidden characters — refusing to store it"
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

        self._purge()
        clip_id = secrets.token_urlsafe(12)
        self._entries[clip_id] = _Entry(recording, time.monotonic() + self._ttl_s)
        while len(self._entries) > self._capacity:
            self._entries.popitem(last=False)
        return clip_id

    def get(self, clip_id: str) -> Recording:
        entry = self._entries.get(clip_id)
        if entry is None:
            raise ClipExpired(clip_id)
        if entry.expires_at <= time.monotonic():
            self._entries.pop(clip_id, None)
            raise ClipExpired(clip_id)
        return entry.recording

    def _purge(self) -> None:
        now = time.monotonic()
        for clip_id in [k for k, v in self._entries.items() if v.expires_at <= now]:
            self._entries.pop(clip_id, None)
