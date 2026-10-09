"""Grade 9 mode: for grade 9 classes, turn off the TVs in the room and mute the entrance
themes, all at once. Turning it off brings them back."""

from __future__ import annotations

import paramiko

from control_panel import login, ui
from control_panel.config import Config
from control_panel.panels import tvs
from control_panel.panels.themes import ThemesPi
from control_panel.ssh import ConnectionFailed

TITLE = "Grade 9 mode (TVs 1-3 off, entrance themes muted)"


def run(config: Config) -> None:
    ui.heading(TITLE)
    engage = ui.choose("Grade 9 mode", [
        ui.choice("Turn it on: TVs 1-3 off, entrance themes muted", True),
        ui.choice("Turn it off: TVs 1-3 on, entrance themes unmuted", False),
        ui.choice("Back", ui.BACK),
    ])
    if engage is None:
        return
    try:
        tvs.set_power(config.tvs, tvs.IN_ROOM, on=not engage)
    except KeyboardInterrupt:
        ui.warning("Cancelled.")
        return
    _set_themes_volume(config, 0 if engage else 100)
    ui.pause()


def _set_themes_volume(config: Config, percent: int) -> None:
    host = config.themes.host
    try:
        connection = login.connect(config.themes)
        if connection is None:
            return
        with connection:
            result = ThemesPi(connection, config.themes).set_volume(percent)
    except ConnectionFailed as e:
        ui.error(f"Entrance themes: {e}")
        return
    except (OSError, paramiko.SSHException) as e:
        ui.error(f"Entrance themes: lost the connection to {host}: {e}")
        return
    except KeyboardInterrupt:
        ui.warning("Cancelled.")
        return
    if result.ok:
        ui.success(f"Entrance themes: {'muted' if percent == 0 else 'unmuted'}.")
    else:
        ui.error(f"Entrance themes: that didn't work: {result.output.strip()}")
