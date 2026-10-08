"""The main menu."""

from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

from control_panel import __version__, config, ui, updater
from control_panel.panels import themes

PANELS = [themes]
NOT_PORTED_YET = ["TVs"]  # from the legacy control panel (panels/TVs), not moved over yet


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.update:
        return _update_now()
    try:
        if (new_version_exit_code := _offer_update()) is not None:
            return new_version_exit_code
        settings = config.load(args.config)
        _main_menu(settings)
    except config.ConfigError as e:
        ui.error(str(e))
        return _pause_if_double_clicked(1)
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
    parser.add_argument("--update", action="store_true",
                        help="update control-panel.exe to the latest version, then exit")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser.parse_args(argv)


def _offer_update() -> int | None:
    """If there's a newer version, offer to switch to it and run it.

    Returns the new version's exit code once it's closed, or None to carry on with this one.
    """
    exe = updater.running_exe()
    if exe is None:
        return None
    updater.remove_leftovers(exe)
    release = updater.latest_release()
    if release is None or not updater.is_newer(release):
        return None
    if not ui.confirm(f"Version {release.tag} is out (you have v{__version__}). Update now?"):
        return None
    if not _install(release, exe):
        return None
    try:
        return updater.run_new_version(exe, sys.argv[1:])
    except OSError:
        ui.info("Close this window and open control-panel again to use it.")
        return None


def _update_now() -> int:
    """control-panel --update"""
    exe = updater.running_exe()
    if exe is None:
        ui.error("Only control-panel.exe can update itself. (From source, use git pull.)")
        return 1
    updater.remove_leftovers(exe)
    release = updater.latest_release(timeout=15)
    if release is None:
        ui.error(f"Couldn't check for a new version. Have a look at {updater.RELEASES_PAGE}")
        return 1
    if not updater.is_newer(release):
        ui.success(f"You have the latest version (v{__version__}).")
        return 0
    return 0 if _install(release, exe) else 1


def _install(release: updater.Release, exe: Path) -> bool:
    ui.info(f"Downloading {release.tag} ({release.size / 1_000_000:.1f} MB)...")
    try:
        updater.install(release, exe)
    except updater.UpdateError as e:
        ui.error(str(e))
        return False
    ui.success(f"Updated to {release.tag}.")
    return True


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
