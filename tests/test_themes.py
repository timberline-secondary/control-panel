import queue
import shlex
import socket
import threading
import zipfile
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


# An MP3 frame: MPEG 1 layer III, 128 kbps, 44.1 kHz, so 417 bytes long.
FRAME = b"\xff\xfb\x90\x00" + b"\x55" * 413


@pytest.mark.parametrize("data, expected", [
    (b"\x00" * 1000 + FRAME * 2, True),  # padding before the audio
    (b"junk from an editor" + FRAME * 3, True),
    (b"\x00" * 100 + FRAME[:4] + b"\x00" * 500, False),  # looks like one frame, by chance
    (b"\x00" * themes.MP3_SEARCH + FRAME * 2, False),  # too far in to be its audio
], ids=["padding first", "junk first", "one frame by chance", "too far in"])
def test_mp3_audio_after_something_else(data, expected):
    assert themes.looks_like_mp3(data) is expected


@pytest.mark.parametrize("data, kind", [
    (b"\n  <!DOCTYPE html><html>", "a web page"),
    (b"\x00\x00\x00\x20ftypM4A ", "an M4A (or MP4) file"),
    (b"RIFF\x00\x00\x00\x00WAVEfmt ", "a WAV file"),
    (b"OggS\x00", "an OGG file"),
    (b"", "an empty file"),
    (b"\x00\x01\x02", None),
])
def test_what_it_is(data, kind):
    assert themes.what_it_is(data) == kind


@pytest.mark.parametrize("name, expected", [
    ("0027.mp3", "0027"),  # keeps leading zeros
    ("1234", "1234"),
    ("my song.mp3", None),
    ("12a.mp3", None),
    ("\u00b2.mp3", None),  # '²'.isdigit() is True, but it's not a code
    ("2+2.mp3", "2+2"), ("-1.5.mp3", "-1.5"),  # every key on the keypad
    ("2x2.mp3", "2*2"), ("3d4.mp3", "3/4"),  # how 2*2 and 3/4 are saved
    ("x5.mp3", None),  # *5 would be an admin code
    ("max.mp3", None),
])
def test_suggest_code(name, expected):
    assert themes.suggest_code(name) == expected


@pytest.mark.parametrize("code, ok", [
    ("0027", True), ("abc", True), ("a-b_c", True),
    ("", False), ("*0000", False), ("exit", False), ("EXIT", False),
    ("12 34", False), ("2#2", False), ("2+2", True), ("2*2", True), ("3/4", True), (".5", True),
    ("../etc", True),  # (saved as ..detc.mp3: / is never a folder in a theme's file name)
])
def test_code_problem(code, ok):
    assert (themes.code_problem(code) is None) is ok


@pytest.mark.parametrize("code, name", [
    ("0027", "0027.mp3"), ("2+2", "2+2.mp3"), ("-1.5", "-1.5.mp3"),
    ("2*2", "2x2.mp3"), ("3/4", "3d4.mp3"), ("*/", None),
])
def test_file_names_for_keypad_codes(code, name):
    if name:
        assert themes.file_name(code) == name
        assert themes.code_of(name[:-4]) == code  # and back again, e.g. when listing them
    assert themes.code_of("max") == "max"  # not typed on the keypad, so not swapped back
    assert themes.code_of("x") == "x"  # * would be an admin code
    assert (themes.swap_note(code) is not None) is ("*" in code or "/" in code)


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
    with pytest.raises(ThemeError, match="theme.mp3 is a web page, not an mp3"):
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
    with pytest.raises(ThemeError, match="goes to a web page"):
        themes.read_mp3("https://example.com/watch?v=123")


def test_read_mp3_link_with_something_before_the_audio(monkeypatch):
    # e.g. a site's mp3 with padding first, given as audio/mpeg
    song = b"\x00" * 2000 + FRAME * 4
    monkeypatch.setattr(themes, "_download", lambda url: (song, "audio/mpeg"))
    assert themes.read_mp3("https://example.com/2502013600.mp3") == ("2502013600.mp3", song)


def test_read_mp3_link_to_another_kind_of_audio(monkeypatch):
    monkeypatch.setattr(themes, "_download", lambda url: (b"\x00\x00\x00\x20ftypM4A ",
                                                         "audio/mpeg"))
    with pytest.raises(ThemeError, match="is an M4A \\(or MP4\\) file, not an mp3"):
        themes.read_mp3("https://example.com/song.mp3")


