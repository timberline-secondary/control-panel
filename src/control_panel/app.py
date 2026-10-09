"""The main menu."""

from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

from control_panel import __version__, config, ui, updater
from control_panel.panels import grade9, themes, tvs

PANELS = [themes, tvs, grade9]


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.update:
        return _update_now()
    if args.self_test:
        return _self_test()
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
    parser.add_argument("--self-test", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def _self_test() -> int:
    """For CI: check the exe has everything it needs to make a title card."""
    from control_panel import shrine

    card = shrine.title_card(shrine.Title("Self Test", "Digital Art", "2027"))
    if card.getbbox() is None:
        ui.error("The title card came out blank.")
        return 1
    ui.success("Self test passed.")
    return 0


def _offer_update() -> int | None:
    """If there's a newer version, offer to switch to it and run it.

    Returns the new version's exit code once it's closed, or None to carry on with this one.
    """
    exe = updater.running_exe()
    if exe is None:
        return None
    updater.protect(exe)
    updater.remove_leftovers(exe)
    release = updater.latest_release()
    if release is None or not updater.is_newer(release):
        return None
    if not ui.confirm(f"Version {release.tag} is out (you have v{__version__}). Update now?"):
        return None
    old = _install(release, exe)
    if old is None:
        ui.pause("Press Enter to carry on with this version.")  # before the menu clears it
        return None
    try:
        return updater.run_new_version(exe, sys.argv[1:])
    except OSError as e:
        return _new_version_wont_start(exe, old, e)


def _new_version_wont_start(exe: Path, old: Path, error: OSError) -> int | None:
    # Put the old exe back: this copy still reads its code from that path.
    try:
        updater.roll_back(exe, old)
    except OSError:
        ui.error(f"The new version won't start ({error}), and the old one couldn't be put "
                 f"back. Please download it again from {updater.RELEASES_PAGE}")
        return _pause_if_double_clicked(1)
    ui.error(f"Windows wouldn't start the new version ({error}). "
             f"Carrying on with v{__version__}.")
    ui.pause()
    return None


def _update_now() -> int:
    """control-panel --update"""
    exe = updater.running_exe()
    if exe is None:
        ui.error("Only control-panel.exe can update itself. (From source, use git pull.)")
        return 1
    updater.protect(exe)
    updater.remove_leftovers(exe)
    release = updater.latest_release(timeout=15)
    if release is None:
        ui.error(f"Couldn't check for a new version. Have a look at {updater.RELEASES_PAGE}")
        return 1
    if not updater.is_newer(release):
        ui.success(f"You have the latest version (v{__version__}).")
        return 0
    return 0 if _install(release, exe) else 1


def _install(release: updater.Release, exe: Path) -> Path | None:
    """Returns where the old exe went, or None if it didn't work (and says why)."""
    ui.info(f"Downloading {release.tag} ({release.size / 1_000_000:.1f} MB)...")
    try:
        old = updater.install(release, exe)
    except updater.UpdateError as e:
        ui.error(str(e))
        return None
    ui.success(f"Updated to {release.tag}.")
    return old


def _main_menu(settings: config.Config) -> None:
    while True:
        ui.clear()
        ui.heading(f"Hackerspace Control Panel v{__version__}")
        choices = [ui.choice(panel.TITLE, panel) for panel in PANELS]
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
