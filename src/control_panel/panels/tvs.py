"""TV Art Shrines: students' art, shown on the four TV Pis.

Every shrine's art is kept on pi-files, with the videos made from it (see art.py). Each TV
Pi runs Raspberry Slideshow, which plays every video in its media folder over and over.
This panel changes the art on pi-files, makes its videos again, then updates the TVs to
match: TV n plays the shrines in pi-files' tv<n> folder, and TV 4 (the hallway) plays them
all.
"""

from __future__ import annotations

import contextlib
import dataclasses
import ipaddress
import os
import posixpath
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import BinaryIO

import paramiko

from control_panel import ffmpeg, login, shrine, ui
from control_panel.art import (
    BACKUP,
    SLIDESHOW_NOTE,
    ArtError,
    ArtFolder,
    ArtServer,
    is_picture,
    is_title_card,
    order,
    piece,
    stored_name,
    video_name,
)
from control_panel.config import Config, TvsSettings
from control_panel.panels.themes import clean_source
from control_panel.ssh import CommandResult, ConnectionFailed, PiConnection

TITLE = "TV Art Shrines"
DOCS = "https://github.com/timberline-secondary/control-panel#tv-art-shrines"

TVS = {
    1: "TV 1: original art, last names A-L",
    2: "TV 2: original art, last names M-Z",
    3: "TV 3: demos, tutorials and Skills Canada",
    4: "TV 4: the hallway (it shows every shrine)",
}
HALLWAY = 4
IN_ROOM = (1, 2, 3)  # the ones grade 9 mode turns off
SPARE_SPACE = 200_000_000  # bytes to leave free on a TV's SD card
MAX_LISTED = 15  # shrines listed at once; more than that, and you search


class TvError(Exception):
    """Something the person can fix. The message says what."""


def size_text(size: int) -> str:
    return f"{size / 1e9:.1f} GB" if size >= 1e9 else f"{size / 1e6:.1f} MB"


def parse_free_space(df_output: str) -> int | None:
    """Bytes available, from `df -Pk <folder>`."""
    lines = df_output.strip().splitlines()
    try:
        return int(lines[-1].split()[3]) * 1024
    except (IndexError, ValueError):
        return None


def is_student(name: str) -> bool:
    """Students' shrines are named firstname.lastname; collections never have a dot."""
    return "." in name


def tvs_showing(tv: int) -> list[int]:
    """The TVs a shrine in pi-files' tv<n> folder is shown on: its own, and the hallway."""
    return sorted({tv, HALLWAY})


def _cec(on: bool) -> str:
    # cec-client on the TVs' current OS; cec-ctl on newer Raspberry Pi OS, which dropped it.
    # 0 is the TV's address.
    client = "on 0" if on else "standby 0"
    ctl = "--image-view-on" if on else "--standby"
    return (f"if command -v cec-client >/dev/null; then echo {client} | cec-client -s -d 1; "
            f"else cec-ctl -d /dev/cec0 --playback --to 0 {ctl}; fi")


# --- Talking to a TV Pi --------------------------------------------------------------------

class TvPi:
    """What we can do on one TV Pi, given an open connection."""

    def __init__(self, connection: PiConnection, number: int, settings: TvsSettings):
        self.connection = connection
        self.number = number
        self.settings = settings
        self.name = f"TV {number}"

    def _path(self, file_name: str) -> str:
        return posixpath.join(self.settings.media_dir, file_name)

    def videos(self) -> dict[str, int]:
        """{file name: size} of the videos it plays."""
        try:
            files = self.connection.file_sizes(self.settings.media_dir)
        except FileNotFoundError as e:
            raise TvError(f"{self.settings.media_dir} doesn't exist on "
                          f"{self.connection.host}.") from e
        return {name: size for name, size in files.items()
                if name.lower().endswith(".mp4") and not name.startswith(".")}

    def free_space(self) -> int | None:
        result = self.connection.run(f"df -Pk {shlex.quote(self.settings.media_dir)}")
        return parse_free_space(result.output) if result.ok else None

    def receive(self, fileobj: BinaryIO, file_name: str, size: int,
                progress: Callable[[int, int], None] | None = None) -> None:
        self.connection.upload(fileobj, self._path(file_name), progress)
        if self.connection.sftp.stat(self._path(file_name)).st_size != size:
            raise TvError(f"{file_name} didn't copy to {self.name} properly. Try again.")

    def open(self, file_name: str) -> BinaryIO:
        return self.connection.open(self._path(file_name))

    def remove(self, file_name: str) -> None:
        self.connection.remove(self._path(file_name))

    def usb_stick_plugged_in(self) -> bool:
        result = self.connection.run("ls /dev/disk/by-id/ 2>/dev/null")
        return any(re.match(r"usb-.*-part", line, re.IGNORECASE)
                   for line in result.output.splitlines())

    def restart_slideshow(self) -> CommandResult:
        return self.connection.run("systemctl restart rs", sudo=True)

    def power(self, on: bool) -> CommandResult:
        return self.connection.run(_cec(on), timeout=30)

    def reboot(self) -> CommandResult:
        return self.connection.run("shutdown -r now", sudo=True)