@pytest.mark.parametrize("line, expected", [
    ("Please enter a code:\r", None),
    ("> ", None),
    ("> Found /mnt/usb0//0027.mp3\r", "Playing 0027."),
    ("File '9.mp3' not found.", "There's no theme with code 9."),
    ("> Found /mnt/usb0/2x2.mp3", "Playing 2*2."),
    ("File '3d4.mp3' not found.", "There's no theme with code 3/4."),
    ("ALSA lib pcm.c:8545:(snd_pcm_recover) underrun occurred", None),
    ("Spam block is now set to 5", "Spam block is now set to 5"),
    ("pygame 2.1.0 (SDL 2.0.16, Python 3.9.2)", None),
    ("Hello from the pygame community. https://www.pygame.org/contribute.html", None),
])
def test_tidy_player_line(line, expected):
    assert themes.tidy_player_line(line) == expected


def test_parse_volume():
    output = "Simple mixer control 'Headphone',0\n  Mono: Playback -2000 [77%] [-20.00dB] [on]"
    assert themes.parse_volume(output) == 77
    assert themes.parse_volume("nothing here") is None


def test_sort_codes_digit_by_digit():
    # Leading zeros count, like words in a dictionary.
    assert themes.sort_codes(["4261992", "15111955", "05111955"]) == [
        "05111955", "15111955", "4261992"]
    assert themes.sort_codes(["100", "abc", "0027", "9", "Abd"]) == [
        "0027", "100", "9", "abc", "Abd"]


def test_search_codes():
    codes = ["05111955", "15111955", "4261992", "Intro"]
    assert themes.search_codes(codes, "1955") == ["05111955", "15111955"]
    assert themes.search_codes(codes, "05") == ["05111955"]
    assert themes.search_codes(codes, "intro") == ["Intro"]
    assert themes.search_codes(codes, "777") == []


def test_columns():
    assert themes.columns(["0027", "100", "9", "abc"], width=12) == "0027  100\n9     abc"


class FakeConnection:
    def __init__(self, files=(), output="", exit_status=0):
        self.files = {f"/mnt/usb0/{name}": b"" for name in files}
        self.commands = []
        self.result = CommandResult(exit_status, output)

    def run(self, command, *, sudo=False, timeout=60):
        self.commands.append((command, sudo))
        if command.startswith("cp -- ") and self.result.ok:  # as the Pi would
            _, _, source, target = shlex.split(command)
            self.files[target] = self.files[source]
        return self.result

    def listdir(self, path):
        if path != "/mnt/usb0":
            raise FileNotFoundError(path)
        return [name.rsplit("/", 1)[1] for name in self.files]

    def exists(self, path):
        return path in self.files

    def upload(self, fileobj, path):
        self.files[path] = fileobj.read()

    def mkdir(self, path):
        pass

    def rename(self, old, new):
        self.files[new] = self.files.pop(old)

    def remove(self, path):
        del self.files[path]

    def start(self, command):
        self.commands.append((command, False))


def make_pi(**kwargs):
    connection = FakeConnection(**kwargs)
    return ThemesPi(connection, ThemesSettings()), connection


def test_list_codes_only_shows_playable_themes():
    pi, _ = make_pi(files=["0027.mp3", "100.mp3", "notes.txt", ".0099.mp3.part", "5.MP3"])
    assert pi.list_codes() == ["0027", "100"]


def test_list_codes_shows_keypad_codes():
    pi, _ = make_pi(files=["2x2.mp3", "3d4.mp3", "2+2.mp3", ".5.mp3", "._0027.mp3", "max.mp3"])
    assert pi.list_codes() == [".5", "2*2", "2+2", "3/4", "max"]  # not the Mac's ._0027


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
    assert shown == ["Playing 0027.", "There's no theme with code x\u00e9."]
    assert session.ended.is_set()


def test_player_session_that_fails_to_start():
    channel, shown = FakeChannel(), []
    session = PlayerSession(channel, show=shown.append)
    channel.incoming.put(b"bash: python: command not found\r\n")
    channel.incoming.put(b"")
    assert session.ready.wait(5) and session.ended.wait(5)
    assert shown == ["bash: python: command not found"]


