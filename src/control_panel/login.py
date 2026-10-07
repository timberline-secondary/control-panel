"""Logging in to a Pi: find the password, connect, and remember it if asked to.

Passwords are never stored in this repository. They come from (in order) the
config file, this run's memory, the OS keychain (Windows Credential Manager on
the lab computers), or by asking.
"""

from __future__ import annotations

import keyring

from control_panel import ui
from control_panel.config import PiSettings
from control_panel.ssh import AuthenticationFailed, ConnectionFailed, PiConnection

KEYRING_SERVICE = "hackerspace-control-panel"
MAX_TRIES = 3

_remembered_this_run: dict[str, str] = {}


def connect(settings: PiSettings) -> PiConnection | None:
    """Open a connection, asking for the password if needed. None if they cancel."""
    account = f"{settings.username}@{settings.host}"
    password = settings.password or _remembered_this_run.get(account) or _load(account)

    for _ in range(MAX_TRIES):
        typed = password is None
        if typed:
            password = ui.ask_password(f"Password for {account}:")
            if password is None:
                return None
        ui.info(f"Connecting to {settings.host}...")
        try:
            connection = PiConnection(settings.host, settings.username, password,
                                      port=settings.port)
        except AuthenticationFailed as e:
            ui.error(str(e))
            _remembered_this_run.pop(account, None)
            _forget(account)
            password = None
            continue

        _remembered_this_run[account] = password
        if typed and _keychain_available() and ui.confirm(
            "Remember this password on this computer?", default=True
        ):
            _save(account, password)
        return connection

    raise ConnectionFailed(f"Couldn't log in to {settings.host}.")


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
