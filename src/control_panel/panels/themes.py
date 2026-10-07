"""Entrance themes: the Pi by the door that plays your theme when you type your
code on its keypad.

The player is themes.py from https://github.com/timberline-secondary/themes,
running on pi-themes. Everything here works over SSH, so nothing on the Pi has
to change.
"""

from __future__ import annotations

import codecs
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

TITLE = "Entrance themes (pi-themes)"

MAX_MP3_BYTES = 50_000_000
PLAYER_STARTUP_SECONDS = 30  # importing pygame on a Pi is slow
PLAYER_READY = "Please enter a code:"  # themes.py prints this when it's waiting for a code
_PLAYER_NOISE = (PLAYER_READY, "pygame ", "Hello from the pygame community")
_CODE_PATTERN = re.compile(r"[0-9A-Za-z_-]+")


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
    return stem if stem.isdigit() else None


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
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    return text


def read_mp3(source: str) -> tuple[str, bytes]:
    """Get an mp3 from a link or a file on this computer. Returns (file name, contents)."""
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
        if path.stat().st_size > MAX_MP3_BYTES:
            raise ThemeError("That file is too big for a theme (over 50 MB).")
        name, data = path.name, path.read_bytes()
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
    except urllib.error.HTTPError as e:
        raise ThemeError(f"The link didn't work (error {e.code}: {e.reason}).") from e
    except (OSError, ValueError) as e:
        raise ThemeError(f"Couldn't download that link: {getattr(e, 'reason', e)}") from e
    if len(data) > MAX_MP3_BYTES:
        raise ThemeError("That file is too big for a theme (over 50 MB).")
    return data, content_type


def tidy_player_line(line: str) -> str | None:
    """Turn a line printed by themes.py into something worth showing, or None."""
    line = line.strip()
    while line.startswith(">"):  # its input prompt, which output gets appended to
        line = line[1:].lstrip()
    if not line or line.startswith(_PLAYER_NOISE):
        return None
    return line


def parse_volume(amixer_output: str) -> int | None:
    match = re.search(r"\[(\d+)%\]", amixer_output)
    return int(match.group(1)) if match else None


def sort_codes(codes: Iterable[str]) -> list[str]:
    """Numbers in number order first, then anything else alphabetically."""
    return sorted(codes, key=lambda c: (not c.isdigit(), int(c) if c.isdigit() else 0, c))


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

    def speak(self, text: str) -> CommandResult:
        return self.connection.run(f"espeak -a 200 {shlex.quote(text)} 2>/dev/null")

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
        pi.start_player(), show=lambda line: print(f"  pi-themes: {line}", flush=True)
    )
    try:
        session.ready.wait(PLAYER_STARTUP_SECONDS)
        if session.ended.is_set():
            ui.error("The theme player on pi-themes didn't start (see above).")
            return
        ui.success("Ready! Type a theme code and press Enter to play it on the entrance speaker.")
        ui.info("Leaving this screen stops anything that's still playing.")
        while not session.ended.is_set():
            code = ui.ask(f"Theme code {ui.QUIT_HINT}", patch_stdout=True)
            if code is None or code.lower() == "exit":
                break
            if code and not session.ended.is_set():
                session.send(code)
    finally:
        session.stop()


def speak(pi: ThemesPi) -> None:
    ui.info("Type something and pi-themes will say it out loud.")
    while (text := ui.ask(f"Say {ui.QUIT_HINT}")) is not None:
        if text:
            result = pi.speak(text)
            if not result.ok:
                ui.error(f"That didn't work (exit status {result.exit_status}). "
                         "Is espeak installed on the Pi?")


def add_theme(pi: ThemesPi) -> None:
    ui.info("Add a theme from a link to an mp3, or from an mp3 file on this computer.")
    while True:
        source = ui.ask(f"Paste a link, or drag an mp3 file into this window {ui.QUIT_HINT}")
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
            if not pi.has_theme(code) or ui.confirm(
                f"There's already a theme with code {code}. Replace it?", default=False
            ):
                break

        try:
            pi.add_theme(code, data)
        except OSError as e:
            ui.error(f"Couldn't save it on pi-themes: {e}")
            return
        ui.success(f"Added! Type {code} on the entrance keypad to play it.")
        if not ui.confirm("Add another theme?", default=True):
            return


def _ask_code(suggestion: str | None) -> str | None:
    while True:
        code = ui.ask("What code should play it?", default=suggestion or "")
        if code is None:
            return None
        if problem := code_problem(code):
            ui.error(problem)
            continue
        if code.isdigit() or ui.confirm(
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


def mute_or_unmute(pi: ThemesPi) -> None:
    level = pi.volume()
    ui.info("Couldn't read the current volume." if level is None
            else f"The entrance speaker is at {level}% volume.")
    percent = ui.choose("Mute or unmute it?", [
        ui.choice("Mute", 0), ui.choice("Unmute", 100), ui.choice("Back", ui.BACK),
    ])
    if percent is None:
        return
    result = pi.set_volume(percent)
    if result.ok:
        ui.success("Muted." if percent == 0 else "Unmuted.")
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


ACTIONS = [
    ("Play a theme", play),
    ("Say something (text to speech)", speak),
    ("Add a new theme", add_theme),
    ("List themes", list_themes),
    ("Mute / unmute the speaker", mute_or_unmute),
    ("Reboot pi-themes", reboot),
]


def run(config: Config) -> None:
    settings = config.themes
    ui.heading(TITLE)
    while True:
        action = ui.choose("What would you like to do?",
                           [ui.choice(title, action) for title, action in ACTIONS]
                           + [ui.choice("Back", ui.BACK)])
        if action is None:
            return
        try:
            connection = login.connect(settings)
            if connection is None:
                continue
            with connection:
                action(ThemesPi(connection, settings))
        except (ConnectionFailed, ThemeError) as e:
            ui.error(str(e))
        except (OSError, paramiko.SSHException) as e:
            ui.error(f"Lost the connection to {settings.host}: {e}")
        except KeyboardInterrupt:
            ui.warning("Cancelled.")
        print()
