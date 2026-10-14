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
    """One shrine's folder on pi-files. (A shrine hightower only kept videos for has none
    yet: it's made when art is first added.)"""

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
    return ".a.title." in posixpath.basename(file_name).lower()


def is_picture(file_name: str) -> bool:
    """Going by its name: pictures are downloaded to check what's really in them."""
    return posixpath.splitext(file_name)[1].lower() in PICTURE_TYPES


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


def same_piece(file_name: str, name: str) -> str:
    """What new art is compared with, to ask before it replaces old art: the piece's name,
    without the type for pictures (hightower turned some GIFs and JPEGs into PNGs)."""
    stem, ext = posixpath.splitext(piece(file_name, name))
    return stem + (".picture" if is_picture(file_name) else ext)


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
    """<name>.z.<piece>.<ext>, the name new art is kept under (made unique among taken,
    which it's added to). label is its name where it came from, e.g. 'Grade 9/Sun Set.JPG'
    (its folders are kept in the name, so the slideshow keeps them together)."""
    *folders, file_name = label.split("/")
    stem, ext = posixpath.splitext(file_name)
    folders = [f for f in folders if not f.lower().endswith(".zip")]
    stem, ext = _piece_stem("-".join([*folders, stem])), _extension(ext)
    lowered = {t.lower() for t in taken}
    candidate, n = f"{name}.z.{stem}{ext}", 2
    while candidate.lower() in lowered:
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


def _local_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError as e:
        raise _unreadable_here(path, e) from e


