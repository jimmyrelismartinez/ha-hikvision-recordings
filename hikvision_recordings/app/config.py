"""Add-on options → typed config.

Supervisor renders the `options`/`schema` blocks in config.yaml into
/data/options.json inside the container. Nothing about the DVR is hardcoded:
the same image installs on any HA instance.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

DEFAULT_OPTIONS_PATH = "/data/options.json"
LOG = logging.getLogger(__name__)

MINT_TOKEN_MIN_LEN = 24
LINK_TTL_DEFAULT = 300
LINK_TTL_MIN = 60
LINK_TTL_MAX = 900
# "auto"     = measure the offset against the device clock (correct on DVR-THD30B-81-HIK)
# "utc"      = trust 'Z' as true UTC, apply no shift
# "local"    = apply the HOST's local offset
# "declared" = apply the UTC offset the device declares in <localTime> (the old default;
#              wrong under DST on this hardware — kept only as an escape hatch)
VALID_TIME_MODES = ("auto", "utc", "local", "declared")

# HA entity id pasted into a URL that carries the Supervisor token — keep tight.
HA_CAMERA_RE = re.compile(r"camera\.[a-z0-9_]{1,64}")


class ConfigError(ValueError):
    """Add-on options are missing or invalid. The add-on refuses to start."""


@dataclass(frozen=True)
class Channel:
    id: int
    name: str
    ha_camera: str | None = None


@dataclass(frozen=True)
class ReolinkCamera:
    """A standalone Reolink camera that records to its own SD card (HTTP API, not ISAPI)."""

    name: str
    host: str
    username: str
    password: str = field(repr=False)
    port: int = 80
    use_https: bool = False
    verify_ssl: bool = False
    channel: int = 0
    ha_camera: str | None = None

    @property
    def base_url(self) -> str:
        scheme = "https" if self.use_https else "http"
        return f"{scheme}://{self.host}:{self.port}"


@dataclass(frozen=True)
class Config:
    dvr_host: str
    dvr_username: str
    dvr_password: str = field(repr=False)
    channels: tuple[Channel, ...]
    dvr_port: int = 80
    dvr_use_https: bool = False
    dvr_time_mode: str = "auto"
    max_results: int = 40
    max_concurrent_downloads: int = 2
    max_stage_mb: int = 256
    client_remux_max_mb: int = 128
    log_level: str = "info"
    # Hard cap on clips per camera per search, across all time-cursor pages. Bounds
    # DVR load and the size of one response; the old max_results is the PAGE size.
    max_search_results: int = 500
    reolink_cameras: tuple[ReolinkCamera, ...] = ()
    # External playback links (v0.7). Invalid values never raise — they disable
    # the link listener and make mint return 503, so the Ingress UI still loads.
    external_links: bool = False
    external_mint_token: str = field(default="", repr=False)
    external_link_ttl_s: int = LINK_TTL_DEFAULT
    external_base_url: str = ""
    external_links_ok: bool = False

    @property
    def dvr_configured(self) -> bool:
        """False in a Reolink-only install (blank dvr_host)."""
        return bool(self.dvr_host)

    @property
    def base_url(self) -> str:
        scheme = "https" if self.dvr_use_https else "http"
        return f"{scheme}://{self.dvr_host}:{self.dvr_port}"


def _normalize_base_url(raw: str) -> str | None:
    """Return a cleaned http(s) origin, or None if invalid.

    http/https only; no path, query or fragment; trailing slash stripped.
    """
    value = str(raw or "").strip()
    if not value:
        return None
    try:
        parsed = urlparse(value)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https"):
        return None
    if not parsed.netloc or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        return None
    if parsed.username or parsed.password:
        return None
    return f"{parsed.scheme}://{parsed.netloc}"


def _link_options(raw: dict) -> tuple[bool, str, int, str, bool]:
    """Parse link options without raising. Returns (enabled, token, ttl, base, ok)."""
    enabled = bool(raw.get("external_links", False))
    token = str(raw.get("external_mint_token", "") or "")
    try:
        ttl = int(raw.get("external_link_ttl_s", LINK_TTL_DEFAULT))
    except (TypeError, ValueError):
        ttl = -1
    base_raw = str(raw.get("external_base_url", "") or "").strip()
    base = _normalize_base_url(base_raw) or ""

    if not enabled:
        # Keep a sane ttl for introspection even when off.
        if ttl < LINK_TTL_MIN or ttl > LINK_TTL_MAX:
            ttl = LINK_TTL_DEFAULT
        return False, token, ttl, base, False

    problems: list[str] = []
    if len(token) < MINT_TOKEN_MIN_LEN:
        problems.append(
            f"external_mint_token must be at least {MINT_TOKEN_MIN_LEN} characters"
        )
    if ttl < LINK_TTL_MIN or ttl > LINK_TTL_MAX:
        problems.append(
            f"external_link_ttl_s must be between {LINK_TTL_MIN} and {LINK_TTL_MAX}"
        )
        ttl = LINK_TTL_DEFAULT
    if not base:
        problems.append(
            "external_base_url must be an http(s) origin with no path, query or fragment"
        )
    if problems:
        for msg in problems:
            LOG.error("external links misconfigured: %s", msg)
        return True, token, ttl, base, False
    return True, token, ttl, base, True


def _required(raw: dict, key: str) -> str:
    value = str(raw.get(key, "") or "").strip()
    if not value:
        raise ConfigError(f"{key} is empty — set it in the add-on configuration")
    return value


def _ha_camera(row: dict, where: str) -> str | None:
    """Optional HA entity id. Empty/whitespace == absent. Reject anything not camera.*.

    Validated with fullmatch on the exact string (no strip), so values like
    `camera.x\\n` or `camera.x/../states` cannot sneak through whitespace trimming.
    """
    raw = row.get("ha_camera", None)
    if raw is None:
        return None
    value = str(raw)
    if not value.strip():
        return None
    if not HA_CAMERA_RE.fullmatch(value):
        raise ConfigError(
            f"{where}.ha_camera must be a camera entity id "
            f"(camera.name_with_underscores), got {value!r}"
        )
    return value


def _reolink_cameras(raw: dict) -> tuple[ReolinkCamera, ...]:
    cameras = []
    for index, row in enumerate(raw.get("reolink_cameras") or []):
        where = f"reolink_cameras[{index}]"
        name = str(row.get("name", "") or "").strip()
        host = str(row.get("host", "") or "").strip()
        username = str(row.get("username", "") or "").strip()
        password = str(row.get("password", "") or "")
        for key, value in (("name", name), ("host", host),
                           ("username", username), ("password", password)):
            if not value:
                raise ConfigError(f"{where}.{key} is empty — set it in the add-on configuration")
        cameras.append(
            ReolinkCamera(
                name=name,
                host=host,
                username=username,
                password=password,
                port=int(row.get("port") or 80),
                use_https=bool(row.get("use_https", False)),
                verify_ssl=bool(row.get("verify_ssl", False)),
                channel=int(row.get("channel") or 0),
                ha_camera=_ha_camera(row, where),
            )
        )
    return tuple(cameras)


def load_config(raw: dict) -> Config:
    reolink = _reolink_cameras(raw)
    host = str(raw.get("dvr_host", "") or "").strip()

    if host or not reolink:
        # The DVR stays mandatory unless at least one Reolink camera is configured,
        # so an existing install with a blank field still fails loudly, as before.
        host = _required(raw, "dvr_host")
        username = _required(raw, "dvr_username")
        password = _required(raw, "dvr_password")
        channels = []
        for index, c in enumerate(raw.get("channels") or []):
            where = f"channels[{index}]"
            channels.append(
                Channel(
                    id=int(c["id"]),
                    name=str(c["name"]).strip(),
                    ha_camera=_ha_camera(c, where),
                )
            )
        channels = tuple(channels)
        if not channels:
            raise ConfigError(
                "no channels configured — add at least one channel row "
                "(id like 101, name like 'Driveway')"
            )
    else:
        # Reolink-only install: no DVR backend is created and `channels` is ignored.
        username = password = ""
        channels = ()

    time_mode = str(raw.get("dvr_time_mode", "auto"))
    if time_mode not in VALID_TIME_MODES:
        raise ConfigError(f"dvr_time_mode must be one of {VALID_TIME_MODES}")

    links_on, mint_token, link_ttl, base_url, links_ok = _link_options(raw)

    return Config(
        dvr_host=host,
        dvr_username=username,
        dvr_password=password,
        channels=channels,
        dvr_port=int(raw.get("dvr_port", 80)),
        dvr_use_https=bool(raw.get("dvr_use_https", False)),
        dvr_time_mode=time_mode,
        max_results=int(raw.get("max_results", 40)),
        max_concurrent_downloads=int(raw.get("max_concurrent_downloads", 2)),
        max_stage_mb=int(raw.get("max_stage_mb", 256)),
        client_remux_max_mb=int(raw.get("client_remux_max_mb", 128)),
        log_level=str(raw.get("log_level", "info")),
        max_search_results=int(raw.get("max_search_results", 500)),
        reolink_cameras=reolink,
        external_links=links_on,
        external_mint_token=mint_token,
        external_link_ttl_s=link_ttl,
        external_base_url=base_url,
        external_links_ok=links_ok,
    )


def load_config_from_file(path: str | None = None) -> Config:
    path = path or os.environ.get("ADDON_OPTIONS_PATH", DEFAULT_OPTIONS_PATH)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError as exc:
        raise ConfigError(f"options file not found at {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"options file at {path} is not valid JSON: {exc}") from exc
    return load_config(raw)
