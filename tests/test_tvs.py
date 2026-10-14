import contextlib
import io
import json
import posixpath
import sys
from pathlib import Path
from types import SimpleNamespace

import paramiko
import pytest
from fakes import FakeFilesConnection
from PIL import Image

from control_panel import ffmpeg, shrine, ui
from control_panel.art import ArtError, ArtFolder, ArtServer
from control_panel.config import TvsSettings
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
        self.downloads = []
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
        if len(data) > self.room:  # the old one, if any, only goes once the new one's in
            raise OSError(28, "No space left on device")
        self.room += len(self.files.get(name, b"")) - len(data)
        self.files[name] = data
        if progress:
            progress(len(data), len(data))

    def download(self, path, local, progress=None):
        self.downloads.append(path)
        Path(local).write_bytes(self.files[posixpath.basename(path)])

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
    """Scripted answers for the prompts, and a record of what was shown."""

    DEFAULT = object()  # for choose_many: keep the boxes that were ticked to start with

    def __init__(self, monkeypatch, asks=(), chooses=(), confirms=(), ticks=()):
        self.messages = []
        self.asks, self.chooses = iter(asks), iter(chooses)
        self.confirms, self.ticks = iter(confirms), iter(ticks)
        self.ticked_at_first = []
        self.questions = []  # what confirm() asked
        for kind in ["info", "success", "warning", "error"]:
            monkeypatch.setattr(ui, kind, lambda text, kind=kind: self.messages.append(
                (kind, text)))
        monkeypatch.setattr(ui, "ask", lambda prompt, **k: next(self.asks))
        monkeypatch.setattr(ui, "choose", self._choose)
        monkeypatch.setattr(ui, "confirm", self._confirm)
        monkeypatch.setattr(ui, "choose_many", self._choose_many)
        monkeypatch.setattr(ui, "progress", no_progress)

    def _choose(self, prompt, options):
        answer = next(self.chooses)
        offered = [getattr(option, "value", option) for option in options]
        # None is Back, or Ctrl+C (which works at any menu).
        assert answer is None or answer in offered, \
            f"{answer!r} isn't one of the choices for {prompt!r}"
        return answer

    def _confirm(self, prompt, **kwargs):
        self.questions.append(prompt)
        return next(self.confirms)

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


def test_shrine_name():
    assert tvs.shrine_name("ann.lee", ["ann.lee.a.mp4", "ann.lee.z.cat.mp4"]) == "ann.lee"
    assert tvs.shrine_name("jo.a.smith", ["jo.a.smith.a.mp4"]) == "jo.a.smith"
    assert tvs.shrine_name("Downloads", ["jo.a.smith.a.mp4", "jo.a.smith.z.x.mp4"]) == \
        "jo.a.smith"  # from its slideshow
    assert tvs.shrine_name("Showcase", ["robot_demo.mp4", "intro.mp4"]) is None


def test_upload_names_play_and_dont_clash():
    videos = [Path(n) for n in ["My Video.MP4", "my_video.mp4", "работа.mp4", "作品.mp4"]]
    assert list(tvs.upload_names(videos).values()) == [
        "my_video.mp4", "my_video-2.mp4", "video.mp4", "video-2.mp4"]


@pytest.mark.parametrize("name, safe", [
    ("ann.lee.a.mp4", True), ("Old-Clip_2.MP4", True), ("Art Show 2024.mp4", True),
    ("old.z.Bob's_cat_(final).mov.mp4", True), ("zoë.lee.a.mp4", True),
    ("CON.mp4", False), ("a:b.mp4", False), ("clip.mp4 ", False),
    ("ann.lee.a.mp4   \\..\\..\\ann.lee\\ann.lee.a.mp4", False), (".hidden.mp4", False),
])
def test_is_safe_name(name, safe):
    assert tvs.is_safe_name(name) is safe


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
    with pytest.raises(KeyboardInterrupt):  # so nothing carries on as if it had worked
        tvs.connect(TvsSettings(), [1, 2])
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
    tvs._push_to(tv, uploads_for(shrine_folder(tmp_path)), "ann.lee")
    assert connection.files == {"ann.lee.a.mp4": b"new a", "ann.lee.z.cat.mp4": b"new z.cat"}
    assert ("systemctl restart rs", True) in connection.commands
    screen.finished()


@pytest.mark.parametrize("answer, expected", [
    ("replace", {"ann.lee.a.mp4": b"new a", "ann.lee.z.cat.mp4": b"new z.cat",
                 "bob.ng.a.mp4": b"bob"}),
    ("keep", {"ann.lee.a.mp4": b"new a", "ann.lee.z.cat.mp4": b"new z.cat",
              "ann.lee.z.old.mp4": b"old z", "bob.ng.a.mp4": b"bob"}),
    ("skip", {"ann.lee.a.mp4": b"old a", "ann.lee.z.old.mp4": b"old z", "bob.ng.a.mp4": b"bob"}),
])
def test_push_when_the_tv_already_has_some(tmp_path, monkeypatch, answer, expected):
    screen = Screen(monkeypatch, chooses=[answer])
    tv, connection = make_tv(files={"ann.lee.a.mp4": b"old a", "ann.lee.z.old.mp4": b"old z",
                                    "bob.ng.a.mp4": b"bob"})
    tvs._push_to(tv, uploads_for(shrine_folder(tmp_path)), "ann.lee")
    assert connection.files == expected
    assert "ann.lee.z.old.mp4" in screen.shown("info")  # it showed what was there
    screen.finished()


def test_ctrl_c_stops_the_whole_push(tmp_path, monkeypatch):
    Screen(monkeypatch, chooses=[None])
    tv, connection = make_tv(files={"ann.lee.a.mp4": b"old a"})
    with pytest.raises(KeyboardInterrupt):
        tvs._push_to(tv, uploads_for(shrine_folder(tmp_path)), "ann.lee")
    assert connection.files == {"ann.lee.a.mp4": b"old a"}


def test_push_leaves_other_students_alone(tmp_path, monkeypatch):
    Screen(monkeypatch, chooses=["replace"])
    others = {"jo.a.brown.a.mp4": b"brown", "jo.a.brown.z.dance.mp4": b"dance",
              "jo.z.collection.a.mp4": b"collection"}
    tv, connection = make_tv(files={**others, "jo.a.smith.z.old.mp4": b"old"})
    tvs._push_to(tv, uploads_for(shrine_folder(tmp_path, "jo.a.smith", ["a"])), "jo.a.smith")
    assert connection.files == {**others, "jo.a.smith.a.mp4": b"new a"}


def test_a_push_that_fails_part_way(tmp_path, monkeypatch):
    """Nothing it didn't need to delete is gone, and the slideshow plays what's there now."""
    screen = Screen(monkeypatch, chooses=["replace"])
    tv, connection = make_tv(files={"ann.lee.a.mp4": b"old a", "ann.lee.z.cat.mp4": b"old c",
                                    "ann.lee.z.old.mp4": b"old z"})
    real_upload = connection.upload

    def drops_out(fileobj, path, progress=None):
        if path.endswith("z.cat.mp4"):
            raise OSError("Socket is closed")
        real_upload(fileobj, path, progress)
    connection.upload = drops_out
    with pytest.raises(OSError):
        tvs._push_to(tv, uploads_for(shrine_folder(tmp_path)), "ann.lee")
    assert connection.files == {"ann.lee.a.mp4": b"new a", "ann.lee.z.cat.mp4": b"old c",
                                "ann.lee.z.old.mp4": b"old z"}
    assert ("systemctl restart rs", True) in connection.commands
    screen.finished()


def test_one_tv_failing_doesnt_stop_the_others(tmp_path, monkeypatch):
    screen = Screen(monkeypatch, ticks=[[1, 2]])
    broken, working = FakeTvConnection(), FakeTvConnection()
    broken.file_sizes = lambda path: (_ for _ in ()).throw(OSError("Socket is closed"))
    connections = {1: broken, 2: working}
    monkeypatch.setattr(tvs, "connect", lambda settings, numbers: {
        n: TvPi(connections[n], n, settings) for n in numbers})
    tvs.push_shrine(TvsSettings(), shrine_folder(tmp_path))
    assert "TV 1: that didn't work (Socket is closed)" in screen.shown("error")
    assert set(working.files) == {"ann.lee.a.mp4", "ann.lee.z.cat.mp4"}


@pytest.fixture
def no_spare_space(monkeypatch):
    monkeypatch.setattr(tvs, "SPARE_SPACE", 0)


def test_push_when_theres_no_room(tmp_path, monkeypatch, no_spare_space):
    screen = Screen(monkeypatch)
    tv, connection = make_tv(room=10 * 1024)
    folder = big_shrine(tmp_path, {"ann.lee.a.mp4": 5, "ann.lee.z.cat.mp4": 20})
    tvs._push_to(tv, uploads_for(folder), "ann.lee")
    assert connection.files == {}
    assert "doesn't have room" in screen.shown("error")


def big_shrine(tmp_path, sizes):
    """A shrine folder whose videos have these sizes in KB."""
    folder = tmp_path / "ann.lee"
    folder.mkdir()
    for name, kb in sizes.items():
        (folder / name).write_bytes(b"n" * kb * 1024)
    return folder


