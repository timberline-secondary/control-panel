"""Entrance themes: the Pi by the door that plays your theme when you type your
code on its keypad.

The player is themes.py from https://github.com/timberline-secondary/themes,
running on pi-themes. Everything here works over SSH, so nothing on the Pi has
to change.
"""

from __future__ import annotations

import codecs
import http.client
import io
import posixpath
import re
import shlex
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from pathlib import Path

import paramiko

from control_panel import __version__, login, ui
from control_panel.config import Config, ThemesSettings
from control_panel.ssh import CommandResult, ConnectionFailed, PiConnection

TITLE = "Entrance Theme Machine"
DOCS = "https://github.com/timberline-secondary/themes"

MAX_MP3_BYTES = 50_000_000
PLAYER_STARTUP_SECONDS = 30  # importing pygame on a Pi is slow
PLAYER_READY = "Please enter a code:"  # themes.py prints this when it's waiting for a code
_PLAYER_NOISE = (PLAYER_READY, "pygame ", "Hello from the pygame community")
_CODE_PATTERN = re.compile(r"[0-9A-Za-z_-]+")
REMOVED = "removed"  # the folder next to the themes that removed ones are kept in


class ThemeError(Exception):
    """Something the person can fix. The message says what."""


# --- Helpers (no prompts, easy to test) -------------------------------------------------

def looks_like_mp3(data: bytes) -> bool:
    """True if data starts the way an MP3 does: an ID3 tag or an MPEG audio frame header."""
    if data[:3] == b"ID3":
        return True
    if len(data) < 2 or data[0] != 0xFF or data[1] & 0xE0 != 0xE0:
        return False
    version = (data[1] >> 3) & 0b11
    layer = (data[1] >> 1) & 0b11
    return version != 0b01 and layer != 0b00  # reserved version; layer 0 is AAC, not MP3


def suggest_code(filename: str) -> str | None:
    """'0027.mp3' -> '0027'. Files named after their code are the usual case."""
    stem = posixpath.splitext(filename)[0]
    return stem if _is_number(stem) else None


def _is_number(text: str) -> bool:
    return text.isascii() and text.isdigit()  # isdigit() alone accepts things like '²'


def code_problem(code: str) -> str | None:
    """Why a code can't be used, or None if it's fine."""
    if not code:
        return "Type a code."
    if code.startswith("*"):
        return "Codes starting with * are reserved for the player's admin codes."
    if code.lower() == "exit":
        return "'exit' is reserved (typing it stops the player)."
    if not _CODE_PATTERN.fullmatch(code):
        return "Use only numbers (or letters, - and _) in a code."
    return None


def clean_source(text: str) -> str:
    """Tidy a pasted link or dragged-in file path (Windows wraps paths in quotes)."""
    text = text.strip()
    if len(text) > 3 and text.startswith("& '") and text.endswith("'"):
        return text[3:-1].replace("''", "'")  # how VS Code's PowerShell terminal drops paths
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    return text


def read_mp3(source: str) -> tuple[str, bytes]:
    """Get an mp3 from a link or a file on this computer. Returns (file name, contents)."""
    if source.startswith(("\\\\", "//")) or source.lower().startswith("file:"):
        # Opening \\server\share paths makes Windows log in to that server as you.
        raise ThemeError("Use a web link, or an mp3 file on this computer.")
    if source.lower().startswith(("http://", "https://")):
        url_path = urllib.parse.unquote(urllib.parse.urlparse(source).path)
        name = posixpath.basename(url_path) or "the download"
        data, content_type = _download(source)
        not_mp3 = (
            f"That link doesn't go straight to an mp3 file (it gave us '{content_type}'). "
            "Make sure the link is to the .mp3 file itself, not a web page about it."
        )
    else:
        path = Path(source).expanduser()
        if not path.is_file():
            raise ThemeError(f"Couldn't find the file {source}")
        try:
            if path.stat().st_size > MAX_MP3_BYTES:
                raise ThemeError("That file is too big for a theme (over 50 MB).")
            name, data = path.name, path.read_bytes()
        except OSError as e:
            raise ThemeError(f"Couldn't read {source}: {e.strerror or e}") from e
        not_mp3 = f"{name} isn't an mp3 file. Only mp3s can be themes."
    if not looks_like_mp3(data):
        raise ThemeError(not_mp3)
    return name, data


