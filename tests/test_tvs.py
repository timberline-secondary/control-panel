import contextlib
import posixpath
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from control_panel import ui
from control_panel.config import TvsSettings
from control_panel.panels import grade9, tvs
from control_panel.panels.tvs import TvError, TvPi
from control_panel.ssh import CommandResult

DF = ("Filesystem     1024-blocks    Used Available Capacity Mounted on\n"
      "/dev/root         30000000 1000000  {free}       4% /\n")


class FakeTvConnection:
    """Pretends to be a TV Pi. files: {name: contents} in its media folder."""

    def __init__(self, files=(), free_kb=10_000_000, usb="", restart_status=0):
        self.host = "pi-tv1.hackerspace.tbl"
        self.files = dict(files)
        self.free_kb = free_kb
        self.usb = usb
        self.restart_status = restart_status
        self.commands = []
        self.closed = False
        self.sftp = SimpleNamespace(stat=lambda path: SimpleNamespace(
            st_size=len(self.files[posixpath.basename(path)])))

    def run(self, command, *, sudo=False, timeout=60):
        self.commands.append((command, sudo))
        if command.startswith("df "):
            return CommandResult(0, DF.format(free=self.free_kb))
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
        self.files[posixpath.basename(path)] = data
        if progress:
            progress(len(data), len(data))

    def download(self, path, local, progress=None):
        Path(local).write_bytes(self.files[posixpath.basename(path)])

    def remove(self, path):
        del self.files[posixpath.basename(path)]

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
    """Scripted answers for the prompts, and a record of what was shown."""

    DEFAULT = object()  # for choose_many: keep the boxes that were ticked to start with

    def __init__(self, monkeypatch, asks=(), chooses=(), confirms=(), ticks=()):
        self.messages = []
        self.asks, self.chooses = iter(asks), iter(chooses)
        self.confirms, self.ticks = iter(confirms), iter(ticks)
        self.ticked_at_first = []
        for kind in ["info", "success", "warning", "error"]:
            monkeypatch.setattr(ui, kind, lambda text, kind=kind: self.messages.append(
                (kind, text)))
        monkeypatch.setattr(ui, "ask", lambda prompt, **k: next(self.asks))
        monkeypatch.setattr(ui, "choose", lambda prompt, options: next(self.chooses))
        monkeypatch.setattr(ui, "confirm", lambda prompt, **k: next(self.confirms))
        monkeypatch.setattr(ui, "choose_many", self._choose_many)
        monkeypatch.setattr(ui, "progress", no_progress)

    def _choose_many(self, prompt, options, **kwargs):
        ticked = [option.value for option in options if option.checked]
        self.ticked_at_first.append(ticked)
        answer = next(self.ticks)
        return ticked if answer is self.DEFAULT else answer

    def finished(self):
        for left in (self.asks, self.chooses, self.confirms, self.ticks):
            assert next(left, "done") == "done", "not every scripted answer was asked for"

    def shown(self, kind=None):
        return "\n".join(text for k, text in self.messages if kind in (None, k))


# --- Helpers -------------------------------------------------------------------------------

def test_parse_free_space():
    assert tvs.parse_free_space(DF.format(free=2000)) == 2000 * 1024
    assert tvs.parse_free_space("df: /nope: No such file or directory") is None


def test_shrine_names():
    assert tvs.shrine_names(["ann.lee.a.mp4", "ann.lee.z.cat.mp4", "skills-2026.a.mp4",
                             "intro.mp4"]) == {"ann.lee", "skills-2026"}


@pytest.mark.parametrize("name, expected", [
    ("tyler.couture", [1, 4]), ("ann.moore", [2, 4]), ("skills-canada-2026", [3, 4]),
    ("x.123", [4]),
])
def test_suggested_tvs(name, expected):
    assert tvs.suggested_tvs(name) == expected


@pytest.mark.parametrize("text, expected", [
    ('"C:\\Users\\Ms T\\Art\\a.jpg" C:\\Art\\b.jpg', ["C:\\Users\\Ms T\\Art\\a.jpg",
                                                     "C:\\Art\\b.jpg"]),
    ("& 'C:\\My Art\\a.jpg' 'C:\\b.jpg'", ["C:\\My Art\\a.jpg", "C:\\b.jpg"]),  # VS Code
    ("https://example.com/a b.jpg", ["https://example.com/a b.jpg"]),
])
def test_split_sources(text, expected):
    assert tvs.split_sources(text) == expected


