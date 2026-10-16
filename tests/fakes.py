"""Stand-ins for the Pis, shared by the tests."""

import hashlib
import io
import posixpath
import shlex
from pathlib import Path

from control_panel.ssh import CommandResult


class FakeFilesConnection:
    """Pretends to be pi-files: files in memory, with its drive mounted at /mnt/ssd.

    locked: folders the app isn't allowed to change (e.g. they belong to root).
    """

    def __init__(self, mounted=True, free_kb=100_000_000, locked=(), read_only=False,
                 not_text=()):
        self.host = "pi-files.hackerspace.tbl"
        self.files = {}  # path: contents
        self.dirs = {"/", "/mnt", "/mnt/ssd"}
        self.mounted = mounted
        self.free_kb = free_kb
        self.locked = set(locked)
        self.read_only = read_only
        self.not_text = set(not_text)  # names that aren't UTF-8 (shown as \ufffd by run)
        self.commands = []

    def add(self, path, data=b"art"):
        """Put a file there, as if it was copied by hand (with its folders)."""
        folder = posixpath.dirname(path)
        while folder not in self.dirs:
            self.dirs.add(folder)
            folder = posixpath.dirname(folder)
        self.files[path] = data

    def run(self, command, *, sudo=False, timeout=60):
        self.commands.append((command, sudo))
        if command.startswith("df "):
            mount = "/mnt/ssd" if self.mounted else "/"
            return CommandResult(0, "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
                                    f"/dev/sda1 1000000 10 {self.free_kb} 1% {mount}\n")
        if command.startswith("find "):
            folder = next(arg for arg in shlex.split(command)[1:] if not arg.startswith("-"))
            inside = {p for p in self.dirs | set(self.files)
                      if posixpath.dirname(p) == folder and p != folder}
            return CommandResult(0, "".join(
                f"{'d' if p in self.dirs else 'f'}\t{len(self.files.get(p, b''))}\t"
                f"{posixpath.basename(p)}\n" for p in sorted(inside)))
        if command.startswith("rm -rf -- "):
            gone = shlex.split(command)[-1]
            self.dirs = {d for d in self.dirs if d != gone and not d.startswith(gone + "/")}
            self.files = {f: data for f, data in self.files.items() if not f.startswith(gone + "/")}
            return CommandResult(0, "")
        if command.startswith("sha256sum -- "):
            lines = []
            for path in shlex.split(command)[2:]:
                if path in self.files:  # coreutils escapes names with \, new lines or \r
                    digest = hashlib.sha256(self.files[path]).hexdigest()
                    escaped = path.replace("\\", "\\\\").replace("\n", "\\n") \
                        .replace("\r", "\\r")
                    lines.append(f"\\{digest}  {escaped}" if escaped != path
                                 else f"{digest}  {path}")
            return CommandResult(0, "\n".join(lines) + "\n")
        return CommandResult(0, "")

    def exists(self, path):
        return path in self.dirs or path in self.files

    def list_folder(self, path):
        if path not in self.dirs:
            raise FileNotFoundError(path)
        if any(posixpath.dirname(p) == path for p in self.not_text):
            raise UnicodeDecodeError("utf-8", b"\xe9", 0, 1, "invalid continuation byte")
        inside = {p for p in self.dirs | set(self.files)
                  if posixpath.dirname(p) == path and p != path}
        return (sorted(posixpath.basename(p) for p in inside if p in self.dirs),
                {posixpath.basename(p): len(self.files[p]) for p in inside if p in self.files})

    def _check_writable(self, folder, path):
        if folder in self.locked:
            raise PermissionError(13, "Permission denied", path)
        if self.read_only:
            raise OSError("Failure")

    def mkdir(self, path):
        if path in self.dirs:
            return
        parent = posixpath.dirname(path)
        if parent not in self.dirs:
            raise FileNotFoundError(path)
        self._check_writable(parent, path)
        self.dirs.add(path)

    def rename(self, old, new):
        self._check_writable(posixpath.dirname(old), old)

        def moved(path):
            return new + path[len(old):] if path == old or path.startswith(old + "/") else path
        self.dirs = {moved(p) for p in self.dirs}
        self.files = {moved(p): data for p, data in self.files.items()}

    def upload(self, fileobj, path, progress=None):
        if posixpath.dirname(path) not in self.dirs:
            raise FileNotFoundError(path)
        self._check_writable(posixpath.dirname(path), path)
        self.files[path] = fileobj.read()

    def remove(self, path):
        del self.files[path]

    def open(self, path):
        if path not in self.files:
            raise FileNotFoundError(path)
        return io.BytesIO(self.files[path])

    def download(self, remote_path, local_path, progress=None):
        if remote_path not in self.files:
            raise FileNotFoundError(remote_path)
        Path(local_path).write_bytes(self.files[remote_path])

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
