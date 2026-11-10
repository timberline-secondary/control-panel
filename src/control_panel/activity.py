"""A log of what was changed, and who by: kept on the Pi that was changed (activity.log), so
it's the same whichever computer the control panel runs on.

    2026-11-10 10:31  tcouture on HS-TEACHER  Removed the shrine ann.lee (TV 1), kept in backup
"""

from __future__ import annotations

import datetime
import getpass
import platform
import shlex

from control_panel import ui
from control_panel.ssh import PiConnection

LOG_NAME = "activity.log"
SHOWN = 30  # lines shown at once


def who() -> str:
    try:
        user = getpass.getuser()
    except (OSError, KeyError):
        user = "someone"
    return f"{user} on {platform.node() or 'this computer'}"


def entry(text: str, when: datetime.datetime | None = None) -> str:
    when = when or datetime.datetime.now()
    return f"{when:%Y-%m-%d %H:%M}  {who()}  {' '.join(text.split())}"


def record(connection: PiConnection, path: str, text: str) -> None:
    """Add a line to the log. Not being able to is said, but doesn't stop anything."""
    result = connection.run(f"printf '%s\\n' {shlex.quote(entry(text))} >> {shlex.quote(path)}")
    if not result.ok:
        ui.warning(f"(Couldn't add this to {path}: {result.output.strip()})")


def read(connection: PiConnection, path: str, search: str = "") -> list[str]:
    """The log's most recent lines (with search in them, if given), newest first."""
    result = connection.run(f"tail -n 5000 {shlex.quote(path)} 2>/dev/null")
    lines = [line for line in result.output.splitlines() if line.strip()]
    if search:
        lines = [line for line in lines if search.casefold() in line.casefold()]
    return lines[::-1][:SHOWN]


def show(connection: PiConnection, path: str) -> None:
    """The 'Recent changes' menu item: the latest changes, or the ones mentioning a name."""
    search = ui.ask("Type part of a name to see only the changes to it (Enter = the latest)")
    if search is None:
        return
    lines = read(connection, path, search)
    if not lines:
        ui.info(f"Nothing in {path} mentions '{search}'." if search else
                f"No changes have been noted in {path} yet.")
        return
    ui.info(f"The latest {len(lines)} change(s){f' to {search!r}' if search else ''}, "
            "newest first:")
    for line in lines:
        ui.info(f"  {line}")