def connect(settings: TvsSettings, numbers: list[int] | tuple[int, ...]) -> dict[int, TvPi]:
    """Log in to these TVs, saying which ones couldn't be reached. Close them when done."""
    unreachable = _check_reachable(settings, numbers)
    tvs: dict[int, TvPi] = {}
    for number in numbers:
        if number in unreachable:
            ui.error(f"TV {number}: {unreachable[number]}")
            continue
        try:
            connection = login.connect(settings.pi(number))
        except ConnectionFailed as e:
            ui.error(f"TV {number}: {e}")
            continue
        if connection is None:  # they cancelled, at the password prompt or the like
            for tv in tvs.values():
                tv.connection.close()
            raise KeyboardInterrupt
        tvs[number] = TvPi(connection, number, settings)
    return tvs


def _reach_problem(host: str, port: int) -> str | None:
    """What's wrong, if host can't be reached (quickly, rather than waiting to log in)."""
    try:
        socket.create_connection((host, port), timeout=5).close()
    except socket.gaierror:
        return f"couldn't find {host} on the network."
    except OSError:
        return f"couldn't reach {host}. Is it plugged in and turned on?"
    return None


def _host_names(host: str) -> list[str]:
    """The names a Pi may be found by: host, then (as Windows finds it with 'ssh pi-files',
    asking the computers nearby rather than the school's DNS) its short name and
    name.local. Just host if it's an IP address."""
    try:
        ipaddress.ip_address(host)
        return [host]
    except ValueError:
        pass
    short = host.split(".", 1)[0]
    return list(dict.fromkeys([host, short, f"{short}.local"]))


def _find_host(host: str, port: int) -> str:
    """The first of _host_names(host) that can be found on the network (host if none)."""
    for name in _host_names(host):
        try:
            socket.getaddrinfo(name, port, type=socket.SOCK_STREAM)
        except (socket.gaierror, UnicodeError):
            continue
        return name
    return host


def _check_reachable(settings: TvsSettings, numbers) -> dict[int, str]:
    """{TV number: what's wrong} for the TVs that can't be reached. Checks them all at once,
    so a TV that's turned off doesn't hold up the others."""
    def check(number: int) -> str | None:
        return _reach_problem(settings.pi(number).host, settings.port)

    with ThreadPoolExecutor(max_workers=len(numbers) or 1) as pool:
        problems = dict(zip(numbers, pool.map(check, numbers), strict=True))
    return {number: problem for number, problem in problems.items() if problem}


@contextlib.contextmanager
def connected(settings: TvsSettings, numbers):
    tvs = connect(settings, numbers)
    try:
        yield tvs
    finally:
        for tv in tvs.values():
            tv.connection.close()


def on_each(tvs: dict[int, TvPi], action: Callable[[TvPi], CommandResult]) -> dict[int, object]:
    """Do something on each TV at the same time. {TV number: its result or exception}."""
    def attempt(tv: TvPi):
        try:
            return action(tv)
        except (OSError, paramiko.SSHException) as e:
            return e

    with ThreadPoolExecutor(max_workers=len(tvs) or 1) as pool:
        return dict(zip(tvs, pool.map(attempt, tvs.values()), strict=True))


def set_power(settings: TvsSettings, numbers, on: bool) -> None:
    """Turn TVs on or off (standby), all at once, and say how each went."""
    with connected(settings, numbers) as tvs:
        if not tvs:
            return
        results = on_each(tvs, lambda tv: tv.power(on))
    for number, result in sorted(results.items()):
        if isinstance(result, CommandResult) and result.ok:
            ui.success(f"TV {number}: turned {'on' if on else 'off'}.")
        else:
            detail = result.output.strip() if isinstance(result, CommandResult) else result
            ui.error(f"TV {number}: that didn't work ({detail}).")


def refresh(tv: TvPi) -> None:
    """Restart the slideshow so it notices new or deleted videos."""
    if tv.usb_stick_plugged_in():
        ui.warning(f"{tv.name} has a USB stick plugged in, so its slideshow wasn't restarted: "
                   "Raspberry Slideshow would replace the videos with what's on the stick. "
                   "Unplug it, then use 'Restart a TV's slideshow'.")
        return
    result = tv.restart_slideshow()
    if result.ok:
        ui.success(f"{tv.name}: restarted the slideshow.")
    else:
        ui.warning(f"{tv.name}: couldn't restart the slideshow ({result.output.strip()}). "
                   "The changes will show after the TV Pi reboots.")


