"""Check the Pis: one screen saying whether each Pi can be reached, how long it's been up,
how much room it has, and whether the thing it's for is running. Changes nothing."""

from __future__ import annotations

import dataclasses
import shlex
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import paramiko

from control_panel import login, ui
from control_panel.config import Config, PiSettings
from control_panel.panels import tvs
from control_panel.ssh import ConnectionFailed

TITLE = "Check the Pis"


@dataclass(frozen=True)
class Pi:
    name: str
    settings: PiSettings
    folder: str  # whose drive's free space matters
    drive: str  # what that drive is called, e.g. "SD card"
    own_drive: bool  # whether it should be a drive of its own (not the SD card)
    running: str  # what should be running, e.g. "slideshow"
    is_running: str  # a command that prints "yes" if it is


def pis(config: Config) -> list[Pi]:
    t, themes, clock = config.tvs, config.themes, config.clock
    return [
        *(Pi(f"TV {n}", t.pi(n), t.media_dir, "SD card", False, "slideshow",
             "systemctl is-active --quiet rs && echo yes") for n in tvs.TVS),
        Pi("pi-files", t.art_pi(), t.art_dir, "SSD", True, "", ""),
        Pi("pi-themes", themes, themes.songs_dir, "USB stick", True, "keypad theme player",
           "pgrep -f '[t]hemes\\.py$' >/dev/null && echo yes"),
        Pi("pi-projector", clock, clock.clock_dir, "SD card", False, "clock",
           "pgrep -f '[c]hromium|[f]irefox' >/dev/null && echo yes"),
    ]


def parse(output: str, pi: Pi) -> tuple[bool, str]:
    """(all's well, what to say) from what the check command printed."""
    lines = output.splitlines() + ["", "", ""]
    uptime = ", ".join(lines[0].strip().split(", ")[:2]) or "up"
    df = lines[1].split()
    said, well = [uptime], True
    if len(df) >= 6 and df[3].isdigit():
        free = int(df[3]) * 1024
        said.append(f"{tvs.size_text(free)} free on its {pi.drive}")
        if pi.own_drive and df[5] == "/":
            said[-1] = f"its {pi.drive} isn't plugged in (or mounted)"
            well = False
        elif free < 500_000_000:
            said[-1] += " (nearly full)"
            well = False
    else:
        said.append(f"couldn't see {pi.folder}")
        well = False
    if pi.running:
        if lines[2].strip() == "yes":
            said.append(f"{pi.running} running")
        else:
            said.append(f"{pi.running} NOT running")
            well = False
    return well, ", ".join(said)


def check(pi: Pi, problem: str | None) -> None:
    """Log in to a Pi (unless it can't be reached) and say how it is."""
    label = f"{pi.name:<13}"
    if problem:
        ui.error(f"{label}{problem}")
        return
    try:
        connection = login.connect(pi.settings)
    except ConnectionFailed as e:
        ui.error(f"{label}{e}")
        return
    if connection is None:
        ui.warning(f"{label}not checked (not logged in)")
        return
    try:
        with connection:
            result = connection.run(
                f"uptime -p; df -Pk {shlex.quote(pi.folder)} 2>/dev/null | tail -n 1; "
                f"{pi.is_running or 'true'}", timeout=30)
    except (OSError, paramiko.SSHException) as e:
        ui.error(f"{label}lost the connection ({e})")
        return
    well, said = parse(result.output, pi)
    (ui.success if well else ui.warning)(f"{label}{said}")


def run(config: Config) -> None:
    ui.clear()
    ui.heading(TITLE)
    everyone = pis(config)
    files = next(i for i, pi in enumerate(everyone) if pi.name == "pi-files")
    found = tvs._find_host(everyone[files].settings.host, everyone[files].settings.port)
    everyone[files] = dataclasses.replace(  # e.g. as pi-files.local, as the TV panel does
        everyone[files], settings=dataclasses.replace(everyone[files].settings, host=found))
    ui.info("Checking which Pis can be reached...")
    with ThreadPoolExecutor(max_workers=len(everyone)) as pool:
        problems = list(pool.map(lambda pi: tvs._reach_problem(pi.settings.host,
                                                                pi.settings.port), everyone))
    for pi, problem in zip(everyone, problems, strict=True):
        try:
            check(pi, problem)
        except KeyboardInterrupt:
            ui.warning("Cancelled.")
            break
    ui.pause()