def test_list_themes_then_search(monkeypatch):
    pi, _ = make_pi(files=["4261992.mp3", "15111955.mp3", "05111955.mp3"])
    shown = []
    for kind in ["success", "info", "warning"]:
        monkeypatch.setattr(themes.ui, kind, lambda text, kind=kind: shown.append((kind, text)))
    answers = iter(["1955", "777", ""])  # Enter on its own goes back
    monkeypatch.setattr(themes.ui, "ask", lambda *a, **k: next(answers))

    themes.list_themes(pi)

    assert shown == [
        ("success", "3 themes on pi-themes:"),
        ("info", "05111955  15111955  4261992"),
        ("success", "2 of 3 codes contain '1955':"),
        ("info", "05111955  15111955"),
        ("warning", "No codes contain '777'."),
    ]
    assert next(answers, "done") == "done"  # stopped asking after the Enter


@pytest.fixture
def add_screen(monkeypatch, tmp_path):
    """Runs 'Add a new theme' with scripted answers. Returns (run, uploaded files, messages)."""
    song = tmp_path / "0027.mp3"
    song.write_bytes(ID3_MP3)
    pi, connection = make_pi(files=["0027.mp3"])
    messages = []
    for kind in ["success", "info", "warning", "error"]:
        monkeypatch.setattr(themes.ui, kind, lambda text, kind=kind: messages.append(text))
    monkeypatch.setattr(themes.ffmpeg, "find", lambda configured="": None)  # not needed

    prompts = []

    def run(asks, chooses=(), confirms=()):
        asks, chooses, confirms = iter(asks), iter(chooses), iter(confirms)
        monkeypatch.setattr(themes.ui, "ask",
                            lambda prompt, **k: prompts.append(prompt) or next(asks))
        monkeypatch.setattr(themes.ui, "choose", lambda *a, **k: next(chooses))
        monkeypatch.setattr(themes.ui, "confirm", lambda prompt, **k: next(confirms))
        themes.add_theme(pi)
        for left in (asks, chooses, confirms):  # every scripted answer was asked for
            assert next(left, "done") == "done"

    run.prompts = prompts
    run.pi = pi
    return run, str(song), connection.files, messages


def test_add_theme_using_the_suggested_code(add_screen):
    run, song, files, messages = add_screen
    run(asks=[song, "", None], chooses=["add", "replace"])  # Enter takes 0027, then q
    assert files["/mnt/usb0/0027.mp3"] == ID3_MP3


def test_after_adding_a_theme_it_asks_for_the_next_one(add_screen):
    run, song, files, messages = add_screen
    run(asks=[song, "0042", song, "0043", None], chooses=["add", "add"])
    assert files["/mnt/usb0/0042.mp3"] == files["/mnt/usb0/0043.mp3"] == ID3_MP3
    links = [p for p in run.prompts if "into this window" in p]
    assert links == ["Paste a link, or drag mp3 files into this window",
                     "To add more, paste a link or drag mp3 files into this window",
                     "To add more, paste a link or drag mp3 files into this window"]


def test_add_a_theme_with_a_code_that_cant_be_a_file_name(add_screen):
    run, song, files, messages = add_screen
    run(asks=[song, "2*2", None], chooses=["add"])
    assert files["/mnt/usb0/2x2.mp3"] == ID3_MP3
    note = ("File names can't have * or / in them, so theme 2*2 is saved as 2x2.mp3 (* as x, "
            "/ as d). The player swaps them back when a code is typed, so typing 2*2 on the "
            "keypad plays it.")
    assert messages.count(note) == 1  # when it's typed
    assert "Added! Type 2*2 on the entrance keypad to play it." in messages
    assert not any("doesn't swap them back yet" in m for m in messages)  # (the player does)
    commands = [command for command, sudo in run.pi.connection.commands]
    assert "grep -q FILE_NAME_SWAPS /home/pi/themes/themes.py" in commands


def test_adding_2x2_mp3_suggests_2_times_2(add_screen, tmp_path):
    run, song, files, messages = add_screen
    named = tmp_path / "2x2.mp3"
    named.write_bytes(ID3_MP3)
    run(asks=[str(named), "", None], chooses=["add"])  # Enter takes the suggested 2*2
    assert files["/mnt/usb0/2x2.mp3"] == ID3_MP3
    assert any(m.startswith("2x2.mp3 is named for the code 2*2. File names can't")
               for m in messages)