# --- pi-files ------------------------------------------------------------------------------

@contextlib.contextmanager
def art_server(settings: TvsSettings):
    """Log in to pi-files. Gives an ArtServer, or None (having said why) if it can't be used."""
    connection = None
    host = _find_host(settings.art_host, settings.port)
    if host != settings.art_host:
        settings = dataclasses.replace(settings, art_host=host)
    problem = _reach_problem(host, settings.port)
    if problem:
        others = _host_names(host)[1:]
        if others and "couldn't find" in problem:
            problem = problem.rstrip(".") + f", or as {' or '.join(others)}."
        ui.error(f"pi-files: {problem} (The art is kept on pi-files.)")
    else:
        try:
            connection = login.connect(settings.art_pi())
        except ConnectionFailed as e:
            ui.error(f"pi-files: {e}")
        else:
            if connection is None:  # they didn't give its password, or didn't trust it
                ui.warning("pi-files: not logged in.")
    if connection is None:
        yield None
        return
    with connection:
        server: ArtServer | None = ArtServer(connection, settings)
        try:
            with on_pi_files(server):
                server.check_drive()
        except ArtError as e:
            ui.error(str(e))
            server = None
        yield server


@contextlib.contextmanager
def on_pi_files(server: ArtServer):
    """Report pi-files' problems as ArtError, so they aren't mistaken for a TV's."""
    try:
        yield
    except PermissionError as e:
        raise server.not_allowed(e.filename or server.root) from e
    except (OSError, paramiko.SSHException) as e:
        raise ArtError(f"Something went wrong on pi-files ({e}).") from e


def _say_unreadable(server: ArtServer) -> None:
    for path in sorted(server.unreadable):
        ui.warning(f"Some names in {path} on pi-files aren't readable text (they may have "
                   "come from an old USB stick), so they're left out. Rename them on pi-files.")
    server.unreadable.clear()


def _choose_shrine(server: ArtServer) -> ArtFolder | None:
    while True:
        text = ui.ask("Type part of the student's username or the collection's name")
        if text is None:
            return None
        if not text:
            continue
        with on_pi_files(server):
            found = server.find(text)
        _say_unreadable(server)
        if not found:
            ui.info(f"pi-files has no shrine with '{text}' in its name.")
            continue
        if len(found) > MAX_LISTED:
            ui.info(f"{len(found)} shrines have '{text}' in their name; here are the first "
                    f"{MAX_LISTED}. Type more of the name to narrow it down.")
        picked = ui.choose([(f"{art.folder} (TV {art.tv})", art) for art in found[:MAX_LISTED]]
                           + [("Search again", "again")], "Which shrine?")
        if picked != "again":
            return picked


# --- Making a shrine's videos (on this computer, from its art on pi-files) -----------------

class Ffmpeg:
    """ffmpeg, found (or downloaded) the first time it's needed."""

    def __init__(self, settings: TvsSettings):
        self.settings = settings
        self._path: Path | None = None

    def __call__(self) -> Path:
        if self._path is None:
            self._path = _get_ffmpeg(self.settings)
        return self._path


def _get_ffmpeg(settings: TvsSettings) -> Path:
    found = ffmpeg.find(settings.ffmpeg)
    if found:
        return found
    build = ffmpeg.build_for_this_computer()
    if build is None:
        raise TvError("Making videos needs ffmpeg. Install it, then set 'ffmpeg' in the "
                      "config file to where it is.")
    if not ui.confirm(f"Making videos needs ffmpeg, a free video tool. Download it now? "
                      f"({build.size / 1e6:.0f} MB, just this once)", default=True):
        raise KeyboardInterrupt
    with ui.progress("Downloading ffmpeg") as update:
        path = ffmpeg.download(lambda done, total: update(done / total))
    ui.success("Got ffmpeg.")
    return path


def make_videos(server: ArtServer, art: ArtFolder, get_ffmpeg: Ffmpeg) -> set[str]:
    """Bring a shrine's videos on pi-files up to date with its art: make its slideshow
    again if its pictures changed, convert any video that hasn't been, and remove the videos
    of art that's gone. Returns the names of the videos removed."""
    name = art.name
    files = server.art_files(art)
    videos = server.videos(art)
    pictures = order([p for p in files if is_picture(p)], name)
    note_path = posixpath.join(art.videos, SLIDESHOW_NOTE)
    note = server.read_json(note_path)
    made_from = [[posixpath.basename(p), files[p]] for p in pictures]
    slideshow = f"{name}.a.mp4"
    with tempfile.TemporaryDirectory(prefix="shrine-") as work:
        work = Path(work)
        if note.get("pictures") != made_from or (note.get("stills") and slideshow not in videos):
            note = _make_slideshow(server, art, files, pictures, videos, work, get_ffmpeg)
            note["pictures"] = made_from
            server.write_json(note_path, note)
        animated = set(note.get("animated", []))
        wanted = {slideshow} if note.get("stills") else set()
        for path in files:
            if is_picture(path) and posixpath.basename(path) not in animated:
                continue
            video = video_name(path, list(files), name)
            wanted.add(video)
            if video not in videos:
                _convert(server, art, path, video, work, get_ffmpeg)
    gone = set(server.videos(art)) - wanted
    for video in sorted(gone):
        server.remove(posixpath.join(art.videos, video))
    return gone


