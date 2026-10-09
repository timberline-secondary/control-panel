import hashlib
import io
import os
import subprocess
import sys
import zipfile

import pytest
from conftest import DOWNLOAD_ENV_VAR

from control_panel import ffmpeg
from control_panel.ffmpeg import Build, FfmpegError


@pytest.fixture
def no_downloads_yet(tmp_path, monkeypatch):
    monkeypatch.setattr(ffmpeg, "data_dir", lambda: tmp_path / "data")
    return tmp_path


def fake_build(tmp_path, monkeypatch, *, corrupt=False):
    """A pretend ffmpeg download (a zip with the program inside) served from a file."""
    member = "imageio_ffmpeg/binaries/ffmpeg-test.exe"
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as wheel:
        wheel.writestr(member, b"pretend ffmpeg")
    package = tmp_path / "ffmpeg.whl"
    package.write_bytes(data.getvalue())
    sha256 = hashlib.sha256(b"not it" if corrupt else data.getvalue()).hexdigest()
    build = Build(package.as_uri(), len(data.getvalue()), sha256, member)
    monkeypatch.setattr(ffmpeg, "build_for_this_computer", lambda: build)
    return build


def test_the_config_setting_comes_first(tmp_path):
    program = tmp_path / "ffmpeg.exe"
    program.write_bytes(b"")
    assert ffmpeg.find(str(program)) == program
    with pytest.raises(FfmpegError, match="no such file"):
        ffmpeg.find(str(tmp_path / "missing.exe"))


def test_an_installed_ffmpeg_needs_h264(no_downloads_yet, monkeypatch):
    monkeypatch.setattr(ffmpeg.sys, "platform", "linux")  # Windows never looks (test below)
    monkeypatch.setattr(ffmpeg.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(ffmpeg, "_makes_h264", lambda path: False)
    assert ffmpeg.find() is None
    monkeypatch.setattr(ffmpeg, "_makes_h264", lambda path: True)
    assert str(ffmpeg.find()).replace("\\", "/") == "/usr/bin/ffmpeg"


def test_download_unpacks_and_keeps_it(no_downloads_yet, monkeypatch):
    fake_build(no_downloads_yet, monkeypatch)
    seen = []
    path = ffmpeg.download(lambda done, total: seen.append((done, total)))
    assert path == no_downloads_yet / "data" / "ffmpeg-test.exe"
    assert path.read_bytes() == b"pretend ffmpeg"
    assert seen[-1][0] == seen[-1][1]
    if sys.platform != "win32":
        assert os.access(path, os.X_OK)
    assert sorted(p.name for p in path.parent.iterdir()) == ["ffmpeg-test.exe"]  # no leftovers
    monkeypatch.setattr(ffmpeg.shutil, "which", lambda name: None)
    assert ffmpeg.find() == path


def test_a_download_that_doesnt_match_is_thrown_away(no_downloads_yet, monkeypatch):
    fake_build(no_downloads_yet, monkeypatch, corrupt=True)
    with pytest.raises(FfmpegError, match="wasn't the file it should be"):
        ffmpeg.download()
    assert list((no_downloads_yet / "data").iterdir()) == []


def test_no_download_for_other_computers(no_downloads_yet, monkeypatch):
    monkeypatch.setattr(ffmpeg, "build_for_this_computer", lambda: None)
    with pytest.raises(FfmpegError, match="Install ffmpeg yourself"):
        ffmpeg.download()


def test_the_pinned_downloads_are_for_windows_and_linux():
    assert set(ffmpeg.BUILDS) == {("win32", "amd64"), ("linux", "x86_64")}
    for build in ffmpeg.BUILDS.values():
        assert build.url.startswith("https://files.pythonhosted.org/")
        assert len(build.sha256) == 64


@pytest.mark.skipif(not os.environ.get(DOWNLOAD_ENV_VAR), reason="downloads ~30 MB")
@pytest.mark.skipif(ffmpeg.build_for_this_computer() is None, reason="no download for this OS")
def test_the_real_download_works(no_downloads_yet):
    path = ffmpeg.download()
    assert ffmpeg._makes_h264(path)


@pytest.mark.parametrize("stderr, expected", [
    ("Input #0, mov,mp4\n  Duration: 00:01:02.50, start: 0.000000, bitrate: 1 kb/s\n"
     "  Stream #0:0[0x1](und): Video: h264 (High), yuv420p, 1920x1080, 25 fps\n",
     ffmpeg.Probe(True, 62.5)),
    ("Input #0, mp3\n  Duration: 00:03:00.00, start: 0.0, bitrate: 128 kb/s\n"
     "  Stream #0:0: Audio: mp3, 44100 Hz\n"
     "  Stream #0:1: Video: mjpeg (Baseline), 500x500, 90k tbr (attached pic)\n",
     ffmpeg.Probe(False, 180.0)),  # an mp3's cover art isn't a video
    ("notes.txt: Invalid data found when processing input\n", ffmpeg.Probe(False, None)),
])
def test_probe(monkeypatch, tmp_path, stderr, expected):
    monkeypatch.setattr(ffmpeg.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 1, "", stderr))
    assert ffmpeg.probe(tmp_path / "ffmpeg", tmp_path / "file") == expected


