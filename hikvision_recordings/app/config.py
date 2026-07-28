"""Add-on options → typed config.

Supervisor renders the `options`/`schema` blocks in config.yaml into
/data/options.json inside the container. Nothing about the DVR is hardcoded:
the same image installs on any HA instance.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

DEFAULT_OPTIONS_PATH = "/data/options.json"
# "auto"     = measure the offset against the device clock (correct on DVR-THD30B-81-HIK)
# "utc"      = trust 'Z' as true UTC, apply no shift
# "local"    = apply the HOST's local offset
# "declared" = apply the UTC offset the device declares in <localTime> (the old default;
#              wrong under DST on this hardware — kept only as an escape hatch)
VALID_TIME_MODES = ("auto", "utc", "local", "declared")


class ConfigError(ValueError):
    """Add-on options are missing or invalid. The add-on refuses to start."""


@dataclass(frozen=True)
class Channel:
    id: int
    name: str


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

    @property
    def base_url(self) -> str:
        scheme = "https" if self.dvr_use_https else "http"
        return f"{scheme}://{self.dvr_host}:{self.dvr_port}"


def _required(raw: dict, key: str) -> str:
    value = str(raw.get(key, "") or "").strip()
    if not value:
        raise ConfigError(f"{key} is empty — set it in the add-on configuration")
    return value


def load_config(raw: dict) -> Config:
    host = _required(raw, "dvr_host")
    username = _required(raw, "dvr_username")
    password = _required(raw, "dvr_password")

    channels = tuple(
        Channel(id=int(c["id"]), name=str(c["name"]).strip())
        for c in (raw.get("channels") or [])
    )
    if not channels:
        raise ConfigError(
            "no channels configured — add at least one channel row "
            "(id like 101, name like 'Driveway')"
        )

    time_mode = str(raw.get("dvr_time_mode", "auto"))
    if time_mode not in VALID_TIME_MODES:
        raise ConfigError(f"dvr_time_mode must be one of {VALID_TIME_MODES}")

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