def _make_slideshow(server: ArtServer, art: ArtFolder, files: dict[str, int],
                    pictures: list[str], videos: dict[str, int], work: Path,
                    get_ffmpeg: Ffmpeg) -> dict:
    """Make a shrine's slideshow from its still pictures (and videos of its animated ones).
    Returns what's noted about it: how many stills, and which pictures are animations."""
    name = art.name
    stills, animated = [], []
    if pictures:
        ui.info(f"{art.folder}: making the slideshow from {len(pictures)} picture(s)...")
    with ui.progress("Getting its pictures from pi-files") as update:
        for i, path in enumerate(pictures, start=1):
            local = work / f"{i:04d}{posixpath.splitext(path)[1].lower()}"
            server.download(path, local)
            try:
                kind = shrine.classify(get_ffmpeg(), local)
            except shrine.ShrineError as e:
                ui.warning(f"Left out {posixpath.basename(path)}: {e}")
                continue
            media = shrine.Media(local, kind, posixpath.basename(path))
            (animated if kind == shrine.ANIMATION else stills).append(media)
            update(i / len(pictures))
    output = work / f"{name}.a.mp4"
    if stills:
        problems = shrine.make_slideshow(get_ffmpeg(), stills, output, work, ui.progress)
        for problem in problems:
            ui.warning(f"Left out {problem.label}: {problem.reason}")
        with ui.progress("Copying the slideshow to pi-files") as update:
            server.upload(output, posixpath.join(art.videos, output.name),
                          lambda done, total: update(done / total if total else 1))
    for media in animated:
        video = video_name(posixpath.join(art.path, media.label), list(files), name)
        if video not in videos:
            _convert_local(server, art, media, video, work, get_ffmpeg)
    return {"stills": len(stills), "animated": [media.label for media in animated]}


def _convert(server: ArtServer, art: ArtFolder, path: str, video: str, work: Path,
             get_ffmpeg: Ffmpeg) -> None:
    """Make a video or animation on pi-files into a video for the TVs."""
    file_name = posixpath.basename(path)
    local = work / f"source{posixpath.splitext(path)[1].lower()}"
    with ui.progress(f"Getting {file_name} from pi-files") as update:
        server.download(path, local, lambda done, total: update(done / total if total else 1))
    try:
        kind = shrine.classify(get_ffmpeg(), local)
    except shrine.ShrineError as e:
        ui.warning(f"{art.folder}: left out {file_name}: {e}")
        return
    _convert_local(server, art, shrine.Media(local, kind, file_name), video, work, get_ffmpeg)


def _convert_local(server: ArtServer, art: ArtFolder, media: shrine.Media, video: str,
                   work: Path, get_ffmpeg: Ffmpeg) -> None:
    output = work / video
    try:
        with ui.progress(f"Converting {media.label}") as update:
            shrine.convert(get_ffmpeg(), media, output, work, update)
    except (shrine.ShrineError, ffmpeg.FfmpegError) as e:
        ui.warning(f"{art.folder}: couldn't convert {media.label} ({e}).")
        return
    with ui.progress(f"Copying {video} to pi-files") as update:
        server.upload(output, posixpath.join(art.videos, video),
                      lambda done, total: update(done / total if total else 1))
    output.unlink()


# --- Keeping the TVs the same as pi-files --------------------------------------------------

def update_tvs(settings: TvsSettings, server: ArtServer, numbers: list[int],
               gone: set[str] | frozenset[str] = frozenset()) -> None:
    """Make any videos pi-files is missing for these TVs' shrines, then make each TV's
    videos the same as pi-files'. gone: videos that were deleted on purpose, so they're
    taken off the TVs without being backed up."""
    get_ffmpeg = Ffmpeg(settings)
    gone = set(gone)
    folders = sorted({tv for n in numbers for tv in art_tvs(n)})
    shrines: dict[int, list[ArtFolder]] = {}
    with on_pi_files(server):
        for tv in folders:
            for said in server.tidy(tv):
                ui.info(said)
            shrines[tv] = server.shrines(tv)
        _say_unreadable(server)
        for tv in folders:
            for art in shrines[tv]:
                try:
                    gone |= make_videos(server, art, get_ffmpeg)
                except (shrine.ShrineError, ffmpeg.FfmpegError) as e:
                    ui.error(f"{art.folder}: couldn't make its videos ({e}).")
        playing = {tv: _videos_for(server, shrines[tv]) for tv in folders}
    with connected(settings, numbers) as tvs:
        for number, tv in sorted(tvs.items()):
            wanted: dict[str, tuple[str, int]] = {}
            for folder in art_tvs(number):
                for file_name, source in playing[folder].items():
                    wanted.setdefault(file_name, source)
            try:
                _sync(tv, server, wanted, gone)
            except (TvError, ArtError, OSError, paramiko.SSHException) as e:
                ui.error(f"{tv.name}: that didn't work ({e}). Try 'Update the TVs from "
                         "pi-files' again.")


