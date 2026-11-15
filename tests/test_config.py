import json

import pytest

from hikvision_recordings.app.config import (
    Channel,
    Config,
    ConfigError,
    load_config,
    load_config_from_file,
)

VALID = {
    "dvr_host": "10.10.11.56",
    "dvr_port": 80,
    "dvr_username": "Jimmy",
    "dvr_password": "s3cret",
    "dvr_use_https": False,
    "dvr_time_mode": "auto",
    "channels": [{"id": 101, "name": "DriveWay1"}, {"id": 401, "name": "ENTRYWAY"}],
    "max_results": 40,
    "max_concurrent_downloads": 2,
    "client_remux_max_mb": 128,
    "log_level": "info",
}


def test_loads_valid_options():
    cfg = load_config(VALID)
    assert cfg.dvr_host == "10.10.11.56"
    assert cfg.channels == (Channel(101, "DriveWay1"), Channel(401, "ENTRYWAY"))
    assert cfg.base_url == "http://10.10.11.56:80"


def test_https_changes_base_url():
    cfg = load_config({**VALID, "dvr_use_https": True, "dvr_port": 443})
    assert cfg.base_url == "https://10.10.11.56:443"


def test_defaults_are_applied():
    minimal = {
        "dvr_host": "1.2.3.4",
        "dvr_username": "u",
        "dvr_password": "p",
        "channels": [{"id": 101, "name": "Cam"}],
    }
    cfg = load_config(minimal)
    assert cfg.dvr_port == 80
    assert cfg.max_results == 40
    assert cfg.max_concurrent_downloads == 2
    assert cfg.dvr_time_mode == "auto"
    assert cfg.client_remux_max_mb == 128


def test_client_remux_max_mb_defaults_to_128_when_omitted():
    without_key = {k: v for k, v in VALID.items() if k != "client_remux_max_mb"}
    cfg = load_config(without_key)
    assert cfg.client_remux_max_mb == 128


def test_client_remux_max_mb_round_trips():
    cfg = load_config({**VALID, "client_remux_max_mb": 256})
    assert cfg.client_remux_max_mb == 256


@pytest.mark.parametrize("field", ["dvr_host", "dvr_username", "dvr_password"])
def test_missing_required_field_raises(field):
    broken = {**VALID, field: ""}
    with pytest.raises(ConfigError) as exc:
        load_config(broken)
    assert field in str(exc.value)


def test_no_channels_raises():
    with pytest.raises(ConfigError) as exc:
        load_config({**VALID, "channels": []})
    assert "channel" in str(exc.value).lower()


def test_bad_time_mode_raises():
    with pytest.raises(ConfigError):
        load_config({**VALID, "dvr_time_mode": "sometimes"})


def test_load_from_file(tmp_path):
    path = tmp_path / "options.json"
    path.write_text(json.dumps(VALID), encoding="utf-8")
    cfg = load_config_from_file(str(path))
    assert isinstance(cfg, Config)
    assert cfg.dvr_username == "Jimmy"


def test_missing_file_raises(tmp_path):
    with pytest.raises(ConfigError):
        load_config_from_file(str(tmp_path / "nope.json"))


def test_password_is_not_in_repr():
    cfg = load_config(VALID)
    assert "s3cret" not in repr(cfg)
    assert cfg.dvr_password == "s3cret"  # still readable by the code that needs it


# ── v0.2.0: Reolink cameras, optional DVR ────────────────────────────────────

REOLINK = {"name": "Porch", "host": "192.0.2.40", "username": "viewer", "password": "pw"}


def test_existing_options_without_reolink_still_load():
    cfg = load_config(VALID)
    assert cfg.reolink_cameras == ()
    assert cfg.dvr_configured
    assert cfg.max_search_results == 500


def test_reolink_rows_get_defaults():
    cfg = load_config({**VALID, "reolink_cameras": [REOLINK]})
    cam = cfg.reolink_cameras[0]
    assert (cam.port, cam.use_https, cam.verify_ssl, cam.channel) == (80, False, False, 0)
    assert cam.base_url == "http://192.0.2.40:80"
    assert "pw" not in repr(cam), "password must not appear in repr/logs"


def test_reolink_only_install_is_allowed():
    cfg = load_config({"dvr_host": "", "channels": [], "reolink_cameras": [REOLINK]})
    assert not cfg.dvr_configured
    assert cfg.channels == ()


def test_blank_dvr_without_reolink_still_fails():
    with pytest.raises(ConfigError, match="dvr_host"):
        load_config({**VALID, "dvr_host": ""})


@pytest.mark.parametrize("missing", ["name", "host", "username", "password"])
def test_reolink_row_requires_its_fields(missing):
    row = {**REOLINK, missing: ""}
    with pytest.raises(ConfigError, match=f"reolink_cameras\\[0\\].{missing}"):
        load_config({**VALID, "reolink_cameras": [row]})


def test_configured_dvr_still_requires_credentials_with_reolink_present():
    with pytest.raises(ConfigError, match="dvr_password"):
        load_config({**VALID, "dvr_password": "", "reolink_cameras": [REOLINK]})
