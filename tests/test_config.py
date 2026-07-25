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