def art_tvs(number: int) -> tuple[int, ...]:
    """The pi-files TV folders a TV shows: its own, or all of them for the hallway."""
    return (1, 2, 3, 4) if number == HALLWAY else (number,)


def _videos_for(server: ArtServer, shrines: list[ArtFolder]) -> dict[str, tuple[str, int]]:
    """{video name: (its path on pi-files, its size)} for these shrines."""
    videos: dict[str, tuple[str, int]] = {}
    for art in shrines:
        for file_name, size in sorted(server.videos(art).items()):
            if file_name in videos:
                ui.warning(f"{art.path} has a video called {file_name}, like "
                           f"{videos[file_name][0]}, so it isn't shown.")
            else:
                videos[file_name] = (posixpath.join(art.videos, file_name), size)
    return videos


def _sync(tv: TvPi, server: ArtServer, wanted: dict[str, tuple[str, int]],
          gone: set[str]) -> None:
    """Make a TV's videos the same as wanted: {name: (path on pi-files, size)}."""
    playing = tv.videos()
    extra = sorted(set(playing) - set(wanted))
    to_copy = sorted(f for f in wanted if playing.get(f) != wanted[f][1])
    unknown = [f for f in extra if f not in gone]
    if unknown:
        ui.warning(f"{tv.name} has {len(unknown)} video(s) that aren't on pi-files:")
        for file_name in unknown[:10]:
            ui.info(f"  {file_name} ({size_text(playing[file_name])})")
        if len(unknown) > 10:
            ui.info(f"  ...and {len(unknown) - 10} more")
        if not ui.confirm(f"Back them up to pi-files' {BACKUP} folder, and take them off "
                          f"{tv.name}?", default=False):
            extra = [f for f in extra if f in gone]
    if not extra and not to_copy:
        ui.success(f"{tv.name}: up to date ({len(wanted)} videos).")
        return
    free = tv.free_space()
    needed = sum(wanted[f][1] - playing.get(f, 0) for f in to_copy) - \
        sum(playing[f] for f in extra)
    if free is not None and needed > free - SPARE_SPACE:
        raise TvError(f"it needs {size_text(needed)} more room, and only has "
                      f"{size_text(max(free - SPARE_SPACE, 0))}.")
    for file_name in extra:
        if file_name not in gone:
            backup = server.backup_path(f"tv{tv.number}-{file_name}")
            with tv.open(file_name) as f, \
                    ui.progress(f"{tv.name}: backing up {file_name}") as update:
                server.receive(f, backup,
                               lambda done, total: update(done / total if total else 1))
        tv.remove(file_name)
        ui.info(f"{tv.name}: took off {file_name}.")
    for file_name in to_copy:
        path, size = wanted[file_name]
        with server.open(path) as f, \
                ui.progress(f"{tv.name}: copying {file_name} ({size_text(size)})") as update:
            tv.receive(f, file_name, size, lambda done, total: update(done / total if total else 1))
    ui.success(f"{tv.name}: copied {len(to_copy)} and took off {len(extra)} video(s).")
    refresh(tv)


# --- Questions -----------------------------------------------------------------------------

def _ask_username() -> str | None:
    while True:
        username = ui.ask("Type the student's username, e.g. firstname.lastname")
        if username is None:
            return None
        name = shrine.clean_name(username, space=".")
        if name:
            return name
        ui.error("Type their username, using letters and numbers.")


def _ask_collection_name() -> tuple[str, str] | None:
    while True:
        text = ui.ask("Type the collection's name, e.g. Skills Canada 2026")
        if text is None:
            return None
        name = shrine.clean_name(text, space="-").replace(".", "-")
        if name:
            return name, text
        ui.error("Type a name, using letters and numbers.")


def _ask_tv(name: str) -> int | None:
    suggested = (shrine.suggest_tv(name) if is_student(name) else 3) or 1
    return ui.choose([(label, number) for number, label in TVS.items()],
                     f"Which TV is it for? (TV {suggested}, going by its name.)")