def test_adding_2_times_2_says_when_the_player_needs_updating(add_screen):
    run, song, files, messages = add_screen
    run.pi.connection.result = CommandResult(1, "")  # its themes.py doesn't have the swaps
    run(asks=[song, "3/4", None], chooses=["add"])
    assert files["/mnt/usb0/3d4.mp3"] == ID3_MP3
    assert any(m.startswith("pi-themes' player doesn't swap them back yet, so typing 3/4 won't")
               for m in messages)


def test_add_theme_with_a_different_code_after_a_clash(add_screen):
    run, song, files, messages = add_screen
    run(asks=[song, "", "0042", None], chooses=["add", "different"])
    assert files["/mnt/usb0/0042.mp3"] == ID3_MP3
    assert files["/mnt/usb0/0027.mp3"] == b""  # untouched


def test_add_several_themes_at_once(add_screen, tmp_path):
    run, song, files, messages = add_screen
    folder = tmp_path / "themes"
    folder.mkdir()
    (folder / "1111.mp3").write_bytes(ID3_MP3)
    (folder / "notes.txt").write_text("not a song")
    with zipfile.ZipFile(tmp_path / "more.zip", "w") as archive:
        archive.writestr("songs/2222.mp3", ID3_MP3)
        archive.writestr("songs/cover.jpg", b"jpg")
        archive.writestr("__MACOSX/songs/._2222.mp3", b"junk")
    run(asks=[f'"{folder}" "{tmp_path / "more.zip"}"', "", "", None], chooses=["add", "add"])
    assert files["/mnt/usb0/1111.mp3"] == files["/mnt/usb0/2222.mp3"] == ID3_MP3
    assert "1 of 2: 1111.mp3" in "".join(messages) and "2 of 2: 2222.mp3" in "".join(messages)


def test_play_a_theme_here_before_adding_it(add_screen, monkeypatch):
    run, song, files, messages = add_screen
    played = []
    monkeypatch.setattr(themes._Tools, "__init__", lambda self, settings: setattr(
        self, "open_file", lambda path: played.append(path.read_bytes())) or setattr(
        self, "_found", None))
    run(asks=[song, "0042", None], chooses=["play", "add"])
    assert played == [ID3_MP3]
    assert files["/mnt/usb0/0042.mp3"] == ID3_MP3


def test_trim_a_theme_and_even_out_its_loudness(add_screen, monkeypatch, tmp_path, ffmpeg_path):
    run, song, files, messages = add_screen
    monkeypatch.setattr(themes.ffmpeg, "find", lambda configured="": ffmpeg_path)
    long_song = tmp_path / "a song.mp3"
    themes.ffmpeg.run(ffmpeg_path, ["-f", "lavfi", "-i", "sine=frequency=440:duration=10",
                                    "-af", "volume=0.05", "-c:a", "libmp3lame", str(long_song)])
    run(asks=[str(long_song), "0:02", "3", "0042", None], chooses=["trim", "level", "add"])
    saved = tmp_path / "saved.mp3"
    saved.write_bytes(files["/mnt/usb0/0042.mp3"])
    assert 2.8 < themes.ffmpeg.audio_seconds(ffmpeg_path, saved) < 3.3
    assert any("0:10 long" in m for m in messages)
    assert any("it is 0:03 long now" in m for m in messages)
    assert run.pi.connection.commands[-1][0].startswith("printf ")  # noted in the log
    assert "Added theme 0042 (a song.mp3, from 0:02 for 0:03, loudness evened out)" in \
        run.pi.connection.commands[-1][0]


@pytest.mark.parametrize("text, seconds", [
    ("30", 30), ("0:30", 30), ("1:02.5", 62.5), ("1:00:00", 3600), ("", None), ("1:x", None),
])
def test_parse_seconds(text, seconds):
    assert themes.parse_seconds(text) == seconds
    if seconds:
        assert themes.parse_seconds(themes.clock_text(seconds)) == round(seconds)


@pytest.mark.parametrize("asks, chooses", [
    ([None], []),  # q (or Ctrl+C) at the link prompt
    (["SONG"], [None]),  # q at "Add it"
    (["SONG", None], ["add"]),  # q at the code prompt
    (["SONG", ""], ["add", None]),  # Cancel (or Ctrl+C) when the code is taken
    (["SONG", "", None], ["add", "different"]),  # q when asked for a different code
])
def test_add_theme_can_always_be_left(add_screen, asks, chooses):
    run, song, files, messages = add_screen
    run(asks=[song if a == "SONG" else a for a in asks], chooses=chooses)
    assert files == {"/mnt/usb0/0027.mp3": b""}


