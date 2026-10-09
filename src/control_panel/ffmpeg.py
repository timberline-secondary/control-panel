"""Getting and running ffmpeg, the free tool that makes the TV videos.

ffmpeg is big (about 85 MB), so it isn't inside control-panel.exe: a one-file exe unpacks
everything in it every time it starts, which would make every start slower. Instead it's
downloaded the first time it's needed and kept in the app's data folder. The download is a
fixed file on PyPI (the imageio-ffmpeg package, which wraps a standard ffmpeg build), and
its size and checksum are checked before it's used.
"""

from __future__ import annotations

import hashlib
import http.client
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from control_panel import __version__
from control_panel.config import APP_NAME

_HEADERS = {"User-Agent": f"hackerspace-control-panel/{__version__}"}
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # Windows: no extra console window


class FfmpegError(Exception):
    """ffmpeg couldn't be found, downloaded or run. The message says why."""


@dataclass(frozen=True)
class Build:
    url: str
    size: int
    sha256: str
    member: str  # the ffmpeg program inside the download (a zip file)


_PYPI = "https://files.pythonhosted.org/packages"
BUILDS = {
    ("win32", "amd64"): Build(
        f"{_PYPI}/2c/c6/fa760e12a2483469e2bf5058c5faff664acf66cadb4df2ad6205b016a73d/"
        "imageio_ffmpeg-0.6.0-py3-none-win_amd64.whl",
        31246824,
        "02fa47c83703c37df6bfe4896aab339013f62bf02c5ebf2dce6da56af04ffc0a",
        "imageio_ffmpeg/binaries/ffmpeg-win-x86_64-v7.1.exe",
    ),
    ("linux", "x86_64"): Build(
        f"{_PYPI}/a0/2d/43c8522a2038e9d0e7dbdf3a61195ecc31ca576fb1527a528c877e87d973/"
        "imageio_ffmpeg-0.6.0-py3-none-manylinux2014_x86_64.whl",
        29498237,
        "c7e46fcec401dd990405049d2e2f475e2b397779df2519b544b8aab515195282",
        "imageio_ffmpeg/binaries/ffmpeg-linux-x86_64-v7.0.2",
    ),
}


def build_for_this_computer() -> Build | None:
    return BUILDS.get((sys.platform, platform.machine().lower()))


def data_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / APP_NAME


def downloaded_path() -> Path | None:
    """Where the downloaded ffmpeg is kept, or None if there's no download for this computer."""
    build = build_for_this_computer()
    return data_dir() / Path(build.member).name if build else None


def find(configured: str = "") -> Path | None:
    """The ffmpeg to use: the one in the config file, then the downloaded one, then one
    that's already installed. None if there isn't one yet."""
    if configured:
        path = Path(configured).expanduser()
        if not path.is_file():
            raise FfmpegError(f"The ffmpeg setting in the config file is {configured}, "
                              "but there's no such file.")
        return path
    downloaded = downloaded_path()
    if downloaded is not None and downloaded.is_file():
        return downloaded
    installed = shutil.which("ffmpeg")
    if installed and _makes_h264(Path(installed)):
        return Path(installed)
    return None


