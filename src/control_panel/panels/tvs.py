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
import shutil
import socket
import subprocess
import sys
import tempfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import paramiko

from control_panel import ffmpeg, login, shrine, ui
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


def shrine_names(file_names: list[str]) -> set[str]:
    """The shrines some videos belong to: 'ann.lee.a.mp4' -> 'ann.lee'."""
    names = set()
    for file_name in file_names:
        match = re.match(r"(.+?)\.[az]\.", file_name)
        if match:
            names.add(match[1])
    return names


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
        if connection is None:  # they cancelled at the password prompt
            for tv in tvs.values():
                tv.connection.close()
            return {}
        tvs[number] = TvPi(connection, number, settings)
    return tvs


def _check_reachable(settings: TvsSettings, numbers) -> dict[int, str]:
    """{TV number: what's wrong} for the TVs that can't be reached. Checks them all at once,
    so a TV that's turned off doesn't hold up the others."""
    def check(number: int) -> str | None:
        host = settings.pi(number).host
        try:
            socket.create_connection((host, settings.port), timeout=5).close()
        except socket.gaierror:
            return f"couldn't find {host} on the network."
        except OSError:
            return f"couldn't reach {host}. Is it plugged in and turned on?"
        return None

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


# --- Menu actions: making a shrine ---------------------------------------------------------

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
    details = _ask_student() if who == "student" else _ask_collection()
    if details is None:
        return
    name, title = details
    folder = shrines_folder(settings) / name
    if folder.is_dir() and any(shrine.is_part_of(p.name, name) for p in videos_in(folder)):
        if not ui.confirm(f"There's already a shrine for {name} in {folder}. "
                          "Make it again, replacing it?", default=True):
            return

    with tempfile.TemporaryDirectory(prefix="shrine-") as work:
        media = _ask_for_media(ffmpeg_path, Path(work))
        if media is None:
            return
        if not media and (title is None or not ui.confirm(
                "Nothing was added. Make a shrine with just the title card?", default=False)):
            return
        made, problems = shrine.make(ffmpeg_path, name, title, media, Path(work) / "made",
                                     ui.progress)
        for problem in problems:
            ui.warning(f"Left out {problem.label}: {problem.reason}")
        if not made:
            ui.error("Nothing could be made.")
            return
        folder.mkdir(parents=True, exist_ok=True)
        shrine.remove_old(folder, name)
        for video in made:
            shutil.move(video, folder / video.name)
    ui.success(f"Made {name}'s shrine, in {folder}:")
    for video in videos_in(folder):
        if shrine.is_part_of(video.name, name):
            ui.info(f"  {video.name} ({size_text(video.stat().st_size)})")
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
            push_shrine(settings, folder)
        return


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


def _ask_student() -> tuple[str, shrine.Title] | None:
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
    default_name = shrine.display_name(name)
    shown = ui.ask(f"Their name on the title card (Enter for {default_name}, q to go back)")
    if shown is None:
        return None
    grad = _ask_grad_year()
    if grad is None:
        return None
    subject = ui.choose("Which subject?", [
        *SUBJECT_CHOICES, ui.choice("Something else (type it)", "other"), ui.choice("Back", ui.BACK),
    ])
    if subject == "other":
        subject = ui.ask(f"The subject, as in 'The ___ of {shown or default_name}' {ui.QUIT_HINT}")
    if not subject:
        return None
    return name, shrine.Title(shown or default_name, subject, grad)


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


def _ask_collection() -> tuple[str, shrine.Title | None] | None:
    while True:
        text = ui.ask(f"Name of the collection, e.g. Skills Canada 2026 {ui.QUIT_HINT}")
        if text is None:
            return None
        name = shrine.clean_name(text, space="-").replace(".", "-")
        if name:
            break
        ui.error("Type a name, using letters and numbers.")
    ui.info(f"Its videos will be named {name}.a.mp4 and so on.")
    choice = ui.choose("Title card", [
        ui.choice(f"Show '{text}' on the title card", "same"),
        ui.choice("Type a different title", "other"),
        ui.choice("No title card", "none"),
        ui.choice("Back", ui.BACK),
    ])
    if choice is None:
        return None
    if choice == "none":
        return name, None
    if choice == "other":
        text = ui.ask(f"Title {ui.QUIT_HINT}")
        if not text:
            return None
    return name, shrine.Title(text)


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