@pytest.fixture
def remove_screen(monkeypatch):
    """Runs 'Remove a theme' with scripted answers. Returns (run, files, messages)."""
    pi, connection = make_pi(files=["0027.mp3", "0042.mp3"])
    connection.files["/mnt/usb0/0027.mp3"] = ID3_MP3
    messages = []
    for kind in ["success", "info", "warning", "error"]:
        monkeypatch.setattr(themes.ui, kind, lambda text, kind=kind: messages.append(text))

    def run(asks, chooses=()):
        asks, chooses = iter(asks), iter(chooses)
        monkeypatch.setattr(themes.ui, "ask", lambda prompt, **k: next(asks))
        monkeypatch.setattr(themes.ui, "choose", lambda *a, **k: next(chooses))
        themes.remove_theme(pi)
        for left in (asks, chooses):  # every scripted answer was asked for
            assert next(left, "done") == "done"

    return run, connection.files, messages


def test_remove_a_theme_keeping_a_backup(remove_screen):
    run, files, messages = remove_screen
    run(asks=["0027"], chooses=["backup"])
    assert files == {"/mnt/usb0/removed/0027.mp3": ID3_MP3, "/mnt/usb0/0042.mp3": b""}
    assert messages == ["Removed theme 0027. Its backup is /mnt/usb0/removed/0027.mp3."]


def test_removing_a_theme_again_keeps_both_backups(remove_screen):
    run, files, messages = remove_screen
    files["/mnt/usb0/removed/0027.mp3"] = b"older"
    run(asks=["0027"], chooses=["backup"])
    assert files["/mnt/usb0/removed/0027.mp3"] == b"older"
    assert files["/mnt/usb0/removed/0027 (2).mp3"] == ID3_MP3


def test_remove_a_theme_saved_with_a_swapped_name(remove_screen):
    run, files, messages = remove_screen
    files["/mnt/usb0/2x2.mp3"] = ID3_MP3
    run(asks=["2*2"], chooses=["backup"])
    assert files["/mnt/usb0/removed/2x2.mp3"] == ID3_MP3 and "/mnt/usb0/2x2.mp3" not in files


def test_delete_a_theme_for_good(remove_screen):
    run, files, messages = remove_screen
    run(asks=["0027"], chooses=["delete"])
    assert files == {"/mnt/usb0/0042.mp3": b""}
    assert messages == ["Deleted theme 0027."]


def test_remove_asks_again_for_a_code_that_isnt_there(remove_screen):
    run, files, messages = remove_screen
    run(asks=["9999", "../0027", "0042"], chooses=["delete"])
    assert messages[0] == "There's no theme with code 9999. 'List and search themes' shows them."
    assert "/mnt/usb0/0027.mp3" in files and "/mnt/usb0/0042.mp3" not in files


@pytest.mark.parametrize("asks, chooses", [([None], []), (["0027"], [None])])
def test_remove_can_be_left_without_removing_anything(remove_screen, asks, chooses):
    run, files, messages = remove_screen
    run(asks=asks, chooses=chooses)
    assert set(files) == {"/mnt/usb0/0027.mp3", "/mnt/usb0/0042.mp3"}


@pytest.mark.parametrize("level, expected", [(42, "0%"), (0, "100%")])
def test_toggle_mute(monkeypatch, level, expected):
    pi, connection = make_pi()
    monkeypatch.setattr(pi, "volume", lambda: level)
    monkeypatch.setattr(themes.ui, "success", lambda text: None)
    themes.toggle_mute(pi)
    assert connection.commands[-1] == (f"amixer sset Headphone,0 {expected}", False)


def test_the_menu_is_in_the_asked_for_order():
    assert [title for title, _ in themes.ACTIONS] == [
        "Add new themes (one or several)",
        "Copy a theme to another code (so both codes play it)",
        "Change a theme's code (rename it)", "Remove a theme", "Play a theme",
        "Watch the keypad (live: the codes typed, and what they played)",
        "List and search themes", "Toggle mute", "Say something (text to speech)",
        "Reboot pi-themes",
        "Recent changes (who added, changed or removed which themes, and when)",
        "pi-themes documentation"]