def _download(url: str) -> tuple[bytes, str]:
    request = urllib.request.Request(
        url, headers={"User-Agent": f"hackerspace-control-panel/{__version__}"}
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = response.read(MAX_MP3_BYTES + 1)
            content_type = response.headers.get_content_type()
            bytes_missing = response.length  # still owed by Content-Length, if it gave one
    except urllib.error.HTTPError as e:
        raise ThemeError(f"The link didn't work (error {e.code}: {e.reason}).") from e
    except http.client.HTTPException as e:
        raise ThemeError("Couldn't download that link (the reply was broken or cut off).") from e
    except (OSError, ValueError) as e:
        raise ThemeError(f"Couldn't download that link: {getattr(e, 'reason', e)}") from e
    if len(data) > MAX_MP3_BYTES:
        raise ThemeError("That file is too big for a theme (over 50 MB).")
    if bytes_missing:
        raise ThemeError("The download was cut off part way. Try again.")
    return data, content_type


def tidy_player_line(line: str) -> str | None:
    """Turn a line printed by themes.py into something worth showing, or None."""
    line = line.strip()
    while line.startswith(">"):  # its input prompt, which output gets appended to
        line = line[1:].lstrip()
    if not line or line.startswith(_PLAYER_NOISE) or "underrun occurred" in line:
        return None  # an underrun is a hiccup in the sound that it recovers from
    if found := re.fullmatch(r"Found .*/(.+)\.mp3", line):
        return f"Playing {found[1]}."
    if missing := re.fullmatch(r"File '(.+)\.mp3' not found\.", line):
        return f"There's no theme with code {missing[1]}."
    return line


def parse_volume(amixer_output: str) -> int | None:
    match = re.search(r"\[(\d+)%\]", amixer_output)
    return int(match.group(1)) if match else None


def sort_codes(codes: Iterable[str]) -> list[str]:
    """Dictionary order, digit by digit, so leading zeros count: 05111955, 15111955, 4261992."""
    return sorted(codes, key=lambda c: (c.casefold(), c))


def search_codes(codes: list[str], text: str) -> list[str]:
    """The codes containing text, ignoring case."""
    text = text.casefold()
    return [code for code in codes if text in code.casefold()]


def columns(items: list[str], width: int = 78) -> str:
    cell = max(map(len, items)) + 2
    per_row = max(1, width // cell)
    rows = [items[i:i + per_row] for i in range(0, len(items), per_row)]
    return "\n".join("".join(item.ljust(cell) for item in row).rstrip() for row in rows)


# --- Talking to pi-themes ------------------------------------------------------------------

class ThemesPi:
    """What we can do on pi-themes, given an open connection."""

    def __init__(self, connection: PiConnection, settings: ThemesSettings):
        self.connection = connection
        self.settings = settings

    def theme_path(self, code: str) -> str:
        return posixpath.join(self.settings.songs_dir, f"{code}.mp3")

    def list_codes(self) -> list[str]:
        try:
            names = self.connection.listdir(self.settings.songs_dir)
        except FileNotFoundError as e:
            raise ThemeError(
                f"{self.settings.songs_dir} doesn't exist on {self.settings.host}. "
                "Is the USB drive with the themes plugged in?"
            ) from e
        return sort_codes(n[:-4] for n in names if n.endswith(".mp3") and not n.startswith("."))

    def has_theme(self, code: str) -> bool:
        return self.connection.exists(self.theme_path(code))

    def add_theme(self, code: str, data: bytes) -> None:
        self.connection.upload(io.BytesIO(data), self.theme_path(code))

    def removed_folder(self) -> str:
        return posixpath.join(self.settings.songs_dir, REMOVED)

    def remove_theme(self, code: str, keep_backup: bool) -> str | None:
        """Take a theme off, so its code doesn't play anything. Returns where its backup is,
        if one was kept (the player only plays themes in songs_dir itself)."""
        path = self.theme_path(code)
        if not keep_backup:
            self.connection.remove(path)
            return None
        folder = self.removed_folder()
        self.connection.mkdir(folder)
        backup, copy = posixpath.join(folder, f"{code}.mp3"), 1
        while self.connection.exists(backup):  # it was removed before
            copy += 1
            backup = posixpath.join(folder, f"{code} ({copy}).mp3")
        self.connection.rename(path, backup)
        return backup

    def speak(self, text: str) -> CommandResult:
        # "--" so text starting with "-" is spoken, not read as espeak options.
        # espeak only finishes once it's done talking, hence the long timeout.
        return self.connection.run(f"espeak -a 200 -- {shlex.quote(text)} 2>/dev/null",
                                   timeout=600)

    def volume(self) -> int | None:
        result = self.connection.run(f"amixer sget {self._mixer}")
        return parse_volume(result.output) if result.ok else None

    def set_volume(self, percent: int) -> CommandResult:
        return self.connection.run(f"amixer sset {self._mixer} {percent}%")

    def reboot(self) -> CommandResult:
        return self.connection.run("shutdown -r now", sudo=True)

    def start_player(self) -> paramiko.Channel:
        """Start a second copy of themes.py, as the old control panel did.

        'silent' skips the startup sound. Echo is turned off so we only get its output back.
        """
        s = self.settings
        return self.connection.start(
            f"cd {shlex.quote(s.player_dir)} && stty -echo && exec {s.python} themes.py silent"
        )

    @property
    def _mixer(self) -> str:
        return shlex.quote(f"{self.settings.mixer_control},0")


class PlayerSession:
    """Sends codes to a running themes.py and passes what it prints to `show`."""

    def __init__(self, channel: paramiko.Channel, show: Callable[[str], None]):
        self.channel = channel
        self.show = show
        self.ready = threading.Event()  # also set if it ends before getting ready
        self.ended = threading.Event()
        self._stopping = False
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def send(self, code: str) -> None:
        self.channel.sendall(f"{code}\n".encode())

    def stop(self) -> None:
        """Hang up, which also stops anything still playing."""
        self._stopping = True
        self.channel.close()
        self._reader.join(timeout=5)

    def _read(self) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        pending = ""
        try:
            while data := self.channel.recv(4096):
                *lines, pending = (pending + decoder.decode(data)).split("\n")
                for line in lines:
                    if PLAYER_READY in line:
                        self.ready.set()
                    if tidy := tidy_player_line(line):
                        self.show(tidy)
        except (OSError, EOFError):
            pass
        finally:
            if tidy := tidy_player_line(pending):
                self.show(tidy)
            was_ready = self.ready.is_set()
            self.ended.set()
            self.ready.set()
            if was_ready and not self._stopping:  # it stopped while they were typing codes
                self.show("(the theme player has stopped - press Enter)")


# --- Menu actions --------------------------------------------------------------------------

def play(pi: ThemesPi) -> None:
    ui.info("Starting the theme player on pi-themes...")
    session = PlayerSession(
        pi.start_player(), show=lambda line: ui.info(f"  pi-themes: {line}")
    )
    try:
        session.ready.wait(PLAYER_STARTUP_SECONDS)
        if session.ended.is_set():
            ui.error("The theme player on pi-themes didn't start (see above).")
            return
        ui.success("Ready! Type a theme code and press Enter to play it on the entrance speaker.")
        ui.info("This screen runs its own copy of the player, so its spam-block and admin (*) "
                "codes don't affect the keypad.\n"
                "Leaving this screen stops anything that's still playing.")
        while not session.ended.is_set():
            code = ui.ask("Type a theme code to play it")
            if code is None or code.lower() == "exit":
                break
            if code and not session.ended.is_set():
                session.send(code)
    finally:
        session.stop()


def speak(pi: ThemesPi) -> None:
    ui.info("Type something and pi-themes will say it out loud.")
    while (text := ui.ask("Type something to say")) is not None:
        if text:
            result = pi.speak(text)
            if not result.ok:
                ui.error(f"That didn't work (exit status {result.exit_status}). "
                         "Is espeak installed on the Pi?")


def add_theme(pi: ThemesPi) -> None:
    ui.info("Add a theme from a link to an mp3, or from an mp3 file on this computer.")
    while True:
        source = ui.ask("Paste a link, or drag an mp3 file into this window")
        if source is None:
            return
        if not source:
            continue
        try:
            name, data = read_mp3(clean_source(source))
        except ThemeError as e:
            ui.error(str(e))
            continue
        ui.success(f"Got {name} ({len(data) / 1_000_000:.1f} MB) - it's an mp3.")

        suggestion = suggest_code(name)
        while True:
            code = _ask_code(suggestion)
            if code is None:
                return
            if not pi.has_theme(code):
                break
            choice = ui.choose([
                ui.choice("Use a different code", "different"),
                ui.choice("Replace it", "replace"),
            ], f"There's already a theme with code {code}.")
            if choice is None:
                return
            if choice == "replace":
                break
            suggestion = None

        try:
            pi.add_theme(code, data)
        except OSError as e:
            ui.error(f"Couldn't save it on pi-themes: {e}")
            return
        if _is_number(code):
            ui.success(f"Added! Type {code} on the entrance keypad to play it.")
        else:
            ui.success(f"Added! Play it by typing {code} in 'Play a theme'.")
        if not ui.confirm("Add another theme?", default=True):
            return


def remove_theme(pi: ThemesPi) -> None:
    while True:
        code = ui.ask("Type the code of the theme to remove")
        if code is None:
            return
        if not code:
            continue
        if problem := code_problem(code):
            ui.error(problem)
        elif pi.has_theme(code):
            break
        else:
            ui.error(f"There's no theme with code {code}. 'List and search themes' shows them.")
    how = ui.choose([
        ui.choice(f"Remove it, and keep a backup on pi-themes (in {pi.removed_folder()})",
                  "backup"),
        ui.choice("Delete it for good", "delete"),
    ], f"Theme {code} won't play any more.")
    if how is None:
        return
    try:
        backup = pi.remove_theme(code, keep_backup=how == "backup")
    except OSError as e:
        ui.error(f"Couldn't remove it from pi-themes: {e}")
        return
    if backup:
        ui.success(f"Removed theme {code}. Its backup is {backup}.")
    else:
        ui.success(f"Deleted theme {code}.")


def _ask_code(suggestion: str | None) -> str | None:
    while True:
        code = ui.ask("What code should play it?", default=suggestion or "")
        if code is None:
            return None
        code = code or suggestion or ""
        if problem := code_problem(code):
            ui.error(problem)
            continue
        if _is_number(code) or ui.confirm(
            f"'{code}' can't be typed on the entrance keypad (it only has numbers). "
            "Use it anyway?", default=False
        ):
            return code


def list_themes(pi: ThemesPi) -> None:
    codes = pi.list_codes()
    if not codes:
        ui.warning(f"There are no themes in {pi.settings.songs_dir} yet.")
        return
    ui.success(f"{len(codes)} themes on pi-themes:")
    ui.info(columns(codes))
    while text := ui.ask("Type part of a code to search for it (Enter = done)"):
        matches = search_codes(codes, text)
        if matches:
            ui.success(f"{len(matches)} of {len(codes)} codes contain '{text}':")
            ui.info(columns(matches))
        else:
            ui.warning(f"No codes contain '{text}'.")


def toggle_mute(pi: ThemesPi) -> None:
    level = pi.volume()
    if level is None:
        mute = ui.confirm("Couldn't read the volume. Mute it? (n = unmute)", default=True)
        if mute is None:
            return
    else:
        mute = level > 0
    result = pi.set_volume(0 if mute else 100)
    if result.ok:
        ui.success("Muted the entrance speaker." if mute else "Unmuted the entrance speaker.")
    else:
        ui.error(f"That didn't work: {result.output.strip()}")


def reboot(pi: ThemesPi) -> None:
    if not ui.confirm("Reboot pi-themes? It takes a minute or two to come back.", default=False):
        return
    try:
        result = pi.reboot()
    except (OSError, paramiko.SSHException):
        result = CommandResult(-1, "")  # it hung up on us, because it's going down
    # -1 means the connection closed before the Pi reported back, which is expected here.
    if result.exit_status in (0, -1):
        ui.success("Rebooting... you'll hear the startup sound when it's back.")
    else:
        ui.error(f"Couldn't reboot: {result.output.strip() or result.exit_status}")


def docs() -> None:
    ui.info(f"pi-themes runs the theme player from {DOCS}")
    ui.info("Its README says how it's set up, and how the keypad codes work.")


ACTIONS = [
    ("Add a new theme", add_theme),
    ("Remove a theme", remove_theme),
    ("Play a theme", play),
    ("List and search themes", list_themes),
    ("Toggle mute", toggle_mute),
    ("Say something (text to speech)", speak),
    ("Reboot pi-themes", reboot),
    ("pi-themes documentation", docs),
]


def run(config: Config) -> None:
    ui.run_menu(TITLE, ACTIONS, lambda action: _do(action, config.themes))


def _do(action, settings: ThemesSettings) -> None:
    if action is docs:
        docs()
        return
    try:
        connection = login.connect(settings)
        if connection is None:
            return
        with connection:
            action(ThemesPi(connection, settings))
    except (ConnectionFailed, ThemeError) as e:
        ui.error(str(e))
    except TimeoutError:
        ui.error(f"{settings.host} stopped answering. Try again, or reboot it if it keeps "
                 "happening.")
    except (OSError, paramiko.SSHException) as e:
        ui.error(f"Lost the connection to {settings.host}: {e}")
    except KeyboardInterrupt:
        ui.warning("Cancelled.")