def test_push_makes_room_by_deleting_the_old_ones_first(tmp_path, monkeypatch, no_spare_space):
    Screen(monkeypatch, chooses=["replace"])
    # The new videos fit only once the old ones have gone, including the big one that's
    # being replaced by a smaller one of the same name, after a new one.
    tv, connection = make_tv(files={"ann.lee.a.mp4": b"o" * 5 * 1024,
                                    "ann.lee.z.big.mp4": b"o" * 1200 * 1024,
                                    "ann.lee.z.gone.mp4": b"o" * 100 * 1024}, room=50 * 1024)
    folder = big_shrine(tmp_path, {"ann.lee.a.mp4": 5, "ann.lee.z.anim.mp4": 400,
                                   "ann.lee.z.big.mp4": 700})
    tvs._push_to(tv, uploads_for(folder), "ann.lee")
    assert {name: len(data) // 1024 for name, data in connection.files.items()} == {
        "ann.lee.a.mp4": 5, "ann.lee.z.anim.mp4": 400, "ann.lee.z.big.mp4": 700}


def test_short_of_room_never_deletes_what_they_keep(tmp_path, monkeypatch, no_spare_space):
    screen = Screen(monkeypatch, chooses=["keep"])
    tv, connection = make_tv(files={"ann.lee.a.mp4": b"o" * 5 * 1024,
                                    "ann.lee.z.old.mp4": b"o" * 1000 * 1024}, room=50 * 1024)
    folder = big_shrine(tmp_path, {"ann.lee.a.mp4": 5, "ann.lee.z.anim.mp4": 400})
    tvs._push_to(tv, uploads_for(folder), "ann.lee")
    assert "doesn't have room" in screen.shown("error")
    assert set(connection.files) == {"ann.lee.a.mp4", "ann.lee.z.old.mp4"}


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


@pytest.mark.parametrize("answer, expected", [
    (True, {"ann.lee.a.mp4", "ann.lee.z.cat.mp4"}), (False, {"ann.lee.a.mp4"}), (None, None),
])
def test_the_hallway_question_is_asked_even_for_the_hallway_alone(tmp_path, monkeypatch,
                                                                   answer, expected):
    screen = Screen(monkeypatch, ticks=[[4]], confirms=[answer])
    connection = FakeTvConnection()
    monkeypatch.setattr(tvs, "connect", lambda settings, numbers: {
        4: TvPi(connection, 4, settings)})
    tvs.push_shrine(TvsSettings(), shrine_folder(tmp_path))
    assert (set(connection.files) or None) == expected  # Ctrl+C (None) copies nothing
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


def test_copying_skips_names_that_arent_safe(tmp_path, monkeypatch):
    screen = Screen(monkeypatch, confirms=[False])
    sneaky = "ann.lee.a.mp4   \\..\\..\\ann.lee\\ann.lee.a.mp4"
    tv, _ = make_tv(files={sneaky: b"x", "bob.ng.a.mp4": b"bob"})
    tvs._copy_from(tv, [sneaky, "bob.ng.a.mp4"], tmp_path / "From TV 1")
    assert sorted(p.name for p in (tmp_path / "From TV 1").iterdir()) == ["bob.ng.a.mp4"]
    assert "Skipping" in screen.shown("warning")


def test_reboot_wont_with_a_usb_stick_in(monkeypatch):
    screen = Screen(monkeypatch, chooses=[2, "reboot"])
    connection = FakeTvConnection(usb="usb-Kingston_DT-0:0-part1\n")
    monkeypatch.setattr(tvs, "connect", lambda settings, numbers: {
        2: TvPi(connection, 2, settings)})
    tvs.restart(TvsSettings())
    assert not any("shutdown" in command for command, _ in connection.commands)
    assert "USB stick" in screen.shown("warning")


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


@pytest.fixture
def pi_files(monkeypatch):
    """A pretend pi-files for the TVs panel to keep art on."""
    server = ArtServer(FakeFilesConnection(), TvsSettings())
    server.check_drive()

    @contextlib.contextmanager
    def fake(settings):
        yield server
    monkeypatch.setattr(tvs, "art_server", fake)
    return server


@pytest.fixture
def no_pi_files(monkeypatch):
    @contextlib.contextmanager
    def fake(settings):
        yield None
    monkeypatch.setattr(tvs, "art_server", fake)


ANN = ArtFolder(1, "/mnt/ssd/tv1/ann.lee", "ann.lee")


def kept_files(server, folder="/mnt/ssd/tv1/ann.lee"):
    """The art in a shrine's folder on pi-files."""
    return sorted(f for f in server.connection.list_folder(folder)[1] if not f.startswith("."))


def tv_names_kept(server, folder="/mnt/ssd/tv1/ann.lee"):
    """Which video on the TVs pi-files says each piece became."""
    path = f"{folder}/.tv-names.json"
    return json.loads(server.connection.files[path]) if path in server.connection.files else {}


def _picture(path, colour, size=(40, 30)):
    Image.new("RGB", size, colour).save(path)
    return path.read_bytes()


def _colour_at(ffmpeg_path, data, seconds, tmp_path):
    video, frame = tmp_path / "check.mp4", tmp_path / f"check-{seconds}.png"
    video.write_bytes(data)
    ffmpeg.run(ffmpeg_path, ["-ss", str(seconds), "-i", str(video), "-frames:v", "1",
                             str(frame)])
    return Image.open(frame).convert("RGB").getpixel((960, 540))


def _slide_colours(ffmpeg_path, data, tmp_path, count):
    return [_colour_at(ffmpeg_path, data, 8 * k + 4.5, tmp_path) for k in range(count)]


def _duration(ffmpeg_path, data, tmp_path):
    video = tmp_path / "check.mp4"
    video.write_bytes(data)
    return ffmpeg.probe(ffmpeg_path, video).duration


WHITE, RED, BLUE, GREEN = (255, 255, 255), (255, 0, 0), (0, 0, 255), (0, 128, 0)


def assert_colours(colours, expected):
    assert len(colours) == len(expected)
    for colour, wanted in zip(colours, expected, strict=True):
        assert colour == pytest.approx(wanted, abs=10), (colours, expected)


def test_make_a_shrine_then_start_it_over(tmp_path, monkeypatch, ffmpeg_path, pi_files):
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
    # Its art is kept on pi-files, in TV 1's folder (last names A-L), with its title card.
    assert kept_files(pi_files) == ["ann.lee.a.title.png", "ann.lee.z.one.jpg",
                                    "ann.lee.z.spin.gif"]
    card = pi_files.connection.files["/mnt/ssd/tv1/ann.lee/ann.lee.a.title.png"]
    assert Image.open(io.BytesIO(card)).size == (1920, 1080)
    assert pi_files.connection.list_folder("/mnt/ssd/tv1")[0] == ["ann.lee"]  # no staging left
    assert tv_names_kept(pi_files) == {"ann.lee.z.spin.gif": "ann.lee.z.spin.mp4"}

    (art / "spin.gif").unlink()  # the old video should go when it's started over
    screen = Screen(monkeypatch, chooses=["student", "over", "Digital Art", None],
                    asks=["ann.lee", "Ann", "", str(art), ""])
    tvs.make_shrine(settings)
    screen.finished()
    assert "already has art for ann.lee" in screen.shown("info")
    assert sorted(p.name for p in made.iterdir()) == ["ann.lee.a.mp4"]
    assert kept_files(pi_files) == ["ann.lee.a.title.png", "ann.lee.z.one.jpg"]
    aside = pi_files.connection.list_folder("/mnt/ssd/.replaced")[0]  # old art kept aside
    assert len(aside) == 1 and aside[0].endswith("-ann.lee")


def test_starting_over_stays_on_the_tv_it_was_on(tmp_path, monkeypatch, ffmpeg_path, pi_files):
    # Hightower split the alphabet A-M, so a Moore can be in TV 1's folder.
    files = pi_files.connection
    files.add("/mnt/ssd/tv1/mia.moore/mia.moore.z.old.jpg")
    files.add("/mnt/ssd/tv1/mia.moore.z.dance.mp4", b"a loose video, as hightower kept them")
    new = tmp_path / "new.png"
    _picture(new, "blue")
    monkeypatch.setattr(tvs, "connect", lambda settings, numbers: {})
    screen = Screen(monkeypatch, chooses=["student", "over", "Digital Art", "push"],
                    asks=["mia.moore", "", "", str(new), ""], ticks=[Screen.DEFAULT])
    tvs.make_shrine(TvsSettings(shrines_dir=str(tmp_path / "shrines"), ffmpeg=str(ffmpeg_path)))
    screen.finished()
    assert screen.ticked_at_first == [[1, 4]]
    assert kept_files(pi_files, "/mnt/ssd/tv1/mia.moore") == ["mia.moore.a.title.png",
                                                             "mia.moore.z.new.png"]
    assert not files.exists("/mnt/ssd/tv1/mia.moore.z.dance.mp4")  # moved aside too
    assert len(files.list_folder("/mnt/ssd/.replaced")[1]) == 1


def test_a_failed_copy_to_pi_files_leaves_its_old_art(tmp_path, monkeypatch, pi_files):
    files = pi_files.connection
    files.add("/mnt/ssd/tv1/ann.lee/ann.lee.z.old.jpg", b"old")
    uploads = 0

    def flaky(fileobj, path, progress=None):
        nonlocal uploads
        uploads += 1
        if uploads == 2:
            raise OSError("Socket is closed")
        FakeFilesConnection.upload(files, fileobj, path)
    monkeypatch.setattr(files, "upload", flaky)
    art = [(shrine.Media(_new_file(tmp_path, n), shrine.PICTURE, n), f"ann.lee.z.{n}")
           for n in ("a.png", "b.png")]
    screen = Screen(monkeypatch)
    assert tvs._keep_new_art(pi_files, "ann.lee", 1, None, art, [ANN], tmp_path) is None
    assert "still has what it had before" in screen.shown("error")
    assert kept_files(pi_files) == ["ann.lee.z.old.jpg"]
    # The next try starts afresh.
    monkeypatch.setattr(files, "upload", lambda fileobj, path, progress=None:
                        FakeFilesConnection.upload(files, fileobj, path))
    assert tvs._keep_new_art(pi_files, "ann.lee", 1, None, art, [ANN], tmp_path) == ANN
    assert kept_files(pi_files) == ["ann.lee.z.a.png", "ann.lee.z.b.png"]


