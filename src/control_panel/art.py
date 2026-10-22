"""pi-files: where every shrine's art is kept, with the videos made from it.

    <art_dir>/tv1/<name>/<name>.a.title.png    its title card (always the first slide)
    <art_dir>/tv1/<name>/<name>.z.<piece>.jpg  each picture or video (pictures are shown in
                                               the order of their names)
    <art_dir>/tv1/<name>/.videos/              the videos made from them, for the TVs
    <art_dir>/backup/                          removed or replaced art, and videos that
                                               were taken off a TV

TV n plays the videos of the shrines in tv<n>, and TV 4 (the hallway) plays them all.
Nothing here asks questions.
"""

from __future__ import annotations

import datetime
import io
import json
import posixpath
import re
import shlex
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from control_panel import shrine
from control_panel.config import TvsSettings
from control_panel.ssh import PiConnection

TVS = (1, 2, 3, 4)
BACKUP = "backup"
VIDEOS = ".videos"
MADE_NOTE = ".made.json"  # in .videos: what its videos were made from
DELETED = ".deleted.json"  # videos deleted on purpose, to take off TVs without backing up
CHECK_FILE = ".control-panel-check"
SPARE_SPACE = 1_000_000_000  # bytes to leave free on the drive
PICTURE_TYPES = {".png", ".jpg", ".jpeg", ".jpe", ".jfif", ".jif", ".jfi", ".pjpeg", ".pjp",
                 ".gif", ".bmp", ".webp", ".tif", ".tiff", ".avif"}
VIDEO_TYPES = {".mp4", ".m4v", ".mov", ".avi", ".webm", ".mkv", ".ogv", ".mpg", ".mpeg", ".wmv",
               ".3gp", ".flv", ".mts", ".m2ts", ".ts"}
MAX_PIECE_NAME = 80  # characters, so names stay well under the drive's 255-byte limit

Progress = Callable[[int, int], None]  # (bytes done, total)


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
        """The shrine's name on the TVs (a folder copied there by hand may have spaces):
        firstname.lastname for a student, skills-canada for a collection."""
        if "." in self.folder:
            return shrine.clean_name(self.folder, space=".") or "art"
        return shrine.clean_name(self.folder, space="-") or "art"

    @property
    def videos(self) -> str:
        return posixpath.join(self.path, VIDEOS)


def is_title_card(file_name: str) -> bool:
    return ".a.title." in posixpath.basename(file_name).lower()


def is_picture(file_name: str) -> bool:
    """Going by its name: pictures are downloaded to check whether they're animations."""
    return posixpath.splitext(file_name)[1].lower() in PICTURE_TYPES


def is_art(file_name: str) -> bool:
    """A picture or video, going by its name (not e.g. hightower's .py scripts)."""
    ext = posixpath.splitext(file_name)[1].lower()
    return ext in PICTURE_TYPES or ext in VIDEO_TYPES


def piece(file_name: str, name: str) -> str:
    """The piece's own name, without the shrine's name in front: 'ann.lee.z.sun.jpg' ->
    'sun.jpg'. Pictures are shown in this order."""
    file_name = posixpath.basename(file_name)
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
    def key(file_name: str) -> tuple:
        stem, ext = posixpath.splitext(piece(file_name, name))
        return not is_title_card(file_name), shrine.natural_key(stem), ext, file_name
    return sorted(file_names, key=key)


def stored_name(name: str, label: str, taken: set[str]) -> str:
    """<name>.z.<piece>.<ext>, the name new art is kept under, with a piece name no other
    art in taken has (it's added to taken). label is its name where it came from, e.g.
    'Grade 9/Sun Set.JPG' (its folders are kept in the name, so they stay together)."""
    *folders, file_name = label.split("/")
    stem, ext = posixpath.splitext(file_name)
    folders = [f for f in folders if not f.lower().endswith(".zip")]
    stem, ext = _piece_stem("-".join([*folders, stem])), _extension(ext)
    used = {posixpath.splitext(t)[0].lower() for t in taken}
    candidate, n = f"{name}.z.{stem}", 2
    while candidate.lower() in used:
        candidate, n = f"{name}.z.{stem}-{n}", n + 1
    taken.add(candidate + ext)
    return candidate + ext


