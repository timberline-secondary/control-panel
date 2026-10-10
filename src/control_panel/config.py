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
class TvsSettings:
    """The TV Pis. They share a login; TV n is at host_pattern with {n} filled in."""

    host_pattern: str = "pi-tv{n}.hackerspace.tbl"
    port: int = 22
    username: str = "pi"
    password: str | None = None
    media_dir: str = "/home/pi/rs_media"  # the folder Raspberry Slideshow plays
    shrines_dir: str = ""  # where shrines are saved; empty means Documents/Hackerspace shrines
    ffmpeg: str = ""  # ffmpeg program to use; empty means download it when it's first needed
    # The Pi that keeps each shrine's original art (with the same login as the TVs), and
    # the folder on its external drive where it's kept.
    art_host: str = "pi-files.hackerspace.tbl"
    art_dir: str = "/mnt/ssd/shrines"

    def pi(self, number: int) -> PiSettings:
        return PiSettings(host=self.host_pattern.format(n=number), port=self.port,
                          username=self.username, password=self.password)

    def art_pi(self) -> PiSettings:
        return PiSettings(host=self.art_host, port=self.port, username=self.username,
                          password=self.password)


@dataclass(frozen=True)
class Config:
    themes: ThemesSettings = field(default_factory=ThemesSettings)
    tvs: TvsSettings = field(default_factory=TvsSettings)


def default_path() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / APP_NAME / "config.toml"


def path() -> Path:
    override = os.environ.get(CONFIG_ENV_VAR)
    return Path(override) if override else default_path()


def known_hosts_path() -> Path:
    """Where each Pi's identity (SSH host key) is remembered."""
    return default_path().parent / "known_hosts"


def load(config_path: Path | None = None) -> Config:
    """config_path: a file the person chose (--config); otherwise see path()."""
    chosen = config_path is not None or bool(os.environ.get(CONFIG_ENV_VAR))
    config_path = config_path or path()
    if not config_path.exists():
        if chosen:  # a typo here shouldn't quietly fall back to the real Pis
            raise ConfigError(f"Couldn't find the config file {config_path}")
        return Config()
    try:
        data = tomllib.loads(_decode(config_path.read_bytes()))
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"Couldn't read the config file {config_path}: {e}") from e
    except UnicodeDecodeError as e:
        raise ConfigError(f"Couldn't read the config file {config_path}: "
                          "save it as UTF-8 text and try again.") from e

    unknown = set(data) - {f.name for f in fields(Config)}
    if unknown:
        raise ConfigError(f"Unknown section(s) in {config_path}: {', '.join(sorted(unknown))}")
    return Config(
        themes=_section(ThemesSettings, data.get("themes", {}), "themes", config_path),
        tvs=_section(TvsSettings, data.get("tvs", {}), "tvs", config_path),
    )


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
        if key == "port" and not 1 <= value <= 65535:
            raise ConfigError(f"Setting 'port' in [{name}] of {config_path} should be "
                              "between 1 and 65535")
    return cls(**values)


def _decode(raw: bytes) -> str:
    # Notepad can save UTF-8 with a BOM, and Windows PowerShell's > and Out-File write UTF-16.
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    return raw.decode("utf-8-sig")