def _new_file(tmp_path, name, data=None):
    path = tmp_path / name
    if data is None:
        _picture(path, "blue")
    else:
        path.write_bytes(data)
    return path


def test_make_shows_pictures_in_the_order_add_art_will(tmp_path, monkeypatch, ffmpeg_path,
                                                      pi_files):
    art = tmp_path / "art"
    for folder, file_name, colour in [("2025", "zebra.jpg", "red"), ("2026", "apple.jpg", "blue"),
                                      ("2026", "zebra.jpg", "green")]:
        (art / folder).mkdir(parents=True, exist_ok=True)
        _picture(art / folder / file_name, colour)
    tv1 = FakeTvConnection()
    monkeypatch.setattr(tvs, "connect", lambda settings, numbers: {1: TvPi(tv1, 1, settings)})
    screen = Screen(monkeypatch, chooses=["collection", "none", "push"],
                    asks=["Art Show", str(art), ""], ticks=[[1]])
    tvs.make_shrine(TvsSettings(shrines_dir=str(tmp_path / "shrines"), ffmpeg=str(ffmpeg_path)))
    screen.finished()
    # Folder by folder, as they're named on pi-files.
    assert kept_files(pi_files, "/mnt/ssd/tv1/art-show") == [
        "art-show.z.2025-zebra.jpg", "art-show.z.2026-apple.jpg", "art-show.z.2026-zebra.jpg"]
    assert_colours(_slide_colours(ffmpeg_path, tv1.files["art-show.a.mp4"], tmp_path, 3),
                   [RED, BLUE, GREEN])


def test_making_a_shrine_pi_files_has_can_add_to_it(tmp_path, monkeypatch, pi_files):
    pi_files.connection.add("/mnt/ssd/tv1/ann.lee/ann.lee.a.title.png")
    pi_files.connection.add("/mnt/ssd/tv4/ann.lee/x.png")
    monkeypatch.setattr(tvs.ffmpeg, "find", lambda configured: Path("ffmpeg"))
    added = []
    monkeypatch.setattr(tvs, "_add_to", lambda settings, ffmpeg_path, server, target:
                        added.append(target))
    hallway = ArtFolder(4, "/mnt/ssd/tv4/ann.lee", "ann.lee")
    screen = Screen(monkeypatch, chooses=["student", "add", hallway], asks=["ann.lee"])
    tvs.make_shrine(TvsSettings(shrines_dir=str(tmp_path / "shrines")))
    screen.finished()
    assert added == [hallway]  # it asked which


def test_without_pi_files_it_asks_first(tmp_path, monkeypatch, no_pi_files):
    monkeypatch.setattr(tvs.ffmpeg, "find", lambda configured: Path("ffmpeg"))
    screen = Screen(monkeypatch, chooses=["student"], asks=["ann.lee"], confirms=[False])
    tvs.make_shrine(TvsSettings(shrines_dir=str(tmp_path / "shrines")))
    screen.finished()
    assert not (tmp_path / "shrines").exists()


def test_art_follows_a_shrine_to_another_tv(tmp_path, monkeypatch, ffmpeg_path, pi_files):
    art = tmp_path / "art"
    art.mkdir()
    Image.new("RGB", (40, 30), "red").save(art / "one.jpg")
    tv3 = FakeTvConnection()
    monkeypatch.setattr(tvs, "connect", lambda settings, numbers: {3: TvPi(tv3, 3, settings)})
    screen = Screen(monkeypatch, chooses=["student", "Digital Art", "push"],
                    asks=["ann.lee", "", "", str(art), ""], ticks=[[3]])
    tvs.make_shrine(TvsSettings(shrines_dir=str(tmp_path / "shrines"), ffmpeg=str(ffmpeg_path)))
    screen.finished()
    assert set(tv3.files) == {"ann.lee.a.mp4"}
    assert kept_files(pi_files, "/mnt/ssd/tv3/ann.lee") == ["ann.lee.a.title.png",
                                                            "ann.lee.z.one.jpg"]
    assert not pi_files.connection.exists("/mnt/ssd/tv1/ann.lee")


@pytest.mark.parametrize("chosen, done, where", [
    ([1, 2], [1, 2], "tv1"),  # more than one in-room TV: it stays put
    ([4], [4], "tv4"),  # only the hallway
    ([2, 4], [2, 4], "tv2"),
    ([2, 4], [4], "tv1"),  # TV 2 was off, or skipped
])
def test_which_tv_art_follows_to(monkeypatch, pi_files, chosen, done, where):
    pi_files.connection.add("/mnt/ssd/tv1/ann.lee/a.jpg")
    Screen(monkeypatch)
    tvs._follow_to(pi_files, ANN, tvs.PushResult(chosen, done))
    assert pi_files.connection.exists(f"/mnt/ssd/{where}/ann.lee/a.jpg")


def test_art_stays_put_if_the_other_tv_has_a_folder_with_that_name(monkeypatch, pi_files):
    pi_files.connection.add("/mnt/ssd/tv1/ann.lee/a.jpg")
    pi_files.connection.add("/mnt/ssd/tv2/ann.lee/b.jpg")
    screen = Screen(monkeypatch)
    tvs._follow_to(pi_files, ANN, tvs.PushResult([2], [2]))
    assert "stays in TV 1's" in screen.shown("warning")
    pi_files.connection.locked.add("/mnt/ssd/tv1")
    tvs._follow_to(pi_files, ANN, tvs.PushResult([3], [3]))
    assert "Couldn't move its art" in screen.shown("warning")
    assert pi_files.connection.exists("/mnt/ssd/tv1/ann.lee/a.jpg")


@pytest.mark.parametrize("chooses, asks", [
    ([None], []),  # Back at "who is it for"
    (["student"], [None]),  # q at the username
    (["student"], ["ann.lee", None]),  # q at the title name
    (["student"], ["ann.lee", "", None]),  # q at the grad year
    (["student", None], ["ann.lee", "", ""]),  # Back at the subject
    (["student", "Digital Art"], ["ann.lee", "", "", None]),  # q when asked for the work
    (["collection", None], ["Skills Canada"]),  # Back at the title card choice
])
def test_making_a_shrine_can_always_be_left(tmp_path, monkeypatch, chooses, asks, pi_files):
    settings = TvsSettings(shrines_dir=str(tmp_path / "shrines"))
    monkeypatch.setattr(tvs.ffmpeg, "find", lambda configured: Path("ffmpeg"))
    screen = Screen(monkeypatch, chooses=chooses, asks=asks)
    tvs.make_shrine(settings)
    screen.finished()
    assert not (tmp_path / "shrines").exists()
    assert pi_files.connection.files == {}


def test_collection_names_never_have_dots(monkeypatch):
    Screen(monkeypatch, asks=["St. Patrick's Day 2026"], chooses=["none"])
    assert tvs._ask_collection_name() == ("st-patricks-day-2026", "St. Patrick's Day 2026")
    assert tvs._ask_title("st-patricks-day-2026", "St. Patrick's Day 2026") == (None,)


def test_pi_files_that_cant_be_used_is_explained(monkeypatch):
    screen = Screen(monkeypatch)
    monkeypatch.setattr(tvs, "_reach_problem", lambda host, port: "couldn't reach it.")
    with tvs.art_server(TvsSettings()) as server:
        assert server is None
    monkeypatch.setattr(tvs, "_reach_problem", lambda host, port: None)
    monkeypatch.setattr(tvs.login, "connect", lambda settings: None)  # no password given

    with tvs.art_server(TvsSettings()) as server:
        assert server is None

    def refused(settings):
        raise tvs.ConnectionFailed("Couldn't log in to pi-files.")
    monkeypatch.setattr(tvs.login, "connect", refused)
    with tvs.art_server(TvsSettings()) as server:
        assert server is None
    connection = FakeFilesConnection(mounted=False)
    monkeypatch.setattr(tvs.login, "connect", lambda settings: connection)
    with tvs.art_server(TvsSettings()) as server:
        assert server is None
    warnings = screen.shown("warning")
    for expected in ["couldn't reach it", "not logged in", "Couldn't log in",
                     "isn't on the external drive"]:
        assert expected in warnings


def test_on_pi_files_names_pi_files(pi_files):
    with pytest.raises(ArtError, match="Something went wrong on pi-files"):
        with tvs.on_pi_files(pi_files):
            raise paramiko.SSHException("Socket is closed")
    with pytest.raises(ArtError, match="won't let the app save in /mnt/ssd"):
        with tvs.on_pi_files(pi_files):
            raise PermissionError(13, "Permission denied")  # paramiko's have no file name