def _unreadable_here(path: Path, e: OSError) -> ArtError:
    return ArtError(f"Couldn't read {path.name} on this computer ({e.strerror or e}). If it was "
                    "on a USB stick, is it still plugged in?")


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
            if "\ufffd" in file_name or not file_name or not size.isdigit():
                self.unreadable.add(path)  # left out, as it can't be named
            elif kind == "d":
                folders.append(file_name)
            elif kind == "f":
                files[file_name] = int(size)
        return folders, files

    def tv_folders(self) -> dict[int, list[str]]:
        """{TV number: its folders}, for the TV folders there are (tv1, TV 2...). The one
        new shrines go in comes first."""
        folders: dict[int, list[str]] = {}
        for entry in sorted(self._list(self.root)[0], key=lambda e: (e.lower() != e, e)):
            match = _TV_FOLDER.fullmatch(entry)
            if match:
                folders.setdefault(int(match.group(1)), []).append(
                    posixpath.join(self.root, entry))
        return folders

    def _tv_folder(self, tv: int) -> str:
        return (self.tv_folders().get(tv) or [posixpath.join(self.root, f"tv{tv}")])[0]

    def find(self, text: str, *, exact: bool = False) -> list[ArtFolder]:
        """The shrines whose names have text in them (or are text, if exact), in TV order,
        then name order. Includes shrines hightower only kept loose videos for."""
        wanted = text.casefold()

        def matches(art: ArtFolder) -> bool:
            if exact:
                return wanted in (art.name, art.folder.casefold())
            return wanted in art.folder.casefold()

        found = []
        for tv, tv_paths in sorted(self.tv_folders().items()):
            for tv_path in tv_paths:
                folders, files = self._list(tv_path)
                shrines = {f for f in folders if not f.startswith(".")}
                have = {f.casefold() for f in shrines}
                for file_name in files:  # videos only, no folder
                    owner = file_name.split(".z.", 1)[0] if ".z." in file_name else ""
                    if owner and not owner.startswith(".") and owner.casefold() not in have:
                        shrines.add(owner)
                        have.add(owner.casefold())
                found += [art for art in (ArtFolder(tv, posixpath.join(tv_path, f), f)
                                          for f in shrines) if matches(art)]
        return sorted(found, key=lambda a: (a.tv, shrine.natural_key(a.folder), a.path))

    def art_files(self, art: ArtFolder) -> dict[str, int]:
        """{path: size} for a shrine's art: what's in its folder, and its loose videos."""
        files = {}
        if self.connection.exists(art.path):
            files = {posixpath.join(art.path, f): size for f, size in self._list(art.path)[1]
                     .items() if not f.startswith(".")}
        tv_path = posixpath.dirname(art.path)
        for file_name, size in self._list(tv_path)[1].items():
            if file_name.lower().startswith(f"{art.name}.z.") and not is_picture(file_name):
                files[posixpath.join(tv_path, file_name)] = size
        return files

    def folder_for(self, name: str, tv: int) -> ArtFolder:
        """Where a new shrine's art goes, in its TV's folder (which is made if need be)."""
        tv_path = self._tv_folder(tv)
        self._mkdir(tv_path)
        return ArtFolder(tv, posixpath.join(tv_path, name), name)

    def staging(self, final: ArtFolder) -> ArtFolder:
        """A hidden, empty folder to copy a new shrine's art into, before it's put in place."""
        path = posixpath.join(posixpath.dirname(final.path), f".{final.folder}.new")
        if self.connection.exists(path):  # left from a try that didn't finish
            self.set_aside(path, f"{final.folder}-unfinished")
        self._mkdir(path)
        return ArtFolder(final.tv, path, final.folder)

    def put_in_place(self, staging: ArtFolder, final: ArtFolder) -> None:
        self._rename(staging.path, final.path)

    def ensure(self, art: ArtFolder) -> None:
        """Make a shrine's folder, if it's one hightower only kept videos for."""
        for path in (posixpath.dirname(art.path), art.path):
            self._mkdir(path)

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

    def hashes(self, paths: list[str]) -> dict[str, str]:
        """{path: sha256} for some files."""
        if not paths:
            return {}
        quoted = " ".join(shlex.quote(path) for path in paths)
        result = self.connection.run(f"sha256sum -- {quoted}", timeout=600)
        hashes = {}
        for line in result.output.split("\n"):
            escaped = line.startswith("\\")  # the name has a \ or a new line in it
            digest, _, path = line.removeprefix("\\").partition("  ")
            if escaped:
                path = re.sub(r"\\(.)", lambda m: {"n": "\n", "r": "\r"}.get(m[1], m[1]), path)
            if re.fullmatch(r"[0-9a-f]{64}", digest):
                hashes[path] = digest
        return {path: hashes[path] for path in paths if path in hashes}

    def check_room(self, files: list[Path]) -> None:
        needed = sum(_local_size(path) for path in files)
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
            f = local.open("rb")
        except OSError as e:  # a problem with the file here isn't pi-files'
            raise _unreadable_here(local, e) from e
        with f:
            try:
                self.connection.upload(f, posixpath.join(art.path, file_name), progress)
            except PermissionError:
                raise self.not_allowed(art.path) from None

    def stage(self, art: ArtFolder, files: list[tuple[Path, str]],
              progress: Progress | None = None) -> list[tuple[str, str]]:
        """Copy files into a shrine's folder under hidden names, to be put in place all at
        once by commit(): a copy that stops part way leaves nothing for Add art to see.
        files: (file here, name on pi-files). Returns (hidden path, path) for each."""
        staged: list[tuple[str, str]] = []
        try:
            for i, (local, file_name) in enumerate(files, start=1):
                hidden = f".{file_name}.new"
                self.upload(art, local, hidden)
                staged.append((posixpath.join(art.path, hidden),
                               posixpath.join(art.path, file_name)))
                if progress:
                    progress(i / len(files))
        except BaseException:
            self.unstage(staged)
            raise
        return staged

    def unstage(self, staged: list[tuple[str, str]]) -> None:
        for hidden, _ in staged:
            try:
                self.connection.remove(hidden)
            except Exception:  # noqa: BLE001 (it's hidden, so it does no harm if it stays)
                pass

    def commit(self, staged: list[tuple[str, str]]) -> None:
        for hidden, path in staged:
            if self.connection.exists(path):  # never write over anyone's art
                raise ArtError(f"pi-files already has {path}.")
            self._rename(hidden, path)

    def put_back(self, aside: str, path: str) -> None:
        """Undo set_aside()."""
        self._rename(aside, path)

    def download(self, paths: list[str], folder: Path,
                 progress: Progress | None = None) -> list[Path]:
        """Copy some files here, in the same order. They're saved under numbers, since names
        from pi-files might not be safe ones on Windows."""
        folder.mkdir(parents=True, exist_ok=True)
        got = []
        for i, path in enumerate(paths, start=1):
            local = folder / f"{i:04d}{_extension(posixpath.splitext(path)[1])}"
            self.connection.download(path, local)
            got.append(local)
            if progress:
                progress(i / len(paths))
        return got

    def fetch(self, path: str, folder: Path) -> Path:
        """Copy one file here to look at, under a name that's safe on Windows."""
        folder.mkdir(parents=True, exist_ok=True)
        stem, ext = posixpath.splitext(posixpath.basename(path))
        safe = re.sub(r"[^A-Za-z0-9 ._-]", "_", stem)[:80].strip(" .") or "art"
        local = folder / f"{len(list(folder.iterdir())) + 1:02d}-{safe}{_extension(ext)}"
        self.connection.download(path, local)
        return local

    def set_aside(self, path: str, label: str) -> str:
        """Move a folder or file into .replaced, rather than deleting anyone's art."""
        aside = posixpath.join(self.root, REPLACED)
        self._mkdir(aside)
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        base, n = posixpath.join(aside, f"{stamp}-{label}"), 2
        target = base
        while self.connection.exists(target):
            target, n = f"{base}-{n}", n + 1
        self._rename(path, target)
        return target

    def move(self, art: ArtFolder, tv: int) -> ArtFolder | None:
        """Move a shrine's folder to another TV's folder. None if there's one there already."""
        tv_path = self._tv_folder(tv)
        moved = ArtFolder(tv, posixpath.join(tv_path, art.folder), art.folder)
        self._mkdir(tv_path)
        if self.connection.exists(moved.path):
            return None
        self._rename(art.path, moved.path)
        return moved
