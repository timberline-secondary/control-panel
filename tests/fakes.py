"""Stand-ins for the Pis, shared by the tests."""

import hashlib
import posixpath
import shlex
from pathlib import Path

from control_panel.ssh import CommandResult


class FakeFilesConnection:
    """Pretends to be pi-files: files in memory, with its drive mounted at /mnt/ssd.

    locked: folders the app isn't allowed to change (e.g. they belong to root).
    """

    def __init__(self, mounted=True, free_kb=100_000_000, locked=(), read_only=False):
        self.host = "pi-files.hackerspace.tbl"
        self.files = {}  # path: contents
        self.dirs = {"/", "/mnt", "/mnt/ssd"}
        self.mounted = mounted
        self.free_kb = free_kb
        self.locked = set(locked)
        self.read_only = read_only
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
        if command.startswith("sha256sum -- "):
            lines = [f"{hashlib.sha256(self.files[path]).hexdigest()}  {path}"
                     for path in shlex.split(command)[2:] if path in self.files]
            return CommandResult(0, "\n".join(lines) + "\n")
        return CommandResult(0, "")

    def exists(self, path):
        return path in self.dirs or path in self.files

    def list_folder(self, path):
        if path not in self.dirs:
            raise FileNotFoundError(path)
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