def test_a_name_that_isnt_text_is_left_out(monkeypatch, pi_files):
    files = pi_files.connection
    files.add("/mnt/ssd/tv1/ann.lee/a.jpg")
    files.add("/mnt/ssd/tv1/Jos\ufffd.lee/b.jpg")  # e.g. Latin-1 from an old USB stick
    files.not_text = {"/mnt/ssd/tv1/Jos\ufffd.lee"}
    screen = Screen(monkeypatch, asks=["lee", None], chooses=[ANN])
    assert tvs._choose_art(pi_files) == ANN
    assert "Some names in /mnt/ssd/tv1 on pi-files aren't readable text" in \
        screen.shown("warning")


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


# --- Adding art ----------------------------------------------------------------------------

def _new_art(tmp_path):
    art = tmp_path / "new art"
    art.mkdir()
    _picture(art / "new.png", "blue")
    frames = [Image.new("RGB", (16, 16), c) for c in ("red", "green")]
    frames[0].save(art / "clip.gif", save_all=True, append_images=frames[1:], duration=500)
    return art


def _two_tvs(monkeypatch, tv1, tv4):
    monkeypatch.setattr(tvs, "connect", lambda settings, numbers: {
        n: TvPi(c, n, settings) for n, c in ((1, tv1), (4, tv4)) if n in numbers})


def test_add_art_makes_the_slideshow_again_from_pi_files(tmp_path, monkeypatch, ffmpeg_path,
                                                         pi_files):
    files = pi_files.connection
    files.add("/mnt/ssd/tv1/ann.lee/ann.lee.a.title.png",
              _picture(tmp_path / "title.png", "white", (1920, 1080)))
    files.add("/mnt/ssd/tv1/ann.lee/old.jpg", _picture(tmp_path / "old.jpg", "red"))
    files.add("/mnt/ssd/tv1/ann.lee.z.clip.avi", b"a video, as hightower kept them")
    tv1 = FakeTvConnection(files={"ann.lee.a.mp4": b"old slideshow",
                                  "ann.lee.z.dance.mp4": b"dance", "bob.ng.a.mp4": b"bob"})
    tv4 = FakeTvConnection(files={"ann.lee.a.mp4": b"old slideshow"})
    _two_tvs(monkeypatch, tv1, tv4)
    local = tmp_path / "shrines" / "ann.lee"  # made on this computer before
    local.mkdir(parents=True)
    (local / "ann.lee.z.dance.mp4").write_bytes(b"dance")
    screen = Screen(monkeypatch, asks=["ann", f'"{_new_art(tmp_path)}"', ""],
                    chooses=[ANN], ticks=[Screen.DEFAULT], confirms=[False])
    settings = TvsSettings(shrines_dir=str(tmp_path / "shrines"), ffmpeg=str(ffmpeg_path))
    tvs.add_art(settings)
    screen.finished()
    assert screen.ticked_at_first == [[1, 4]]  # its TV, and the hallway
    assert kept_files(pi_files) == ["ann.lee.a.title.png", "ann.lee.z.clip.gif",
                                    "ann.lee.z.new.png", "old.jpg"]
    # The new video doesn't take the name of the one kept loose in tv1.
    assert set(tv1.files) == {"ann.lee.a.mp4", "ann.lee.z.dance.mp4", "ann.lee.z.clip-2.mp4",
                              "bob.ng.a.mp4"}
    assert set(tv4.files) == {"ann.lee.a.mp4"}
    assert sorted(p.name for p in local.iterdir()) == [
        "ann.lee.a.mp4", "ann.lee.z.clip-2.mp4", "ann.lee.z.dance.mp4"]  # the others stay
    slideshow = tv1.files["ann.lee.a.mp4"]
    assert tv4.files["ann.lee.a.mp4"] == slideshow
    # The title card, then the pictures in the order of their names: new, then old.
    assert _duration(ffmpeg_path, slideshow, tmp_path) == pytest.approx(8 * 3 + 1, abs=0.1)
    assert_colours(_slide_colours(ffmpeg_path, slideshow, tmp_path, 3), [WHITE, BLUE, RED])


def test_add_art_can_make_a_missing_title_card(tmp_path, monkeypatch, ffmpeg_path, pi_files):
    pi_files.connection.add("/mnt/ssd/tv1/ann.lee/old.jpg", _picture(tmp_path / "o.jpg", "red"))
    tv1 = FakeTvConnection()
    monkeypatch.setattr(tvs, "connect", lambda settings, numbers: {1: TvPi(tv1, 1, settings)})
    new = _new_file(tmp_path, "new.png")
    screen = Screen(monkeypatch, asks=["ann", "", "", str(new), ""], chooses=[ANN, "Digital Art"],
                    confirms=[True], ticks=[[1]])
    tvs.add_art(TvsSettings(shrines_dir=str(tmp_path / "shrines"), ffmpeg=str(ffmpeg_path)))
    screen.finished()
    assert kept_files(pi_files) == ["ann.lee.a.title.png", "ann.lee.z.new.png", "old.jpg"]
    assert _duration(ffmpeg_path, tv1.files["ann.lee.a.mp4"], tmp_path) == \
        pytest.approx(8 * 3 + 1, abs=0.1)


def test_replacing_a_piece(tmp_path, monkeypatch, ffmpeg_path, pi_files):
    files = pi_files.connection
    files.add("/mnt/ssd/tv1/ann.lee/ann.lee.a.title.png",
              _picture(tmp_path / "title.png", "white", (1920, 1080)))
    files.add("/mnt/ssd/tv1/ann.lee/ann.lee.z.moon.png", _picture(tmp_path / "m.png", "red"))
    tv1 = FakeTvConnection()
    monkeypatch.setattr(tvs, "connect", lambda settings, numbers: {1: TvPi(tv1, 1, settings)})
    new = tmp_path / "new"
    new.mkdir()
    _picture(new / "Moon.jpg", "blue")  # hightower may have turned it into a PNG
    screen = Screen(monkeypatch, asks=["ann", str(new), ""], chooses=[ANN, "replace"],
                    ticks=[[1]])
    tvs.add_art(TvsSettings(shrines_dir=str(tmp_path / "shrines"), ffmpeg=str(ffmpeg_path)))
    screen.finished()
    assert kept_files(pi_files) == ["ann.lee.a.title.png", "ann.lee.z.moon.jpg"]
    aside = files.list_folder("/mnt/ssd/.replaced")[1]
    assert len(aside) == 1 and list(aside)[0].endswith("-ann.lee.z.moon.png")
    assert_colours(_slide_colours(ffmpeg_path, tv1.files["ann.lee.a.mp4"], tmp_path, 2),
                   [WHITE, BLUE])


def test_nothing_new_can_still_make_the_slideshow_again(tmp_path, monkeypatch, ffmpeg_path,
                                                        pi_files):
    """E.g. after an add was cancelled while its videos were being made."""
    files = pi_files.connection
    files.add("/mnt/ssd/tv1/ann.lee/ann.lee.z.sun.png", _picture(tmp_path / "sun.png", "red"))
    tv1 = FakeTvConnection()
    monkeypatch.setattr(tvs, "connect", lambda settings, numbers: {1: TvPi(tv1, 1, settings)})
    screen = Screen(monkeypatch, asks=["ann", str(tmp_path / "sun.png"), ""], chooses=[ANN],
                    confirms=[False, True], ticks=[[1]])
    tvs.add_art(TvsSettings(shrines_dir=str(tmp_path / "shrines"), ffmpeg=str(ffmpeg_path)))
    screen.finished()
    assert "not added again: sun.png" in screen.shown("info")
    assert _duration(ffmpeg_path, tv1.files["ann.lee.a.mp4"], tmp_path) == \
        pytest.approx(8 + 1, abs=0.1)
    assert kept_files(pi_files) == ["ann.lee.z.sun.png"]


def test_nothing_changes_on_pi_files_if_making_the_videos_stops(tmp_path, monkeypatch,
                                                                 pi_files):
    pi_files.connection.add("/mnt/ssd/tv1/ann.lee/ann.lee.a.title.png",
                            _picture(tmp_path / "title.png", "white"))
    before = dict(pi_files.connection.files)

    def cancelled(*args, **kwargs):
        raise KeyboardInterrupt
    monkeypatch.setattr(tvs.shrine, "make", cancelled)
    monkeypatch.setattr(tvs.ffmpeg, "find", lambda configured: Path("ffmpeg"))
    new = _new_file(tmp_path, "new.png")
    Screen(monkeypatch, asks=["ann", str(new), ""], chooses=[ANN])
    with pytest.raises(KeyboardInterrupt):
        tvs.add_art(TvsSettings(shrines_dir=str(tmp_path / "shrines")))
    assert pi_files.connection.files == before


def test_a_shrine_on_the_hallway_tv_gets_its_videos_there(tmp_path, monkeypatch, ffmpeg_path,
                                                          pi_files):
    pi_files.connection.add("/mnt/ssd/tv4/bob.ng/bob.ng.a.title.png")
    tv4 = FakeTvConnection(files={"bob.ng.a.mp4": b"slideshow"})
    monkeypatch.setattr(tvs, "connect", lambda settings, numbers: {4: TvPi(tv4, 4, settings)})
    art = _new_art(tmp_path)
    (art / "new.png").unlink()  # just a video
    screen = Screen(monkeypatch, asks=["bob", str(art), ""],
                    chooses=[ArtFolder(4, "/mnt/ssd/tv4/bob.ng", "bob.ng")], ticks=[[4]])
    tvs.add_art(TvsSettings(shrines_dir=str(tmp_path / "shrines"), ffmpeg=str(ffmpeg_path)))
    screen.finished()
    assert set(tv4.files) == {"bob.ng.a.mp4", "bob.ng.z.clip.mp4"}  # no hallway question