def video_name(path: str, paths: list[str], name: str) -> str:
    """The name of the video made from a video or animation. A piece already kept under
    the shrine's name keeps it (name.z.Sunset_Clip.mov -> name.z.Sunset_Clip.mp4), so
    hightower's videos keep the names the TVs know them by; any other piece is
    name.z.<piece>.mp4. Its type is added if another piece would get the same name (e.g.
    clip.gif and clip.mov)."""
    def stem_of(p: str) -> str:
        stem = posixpath.splitext(posixpath.basename(p))[0]
        if stem.lower().startswith(f"{name}.") and stem.lower() != f"{name}.a":
            return re.sub(r"[^A-Za-z0-9._-]", "_", stem)
        return f"{name}.z.{posixpath.splitext(piece(p, name))[0]}"

    stem, ext = stem_of(path), posixpath.splitext(path)[1].lower()
    clash = any(p != path and stem_of(p) == stem for p in paths)
    return f"{stem}{'-' + ext[1:] if clash and ext else ''}.mp4"


def owner_of(file_name: str) -> str | None:
    """Whose shrine a file is, going by its name: 'ann.lee.z.clip.mp4' -> 'ann.lee'."""
    owner = re.split(r"\.[az]\.", file_name, maxsplit=1)[0]
    return None if owner == file_name or not owner else owner


def _below(top: str, path: str) -> list[str]:
    """The folders from just below top down to path: ('/a', '/a/b/c') -> ['/a/b', '/a/b/c']."""
    folders = []
    while path != top and path != "/":
        folders.insert(0, path)
        path = posixpath.dirname(path)
    return folders


