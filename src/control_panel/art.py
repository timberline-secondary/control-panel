"""The art server, pi-files: where each shrine's original art is kept.

A shrine's slideshow is a video, and taking pictures back out of a video and making it again
loses a little quality every time. So the originals are kept on a Pi with an external drive,
and adding art to a shrine makes its slideshow again from them. Each shrine has a folder:

    <art_dir>/<name>/shrine.json      what's in it (see Record), written last
    <art_dir>/<name>/pictures/        the original pictures, in the order they're shown
    <art_dir>/<name>/videos/          the videos made for the TVs (.a slideshow, .z videos)

A shrine that's made again has its old folder moved into <art_dir>/.replaced, not deleted.
Nothing here asks questions.
"""

from __future__ import annotations

import datetime
import io
import json
import posixpath
import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from control_panel import shrine
from control_panel.config import TvsSettings
from control_panel.ssh import PiConnection

RECORD = "shrine.json"
FORMAT = 1
REPLACED = ".replaced"
SPARE_SPACE = 1_000_000_000  # bytes to leave free on the drive


class ArtError(Exception):
    """Something the person can fix. The message says what."""


@dataclass
class Record:
    """What pi-files knows about a shrine."""

    name: str
    title: shrine.Title | None
    pictures: list[str] = field(default_factory=list)  # in pictures/, in order
    videos: list[str] = field(default_factory=list)  # its .z videos (in videos/ if we have them)
    tvs: list[int] = field(default_factory=list)  # the TVs it was last put on

    def to_json(self) -> str:
        title = None if self.title is None else {
            "name": self.title.name, "subject": self.title.subject,
            "grad_year": self.title.grad_year,
        }
        return json.dumps({
            "format": FORMAT, "name": self.name, "title": title, "pictures": self.pictures,
            "videos": self.videos, "tvs": self.tvs,
            "updated": datetime.datetime.now().isoformat(timespec="seconds"),
        }, indent=2) + "\n"

    @classmethod
    def from_json(cls, text: str) -> Record:
        try:
            data = json.loads(text)
            title = data["title"]
            record = cls(
                name=str(data["name"]),
                title=None if title is None else shrine.Title(
                    str(title["name"]), title.get("subject"), title.get("grad_year")),
                pictures=[str(p) for p in data["pictures"]],
                videos=[str(v) for v in data.get("videos", [])],
                tvs=[int(n) for n in data.get("tvs", [])],
            )
        except (ValueError, KeyError, TypeError, AttributeError) as e:
            raise ArtError(f"The record on pi-files is damaged ({e}).") from e
        if any("/" in p or p.startswith(".") for p in record.pictures):
            raise ArtError("The record on pi-files is damaged (odd picture names).")
        return record


def stored_name(number: int, label: str) -> str:
    """The name a picture is kept under: its place in the shrine, then its own name."""
    stem, ext = posixpath.splitext(posixpath.basename(label))
    stem = shrine.clean_name(stem).replace(".", "-") or "picture"
    ext = ext.lower() if re.fullmatch(r"\.[a-z0-9]{1,5}", ext.lower()) else ""
    return f"{number:04d}-{stem}{ext}"


Progress = Callable[[float], None]