def test_split_sources_keeps_a_real_path_with_spaces(tmp_path):
    folder = tmp_path / "Ann Lee art"
    folder.mkdir()
    assert tvs.split_sources(str(folder)) == [str(folder)]


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


def test_upload_checks_the_size(tmp_path, monkeypatch):
    video = tmp_path / "ann.lee.a.mp4"
    video.write_bytes(b"video")
    tv, connection = make_tv()
    seen = []
    tv.upload(video, "ann.lee.a.mp4", seen.append)
    assert connection.files == {"ann.lee.a.mp4": b"video"} and seen == [1.0]
    monkeypatch.setattr(connection, "upload", lambda f, path, progress=None: None)
    connection.files["ann.lee.a.mp4"] = b"vid"  # cut short
    with pytest.raises(TvError, match="didn't copy"):
        tv.upload(video, "ann.lee.a.mp4")


def test_commands():
    tv, connection = make_tv(usb="mmc-SD16G_0x1\nusb-SanDisk_Cruzer-0:0-part1\n")
    assert tv.usb_stick_plugged_in()
    tv.restart_slideshow()
    tv.reboot()
    tv.power(on=True)
    tv.power(on=False)
    assert connection.commands[1:3] == [("systemctl restart rs", True),
                                        ("shutdown -r now", True)]
    on, off = connection.commands[3][0], connection.commands[4][0]
    assert "echo on 0 | cec-client -s -d 1" in on and "--image-view-on" in on
    assert "echo standby 0 | cec-client -s -d 1" in off and "--standby" in off
    assert all(not sudo for command, sudo in connection.commands[3:])


def test_no_usb_stick():
    tv, _ = make_tv(usb="mmc-SD16G_0x1\nmmc-SD16G_0x1-part1\n")
    assert not tv.usb_stick_plugged_in()


def test_refresh_restarts_the_slideshow(monkeypatch):
    screen = Screen(monkeypatch)
    tv, connection = make_tv()
    tvs.refresh(tv)
    assert ("systemctl restart rs", True) in connection.commands
    assert "restarted the slideshow" in screen.shown("success")


def test_refresh_wont_restart_with_a_usb_stick_in(monkeypatch):
    screen = Screen(monkeypatch)
    tv, connection = make_tv(usb="usb-Kingston_DT-0:0-part1\n")
    tvs.refresh(tv)
    assert not any("restart" in command for command, _ in connection.commands)
    assert "USB stick" in screen.shown("warning")


def test_refresh_that_fails_says_what_happens_next(monkeypatch):
    screen = Screen(monkeypatch)
    tv, _ = make_tv(restart_status=5)
    tvs.refresh(tv)
    assert "after the TV Pi reboots" in screen.shown("warning")


# --- Several TVs at once -------------------------------------------------------------------

def test_connect_skips_tvs_that_cant_be_reached(monkeypatch):
    screen = Screen(monkeypatch)
    monkeypatch.setattr(tvs, "_check_reachable",
                        lambda settings, numbers: {2: "couldn't reach pi-tv2."})
    logged_in = []
    monkeypatch.setattr(tvs.login, "connect", lambda pi: logged_in.append(pi.host)
                        or FakeTvConnection())
    connected = tvs.connect(TvsSettings(), [1, 2, 3])
    assert list(connected) == [1, 3]
    assert logged_in == ["pi-tv1.hackerspace.tbl", "pi-tv3.hackerspace.tbl"]
    assert "TV 2: couldn't reach pi-tv2." in screen.shown("error")


def test_cancelling_the_password_closes_the_others(monkeypatch):
    Screen(monkeypatch)
    monkeypatch.setattr(tvs, "_check_reachable", lambda settings, numbers: {})
    first = FakeTvConnection()
    answers = iter([first, None])
    monkeypatch.setattr(tvs.login, "connect", lambda pi: next(answers))
    assert tvs.connect(TvsSettings(), [1, 2]) == {}
    assert first.closed


