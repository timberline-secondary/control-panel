"""A small wrapper around paramiko for running commands and copying files on a Pi."""

from __future__ import annotations

import posixpath
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

import paramiko


class ConnectionFailed(Exception):
    """Couldn't connect to the Pi. The message is written for the person using the app."""


class AuthenticationFailed(ConnectionFailed):
    """The Pi said the password was wrong."""


class HostKeyChanged(ConnectionFailed):
    """The Pi's identity isn't the one we saw before: it was re-imaged, or it's an impostor."""


def forget_host_key(known_hosts: Path, host: str, port: int = 22) -> None:
    """Remove what we remember about a Pi's identity, so its new one is accepted."""
    if not known_hosts.exists():
        return
    keys = paramiko.HostKeys(str(known_hosts))
    name = host if port == 22 else f"[{host}]:{port}"  # how paramiko names entries
    while True:
        try:
            del keys[name]
        except KeyError:
            break
    keys.save(str(known_hosts))


@dataclass(frozen=True)
class CommandResult:
    exit_status: int
    output: str  # stdout and stderr combined

    @property
    def ok(self) -> bool:
        return self.exit_status == 0


class PiConnection:
    """An open SSH connection. Use it as a context manager so it gets closed."""

    def __init__(self, host: str, username: str, password: str, port: int = 22,
                 timeout: float = 10, known_hosts: Path | None = None):
        """known_hosts: file where each Pi's identity (host key) is remembered. The first
        connection to a Pi trusts and saves its key; after that a different key raises
        HostKeyChanged before the password is sent."""
        self.host = host
        self._password = password
        self._sftp: paramiko.SFTPClient | None = None
        self._client = paramiko.SSHClient()
        if known_hosts is not None:
            known_hosts.parent.mkdir(parents=True, exist_ok=True)
            known_hosts.touch()
            self._client.load_host_keys(str(known_hosts))
        self._client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            self._client.connect(
                host, port=port, username=username, password=password,
                timeout=timeout, auth_timeout=30, banner_timeout=timeout,
                look_for_keys=False, allow_agent=False,
            )
        except paramiko.BadHostKeyException as e:
            raise HostKeyChanged(
                f"{host} isn't the same computer this one connected to before. That's expected "
                "if it was just re-installed. Otherwise something else on the network may be "
                "pretending to be it, to collect the password."
            ) from e
        except paramiko.AuthenticationException as e:
            # paramiko raises this for a rejected password, but also when the Pi is too slow
            # to answer ("Authentication timeout.") or hangs up ("...transport shut down...").
            message = str(e).lower()
            if "timeout" in message or "shut down" in message:
                raise ConnectionFailed(
                    f"{host} stopped answering while logging in. Try again in a minute."
                ) from e
            raise AuthenticationFailed(f"{username}@{host} didn't accept that password.") from e
        except socket.gaierror as e:
            raise ConnectionFailed(
                f"Couldn't find {host} on the network. "
                "Check that this computer is on the school network."
            ) from e
        except (OSError, paramiko.SSHException) as e:
            raise ConnectionFailed(
                f"Couldn't connect to {host}. Is it plugged in and turned on? ({e})"
            ) from e
        self._client.get_transport().set_keepalive(30)

    def __enter__(self) -> PiConnection:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        if self._sftp is not None:
            self._sftp.close()
        self._client.close()

    @property
    def sftp(self) -> paramiko.SFTPClient:
        if self._sftp is None:
            self._sftp = self._client.open_sftp()
        return self._sftp

    def run(self, command: str, *, sudo: bool = False, timeout: float = 60) -> CommandResult:
        """Run a shell command, wait for it to finish and return its output."""
        if sudo:
            command = f"sudo -S -p '' {command}"
        channel = self._client.get_transport().open_session(timeout=timeout)
        try:
            channel.settimeout(timeout)
            channel.set_combine_stderr(True)
            channel.exec_command(command)
            if sudo:
                channel.sendall(f"{self._password}\n".encode())
            channel.shutdown_write()
            output = channel.makefile("rb").read().decode("utf-8", "replace")
            return CommandResult(channel.recv_exit_status(), output)
        finally:
            channel.close()

    def start(self, command: str) -> paramiko.Channel:
        """Start a long-running command in a terminal and return the channel to talk to it.

        Closing the channel hangs up the terminal, which stops the command.
        """
        channel = self._client.get_transport().open_session()
        channel.get_pty(term="dumb", width=200)
        channel.set_combine_stderr(True)
        channel.exec_command(command)
        return channel

    def exists(self, path: str) -> bool:
        try:
            self.sftp.stat(path)
        except FileNotFoundError:
            return False
        return True

    def listdir(self, path: str) -> list[str]:
        return self.sftp.listdir(path)

    def upload(self, fileobj: BinaryIO, remote_path: str) -> None:
        """Upload under a temporary name, then rename, so nothing sees a half-written file."""
        directory, name = posixpath.split(remote_path)
        partial = posixpath.join(directory, f".{name}.part")
        try:
            self.sftp.putfo(fileobj, partial)
            self.sftp.posix_rename(partial, remote_path)
        except BaseException:
            try:
                self.sftp.remove(partial)
            except (OSError, paramiko.SSHException):
                pass  # e.g. the connection dropped; report the original error instead
            raise
