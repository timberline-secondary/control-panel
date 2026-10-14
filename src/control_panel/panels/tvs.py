"""The TVs: art shrines (videos of students' work) on the four TV Pis.

Each TV Pi runs Raspberry Slideshow, which plays every video in its media folder, in
alphabetical order, over and over. This panel makes the videos on this computer (see
shrine.py), copies them to the TVs, and turns the TVs on and off.
"""

from __future__ import annotations

import contextlib
import datetime
import os
import posixpath
import re
import shlex
import socket
import subprocess
import sys
import tempfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import NamedTuple

import paramiko

from control_panel import ffmpeg, login, shrine, ui
from control_panel.art import (
    REPLACED,
    ArtError,
    ArtFolder,
    ArtServer,
    is_picture,
    is_title_card,
    order,
    piece,
    same_piece,
    sha256,
    stored_name,
)
from control_panel.config import Config, TvsSettings
from control_panel.panels.themes import clean_source
from control_panel.ssh import CommandResult, ConnectionFailed, PiConnection

TITLE = "TVs (art shrines)"

TVS = {
    1: "TV 1: original art, last names A-L",
    2: "TV 2: original art, last names M-Z",
    3: "TV 3: demos, tutorials and Skills Canada",
    4: "TV 4: the hallway",
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


def shrine_name(folder_name: str, file_names: list[str]) -> str | None:
    """Which shrine some videos are: the folder's name if they're all part of it (as when
    this app made them), or else the name of their slideshow. None if it isn't clear."""
    if file_names and all(shrine.is_part_of(f, folder_name) for f in file_names):
        return folder_name
    slideshows = [f.removesuffix(".a.mp4") for f in file_names if f.endswith(".a.mp4")]
    if len(slideshows) == 1 and all(shrine.is_part_of(f, slideshows[0]) for f in file_names):
        return slideshows[0]
    return None


def upload_names(videos: list[Path]) -> dict[Path, str]:
    """The name each video gets on a TV: one the slideshow will play, and no two the same."""
    names: dict[Path, str] = {}
    for video in videos:
        stem = shrine.clean_name(video.stem) or "video"
        candidate, n = f"{stem}.mp4", 2
        while candidate in names.values():
            candidate, n = f"{stem}-{n}.mp4", n + 1
        names[video] = candidate
    return names


_WINDOWS_DEVICES = re.compile(r"(con|prn|aux|nul|com\d|lpt\d)(\..*)?", re.IGNORECASE)


def is_safe_name(file_name: str) -> bool:
    """Whether a file from a TV can be saved under its own name on Windows. A name with '\\'
    in it, for example, could point outside the folder."""
    return (bool(file_name) and not re.search(r'[\\/:*?"<>|\x00-\x1f]', file_name)
            and not file_name.startswith(".") and not file_name.endswith((" ", "."))
            and not _WINDOWS_DEVICES.fullmatch(file_name))


def is_student(name: str) -> bool:
    """Students' shrines are named firstname.lastname; collections never have a dot."""
    return "." in name


def suggested_tvs(name: str) -> list[int]:
    """Where a shrine usually goes: the student's alphabet TV (or TV 3 for a collection),
    plus the hallway."""
    if not is_student(name):
        return [3, HALLWAY]
    tv = shrine.suggest_tv(name)
    return [tv, HALLWAY] if tv else [HALLWAY]


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

    def upload(self, local: Path, file_name: str,
               progress: Callable[[float], None] | None = None) -> None:
        size = local.stat().st_size
        with local.open("rb") as f:
            self.connection.upload(f, self._path(file_name),
                                   progress=lambda done, total: progress and progress(done / size))
        if self.connection.sftp.stat(self._path(file_name)).st_size != size:
            raise TvError(f"{file_name} didn't copy to {self.name} properly. Try again.")

    def download(self, file_name: str, local: Path,
                 progress: Callable[[float], None] | None = None) -> None:
        self.connection.download(
            self._path(file_name), local,
            progress=lambda done, total: progress and progress(done / total if total else 1),
        )

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
        ui.success(f"{tv.name}: restarted the slideshow. It shows its network details for "
                   "a few seconds, then plays the videos.")
    else:
        ui.warning(f"{tv.name}: couldn't restart the slideshow ({result.output.strip()}). "
                   "The changes will show after the TV Pi reboots.")


@contextlib.contextmanager
def art_server(settings: TvsSettings):
    """Log in to pi-files. Gives an ArtServer, or None (having said why) if it can't be used."""
    connection = None
    problem = _reach_problem(settings.art_host, settings.port)
    if problem:
        ui.warning(f"pi-files: {problem}")
    else:
        try:
            connection = login.connect(settings.art_pi())
        except ConnectionFailed as e:
            ui.warning(f"pi-files: {e}")
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
            ui.warning(str(e))
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


def _save_locally(made: list[Path], folder: Path, name: str,
                  replace_all: bool = True) -> list[Path] | None:
    while True:  # the new videos are only thrown away if they say so
        try:
            return shrine.save(made, folder, name, replace_all)
        except OSError as e:
            ui.error(f"Couldn't save the shrine in {folder} ({e.strerror or e}). If one "
                     "of its videos is open, e.g. in a video player, close it.")
            if not ui.confirm("Try again?", default=True):
                return None


def _in_slideshow_order(name: str,
                        media: list[shrine.Media]) -> list[tuple[shrine.Media, str]]:
    """Each piece of art with the name it's kept under on pi-files, the pictures in the
    order the slideshow shows them (the same order as when art is added later). Videos are
    named after what they're kept as, so their names on the TVs can be worked out again."""
    taken: set[str] = set()
    named = {stored_name(name, item.label, taken): item for item in media}
    pictures = order([s for s, item in named.items() if item.kind == shrine.PICTURE], name)
    return [(named[s], s) for s in pictures] + \
        [(shrine.Media(item.path, item.kind, piece(s, name)), s)
         for s, item in named.items() if item.kind != shrine.PICTURE]


def _put_back(server: ArtServer, moved: list[tuple[str, str]]) -> bool:
    """Undo set_aside()s, newest first. Returns whether it all went back."""
    for aside, path in reversed(moved):
        try:
            with on_pi_files(server):
                server.put_back(aside, path)
        except ArtError:
            return False
    return True


def _keep_new_art(server: ArtServer, name: str, tv: int, title: shrine.Title | None,
                  art: list[tuple[shrine.Media, str]], old: list[ArtFolder],
                  work: Path) -> ArtFolder | None:
    """Copy a new shrine's art to pi-files, then move its old art (if it had some) aside.
    Returns its folder there, or None (having said why) if it couldn't."""
    moved: list[tuple[str, str]] = []  # (where it went, where it was), to undo on failure
    try:
        with on_pi_files(server):
            server.check_room([item.path for item, _ in art])
            kept = server.folder_for(name, tv)
            staging = server.staging(kept)  # so nothing changes until it's all there
            files = [(item.path, file_name) for item, file_name in art]
            if title is not None:
                card = work / f"{name}.a.title.png"
                shrine.title_card(title).save(card)
                files.insert(0, (card, card.name))
            with ui.progress(f"Keeping {name}'s art on pi-files") as update:
                for i, (local, file_name) in enumerate(files, start=1):
                    server.upload(staging, local, file_name)
                    update(i / len(files))
            for folder in old:  # its folder, then its loose videos
                loose = [p for p in server.art_files(folder)
                         if posixpath.dirname(p) != folder.path]
                if server.connection.exists(folder.path):
                    moved.append((server.set_aside(folder.path, folder.folder), folder.path))
                for path in loose:
                    moved.append((server.set_aside(path, posixpath.basename(path)), path))
            server.put_in_place(staging, kept)
    except BaseException as e:  # Ctrl+C too: never leave old art aside, half done
        restored = _put_back(server, moved)
        if not isinstance(e, ArtError):
            raise
        where = "pi-files still has what it had before." if restored else \
            f"Some of its old art is in {server.root}/{REPLACED} now: move it back by hand."
        ui.error(f"Couldn't keep its art on pi-files: {e} {where} The videos are on this "
                 "computer, and you can put them on the TVs.")
        return None
    ui.success(f"pi-files is keeping its art, in {kept.path}, so you can add to it later.")
    return kept


def _follow_to(server: ArtServer, kept: ArtFolder, pushed: PushResult) -> None:
    """If a new shrine went on a different TV than expected, move its art to that TV's
    folder on pi-files, so adding art to it later puts it on the same TV."""
    chosen = [n for n in pushed.chosen if n != HALLWAY] or pushed.chosen
    if len(chosen) != 1 or chosen[0] == kept.tv or chosen[0] not in pushed.done:
        return
    try:
        with on_pi_files(server):
            moved = server.move(kept, chosen[0])
    except ArtError as e:
        ui.warning(f"Couldn't move its art to TV {chosen[0]}'s folder on pi-files: {e}")
        return
    if moved is None:
        ui.warning(f"TV {chosen[0]}'s folder on pi-files already has a {kept.folder}, so its "
                   f"art stays in TV {kept.tv}'s.")
    else:
        ui.info(f"Moved its art on pi-files to {moved.path}, since it's on TV {chosen[0]}.")


def _video_names(name: str, old: list[str], folder: Path,
                 replaced: set[str] | frozenset[str] = frozenset()) -> set[str]:
    """The names the shrine's videos have on the TVs already, so new ones don't take them:
    worked out from its art on pi-files the way Make a shrine names them, plus any in its
    folder on this computer (except those of art being replaced, which its replacement
    takes over, so the TVs don't play both)."""
    taken: set[str] = set()
    for path in order([p for p in old if not is_title_card(p)], name):
        shrine.video_file_name(name, piece(path, name), taken)
    if folder.is_dir():
        gone = {shrine.video_file_name(name, piece(p, name), set()) for p in replaced}
        taken |= {p.name for p in videos_in(folder) if shrine.is_part_of(p.name, name)} - gone
    return taken


# --- Where things go on this computer ------------------------------------------------------

def shrines_folder(settings: TvsSettings) -> Path:
    if settings.shrines_dir:
        return Path(settings.shrines_dir).expanduser()
    return _documents() / "Hackerspace shrines"


def _documents() -> Path:
    if sys.platform == "win32":  # it may have been moved, e.g. into OneDrive
        import ctypes

        buffer = ctypes.create_unicode_buffer(260)
        csidl_personal = 5  # Documents
        if ctypes.windll.shell32.SHGetFolderPathW(None, csidl_personal, None, 0, buffer) == 0:
            return Path(buffer.value)
    return Path.home() / "Documents"


def open_folder(folder: Path) -> None:
    try:
        if sys.platform == "win32":
            os.startfile(folder)  # opens it in File Explorer
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(folder)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        ui.error(f"Couldn't open the folder: {e}")


def videos_in(folder: Path) -> list[Path]:
    return sorted((p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == ".mp4"
                   and not p.name.startswith(".")), key=lambda p: shrine.natural_key(p.name))


# --- Menu actions: making a shrine, and adding art to one ----------------------------------

def make_shrine(settings: TvsSettings) -> None:
    ffmpeg_path = _get_ffmpeg(settings)
    if ffmpeg_path is None:
        return
    who = ui.choose("Who is the shrine for?", [
        ui.choice("A student", "student"),
        ui.choice("A collection, e.g. Skills Canada or a class project (usually for TV 3)",
                  "collection"),
        ui.choice("Back", ui.BACK),
    ])
    if who is None:
        return
    named = _ask_username() if who == "student" else _ask_collection_name()
    if named is None:
        return
    name, text = named
    with art_server(settings) as server:
        old_art: list[ArtFolder] = []
        if server is None:
            if not ui.confirm("So its art can't be kept on pi-files, and adding art to it "
                              "later won't include this art. Make it anyway?", default=True):
                return
        else:
            with on_pi_files(server):
                old_art = server.find(name, exact=True)
            _say_unreadable(server)
            if old_art:
                where = " and ".join(folder.path for folder in old_art)
                ui.info(f"pi-files already has art for {name}, in {where}.")
                answer = ui.choose("What would you like to do?", [
                    ui.choice("Add the new art to it, keeping the art it has", "add"),
                    ui.choice("Start over with only the new art (its old art is moved aside, "
                              "not deleted)", "over"),
                    ui.choice("Back", ui.BACK),
                ])
                if answer is None:
                    return
                if answer == "add":
                    target = old_art[0] if len(old_art) == 1 else ui.choose(
                        "Which one?", [*(ui.choice(f"{folder.path} (TV {folder.tv})", folder)
                                         for folder in old_art), ui.choice("Back", ui.BACK)])
                    if target is not None:
                        _add_to(settings, ffmpeg_path, server, target)
                    return
        asked = _ask_title(name, text)
        if asked is None:
            return
        (title,) = asked
        folder = shrines_folder(settings) / name
        if not old_art and folder.is_dir() and \
                any(shrine.is_part_of(p.name, name) for p in videos_in(folder)):
            if not ui.confirm(f"There's already a shrine for {name} in {folder}. "
                              "Make it again, replacing it?", default=True):
                return
        # Starting over keeps it on the TV it's on; a new shrine goes by the last name.
        home = old_art[0].tv if old_art else \
            next((n for n in suggested_tvs(name) if n != HALLWAY), HALLWAY)
        kept = None
        with tempfile.TemporaryDirectory(prefix="shrine-") as work:
            media = _ask_for_media(ffmpeg_path, Path(work))
            if media is None:
                return
            if not media and (title is None or not ui.confirm(
                    "Nothing was added. Make a shrine with just the title card?",
                    default=False)):
                return
            art = _in_slideshow_order(name, media)
            made, problems = shrine.make(ffmpeg_path, name, title, [item for item, _ in art],
                                         Path(work) / "made", ui.progress)
            for problem in problems:
                ui.warning(f"Left out {problem.label}: {problem.reason}")
            if not made:
                ui.error("Nothing could be made.")
                return
            saved = _save_locally(made, folder, name)
            if saved is None:
                return
            ui.success(f"Made {name}'s shrine, in {folder}:")
            for video in saved:
                ui.info(f"  {video.name} ({size_text(video.stat().st_size)})")
            if server is not None:
                failed = {problem.label for problem in problems}
                kept = _keep_new_art(server, name, home, title,
                                     [(item, f) for item, f in art if item.label not in failed],
                                     old_art, Path(work))
        while True:
            next_step = ui.choose("What next?", [
                ui.choice("Put it on the TVs", "push"),
                ui.choice("Open the folder, to watch it first", "open"),
                ui.choice("Back to the TV menu", ui.BACK),
            ])
            if next_step == "open":
                open_folder(folder)
                continue
            if next_step == "push":
                ticked = sorted({home, HALLWAY}) if old_art else None
                pushed = push_shrine(settings, folder, name, ticked=ticked, home=home)
                if server is not None and kept is not None:
                    _follow_to(server, kept, pushed)
            return


def add_art(settings: TvsSettings) -> None:
    """Add pictures or videos to a shrine, making its slideshow again from its art on
    pi-files, as the old control panel did."""
    ffmpeg_path = _get_ffmpeg(settings)
    if ffmpeg_path is None:
        return
    with art_server(settings) as server:
        if server is None:
            ui.error("Adding art needs pi-files, where each shrine's art is kept. (The README "
                     "says how to set it up.)")
            return
        with on_pi_files(server):
            picked = _choose_art(server)
        if picked is not None:
            _add_to(settings, ffmpeg_path, server, picked)


def _say_unreadable(server: ArtServer) -> None:
    for path in sorted(server.unreadable):
        ui.warning(f"Some names in {path} on pi-files aren't readable text (they may have "
                   "come from an old USB stick), so they're left out. Rename them on pi-files.")
    server.unreadable.clear()


def _choose_art(server: ArtServer) -> ArtFolder | None:
    while True:
        text = ui.ask(f"Whose shrine? Type their username, or part of it {ui.QUIT_HINT}")
        if text is None:
            return None
        text = text.strip()
        if not text:
            continue
        found = server.find(text)
        _say_unreadable(server)
        if not found:
            ui.info(f"pi-files has no shrine with '{text}' in its name. (Each shrine's art is "
                    f"in a folder named after it, in {server.root}/tv1 to tv4.)")
            continue
        if len(found) > MAX_LISTED:
            ui.info(f"{len(found)} shrines have '{text}' in their name; here are the first "
                    f"{MAX_LISTED}. Type more of the name to narrow it down.")
        picked = ui.choose("Which shrine?", [
            *(ui.choice(f"{folder.folder} (TV {folder.tv})", folder)
              for folder in found[:MAX_LISTED]),
            ui.choice("Search again", "again"),
            ui.choice("Back", ui.BACK),
        ])
        if picked != "again":
            return picked


def _add_to(settings: TvsSettings, ffmpeg_path: Path, server: ArtServer,
            target: ArtFolder) -> None:
    """Add art to a shrine on pi-files. Nothing on pi-files changes until its new videos
    have been made, so if something goes wrong part way, it can just be tried again."""
    name = target.name
    with on_pi_files(server):
        existing = server.art_files(target)
    left_out = target.path in server.unreadable
    _say_unreadable(server)
    if left_out and not ui.confirm("Its slideshow will be made without those. Carry on?",
                                   default=False):
        return
    pictures = [p for p in existing if is_picture(p)]
    ui.info(f"{target.path} on pi-files has {len(pictures)} picture(s) and "
            f"{len(existing) - len(pictures)} video(s) or other file(s).")
    if target.folder != name:
        ui.info(f"Its videos on the TVs are named {name}.a.mp4 and so on.")
    title = None
    if not any(is_title_card(p) for p in existing):
        answer = ui.confirm("It has no title card. Make one?", default=is_student(name))
        if answer is None:
            return
        if answer:
            asked = _ask_title(name, target.folder)
            if asked is None:
                return
            (title,) = asked
    folder = shrines_folder(settings) / name
    with tempfile.TemporaryDirectory(prefix="shrine-") as work:
        work = Path(work)
        media = _ask_for_media(ffmpeg_path, work)
        if media is None:
            return
        media = _readable(media, work)
        planned = _plan_new_art(server, target, existing, media)
        if planned is None:
            return
        replaced = {old for _, _, olds in planned for old in olds}
        new_pictures = [(item, f) for item, f, _ in planned if item.kind == shrine.PICTURE]
        remake = title is not None or bool(new_pictures) or any(is_picture(p) for p in replaced)
        if not planned and title is None:
            if not ui.confirm(f"{'Nothing new was added. ' if media else ''}Make its slideshow "
                              "again from its art on pi-files, and put it on the TVs?",
                              default=bool(media)):
                return
            remake = True
        card = None
        if title is not None:
            card = work / f"{name}.a.title.png"
            shrine.title_card(title).save(card)
        old_files = [p for p in existing if p not in replaced]
        slides: list[tuple[shrine.Media, str]] = []
        if remake:  # the slideshow is made again, from all the pictures
            old_pictures = [p for p in old_files if is_picture(p)]
            with on_pi_files(server), ui.progress("Getting its pictures from pi-files") as update:
                got = server.download(old_pictures, work / "kept", update)
            for path, remote in zip(got, old_pictures, strict=True):
                file_name = posixpath.basename(remote)
                try:
                    kind = shrine.classify(ffmpeg_path, path)
                except shrine.ShrineError as e:
                    ui.warning(f"Left out {file_name}: {e}")
                    continue
                if kind == shrine.PICTURE:  # an animation's video is on the TVs already
                    slides.append((shrine.Media(path, kind, file_name), file_name))
            slides += new_pictures
            if card:
                slides.append((shrine.Media(card, shrine.PICTURE, card.name), card.name))
            by_name = {f: item for item, f in slides}
            slides = [(by_name[f], f) for f in order(list(by_name), name)]
        # Videos are named after what they're kept as.
        videos = [shrine.Media(item.path, item.kind, piece(f, name))
                  for item, f, _ in planned if item.kind != shrine.PICTURE]
        made, problems = shrine.make(ffmpeg_path, name, None,
                                     [item for item, _ in slides] + videos, work / "made",
                                     ui.progress,
                                     taken=_video_names(name, old_files, folder, replaced))
        for problem in problems:
            ui.warning(f"Left out {problem.label}: {problem.reason}")
        if not made:
            ui.error("Nothing could be made.")
            return
        failed = {problem.path for problem in problems if problem.path}
        keep = [(item, f, olds) for item, f, olds in planned if item.path not in failed]
        for item, _, olds in keep:
            for old in olds:
                if item.kind == shrine.PICTURE and not is_picture(old):
                    ui.warning(f"The video of {posixpath.basename(old)} stays on the TVs; "
                               "delete it there with 'See, copy or delete the videos on a TV'.")
        # Copy the new art to pi-files under hidden names; it's only put in place, with the
        # art it replaces moved aside, once it's all there and the videos are saved.
        files = ([(card, card.name)] if card else []) + [(item.path, f) for item, f, _ in keep]
        try:
            with on_pi_files(server):
                server.check_room([local for local, _ in files])
                server.ensure(target)
                with ui.progress("Copying the new art to pi-files") as update:
                    staged = server.stage(target, files, update)
        except ArtError as e:
            ui.error(f"Couldn't copy the new art to pi-files: {e} So nothing has changed; "
                     "try adding the art again.")
            return
        before = {p.name for p in videos_in(folder)} if folder.is_dir() else set()
        saved = _save_locally(made, folder, name, replace_all=False)
        if saved is None:
            server.unstage(staged)
            return
        moved: list[tuple[str, str]] = []
        try:
            with on_pi_files(server):
                for _, _, olds in keep:
                    for old in olds:
                        label = posixpath.basename(old)
                        if not label.lower().startswith(f"{name}."):
                            label = f"{target.folder}-{label}"
                        moved.append((server.set_aside(old, label), old))
                server.commit(staged)
        except BaseException as e:  # Ctrl+C too: never leave old art aside, half done
            _put_back(server, moved)
            server.unstage(staged)
            for video in saved:  # so trying again doesn't give them new names
                if video.name not in before:
                    video.unlink(missing_ok=True)
            if not isinstance(e, ArtError):
                raise
            ui.error(f"Couldn't copy the new art to pi-files: {e} So it hasn't been put on "
                     "the TVs; try adding the art again.")
            return
        if files:
            ui.success(f"pi-files now has the new art, in {target.path}.")
    ui.success(f"Made {name}'s new videos, in {folder}:")
    for video in saved:
        ui.info(f"  {video.name} ({size_text(video.stat().st_size)})")
    has_slideshow = any(video.name == f"{name}.a.mp4" for video in saved)
    push_shrine(settings, folder, name, only={video.name for video in saved}, adding=True,
                ticked=sorted({target.tv, HALLWAY}) if has_slideshow else [target.tv],
                home=target.tv)


def _readable(media: list[shrine.Media], work: Path) -> list[shrine.Media]:
    """Leave out pictures that can't be made into slides, before anything is planned with
    them (so a damaged picture can't stand in for one it would replace)."""
    good = []
    for item in media:
        if item.kind == shrine.PICTURE:
            try:
                shrine.prepare_picture(item.path, work / "check.png")
            except Exception as e:  # noqa: BLE001 (a damaged or odd file; Pillow raises all sorts)
                ui.warning(f"Left out {item.label}: couldn't read it ({e})")
                continue
        good.append(item)
    return good


def _plan_new_art(server: ArtServer, target: ArtFolder, existing: dict[str, int],
                  media: list[shrine.Media]) -> list[tuple[shrine.Media, str, list[str]]] | None:
    """[(new art, the name to keep it under, the old files it replaces)].

    Art that's there already (the same file) is left out, so dragging in a student's whole
    folder again only adds what's new. For pieces with the same names as old ones, it asks
    what to do. None if they go back.
    """
    name = target.name
    sizes = {item.path: item.path.stat().st_size for item in media}
    same_size = [p for p, size in existing.items() if size in set(sizes.values())]
    with on_pi_files(server):
        there = set(server.hashes(same_size).values())
    old_sizes = set(existing.values())
    by_piece: dict[str, list[str]] = {}
    for path in existing:
        if not is_title_card(path):  # only the title card question changes that
            by_piece.setdefault(same_piece(path, name), []).append(path)
    fresh, already, clashes = [], [], []
    for item in media:
        if sizes[item.path] in old_sizes and sha256(item.path) in there:
            already.append(item.label)
            continue
        olds = by_piece.get(same_piece(stored_name(name, item.label, set()), name), [])
        fresh.append((item, olds))
        if olds:
            clashes.append(item)
    if already:
        listed = ", ".join(already[:5]) + (f" and {len(already) - 5} more"
                                           if len(already) > 5 else "")
        ui.info(f"Already on pi-files, so not added again: {listed}.")
    answer = "keep"
    if clashes:
        listed = ", ".join(item.label for item in clashes[:5]) + (
            f" and {len(clashes) - 5} more" if len(clashes) > 5 else "")
        ui.info(f"{target.folder} already has pieces with the same names as {listed}. "
                "(Hightower saved some pictures again, so they may be the same art.)")
        answer = ui.choose("What should happen to them?", [
            ui.choice("Replace the old ones with these (the old ones are moved to "
                      f"{server.root}/{REPLACED})", "replace"),
            ui.choice("Leave these out, and keep the old ones", "skip"),
            ui.choice("Keep both", "both"),
            ui.choice("Back", ui.BACK),
        ])
        if answer is None:
            return None
    taken = {posixpath.basename(p) for p in existing}
    claimed: set[str] = set()
    planned: list[tuple[shrine.Media, str, list[str]]] = []
    for item, olds in fresh:
        if olds and answer == "skip":
            continue
        replaces = [o for o in olds if o not in claimed] if answer == "replace" else []
        claimed.update(replaces)
        # Only the piece that replaces an old file may take its name.
        free = {posixpath.basename(o) for o in replaces if posixpath.dirname(o) == target.path}
        file_name = stored_name(name, item.label, taken - free)
        taken.add(file_name)
        planned.append((item, file_name, replaces))
    return planned


def _get_ffmpeg(settings: TvsSettings) -> Path | None:
    found = ffmpeg.find(settings.ffmpeg)
    if found:
        return found
    build = ffmpeg.build_for_this_computer()
    if build is None:
        ui.error("Making shrines needs ffmpeg. Install it, then set 'ffmpeg' in the config "
                 "file to where it is.")
        return None
    if not ui.confirm(f"Making shrines needs ffmpeg, a free video tool. Download it now? "
                      f"({build.size / 1e6:.0f} MB, just this once)", default=True):
        return None
    with ui.progress("Downloading ffmpeg") as update:
        path = ffmpeg.download(lambda done, total: update(done / total))
    ui.success("Got ffmpeg.")
    return path


SUBJECT_CHOICES = [ui.choice(subject, subject) for subject in shrine.SUBJECTS]


def _ask_username() -> tuple[str, None] | None:
    while True:
        username = ui.ask(f"The student's username, e.g. firstname.lastname {ui.QUIT_HINT}")
        if username is None:
            return None
        name = shrine.clean_name(username, space=".")
        if name:
            break
        ui.error("Type their username, using letters and numbers.")
    if name != username.lower():
        ui.info(f"Their videos will be named {name}.a.mp4 and so on.")
    return name, None


def _ask_collection_name() -> tuple[str, str] | None:
    while True:
        text = ui.ask(f"Name of the collection, e.g. Skills Canada 2026 {ui.QUIT_HINT}")
        if text is None:
            return None
        name = shrine.clean_name(text, space="-").replace(".", "-")
        if name:
            break
        ui.error("Type a name, using letters and numbers.")
    ui.info(f"Its videos will be named {name}.a.mp4 and so on.")
    return name, text


def _ask_title(name: str, text: str | None) -> tuple[shrine.Title | None] | None:
    """What the title card says: (a Title,), or (None,) for no title card. None if they go
    back. text: a collection's name, as they typed it."""
    if not is_student(name):
        return _ask_collection_title(text or name)
    default_name = shrine.display_name(name)
    shown = ui.ask(f"Their name on the title card (Enter for {default_name}, q to go back)")
    if shown is None:
        return None
    grad = _ask_grad_year()
    if grad is None:
        return None
    subject = ui.choose("Which subject?", [
        *SUBJECT_CHOICES, ui.choice("Something else (type it)", "other"),
        ui.choice("Back", ui.BACK),
    ])
    if subject == "other":
        subject = ui.ask(f"The subject, as in 'The ___ of {shown or default_name}' {ui.QUIT_HINT}")
    if not subject:
        return None
    return (shrine.Title(shown or default_name, subject, grad),)


def _ask_grad_year() -> str | None:
    default = str(shrine.this_years_grads())
    while True:
        year = ui.ask(f"Grad year (Enter for {default}, q to go back)")
        if year is None:
            return None
        year = year or default
        if re.fullmatch(r"20\d\d", year):
            return year
        ui.error("Type a year, like 2027.")


def _ask_collection_title(text: str) -> tuple[shrine.Title | None] | None:
    choice = ui.choose("Title card", [
        ui.choice(f"Show '{text}' on the title card", "same"),
        ui.choice("Type a different title", "other"),
        ui.choice("No title card", "none"),
        ui.choice("Back", ui.BACK),
    ])
    if choice is None:
        return None
    if choice == "none":
        return (None,)
    if choice == "other":
        text = ui.ask(f"Title {ui.QUIT_HINT}")
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
    ui.info("Add the work to show. Drag a folder (or some files) into this window, or paste a "
            "link to a picture, video or .zip file. Add as many as you like.\n"
            "Pictures are shown in the order of their names; subfolders are included.")
    media: list[shrine.Media] = []
    seen: set[Path] = set()
    downloads = work / "downloads"
    while True:
        hint = "Enter when done" if media else "q to go back"
        answer = ui.ask(f"Drag in a folder or files, or paste a link ({hint})")
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
        videos = len(media) - pictures
        ui.success(f"So far: {pictures} picture(s) and {videos} video(s) or animation(s).")


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


# --- Menu actions: the TVs -----------------------------------------------------------------

class PushResult(NamedTuple):
    chosen: list[int]  # the TVs ticked
    done: list[int]  # the ones the videos went on


def push_shrine(settings: TvsSettings, folder: Path | None = None, name: str | None = None,
                *, only: set[str] | None = None, adding: bool = False,
                ticked: list[int] | None = None, home: int | None = None) -> PushResult:
    """Copy a shrine's videos to the TVs. Returns the TVs chosen, and those they went on.

    name: the shrine's, if known. only: copy just these videos. adding: they're new or
    remade videos for a shrine that's already on the TVs, so its others stay. ticked: the
    TVs to suggest. home: the shrine's own TV, if known (the hallway's own shrines get
    their videos there too).
    """
    if folder is None:
        folder = _choose_shrine_folder(settings)
        if folder is None:
            return PushResult([], [])
    videos = [v for v in videos_in(folder) if only is None or v.name in only]
    if not videos:
        ui.error(f"There are no .mp4 videos in {folder}.")
        return PushResult([], [])
    uploads = upload_names(videos)
    ui.info(f"From {folder}:")
    for video, file_name in uploads.items():
        renamed = f" (as {file_name}, so the TVs can play it)" if file_name != video.name else ""
        ui.info(f"  {video.name}{renamed} ({size_text(video.stat().st_size)})")

    name = name or shrine_name(folder.name, list(uploads.values()))
    suggested = ticked or (suggested_tvs(name) if name else [])
    numbers = ui.choose_many("Which TVs should it go on?", [
        ui.choice(label, number, checked=number in suggested) for number, label in TVS.items()
    ], require_one=True)
    if not numbers:
        return PushResult([], [])
    for_hallway = uploads
    slideshow = {v: f for v, f in uploads.items() if name and f == f"{name}.a.mp4"}
    if HALLWAY in numbers and home != HALLWAY and len(slideshow) < len(uploads):
        answer = ui.confirm("Put the videos on the hallway TV (4) too? It usually only gets "
                            "the title slideshow.", default=False)
        if answer is None:
            return PushResult([], [])
        if not answer:
            for_hallway = slideshow

    done = []
    with connected(settings, numbers) as tvs:
        for number, tv in tvs.items():
            going = for_hallway if number == HALLWAY else uploads
            if not going:
                ui.info(f"{tv.name}: nothing to copy (there's no title slideshow).")
                continue
            try:
                if _push_to(tv, going, name, adding):
                    done.append(number)
            except (TvError, OSError, paramiko.SSHException) as e:
                ui.error(f"{tv.name}: that didn't work ({e}).")
    return PushResult(numbers, done)


def _push_to(tv: TvPi, uploads: dict[Path, str], name: str | None,
             adding: bool = False) -> bool:
    """Returns whether the videos were copied."""
    existing = tv.videos()
    new_names = set(uploads.values())
    old = sorted((f for f in existing
                  if f in new_names or (name and shrine.is_part_of(f, name))),
                 key=shrine.natural_key)
    replace_all = not adding  # when adding art, the shrine's other videos stay
    if old and not adding:
        ui.info(f"{tv.name} already has:")
        for file_name in old:
            ui.info(f"  {file_name} ({size_text(existing[file_name])})")
        gone = [f for f in old if f not in new_names]  # what replacing them would delete
        replace = ui.choice("Replace them with the new ones" + (
            f" (deleting {len(gone)} that aren't among the new ones)" if gone else ""),
            "replace")
        keep = ui.choice("Keep them, and add the new ones (any with the same name are "
                         "replaced)", "keep")
        answer = ui.choose(f"What should happen to those on {tv.name}?", [
            *([keep, replace] if gone else [replace, keep]),  # deleting isn't the default
            ui.choice(f"Skip {tv.name}", "skip"),
        ])
        if answer is None:
            raise KeyboardInterrupt  # Ctrl+C stops the whole push, not just this TV
        if answer == "skip":
            return False
        replace_all = answer == "replace"
    leaving = [f for f in old if replace_all and f not in new_names]  # replaced, not re-copied
    overwritten = [f for f in old if f in new_names]

    needed = sum(video.stat().st_size for video in uploads)
    free = tv.free_space()
    short = free is not None and free - SPARE_SPACE < needed
    if short and free - SPARE_SPACE + sum(existing[f] for f in leaving + overwritten) < needed:
        ui.error(f"{tv.name} doesn't have room: these need {size_text(needed)} and it has "
                 f"{size_text(max(0, free - SPARE_SPACE))} to spare. Delete some old "
                 "videos from it first.")
        return False
    changed = False
    try:
        if short:  # make room first, deleting only what's being replaced anyway
            for file_name in leaving + overwritten:
                tv.remove(file_name)
                changed = True
        for video, file_name in uploads.items():
            changed = True
            with ui.progress(f"{tv.name}: copying {file_name}") as update:
                tv.upload(video, file_name, update)
        still_there = tv.videos()
        for file_name in leaving:
            if file_name in still_there:
                tv.remove(file_name)
        ui.success(f"{tv.name}: copied {len(uploads)} video(s).")
        return True
    finally:
        if changed:  # even if it stopped part way, so it plays what's there now
            with contextlib.suppress(OSError, paramiko.SSHException):
                refresh(tv)


def _choose_shrine_folder(settings: TvsSettings) -> Path | None:
    base = shrines_folder(settings)
    folders = []
    if base.is_dir():
        folders = sorted((d for d in base.iterdir() if d.is_dir() and videos_in(d)),
                         key=lambda d: d.stat().st_mtime, reverse=True)
    if len(folders) > MAX_LISTED:
        text = ui.ask("Whose shrine? Type part of the name (or press Enter for the newest ones)")
        if text is None:
            return None
        folders = [d for d in folders if text.casefold() in d.name.casefold()]
        if not folders:
            ui.warning(f"No shrines in {base} have '{text}' in their name.")
    choices = [ui.choice(f"{d.name} (made {_date(d)})", d) for d in folders[:MAX_LISTED]]
    picked = ui.choose("Which shrine?", [
        *choices, ui.choice("One in another folder (drag it in)", "other"),
        ui.choice("Back", ui.BACK),
    ])
    if picked != "other":
        return picked
    while True:
        answer = ui.ask(f"Drag the folder with the videos into this window {ui.QUIT_HINT}")
        if answer is None:
            return None
        folder = Path(clean_source(answer)).expanduser()
        if folder.is_dir():
            return folder
        ui.error(f"Couldn't find the folder {answer}")


def _date(folder: Path) -> str:
    made = datetime.date.fromtimestamp(folder.stat().st_mtime)
    return f"{made:%b} {made.day}, {made.year}"


def _choose_tv(prompt: str = "Which TV?") -> int | None:
    return ui.choose(prompt, [ui.choice(label, number) for number, label in TVS.items()]
                     + [ui.choice("Back", ui.BACK)])


def manage_videos(settings: TvsSettings) -> None:
    number = _choose_tv()
    if number is None:
        return
    with connected(settings, [number]) as tvs:
        if tvs:
            _manage(tvs[number], settings)


def _manage(tv: TvPi, settings: TvsSettings) -> None:
    while True:
        videos = tv.videos()
        free = tv.free_space()
        spare = f", with {size_text(free)} free" if free is not None else ""
        ui.info(f"{tv.name} has {len(videos)} video(s), {size_text(sum(videos.values()))}"
                f"{spare}.")
        action = ui.choose("What would you like to do?", [
            ui.choice("List them (and search)", "list"),
            ui.choice("Copy some to this computer", "copy"),
            ui.choice("Delete some", "delete"),
            ui.choice("Back", ui.BACK),
        ])
        if action is None:
            return
        if action == "list":
            _list(videos)
            continue
        picked = _pick(videos, "copy" if action == "copy" else "delete")
        if not picked:
            continue
        if action == "copy":
            _copy_from(tv, picked, shrines_folder(settings) / f"From TV {tv.number}")
        elif ui.confirm(f"Delete {len(picked)} video(s) from {tv.name}? This can't be undone.",
                        default=False):
            for file_name in picked:
                tv.remove(file_name)
            ui.success(f"Deleted {len(picked)} video(s).")
            refresh(tv)


def _list(videos: dict[str, int]) -> None:
    names = sorted(videos, key=shrine.natural_key)
    for file_name in names:
        ui.info(f"  {file_name} ({size_text(videos[file_name])})")
    while text := ui.ask("Search for videos containing (or just press Enter to go back)"):
        matches = [n for n in names if text.casefold() in n.casefold()]
        if not matches:
            ui.warning(f"No videos have '{text}' in their name.")
        for file_name in matches:
            ui.info(f"  {file_name} ({size_text(videos[file_name])})")


def _pick(videos: dict[str, int], verb: str) -> list[str]:
    text = ui.ask("Which videos? Type part of their name, e.g. a username (or press Enter "
                  "for all, q to go back)")
    if text is None:
        return []
    names = [n for n in sorted(videos, key=shrine.natural_key) if text.casefold() in n.casefold()]
    if not names:
        ui.warning(f"No videos have '{text}' in their name.")
        return []
    return ui.choose_many(f"Tick the ones to {verb}", [
        ui.choice(f"{n} ({size_text(videos[n])})", n) for n in names
    ]) or []


def _copy_from(tv: TvPi, file_names: list[str], folder: Path) -> None:
    for file_name in [n for n in file_names if not is_safe_name(n)]:
        ui.warning(f"Skipping {file_name}: its name has characters that aren't safe to copy.")
    file_names = [n for n in file_names if is_safe_name(n)]
    folder.mkdir(parents=True, exist_ok=True)
    already = [n for n in file_names if (folder / n).exists()]
    if already:
        replace = ui.confirm(f"{len(already)} of these are already in {folder}. Replace them?",
                             default=True)
        if replace is None:
            return
        if not replace:
            file_names = [n for n in file_names if n not in already]
    for file_name in file_names:
        partial = folder / f".{file_name}.part"
        try:
            with ui.progress(f"Copying {file_name}") as update:
                tv.download(file_name, partial, update)
            os.replace(partial, folder / file_name)
        finally:
            partial.unlink(missing_ok=True)
    ui.success(f"Copied {len(file_names)} video(s) to {folder}")
    if file_names and ui.confirm("Open the folder?", default=False):
        open_folder(folder)


def power(settings: TvsSettings) -> None:
    on = ui.choose("Turn TVs on or off?", [
        ui.choice("Turn on", True), ui.choice("Turn off (standby)", False),
        ui.choice("Back", ui.BACK),
    ])
    if on is None:
        return
    numbers = ui.choose_many("Which TVs?", [
        ui.choice(label, number, checked=True) for number, label in TVS.items()
    ], require_one=True)
    if numbers:
        set_power(settings, numbers, on)


def restart(settings: TvsSettings) -> None:
    number = _choose_tv()
    if number is None:
        return
    what = ui.choose(f"Restart what on TV {number}?", [
        ui.choice("The slideshow (quick: it notices new or deleted videos)", "slideshow"),
        ui.choice("The whole TV Pi (reboot: takes a minute or two)", "reboot"),
        ui.choice("Back", ui.BACK),
    ])
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


ACTIONS = [
    ("Make a shrine (a student's art, or a collection)", make_shrine),
    ("Add art to a shrine", add_art),
    ("Put a shrine on the TVs", push_shrine),
    ("See, copy or delete the videos on a TV", manage_videos),
    ("Turn TVs on or off", power),
    ("Restart a TV's slideshow, or reboot it", restart),
]


def run(config: Config) -> None:
    settings = config.tvs
    ui.heading(TITLE)
    while True:
        action = ui.choose("What would you like to do?",
                           [ui.choice(title, action) for title, action in ACTIONS]
                           + [ui.choice("Back", ui.BACK)])
        if action is None:
            return
        try:
            action(settings)
        except (ConnectionFailed, TvError, ArtError, shrine.ShrineError, ffmpeg.FfmpegError) as e:
            ui.error(str(e))
        except TimeoutError:
            ui.error("A TV Pi stopped answering. Try again, or reboot it if it keeps happening.")
        except paramiko.SSHException as e:
            ui.error(f"Lost the connection to a TV Pi: {e}")
        except OSError as e:
            ui.error(f"That didn't work: {e}")
        except KeyboardInterrupt:
            ui.warning("Cancelled.")
        print()
