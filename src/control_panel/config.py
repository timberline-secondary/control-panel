"""Settings for the control panel.

The defaults match the Hackerspace lab, so most people never need a config
file. To change something, create ``config.toml`` at the path shown by
``control-panel --help`` (or point the CONTROL_PANEL_CONFIG environment
variable at one). Only include the settings you want to change, e.g.::

    [themes]
    host = "192.168.43.50"
"""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

APP_NAME = "hackerspace-control-panel"
CONFIG_ENV_VAR = "CONTROL_PANEL_CONFIG"


class ConfigError(Exception):
    """The config file exists but can't be used; the message says why."""


@dataclass(frozen=True)
class PiSettings:
    """How to log in to a Pi over SSH."""

    host: str
    port: int = 22
    username: str = "pi"
    # Leave unset to be asked (and offered to remember it in the OS keychain).
    password: str | None = None


@dataclass(frozen=True)
class ThemesSettings(PiSettings):
    host: str = "pi-themes.hackerspace.tbl"
    player_dir: str = "/home/pi/themes"  # where themes.py and its .env live
    songs_dir: str = "/mnt/usb0"  # must match THEME_PATH in the player's .env
    python: str = "python"
    mixer_control: str = "Headphone"  # amixer control used to mute/unmute


@dataclass(frozen=True)
class Config:
    themes: ThemesSettings = field(default_factory=ThemesSettings)


def default_path() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / APP_NAME / "config.toml"


def path() -> Path:
    override = os.environ.get(CONFIG_ENV_VAR)
    return Path(override) if override else default_path()


def load(config_path: Path | None = None) -> Config:
    config_path = config_path or path()
    if not config_path.exists():
        return Config()
    try:
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"Couldn't read the config file {config_path}: {e}") from e

    unknown = set(data) - {f.name for f in fields(Config)}
    if unknown:
        raise ConfigError(f"Unknown section(s) in {config_path}: {', '.join(sorted(unknown))}")
    return Config(themes=_section(ThemesSettings, data.get("themes", {}), "themes", config_path))


def _section(cls, values, name: str, config_path: Path):
    if not isinstance(values, dict):
        raise ConfigError(f"[{name}] in {config_path} should be a section of settings")
    known = {f.name for f in fields(cls)}
    unknown = set(values) - known
    if unknown:
        raise ConfigError(
            f"Unknown setting(s) in [{name}] of {config_path}: {', '.join(sorted(unknown))}"
        )
    defaults = cls()
    for key, value in values.items():
        expected = int if isinstance(getattr(defaults, key), int) else str
        if isinstance(value, bool) or not isinstance(value, expected):
            raise ConfigError(
                f"Setting '{key}' in [{name}] of {config_path} should be "
                f"{'a whole number' if expected is int else 'text in quotes'}"
            )
    return cls(**values)