def test_check_reachable_says_why(monkeypatch):
    settings = TvsSettings(host_pattern="127.0.0.1", port=1)  # nothing listens on port 1
    problems = tvs._check_reachable(settings, [1])
    assert "Is it plugged in and turned on?" in problems[1]


def test_set_power_reports_each_tv(monkeypatch):
    screen = Screen(monkeypatch)
    working, broken, gone = (FakeTvConnection() for _ in range(3))
    broken.run = lambda command, **k: CommandResult(1, "no CEC adapter found")

    def hangs_up(command, **k):
        raise OSError("Socket is closed")
    gone.run = hangs_up
    connections = {1: working, 2: broken, 3: gone}
    monkeypatch.setattr(tvs, "connect", lambda settings, numbers: {
        n: TvPi(connections[n], n, settings) for n in numbers})
    tvs.set_power(TvsSettings(), [1, 2, 3], on=False)
    assert screen.shown("success") == "TV 1: turned off."
    assert "TV 2: that didn't work (no CEC adapter found)" in screen.shown("error")
    assert "TV 3: that didn't work (Socket is closed)" in screen.shown("error")
    assert all(c.closed for c in connections.values())


# --- Putting a shrine on the TVs -----------------------------------------------------------

def shrine_folder(tmp_path, name="ann.lee", videos=("a", "z.cat")):
    folder = tmp_path / name
    folder.mkdir()
    for video in videos:
        (folder / f"{name}.{video}.mp4").write_bytes(f"new {video}".encode())
    return folder


def uploads_for(folder):
    return {video: video.name for video in tvs.videos_in(folder)}


def test_push_to_an_empty_tv(tmp_path, monkeypatch):
    screen = Screen(monkeypatch)
    tv, connection = make_tv()
    tvs._push_to(tv, uploads_for(shrine_folder(tmp_path)), {"ann.lee"})
    assert connection.files == {"ann.lee.a.mp4": b"new a", "ann.lee.z.cat.mp4": b"new z.cat"}
    assert ("systemctl restart rs", True) in connection.commands
    screen.finished()


@pytest.mark.parametrize("answer, expected", [
    ("replace", {"ann.lee.a.mp4": b"new a", "ann.lee.z.cat.mp4": b"new z.cat",
                 "bob.ng.a.mp4": b"bob"}),
    ("keep", {"ann.lee.a.mp4": b"new a", "ann.lee.z.cat.mp4": b"new z.cat",
              "ann.lee.z.old.mp4": b"old z", "bob.ng.a.mp4": b"bob"}),
    (None, {"ann.lee.a.mp4": b"old a", "ann.lee.z.old.mp4": b"old z", "bob.ng.a.mp4": b"bob"}),
])
def test_push_when_the_tv_already_has_some(tmp_path, monkeypatch, answer, expected):
    screen = Screen(monkeypatch, chooses=[answer])
    tv, connection = make_tv(files={"ann.lee.a.mp4": b"old a", "ann.lee.z.old.mp4": b"old z",
                                    "bob.ng.a.mp4": b"bob"})
    tvs._push_to(tv, uploads_for(shrine_folder(tmp_path)), {"ann.lee"})
    assert connection.files == expected
    assert "ann.lee.z.old.mp4" in screen.shown("info")  # it showed what was there
    screen.finished()


def test_push_when_theres_no_room(tmp_path, monkeypatch):
    screen = Screen(monkeypatch)
    tv, connection = make_tv(free_kb=10)
    tvs._push_to(tv, uploads_for(shrine_folder(tmp_path)), {"ann.lee"})
    assert connection.files == {}
    assert "doesn't have room" in screen.shown("error")


def test_push_makes_room_by_deleting_the_old_ones_first(tmp_path, monkeypatch):
    Screen(monkeypatch, chooses=["replace"])
    tv, connection = make_tv(files={"ann.lee.z.big.mp4": b"x" * 100})
    # Room for the new videos (14 bytes) only once the old one has gone.
    monkeypatch.setattr(tv, "free_space", lambda: tvs.SPARE_SPACE + 5)
    tvs._push_to(tv, uploads_for(shrine_folder(tmp_path)), {"ann.lee"})
    assert set(connection.files) == {"ann.lee.a.mp4", "ann.lee.z.cat.mp4"}


