"""Keeping control-panel.exe up to date.

When the exe starts, it asks GitHub for the latest release. If that's newer, it
offers to download it, swaps it in for itself and restarts. Windows won't let a
running program overwrite its own file, but it will let it rename it, so the
running exe is moved aside to control-panel.exe.old (deleted next time) and the
new one takes its place.
"""

from __future__ import annotations

import glob
import hashlib
import http.client
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from control_panel import __version__

RELEASES_PAGE = "https://github.com/timberline-secondary/control-panel/releases/latest"
LATEST_RELEASE_API = (
    "https://api.github.com/repos/timberline-secondary/control-panel/releases/latest"
)
URL_ENV_VAR = "CONTROL_PANEL_UPDATE_URL"  # to test against a local server instead of GitHub
ASSET_NAME = "control-panel.exe"
ERROR_SHARING_VIOLATION = 32  # Windows: the file is open somewhere that doesn't allow this
_HEADERS = {"User-Agent": f"hackerspace-control-panel/{__version__}"}


class UpdateError(Exception):
    """The update didn't happen. The message says why, for the person using the app."""


@dataclass(frozen=True)
class Release:
    tag: str
    version: tuple[int, ...]
    download_url: str
    size: int
    sha256: str | None  # GitHub's checksum of the exe, when it gives one


def parse_version(text: str) -> tuple[int, ...] | None:
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", text.strip())
    return tuple(int(part) for part in match.groups()) if match else None


def running_exe() -> Path | None:
    """The exe that can update itself, or None when running from source."""
    if not getattr(sys, "frozen", False):
        return None
    if sys.platform != "win32" and URL_ENV_VAR not in os.environ:
        return None  # releases only have a Windows exe
    return Path(sys.executable).resolve()


def latest_release(timeout: float = 3) -> Release | None:
    """The newest release with an exe, or None if we can't tell (offline, GitHub down...)."""
    request = urllib.request.Request(
        os.environ.get(URL_ENV_VAR) or LATEST_RELEASE_API,
        headers={**_HEADERS, "Accept": "application/vnd.github+json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.load(response)
        asset = next(a for a in data["assets"] if a["name"] == ASSET_NAME)
        digest = asset.get("digest") or ""
        release = Release(
            tag=data["tag_name"],
            version=parse_version(data["tag_name"]),
            download_url=asset["browser_download_url"],
            size=int(asset["size"]),
            sha256=digest.removeprefix("sha256:").lower() if digest.startswith("sha256:")
            else None,
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError, StopIteration,
            http.client.HTTPException):
        return None
    return release if release.version else None


def is_newer(release: Release, current: str = __version__) -> bool:
    current_version = parse_version(current)
    return current_version is not None and release.version > current_version


_held_exe = None  # see protect()


def protect(exe: Path) -> None:
    """Keep our own exe open while we run, so another window can't swap it out from under us.

    PyInstaller reads modules from the exe, by path, the first time they're used, so a
    copy whose exe was replaced would read garbage. Windows won't rename a file that's
    open the way Python opens files, so another window's update fails cleanly instead.
    """
    global _held_exe
    try:
        _held_exe = exe.open("rb")
    except OSError:
        _held_exe = None  # not worth stopping for


def _unprotect() -> None:
    global _held_exe
    if _held_exe is not None:
        _held_exe.close()
        _held_exe = None


def install(release: Release, exe: Path) -> Path:
    """Download the release and put it in place of exe, which may be running.

    Returns where the old exe was moved to, so roll_back() can put it back.
    """
    new = exe.with_name(f"{exe.name}.new")
    try:
        _download(release, new)
        shutil.copymode(exe, new)  # e.g. keep it executable on Linux/macOS
        _unprotect()  # our own hold on the exe would stop us moving it too
        try:
            old = _move_aside(exe)
        except OSError as e:
            protect(exe)
            if getattr(e, "winerror", None) == ERROR_SHARING_VIOLATION:
                raise UpdateError("control-panel is open in another window. Close the other "
                                  "window, then try again.") from e
            raise
        try:
            os.replace(new, exe)
        except OSError:
            roll_back(exe, old)
            raise
    except OSError as e:
        raise UpdateError(
            f"Couldn't update ({e}). You can download the new version from {RELEASES_PAGE}"
        ) from e
    finally:
        _remove(new)  # only still there if something went wrong
    return old


def roll_back(exe: Path, old: Path) -> None:
    """Put the old exe back where it was, e.g. because the new one won't start."""
    os.replace(old, exe)
    protect(exe)


def run_new_version(exe: Path, args: list[str]) -> int:
    """Run the freshly installed exe in this same window, and wait until it's closed.

    Waiting (rather than exiting straight away) keeps a Command Prompt that started
    us from taking the keyboard back while the new version is still using it.
    """
    # Otherwise PyInstaller treats the new process as part of this one.
    env = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT="1")
    process = subprocess.Popen([str(exe), *args], env=env)
    # Ctrl+C reaches both copies; it's the new one's to deal with.
    previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        return process.wait()
    finally:
        signal.signal(signal.SIGINT, previous)


def remove_leftovers(exe: Path) -> None:
    """Delete the old exe (and any half-finished download) from an earlier update."""
    for path in exe.parent.glob(f"{glob.escape(exe.name)}.old*"):
        _remove(path)
    _remove(exe.with_name(f"{exe.name}.new"))


def _download(release: Release, path: Path, timeout: float = 60) -> None:
    sha256, size = hashlib.sha256(), 0
    request = urllib.request.Request(release.download_url, headers=_HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response, path.open("wb") as f:
            while chunk := response.read(1 << 16):
                f.write(chunk)
                sha256.update(chunk)
                size += len(chunk)
    except http.client.HTTPException as e:
        raise UpdateError("The download was cut off. Try again later.") from e
    if size != release.size or (release.sha256 and sha256.hexdigest() != release.sha256):
        raise UpdateError("The download didn't match the release (it may have been cut off). "
                          "Try again later.")


def _move_aside(exe: Path) -> Path:
    """Rename exe out of the way. Windows allows this even while it's running."""
    old = exe.with_name(f"{exe.name}.old")
    try:
        old.unlink(missing_ok=True)  # left over from an earlier update
    except OSError:  # that old version is still running somewhere
        old = exe.with_name(f"{exe.name}.old-{os.getpid()}")
    os.replace(exe, old)
    return old


def _remove(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass  # in use; it'll be tidied up next time
