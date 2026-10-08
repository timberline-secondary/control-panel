import queue
import socket
import threading
from pathlib import Path

import pytest

from control_panel.config import ThemesSettings
from control_panel.panels import themes
from control_panel.panels.themes import PlayerSession, ThemeError, ThemesPi
from control_panel.ssh import CommandResult

ID3_MP3 = b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\x00" * 100
FRAME_MP3 = b"\xff\xfb\x90\x64" + b"\x00" * 100  # MPEG-1 layer III frame header


@pytest.mark.parametrize("data, expected", [
    (ID3_MP3, True),
    (FRAME_MP3, True),
    (b"\xff\xf1\x50\x80", False),  # AAC (ADTS), not mp3
    (b"<!DOCTYPE html><html>", False),
    (b"RIFF....WAVE", False),
    (b"", False),
])
def test_looks_like_mp3(data, expected):
    assert themes.looks_like_mp3(data) is expected


@pytest.mark.parametrize("name, expected", [
    ("0027.mp3", "0027"),  # keeps leading zeros
    ("1234", "1234"),
    ("my song.mp3", None),
    ("12a.mp3", None),
    ("\u00b2.mp3", None),  # '²'.isdigit() is True, but it's not a code
])
def test_suggest_code(name, expected):
    assert themes.suggest_code(name) == expected


@pytest.mark.parametrize("code, ok", [
    ("0027", True), ("abc", True), ("a-b_c", True),
    ("", False), ("*0000", False), ("exit", False), ("EXIT", False),
    ("12 34", False), ("../etc", False), ("a/b", False),
])
def test_code_problem(code, ok):
    assert (themes.code_problem(code) is None) is ok


@pytest.mark.parametrize("text, expected", [
    ('  "C:\\Users\\me\\My Music\\theme.mp3" ', "C:\\Users\\me\\My Music\\theme.mp3"),
    ("'/home/me/theme.mp3'", "/home/me/theme.mp3"),
    ("& 'C:\\Users\\me\\My Music\\theme.mp3'", "C:\\Users\\me\\My Music\\theme.mp3"),  # VS Code
    ("& 'C:\\Music\\Rock ''n'' Roll.mp3'", "C:\\Music\\Rock 'n' Roll.mp3"),
    ("https://example.com/a.mp3", "https://example.com/a.mp3"),
    ('"', '"'),
])
def test_clean_source(text, expected):
    assert themes.clean_source(text) == expected


def test_read_mp3_from_file(tmp_path):
    song = tmp_path / "0042.mp3"
    song.write_bytes(ID3_MP3)
    assert themes.read_mp3(str(song)) == ("0042.mp3", ID3_MP3)


def test_read_mp3_rejects_other_files(tmp_path):
    page = tmp_path / "theme.mp3"
    page.write_bytes(b"<html></html>")
    with pytest.raises(ThemeError, match="isn't an mp3"):
        themes.read_mp3(str(page))
    with pytest.raises(ThemeError, match="Couldn't find"):
        themes.read_mp3(str(tmp_path / "missing.mp3"))


@pytest.mark.parametrize("source", [
    "\\\\10.0.0.9\\share\\0042.mp3", "//10.0.0.9/share/0042.mp3", "file:///C:/music/0042.mp3",
])
def test_read_mp3_refuses_network_shares(source):
    with pytest.raises(ThemeError, match="web link"):
        themes.read_mp3(source)


def test_read_mp3_unreadable_file(tmp_path, monkeypatch):
    song = tmp_path / "0042.mp3"
    song.write_bytes(ID3_MP3)
    monkeypatch.setattr(Path, "read_bytes", lambda self: (_ for _ in ()).throw(
        PermissionError(13, "Permission denied")))
    with pytest.raises(ThemeError, match="Couldn't read .*Permission denied"):
        themes.read_mp3(str(song))


@pytest.fixture
def web_server():
    """A one-shot web server that sends whatever raw bytes the test gives it."""
    servers = []

    def serve(reply: bytes) -> str:
        sock = socket.create_server(("127.0.0.1", 0))
        servers.append(sock)

        def answer():
            client, _ = sock.accept()
            with client:
                client.recv(65536)
                client.sendall(reply)
                client.shutdown(socket.SHUT_WR)

        threading.Thread(target=answer, daemon=True).start()
        return f"http://127.0.0.1:{sock.getsockname()[1]}/0042.mp3"

    yield serve
    for sock in servers:
        sock.close()


def test_download_ok(web_server):
    url = web_server(b"HTTP/1.1 200 OK\r\nContent-Type: audio/mpeg\r\nContent-Length: %d\r\n"
                     b"Connection: close\r\n\r\n%s" % (len(FRAME_MP3), FRAME_MP3))
    assert themes.read_mp3(url) == ("0042.mp3", FRAME_MP3)