class ArtServer:
    """The shrines on pi-files, given an open connection."""

    def __init__(self, connection: PiConnection, settings: TvsSettings):
        self.connection = connection
        self.settings = settings
        self.root = settings.art_dir.rstrip("/") or "/"

    def _folder(self, name: str, *parts: str) -> str:
        return posixpath.join(self.root, name, *parts)

    def check_drive(self) -> None:
        """Make sure the art folder is on its own drive, not the Pi's SD card, and exists."""
        existing = self.root
        while not self.connection.exists(existing):
            existing = posixpath.dirname(existing)
        result = self.connection.run(f"df -P {shlex.quote(existing)}")
        lines = result.output.strip().splitlines()
        mounted_at = lines[-1].split()[-1] if result.ok and len(lines) > 1 else None
        if mounted_at in (None, "/"):
            raise ArtError(f"{self.root} on {self.connection.host} isn't on the external drive. "
                           "Is the drive plugged in and mounted?")
        if self.connection.exists(self.root):
            return
        try:
            self.connection.mkdir(self.root)
        except PermissionError:  # the drive's top folder belongs to root
            quoted = shlex.quote(self.root)
            user = shlex.quote(self.settings.username)
            both = f"mkdir -p {quoted} && chown {user}: {quoted}"  # sudo for both, not just one
            result = self.connection.run(f"sh -c {shlex.quote(both)}", sudo=True)
            if not result.ok:
                raise ArtError(f"Couldn't make {self.root} on {self.connection.host}: "
                               f"{result.output.strip()}") from None

    def names(self) -> list[str]:
        """The shrines that have art here, in name order."""
        return sorted((name for name in self.connection.listdir(self.root)
                       if not name.startswith(".")
                       and self.connection.exists(self._folder(name, RECORD))),
                      key=shrine.natural_key)

    def has_folder(self, name: str) -> bool:
        return self.connection.exists(self._folder(name))

    def load(self, name: str) -> Record | None:
        try:
            return Record.from_json(self.connection.read_bytes(self._folder(name, RECORD))
                                    .decode("utf-8"))
        except FileNotFoundError:
            return None

    def save_record(self, record: Record) -> None:
        """Written last, so a shrine only shows up here once its art is all in place."""
        data = io.BytesIO(record.to_json().encode("utf-8"))
        self.connection.mkdir(self._folder(record.name))  # a title-only shrine has no art
        self.connection.upload(data, self._folder(record.name, RECORD))

    def set_aside(self, name: str) -> str:
        """Move a shrine's folder into .replaced, rather than deleting anyone's art."""
        aside = posixpath.join(self.root, REPLACED)
        self.connection.mkdir(aside)
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        target = posixpath.join(aside, f"{name}-{stamp}")
        self.connection.rename(self._folder(name), target)
        return target

    def free_space(self) -> int | None:
        result = self.connection.run(f"df -Pk {shlex.quote(self.root)}")
        try:
            return int(result.output.strip().splitlines()[-1].split()[3]) * 1024
        except (IndexError, ValueError):
            return None

    def _check_room(self, files: list[Path]) -> None:
        needed = sum(path.stat().st_size for path in files)
        free = self.free_space()
        if free is not None and free - SPARE_SPACE < needed:
            raise ArtError(f"The drive on {self.connection.host} is full.")

    def add_pictures(self, record: Record, pictures: list[tuple[Path, str]],
                     progress: Progress | None = None) -> None:
        """Copy pictures (path, its name) to the end of the shrine's pictures."""
        if not pictures:
            return
        self._check_room([path for path, _ in pictures])
        for part in ("", "pictures"):
            self.connection.mkdir(self._folder(record.name, part).rstrip("/"))
        for i, (path, label) in enumerate(pictures, start=1):
            name = stored_name(len(record.pictures) + 1, label)
            with path.open("rb") as f:
                self.connection.upload(f, self._folder(record.name, "pictures", name))
            record.pictures.append(name)
            if progress:
                progress(i / len(pictures))

    def add_videos(self, record: Record, videos: list[Path],
                   progress: Progress | None = None) -> None:
        """Keep copies of videos made for the TVs (same names are replaced)."""
        if not videos:
            return
        self._check_room(videos)
        for part in ("", "videos"):
            self.connection.mkdir(self._folder(record.name, part).rstrip("/"))
        for i, video in enumerate(videos, start=1):
            with video.open("rb") as f:
                self.connection.upload(f, self._folder(record.name, "videos", video.name))
            if shrine.is_part_of(video.name, record.name) and ".z." in video.name \
                    and video.name not in record.videos:
                record.videos.append(video.name)
            if progress:
                progress(i / len(videos))

    def get_pictures(self, record: Record, folder: Path,
                     progress: Progress | None = None) -> list[Path]:
        """Download the shrine's pictures, in order."""
        folder.mkdir(parents=True, exist_ok=True)
        paths = []
        for i, name in enumerate(record.pictures, start=1):
            path = folder / name
            self.connection.download(self._folder(record.name, "pictures", name), path)
            paths.append(path)
            if progress:
                progress(i / len(record.pictures))
        return paths