def test_adding_just_a_video_doesnt_tick_the_hallway(tmp_path, monkeypatch, ffmpeg_path,
                                                     pi_files):
    pi_files.connection.add("/mnt/ssd/tv1/ann.lee/ann.lee.a.title.png")
    tv1, tv4 = FakeTvConnection(), FakeTvConnection()
    _two_tvs(monkeypatch, tv1, tv4)
    art = _new_art(tmp_path)
    (art / "new.png").unlink()
    screen = Screen(monkeypatch, asks=["ann", str(art), ""], chooses=[ANN],
                    ticks=[Screen.DEFAULT])
    tvs.add_art(TvsSettings(shrines_dir=str(tmp_path / "shrines"), ffmpeg=str(ffmpeg_path)))
    screen.finished()
    assert screen.ticked_at_first == [[1]]
    assert set(tv1.files) == {"ann.lee.z.clip.mp4"}


def test_a_shrine_hightower_only_kept_videos_for(tmp_path, monkeypatch, ffmpeg_path, pi_files):
    pi_files.connection.add("/mnt/ssd/tv2/zed.moore.z.clip.mp4")
    found = pi_files.find("zed")
    assert found == [ArtFolder(2, "/mnt/ssd/tv2/zed.moore", "zed.moore")]
    tv2 = FakeTvConnection()
    monkeypatch.setattr(tvs, "connect", lambda settings, numbers: {2: TvPi(tv2, 2, settings)})
    new = _new_file(tmp_path, "new.png")
    screen = Screen(monkeypatch, asks=["zed", str(new), ""], chooses=found, confirms=[False],
                    ticks=[[2]])
    tvs.add_art(TvsSettings(shrines_dir=str(tmp_path / "shrines"), ffmpeg=str(ffmpeg_path)))
    screen.finished()
    assert kept_files(pi_files, "/mnt/ssd/tv2/zed.moore") == ["zed.moore.z.new.png"]
    assert set(tv2.files) == {"zed.moore.a.mp4"}


def test_art_already_on_pi_files_isnt_added_again(tmp_path, monkeypatch, pi_files):
    files = pi_files.connection
    files.add("/mnt/ssd/tv1/ann.lee/ann.lee.a.title.png", b"title")
    files.add("/mnt/ssd/tv1/ann.lee/ann.lee.z.sun.jpg", b"sun")
    files.add("/mnt/ssd/tv1/ann.lee/moon.jpg", b"moon")
    files.add("/mnt/ssd/tv1/ann.lee/ann.lee.z.star.png", b"star")
    files.add("/mnt/ssd/tv1/ann.lee.z.dance.mp4", b"dance")  # loose, as hightower kept it
    local = {}
    for name, data in [("Sun.jpg", b"sun"), ("moon.jpg", b"new moon"), ("star.jpg", b"new star"),
                       ("comet.jpg", b"comet"), ("dance.mp4", b"dance"), ("title.png", b"t")]:
        local[name] = _new_file(tmp_path, name, data)
    media = [shrine.Media(path, shrine.VIDEO if name.endswith(".mp4") else shrine.PICTURE, name)
             for name, path in local.items()]
    existing = pi_files.art_files(ANN)

    def plan(answer):
        screen = Screen(monkeypatch, chooses=[answer])
        planned = tvs._plan_new_art(pi_files, ANN, existing, media)
        screen.finished()
        return screen, [(item.label, name, old) for item, name, old in planned]

    screen, planned = plan("replace")
    assert "not added again: Sun.jpg, dance.mp4" in screen.shown("info")
    assert "already has pieces with the same names as moon.jpg, star.jpg" in screen.shown("info")
    assert planned == [
        ("moon.jpg", "ann.lee.z.moon.jpg", ["/mnt/ssd/tv1/ann.lee/moon.jpg"]),
        ("star.jpg", "ann.lee.z.star.jpg", ["/mnt/ssd/tv1/ann.lee/ann.lee.z.star.png"]),
        ("comet.jpg", "ann.lee.z.comet.jpg", []),
        ("title.png", "ann.lee.z.title.png", [])]  # not the title card
    assert plan("skip")[1] == [("comet.jpg", "ann.lee.z.comet.jpg", []),
                               ("title.png", "ann.lee.z.title.png", [])]
    assert [name for _, name, _ in plan("both")[1]] == [
        "ann.lee.z.moon.jpg", "ann.lee.z.star.jpg", "ann.lee.z.comet.jpg", "ann.lee.z.title.png"]
    Screen(monkeypatch, chooses=[None])
    assert tvs._plan_new_art(pi_files, ANN, existing, media) is None


def test_replace_takes_every_old_copy_and_only_the_replacer_takes_its_name(tmp_path, monkeypatch,
                                                                           pi_files):
    files = pi_files.connection
    files.add("/mnt/ssd/tv1/ann.lee/ann.lee.z.moon.jpg", b"moon")
    files.add("/mnt/ssd/tv1/ann.lee/ann.lee.z.moon.png", b"moon, saved again by hightower")
    media = [shrine.Media(_new_file(tmp_path, f, d), shrine.PICTURE, label)
             for f, d, label in [("1.png", b"new moon", "moon.png"),
                                 ("2.png", b"another moon", "Moon.png")]]
    Screen(monkeypatch, chooses=["replace"])
    planned = tvs._plan_new_art(pi_files, ANN, pi_files.art_files(ANN), media)
    assert [(item.label, name, sorted(olds)) for item, name, olds in planned] == [
        ("moon.png", "ann.lee.z.moon.png", ["/mnt/ssd/tv1/ann.lee/ann.lee.z.moon.jpg",
                                            "/mnt/ssd/tv1/ann.lee/ann.lee.z.moon.png"]),
        ("Moon.png", "ann.lee.z.moon-2.png", [])]


