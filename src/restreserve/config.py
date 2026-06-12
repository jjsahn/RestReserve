"""Configuration loading: CLI args > env vars > config file > defaults."""

from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

DEFAULT_API_KEY = "VbWk7s3L4KiK5fzlO7JD3Q5EYolJI7n5"  # public resy.com web-client key

ENV_VARS = {
    "email": "RESY_EMAIL",
    "password": "RESY_PASSWORD",
    "auth_token": "RESY_AUTH_TOKEN",
    "api_key": "RESY_API_KEY",
}


class ConfigError(Exception):
    """Raised with a human-readable list of all config problems."""


@dataclass
class SniperConfig:
    # auth
    email: str | None = None
    password: str | None = None
    auth_token: str | None = None  # alternative to email/password
    api_key: str = DEFAULT_API_KEY
    # target
    venue_id: int | None = None
    venue_slug: str | None = None
    location: str | None = None  # e.g. "new-york-ny"
    date: dt.date | None = None
    party_size: int = 2
    window_start: dt.time = dt.time(18, 0)
    window_end: dt.time = dt.time(21, 0)
    ideal_time: dt.time | None = None  # defaults to window midpoint
    table_type: str | None = None  # soft preference
    # timing
    drop_time: dt.datetime | None = None  # tz-aware
    lead_ms: int = 1000
    # polling
    poll_interval_ms: int = 250
    max_duration_s: int = 180
    request_timeout_s: float = 4.0
    # behavior
    dry_run: bool = False

    def __post_init__(self) -> None:
        if self.ideal_time is None:
            mid = (
                _time_to_minutes(self.window_start) + _time_to_minutes(self.window_end)
            ) // 2
            self.ideal_time = dt.time(mid // 60, mid % 60)


def _time_to_minutes(t: dt.time) -> int:
    return t.hour * 60 + t.minute


def _parse_time(value: str, label: str, errors: list[str]) -> dt.time | None:
    try:
        return dt.time.fromisoformat(value)
    except ValueError:
        errors.append(f"{label}: invalid time {value!r} (expected HH:MM)")
        return None


def load_config(
    path: Path | None, overrides: dict[str, Any] | None = None
) -> SniperConfig:
    """Build a SniperConfig from file + env + CLI overrides; validate everything.

    `overrides` is a flat dict of SniperConfig field names (CLI values);
    None values are ignored.
    """
    errors: list[str] = []
    flat: dict[str, Any] = {}

    if path is not None:
        try:
            raw = json.loads(path.read_text())
        except FileNotFoundError:
            raise ConfigError(f"config file not found: {path}") from None
        except json.JSONDecodeError as e:
            raise ConfigError(f"config file is not valid JSON: {e}") from None
        flat.update(_flatten_file(raw, errors))

    # env vars override file
    for fld, env in ENV_VARS.items():
        if os.environ.get(env):
            flat[fld] = os.environ[env]

    # CLI overrides everything
    for key, value in (overrides or {}).items():
        if value is not None:
            flat[key] = value

    cfg = _build(flat, errors)
    _validate(cfg, errors)
    if errors:
        raise ConfigError("configuration problems:\n  - " + "\n  - ".join(errors))
    return cfg


def _flatten_file(raw: dict, errors: list[str]) -> dict[str, Any]:
    """Map the nested config.json layout onto flat SniperConfig fields."""
    flat: dict[str, Any] = {}
    resy = raw.get("resy", {})
    for key in ("email", "password", "auth_token", "api_key"):
        if resy.get(key) is not None:
            flat[key] = resy[key]

    target = raw.get("target", {})
    for key in ("venue_id", "venue_slug", "location", "party_size", "table_type"):
        if target.get(key) is not None:
            flat[key] = target[key]
    if target.get("date"):
        flat["date"] = str(target["date"])
    for key in ("window_start", "window_end", "ideal_time"):
        if target.get(key):
            flat[key] = str(target[key])

    drop = raw.get("drop", {})
    if drop.get("time"):
        flat["drop_time"] = str(drop["time"])
        if drop.get("timezone"):
            flat["drop_timezone"] = str(drop["timezone"])
    if drop.get("lead_ms") is not None:
        flat["lead_ms"] = drop["lead_ms"]

    polling = raw.get("polling", {})
    for src, dst in (
        ("interval_ms", "poll_interval_ms"),
        ("max_duration_s", "max_duration_s"),
        ("request_timeout_s", "request_timeout_s"),
    ):
        if polling.get(src) is not None:
            flat[dst] = polling[src]
    return flat


def _build(flat: dict[str, Any], errors: list[str]) -> SniperConfig:
    kwargs: dict[str, Any] = {}

    for key in ("email", "password", "auth_token", "api_key", "venue_slug",
                "location", "table_type"):
        if flat.get(key) is not None:
            kwargs[key] = str(flat[key])

    if flat.get("venue_id") is not None:
        try:
            kwargs["venue_id"] = int(flat["venue_id"])
        except (TypeError, ValueError):
            errors.append(f"venue_id: not an integer: {flat['venue_id']!r}")

    if flat.get("date") is not None:
        try:
            kwargs["date"] = dt.date.fromisoformat(str(flat["date"]))
        except ValueError:
            errors.append(f"date: invalid date {flat['date']!r} (expected YYYY-MM-DD)")

    for key in ("party_size", "lead_ms", "poll_interval_ms", "max_duration_s"):
        if flat.get(key) is not None:
            try:
                kwargs[key] = int(flat[key])
            except (TypeError, ValueError):
                errors.append(f"{key}: not an integer: {flat[key]!r}")

    if flat.get("request_timeout_s") is not None:
        try:
            kwargs["request_timeout_s"] = float(flat["request_timeout_s"])
        except (TypeError, ValueError):
            errors.append(f"request_timeout_s: not a number: {flat['request_timeout_s']!r}")

    for key in ("window_start", "window_end", "ideal_time"):
        if flat.get(key) is not None:
            parsed = _parse_time(str(flat[key]), key, errors)
            if parsed is not None:
                kwargs[key] = parsed

    if flat.get("drop_time") is not None:
        kwargs["drop_time"] = _parse_drop_time(
            str(flat["drop_time"]), flat.get("drop_timezone"), errors
        )

    if flat.get("dry_run") is not None:
        kwargs["dry_run"] = bool(flat["dry_run"])

    return SniperConfig(**kwargs)


def _parse_drop_time(
    value: str, tz_name: str | None, errors: list[str]
) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        errors.append(f"drop time: invalid datetime {value!r} (expected ISO 8601)")
        return None
    if parsed.tzinfo is None:
        if not tz_name:
            errors.append(
                f"drop time {value!r} has no UTC offset and no timezone configured; "
                'set drop.timezone (e.g. "America/New_York") or include an offset'
            )
            return None
        try:
            parsed = parsed.replace(tzinfo=ZoneInfo(tz_name))
        except KeyError:
            errors.append(f"drop timezone: unknown IANA timezone {tz_name!r}")
            return None
    return parsed


def _validate(cfg: SniperConfig, errors: list[str]) -> None:
    if not cfg.auth_token and not (cfg.email and cfg.password):
        errors.append(
            "credentials missing: set resy.email + resy.password (or RESY_EMAIL/"
            "RESY_PASSWORD), or an auth_token"
        )
    if cfg.venue_id is None and not cfg.venue_slug:
        errors.append("venue missing: set target.venue_id or target.venue_slug")
    if cfg.venue_id is None and cfg.venue_slug and not cfg.location:
        errors.append("target.location is required when using venue_slug")
    if cfg.date is None:
        errors.append("target.date is required (YYYY-MM-DD)")
    if cfg.party_size < 1:
        errors.append(f"party_size must be >= 1, got {cfg.party_size}")
    if cfg.window_end <= cfg.window_start:
        errors.append(
            f"window_end ({cfg.window_end}) must be after window_start ({cfg.window_start})"
        )
    if cfg.drop_time is None:
        errors.append("drop.time is required (when slots open)")
    if cfg.poll_interval_ms < 50:
        errors.append(f"poll interval must be >= 50ms, got {cfg.poll_interval_ms}")
    if cfg.lead_ms < 0:
        errors.append(f"lead_ms must be >= 0, got {cfg.lead_ms}")