def _ask_title(name: str, text: str | None) -> tuple[shrine.Title | None] | None:
    """What the title card says: (a Title,), or (None,) for no title card. None if they go
    back. text: a collection's name, as they typed it."""
    if not is_student(name):
        return _ask_collection_title(text or name)
    default_name = shrine.display_name(name)
    shown = ui.ask("Type their name for the title card", default=default_name)
    if shown is None:
        return None
    grad = _ask_grad_year()
    if grad is None:
        return None
    subject = ui.choose([*shrine.SUBJECTS, ("Something else (type it)", "other")],
                        "Which subject?")
    if subject == "other":
        subject = ui.ask(f"Type the subject, as in 'The ___ of {shown}'")
    if not subject:
        return None
    return (shrine.Title(shown, subject, grad),)


def _ask_grad_year() -> str | None:
    default = str(shrine.this_years_grads())
    while True:
        year = ui.ask("Type their grad year", default=default)
        if year is None:
            return None
        if re.fullmatch(r"20\d\d", year):
            return year
        ui.error("Type a year, like 2027.")


def _ask_collection_title(text: str) -> tuple[shrine.Title | None] | None:
    choice = ui.choose([(f"Show '{text}' on the title card", "same"),
                        ("Type a different title", "other"),
                        ("No title card", "none")], "Title card")
    if choice is None:
        return None
    if choice == "none":
        return (None,)
    if choice == "other":
        text = ui.ask("Type the title")
        if not text:
            return None
    return (shrine.Title(text),)


def split_sources(text: str) -> list[str]:
    """Several files dragged in at once arrive as one line: "C:\\a b.jpg" C:\\c.jpg"""
    text = text.strip()
    single = clean_source(text)
    if single.lower().startswith(("http://", "https://")) or Path(single).expanduser().exists():
        return [single]
    text = text.removeprefix("& ")  # VS Code's PowerShell terminal
    tokens = re.findall(r'"[^"]*"|\'(?:[^\']|\'\')*\'|\S+', text)
    return [clean_source(token) for token in tokens]


def _ask_for_media(ffmpeg_path: Path, work: Path) -> list[shrine.Media] | None:
    """Ask for folders, files and links until they press Enter. None if they go back."""
    media: list[shrine.Media] = []
    seen: set[Path] = set()
    downloads = work / "downloads"
    while True:
        answer = ui.ask("Drag in a folder or files, or paste a link to a picture, video or "
                        f".zip file{' (Enter = done)' if media else ''}")
        if answer is None:
            return None
        if not answer:
            return media
        for source in split_sources(answer):
            found = _gather(ffmpeg_path, source, work, downloads)
            new = [m for m in found if m.path.resolve() not in seen]
            seen.update(m.path.resolve() for m in new)
            media += new
        pictures = sum(m.kind == shrine.PICTURE for m in media)
        ui.success(f"So far: {pictures} picture(s) and {len(media) - pictures} video(s) or "
                   "animation(s).")


def _gather(ffmpeg_path: Path, source: str, work: Path, downloads: Path) -> list[shrine.Media]:
    try:
        if source.lower().startswith(("http://", "https://")):
            downloads.mkdir(parents=True, exist_ok=True)
            with ui.progress("Downloading") as update:
                path = shrine.download_link(
                    source, downloads,
                    lambda done, total: update(done / total if total else 0),
                )
        else:
            path = Path(source).expanduser()
            if not path.exists():
                ui.error(f"Couldn't find {source}")
                return []
        ui.info(f"Looking through {path.name}...")
        found, skipped = shrine.gather(ffmpeg_path, path, work)
    except (shrine.ShrineError, ffmpeg.FfmpegError) as e:
        ui.error(str(e))
        return []
    for item in skipped[:10]:
        ui.warning(f"  Skipped {item.label}: {item.reason}")
    if len(skipped) > 10:
        ui.warning(f"  ...and {len(skipped) - 10} more files that aren't pictures or videos.")
    for item in found:
        ui.info(f"  {item.kind}: {item.label}")
    return found


def open_file(path: Path) -> None:
    """Open a file in the app Windows uses for it (e.g. Photos)."""
    try:
        if sys.platform == "win32":
            os.startfile(path)
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        ui.error(f"Couldn't open it: {e}")


# --- Menu actions --------------------------------------------------------------------------