@pytest.mark.parametrize("reply, message", [
    (b"SSH-2.0-OpenSSH_9.6\r\n", "broken or cut off"),  # not a web server at all
    (b"HTTP/1.1 200 OK\r\nContent-Length: 5000\r\nConnection: close\r\n\r\n" + FRAME_MP3,
     "cut off part way"),
    (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n400\r\n" + FRAME_MP3,
     "broken or cut off"),
    (b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n", "error 404"),
])
def test_download_problems_are_explained(web_server, reply, message):
    with pytest.raises(ThemeError, match=message):
        themes.read_mp3(web_server(reply))


def test_read_mp3_from_link(monkeypatch):
    monkeypatch.setattr(themes, "_download", lambda url: (FRAME_MP3, "audio/mpeg"))
    assert themes.read_mp3("https://example.com/songs/00%2042.mp3") == ("00 42.mp3", FRAME_MP3)


def test_read_mp3_link_to_web_page(monkeypatch):
    monkeypatch.setattr(themes, "_download", lambda url: (b"<html>", "text/html"))
    with pytest.raises(ThemeError, match="text/html"):
        themes.read_mp3("https://example.com/watch?v=123")


@pytest.mark.parametrize("line, expected", [
    ("Please enter a code:\r", None),
    ("> ", None),
    ("> Found /mnt/usb0/0027.mp3\r", "Found /mnt/usb0/0027.mp3"),
    ("File '9.mp3' not found.", "File '9.mp3' not found."),
    ("pygame 2.1.0 (SDL 2.0.16, Python 3.9.2)", None),
    ("Hello from the pygame community. https://www.pygame.org/contribute.html", None),
])
def test_tidy_player_line(line, expected):
    assert themes.tidy_player_line(line) == expected


def test_parse_volume():
    output = "Simple mixer control 'Headphone',0\n  Mono: Playback -2000 [77%] [-20.00dB] [on]"
    assert themes.parse_volume(output) == 77
    assert themes.parse_volume("nothing here") is None


def test_sort_codes_and_columns():
    codes = themes.sort_codes(["100", "abc", "0027", "9"])
    assert codes == ["9", "0027", "100", "abc"]
    assert themes.sort_codes(["\u00b2", "1"]) == ["1", "\u00b2"]
    assert themes.columns(codes, width=12) == "9     0027\n100   abc"


class FakeConnection:
    def __init__(self, files=(), output="", exit_status=0):
        self.files = {f"/mnt/usb0/{name}": b"" for name in files}
        self.commands = []
        self.result = CommandResult(exit_status, output)

    def run(self, command, *, sudo=False, timeout=60):
        self.commands.append((command, sudo))
        return self.result

    def listdir(self, path):
        if path != "/mnt/usb0":
            raise FileNotFoundError(path)
        return [name.rsplit("/", 1)[1] for name in self.files]

    def exists(self, path):
        return path in self.files

    def upload(self, fileobj, path):
        self.files[path] = fileobj.read()

    def start(self, command):
        self.commands.append((command, False))


def make_pi(**kwargs):
    connection = FakeConnection(**kwargs)
    return ThemesPi(connection, ThemesSettings()), connection


def test_list_codes_only_shows_playable_themes():
    pi, _ = make_pi(files=["0027.mp3", "100.mp3", "notes.txt", ".0099.mp3.part", "5.MP3"])
    assert pi.list_codes() == ["0027", "100"]


def test_list_codes_without_usb_drive():
    pi, _ = make_pi()
    pi.settings = ThemesSettings(songs_dir="/mnt/usb1")
    with pytest.raises(ThemeError, match="USB drive"):
        pi.list_codes()


def test_add_and_find_theme():
    pi, connection = make_pi(files=["0027.mp3"])
    assert pi.has_theme("0027") and not pi.has_theme("0042")
    pi.add_theme("0042", ID3_MP3)
    assert connection.files["/mnt/usb0/0042.mp3"] == ID3_MP3


def test_speak_quotes_text_for_the_shell():
    pi, connection = make_pi()
    pi.speak("it's $(rm -rf ~)")
    assert connection.commands == [
        ("espeak -a 200 -- 'it'\"'\"'s $(rm -rf ~)' 2>/dev/null", False)
    ]
    pi.speak("-5 minutes to the bell")  # not read as espeak options
    assert connection.commands[-1][0].startswith("espeak -a 200 -- '-5 minutes")


def test_volume_commands():
    pi, connection = make_pi(output="Mono: Playback [42%] [on]")
    assert pi.volume() == 42
    pi.set_volume(0)
    assert connection.commands[-1] == ("amixer sset Headphone,0 0%", False)


def test_reboot_uses_sudo():
    pi, connection = make_pi()
    pi.reboot()
    assert connection.commands == [("shutdown -r now", True)]


def test_start_player_runs_a_silent_copy_of_themes_py():
    pi, connection = make_pi()
    pi.start_player()
    assert connection.commands == [
        ("cd /home/pi/themes && stty -echo && exec python themes.py silent", False)
    ]


class FakeChannel:
    def __init__(self):
        self.incoming = queue.Queue()
        self.sent = []

    def recv(self, size):
        return self.incoming.get(timeout=5)

    def sendall(self, data):
        self.sent.append(data)

    def close(self):
        self.incoming.put(b"")


def test_player_session_shows_tidy_output_and_sends_codes():
    channel, shown = FakeChannel(), []
    session = PlayerSession(channel, show=shown.append)
    channel.incoming.put(b"pygame 2.1.0\r\nPlease enter a code:\r\n> ")
    assert session.ready.wait(5) and not session.ended.is_set()

    session.send("0027")
    channel.incoming.put(b"Found /mnt/usb0/0027.mp3\r\nPlease enter a code:\r\n> File 'x\xc3")
    channel.incoming.put(b"\xa9.mp3' not found.\r\n")  # a UTF-8 character split between reads
    session.stop()

    assert channel.sent == [b"0027\n"]
    assert shown == ["Found /mnt/usb0/0027.mp3", "File 'x\u00e9.mp3' not found."]
    assert session.ended.is_set()


def test_player_session_that_fails_to_start():
    channel, shown = FakeChannel(), []
    session = PlayerSession(channel, show=shown.append)
    channel.incoming.put(b"bash: python: command not found\r\n")
    channel.incoming.put(b"")
    assert session.ready.wait(5) and session.ended.wait(5)
    assert shown == ["bash: python: command not found"]