def test_a_damaged_picture_cant_replace_anything(tmp_path, monkeypatch, ffmpeg_path, pi_files):
    files = pi_files.connection
    files.add("/mnt/ssd/tv1/ann.lee/ann.lee.a.title.png",
              _picture(tmp_path / "title.png", "white", (1920, 1080)))
    files.add("/mnt/ssd/tv1/ann.lee/ann.lee.z.moon.png", _picture(tmp_path / "old.png", "red"))
    new = tmp_path / "new"
    new.mkdir()
    noise = Image.effect_noise((600, 400), 80).convert("RGB")
    noise.save(new / "Moon.jpg", quality=95)
    data = (new / "Moon.jpg").read_bytes()
    (new / "Moon.jpg").write_bytes(data[:len(data) // 2])  # cut short, as if partly copied
    tv1 = FakeTvConnection()
    monkeypatch.setattr(tvs, "connect", lambda settings, numbers: {1: TvPi(tv1, 1, settings)})
    screen = Screen(monkeypatch, asks=["ann", str(new), ""], chooses=[ANN], confirms=[False])
    tvs.add_art(TvsSettings(shrines_dir=str(tmp_path / "shrines"), ffmpeg=str(ffmpeg_path)))
    screen.finished()
    assert "Left out Moon.jpg" in screen.shown("warning")
    assert files.files["/mnt/ssd/tv1/ann.lee/ann.lee.z.moon.png"] == \
        (tmp_path / "old.png").read_bytes()
    assert not files.exists("/mnt/ssd/.replaced")


def test_a_failed_copy_to_pi_files_can_be_tried_again(tmp_path, monkeypatch, ffmpeg_path,
                                                      pi_files):
    files = pi_files.connection
    files.add("/mnt/ssd/tv1/ann.lee/ann.lee.a.title.png",
              _picture(tmp_path / "title.png", "white", (1920, 1080)))
    tv1 = FakeTvConnection()
    monkeypatch.setattr(tvs, "connect", lambda settings, numbers: {1: TvPi(tv1, 1, settings)})
    art = _new_art(tmp_path)
    uploads = 0

    def flaky(fileobj, path, progress=None):
        nonlocal uploads
        uploads += 1
        if uploads == 2:
            raise OSError("Socket is closed")
        FakeFilesConnection.upload(files, fileobj, path)
    monkeypatch.setattr(files, "upload", flaky)
    settings = TvsSettings(shrines_dir=str(tmp_path / "shrines"), ffmpeg=str(ffmpeg_path))
    screen = Screen(monkeypatch, asks=["ann", str(art), ""], chooses=[ANN])
    tvs.add_art(settings)
    screen.finished()
    assert "So nothing has changed" in screen.shown("error")
    assert kept_files(pi_files) == ["ann.lee.a.title.png"]  # nothing half there
    assert tv1.files == {}
    screen = Screen(monkeypatch, asks=["ann", str(art), ""], chooses=[ANN], ticks=[[1]])
    tvs.add_art(settings)
    screen.finished()
    assert kept_files(pi_files) == ["ann.lee.a.title.png", "ann.lee.z.clip.gif",
                                    "ann.lee.z.new.png"]
    assert set(tv1.files) == {"ann.lee.a.mp4", "ann.lee.z.clip.mp4"}


def test_a_replaced_video_keeps_its_name_on_the_tvs(tmp_path, monkeypatch, ffmpeg_path,
                                                    pi_files):
    files = pi_files.connection
    files.add("/mnt/ssd/tv1/ann.lee/ann.lee.a.title.png")
    files.add("/mnt/ssd/tv1/ann.lee/ann.lee.z.clip.gif", b"the old animation")
    local = tmp_path / "shrines" / "ann.lee"
    local.mkdir(parents=True)
    (local / "ann.lee.z.clip.mp4").write_bytes(b"old video")
    tv1 = FakeTvConnection(files={"ann.lee.z.clip.mp4": b"old video"})
    monkeypatch.setattr(tvs, "connect", lambda settings, numbers: {1: TvPi(tv1, 1, settings)})
    art = _new_art(tmp_path)
    (art / "new.png").unlink()
    screen = Screen(monkeypatch, asks=["ann", str(art), ""], chooses=[ANN, "replace"],
                    ticks=[[1]])
    tvs.add_art(TvsSettings(shrines_dir=str(tmp_path / "shrines"), ffmpeg=str(ffmpeg_path)))
    screen.finished()
    assert set(tv1.files) == {"ann.lee.z.clip.mp4"}  # not a second, clip-2
    assert tv1.files["ann.lee.z.clip.mp4"] != b"old video"
    assert tv_names_kept(pi_files) == {"ann.lee.z.clip.gif": "ann.lee.z.clip.mp4"}


def test_videos_from_subfolders_are_named_as_theyre_kept(tmp_path, monkeypatch, ffmpeg_path,
                                                         pi_files):
    art = tmp_path / "art"
    (art / "Grade 9").mkdir(parents=True)
    frames = [Image.new("RGB", (16, 16), c) for c in ("red", "green")]
    frames[0].save(art / "Grade 9" / "dance.gif", save_all=True, append_images=frames[1:],
                   duration=500)
    screen = Screen(monkeypatch, chooses=["student", "Digital Art", None],
                    asks=["ann.lee", "", "", str(art), ""])
    tvs.make_shrine(TvsSettings(shrines_dir=str(tmp_path / "shrines"), ffmpeg=str(ffmpeg_path)))
    screen.finished()
    assert sorted(p.name for p in (tmp_path / "shrines" / "ann.lee").iterdir()) == [
        "ann.lee.a.mp4", "ann.lee.z.grade_9-dance.mp4"]
    assert "ann.lee.z.grade_9-dance.gif" in kept_files(pi_files)
    assert tvs._video_names("ann.lee", ["/mnt/ssd/tv1/ann.lee/ann.lee.z.grade_9-dance.gif"],
                            tmp_path / "x") == {"ann.lee.z.grade_9-dance.mp4"}


def test_starting_over_on_the_hallway_puts_its_videos_there(tmp_path, monkeypatch, ffmpeg_path,
                                                            pi_files):
    pi_files.connection.add("/mnt/ssd/tv4/bob.ng/old.jpg")
    tv4 = FakeTvConnection()
    monkeypatch.setattr(tvs, "connect", lambda settings, numbers: {4: TvPi(tv4, 4, settings)})
    art = _new_art(tmp_path)
    screen = Screen(monkeypatch, chooses=["student", "over", "Digital Art", "push"],
                    asks=["bob.ng", "", "", str(art), ""], ticks=[Screen.DEFAULT])
    tvs.make_shrine(TvsSettings(shrines_dir=str(tmp_path / "shrines"), ffmpeg=str(ffmpeg_path)))
    screen.finished()  # no hallway question
    assert set(tv4.files) == {"bob.ng.a.mp4", "bob.ng.z.clip.mp4"}


def test_starting_over_puts_old_art_back_if_it_cant_finish(tmp_path, monkeypatch, pi_files):
    files = pi_files.connection
    files.add("/mnt/ssd/tv1/ann.lee/old.jpg", b"old")
    files.add("/mnt/ssd/tv1/ann.lee.z.clip.mp4", b"loose")
    real_rename = files.rename

    def rename(old, new):
        if old == "/mnt/ssd/tv1/.ann.lee.new":
            raise PermissionError(13, "Permission denied")
        real_rename(old, new)
    monkeypatch.setattr(files, "rename", rename)
    art = [(shrine.Media(_new_file(tmp_path, "a.png"), shrine.PICTURE, "a.png"),
            "ann.lee.z.a.png")]
    screen = Screen(monkeypatch)
    old = pi_files.find("ann.lee", exact=True)
    assert tvs._keep_new_art(pi_files, "ann.lee", 1, None, art, old, tmp_path) is None
    assert "still has what it had before" in screen.shown("error")
    assert files.files["/mnt/ssd/tv1/ann.lee/old.jpg"] == b"old"
    assert files.files["/mnt/ssd/tv1/ann.lee.z.clip.mp4"] == b"loose"


def test_ctrl_c_while_putting_art_in_place_puts_the_old_art_back(tmp_path, monkeypatch,
                                                                 ffmpeg_path, pi_files):
    files = pi_files.connection
    files.add("/mnt/ssd/tv1/ann.lee/ann.lee.a.title.png",
              _picture(tmp_path / "title.png", "white", (1920, 1080)))
    files.add("/mnt/ssd/tv1/ann.lee/ann.lee.z.moon.png", b"old moon")
    new = tmp_path / "new"
    new.mkdir()
    _picture(new / "moon.png", "blue")

    def cancelled(staged):
        raise KeyboardInterrupt
    monkeypatch.setattr(pi_files, "commit", cancelled)
    Screen(monkeypatch, asks=["ann", str(new), ""], chooses=[ANN, "replace"])
    with pytest.raises(KeyboardInterrupt):
        tvs.add_art(TvsSettings(shrines_dir=str(tmp_path / "shrines"), ffmpeg=str(ffmpeg_path)))
    assert files.files["/mnt/ssd/tv1/ann.lee/ann.lee.z.moon.png"] == b"old moon"
    assert kept_files(pi_files) == ["ann.lee.a.title.png", "ann.lee.z.moon.png"]  # no .new left
    assert not files.list_folder("/mnt/ssd/.replaced")[1]
    assert sorted(p.name for p in (tmp_path / "shrines" / "ann.lee").iterdir()) == []


def test_names_that_arent_text_in_the_shrines_own_folder_are_asked_about(monkeypatch, pi_files):
    files = pi_files.connection
    files.add("/mnt/ssd/tv1/ann.lee/a.jpg")
    files.add("/mnt/ssd/tv1/ann.lee/caf\ufffd.jpg")
    files.not_text = {"/mnt/ssd/tv1/ann.lee/caf\ufffd.jpg"}
    screen = Screen(monkeypatch, confirms=[False])
    tvs._add_to(TvsSettings(), Path("ffmpeg"), pi_files, ANN)
    screen.finished()
    assert "Some names in /mnt/ssd/tv1/ann.lee" in screen.shown("warning")


def test_hashes_match_names_sha256sum_escapes(pi_files):
    path = "/mnt/ssd/tv1/ann.lee/back\\slash\nnew line.png"
    pi_files.connection.add(path, b"x")
    assert path in pi_files.hashes([path])


def test_new_video_names_skip_the_ones_the_tvs_have(tmp_path):
    old = ["/mnt/ssd/tv1/ann.lee/ann.lee.z.anim.gif", "/mnt/ssd/tv1/ann.lee/ann.lee.z.anim.mp4",
           "/mnt/ssd/tv1/ann.lee/ann.lee.a.title.png"]
    taken = tvs._video_names("ann.lee", old, tmp_path / "nowhere")
    assert taken == {"ann.lee.z.anim.mp4", "ann.lee.z.anim-2.mp4"}
    # The names pi-files has noted are taken too, unless that piece is being replaced.
    known = {"ann.lee.z.x.mov": "ann.lee.z.clip.mp4"}
    assert tvs._video_names("ann.lee", [], tmp_path / "nowhere", known=known) == \
        {"ann.lee.z.clip.mp4"}
    assert tvs._video_names("ann.lee", [], tmp_path / "nowhere",
                            {"/mnt/ssd/tv1/ann.lee/ann.lee.z.x.mov"}, known) == set()


def test_pi_files_problems_are_reported_as_pi_files(tmp_path, monkeypatch, ffmpeg_path, pi_files):
    pi_files.connection.add("/mnt/ssd/tv1/ann.lee/ann.lee.a.title.png")
    pi_files.connection.locked.add("/mnt/ssd/tv1/ann.lee")
    new = _new_file(tmp_path, "new.png")
    screen = Screen(monkeypatch, asks=[str(new), ""])
    tvs._add_to(TvsSettings(shrines_dir=str(tmp_path)), ffmpeg_path, pi_files, ANN)
    assert "won't let the app save in /mnt/ssd/tv1/ann.lee" in screen.shown("error")
    assert "So nothing has changed" in screen.shown("error")


def test_add_art_needs_pi_files(monkeypatch, tmp_path, no_pi_files):
    monkeypatch.setattr(tvs.ffmpeg, "find", lambda configured: Path("ffmpeg"))
    screen = Screen(monkeypatch)
    tvs.add_art(TvsSettings(shrines_dir=str(tmp_path)))
    assert "needs pi-files" in screen.shown("error")


@pytest.mark.parametrize("asks, chooses, confirms", [
    ([None], [], []),  # q at "whose shrine"
    (["zzz", None], [], []),  # no such shrine, then q
    (["ann"], [None], []),  # Back at the list
    (["ann", None], ["again"], []),  # search again, then q
    (["ann", None], [ANN], [False]),  # q when asked for the new art
    (["ann"], [ANN], [None]),  # Ctrl+C at "make a title card?"
    (["ann", None], [ANN], [True]),  # q at the title card's name
    (["ann", ""], [ANN], [False, False]),  # nothing added, and no remaking the slideshow
])
def test_adding_art_can_always_be_left(tmp_path, monkeypatch, pi_files, asks, chooses,
                                       confirms):
    pi_files.connection.add("/mnt/ssd/tv1/ann.lee/old.jpg")
    before = dict(pi_files.connection.files)
    monkeypatch.setattr(tvs.ffmpeg, "find", lambda configured: Path("ffmpeg"))
    screen = Screen(monkeypatch, asks=asks, chooses=chooses, confirms=confirms)
    tvs.add_art(TvsSettings(shrines_dir=str(tmp_path / "shrines")))
    screen.finished()
    assert not (tmp_path / "shrines").exists()
    assert pi_files.connection.files == before


def test_adding_never_asks_about_or_removes_the_other_videos(tmp_path, monkeypatch):
    screen = Screen(monkeypatch)
    tv, connection = make_tv(files={"ann.lee.a.mp4": b"old a", "ann.lee.z.old.mp4": b"old z"})
    folder = shrine_folder(tmp_path, videos=("a",))
    assert tvs._push_to(tv, uploads_for(folder), "ann.lee", adding=True)
    assert connection.files == {"ann.lee.a.mp4": b"new a", "ann.lee.z.old.mp4": b"old z"}
    screen.finished()


# --- Seeing, opening and deleting a shrine's art -------------------------------------------

@pytest.fixture
def opened(monkeypatch):
    """What was opened to look at: {its name here: what was in it}."""
    seen = {}
    monkeypatch.setattr(tvs, "open_file", lambda path, what="it": seen.update(
        {path.name: path.read_bytes()}))
    return seen


def _shrine_with_art(pi_files, tmp_path):
    files = pi_files.connection
    files.add("/mnt/ssd/tv1/ann.lee/ann.lee.a.title.png",
              _picture(tmp_path / "title.png", "white", (1920, 1080)))
    files.add("/mnt/ssd/tv1/ann.lee/ann.lee.z.red.jpg", _picture(tmp_path / "red.jpg", "red"))
    files.add("/mnt/ssd/tv1/ann.lee/ann.lee.z.blue.png", _picture(tmp_path / "blue.png", "blue"))
    return files


def test_see_a_piece_then_delete_it(tmp_path, monkeypatch, ffmpeg_path, pi_files, opened):
    files = _shrine_with_art(pi_files, tmp_path)
    tv1 = FakeTvConnection(files={"ann.lee.a.mp4": b"old slideshow", "ann.lee.z.cat.mp4": b"cat"})
    tv4 = FakeTvConnection(files={"ann.lee.a.mp4": b"old slideshow"})
    _two_tvs(monkeypatch, tv1, tv4)
    red = "/mnt/ssd/tv1/ann.lee/ann.lee.z.red.jpg"
    screen = Screen(monkeypatch, asks=["ann"], chooses=[ANN, red, None],
                    confirms=[True, True], ticks=[Screen.DEFAULT])
    tvs.view_art(TvsSettings(shrines_dir=str(tmp_path / "shrines"), ffmpeg=str(ffmpeg_path)))
    screen.finished()
    assert list(opened.values()) == [(tmp_path / "red.jpg").read_bytes()]  # it was opened
    assert screen.ticked_at_first == [[1, 4]]
    assert kept_files(pi_files) == ["ann.lee.a.title.png", "ann.lee.z.blue.png"]
    assert files.list_folder("/mnt/ssd/.replaced")[1]  # moved aside, not deleted
    # The slideshow was made again without it, and put on both TVs; the video stays.
    assert set(tv1.files) == {"ann.lee.a.mp4", "ann.lee.z.cat.mp4"}
    slideshow = tv1.files["ann.lee.a.mp4"]
    assert tv4.files["ann.lee.a.mp4"] == slideshow
    assert_colours(_slide_colours(ffmpeg_path, slideshow, tmp_path, 2), [WHITE, BLUE])


def test_pieces_are_listed_in_slideshow_order_then_videos(pi_files, tmp_path):
    files = _shrine_with_art(pi_files, tmp_path)
    files.add("/mnt/ssd/tv1/ann.lee/ann.lee.z.clip.mov", b"clip")
    files.add("/mnt/ssd/tv1/ann.lee.z.dance.mp4", b"dance")
    existing = pi_files.art_files(ANN)
    listed = [tvs._describe(p, size, ANN) for p, size in tvs._viewing_order(existing, "ann.lee")]
    assert [line.split(" (")[0] for line in listed] == [
        "ann.lee.a.title.png", "ann.lee.z.blue.png", "ann.lee.z.red.jpg", "ann.lee.z.clip.mov",
        "ann.lee.z.dance.mp4"]
    assert "(the title card," in listed[0] and "(picture," in listed[1]
    assert "(video," in listed[3] and "(video, loose in the TV's folder," in listed[4]


def test_deleting_a_video_takes_it_off_the_tvs(tmp_path, monkeypatch, pi_files, opened):
    files = pi_files.connection
    files.add("/mnt/ssd/tv1/ann.lee/ann.lee.z.clip.mov", b"clip")
    files.add("/mnt/ssd/tv1/ann.lee.z.dance.mp4", b"dance")  # loose, as hightower kept it
    local = tmp_path / "shrines" / "ann.lee"
    local.mkdir(parents=True)
    (local / "ann.lee.z.clip.mp4").write_bytes(b"clip")
    tv1 = FakeTvConnection(files={"ann.lee.a.mp4": b"slideshow", "ann.lee.z.clip.mp4": b"clip",
                                  "ann.lee.z.dance.mp4": b"dance", "bob.ng.z.clip.mp4": b"bob"})
    tv4 = FakeTvConnection(files={"ann.lee.a.mp4": b"slideshow"})
    _two_tvs(monkeypatch, tv1, tv4)
    screen = Screen(monkeypatch, asks=["ann"], confirms=[True, True], ticks=[Screen.DEFAULT],
                    chooses=[ANN, "/mnt/ssd/tv1/ann.lee/ann.lee.z.clip.mov", None])
    tvs.view_art(TvsSettings(shrines_dir=str(tmp_path / "shrines")))  # no ffmpeg needed
    screen.finished()
    assert set(tv1.files) == {"ann.lee.a.mp4", "ann.lee.z.dance.mp4", "bob.ng.z.clip.mp4"}
    assert tv1.files["ann.lee.a.mp4"] == b"slideshow"  # not made again
    assert not (local / "ann.lee.z.clip.mp4").exists()
    assert "TV 1: took off ann.lee.z.clip.mp4" in screen.shown("success")


def test_deleting_an_animation_takes_its_video_off(tmp_path, monkeypatch, pi_files, opened):
    frames = [Image.new("RGB", (16, 16), c) for c in ("red", "green")]
    frames[0].save(tmp_path / "spin.gif", save_all=True, append_images=frames[1:], duration=500)
    pi_files.connection.add("/mnt/ssd/tv1/ann.lee/ann.lee.z.spin.gif",
                            (tmp_path / "spin.gif").read_bytes())
    tv1 = FakeTvConnection(files={"ann.lee.a.mp4": b"slideshow", "ann.lee.z.spin.mp4": b"spin"})
    monkeypatch.setattr(tvs, "connect", lambda settings, numbers: {1: TvPi(tv1, 1, settings)})
    screen = Screen(monkeypatch, asks=["ann"], confirms=[True, True], ticks=[[1]],
                    chooses=[ANN, "/mnt/ssd/tv1/ann.lee/ann.lee.z.spin.gif"])
    tvs.view_art(TvsSettings(shrines_dir=str(tmp_path / "shrines")))
    screen.finished()  # no art left, so it stops by itself
    assert tv1.files == {"ann.lee.a.mp4": b"slideshow"}


def test_not_making_it_again_is_asked_about_when_leaving(tmp_path, monkeypatch, pi_files,
                                                         opened):
    _shrine_with_art(pi_files, tmp_path)
    screen = Screen(monkeypatch, asks=["ann"], confirms=[True, False, False],
                    chooses=[ANN, "/mnt/ssd/tv1/ann.lee/ann.lee.z.red.jpg", None])
    tvs.view_art(TvsSettings(shrines_dir=str(tmp_path / "shrines")))
    screen.finished()
    assert screen.questions[1] == \
        "Make ann.lee's slideshow again without ann.lee.z.red.jpg, and put it on the TVs?"
    assert "Don't forget" in screen.shown("warning")
    assert "use 'Add art to a shrine' for ann.lee, press Enter without adding any, then type y" \
        in screen.shown("warning")


def test_looking_without_deleting_changes_nothing(tmp_path, monkeypatch, pi_files, opened):
    files = _shrine_with_art(pi_files, tmp_path)
    before = dict(files.files)
    red = "/mnt/ssd/tv1/ann.lee/ann.lee.z.red.jpg"
    shown = []
    monkeypatch.setattr(tvs, "open_file", lambda path, what="it": shown.append(path.name))
    screen = Screen(monkeypatch, asks=["ann"], confirms=[False, None],
                    chooses=[ANN, red, red, None])
    tvs.view_art(TvsSettings(shrines_dir=str(tmp_path / "shrines")))
    screen.finished()
    assert files.files == before
    assert len(shown) == 2


def test_deleting_the_last_picture_takes_the_slideshow_off(tmp_path, monkeypatch, ffmpeg_path,
                                                           pi_files, opened):
    pi_files.connection.add("/mnt/ssd/tv1/ann.lee/ann.lee.z.red.jpg",
                            _picture(tmp_path / "red.jpg", "red"))
    tv1 = FakeTvConnection(files={"ann.lee.a.mp4": b"slideshow", "ann.lee.z.cat.mp4": b"cat"})
    monkeypatch.setattr(tvs, "connect", lambda settings, numbers: {1: TvPi(tv1, 1, settings)})
    screen = Screen(monkeypatch, asks=["ann"], confirms=[True, True], ticks=[[1]],
                    chooses=[ANN, "/mnt/ssd/tv1/ann.lee/ann.lee.z.red.jpg"])
    tvs.view_art(TvsSettings(shrines_dir=str(tmp_path / "shrines"), ffmpeg=str(ffmpeg_path)))
    screen.finished()
    assert tv1.files == {"ann.lee.z.cat.mp4": b"cat"}


def test_tv_names_of_a_piece():
    paths = ["/mnt/ssd/tv1/ann.lee/ann.lee.z.anim.gif", "/mnt/ssd/tv1/ann.lee/ann.lee.z.anim.mp4",
             "/mnt/ssd/tv1/ann.lee.z.Clip.avi", "/mnt/ssd/tv1/ann.lee/ann.lee.z.red.jpg"]
    # Worked out as Add art named them, as hightower had them, and as the old Windows panel
    # did. A name the animation's video could have too is only a maybe.
    assert tvs._tv_names("ann.lee", ANN, paths[1], paths, {}) == (
        {"ann.lee.z.anim-2.mp4", "ann.lee.z.anim.mp4.mp4"},
        {"ann.lee.z.anim.mp4": "ann.lee.z.anim.gif"})
    assert tvs._tv_names("ann.lee", ANN, paths[2], paths, {}) == (
        {"ann.lee.z.clip.mp4", "ann.lee.z.Clip.avi.mp4"}, {})
    # What pi-files says it became is sure.
    known = {"ann.lee.z.anim.gif": "ann.lee.z.anim.mp4"}
    assert tvs._tv_names("ann.lee", ANN, paths[0], paths, known) == ({"ann.lee.z.anim.mp4"}, {})


def test_deleting_takes_off_the_video_pi_files_says_it_became(tmp_path, monkeypatch, pi_files,
                                                              opened):
    # Make a shrine named the video donut.mp4, though Add art would have named it donut-2.
    files = pi_files.connection
    files.add("/mnt/ssd/tv1/ann.lee/ann.lee.z.donut.jpg", _picture(tmp_path / "d.jpg", "red"))
    donut = "/mnt/ssd/tv1/ann.lee/ann.lee.z.donut.mov"
    files.add(donut, b"donut video")
    files.add("/mnt/ssd/tv1/ann.lee/.tv-names.json",
              json.dumps({"ann.lee.z.donut.mov": "ann.lee.z.donut.mp4"}).encode())
    tv1 = FakeTvConnection(files={"ann.lee.a.mp4": b"slideshow", "ann.lee.z.donut.mp4": b"donut",
                                  "ann.lee.z.donut-2.mp4": b"another piece's"})
    monkeypatch.setattr(tvs, "connect", lambda settings, numbers: {1: TvPi(tv1, 1, settings)})
    screen = Screen(monkeypatch, asks=["ann"], chooses=[ANN, donut, None],
                    confirms=[True, True], ticks=[[1]])
    tvs.view_art(TvsSettings(shrines_dir=str(tmp_path / "shrines")))
    screen.finished()
    assert set(tv1.files) == {"ann.lee.a.mp4", "ann.lee.z.donut-2.mp4"}
    assert screen.questions[1] == "Take the video of ann.lee.z.donut.mov off the TVs?"
    assert tv_names_kept(pi_files) == {}


def test_a_video_name_another_piece_could_have_is_asked_about(tmp_path, monkeypatch, pi_files,
                                                              opened):
    files = pi_files.connection
    files.add("/mnt/ssd/tv1/ann.lee/ann.lee.z.anim.gif", b"an animation")
    anim = "/mnt/ssd/tv1/ann.lee/ann.lee.z.anim.mp4"
    files.add(anim, b"hightower's video")
    tv1 = FakeTvConnection(files={"ann.lee.z.anim.mp4": b"?", "ann.lee.z.anim-2.mp4": b"?"})
    monkeypatch.setattr(tvs, "connect", lambda settings, numbers: {1: TvPi(tv1, 1, settings)})
    screen = Screen(monkeypatch, asks=["ann"], chooses=[ANN, anim, None],
                    confirms=[True, False, True], ticks=[[1]])
    tvs.view_art(TvsSettings(shrines_dir=str(tmp_path / "shrines")))
    screen.finished()
    assert "or of ann.lee.z.anim.gif, and it can't tell which" in screen.questions[1]
    assert set(tv1.files) == {"ann.lee.z.anim.mp4"}  # they said to leave it


def _a_shrine_with_a_clip(pi_files):
    clip = "/mnt/ssd/tv1/ann.lee/ann.lee.z.clip.mov"
    pi_files.connection.add(clip, b"clip")
    pi_files.connection.add("/mnt/ssd/tv1/ann.lee/ann.lee.z.red.jpg", b"red")
    return clip


def test_the_reminder_stays_if_a_tv_isnt_changed(tmp_path, monkeypatch, pi_files, opened):
    clip = _a_shrine_with_a_clip(pi_files)
    monkeypatch.setattr(tvs, "connect", lambda settings, numbers: {})  # TV 1 is off
    screen = Screen(monkeypatch, asks=["ann"], chooses=[ANN, clip, None],
                    confirms=[True, True, False], ticks=[[1]])
    tvs.view_art(TvsSettings(shrines_dir=str(tmp_path / "shrines")))
    screen.finished()
    assert "Not changed on TV 1." in screen.shown("warning")
    assert screen.questions[2] == \
        "Before you go: Take the video of ann.lee.z.clip.mov off the TVs?"
    assert "Don't forget" in screen.shown("warning")
    assert "To take the video of ann.lee.z.clip.mov off them, use 'See, copy or delete the " \
        "videos on a TV'" in screen.shown("warning")
    assert "Add art" not in screen.shown("warning")  # that wouldn't take it off


def test_the_reminder_stays_if_its_cancelled(tmp_path, monkeypatch, pi_files, opened):
    clip = _a_shrine_with_a_clip(pi_files)

    def q_at_the_password(settings, numbers):
        raise KeyboardInterrupt

    monkeypatch.setattr(tvs, "connect", q_at_the_password)
    screen = Screen(monkeypatch, asks=["ann"], chooses=[ANN, clip, None],
                    confirms=[True, True, False], ticks=[[1]])
    tvs.view_art(TvsSettings(shrines_dir=str(tmp_path / "shrines")))
    screen.finished()
    assert "Cancelled." in screen.shown("warning")
    assert "Don't forget" in screen.shown("warning")


def test_the_reminder_stays_if_getting_a_piece_is_stopped(tmp_path, monkeypatch, pi_files,
                                                          opened):
    clip = _a_shrine_with_a_clip(pi_files)
    fetch = pi_files.fetch
    monkeypatch.setattr(pi_files, "fetch", lambda path, *a: fetch(path, *a) if path == clip
                        else (_ for _ in ()).throw(KeyboardInterrupt))  # Ctrl+C
    screen = Screen(monkeypatch, asks=["ann"],
                    chooses=[ANN, clip, "/mnt/ssd/tv1/ann.lee/ann.lee.z.red.jpg"],
                    confirms=[True, False, False])
    tvs.view_art(TvsSettings(shrines_dir=str(tmp_path / "shrines")))
    screen.finished()
    assert "Cancelled." in screen.shown("error")
    assert "Don't forget" in screen.shown("warning")


def test_remaking_asks_before_leaving_out_unreadable_names(tmp_path, monkeypatch, ffmpeg_path,
                                                           pi_files):
    files = pi_files.connection
    files.add("/mnt/ssd/tv1/ann.lee/ann.lee.z.red.jpg", _picture(tmp_path / "red.jpg", "red"))
    files.add("/mnt/ssd/tv1/ann.lee/Jos\ufffd.png")
    files.not_text = {"/mnt/ssd/tv1/ann.lee/Jos\ufffd.png"}
    screen = Screen(monkeypatch, confirms=[False])
    assert not tvs._after_deleting(TvsSettings(ffmpeg=str(ffmpeg_path)), pi_files, ANN, True,
                                   set())
    screen.finished()
    assert screen.questions == ["Its slideshow will be made without those. Carry on?"]


def test_a_piece_is_only_got_once_and_not_left_behind(tmp_path, monkeypatch, pi_files, opened):
    _shrine_with_art(pi_files, tmp_path)
    red = "/mnt/ssd/tv1/ann.lee/ann.lee.z.red.jpg"
    got = []
    fetch = pi_files.fetch
    monkeypatch.setattr(pi_files, "fetch", lambda path, *a: got.append(path) or fetch(path, *a))
    monkeypatch.setattr(tvs.tempfile, "gettempdir", lambda: str(tmp_path / "temp"))
    leftover = tmp_path / "temp" / tvs.VIEWING / "01-old.mp4"
    leftover.parent.mkdir(parents=True)
    leftover.write_bytes(b"from last time")
    screen = Screen(monkeypatch, asks=["ann"], confirms=[False, False],
                    chooses=[ANN, red, red, None])
    tvs.view_art(TvsSettings(shrines_dir=str(tmp_path / "shrines")))
    screen.finished()
    assert got == [red]
    assert not (tmp_path / "temp" / tvs.VIEWING).exists()


def test_seeing_art_needs_pi_files(monkeypatch, no_pi_files):
    screen = Screen(monkeypatch)
    tvs.view_art(TvsSettings())
    assert "needs pi-files" in screen.shown("error")