def test_run_reports_progress(ffmpeg_path, tmp_path):
    seen = []
    ffmpeg.run(ffmpeg_path, ["-f", "lavfi", "-i", "color=black:size=64x64:rate=25:duration=2",
                             str(tmp_path / "out.mp4")], seconds=2, progress=seen.append)
    assert (tmp_path / "out.mp4").stat().st_size > 0
    assert seen[-1] == 1.0 and seen == sorted(seen)


def test_run_raises_ffmpegs_own_message(ffmpeg_path, tmp_path):
    with pytest.raises(FfmpegError, match="No such file|does not exist"):
        ffmpeg.run(ffmpeg_path, ["-i", str(tmp_path / "missing.mov"), str(tmp_path / "x.mp4")])


def test_windows_only_uses_the_checked_download(no_downloads_yet, monkeypatch):
    # Windows would also find an ffmpeg.bat in the current folder (often Downloads).
    monkeypatch.setattr(ffmpeg.sys, "platform", "win32")
    monkeypatch.setattr(ffmpeg.shutil, "which", lambda name: pytest.fail("looked on PATH"))
    assert ffmpeg.find() is None


def _raw_frames(count):
    return (bytes([value]) * (64 * 48 * 3) for value in range(count))


RAW_64x48 = ["-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "64x48", "-r", "25", "-i", "pipe:0"]


def test_encode_frames(ffmpeg_path, tmp_path):
    seen = []
    ffmpeg.encode(ffmpeg_path, _raw_frames(50), 50, [*RAW_64x48, str(tmp_path / "out.mp4")],
                  progress=seen.append)
    assert ffmpeg.probe(ffmpeg_path, tmp_path / "out.mp4").duration == pytest.approx(2, abs=0.1)
    assert seen == [0.5, 1.0, 1.0]


def test_encode_stops_ffmpeg_if_a_frame_cant_be_made(ffmpeg_path, tmp_path):
    def frames():
        yield from _raw_frames(10)
        raise ValueError("a damaged slide")
    with pytest.raises(ValueError):
        ffmpeg.encode(ffmpeg_path, frames(), 50, [*RAW_64x48, str(tmp_path / "out.mp4")])


def test_encode_reports_ffmpegs_error(ffmpeg_path, tmp_path):
    with pytest.raises(FfmpegError, match="nope|Unknown encoder|not found"):
        ffmpeg.encode(ffmpeg_path, _raw_frames(500), 500,
                      [*RAW_64x48, "-c:v", "nope", str(tmp_path / "out.mp4")])
