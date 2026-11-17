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


# ── v0.4.0: ha_camera ─────────────────────────────────────────────────────────

def test_ha_camera_accepted_on_channel_and_reolink():
    cfg = load_config({
        **VALID,
        "channels": [{"id": 101, "name": "Driveway", "ha_camera": "camera.driveway"}],
        "reolink_cameras": [{**REOLINK, "ha_camera": "camera.porch"}],
    })
    assert cfg.channels[0].ha_camera == "camera.driveway"
    assert cfg.reolink_cameras[0].ha_camera == "camera.porch"


def test_ha_camera_empty_string_is_absent():
    cfg = load_config({
        **VALID,
        "channels": [{"id": 101, "name": "Garden", "ha_camera": ""}],
    })
    assert cfg.channels[0].ha_camera is None


@pytest.mark.parametrize("bad", [
    "camera.x\n",
    "camera.x/../states",
    "light.x",
    "Camera.porch",
    "camera.UPPER",
    "camera.",
    "camera.this_name_is_way_too_long_for_the_sixty_four_char_limit_abcdefghijk",
])
def test_ha_camera_rejects_hostile_values(bad):
    with pytest.raises(ConfigError, match=r"channels\[0\]\.ha_camera"):
        load_config({
            **VALID,
            "channels": [{"id": 101, "name": "Porch", "ha_camera": bad}],
        })


def test_ha_camera_reject_names_the_reolink_row():
    with pytest.raises(ConfigError, match=r"reolink_cameras\[0\]\.ha_camera"):
        load_config({
            **VALID,
            "reolink_cameras": [{**REOLINK, "ha_camera": "light.porch"}],
        })


# ── v0.7.0: external links (never raise on bad link options) ─────────────────

def test_external_links_defaults_off():
    cfg = load_config(VALID)
    assert cfg.external_links is False
    assert cfg.external_links_ok is False
    assert cfg.external_link_ttl_s == 300


def test_external_links_ok_when_complete():
    cfg = load_config({
        **VALID,
        "external_links": True,
        "external_mint_token": "x" * 24,
        "external_base_url": "http://192.0.2.10:8100/",
        "external_link_ttl_s": 600,
    })
    assert cfg.external_links_ok is True
    assert cfg.external_base_url == "http://192.0.2.10:8100"
    assert cfg.external_link_ttl_s == 600
    assert "x" * 24 not in repr(cfg)


def test_external_links_invalid_never_raises():
    cfg = load_config({
        **VALID,
        "external_links": True,
        "external_mint_token": "short",
        "external_base_url": "http://192.0.2.10:8100/path",
        "external_link_ttl_s": 9999,
    })
    assert cfg.external_links is True
    assert cfg.external_links_ok is False