def make_shrine(settings: TvsSettings) -> None:
    who = ui.choose([("A student", "student"),
                     ("A collection, e.g. Skills Canada or a class project", "collection")],
                    "Who is the shrine for?")
    if who is None:
        return
    if who == "student":
        name, text = _ask_username(), None
    else:
        name, text = _ask_collection_name() or (None, None)
    if name is None:
        return
    get_ffmpeg = Ffmpeg(settings)
    get_ffmpeg()
    with art_server(settings) as server:
        if server is None:
            return
        with on_pi_files(server):
            found = server.find(name, exact=True)
        if found:
            ui.info(f"pi-files already has a shrine for {name} (TV {found[0].tv}).")
            if ui.confirm("Add art to it instead?", default=True):
                _add_to(settings, server, found[0], get_ffmpeg)
            return
        tv = _ask_tv(name)
        if tv is None:
            return
        asked = _ask_title(name, text)
        if asked is None:
            return
        (title,) = asked
        with tempfile.TemporaryDirectory(prefix="shrine-") as work:
            media = _ask_for_media(get_ffmpeg(), Path(work))
            if media is None or (not media and title is None):
                return
            files = [(m.path, m.label) for m in media]
            if title is not None:
                card = Path(work) / f"{name}.a.title.png"
                shrine.title_card(title).save(card)
                files.insert(0, (card, None))
            with on_pi_files(server):
                art = server.folder_for(name, tv)
                _copy_to(server, art, files, set())
        update_tvs(settings, server, tvs_showing(tv))


def _copy_to(server: ArtServer, art: ArtFolder, files: list[tuple[Path, str | None]],
             taken: set[str]) -> None:
    """Copy art to a shrine's folder on pi-files: (file here, its name where it came from,
    or None to keep its name)."""
    with ui.progress(f"Copying {len(files)} piece(s) of art to pi-files") as update:
        for i, (local, label) in enumerate(files, start=1):
            file_name = local.name if label is None else stored_name(art.name, label, taken)
            server.upload(local, posixpath.join(art.path, file_name))
            update(i / len(files))
    ui.success(f"Copied {len(files)} piece(s) of art to {art.path} on pi-files.")


def add_art(settings: TvsSettings) -> None:
    with art_server(settings) as server:
        if server is None:
            return
        art = _choose_shrine(server)
        if art is not None:
            _add_to(settings, server, art, Ffmpeg(settings))


def _add_to(settings: TvsSettings, server: ArtServer, art: ArtFolder,
            get_ffmpeg: Ffmpeg) -> None:
    name = art.name
    with on_pi_files(server):
        existing = {posixpath.basename(p): size for p, size in server.art_files(art).items()}
    ui.info(f"{art.folder} has {len(existing)} piece(s) of art on pi-files (TV {art.tv}).")
    with tempfile.TemporaryDirectory(prefix="shrine-") as work:
        media = _ask_for_media(get_ffmpeg(), Path(work))
        if not media:
            return
        stems = {posixpath.splitext(f)[0].lower(): f for f in existing}
        new, same_name = [], []
        for m in media:
            natural = stored_name(name, m.label, set())
            old = stems.get(posixpath.splitext(natural)[0].lower())
            if old is None:
                new.append((m.path, m.label))
            elif old == natural and existing[old] == m.path.stat().st_size:
                ui.info(f"Already on pi-files: {m.label}")
            else:
                same_name.append((m, old))
        replace = False
        if same_name:
            ui.info(f"{len(same_name)} of them have the same names as art {art.folder} "
                    "already has:")
            for m, old in same_name:
                ui.info(f"  {m.label} (and {old})")
            replace = ui.confirm(f"Replace those? (The old ones go to pi-files' {BACKUP} "
                                 "folder. n = keep both.)", default=True)
            if replace is None:
                return
        with on_pi_files(server):
            for m, old in same_name:
                if replace:
                    server.set_aside(posixpath.join(art.path, old), old)
                    server.upload(m.path, posixpath.join(art.path, stored_name(name, m.label,
                                                                               set())))
                else:
                    new.append((m.path, m.label))
            if new:
                _copy_to(server, art, new, set(existing))
    if new or (same_name and replace):
        update_tvs(settings, server, tvs_showing(art.tv))


def view_art(settings: TvsSettings) -> None:
    with art_server(settings) as server:
        if server is None:
            return
        art = _choose_shrine(server)
        if art is None:
            return
        name = art.name
        deleted: set[str] = set()
        viewing = Path(tempfile.mkdtemp(prefix="shrine-art-"))
        try:
            while True:
                with on_pi_files(server):
                    files = server.art_files(art)
                if not files:
                    ui.info(f"{art.folder} has no art left on pi-files.")
                    break
                pictures = order([p for p in files if is_picture(p)], name)
                others = sorted((p for p in files if not is_picture(p)),
                                key=lambda p: shrine.natural_key(piece(p, name)))
                path = ui.choose([(_describe(p, files[p]), p) for p in pictures + others],
                                 f"{art.folder}'s art (TV {art.tv}). Which piece do you want "
                                 "to see?")
                if path is None:
                    break
                file_name = posixpath.basename(path)
                local = viewing / f"{len(list(viewing.iterdir())) + 1:02d}" \
                    f"{posixpath.splitext(path)[1].lower()}"
                with on_pi_files(server), ui.progress(f"Getting {file_name}") as update:
                    server.download(path, local, lambda done, total:
                                    update(done / total if total else 1))
                open_file(local)
                if ui.confirm(f"Delete {file_name}? (It's moved to pi-files' {BACKUP} folder.)",
                              default=False):
                    with on_pi_files(server):
                        server.set_aside(path, file_name)
                    deleted.add(file_name)
                    ui.success(f"Deleted {file_name}.\n")
        finally:
            shutil.rmtree(viewing, ignore_errors=True)  # whatever a viewer isn't holding open
        if deleted:
            update_tvs(settings, server, tvs_showing(art.tv))


