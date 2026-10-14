"""The art server, pi-files: where each shrine's original art is kept, like the old hightower.

A shrine's slideshow is a video, and taking pictures back out of a video and making it again
loses a little quality every time. So the originals are kept on a Pi with an external drive,
and adding art to a shrine makes its slideshow again from them. There's a folder for each TV,
and in it a folder for each shrine:

    <art_dir>/tv1/<name>/<name>.a.title.png    its title card (always shown first)
    <art_dir>/tv1/<name>/<name>.z.<piece>.jpg  each picture or video; pictures are shown in
                                               the order of their names
    <art_dir>/.replaced/                       art that was replaced, rather than deleted

Files copied there by hand can have any names. Videos left loose in the TV's folder, as
hightower had them (<art_dir>/tv1/<name>.z.<piece>.mp4), count as the shrine's too.
Nothing here asks questions.
"""

from __future__ import annotations

import datetime
import hashlib
import io
import posixpath
import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from control_panel import shrine
from control_panel.config import TvsSettings
from control_panel.ssh import PiConnection

TVS = (1, 2, 3, 4)
REPLACED = ".replaced"
CHECK_FILE = ".control-panel-check"
SPARE_SPACE = 1_000_000_000  # bytes to leave free on the drive
PICTURE_TYPES = {".png", ".jpg", ".jpeg", ".jfif", ".gif", ".bmp", ".webp", ".tif", ".tiff"}
MAX_PIECE_NAME = 80  # characters, so names stay well under the drive's 255-byte limit

_TV_FOLDER = re.compile(r"tv[ _-]?([1-4])", re.IGNORECASE)


class ArtError(Exception):
    """Something the person can fix. The message says what."""


@dataclass(frozen=True)
class ArtFolder:
    """One shrine's folder on pi-files."""

    tv: int
    path: str  # e.g. /mnt/ssd/tv1/ann.lee
    folder: str  # the folder's own name, as it is on pi-files

    @property
    def name(self) -> str:
        """The shrine's name on the TVs (a folder copied there by hand may have spaces).
        Like Make a shrine: firstname.lastname for a student, skills-canada for a collection."""
        if "." in self.folder:
            return shrine.clean_name(self.folder, space=".") or "art"
        return shrine.clean_name(self.folder, space="-") or "art"


def is_title_card(file_name: str) -> bool:
    return ".a.title." in file_name.lower()


def is_picture(file_name: str) -> bool:
    """Going by its name: pictures are downloaded to check what's really in them."""
    return posixpath.splitext(file_name)[1].lower() in PICTURE_TYPES


def piece(file_name: str, name: str) -> str:
    """The piece's own name, without the shrine's name in front: 'ann.lee.z.sun.jpg' ->
    'sun.jpg'. Pictures are shown in this order, and new art with the same piece name as
    old art is asked about."""
    lowered = file_name.lower()
    for prefix in (f"{name}.z.", f"{name}.a.", f"{name}."):
        if lowered.startswith(prefix):
            file_name = file_name[len(prefix):]
            break
    stem, ext = posixpath.splitext(file_name)
    return _piece_stem(stem) + _extension(ext)


def _piece_stem(stem: str) -> str:
    return shrine.clean_name(stem).replace(".", "-")[:MAX_PIECE_NAME].strip("._-") or "art"


def _extension(ext: str) -> str:
    return ext.lower() if re.fullmatch(r"\.[a-z0-9]{1,5}", ext.lower()) else ""


def order(file_names: list[str], name: str) -> list[str]:
    """The order a slideshow shows them in: title card first, then by piece name."""
    return sorted(file_names, key=lambda f: (not is_title_card(f),
                                             shrine.natural_key(piece(f, name)), f))


