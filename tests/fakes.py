"""Stand-ins for the Pis, shared by the tests."""

import posixpath
from pathlib import Path

from control_panel.ssh import CommandResult


class FakeFilesConnection:
    """Pretends to be pi-files: files in memory, with its drive mounted at /mnt/ssd."""

    def __init__(self, mounted=True, free_kb=100_000_000, root_owned=False):
        self.host = "pi-files.hackerspace.tbl"
        self.files = {}  # path: contents
        self.dirs = {"/", "/mnt", "/mnt/ssd"}
        self.mounted = mounted
        self.free_kb = free_kb
        self.root_owned = root_owned  # the drive's top folder belongs to root
        self.commands = []

    def run(self, command, *, sudo=False, timeout=60):
        self.commands.append((command, sudo))
        if command.startswith("df "):
            mount = "/mnt/ssd" if self.mounted else "/"
            return CommandResult(0, "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
                                    f"/dev/sda1 1000000 10 {self.free_kb} 1% {mount}\n")
        if command.startswith("sh -c 'mkdir -p") and sudo:
            self.dirs.add(command.split()[4])
        return CommandResult(0, "")

    def exists(self, path):
        return path in self.dirs or path in self.files

    def listdir(self, path):
        if path not in self.dirs:
            raise FileNotFoundError(path)
        return sorted({p[len(path):].lstrip("/").split("/")[0]
                       for p in self.dirs | set(self.files) if p.startswith(path + "/")})

    def mkdir(self, path):
        if path in self.dirs:
            return
        parent = posixpath.dirname(path)
        if parent not in self.dirs:
            raise FileNotFoundError(path)
        if self.root_owned and parent == "/mnt/ssd":
            raise PermissionError(13, "Permission denied")
        self.dirs.add(path)

    def rename(self, old, new):
        def moved(path):
            return new + path[len(old):] if path == old or path.startswith(old + "/") else path
        self.dirs = {moved(p) for p in self.dirs}
        self.files = {moved(p): data for p, data in self.files.items()}

    def upload(self, fileobj, path, progress=None):
        if posixpath.dirname(path) not in self.dirs:
            raise FileNotFoundError(path)
        self.files[path] = fileobj.read()

    def read_bytes(self, path):
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    def download(self, remote_path, local_path, progress=None):
        Path(local_path).write_bytes(self.read_bytes(remote_path))

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
