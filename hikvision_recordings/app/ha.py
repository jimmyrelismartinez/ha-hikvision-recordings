"""Home Assistant camera snapshots via the Supervisor API.

An add-on with `homeassistant_api: true` receives SUPERVISOR_TOKEN and can
`GET http://supervisor/core/api/camera_proxy/<entity_id>`. The token is read
once at construction (then removed from the process environment by the app) and
never logged, never returned to the browser.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

import httpx

from .config import HA_CAMERA_RE

LOG = logging.getLogger(__name__)

DEFAULT_TTL_S = 2.0
DEFAULT_TIMEOUT_S = 10.0
DEFAULT_MAX_BYTES = 5 * 1024 * 1024
ALLOWED_TYPES = frozenset({"image/jpeg", "image/png"})


class HaError(Exception):
    """Something went wrong talking to Home Assistant."""


class HaUnreachable(HaError):
    """HA did not return a usable snapshot (timeout, 5xx, bad body)."""


class HaAuthError(HaError):
    """HA rejected the Supervisor token (401/403)."""


class HaNotFound(HaError):
    """HA has no camera entity with that id."""


class HaBadEntity(HaError):
    """entity_id failed the camera.* regex (defence in depth)."""


@dataclass
class _CacheEntry:
    payload: tuple[bytes, str] | None
    error: HaError | None
    expires_at: float


class HaClient:
    """Cached, single-flight snapshot client for HA camera entities."""

    base = "http://supervisor/core/api"

    def __init__(
        self,
        token: str | None,
        ttl_s: float = DEFAULT_TTL_S,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_bytes: int = DEFAULT_MAX_BYTES,
        base_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._token = token or None
        self._ttl_s = ttl_s
        self._max_bytes = max_bytes
        self._cache: dict[str, _CacheEntry] = {}
        self._inflight: dict[str, asyncio.Task] = {}
        # Own client: never follow redirects (SSRF), never pick up proxy env.
        kwargs: dict = {
            "base_url": base_url or self.base,
            "timeout": httpx.Timeout(timeout_s),
            "follow_redirects": False,
            "trust_env": False,
        }
        if transport is not None:
            kwargs["transport"] = transport
        self._http = httpx.AsyncClient(**kwargs)

    @property
    def enabled(self) -> bool:
        return self._token is not None

    async def aclose(self) -> None:
        await self._http.aclose()

    def _validate_entity(self, entity_id: str) -> None:
        if not isinstance(entity_id, str) or not HA_CAMERA_RE.fullmatch(entity_id):
            raise HaBadEntity(f"invalid camera entity id: {entity_id!r}")

    async def snapshot(self, entity_id: str) -> tuple[bytes, str]:
        """Return (image_bytes, content_type). Raises HaError on failure."""
        self._validate_entity(entity_id)
        now = time.monotonic()
        cached = self._cache.get(entity_id)
        if cached is not None and cached.expires_at > now:
            if cached.error is not None:
                raise cached.error
            assert cached.payload is not None
            return cached.payload

        existing = self._inflight.get(entity_id)
        if existing is not None:
            return await asyncio.shield(existing)

        task = asyncio.create_task(
            self._fetch_and_cache(entity_id), name=f"ha-snap-{entity_id}"
        )
        self._inflight[entity_id] = task
        # The task clears its own slot when it finishes — not the first caller,
        # who may disconnect while the shared fetch is still running.
        task.add_done_callback(lambda t, e=entity_id: _finish(self._inflight, e, t))
        return await asyncio.shield(task)

    async def _fetch_and_cache(self, entity_id: str) -> tuple[bytes, str]:
        try:
            payload = await self._do_fetch(entity_id)
        except HaError as exc:
            self._cache[entity_id] = _CacheEntry(
                None, exc, time.monotonic() + self._ttl_s
            )
            raise
        self._cache[entity_id] = _CacheEntry(
            payload, None, time.monotonic() + self._ttl_s
        )
        return payload

    async def _do_fetch(self, entity_id: str) -> tuple[bytes, str]:
        # Re-validate before building the URL (defence in depth).
        self._validate_entity(entity_id)
        headers = {"Authorization": f"Bearer {self._token}"}
        url = f"/camera_proxy/{entity_id}"
        try:
            async with self._http.stream("GET", url, headers=headers) as response:
                status = response.status_code
                if status == 404:
                    raise HaNotFound(
                        f"Home Assistant has no camera `{entity_id}`."
                    )
                if status in (401, 403):
                    LOG.error(
                        "Home Assistant rejected the snapshot request (HTTP %s) — "
                        "the add-on needs homeassistant_api enabled",
                        status,
                    )
                    raise HaAuthError(
                        "Home Assistant did not return a snapshot."
                    )
                if status >= 500 or status != 200:
                    raise HaUnreachable(
                        "Home Assistant did not return a snapshot."
                    )
                content_type = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
                if content_type not in ALLOWED_TYPES:
                    raise HaUnreachable(
                        "Home Assistant did not return a snapshot."
                    )
                chunks: list[bytes] = []
                taken = 0
                async for chunk in response.aiter_bytes():
                    taken += len(chunk)
                    if taken > self._max_bytes:
                        raise HaUnreachable(
                            "Home Assistant did not return a snapshot."
                        )
                    chunks.append(chunk)
                body = b"".join(chunks)
                if not body:
                    raise HaUnreachable(
                        "Home Assistant did not return a snapshot."
                    )
                return body, content_type
        except HaError:
            raise
        except httpx.TimeoutException as exc:
            raise HaUnreachable(
                "Home Assistant did not return a snapshot."
            ) from exc
        except httpx.HTTPError as exc:
            raise HaUnreachable(
                "Home Assistant did not return a snapshot."
            ) from exc


def _finish(inflight: dict, key, task: asyncio.Task) -> None:
    if inflight.get(key) is task:
        inflight.pop(key, None)
    if not task.cancelled():
        task.exception()    # mark retrieved even if every waiter left


def is_valid_ha_camera(value: str) -> bool:
    return bool(isinstance(value, str) and HA_CAMERA_RE.fullmatch(value))