def _describe(path: str, size: int) -> str:
    kind = "title card" if is_title_card(path) else "picture" if is_picture(path) else "video"
    return f"{posixpath.basename(path)} ({kind}, {size_text(size)})"


def remove_shrine(settings: TvsSettings) -> None:
    with art_server(settings) as server:
        if server is None:
            return
        art = _choose_shrine(server)
        if art is None:
            return
        how = ui.choose([
            (f"Move its art to pi-files' {BACKUP} folder (it can be put back)", "backup"),
            ("Delete its art for good", "delete"),
        ], f"Take {art.folder}'s shrine off the TVs, and:")
        if how is None:
            return
        if how == "delete" and not ui.confirm(f"Delete all of {art.folder}'s art for good? "
                                              "This can't be undone.", default=False):
            return
        with on_pi_files(server):
            gone = set(server.videos(art))
            if how == "delete":
                server.delete(art.path)
                ui.success(f"Deleted {art.path}.")
            else:
                ui.success(f"Moved {art.path} to {server.set_aside(art.path, art.folder)}.")
        update_tvs(settings, server, tvs_showing(art.tv), gone)


def update(settings: TvsSettings) -> None:
    numbers = ui.choose_many([(label, number) for number, label in TVS.items()],
                             "Which TVs should be updated from pi-files?")
    if not numbers:
        return
    with art_server(settings) as server:
        if server is not None:
            update_tvs(settings, server, numbers)


def power(settings: TvsSettings) -> None:
    on = ui.choose([("Turn on", True), ("Turn off (standby)", False)], "Turn TVs on or off?")
    if on is None:
        return
    numbers = ui.choose_many([(label, number) for number, label in TVS.items()], "Which TVs?")
    if numbers:
        set_power(settings, numbers, on)


def restart(settings: TvsSettings) -> None:
    number = ui.choose([(label, number) for number, label in TVS.items()], "Which TV?")
    if number is None:
        return
    what = ui.choose([
        ("The slideshow (quick: it notices new or deleted videos)", "slideshow"),
        ("The whole TV Pi (reboot: takes a minute or two)", "reboot"),
    ], f"Restart what on TV {number}?")
    if what is None:
        return
    with connected(settings, [number]) as tvs:
        if not tvs:
            return
        tv = tvs[number]
        if what == "slideshow":
            refresh(tv)
            return
        if tv.usb_stick_plugged_in():
            ui.warning(f"{tv.name} has a USB stick plugged in, so it wasn't rebooted: when it "
                       "starts, Raspberry Slideshow would replace the videos with what's on "
                       "the stick. Unplug it, then try again.")
            return
        try:
            result = tv.reboot()
        except (OSError, paramiko.SSHException):
            result = CommandResult(-1, "")  # it hung up on us, because it's going down
        if result.exit_status in (0, -1):
            ui.success(f"Rebooting {tv.name}... it'll be back in a minute or two.")
        else:
            ui.error(f"Couldn't reboot: {result.output.strip() or result.exit_status}")


def docs(settings: TvsSettings) -> None:
    ui.info(f"How the TV Pis and pi-files are set up: {DOCS}")


ACTIONS = [
    ("Make a shrine (a student's art, or a collection)", make_shrine),
    ("Add art to a shrine", add_art),
    ("See, open or delete a shrine's art", view_art),
    ("Remove a shrine", remove_shrine),
    ("Update the TVs from pi-files", update),
    ("Turn TVs on or off", power),
    ("Restart a TV's slideshow, or reboot it", restart),
    ("TV Art Shrines documentation", docs),
]


def run(config: Config) -> None:
    ui.run_menu(TITLE, ACTIONS, lambda action: _do(action, config.tvs))


def _do(action: Callable[[TvsSettings], None], settings: TvsSettings) -> None:
    try:
        action(settings)
    except (ConnectionFailed, TvError, ArtError, shrine.ShrineError, ffmpeg.FfmpegError) as e:
        ui.error(str(e))
    except TimeoutError:
        ui.error("A Pi stopped answering. Try again, or reboot it if it keeps happening.")
    except paramiko.SSHException as e:
        ui.error(f"Lost the connection to a Pi: {e}")
    except OSError as e:
        ui.error(f"That didn't work: {e}")
    except KeyboardInterrupt:
        ui.warning("Cancelled.")
