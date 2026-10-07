"""The main menu."""

from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

from control_panel import __version__, config, ui
from control_panel.panels import themes

PANELS = [themes]
NOT_PORTED_YET = ["TVs"]  # from the legacy control panel (panels/TVs), not moved over yet


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        settings = config.load(args.config)
    except config.ConfigError as e:
        ui.error(str(e))
        return _pause_if_double_clicked(1)
    try:
        _main_menu(settings)
    except KeyboardInterrupt:
        pass
    except Exception:  # last resort, so a double-clicked window doesn't just vanish
        traceback.print_exc()
        ui.error("Sorry, something went wrong. Please report the error above.")
        return _pause_if_double_clicked(1)
    ui.info("Goodbye!")
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="control-panel",
        description="Control panel for the Timberline Hackerspace Raspberry Pis.",
        epilog=f"Optional settings file: {config.path()}",
    )
    parser.add_argument("--config", type=Path, help="use this settings file instead")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser.parse_args(argv)


def _main_menu(settings: config.Config) -> None:
    while True:
        ui.clear()
        ui.heading(f"Hackerspace Control Panel v{__version__}")
        choices = [ui.choice(panel.TITLE, panel) for panel in PANELS]
        choices += [ui.choice(name, disabled="coming soon") for name in NOT_PORTED_YET]
        choices.append(ui.choice("Quit", ui.BACK))
        panel = ui.choose("Choose a panel", choices)
        if panel is None:
            return
        ui.clear()
        panel.run(settings)


def _pause_if_double_clicked(exit_code: int) -> int:
    if getattr(sys, "frozen", False):  # running as control-panel.exe
        input("Press Enter to close this window.")
    return exit_code