def _makes_h264(ffmpeg: Path) -> bool:
    try:
        result = subprocess.run([str(ffmpeg), "-hide_banner", "-encoders"], capture_output=True,
                                text=True, errors="replace", timeout=30,
                                creationflags=_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return False
    return "libx264" in result.stdout


def download(progress: Callable[[int, int], None] | None = None, timeout: float = 60) -> Path:
    """Download ffmpeg, check it, and keep it in the data folder. Returns its path."""
    build = build_for_this_computer()
    target = downloaded_path()
    if build is None or target is None:
        raise FfmpegError("This app can't download ffmpeg for this kind of computer. Install "
                          "ffmpeg yourself, then set 'ffmpeg' in the config file to where it is.")
    target.parent.mkdir(parents=True, exist_ok=True)
    package = target.with_name(f"{target.name}.download")
    partial = target.with_name(f"{target.name}.part")
    try:
        _fetch(build, package, progress, timeout)
        with zipfile.ZipFile(package) as wheel, wheel.open(build.member) as src, \
                partial.open("wb") as dst:
            shutil.copyfileobj(src, dst, 1 << 20)
        partial.chmod(0o755)
        os.replace(partial, target)
    except (OSError, zipfile.BadZipFile, KeyError) as e:
        raise FfmpegError(f"Couldn't set up ffmpeg: {e}") from e
    finally:
        for leftover in (package, partial):
            try:
                leftover.unlink(missing_ok=True)
            except OSError:
                pass
    return target


def _fetch(build: Build, path: Path, progress: Callable[[int, int], None] | None,
           timeout: float) -> None:
    sha256, size = hashlib.sha256(), 0
    request = urllib.request.Request(build.url, headers=_HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response, path.open("wb") as f:
            while chunk := response.read(1 << 16):
                f.write(chunk)
                sha256.update(chunk)
                size += len(chunk)
                if progress:
                    progress(size, build.size)
                if size > build.size:
                    break
    except http.client.HTTPException as e:
        raise FfmpegError("The ffmpeg download was cut off. Try again.") from e
    except OSError as e:
        raise FfmpegError(f"Couldn't download ffmpeg: {getattr(e, 'reason', e)}") from e
    if size != build.size or sha256.hexdigest() != build.sha256:
        raise FfmpegError("The ffmpeg download wasn't the file it should be (it may have been "
                          "cut off). Try again.")


@dataclass(frozen=True)
class Probe:
    """What ffmpeg can tell about a file without converting it."""

    has_video: bool  # a moving picture (not just an mp3's cover art)
    duration: float | None  # seconds, if it says


_DURATION = re.compile(r"Duration: (\d+):(\d\d):(\d\d(?:\.\d+)?)")
_VIDEO_STREAM = re.compile(r"Stream #.*: Video: .*")


def probe(ffmpeg: Path, path: Path) -> Probe:
    try:
        result = subprocess.run(
            [str(ffmpeg), "-hide_banner", "-nostdin", "-i", str(path)], capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=60, creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise FfmpegError(f"Couldn't run ffmpeg: {e}") from e
    has_video = any("(attached pic)" not in line
                    for line in _VIDEO_STREAM.findall(result.stderr))
    match = _DURATION.search(result.stderr)
    duration = (int(match[1]) * 3600 + int(match[2]) * 60 + float(match[3])) if match else None
    return Probe(has_video, duration)


def run(ffmpeg: Path, args: list[str], *, seconds: float | None = None,
        progress: Callable[[float], None] | None = None) -> None:
    """Run ffmpeg with these arguments and wait for it.

    seconds: how long the output will be, so progress(fraction done) can be called.
    Raises FfmpegError with ffmpeg's own error message if it fails.
    """
    command = [str(ffmpeg), "-hide_banner", "-nostdin", "-y", "-v", "error",
               "-progress", "pipe:1", "-nostats", *args]
    with tempfile.TemporaryFile() as errors:
        try:
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors,
                                       creationflags=_NO_WINDOW)
        except OSError as e:
            raise FfmpegError(f"Couldn't run ffmpeg: {e}") from e
        try:
            for line in process.stdout:
                key, _, value = line.decode("ascii", "replace").strip().partition("=")
                if key == "out_time_us" and progress and seconds and value.isdigit():
                    progress(min(1.0, int(value) / 1_000_000 / seconds))
            exit_status = process.wait()
        finally:
            if process.poll() is None:  # e.g. Ctrl+C
                process.kill()
                process.wait()
        if exit_status != 0:
            errors.seek(0)
            message = errors.read().decode("utf-8", "replace").strip().splitlines()
            raise FfmpegError(message[-1] if message else f"ffmpeg stopped ({exit_status}).")
    if progress:
        progress(1.0)
