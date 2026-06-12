import datetime as dt
import json

import pytest

from restreserve.config import DEFAULT_API_KEY, ConfigError, SniperConfig, load_config

VALID_FILE = {
    "resy": {"email": "a@b.com", "password": "pw"},
    "target": {
        "venue_id": 1234,
        "date": "2026-07-04",
        "party_size": 2,
        "window_start": "18:30",
        "window_end": "20:00",
    },
    "drop": {"time": "2026-06-13T09:00:00", "timezone": "America/New_York"},
}


def write_config(tmp_path, data):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data))
    return path


def test_load_valid_file(tmp_path):
    cfg = load_config(write_config(tmp_path, VALID_FILE), {})
    assert cfg.email == "a@b.com"
    assert cfg.venue_id == 1234
    assert cfg.date == dt.date(2026, 7, 4)
    assert cfg.window_start == dt.time(18, 30)
    assert cfg.api_key == DEFAULT_API_KEY
    assert cfg.drop_time.tzinfo is not None
    # 09:00 America/New_York in June is 13:00 UTC
    assert cfg.drop_time.astimezone(dt.timezone.utc).hour == 13


def test_ideal_time_defaults_to_window_midpoint(tmp_path):
    cfg = load_config(write_config(tmp_path, VALID_FILE), {})
    assert cfg.ideal_time == dt.time(19, 15)


def test_cli_overrides_file(tmp_path):
    cfg = load_config(
        write_config(tmp_path, VALID_FILE),
        {"party_size": 4, "date": "2026-08-01", "dry_run": True},
    )
    assert cfg.party_size == 4
    assert cfg.date == dt.date(2026, 8, 1)
    assert cfg.dry_run is True


def test_env_overrides_file(tmp_path, monkeypatch):
    monkeypatch.setenv("RESY_EMAIL", "env@example.com")
    monkeypatch.setenv("RESY_API_KEY", "env-key")
    cfg = load_config(write_config(tmp_path, VALID_FILE), {})
    assert cfg.email == "env@example.com"
    assert cfg.api_key == "env-key"


def test_cli_overrides_env(tmp_path, monkeypatch):
    monkeypatch.setenv("RESY_EMAIL", "env@example.com")
    cfg = load_config(write_config(tmp_path, VALID_FILE), {"email": "cli@example.com"})
    assert cfg.email == "cli@example.com"


def test_missing_credentials(tmp_path):
    data = {k: v for k, v in VALID_FILE.items() if k != "resy"}
    with pytest.raises(ConfigError, match="credentials missing"):
        load_config(write_config(tmp_path, data), {})


def test_auth_token_alone_suffices(tmp_path):
    data = dict(VALID_FILE, resy={"auth_token": "tok"})
    cfg = load_config(write_config(tmp_path, data), {})
    assert cfg.auth_token == "tok"


def test_missing_venue(tmp_path):
    data = dict(VALID_FILE, target={k: v for k, v in VALID_FILE["target"].items()
                                    if k != "venue_id"})
    with pytest.raises(ConfigError, match="venue missing"):
        load_config(write_config(tmp_path, data), {})


def test_slug_requires_location(tmp_path):
    target = {k: v for k, v in VALID_FILE["target"].items() if k != "venue_id"}
    target["venue_slug"] = "some-place"
    with pytest.raises(ConfigError, match="location is required"):
        load_config(write_config(tmp_path, dict(VALID_FILE, target=target)), {})


def test_inverted_window(tmp_path):
    target = dict(VALID_FILE["target"], window_start="20:00", window_end="18:00")
    with pytest.raises(ConfigError, match="window_end"):
        load_config(write_config(tmp_path, dict(VALID_FILE, target=target)), {})


def test_naive_drop_time_without_timezone(tmp_path):
    data = dict(VALID_FILE, drop={"time": "2026-06-13T09:00:00"})
    with pytest.raises(ConfigError, match="no UTC offset"):
        load_config(write_config(tmp_path, data), {})


def test_drop_time_with_offset_needs_no_timezone(tmp_path):
    data = dict(VALID_FILE, drop={"time": "2026-06-13T09:00:00-04:00"})
    cfg = load_config(write_config(tmp_path, data), {})
    assert cfg.drop_time.utcoffset() == dt.timedelta(hours=-4)


def test_multiple_errors_reported_together(tmp_path):
    data = {"resy": {}, "target": {}, "drop": {}}
    with pytest.raises(ConfigError) as exc:
        load_config(write_config(tmp_path, data), {})
    message = str(exc.value)
    assert "credentials missing" in message
    assert "venue missing" in message
    assert "date is required" in message
    assert "drop.time is required" in message


def test_missing_file():
    from pathlib import Path

    with pytest.raises(ConfigError, match="not found"):
        load_config(Path("/nonexistent/config.json"), {})


def test_invalid_json(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{not json")
    with pytest.raises(ConfigError, match="not valid JSON"):
        load_config(path, {})


def test_no_file_cli_only():
    cfg = load_config(
        None,
        {
            "email": "a@b.com",
            "password": "pw",
            "venue_id": 99,
            "date": "2026-07-04",
            "drop_time": "2026-06-13T09:00:00+00:00",
        },
    )
    assert cfg.venue_id == 99


def test_poll_interval_floor(tmp_path):
    data = dict(VALID_FILE, polling={"interval_ms": 10})
    with pytest.raises(ConfigError, match="poll interval"):
        load_config(write_config(tmp_path, data), {})


def test_defaults():
    cfg = SniperConfig()
    assert cfg.poll_interval_ms == 250
    assert cfg.lead_ms == 1000
    assert cfg.max_duration_s == 180
    assert cfg.ideal_time == dt.time(19, 30)  # midpoint of 18:00-21:00