def stored_name(name: str, label: str, taken: set[str]) -> str:
    """<name>.z.<piece>.<ext>, the name new art is kept under (made unique among taken,
    which it's added to). label is its name where it came from, e.g. 'sub/Sun Set.JPG'."""
    stem, ext = posixpath.splitext(posixpath.basename(label))
    stem, ext = _piece_stem(stem), _extension(ext)
    lowered = {t.lower() for t in taken}
    candidate, n = f"{name}.z.{stem}{ext}", 2
    while candidate in lowered:
        candidate, n = f"{name}.z.{stem}-{n}{ext}", n + 1
    taken.add(candidate)
    return candidate


def _below(top: str, path: str) -> list[str]:
    """The folders from just below top down to path: ('/a', '/a/b/c') -> ['/a/b', '/a/b/c']."""
    folders = []
    while path != top and path != "/":
        folders.insert(0, path)
        path = posixpath.dirname(path)
    return folders


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


Progress = Callable[[float], None]


class ArtServer:
    """The shrines' art on pi-files, given an open connection. Errors from the connection
    (OSError, paramiko.SSHException) are left for the caller to report."""

    def __init__(self, connection: PiConnection, settings: TvsSettings):
        self.connection = connection
        self.settings = settings
        self.root = settings.art_dir.rstrip("/") or "/"
        self.host = settings.art_host

    def check_drive(self) -> None:
        """Make sure the art folder is on its own drive (not the Pi's SD card), and that the
        app can save things in it."""
        if not self.root.startswith("/"):
            raise ArtError(f"art_dir in the config file should start with /, like /mnt/ssd "
                           f"(it's {self.root}).")
        existing = self.root
        while existing != "/" and not self.connection.exists(existing):
            existing = posixpath.dirname(existing)
        result = self.connection.run(f"df -P {shlex.quote(existing)}")
        lines = result.output.strip().splitlines()
        mounted_at = lines[-1].split()[-1] if result.ok and len(lines) > 1 else None
        if mounted_at in (None, "/"):
            raise ArtError(f"{self.root} on {self.host} isn't on the external drive. Is the "
                           "drive plugged in and mounted?")
        check = posixpath.join(self.root, CHECK_FILE)
        try:
            for path in _below(existing, self.root):  # the folders that aren't there yet
                self.connection.mkdir(path)
            self.connection.upload(io.BytesIO(b"ok\n"), check)
            self.connection.remove(check)
        except PermissionError:
            raise self.not_allowed(self.root) from None
        except OSError as e:
            raise ArtError(f"Couldn't save anything in {self.root} on {self.host} ({e}). Its "
                           "drive may have gone read-only: try restarting pi-files.") from e

    def not_allowed(self, path: str) -> ArtError:
        return ArtError(f"pi-files won't let the app save in {path}. On pi-files, run: "
                        f"sudo chown -R {self.settings.username}: {self.root}")

    def tv_folders(self) -> dict[int, str]:
        """{TV number: its folder}, for the TV folders there are (tv1, TV 2...)."""
        folders: dict[int, str] = {}
        for entry in sorted(self.connection.list_folder(self.root)[0]):
            match = _TV_FOLDER.fullmatch(entry)
            if match:
                folders.setdefault(int(match.group(1)), posixpath.join(self.root, entry))
        return folders

    def find(self, text: str, *, exact: bool = False) -> list[ArtFolder]:
        """The shrines whose folder names have text in them (or are text, if exact), in
        TV order, then name order."""
        wanted = text.casefold()
        found = []
        for tv, tv_path in sorted(self.tv_folders().items()):
            for folder in self.connection.list_folder(tv_path)[0]:
                art = ArtFolder(tv, posixpath.join(tv_path, folder), folder)
                if folder.startswith("."):
                    continue
                if (art.name == wanted or folder.casefold() == wanted) if exact \
                        else wanted in folder.casefold():
                    found.append(art)
        return sorted(found, key=lambda a: (a.tv, shrine.natural_key(a.folder)))

    def files(self, art: ArtFolder) -> dict[str, int]:
        """{file name: size} for the art in a shrine's folder."""
        files = self.connection.list_folder(art.path)[1]
        return {name: size for name, size in files.items() if not name.startswith(".")}

    def loose_videos(self, art: ArtFolder) -> list[str]:
        """The shrine's videos in its TV's folder, as hightower kept them."""
        tv_path = posixpath.dirname(art.path)
        return [f for f in self.connection.list_folder(tv_path)[1]
                if f.lower().startswith(f"{art.name}.z.") and not is_picture(f)]

    def folder_for(self, name: str, tv: int) -> ArtFolder:
        """A new shrine's folder (made, along with its TV's folder if need be)."""
        tv_path = self.tv_folders().get(tv) or posixpath.join(self.root, f"tv{tv}")
        art = ArtFolder(tv, posixpath.join(tv_path, name), name)
        for path in (tv_path, art.path):
            self._mkdir(path)
        return art

    def _mkdir(self, path: str) -> None:
        try:
            self.connection.mkdir(path)
        except PermissionError:
            raise self.not_allowed(posixpath.dirname(path)) from None

    def hashes(self, art: ArtFolder, file_names: list[str]) -> dict[str, str]:
        """{file name: sha256} for some of the files in a shrine's folder."""
        if not file_names:
            return {}
        paths = " ".join(shlex.quote(posixpath.join(art.path, f)) for f in file_names)
        result = self.connection.run(f"sha256sum -- {paths}", timeout=600)
        hashes = {}
        for line in result.output.splitlines():
            digest, _, path = line.partition("  ")
            if re.fullmatch(r"[0-9a-f]{64}", digest):
                hashes[posixpath.basename(path)] = digest
        return hashes

    def check_room(self, files: list[Path]) -> None:
        needed = sum(path.stat().st_size for path in files)
        result = self.connection.run(f"df -Pk {shlex.quote(self.root)}")
        try:
            free = int(result.output.strip().splitlines()[-1].split()[3]) * 1024
        except (IndexError, ValueError):
            return
        if free - SPARE_SPACE < needed:
            raise ArtError(f"The drive on {self.host} is full.")

    def upload(self, art: ArtFolder, local: Path, file_name: str,
               progress: Callable[[int, int], None] | None = None) -> None:
        try:
            with local.open("rb") as f:
                self.connection.upload(f, posixpath.join(art.path, file_name), progress)
        except PermissionError:
            raise self.not_allowed(art.path) from None

    def download(self, art: ArtFolder, file_names: list[str], folder: Path,
                 progress: Progress | None = None) -> list[Path]:
        """Copy some of a shrine's files here, in the same order. They're saved under
        numbers, since names from pi-files might not be safe ones on Windows."""
        folder.mkdir(parents=True, exist_ok=True)
        paths = []
        for i, file_name in enumerate(file_names, start=1):
            path = folder / f"{i:04d}{_extension(posixpath.splitext(file_name)[1])}"
            self.connection.download(posixpath.join(art.path, file_name), path)
            paths.append(path)
            if progress:
                progress(i / len(file_names))
        return paths

    def set_aside(self, path: str, label: str) -> str:
        """Move a folder or file into .replaced, rather than deleting anyone's art."""
        aside = posixpath.join(self.root, REPLACED)
        self._mkdir(aside)
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        base, n = posixpath.join(aside, f"{label}-{stamp}"), 2
        target = base
        while self.connection.exists(target):
            target, n = f"{base}-{n}", n + 1
        try:
            self.connection.rename(path, target)
        except PermissionError:
            raise self.not_allowed(posixpath.dirname(path)) from None
        return target

    def move(self, art: ArtFolder, tv: int) -> ArtFolder | None:
        """Move a shrine's folder to another TV's folder. None if there's one there already."""
        tv_path = self.tv_folders().get(tv) or posixpath.join(self.root, f"tv{tv}")
        moved = ArtFolder(tv, posixpath.join(tv_path, art.folder), art.folder)
        self._mkdir(tv_path)
        if self.connection.exists(moved.path):
            return None
        try:
            self.connection.rename(art.path, moved.path)
        except PermissionError:
            raise self.not_allowed(posixpath.dirname(art.path)) from None
        return moved
