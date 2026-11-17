"""In-memory link-entry cache for external playback (prefix hiklink-).

Entries are keyed by a random id (referenced from the HMAC token) and de-duplicated
by (camera_key, clip start, main_ref, quality). At most 2 live entries; hard-capped
at 900 s after staging finishes. Staging uses detached single-flight tasks with
asyncio.shield so a cancelled mint never kills a shared stage.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable, Hashable

from .clips import Clip
from .token import mint as mint_token
from .token import new_entry_id, new_signing_key

LOG = logging.getLogger(__name__)

LINK_STAGE_PREFIX = "hiklink-"
LINK_HARD_CAP_S = 900.0
LINK_MAX_ENTRIES = 2
SWEEP_INTERVAL_S = 60.0

# stage_fn(clip, quality, on_path=None) — on_path is called as soon as the
# tempfile exists so in-flight files count as live during orphan sweeps.
OnPath = Callable[[Path], None]
StageFn = Callable[..., Awaitable[Path | None]]


class LinkCacheBusy(Exception):
    """All slots are occupied by in-flight staging; mint should return 503 busy."""


@dataclass
class LinkEntry:
    id: str
    clip: Clip
    quality: str
    path: Path | None  # DVR staged file; None for Reolink
    created: float
    hard_expiry: float
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    error: BaseException | None = None
    staging: bool = False


def _dedup_key(clip: Clip, quality: str) -> tuple:
    return (clip.camera_key, clip.start.isoformat(), clip.main_ref, quality)


class LinkStageCache:
    def __init__(
        self,
        stage_fn: StageFn,
        *,
        max_entries: int = LINK_MAX_ENTRIES,
        hard_cap_s: float = LINK_HARD_CAP_S,
        time_fn: Callable[[], float] | None = None,
    ) -> None:
        self._stage_fn = stage_fn
        self._max_entries = max_entries
        self._hard_cap_s = hard_cap_s
        self._time = time_fn or time.time
        self._key = new_signing_key()
        self._by_id: dict[str, LinkEntry] = {}
        self._by_dedup: dict[tuple, str] = {}
        self._inflight: dict[tuple, asyncio.Task] = {}

    @property
    def signing_key(self) -> bytes:
        return self._key

    def get(self, entry_id: str) -> LinkEntry | None:
        return self._by_id.get(entry_id)

    def sweep(self, staging_dir: Path | None = None) -> None:
        """Drop expired entries and unlink their files. Optionally wipe orphans on disk."""
        now = self._time()
        dead = [eid for eid, e in self._by_id.items() if e.hard_expiry <= now and e.ready.is_set()]
        # Also drop entries that finished with an error and have no waiters interest —
        # failures are not cached, so they should already be gone; belt-and-braces:
        for eid, e in list(self._by_id.items()):
            if e.ready.is_set() and e.error is not None:
                dead.append(eid)
        for eid in dict.fromkeys(dead):
            self._evict(eid)
        if staging_dir is not None:
            # Never delete every hiklink-* here — that would kill live + in-flight
            # stages within one sweep interval. Only remove untracked leftovers.
            try:
                live_paths = {e.path.resolve() for e in self._by_id.values() if e.path}
                for leftover in staging_dir.glob(f"{LINK_STAGE_PREFIX}*"):
                    try:
                        if leftover.resolve() in live_paths:
                            continue
                        leftover.unlink(missing_ok=True)
                        LOG.warning("swept orphaned link stage %s", leftover)
                    except OSError:
                        continue
            except OSError:
                pass

    def cleanup_files(self, staging_dir: Path, older_than: float = 0) -> None:
        """Delete hiklink-* files (startup: older_than=0 deletes all)."""
        now = self._time()
        try:
            for leftover in staging_dir.glob(f"{LINK_STAGE_PREFIX}*"):
                try:
                    if older_than and now - leftover.stat().st_mtime <= older_than:
                        continue
                    leftover.unlink(missing_ok=True)
                except OSError:
                    continue
        except OSError:
            pass

    def _evict(self, entry_id: str) -> None:
        entry = self._by_id.pop(entry_id, None)
        if entry is None:
            return
        key = _dedup_key(entry.clip, entry.quality)
        if self._by_dedup.get(key) == entry_id:
            self._by_dedup.pop(key, None)
        if entry.path is not None:
            try:
                entry.path.unlink(missing_ok=True)
            except OSError as exc:  # pragma: no cover
                LOG.warning("could not remove link stage %s: %s", entry.path, exc)

    def _evict_oldest_if_needed(self) -> None:
        """Free a slot by evicting the oldest *ready* entry. Staging entries stay.

        If every slot is still staging, raise LinkCacheBusy so mint returns 503.
        """
        while len(self._by_id) >= self._max_entries:
            ready = [e for e in self._by_id.values() if e.ready.is_set()]
            if not ready:
                raise LinkCacheBusy("link stage cache busy")
            oldest_id = min(ready, key=lambda e: e.created).id
            self._evict(oldest_id)

    async def acquire(
        self,
        clip: Clip,
        quality: str,
        *,
        needs_stage: bool,
        ttl_s: int,
    ) -> tuple[LinkEntry, str, int]:
        """Stage (if needed), return (entry, token, exp_unix). Failures are not cached."""
        self.sweep()
        key = _dedup_key(clip, quality)
        existing_id = self._by_dedup.get(key)
        if existing_id is not None:
            entry = self._by_id.get(existing_id)
            if entry is not None and entry.hard_expiry > self._time():
                if not entry.ready.is_set():
                    await asyncio.shield(self._wait_ready(entry))
                if entry.error is not None:
                    # Should not linger; evict and fall through to re-stage.
                    self._evict(entry.id)
                elif entry.id not in self._by_id:
                    # Evicted while we waited — do not mint a token for it.
                    if entry.path is not None:
                        try:
                            entry.path.unlink(missing_ok=True)
                        except OSError:
                            pass
                    raise LinkCacheBusy("link stage cache busy")
                else:
                    exp = self._token_exp(entry, ttl_s)
                    token = mint_token(self._key, entry.id, exp)
                    return entry, token, exp

        existing_task = self._inflight.get(key)
        if existing_task is not None:
            entry = await asyncio.shield(existing_task)
            if entry.error is not None:
                raise entry.error
            if entry.id not in self._by_id:
                if entry.path is not None:
                    try:
                        entry.path.unlink(missing_ok=True)
                    except OSError:
                        pass
                raise LinkCacheBusy("link stage cache busy")
            exp = self._token_exp(entry, ttl_s)
            token = mint_token(self._key, entry.id, exp)
            return entry, token, exp

        self._evict_oldest_if_needed()
        entry_id = new_entry_id()
        now = self._time()
        entry = LinkEntry(
            id=entry_id,
            clip=clip,
            quality=quality,
            path=None,
            created=now,
            hard_expiry=now + self._hard_cap_s,  # updated when staging finishes
            staging=needs_stage,
        )
        self._by_id[entry_id] = entry
        self._by_dedup[key] = entry_id

        task = asyncio.create_task(
            self._run_stage(entry, needs_stage),
            name=f"link-stage-{entry_id[:8]}",
        )
        self._inflight[key] = task
        task.add_done_callback(lambda t, k=key: self._finish_inflight(k, t))

        entry = await asyncio.shield(task)
        if entry.error is not None:
            err = entry.error
            self._evict(entry.id)
            raise err
        if entry.id not in self._by_id:
            # Evicted while staging finished — unlink and refuse a token.
            if entry.path is not None:
                try:
                    entry.path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise LinkCacheBusy("link stage cache busy")
        exp = self._token_exp(entry, ttl_s)
        token = mint_token(self._key, entry.id, exp)
        return entry, token, exp

    def _token_exp(self, entry: LinkEntry, ttl_s: int) -> int:
        now = self._time()
        return int(min(now + ttl_s, entry.hard_expiry))

    async def _wait_ready(self, entry: LinkEntry) -> None:
        await entry.ready.wait()

    async def _run_stage(self, entry: LinkEntry, needs_stage: bool) -> LinkEntry:
        try:
            if needs_stage:
                def _on_path(path: Path) -> None:
                    # Record before bytes are written so orphan sweeps keep it live.
                    entry.path = path

                path = await self._stage_fn(entry.clip, entry.quality, _on_path)
                entry.path = path
            finished = self._time()
            entry.hard_expiry = finished + self._hard_cap_s
            entry.created = finished  # eviction order = staging-finished order
        except BaseException as exc:
            entry.error = exc
        finally:
            entry.staging = False
            entry.ready.set()
            # If we finished after being dropped from the cache, never leave the file.
            if entry.id not in self._by_id and entry.path is not None:
                try:
                    entry.path.unlink(missing_ok=True)
                except OSError:
                    pass
                entry.path = None
        return entry

    def _finish_inflight(self, key: Hashable, task: asyncio.Task) -> None:
        current = self._inflight.get(key)
        if current is task:
            self._inflight.pop(key, None)