def test_push_shrine_picks_tvs_and_keeps_videos_off_the_hallway(tmp_path, monkeypatch):
    screen = Screen(monkeypatch, ticks=[Screen.DEFAULT], confirms=[False])
    folder = shrine_folder(tmp_path)
    connections = {1: FakeTvConnection(), 4: FakeTvConnection()}
    monkeypatch.setattr(tvs, "connect", lambda settings, numbers: {
        n: TvPi(connections[n], n, settings) for n in numbers})
    tvs.push_shrine(TvsSettings(), folder)
    assert screen.ticked_at_first == [[1, 4]]  # A-L, and the hallway
    assert set(connections[1].files) == {"ann.lee.a.mp4", "ann.lee.z.cat.mp4"}
    assert set(connections[4].files) == {"ann.lee.a.mp4"}
    screen.finished()


def test_push_renames_files_the_tvs_would_skip(tmp_path, monkeypatch):
    Screen(monkeypatch, ticks=[[3]])
    folder = tmp_path / "Showcase"
    folder.mkdir()
    (folder / "Robot Demo.MP4").write_bytes(b"demo")
    connection = FakeTvConnection()
    monkeypatch.setattr(tvs, "connect", lambda settings, numbers: {
        3: TvPi(connection, 3, settings)})
    tvs.push_shrine(TvsSettings(), folder)
    assert connection.files == {"robot_demo.mp4": b"demo"}


def test_choose_a_shrine_folder(tmp_path, monkeypatch):
    for name in ["ann.lee", "bob.ng"]:
        shrine_folder(tmp_path, name)
    (tmp_path / "empty").mkdir()
    chosen = []
    monkeypatch.setattr(ui, "choose", lambda prompt, options: chosen.append(
        [o.value for o in options]) or options[0].value)
    folder = tvs._choose_shrine_folder(TvsSettings(shrines_dir=str(tmp_path)))
    assert folder in (tmp_path / "ann.lee", tmp_path / "bob.ng")
    assert set(chosen[0][:2]) == {tmp_path / "ann.lee", tmp_path / "bob.ng"}
    assert chosen[0][2:] == ["other", ui.BACK]  # no folder without videos


# --- Managing a TV's videos ----------------------------------------------------------------

def test_copy_videos_from_a_tv(tmp_path, monkeypatch):
    screen = Screen(monkeypatch, confirms=[False])
    tv, _ = make_tv(files={"ann.lee.a.mp4": b"ann"})
    tvs._copy_from(tv, ["ann.lee.a.mp4"], tmp_path / "From TV 1")
    assert (tmp_path / "From TV 1" / "ann.lee.a.mp4").read_bytes() == b"ann"
    assert sorted(p.name for p in (tmp_path / "From TV 1").iterdir()) == ["ann.lee.a.mp4"]
    screen.finished()


def test_delete_videos_from_a_tv(monkeypatch):
    screen = Screen(monkeypatch, chooses=["delete", None], asks=["ann"],
                    ticks=[["ann.lee.z.cat.mp4"]], confirms=[True])
    tv, connection = make_tv(files={"ann.lee.a.mp4": b"a", "ann.lee.z.cat.mp4": b"c",
                                    "bob.ng.a.mp4": b"b"})
    tvs._manage(tv, TvsSettings())
    assert set(connection.files) == {"ann.lee.a.mp4", "bob.ng.a.mp4"}
    assert ("systemctl restart rs", True) in connection.commands
    screen.finished()


def test_deleting_can_be_called_off(monkeypatch):
    Screen(monkeypatch, chooses=["delete", None], asks=[""],
           ticks=[["ann.lee.a.mp4"]], confirms=[False])
    tv, connection = make_tv(files={"ann.lee.a.mp4": b"a"})
    tvs._manage(tv, TvsSettings())
    assert set(connection.files) == {"ann.lee.a.mp4"}


# --- Making a shrine (the questions) -------------------------------------------------------

def test_shrines_folder(tmp_path):
    assert tvs.shrines_folder(TvsSettings(shrines_dir=str(tmp_path))) == tmp_path
    assert tvs.shrines_folder(TvsSettings()).name == "Hackerspace shrines"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_documents_folder_on_windows():
    assert tvs._documents().is_dir()