@pytest.fixture
def theme_screen(monkeypatch):
    """Runs a theme menu item with scripted answers. Returns (run, files, messages, log)."""
    pi, connection = make_pi(files=["0042.mp3"])
    connection.files["/mnt/usb0/0027.mp3"] = ID3_MP3
    messages = []
    for kind in ["success", "info", "warning", "error"]:
        monkeypatch.setattr(themes.ui, kind, lambda text, kind=kind: messages.append(text))

    def run(action, asks, chooses=(), confirms=()):
        asks, chooses, confirms = iter(asks), iter(chooses), iter(confirms)
        monkeypatch.setattr(themes.ui, "ask", lambda prompt, **k: next(asks))
        monkeypatch.setattr(themes.ui, "choose", lambda *a, **k: next(chooses))
        monkeypatch.setattr(themes.ui, "confirm", lambda prompt, **k: next(confirms))
        action(pi)
        for left in (asks, chooses, confirms):  # every scripted answer was asked for
            assert next(left, "done") == "done"

    def log():
        return [shlex.split(c)[2] for c, _ in connection.commands if c.startswith("printf")]

    return run, connection.files, messages, log


def test_copy_a_theme_to_another_code(theme_screen):
    run, files, messages, log = theme_screen
    run(themes.copy_theme, asks=["0027", "2*2"])
    assert files["/mnt/usb0/0027.mp3"] == files["/mnt/usb0/2x2.mp3"] == ID3_MP3
    assert set(files) == {"/mnt/usb0/0027.mp3", "/mnt/usb0/0042.mp3", "/mnt/usb0/2x2.mp3"}
    assert ("Copied! Now 0027 and 2*2 both play it. Type 2*2 on the entrance keypad to play "
            "it.") in messages
    assert any(m.startswith("File names can't have * or /") for m in messages)
    assert "Copied theme 0027 to 2*2, as 2x2.mp3" in log()[0]


def test_copy_a_theme_over_another_keeps_that_one_in_removed(theme_screen):
    run, files, messages, log = theme_screen
    run(themes.copy_theme, asks=["0027", "0042"], chooses=["replace"])
    assert files["/mnt/usb0/0042.mp3"] == ID3_MP3  # 0027's song
    assert files["/mnt/usb0/removed/0042.mp3"] == b""  # the song 0042 had
    assert "Theme 0042 is kept as /mnt/usb0/removed/0042.mp3." in messages
    assert "the theme it replaced is kept as /mnt/usb0/removed/0042.mp3" in log()[-1]


def test_copy_needs_a_different_code(theme_screen):
    run, files, messages, log = theme_screen
    run(themes.copy_theme, asks=["0099", "0027", "0027", "0043"])  # no 0099; 0027 is its own
    assert "There's no theme with code 0099. 'List and search themes' shows them." in messages
    assert "That's the code it has now. Type a different one." in messages
    assert files["/mnt/usb0/0043.mp3"] == ID3_MP3


def test_copy_that_fails_on_the_pi_says_so(theme_screen):
    run, files, messages, log = theme_screen
    full = CommandResult(1, "cp: No space left on device")
    run(lambda pi: (setattr(pi.connection, "result", full), themes.copy_theme(pi)),
        asks=["0027", "0043"])
    assert "Couldn't copy it on pi-themes: cp: No space left on device" in messages
    assert "/mnt/usb0/0043.mp3" not in files


def test_change_a_themes_code(theme_screen):
    run, files, messages, log = theme_screen
    run(themes.rename_theme, asks=["0027", "3/4"])
    assert "/mnt/usb0/0027.mp3" not in files
    assert files["/mnt/usb0/3d4.mp3"] == ID3_MP3
    assert ("Done! Theme 0027 is now 3/4, and 0027 doesn't play anything. Type 3/4 on the "
            "entrance keypad to play it.") in messages
    assert "Changed theme 0027's code to 3/4, as 3d4.mp3" in log()[0]


def test_change_a_themes_code_to_one_thats_taken(theme_screen):
    run, files, messages, log = theme_screen
    run(themes.rename_theme, asks=["0027", "0042", "0043"], chooses=["different"])
    assert set(files) == {"/mnt/usb0/0042.mp3", "/mnt/usb0/0043.mp3"}  # 0042 untouched


def test_change_a_code_to_the_same_file_name(theme_screen):
    run, files, messages, log = theme_screen
    files["/mnt/usb0/2x2.mp3"] = b"two"
    run(themes.rename_theme, asks=["2x2", "2*2", None])  # 2*2 is 2x2.mp3 too
    assert "That's the code it has now. Type a different one." in messages
    assert files["/mnt/usb0/2x2.mp3"] == b"two"