def push_shrine(settings: TvsSettings, folder: Path | None = None) -> None:
    if folder is None:
        folder = _choose_shrine_folder(settings)
        if folder is None:
            return
    videos = videos_in(folder)
    if not videos:
        ui.error(f"There are no .mp4 videos in {folder}.")
        return
    uploads = {video: shrine.clean_name(video.stem) + ".mp4" for video in videos}
    ui.info(f"From {folder}:")
    for video, file_name in uploads.items():
        renamed = f" (as {file_name}, so the TVs can play it)" if file_name != video.name else ""
        ui.info(f"  {video.name}{renamed} ({size_text(video.stat().st_size)})")

    names = shrine_names(list(uploads.values()))
    suggested = suggested_tvs(next(iter(names))) if len(names) == 1 else []
    numbers = ui.choose_many("Which TVs should it go on?", [
        ui.choice(label, number, checked=number in suggested) for number, label in TVS.items()
    ], require_one=True)
    if not numbers:
        return
    slideshows = {v: n for v, n in uploads.items() if ".a." in n}
    for_hallway = uploads
    if HALLWAY in numbers and len(numbers) > 1 and slideshows and len(slideshows) < len(uploads):
        if not ui.confirm("Put the videos on the hallway TV (4) too? It usually only gets the "
                          "title slideshow.", default=False):
            for_hallway = slideshows

    with connected(settings, numbers) as tvs:
        for number, tv in tvs.items():
            _push_to(tv, for_hallway if number == HALLWAY else uploads, names)


def _push_to(tv: TvPi, uploads: dict[Path, str], names: set[str]) -> None:
    existing = tv.videos()
    new_names = set(uploads.values())
    old = sorted((f for f in existing
                  if f in new_names or any(shrine.is_part_of(f, n) for n in names)),
                 key=shrine.natural_key)
    replace_all = True
    if old:
        ui.info(f"{tv.name} already has:")
        for file_name in old:
            ui.info(f"  {file_name} ({size_text(existing[file_name])})")
        answer = ui.choose(f"What should happen to those on {tv.name}?", [
            ui.choice("Replace them with the new ones", "replace"),
            ui.choice("Keep them, and add the new ones (any with the same name are replaced)",
                      "keep"),
            ui.choice(f"Skip {tv.name}", ui.BACK),
        ])
        if answer is None:
            return
        replace_all = answer == "replace"
    going = [f for f in old if replace_all or f in new_names]

    needed = sum(video.stat().st_size for video in uploads)
    free = tv.free_space()
    if free is not None and free - SPARE_SPACE < needed:
        freed = sum(existing[f] for f in going)
        if free - SPARE_SPACE + freed < needed:
            ui.error(f"{tv.name} doesn't have room: these need {size_text(needed)} and it has "
                     f"{size_text(max(0, free - SPARE_SPACE))} to spare. Delete some old "
                     "videos from it first.")
            return
        for file_name in going:  # make room first
            tv.remove(file_name)
    for video, file_name in uploads.items():
        with ui.progress(f"{tv.name}: copying {file_name}") as update:
            tv.upload(video, file_name, update)
    still_there = tv.videos()
    for file_name in going:
        if file_name not in new_names and file_name in still_there:
            tv.remove(file_name)
    ui.success(f"{tv.name}: copied {len(uploads)} video(s).")
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
    folder.mkdir(parents=True, exist_ok=True)
    already = [n for n in file_names if (folder / n).exists()]
    if already and not ui.confirm(f"{len(already)} of these are already in {folder}. "
                                  "Replace them?", default=True):
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
        except (ConnectionFailed, TvError, shrine.ShrineError, ffmpeg.FfmpegError) as e:
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
