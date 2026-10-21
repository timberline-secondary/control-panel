import contextlib
import io
import json
import posixpath
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest
from fakes import FakeFilesConnection
from PIL import Image

from control_panel import ffmpeg, shrine, ui
from control_panel.art import ArtFolder, ArtServer
from control_panel.config import Config, TvsSettings
from control_panel.panels import grade9, tvs
from control_panel.panels.tvs import TvError, TvPi
from control_panel.ssh import CommandResult

DF = ("Filesystem     1024-blocks    Used Available Capacity Mounted on\n"
      "/dev/root         30000000 1000000  {free}       4% /\n")


class FakeTvConnection:
    """Pretends to be a TV Pi. files: {name: contents} in its media folder."""

    def __init__(self, files=(), room=10_000_000_000, usb="", restart_status=0):
        self.host = "pi-tv1.hackerspace.tbl"
        self.files = dict(files)
        self.room = room  # bytes free on its SD card
        self.usb = usb
        self.restart_status = restart_status
        self.commands = []
        self.closed = False
        self.sftp = SimpleNamespace(stat=lambda path: SimpleNamespace(
            st_size=len(self.files[posixpath.basename(path)])))

    def run(self, command, *, sudo=False, timeout=60):
        self.commands.append((command, sudo))
        if command.startswith("df "):
            return CommandResult(0, DF.format(free=self.room // 1024))
        if command.startswith("ls /dev/disk/by-id"):
            return CommandResult(0, self.usb)
        if "restart rs" in command:
            return CommandResult(self.restart_status, "Unit rs.service not found."
                                 if self.restart_status else "")
        return CommandResult(0, "")

    def file_sizes(self, path):
        if path != "/home/pi/rs_media":
            raise FileNotFoundError(path)
        return {name: len(data) for name, data in self.files.items()}

    def upload(self, fileobj, path, progress=None):
        data = fileobj.read()
        name = posixpath.basename(path)
        self.room += len(self.files.get(name, b"")) - len(data)
        self.files[name] = data
        if progress:
            progress(len(data), len(data))

    def open(self, path):
        return io.BytesIO(self.files[posixpath.basename(path)])

    def remove(self, path):
        self.room += len(self.files.pop(posixpath.basename(path)))

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def make_tv(number=1, **kwargs):
    connection = FakeTvConnection(**kwargs)
    return TvPi(connection, number, TvsSettings()), connection


@contextlib.contextmanager
def no_progress(label):
    yield lambda fraction: None


class Screen:
    """Scripted answers for the questions, and a record of what was shown and asked."""

    def __init__(self, monkeypatch, asks=(), chooses=(), confirms=(), picks=()):
        self.messages = []
        self.questions = []
        self.asks, self.chooses = iter(asks), iter(chooses)
        self.confirms, self.picks = iter(confirms), iter(picks)
        for kind in ["info", "success", "warning", "error"]:
            monkeypatch.setattr(ui, kind, lambda text, kind=kind: self.messages.append(
                (kind, text)))
        monkeypatch.setattr(ui, "ask", lambda prompt, **k: self._next(self.asks, prompt))
        monkeypatch.setattr(ui, "choose", self._choose)
        monkeypatch.setattr(ui, "confirm", lambda prompt, **k: self._next(self.confirms, prompt))
        monkeypatch.setattr(ui, "choose_many", lambda choices, question="": self._next(
            self.picks, question))
        monkeypatch.setattr(ui, "progress", no_progress)

    def _next(self, answers, question):
        self.questions.append(question)
        return next(answers)

    def _choose(self, choices, question="", default=None):
        answer = self._next(self.chooses, question)
        offered = [c[1] if isinstance(c, tuple) else c for c in choices]
        assert answer is None or answer in offered, \
            f"{answer!r} isn't one of the choices for {question!r}"
        return answer

    def finished(self):
        for left in (self.asks, self.chooses, self.confirms, self.picks):
            assert next(left, "done") == "done", "not every scripted answer was asked for"

    def shown(self, kind=None):
        return "\n".join(text for k, text in self.messages if kind in (None, k))


ANN = ArtFolder(1, "/mnt/ssd/tv1/ann.lee", "ann.lee")


@pytest.fixture
def pi_files(monkeypatch):
    """pi-files, with nothing on it yet."""
    server = ArtServer(FakeFilesConnection(), TvsSettings())

    @contextlib.contextmanager
    def fake(settings):
        yield server
    monkeypatch.setattr(tvs, "art_server", fake)
    return server


@pytest.fixture
def the_tvs(monkeypatch):
    """Four empty TVs: {number: its connection}."""
    connections = {n: FakeTvConnection() for n in tvs.TVS}
    monkeypatch.setattr(tvs, "connect", lambda settings, numbers: {
        n: TvPi(connections[n], n, settings) for n in numbers})
    return connections


def _picture(path, colour, size=(40, 30)):
    Image.new("RGB", size, colour).save(path)
    return path.read_bytes()


def _animation(path):
    frames = [Image.new("RGB", (16, 16), c) for c in ("red", "green")]
    frames[0].save(path, save_all=True, append_images=frames[1:], duration=500)
    return path.read_bytes()


def _colour_at(ffmpeg_path, data, seconds, tmp_path):
    video, frame = tmp_path / "check.mp4", tmp_path / f"check-{seconds}.png"
    video.write_bytes(data)
    ffmpeg.run(ffmpeg_path, ["-ss", str(seconds), "-i", str(video), "-frames:v", "1",
                             str(frame)])
    return Image.open(frame).convert("RGB").getpixel((960, 540))


def _slide_colours(ffmpeg_path, data, tmp_path, count):
    return [_colour_at(ffmpeg_path, data, 8 * k + 4.5, tmp_path) for k in range(count)]


WHITE, RED, BLUE = (255, 255, 255), (255, 0, 0), (0, 0, 255)


def assert_colours(colours, expected):
    assert len(colours) == len(expected)
    for colour, wanted in zip(colours, expected, strict=True):
        assert colour == pytest.approx(wanted, abs=10), (colours, expected)


def videos_on(server, art=ANN):
    return sorted(server.videos(art))


# --- Helpers -------------------------------------------------------------------------------

def test_parse_free_space():
    assert tvs.parse_free_space(DF.format(free=2000)) == 2000 * 1024
    assert tvs.parse_free_space("df: /nope: No such file or directory") is None


def test_which_tvs_show_what():
    assert tvs.tvs_showing(1) == [1, 4]
    assert tvs.tvs_showing(4) == [4]
    assert tvs.art_tvs(2) == (2,)
    assert tvs.art_tvs(4) == (1, 2, 3, 4)  # the hallway shows every shrine


@pytest.mark.parametrize("text, expected", [
    ('"C:\\Users\\Ms T\\Art\\a.jpg" C:\\Art\\b.jpg', ["C:\\Users\\Ms T\\Art\\a.jpg",
                                                     "C:\\Art\\b.jpg"]),
    ("& 'C:\\My Art\\a.jpg' 'C:\\b.jpg'", ["C:\\My Art\\a.jpg", "C:\\b.jpg"]),  # VS Code
    ("https://example.com/a b.jpg", ["https://example.com/a b.jpg"]),
])
def test_split_sources(text, expected):
    assert tvs.split_sources(text) == expected


def test_host_names_to_try():
    assert tvs._host_names("pi-files.hackerspace.tbl") == [
        "pi-files.hackerspace.tbl", "pi-files", "pi-files.local"]
    assert tvs._host_names("10.0.0.5") == ["10.0.0.5"]


def test_pi_files_is_found_by_its_short_name_too(monkeypatch):
    def getaddrinfo(name, port, **kwargs):
        if name != "pi-files":
            raise socket.gaierror("not found")
        return [("address",)]

    monkeypatch.setattr(tvs.socket, "getaddrinfo", getaddrinfo)
    reached, logged_in = [], []
    monkeypatch.setattr(tvs, "_reach_problem", lambda host, port: reached.append(host))
    monkeypatch.setattr(tvs.login, "connect", lambda settings: logged_in.append(settings.host))
    Screen(monkeypatch)
    with tvs.art_server(TvsSettings()) as server:
        assert server is None  # (not logged in)
    assert reached == ["pi-files"] and logged_in == ["pi-files"]


def test_pi_files_not_found_says_what_was_tried(monkeypatch):
    def not_found(*args, **kwargs):
        raise socket.gaierror("not found")

    monkeypatch.setattr(tvs.socket, "getaddrinfo", not_found)
    monkeypatch.setattr(tvs.socket, "create_connection", not_found)
    screen = Screen(monkeypatch)
    with tvs.art_server(TvsSettings()) as server:
        assert server is None
    assert "couldn't find pi-files.hackerspace.tbl on the network, or as pi-files or " \
        "pi-files.local." in screen.shown("error")


def test_pi_files_without_its_drive_is_explained(monkeypatch):
    screen = Screen(monkeypatch)
    monkeypatch.setattr(tvs, "_find_host", lambda host, port: host)
    monkeypatch.setattr(tvs, "_reach_problem", lambda host, port: None)
    monkeypatch.setattr(tvs.login, "connect", lambda s: FakeFilesConnection(mounted=False))
    with tvs.art_server(TvsSettings()) as server:
        assert server is None
    assert "isn't on the external drive" in screen.shown("error")


# --- Talking to a TV -----------------------------------------------------------------------

def test_videos_are_only_the_playable_files():
    tv, _ = make_tv(files={"ann.lee.a.mp4": b"12", ".ann.lee.z.x.mp4.part": b"",
                           "media.conf": b"", "OLD.MP4": b"1"})
    assert tv.videos() == {"ann.lee.a.mp4": 2, "OLD.MP4": 1}


def test_missing_media_folder():
    tv, _ = make_tv()
    tv.settings = TvsSettings(media_dir="/home/pi/elsewhere")
    with pytest.raises(TvError, match="doesn't exist"):
        tv.videos()


def test_receive_checks_the_size(monkeypatch):
    tv, connection = make_tv()
    tv.receive(io.BytesIO(b"video"), "ann.lee.a.mp4", 5)
    assert connection.files == {"ann.lee.a.mp4": b"video"}
    with pytest.raises(TvError, match="didn't copy"):
        tv.receive(io.BytesIO(b"vid"), "ann.lee.a.mp4", 5)  # cut short


def test_commands():
    tv, connection = make_tv(usb="mmc-SD16G_0x1\nusb-SanDisk_Cruzer-0:0-part1\n")
    assert tv.usb_stick_plugged_in()
    tv.restart_slideshow()
    tv.reboot()
    tv.power(on=True)
    assert connection.commands[1:3] == [("systemctl restart rs", True),
                                        ("shutdown -r now", True)]
    assert "echo on 0 | cec-client -s -d 1" in connection.commands[3][0]


def test_refresh(monkeypatch):
    screen = Screen(monkeypatch)
    tv, connection = make_tv()
    tvs.refresh(tv)
    assert ("systemctl restart rs", True) in connection.commands
    tv, connection = make_tv(usb="usb-Kingston_DT-0:0-part1\n")
    tvs.refresh(tv)  # Raspberry Slideshow would copy what's on the stick instead
    assert not any("restart" in command for command, _ in connection.commands)
    assert "USB stick" in screen.shown("warning")


def test_connect_skips_tvs_that_cant_be_reached(monkeypatch):
    screen = Screen(monkeypatch)
    monkeypatch.setattr(tvs, "_check_reachable",
                        lambda settings, numbers: {2: "couldn't reach pi-tv2."})
    monkeypatch.setattr(tvs.login, "connect", lambda pi: FakeTvConnection())
    assert list(tvs.connect(TvsSettings(), [1, 2, 3])) == [1, 3]
    assert "TV 2: couldn't reach pi-tv2." in screen.shown("error")


def test_set_power_reports_each_tv(monkeypatch):
    screen = Screen(monkeypatch)
    working, broken = FakeTvConnection(), FakeTvConnection()
    broken.run = lambda command, **k: CommandResult(1, "no CEC adapter found")
    connections = {1: working, 2: broken}
    monkeypatch.setattr(tvs, "connect", lambda settings, numbers: {
        n: TvPi(connections[n], n, settings) for n in numbers})
    tvs.set_power(TvsSettings(), [1, 2], on=False)
    assert screen.shown("success") == "TV 1: turned off."
    assert "TV 2: that didn't work (no CEC adapter found)" in screen.shown("error")


# --- Making a shrine's videos --------------------------------------------------------------

def _shrine_on_pi_files(server, tmp_path):
    files = server.connection
    files.add("/mnt/ssd/tv1/ann.lee/ann.lee.a.title.png",
              _picture(tmp_path / "title.png", "white", (1920, 1080)))
    files.add("/mnt/ssd/tv1/ann.lee/ann.lee.z.red.jpg", _picture(tmp_path / "red.jpg", "red"))
    files.add("/mnt/ssd/tv1/ann.lee/ann.lee.z.blue.png", _picture(tmp_path / "blue.png", "blue"))
    files.add("/mnt/ssd/tv1/ann.lee/ann.lee.z.spin.gif", _animation(tmp_path / "spin.gif"))
    return files


def test_make_videos_then_only_what_changed(tmp_path, monkeypatch, ffmpeg_path, pi_files):
    Screen(monkeypatch)
    files = _shrine_on_pi_files(pi_files, tmp_path)
    get = tvs.Ffmpeg(TvsSettings(ffmpeg=str(ffmpeg_path)))
    assert tvs.make_videos(pi_files, ANN, get) == set()
    assert videos_on(pi_files) == ["ann.lee.a.mp4", "ann.lee.z.spin.mp4"]
    slideshow = files.files["/mnt/ssd/tv1/ann.lee/.videos/ann.lee.a.mp4"]
    # Title card first, then the pictures by name; the animation is a video of its own.
    assert_colours(_slide_colours(ffmpeg_path, slideshow, tmp_path, 3), [WHITE, BLUE, RED])

    made = []
    monkeypatch.setattr(shrine, "make_slideshow", lambda ffmpeg_path, stills, output, *a:
                        made.append(output.write_bytes(b"new")) or [])
    monkeypatch.setattr(shrine, "convert", lambda *a: made.append(a))
    assert tvs.make_videos(pi_files, ANN, get) == set()
    assert made == []  # nothing changed, so nothing was made again

    files.remove("/mnt/ssd/tv1/ann.lee/ann.lee.z.spin.gif")
    assert tvs.make_videos(pi_files, ANN, get) == {"ann.lee.z.spin.mp4"}
    assert len(made) == 1  # the slideshow, as its pictures changed


def test_videos_are_converted_and_their_art_removed(tmp_path, monkeypatch, ffmpeg_path,
                                                   pi_files):
    Screen(monkeypatch)
    clip = tmp_path / "clip.mp4"
    ffmpeg.run(ffmpeg_path, ["-f", "lavfi", "-i", "color=c=green:s=64x48:d=1", "-pix_fmt",
                             "yuv420p", str(clip)])
    pi_files.connection.add("/mnt/ssd/tv1/ann.lee/ann.lee.z.clip.mp4", clip.read_bytes())
    get = tvs.Ffmpeg(TvsSettings(ffmpeg=str(ffmpeg_path)))
    tvs.make_videos(pi_files, ANN, get)
    assert videos_on(pi_files) == ["ann.lee.z.clip.mp4"]  # no pictures, so no slideshow
    pi_files.connection.remove("/mnt/ssd/tv1/ann.lee/ann.lee.z.clip.mp4")
    assert tvs.make_videos(pi_files, ANN, get) == {"ann.lee.z.clip.mp4"}
    assert videos_on(pi_files) == []


def test_art_that_cant_be_used_is_left_out(tmp_path, monkeypatch, ffmpeg_path, pi_files):
    screen = Screen(monkeypatch)
    pi_files.connection.add("/mnt/ssd/tv1/ann.lee/ann.lee.z.red.jpg",
                            _picture(tmp_path / "red.jpg", "red"))
    pi_files.connection.add("/mnt/ssd/tv1/ann.lee/ann.lee.z.notes.txt", b"not art")
    tvs.make_videos(pi_files, ANN, tvs.Ffmpeg(TvsSettings(ffmpeg=str(ffmpeg_path))))
    assert videos_on(pi_files) == ["ann.lee.a.mp4"]
    assert "left out ann.lee.z.notes.txt" in screen.shown("warning")


# --- Keeping the TVs the same as pi-files --------------------------------------------------

def _rendered(server, path, data):
    server.connection.add(path, data)
    folder = posixpath.dirname(path)
    server.connection.add(posixpath.join(folder, ".made.json"),
                          json.dumps({"pictures": [], "stills": 0}).encode())


def test_sync_copies_whats_missing_and_backs_up_what_isnt_on_pi_files(monkeypatch, pi_files):
    screen = Screen(monkeypatch, chooses=["backup"])
    tv, connection = make_tv(files={"ann.lee.a.mp4": b"old", "same.mp4": b"same",
                                    "stray.mp4": b"stray", "deleted.mp4": b"gone"})
    pi_files.connection.add("/v/ann.lee.a.mp4", b"new slideshow")
    pi_files.connection.add("/v/same.mp4", b"same")
    pi_files.connection.add("/v/bo.ng.a.mp4", b"bo")
    wanted = {"ann.lee.a.mp4": ("/v/ann.lee.a.mp4", 13), "same.mp4": ("/v/same.mp4", 4),
              "bo.ng.a.mp4": ("/v/bo.ng.a.mp4", 2)}
    tvs._sync(tv, pi_files, wanted, {"deleted.mp4"}, {})
    assert connection.files == {"ann.lee.a.mp4": b"new slideshow", "same.mp4": b"same",
                                "bo.ng.a.mp4": b"bo"}
    backup = pi_files.connection.list_folder("/mnt/ssd/backup")[1]
    assert len(backup) == 1 and next(iter(backup)).endswith("-tv1-stray.mp4")  # only it
    assert "  stray.mp4 (" in screen.shown("info")  # listed, unlike deleted.mp4
    assert "  deleted.mp4 (" not in screen.shown("info")
    assert ("systemctl restart rs", True) in connection.commands
    screen.finished()


def test_sync_leaves_unknown_videos_if_asked_to(monkeypatch, pi_files):
    Screen(monkeypatch, chooses=["leave"])
    tv, connection = make_tv(files={"stray.mp4": b"stray"})
    tvs._sync(tv, pi_files, {}, set(), {})
    assert connection.files == {"stray.mp4": b"stray"}


def test_sync_says_when_theres_nothing_to_do(monkeypatch, pi_files):
    screen = Screen(monkeypatch)
    tv, connection = make_tv(files={"a.mp4": b"a"})
    tvs._sync(tv, pi_files, {"a.mp4": ("/v/a.mp4", 1)}, set(), {})
    assert screen.shown("success") == "TV 1: up to date (1 videos)."
    assert not any("restart" in command for command, _ in connection.commands)


def test_sync_checks_theres_room(monkeypatch, pi_files):
    Screen(monkeypatch)
    tv, connection = make_tv(room=tvs.SPARE_SPACE + 5)
    pi_files.connection.add("/v/big.mp4", b"x" * 10)
    with pytest.raises(TvError, match="needs"):
        tvs._sync(tv, pi_files, {"big.mp4": ("/v/big.mp4", 10)}, set(), {})
    assert connection.files == {}


def test_update_tvs_puts_every_shrine_on_the_hallway(monkeypatch, pi_files, the_tvs):
    screen = Screen(monkeypatch)
    _rendered(pi_files, "/mnt/ssd/tv1/ann.lee/.videos/ann.lee.a.mp4", b"ann")
    _rendered(pi_files, "/mnt/ssd/tv3/skills/.videos/skills.a.mp4", b"skills")
    pi_files.connection.add("/mnt/ssd/tv1/ann.lee.z.clip.mp4", b"hightower")  # left loose
    monkeypatch.setattr(tvs, "make_videos", lambda server, art, get: set())
    tvs.update_tvs(TvsSettings(), pi_files, [1, 4])
    assert the_tvs[1].files == {"ann.lee.a.mp4": b"ann"}
    assert the_tvs[4].files == {"ann.lee.a.mp4": b"ann", "skills.a.mp4": b"skills"}
    assert the_tvs[3].files == {}  # not asked for
    assert "Moved tv1/ann.lee.z.clip.mp4 into tv1/ann.lee." in screen.shown("info")


def test_a_tv_that_fails_doesnt_stop_the_others(monkeypatch, pi_files, the_tvs):
    screen = Screen(monkeypatch)
    _rendered(pi_files, "/mnt/ssd/tv1/ann.lee/.videos/ann.lee.a.mp4", b"ann")
    monkeypatch.setattr(tvs, "make_videos", lambda server, art, get: set())

    def broken(path):
        raise FileNotFoundError(path)
    the_tvs[1].file_sizes = broken
    tvs.update_tvs(TvsSettings(), pi_files, [1, 4])
    assert "TV 1: that didn't work" in screen.shown("error")
    assert the_tvs[4].files == {"ann.lee.a.mp4": b"ann"}


# --- The menu items ------------------------------------------------------------------------

def test_make_a_shrine(tmp_path, monkeypatch, ffmpeg_path, pi_files, the_tvs):
    art = tmp_path / "Ann's art"
    art.mkdir()
    _picture(art / "red.jpg", "red")
    _animation(art / "spin.gif")
    screen = Screen(monkeypatch, chooses=["student", 1, "Digital Art"],
                    asks=["Ann.Lee", "", "2027", f'"{art}"', ""])
    tvs.make_shrine(TvsSettings(ffmpeg=str(ffmpeg_path)))
    screen.finished()
    assert sorted(pi_files.connection.list_folder("/mnt/ssd/tv1/ann.lee")[1]) == [
        "ann.lee.a.title.png", "ann.lee.z.red.jpg", "ann.lee.z.spin.gif"]
    assert videos_on(pi_files) == ["ann.lee.a.mp4", "ann.lee.z.spin.mp4"]
    for number in (1, 4):  # its TV, and the hallway
        assert sorted(the_tvs[number].files) == ["ann.lee.a.mp4", "ann.lee.z.spin.mp4"]
    title, picture = _slide_colours(ffmpeg_path, the_tvs[1].files["ann.lee.a.mp4"], tmp_path, 2)
    assert_colours([picture], [RED])  # after the title card
    assert "Which TV is it for? (TV 1, going by its name.)" in screen.questions


def test_making_a_shrine_that_exists_offers_to_add_to_it(monkeypatch, pi_files):
    pi_files.connection.add("/mnt/ssd/tv2/ann.lee/a.jpg")
    added = []
    monkeypatch.setattr(tvs, "_add_to", lambda settings, server, art, get: added.append(art))
    monkeypatch.setattr(tvs.Ffmpeg, "__call__", lambda self: Path("ffmpeg"))
    screen = Screen(monkeypatch, chooses=["student"], asks=["ann.lee"], confirms=[True])
    tvs.make_shrine(TvsSettings())
    screen.finished()
    assert added == [ArtFolder(2, "/mnt/ssd/tv2/ann.lee", "ann.lee")]


def test_add_art_skips_whats_there_and_replaces_same_names(tmp_path, monkeypatch, ffmpeg_path,
                                                          pi_files, the_tvs):
    files = pi_files.connection
    files.add("/mnt/ssd/tv1/ann.lee/ann.lee.z.red.jpg", _picture(tmp_path / "red.jpg", "red"))
    files.add("/mnt/ssd/tv1/ann.lee/ann.lee.z.sun.png", b"hightower's copy")
    new = tmp_path / "new"
    new.mkdir()
    _picture(new / "red.jpg", "red")  # the same file again
    _picture(new / "sun.png", "blue")  # the same name, a new version
    _picture(new / "moon.png", "white")
    updated = []
    monkeypatch.setattr(tvs, "update_shrine", lambda settings, server, art, **k:
                        updated.append(art))
    screen = Screen(monkeypatch, asks=["ann", str(new), ""], chooses=[ANN], confirms=[True])
    tvs.add_art(TvsSettings(ffmpeg=str(ffmpeg_path)))
    screen.finished()
    assert sorted(files.list_folder("/mnt/ssd/tv1/ann.lee")[1]) == [
        "ann.lee.z.moon.png", "ann.lee.z.red.jpg", "ann.lee.z.sun.png"]
    assert files.files["/mnt/ssd/tv1/ann.lee/ann.lee.z.sun.png"] == (new / "sun.png").read_bytes()
    backup = files.list_folder("/mnt/ssd/backup")[1]
    assert [f.endswith("-ann.lee.z.sun.png") for f in backup] == [True]
    assert "Already on pi-files: red.jpg" in screen.shown("info")
    assert updated == [ANN]


def test_add_art_can_keep_both(tmp_path, monkeypatch, ffmpeg_path, pi_files):
    pi_files.connection.add("/mnt/ssd/tv1/ann.lee/ann.lee.z.sun.png", b"old")
    new = tmp_path / "new"
    new.mkdir()
    _picture(new / "sun.png", "blue")
    monkeypatch.setattr(tvs, "update_shrine", lambda *a, **k: None)
    Screen(monkeypatch, asks=["ann", str(new), ""], chooses=[ANN], confirms=[False])
    tvs.add_art(TvsSettings(ffmpeg=str(ffmpeg_path)))
    assert sorted(pi_files.connection.list_folder("/mnt/ssd/tv1/ann.lee")[1]) == [
        "ann.lee.z.sun-2.png", "ann.lee.z.sun.png"]


def test_see_a_piece_then_delete_it(tmp_path, monkeypatch, pi_files):
    files = _shrine_on_pi_files(pi_files, tmp_path)
    opened = []
    monkeypatch.setattr(tvs, "open_file", lambda path: opened.append(path.read_bytes()))
    updated = []
    monkeypatch.setattr(tvs, "update_shrine", lambda settings, server, art, **k:
                        updated.append(art))
    red = "/mnt/ssd/tv1/ann.lee/ann.lee.z.red.jpg"
    blue = "/mnt/ssd/tv1/ann.lee/ann.lee.z.blue.png"
    screen = Screen(monkeypatch, asks=["ann"], chooses=["search", ANN, blue, red, None],
                    confirms=[False, True])
    tvs.view_art(TvsSettings())
    screen.finished()
    assert opened == [(tmp_path / "blue.png").read_bytes(), (tmp_path / "red.jpg").read_bytes()]
    assert red not in files.files and blue in files.files
    assert [f.endswith("-ann.lee.z.red.jpg") for f in files.list_folder("/mnt/ssd/backup")[1]] \
        == [True]
    assert updated == [ANN]  # the slideshow's made again, and its TVs updated


def test_looking_without_deleting_changes_nothing(tmp_path, monkeypatch, pi_files):
    files = _shrine_on_pi_files(pi_files, tmp_path)
    before = dict(files.files)
    monkeypatch.setattr(tvs, "open_file", lambda path: None)
    monkeypatch.setattr(tvs, "update_shrine", lambda *a, **k: pytest.fail("updated the TVs"))
    Screen(monkeypatch, asks=["ann"], chooses=["search", ANN,
                                               "/mnt/ssd/tv1/ann.lee/ann.lee.z.red.jpg", None],
           confirms=[False])
    tvs.view_art(TvsSettings())
    assert files.files == before


def test_list_a_tvs_shrines_then_see_one(tmp_path, monkeypatch, pi_files):
    _shrine_on_pi_files(pi_files, tmp_path)
    pi_files.connection.add("/mnt/ssd/tv1/bo.ng/bo.ng.z.cat.png")
    pi_files.connection.add("/mnt/ssd/tv2/cy.mo/cy.mo.z.dog.png")
    opened = []
    monkeypatch.setattr(tvs, "open_file", lambda path: opened.append(path))
    monkeypatch.setattr(tvs, "update_shrine", lambda *a, **k: pytest.fail("updated the TVs"))
    seen = {}
    real_choose = Screen._choose

    def choose(self, choices, question=""):
        seen[question] = [c[0] for c in choices]
        return real_choose(self, choices, question)
    monkeypatch.setattr(Screen, "_choose", choose)
    screen = Screen(monkeypatch, chooses=[4, 1, ANN, "/mnt/ssd/tv1/ann.lee/ann.lee.z.red.jpg",
                                          None], confirms=[False])
    tvs.view_art(TvsSettings())
    screen.finished()
    assert seen["Find the shrine:"] == [
        "Search by name", "List TV 1's shrines (original art, last names A-L)",
        "List TV 2's shrines (original art, last names M-Z)",
        "List TV 3's shrines (demos, tutorials and Skills Canada)",
        "List TV 4's own shrines (the hallway also shows all of TV 1-3's)"]
    assert "TV 4 has no shrines of its own on pi-files." in screen.shown("info")
    assert seen["TV 1's 2 shrines. Which one?"] == ["ann.lee", "bo.ng", "Search or list again"]
    assert len(opened) == 1


@pytest.mark.parametrize("how, kept", [("backup", True), ("delete", False)])
def test_remove_a_shrine(monkeypatch, pi_files, the_tvs, how, kept):
    _rendered(pi_files, "/mnt/ssd/tv1/ann.lee/.videos/ann.lee.a.mp4", b"ann")
    _rendered(pi_files, "/mnt/ssd/tv1/bo.ng/.videos/bo.ng.a.mp4", b"bo")
    pi_files.connection.add("/mnt/ssd/tv1/ann.lee/ann.lee.z.red.jpg")
    for number in (1, 4):
        the_tvs[number].files = {"ann.lee.a.mp4": b"ann", "bo.ng.a.mp4": b"bo"}
    monkeypatch.setattr(tvs, "make_videos", lambda server, art, get: set())
    screen = Screen(monkeypatch, asks=["ann.lee"], chooses=[ANN, how],
                    confirms=[True] if how == "delete" else [])
    tvs.remove_shrine(TvsSettings())
    screen.finished()  # its videos came off the TVs without asking about backing them up
    for number in (1, 4):
        assert the_tvs[number].files == {"bo.ng.a.mp4": b"bo"}
    assert not pi_files.connection.exists("/mnt/ssd/tv1/ann.lee")
    backup = pi_files.connection.list_folder("/mnt/ssd/backup")[0] if kept else []
    assert [f.endswith("-ann.lee") for f in backup] == ([True] if kept else [])
    assert pi_files.connection.exists("/mnt/ssd/backup") is kept


def _made_shrine(pi_files, the_tvs):
    """ann.lee's shrine on TV 1, with its videos made, and on TV 1 and the hallway."""
    files = pi_files.connection
    art = {"ann.lee.a.title.png": b"title", "ann.lee.z.red.jpg": b"red",
           "ann.lee.z.clip.mov": b"clip", "notes.mp4": b"notes"}
    for file_name, data in art.items():
        files.add(f"/mnt/ssd/tv1/ann.lee/{file_name}", data)
    videos = {"ann.lee.a.mp4": b"slideshow", "ann.lee.z.clip.mp4": b"clip video",
              "ann.lee.z.notes.mp4": b"notes video"}
    for file_name, data in videos.items():
        files.add(f"/mnt/ssd/tv1/ann.lee/.videos/{file_name}", data)
    files.add("/mnt/ssd/tv1/ann.lee/.videos/.made.json", json.dumps({
        "pictures": [["ann.lee.a.title.png", 5], ["ann.lee.z.red.jpg", 3]], "stills": 2,
        "animated": [], "sources": {"ann.lee.z.clip.mp4": ["ann.lee.z.clip.mov", 4],
                                    "ann.lee.z.notes.mp4": ["notes.mp4", 5]},
        "unusable": {}}).encode())
    for number in (1, 4):
        the_tvs[number].files = dict(videos)
    return files


def test_rename_a_shrine_without_making_its_videos_again(monkeypatch, pi_files, the_tvs):
    files = _made_shrine(pi_files, the_tvs)
    monkeypatch.setattr(tvs, "_get_ffmpeg", lambda settings: pytest.fail("made videos again"))
    screen = Screen(monkeypatch, asks=["ann", "Ann.Smith"], chooses=["search", ANN, 1],
                    confirms=[False])
    tvs.rename_shrine(TvsSettings())
    screen.finished()
    assert not files.exists("/mnt/ssd/tv1/ann.lee")
    assert sorted(files.list_folder("/mnt/ssd/tv1/ann.smith")[1]) == [
        "ann.smith.a.title.png", "ann.smith.z.clip.mov", "ann.smith.z.red.jpg", "notes.mp4"]
    renamed = {"ann.smith.a.mp4": b"slideshow", "ann.smith.z.clip.mp4": b"clip video",
               "ann.smith.z.notes.mp4": b"notes video"}
    for number in (1, 4):
        assert the_tvs[number].files == renamed
    assert "Which TV is it for? (It's on TV 1 now; going by its name, TV 2.)" in \
        screen.questions  # Enter keeps it where it is
    assert "Its title card has ann.lee's name on it. Make a new one?" in screen.questions


def test_rename_a_shrine_with_a_new_title_card(monkeypatch, pi_files, the_tvs, ffmpeg_path):
    files = _made_shrine(pi_files, the_tvs)
    screen = Screen(monkeypatch, asks=["ann", "bo.zed", "", "2027"],
                    chooses=["search", ANN, 1, "Digital Art"], confirms=[True])
    tvs.rename_shrine(TvsSettings(ffmpeg=str(ffmpeg_path)))
    screen.finished()
    assert files.files["/mnt/ssd/tv1/bo.zed/bo.zed.a.title.png"] != b"title"  # a new one
    assert "bo.zed.a.mp4" in the_tvs[1].files and "ann.lee.a.mp4" not in the_tvs[1].files
    assert the_tvs[1].files["bo.zed.a.mp4"] != b"slideshow"  # made again, with the new card


def test_move_a_shrine_to_another_tv(monkeypatch, pi_files, the_tvs):
    files = _made_shrine(pi_files, the_tvs)
    monkeypatch.setattr(tvs, "_get_ffmpeg", lambda settings: pytest.fail("made videos again"))
    screen = Screen(monkeypatch, asks=["ann", "ann.lee"], chooses=["search", ANN, 2])
    tvs.rename_shrine(TvsSettings())
    screen.finished()
    assert files.exists("/mnt/ssd/tv2/ann.lee/ann.lee.a.title.png")
    assert the_tvs[1].files == {}  # taken off its old TV
    assert sorted(the_tvs[2].files) == sorted(the_tvs[4].files) == [
        "ann.lee.a.mp4", "ann.lee.z.clip.mp4", "ann.lee.z.notes.mp4"]


def test_rename_to_a_name_thats_taken(monkeypatch, pi_files, the_tvs):
    files = _made_shrine(pi_files, the_tvs)
    files.add("/mnt/ssd/tv2/bo.zed/bo.zed.z.cat.png")
    before = dict(files.files)
    screen = Screen(monkeypatch, asks=["ann", "bo.zed"], chooses=["search", ANN])
    tvs.rename_shrine(TvsSettings())
    screen.finished()
    assert "pi-files already has a shrine for bo.zed (TV 2)" in screen.shown("error")
    assert files.files == before


def test_update_the_tvs(monkeypatch, pi_files):
    updated = []
    monkeypatch.setattr(tvs, "update_tvs", lambda settings, server, numbers:
                        updated.append(numbers))
    Screen(monkeypatch, picks=[[2, 3]])
    tvs.update(TvsSettings())
    assert updated == [[2, 3]]


def test_the_menu():
    assert [title for title, _ in tvs.ACTIONS] == [
        "Make a shrine (a student's art, or a collection)", "Add art to a shrine",
        "List, View, and/or Delete a Shrine's Art",
        "Rename a shrine (e.g. to a different username)", "Remove a shrine",
        "Update the TVs from pi-files", "Turn TVs on or off",
        "Restart a TV's slideshow, or reboot it",
        "TV Art Shrines documentation"]


def test_problems_are_reported_not_crashes(monkeypatch):
    screen = Screen(monkeypatch)

    def broken(settings):
        raise TvError("TV 1 is full.")
    tvs._do(broken, TvsSettings())

    def cancelled(settings):
        raise KeyboardInterrupt
    tvs._do(cancelled, TvsSettings())
    assert "TV 1 is full." in screen.shown("error")
    assert "Cancelled." in screen.shown("warning")


# --- Grade 9 mode --------------------------------------------------------------------------

@pytest.mark.parametrize("engage, tvs_on, volume", [(True, False, 0), (False, True, 100)])
def test_grade_9_mode(monkeypatch, engage, tvs_on, volume):
    screen = Screen(monkeypatch)
    powered = []
    monkeypatch.setattr(grade9.tvs, "set_power",
                        lambda settings, numbers, on: powered.append((numbers, on)))
    themes_pi = FakeTvConnection()
    monkeypatch.setattr(grade9.login, "connect", lambda settings: themes_pi)
    grade9._engage(Config(), engage)
    assert powered == [((1, 2, 3), tvs_on)]
    assert themes_pi.commands == [(f"amixer sset Headphone,0 {volume}%", False)]
    assert ("muted" if engage else "unmuted") in screen.shown("success")


# --- From the review -----------------------------------------------------------------------

def test_a_replaced_video_is_made_again(tmp_path, monkeypatch, pi_files):
    Screen(monkeypatch)
    pi_files.connection.add("/mnt/ssd/tv1/ann.lee/ann.lee.z.clip.mp4", b"first")
    converted = []

    def convert(server, art, path, video, work, get, media=None):
        converted.append(server.connection.files[path])
        server.connection.add(posixpath.join(art.videos, video), b"video")
    monkeypatch.setattr(tvs, "_convert", convert)
    get = tvs.Ffmpeg(TvsSettings())
    tvs.make_videos(pi_files, ANN, get)
    tvs.make_videos(pi_files, ANN, get)
    assert converted == [b"first"]  # only once
    pi_files.connection.add("/mnt/ssd/tv1/ann.lee/ann.lee.z.clip.mp4", b"the new one")
    tvs.make_videos(pi_files, ANN, get)
    assert converted == [b"first", b"the new one"]


def test_art_that_cant_be_used_isnt_tried_again(tmp_path, monkeypatch, ffmpeg_path, pi_files):
    screen = Screen(monkeypatch)
    pi_files.connection.add("/mnt/ssd/tv1/ann.lee/ann.lee.z.notes.txt", b"not art")
    get = tvs.Ffmpeg(TvsSettings(ffmpeg=str(ffmpeg_path)))
    tvs.make_videos(pi_files, ANN, get)
    got = []
    monkeypatch.setattr(pi_files, "download", lambda *a, **k: got.append(a))
    tvs.make_videos(pi_files, ANN, get)
    assert got == []  # not downloaded again
    assert screen.shown("warning").count("left out ann.lee.z.notes.txt") == 2  # still said


def test_pictures_get_a_picture_type_in_their_name(tmp_path):
    picture = tmp_path / "photo.jpg_large"
    Image.new("RGB", (8, 8), "red").save(picture, "PNG")
    assert tvs._label(shrine.Media(picture, shrine.PICTURE, "photo.jpg_large")) == "photo.png"
    assert tvs._label(shrine.Media(picture, shrine.PICTURE, "art/sun.jpg")) == "art/sun.jpg"
    assert tvs._label(shrine.Media(picture, shrine.VIDEO, "clip")) == "clip"


def test_a_replace_that_fails_puts_the_old_piece_back(tmp_path, monkeypatch, ffmpeg_path,
                                                      pi_files):
    pi_files.connection.add("/mnt/ssd/tv1/ann.lee/ann.lee.z.sun.png", b"old")
    new = tmp_path / "new"
    new.mkdir()
    _picture(new / "sun.png", "blue")

    def unplugged(local, path, progress=None):
        raise tvs.ArtError("Couldn't read sun.png on this computer.")
    monkeypatch.setattr(pi_files, "upload", unplugged)
    Screen(monkeypatch, asks=["ann", str(new), ""], chooses=[ANN], confirms=[True])
    with pytest.raises(tvs.ArtError):
        tvs.add_art(TvsSettings(ffmpeg=str(ffmpeg_path)))
    assert pi_files.connection.files["/mnt/ssd/tv1/ann.lee/ann.lee.z.sun.png"] == b"old"


def test_videos_deleted_while_a_tv_is_off_come_off_it_later(monkeypatch, pi_files, the_tvs):
    _rendered(pi_files, "/mnt/ssd/tv1/ann.lee/.videos/ann.lee.a.mp4", b"ann")
    _rendered(pi_files, "/mnt/ssd/tv1/bo.ng/.videos/bo.ng.a.mp4", b"bo")
    monkeypatch.setattr(tvs, "make_videos", lambda server, art, get: set())
    the_tvs[4].files = {"ann.lee.a.mp4": b"ann", "bo.ng.a.mp4": b"bo"}
    real_connect = tvs.connect
    monkeypatch.setattr(tvs, "connect", lambda settings, numbers: {})  # the TVs are off
    Screen(monkeypatch, asks=["bo.ng"], chooses=[ArtFolder(1, "/mnt/ssd/tv1/bo.ng", "bo.ng"),
                                                "backup"])
    tvs.remove_shrine(TvsSettings())
    assert pi_files.deleted() == {"bo.ng.a.mp4"}
    monkeypatch.setattr(tvs, "connect", real_connect)
    screen = Screen(monkeypatch)  # no question about backing it up
    tvs.update_tvs(TvsSettings(), pi_files, [4])
    screen.finished()
    assert the_tvs[4].files == {"ann.lee.a.mp4": b"ann"}


def test_the_same_video_in_two_tv_folders_is_shown_once(monkeypatch, pi_files, the_tvs):
    screen = Screen(monkeypatch)
    _rendered(pi_files, "/mnt/ssd/tv3/skills/.videos/skills.a.mp4", b"tv3's")
    _rendered(pi_files, "/mnt/ssd/tv4/skills/.videos/skills.a.mp4", b"tv4's, a different one")
    monkeypatch.setattr(tvs, "make_videos", lambda server, art, get: set())
    tvs.update_tvs(TvsSettings(), pi_files, [4])
    assert the_tvs[4].files == {"skills.a.mp4": b"tv3's"}
    assert "has the same name as /mnt/ssd/tv3/skills/.videos/skills.a.mp4" in \
        screen.shown("warning")


# --- Tweaks from trying it on the real Pis -------------------------------------------------

def _clip(ffmpeg_path, path, size="64x48", rate=25, codec="libx264", sound=False):
    args = ["-f", "lavfi", "-i", f"color=c=green:s={size}:d=1:r={rate}"]
    if sound:
        args += ["-f", "lavfi", "-i", "sine=d=1", "-shortest", "-c:a", "aac"]
    ffmpeg.run(ffmpeg_path, [*args, "-pix_fmt", "yuv420p", "-c:v", codec, str(path)])
    return path.read_bytes()


def test_a_video_already_in_the_tvs_format_is_used_as_it_is(tmp_path, monkeypatch, ffmpeg_path,
                                                           pi_files):
    screen = Screen(monkeypatch)
    ready = _clip(ffmpeg_path, tmp_path / "ready.mp4")
    pi_files.connection.add("/mnt/ssd/tv1/ann.lee/ann.lee.z.Stories_of_Ribbecca.mp4", ready)
    monkeypatch.setattr(shrine, "convert", lambda *a: pytest.fail("converted it"))
    tvs.make_videos(pi_files, ANN, tvs.Ffmpeg(TvsSettings(ffmpeg=str(ffmpeg_path))))
    # The same file, under the same name (capitals and all), as hightower's TVs have it.
    assert pi_files.connection.files[
        "/mnt/ssd/tv1/ann.lee/.videos/ann.lee.z.Stories_of_Ribbecca.mp4"] == ready
    assert "is already in the TVs' format, so it's used as it is" in screen.shown("info")


def test_only_the_sound_is_taken_out_of_an_otherwise_ready_video(tmp_path, monkeypatch,
                                                                 ffmpeg_path, pi_files):
    Screen(monkeypatch)
    pi_files.connection.add("/mnt/ssd/tv1/ann.lee/ann.lee.z.talk.mp4",
                            _clip(ffmpeg_path, tmp_path / "talk.mp4", sound=True))
    monkeypatch.setattr(shrine, "convert", lambda *a: pytest.fail("converted it"))
    tvs.make_videos(pi_files, ANN, tvs.Ffmpeg(TvsSettings(ffmpeg=str(ffmpeg_path))))
    made = tmp_path / "made.mp4"
    made.write_bytes(pi_files.connection.files["/mnt/ssd/tv1/ann.lee/.videos/ann.lee.z.talk.mp4"])
    assert not ffmpeg.probe(ffmpeg_path, made).has_sound


def test_converting_says_why(tmp_path, monkeypatch, ffmpeg_path, pi_files):
    screen = Screen(monkeypatch)
    pi_files.connection.add("/mnt/ssd/tv1/ann.lee/ann.lee.z.fast.mov",
                            _clip(ffmpeg_path, tmp_path / "fast.mov", rate=60))
    tvs.make_videos(pi_files, ANN, tvs.Ffmpeg(TvsSettings(ffmpeg=str(ffmpeg_path))))
    assert "Converting ann.lee.z.fast.mov for the TVs: it's a .mov file, it's 60 fps, and the " \
        "TVs need 1080p H.264 without sound." in screen.shown("info")
    assert videos_on(pi_files) == ["ann.lee.z.fast.mp4"]


def test_the_slideshow_message_explains_animations(tmp_path, monkeypatch, ffmpeg_path,
                                                   pi_files):
    screen = Screen(monkeypatch)
    _shrine_on_pi_files(pi_files, tmp_path)
    tvs.make_videos(pi_files, ANN, tvs.Ffmpeg(TvsSettings(ffmpeg=str(ffmpeg_path))))
    assert "Making ann.lee's slideshow (title card and 2 pictures)..." in screen.shown("info")
    assert "ann.lee.z.spin.gif is an animation, so it plays as its own video." in \
        screen.shown("info")


def test_jpe_pictures_go_in_the_slideshow(tmp_path, monkeypatch, ffmpeg_path, pi_files):
    Screen(monkeypatch)
    pi_files.connection.add("/mnt/ssd/tv1/ann.lee/9923251.z.WonkyDonkey.jpe",
                            _picture(tmp_path / "w.jpg", "red"))
    tvs.make_videos(pi_files, ANN, tvs.Ffmpeg(TvsSettings(ffmpeg=str(ffmpeg_path))))
    assert videos_on(pi_files) == ["ann.lee.a.mp4"]


def test_changing_one_shrine_only_updates_it(monkeypatch, pi_files, the_tvs):
    screen = Screen(monkeypatch)
    _rendered(pi_files, "/mnt/ssd/tv1/ann.lee/.videos/ann.lee.a.mp4", b"new slideshow")
    pi_files.connection.add("/mnt/ssd/tv1/bo.ng.z.loose.mp4", b"hightower's")  # not tidied
    monkeypatch.setattr(tvs, "make_videos", lambda server, art, get: {"ann.lee.z.old.mp4"})
    for number in (1, 4):
        the_tvs[number].files = {"ann.lee.a.mp4": b"old", "ann.lee.z.old.mp4": b"gone",
                                 "someone.else.a.mp4": b"theirs"}
    tvs.update_shrine(TvsSettings(), pi_files, ANN)
    screen.finished()  # no questions about other shrines' videos
    for number in (1, 4):
        assert the_tvs[number].files == {"ann.lee.a.mp4": b"new slideshow",
                                         "someone.else.a.mp4": b"theirs"}
    assert the_tvs[2].files == {} and the_tvs[3].files == {}
    assert "/mnt/ssd/tv1/bo.ng.z.loose.mp4" in pi_files.connection.files
    assert "Updating TV 1 and TV 4 (the hallway):" in screen.shown("info")
    assert "isn't in a shrine's folder" not in screen.shown()


def test_loose_videos_in_a_tv_folder_play_as_they_are(monkeypatch, pi_files, the_tvs):
    Screen(monkeypatch)
    pi_files.connection.add("/mnt/ssd/tv3/0_RubeGoldberg-Webb.mp4", b"demo")
    pi_files.connection.add("/mnt/ssd/tv4/0_RubeGoldberg-Webb.mp4", b"demo")  # a copy
    pi_files.connection.add("/mnt/ssd/tv3/organize_images.py", b"hightower's script")
    tvs.update_tvs(TvsSettings(), pi_files, [3, 4])
    assert the_tvs[3].files == {"0_RubeGoldberg-Webb.mp4": b"demo"}
    assert the_tvs[4].files == {"0_RubeGoldberg-Webb.mp4": b"demo"}


def test_videos_only_on_a_tv_can_be_added_to_pi_files(monkeypatch, pi_files):
    screen = Screen(monkeypatch, chooses=["add"])
    tv, connection = make_tv(files={"ann.lee.z.Sonic.mp4": b"sonic", "new.kid.a.mp4": b"kid",
                                    "robot_demo.mp4": b"robot"})
    tvs._sync(tv, pi_files, {}, set(), {"ann.lee": ANN})
    files = pi_files.connection.files
    assert files["/mnt/ssd/tv1/ann.lee/ann.lee.z.Sonic.mp4"] == b"sonic"  # its shrine
    assert files["/mnt/ssd/tv1/new.kid/new.kid.a.mp4"] == b"kid"  # a new one, on this TV
    assert files["/mnt/ssd/tv1/robot_demo.mp4"] == b"robot"  # nobody's: loose
    assert set(connection.files) == {"ann.lee.z.Sonic.mp4", "new.kid.a.mp4", "robot_demo.mp4"}
    assert "TV 1: added ann.lee.z.Sonic.mp4 to /mnt/ssd/tv1/ann.lee on pi-files." in \
        screen.shown("info")


def test_a_dropped_connection_is_tried_again(monkeypatch, the_tvs):
    screen = Screen(monkeypatch)
    tries = []

    def flaky(tv):
        tries.append(tv)
        if len(tries) == 1:
            raise ConnectionResetError(10054, "An existing connection was forcibly closed")
    assert tvs._on_tv(TvsSettings(), 1, flaky)
    assert len(tries) == 2
    assert "Lost the connection to TV 1; trying again..." in screen.shown("warning")