class ArtServer:
    """The shrines on pi-files, given an open connection. Errors from the connection
    (OSError, paramiko.SSHException) are left for the caller to report."""

    def __init__(self, connection: PiConnection, settings: TvsSettings):
        self.connection = connection
        self.settings = settings
        self.root = settings.art_dir.rstrip("/") or "/"
        self.host = settings.art_host
        self.unreadable: set[str] = set()  # folders with names in them that aren't text

    def check_drive(self) -> None:
        """Make sure the art folder is on its own drive (not the Pi's SD card), and that the
        app can save things in it."""
        if not self.root.startswith("/"):
            raise ArtError(f"art_dir in the config file should start with /, like /mnt/ssd "
                           f"(it's {self.root}).")
        existing = self.root
        while existing != "/" and not self.connection.exists(existing):
            existing = posixpath.dirname(existing)
        result = self.connection.run(f"df -Pk {shlex.quote(existing)}")
        lines = result.output.strip().splitlines()
        fields = lines[-1].split() if result.ok and len(lines) > 1 else []
        if len(fields) < 6 or fields[-1] == "/":
            raise ArtError(f"{self.root} on {self.host} isn't on the external drive. Is the "
                           "drive plugged in and mounted?")
        check = posixpath.join(self.root, CHECK_FILE)
        try:
            for path in _below(existing, self.root):  # the folders that aren't there yet
                self.connection.mkdir(path)
            self.connection.upload(io.BytesIO(b"ok\n"), check)
            self.connection.remove(check)
        except PermissionError:
            if existing != self.root:
                raise ArtError(f"pi-files won't let the app make {self.root}. On pi-files, "
                               f"run: sudo mkdir -p {self.root} && "
                               f"sudo chown -R {self.settings.username}: {self.root}") from None
            raise self.not_allowed(self.root) from None
        except OSError as e:
            if fields[3].isdigit() and int(fields[3]) < 1024:
                raise ArtError(f"The drive on {self.host} is full.") from e
            raise ArtError(f"Couldn't save anything in {self.root} on {self.host} ({e}). Its "
                           "drive may have gone read-only: try restarting pi-files.") from e

    def not_allowed(self, path: str) -> ArtError:
        return ArtError(f"pi-files won't let the app save in {path}. On pi-files, run: "
                        f"sudo chown -R {self.settings.username}: {self.root}")

    def _list(self, path: str) -> tuple[list[str], dict[str, int]]:
        """(folders, {file: size}) in a folder; nothing if it isn't there."""
        if not self.connection.exists(path):
            return [], {}
        try:
            return self.connection.list_folder(path)
        except UnicodeDecodeError:  # SFTP can't list it, so list it on pi-files instead
            pass
        result = self.connection.run(f"find -L {shlex.quote(path)} -mindepth 1 -maxdepth 1 "
                                     r"-printf '%y\t%s\t%f\n' 2>/dev/null")
        folders, files = [], {}
        for line in result.output.splitlines():
            kind, _, rest = line.partition("\t")
            size, _, file_name = rest.partition("\t")
            if "�" in file_name or not file_name or not size.isdigit():
                self.unreadable.add(path)  # left out, as it can't be named
            elif kind == "d":
                folders.append(file_name)
            elif kind == "f":
                files[file_name] = int(size)
        return folders, files

    def tv_path(self, tv: int) -> str:
        return posixpath.join(self.root, f"tv{tv}")

    def shrines(self, tv: int) -> list[ArtFolder]:
        """The shrines in a TV's folder, by name."""
        folders = [f for f in self._list(self.tv_path(tv))[0] if not f.startswith(".")]
        return [ArtFolder(tv, posixpath.join(self.tv_path(tv), f), f)
                for f in sorted(folders, key=shrine.natural_key)]

    def find(self, text: str, *, exact: bool = False) -> list[ArtFolder]:
        """The shrines whose names have text in them (or are text, if exact)."""
        wanted = text.casefold()
        return [art for tv in TVS for art in self.shrines(tv)
                if (wanted in (art.name, art.folder.casefold()) if exact
                    else wanted in art.folder.casefold())]

    def tidy(self, tv: int) -> list[str]:
        """Put files left loose in a TV's folder, as hightower kept them, where they go now:
        a shrine's video (name.z.clip.mp4) in its folder, and a slideshow (name.a.mp4, made
        from the pictures now) in backup. Other loose videos stay, and play as they are.
        Returns what it did, and what it left."""
        tv_path = self.tv_path(tv)
        said = []
        for file_name in sorted(self._list(tv_path)[1]):
            path = posixpath.join(tv_path, file_name)
            owner = owner_of(file_name)
            if file_name.startswith(".") or not is_art(file_name):
                continue  # e.g. hightower's scripts
            if owner is None:
                if posixpath.splitext(file_name)[1].lower() != ".mp4":
                    said.append(f"tv{tv}/{file_name} isn't in a shrine's folder, so it isn't "
                                "shown. Move it into one on pi-files.")
            elif file_name.endswith(".a.mp4"):
                self.set_aside(path, f"tv{tv}-{file_name}")
                said.append(f"Moved tv{tv}/{file_name} to {BACKUP}: slideshows are made from "
                            "the pictures now.")
            elif self.connection.exists(posixpath.join(tv_path, owner, file_name)):
                said.append(f"tv{tv}/{file_name} is also in tv{tv}/{owner}, so it was left.")
            else:
                self._mkdir(posixpath.join(tv_path, owner))
                self._rename(path, posixpath.join(tv_path, owner, file_name))
                said.append(f"Moved tv{tv}/{file_name} into tv{tv}/{owner}.")
        return said

    def loose_videos(self, tv: int) -> dict[str, int]:
        """{file name: size} for the videos left in a TV's folder that aren't any shrine's
        (e.g. tv3/0_RubeGoldberg-Webb.mp4): they play as they are."""
        return {f: size for f, size in self._list(self.tv_path(tv))[1].items()
                if f.lower().endswith(".mp4") and not f.startswith(".") and owner_of(f) is None}

    def art_files(self, art: ArtFolder) -> dict[str, int]:
        """{path: size} for a shrine's art."""
        return {posixpath.join(art.path, f): size
                for f, size in self._list(art.path)[1].items() if not f.startswith(".")}

    def subfolders(self, art: ArtFolder) -> list[str]:
        """Folders in a shrine's folder (its art isn't looked for in them)."""
        return [f for f in self._list(art.path)[0] if not f.startswith(".")]

    def videos(self, art: ArtFolder) -> dict[str, int]:
        """{file name: size} for the videos made from a shrine's art."""
        return {f: size for f, size in self._list(art.videos)[1].items()
                if f.endswith(".mp4") and not f.startswith(".")}

    def folder_for(self, name: str, tv: int) -> ArtFolder:
        """A new shrine's folder, in its TV's folder."""
        art = ArtFolder(tv, posixpath.join(self.tv_path(tv), name), name)
        self._mkdir(self.tv_path(tv))
        self._mkdir(art.path)
        return art

    def rename(self, art: ArtFolder, name: str, tv: int) -> ArtFolder:
        """Rename a shrine (into tv's folder), and its art and videos with it:
        old.name.z.sun.png -> new.name.z.sun.png. The note of what its videos were made from
        is renamed too, so they aren't made again."""
        def renamed(file_name: str) -> str:
            if file_name.lower().startswith(f"{art.name}."):
                return name + file_name[len(art.name):]
            return file_name

        new = ArtFolder(tv, posixpath.join(self.tv_path(tv), name), name)
        self._mkdir(self.tv_path(tv))
        self._rename(art.path, new.path)
        for folder in (new.path, new.videos):
            for file_name in self._list(folder)[1]:
                if renamed(file_name) != file_name:
                    self._rename(posixpath.join(folder, file_name),
                                 posixpath.join(folder, renamed(file_name)))
        note_path = posixpath.join(new.videos, MADE_NOTE)
        if note := self.read_json(note_path):
            note["pictures"] = [[renamed(f), size] for f, size in note.get("pictures", [])]
            note["animated"] = [renamed(f) for f in note.get("animated", [])]
            note["sources"] = {renamed(video): [renamed(f), size]
                               for video, (f, size) in note.get("sources", {}).items()}
            note["unusable"] = {renamed(f): why for f, why in note.get("unusable", {}).items()}
            self.write_json(note_path, note)
        return new

    def read_json(self, path: str) -> dict:
        """A small JSON file's contents ({} if it isn't there or is damaged)."""
        if not self.connection.exists(path):
            return {}
        with tempfile.TemporaryDirectory() as folder:
            local = Path(folder) / "file.json"
            self.connection.download(path, local)
            try:
                data = json.loads(local.read_text(encoding="utf-8"))
            except ValueError:
                return {}
        return data if isinstance(data, dict) else {}

    def write_json(self, path: str, data: dict) -> None:
        self.receive(io.BytesIO(json.dumps(data, indent=1).encode("utf-8")), path)

    def upload(self, local: Path, path: str, progress: Progress | None = None) -> None:
        """Copy a file from this computer to pi-files (making its folder if need be)."""
        try:
            f = local.open("rb")
        except OSError as e:  # a problem with the file here isn't pi-files'
            raise ArtError(f"Couldn't read {local.name} on this computer ({e.strerror or e}). "
                           "If it was on a USB stick, is it still plugged in?") from e
        with f:
            self.receive(f, path, progress)

    def receive(self, fileobj: BinaryIO, path: str, progress: Progress | None = None) -> None:
        """Save what's read from fileobj on pi-files (making its folders if need be)."""
        for folder in _below(self.root, posixpath.dirname(path)):
            self._mkdir(folder)
        try:
            self.connection.upload(fileobj, path, progress)
        except PermissionError:
            raise self.not_allowed(posixpath.dirname(path)) from None

    def download(self, path: str, local: Path, progress: Progress | None = None) -> None:
        self.connection.download(path, local, progress)

    def open(self, path: str) -> BinaryIO:
        """A file on pi-files, to read (e.g. to copy it straight to a TV)."""
        return self.connection.open(path)

    def remove(self, path: str) -> None:
        self.connection.remove(path)

    def copy(self, path: str, new_path: str) -> None:
        """Copy a file on pi-files itself (quick: nothing comes over the network)."""
        for folder in _below(self.root, posixpath.dirname(new_path)):
            self._mkdir(folder)
        result = self.connection.run(f"cp -- {shlex.quote(path)} {shlex.quote(new_path)}",
                                     timeout=600)
        if not result.ok:
            raise ArtError(f"Couldn't copy {path} on pi-files: {result.output.strip()}")

    def backup_path(self, label: str) -> str:
        """A new place in the backup folder: <stamp>-<label>."""
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        base = posixpath.join(self.root, BACKUP, f"{stamp}-{label}")
        target, n = base, 2
        while self.connection.exists(target):
            target, n = f"{base}-{n}", n + 1
        return target

    def set_aside(self, path: str, label: str) -> str:
        """Move a file or folder into the backup folder, rather than deleting it."""
        self._mkdir(posixpath.join(self.root, BACKUP))
        target = self.backup_path(label)
        self._rename(path, target)
        return target

    def put_back(self, aside: str, path: str) -> None:
        """Undo set_aside()."""
        self._rename(aside, path)

    def deleted(self) -> set[str]:
        """Videos deleted on purpose that may still be on a TV that was off."""
        return set(self.read_json(posixpath.join(self.root, DELETED)).get("videos", []))

    def note_deleted(self, videos: set[str]) -> None:
        self.write_json(posixpath.join(self.root, DELETED), {"videos": sorted(videos)})

    def delete(self, path: str) -> None:
        """Delete a folder and everything in it, for good."""
        result = self.connection.run(f"rm -rf -- {shlex.quote(path)}")
        if not result.ok:
            raise ArtError(f"Couldn't delete {path}: {result.output.strip()}")

    def _mkdir(self, path: str) -> None:
        try:
            self.connection.mkdir(path)
        except PermissionError:
            raise self.not_allowed(posixpath.dirname(path)) from None

    def _rename(self, old: str, new: str) -> None:
        try:
            self.connection.rename(old, new)
        except PermissionError:
            raise self.not_allowed(posixpath.dirname(old)) from None