def test_replacing_a_theme_when_adding_keeps_the_old_one(add_screen):
    run, song, files, messages = add_screen
    run(asks=[song, "", None], chooses=["add", "replace"])  # Enter takes 0027, then q
    assert files["/mnt/usb0/0027.mp3"] == ID3_MP3
    assert files["/mnt/usb0/removed/0027.mp3"] == b""  # the one it replaced


def test_documentation_needs_no_login(monkeypatch, capsys):
    monkeypatch.setattr(themes.login, "connect", lambda settings: pytest.fail("logged in"))
    themes._do(themes.docs, themes.ThemesSettings())
    assert "https://github.com/timberline-secondary/themes" in capsys.readouterr().out


@pytest.mark.parametrize("line, expected", [
    ("2026-11-27 10:31:02  keypad  2*2           played 2x2.mp3\r",
     "10:31:02  2*2         played 2x2.mp3"),
    ("2026-11-26 16:00:00  keypad  9/9           not found (there's no 9d9.mp3)",
     "2026-11-26 16:00:00  9/9         not found (there's no 9d9.mp3)"),  # not today
    ("2026-11-27 10:35:00  ssh     0027          played 0027.mp3",
     "10:35:00  0027        played 0027.mp3  (not the keypad)"),
    ("2026-11-27 10:36:00  keypad  (admin)       spam block set to 7 minutes",
     "10:36:00  (admin)     spam block set to 7 minutes"),
    ("tail: something odd", "tail: something odd"),  # not a line it wrote: as it is
    ("\r", None),
])
def test_tidy_keypad_line(line, expected):
    assert themes.tidy_keypad_line(line, "2026-11-27") == expected


def test_keypad_watch_shows_each_line_until_stopped():
    channel, shown = FakeChannel(), []
    watch = themes.KeypadWatch(channel, show=shown.append)
    channel.incoming.put(b"first\r\nsec")
    channel.incoming.put(b"ond\r\n")
    deadline = threading.Event()
    for _ in range(50):
        if len(shown) == 2:
            break
        deadline.wait(0.05)
    watch.stop()
    assert shown == ["first\r", "second\r"]


@pytest.fixture
def watch_screen(monkeypatch):
    """Runs 'Watch the keypad'. Returns (pi, connection, channel, shown messages)."""
    pi, connection = make_pi()
    channel, shown = FakeChannel(), []
    for kind in ["success", "info", "warning", "error"]:
        monkeypatch.setattr(themes.ui, kind, lambda text, kind=kind: shown.append((kind, text)))
    monkeypatch.setattr(connection, "start", lambda command: (
        connection.commands.append((command, False)), channel)[1])
    return pi, connection, channel, shown


def test_watch_the_keypad(watch_screen, monkeypatch):
    pi, connection, channel, shown = watch_screen
    connection.files["/home/pi/themes/keypad.log"] = b""
    today = themes.datetime.date.today().isoformat()

    def press_enter_once_shown(prompt):  # the codes arrive while they're watching
        channel.incoming.put(f"{today} 10:31:02  keypad  2*2  played 2x2.mp3\r\n".encode())
        for _ in range(50):
            if any("2*2" in text for _, text in shown):
                break
            threading.Event().wait(0.05)

    monkeypatch.setattr(themes.ui, "pause", press_enter_once_shown)
    themes.watch_keypad(pi)
    commands = [c for c, _ in connection.commands]
    assert commands == ["grep -q KEYPAD_LOG /home/pi/themes/themes.py",
                        "tail -n 20 -F /home/pi/themes/keypad.log 2>/dev/null"]
    assert ("info", "  10:31:02  2*2         played 2x2.mp3") in shown


def test_watch_the_keypad_with_an_old_player(watch_screen, monkeypatch):
    pi, connection, channel, shown = watch_screen
    connection.result = CommandResult(1, "")  # its themes.py doesn't have KEYPAD_LOG
    monkeypatch.setattr(themes.ui, "pause", lambda prompt: pytest.fail("shouldn't watch"))
    themes.watch_keypad(pi)
    assert shown[0][0] == "warning" and "doesn't note the codes typed yet" in shown[0][1]
    assert [c for c, _ in connection.commands] == [
        "grep -q KEYPAD_LOG /home/pi/themes/themes.py"]  # nothing started