def test_make_a_shrine_then_make_it_again(tmp_path, monkeypatch, ffmpeg_path):
    art = tmp_path / "Ann's art"
    art.mkdir()
    Image.new("RGB", (40, 30), "red").save(art / "one.jpg")
    frames = [Image.new("RGB", (16, 16), c) for c in ("red", "green")]
    frames[0].save(art / "spin.gif", save_all=True, append_images=frames[1:], duration=500)
    settings = TvsSettings(shrines_dir=str(tmp_path / "shrines"), ffmpeg=str(ffmpeg_path))
    screen = Screen(monkeypatch,
                    chooses=["student", "Digital Art", None],
                    asks=["Ann.Lee", "", "2027", f'"{art}"', ""])
    tvs.make_shrine(settings)
    screen.finished()
    made = tmp_path / "shrines" / "ann.lee"
    assert sorted(p.name for p in made.iterdir()) == ["ann.lee.a.mp4", "ann.lee.z.spin.mp4"]

    (art / "spin.gif").unlink()  # the old video should go when it's made again
    screen = Screen(monkeypatch, chooses=["student", "Digital Art", None],
                    asks=["ann.lee", "Ann", "", str(art), ""], confirms=[True])
    tvs.make_shrine(settings)
    screen.finished()
    assert sorted(p.name for p in made.iterdir()) == ["ann.lee.a.mp4"]


@pytest.mark.parametrize("chooses, asks", [
    ([None], []),  # Back at "who is it for"
    (["student"], [None]),  # q at the username
    (["student"], ["ann.lee", None]),  # q at the title name
    (["student"], ["ann.lee", "", None]),  # q at the grad year
    (["student", None], ["ann.lee", "", ""]),  # Back at the subject
    (["student", "Digital Art"], ["ann.lee", "", "", None]),  # q when asked for the work
    (["collection", None], ["Skills Canada"]),  # Back at the title card choice
])
def test_making_a_shrine_can_always_be_left(tmp_path, monkeypatch, chooses, asks):
    settings = TvsSettings(shrines_dir=str(tmp_path / "shrines"))
    monkeypatch.setattr(tvs.ffmpeg, "find", lambda configured: Path("ffmpeg"))
    screen = Screen(monkeypatch, chooses=chooses, asks=asks)
    tvs.make_shrine(settings)
    screen.finished()
    assert not (tmp_path / "shrines").exists()


def test_collection_names_never_have_dots(monkeypatch):
    Screen(monkeypatch, asks=["St. Patrick's Day 2026"], chooses=["none"])
    assert tvs._ask_collection() == ("st-patricks-day-2026", None)


def test_ffmpeg_is_offered_once(monkeypatch, tmp_path):
    screen = Screen(monkeypatch, confirms=[True])
    monkeypatch.setattr(tvs.ffmpeg, "find", lambda configured: None)
    monkeypatch.setattr(tvs.ffmpeg, "build_for_this_computer",
                        lambda: tvs.ffmpeg.Build("https://x", 31_000_000, "0" * 64, "ffmpeg"))
    monkeypatch.setattr(tvs.ffmpeg, "download", lambda progress: tmp_path / "ffmpeg")
    assert tvs._get_ffmpeg(TvsSettings()) == tmp_path / "ffmpeg"
    screen.finished()


# --- Grade 9 mode --------------------------------------------------------------------------

@pytest.mark.parametrize("engage, tvs_on, volume", [(True, False, 0), (False, True, 100)])
def test_grade_9_mode(monkeypatch, engage, tvs_on, volume):
    from control_panel.config import Config

    screen = Screen(monkeypatch, chooses=[engage])
    monkeypatch.setattr(ui, "heading", lambda text: None)
    monkeypatch.setattr(ui, "pause", lambda *a: None)
    powered = []
    monkeypatch.setattr(grade9.tvs, "set_power",
                        lambda settings, numbers, on: powered.append((numbers, on)))
    themes_pi = FakeTvConnection()
    monkeypatch.setattr(grade9.login, "connect", lambda settings: themes_pi)
    grade9.run(Config())
    assert powered == [((1, 2, 3), tvs_on)]
    assert themes_pi.commands == [(f"amixer sset Headphone,0 {volume}%", False)]
    assert ("muted" if engage else "unmuted") in screen.shown("success")
    screen.finished()
