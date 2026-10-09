"""Logging in to a Pi: find the password, connect, and remember it if asked to.

This app doesn't keep passwords in its code. They come from (in order) the
config file, this run's memory, the OS keychain (Windows Credential Manager on
the lab computers), or by asking.
"""

from __future__ import annotations

import keyring

from control_panel import config, ui
from control_panel.config import PiSettings
from control_panel.ssh import (
    AuthenticationFailed,
    ConnectionFailed,
    HostKeyChanged,
    PiConnection,
    forget_host_key,
)

KEYRING_SERVICE = "hackerspace-control-panel"
MAX_TYPED_TRIES = 3
SHARED_HOST = "any-hackerspace-pi"  # keychain entry for a password shared by the Pis

_remembered_this_run: dict[str, str] = {}
_rejected_this_run: set[tuple[str, str]] = set()  # (account, password) the Pi said no to


def connect(settings: PiSettings) -> PiConnection | None:
    """Open a connection, asking for the password if needed. None if they cancel."""
    account = f"{settings.username}@{settings.host}"
    # The lab's Pis share a login, so a password that works on one is worth trying on the
    # others before asking.
    shared_account = f"{settings.username}@{SHARED_HOST}"
    other_pis = [pw for acct, pw in _remembered_this_run.items()
                 if acct.startswith(f"{settings.username}@") and acct != account]
    saved = [
        (settings.password, "config"),
        (_remembered_this_run.get(account), "memory"),
        (_load(account), "keychain"),
        *[(pw, "another Pi") for pw in other_pis],
        (_load(shared_account), "another Pi"),
    ]
    typed_tries = 0

    while True:
        # Try each saved password once per run, then ask.
        password, source = next(
            ((pw, src) for pw, src in saved if pw and (account, pw) not in _rejected_this_run),
            (None, "typed"),
        )
        if password is None:
            if typed_tries == MAX_TYPED_TRIES:
                raise ConnectionFailed(f"Couldn't log in to {settings.host}.")
            password = ui.ask_password(f"Password for {account}:")
            if password is None:
                return None
            typed_tries += 1

        try:
            connection = _open(settings, password)
        except AuthenticationFailed as e:
            _rejected_this_run.add((account, password))
            if source == "another Pi":
                continue  # only a guess, so no need to mention it
            ui.error(f"{e} (It's the one in the config file.)" if source == "config" else str(e))
            if source == "keychain":
                _forget(account)
            continue
        if connection is None:
            return None

        _remembered_this_run[account] = password
        if source == "typed" and _keychain_available() and ui.confirm(
            "Remember this password? (Only on your own Windows login, not a shared one.)",
            default=False,
        ):
            _save(account, password)
            _save(shared_account, password)  # for the other Pis, which usually share it
        return connection


def _open(settings: PiSettings, password: str) -> PiConnection | None:
    """Connect, asking whether to trust the Pi if its identity has changed."""
    ui.info(f"Connecting to {settings.host}...")
    known_hosts = config.known_hosts_path()
    try:
        return PiConnection(settings.host, settings.username, password, port=settings.port,
                            known_hosts=known_hosts)
    except HostKeyChanged as e:
        ui.warning(str(e))
        if not ui.confirm(f"Was {settings.host} just re-installed? Trust it from now on?",
                          default=False):
            return None
    forget_host_key(known_hosts, settings.host, settings.port)
    ui.info(f"Connecting to {settings.host}...")
    return PiConnection(settings.host, settings.username, password, port=settings.port,
                        known_hosts=known_hosts)


# The keychain is a convenience: if there isn't one (e.g. a headless Linux box),
# we just ask every time. Backends raise all sorts of errors, hence the broad excepts.

def _keychain_available() -> bool:
    try:
        return keyring.get_keyring().priority > 0
    except Exception:
        return False


def _load(account: str) -> str | None:
    try:
        return keyring.get_password(KEYRING_SERVICE, account)
    except Exception:
        return None


def _save(account: str, password: str) -> None:
    try:
        keyring.set_password(KEYRING_SERVICE, account, password)
    except Exception as e:
        ui.warning(f"Couldn't save the password ({e}). You'll be asked again next time.")


def _forget(account: str) -> None:
    try:
        keyring.delete_password(KEYRING_SERVICE, account)
    except Exception:
        pass
