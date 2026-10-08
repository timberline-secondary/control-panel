import hashlib
import http.server
import json
import signal
import sys
import threading

import pytest

from control_panel import app, updater

NEW_EXE = b"MZ new version " * 1000
OLD_EXE = b"MZ old version"


@pytest.fixture(autouse=True)
def let_go_of_the_exe():
    yield
    updater._unprotect()  # or Windows can't delete the test's temporary folder


@pytest.fixture
def github(monkeypatch):
    """A local stand-in for GitHub. Set .routes[path] = (status, body bytes, extra headers)."""
    routes = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            status, body, headers = routes.get(self.path, (404, b"not found", {}))
            self.send_response(status)
            headers = {"Content-Length": str(len(body)), **headers}
            for name, value in headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body[: int(headers.get("X-Send-Only", len(body)))])

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    monkeypatch.setenv(updater.URL_ENV_VAR, f"{base}/latest")

    class GitHub:
        url = base

        def release(self, tag="v9.0.0", exe=NEW_EXE, digest="right", **asset):
            asset = {"name": "control-panel.exe", "size": len(exe),
                     "browser_download_url": f"{base}/download/control-panel.exe", **asset}
            if digest:
                checksum = hashlib.sha256(exe if digest == "right" else b"other").hexdigest()
                asset["digest"] = f"sha256:{checksum}"
            routes["/latest"] = (200, json.dumps({"tag_name": tag, "assets": [asset]}).encode(), {})
            routes["/download/control-panel.exe"] = (200, exe, {})

    GitHub.routes = routes
    yield GitHub()
    server.shutdown()


@pytest.mark.parametrize("text, expected", [
    ("v0.3.0", (0, 3, 0)), ("1.20.3", (1, 20, 3)), (" v2.0.1 ", (2, 0, 1)),
    ("v2.0", None), ("latest", None), ("v1.2.3-beta", None),
])
def test_parse_version(text, expected):
    assert updater.parse_version(text) == expected


def test_latest_release(github):
    github.release()
    release = updater.latest_release()
    assert (release.tag, release.version, release.size) == ("v9.0.0", (9, 0, 0), len(NEW_EXE))
    assert release.sha256 == hashlib.sha256(NEW_EXE).hexdigest()


def test_latest_release_without_a_checksum(github):
    github.release(digest=None)
    assert updater.latest_release().sha256 is None


@pytest.mark.parametrize("body", [
    b"not json",
    b"[]",
    json.dumps({"tag_name": "v9.0.0", "assets": []}).encode(),  # no exe attached
    json.dumps({"tag_name": "nightly", "assets": [
        {"name": "control-panel.exe", "size": 1, "browser_download_url": "x"}]}).encode(),
    json.dumps({"tag_name": "v9.0.0", "assets": [
        {"name": "control-panel.exe", "size": 1, "browser_download_url": "x",
         "digest": 5}]}).encode(),
])
def test_latest_release_ignores_odd_replies(github, body):
    github.routes["/latest"] = (200, body, {})
    assert updater.latest_release() is None


def test_latest_release_when_github_says_no(github):
    github.routes["/latest"] = (403, b"rate limited", {})
    assert updater.latest_release() is None


def test_latest_release_when_offline(monkeypatch):
    monkeypatch.setenv(updater.URL_ENV_VAR, "http://127.0.0.1:9/latest")  # nothing listens here
    assert updater.latest_release(timeout=2) is None


def test_is_newer():
    release = updater.Release("v0.3.0", (0, 3, 0), "x", 1, None)
    assert updater.is_newer(release, current="0.2.9")
    assert not updater.is_newer(release, current="0.3.0")
    assert not updater.is_newer(release, current="0.10.0")  # numbers, not text, are compared


@pytest.fixture
def exe(tmp_path):
    path = tmp_path / "control-panel.exe"
    path.write_bytes(OLD_EXE)
    return path


def test_install_swaps_the_exe(github, exe):
    github.release()
    updater.install(updater.latest_release(), exe)
    assert exe.read_bytes() == NEW_EXE
    assert exe.with_name("control-panel.exe.old").read_bytes() == OLD_EXE
    assert not exe.with_name("control-panel.exe.new").exists()


def test_install_replaces_a_leftover_old_exe(github, exe):
    exe.with_name("control-panel.exe.old").write_bytes(b"older still")
    github.release()
    updater.install(updater.latest_release(), exe)
    assert exe.with_name("control-panel.exe.old").read_bytes() == OLD_EXE


@pytest.mark.parametrize("problem, message", [
    ({"digest": "wrong"}, "didn't match"),
    ({"size": 5}, "didn't match"),
    ({"browser_download_url": "http://127.0.0.1:9/gone.exe"}, "releases/latest"),
])
def test_failed_install_leaves_the_exe_alone(github, exe, problem, message):
    github.release(**problem)
    with pytest.raises(updater.UpdateError, match=message):
        updater.install(updater.latest_release(), exe)
    assert exe.read_bytes() == OLD_EXE
    assert sorted(p.name for p in exe.parent.iterdir()) == ["control-panel.exe"]


def test_cut_off_download_is_refused(github, exe):
    github.release(digest=None)
    status, body, _ = github.routes["/download/control-panel.exe"]
    github.routes["/download/control-panel.exe"] = (status, body, {"X-Send-Only": "100"})
    with pytest.raises(updater.UpdateError):
        updater.install(updater.latest_release(), exe)
    assert exe.read_bytes() == OLD_EXE


def test_remove_leftovers(exe):
    for name in ["control-panel.exe.old", "control-panel.exe.old-1234", "control-panel.exe.new",
                 "notes.txt"]:
        exe.with_name(name).write_bytes(b"x")
    updater.remove_leftovers(exe)
    assert sorted(p.name for p in exe.parent.iterdir()) == ["control-panel.exe", "notes.txt"]


def test_run_new_version_as_a_fresh_copy_and_wait(monkeypatch, exe):
    started = {}

    class Process:
        def __init__(self, args, env):
            started.update(args=args, env=env)

        def wait(self):
            started["ctrl_c_while_waiting"] = signal.getsignal(signal.SIGINT)
            return 7

    monkeypatch.setattr(updater.subprocess, "Popen", Process)
    before = signal.getsignal(signal.SIGINT)
    assert updater.run_new_version(exe, ["--config", "lab.toml"]) == 7
    assert started["args"] == [str(exe), "--config", "lab.toml"]
    assert started["env"]["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
    assert started["ctrl_c_while_waiting"] is signal.SIG_IGN  # it's the new version's Ctrl+C
    assert signal.getsignal(signal.SIGINT) is before


def test_install_returns_the_old_exe_for_roll_back(github, exe):
    github.release()
    old = updater.install(updater.latest_release(), exe)
    assert old.read_bytes() == OLD_EXE
    updater.roll_back(exe, old)
    assert exe.read_bytes() == OLD_EXE and not old.exists()


def test_protect_holds_the_exe_until_install_moves_it(github, exe, monkeypatch):
    held_during_move = []
    move_aside = updater._move_aside
    monkeypatch.setattr(updater, "_move_aside",
                        lambda path: held_during_move.append(updater._held_exe) or move_aside(path))
    updater.protect(exe)
    assert updater._held_exe is not None and not updater._held_exe.closed
    github.release()
    updater.install(updater.latest_release(), exe)
    assert held_during_move == [None]  # let go of it first, or Windows won't move it


def test_install_when_another_window_has_the_exe_open(github, exe, monkeypatch):
    def replace(source, destination):
        error = PermissionError(13, "The process cannot access the file")
        error.winerror = updater.ERROR_SHARING_VIOLATION
        raise error

    monkeypatch.setattr(updater.os, "replace", replace)
    github.release()
    with pytest.raises(updater.UpdateError, match="open in another window"):
        updater.install(updater.latest_release(), exe)
    assert exe.read_bytes() == OLD_EXE
    assert sorted(p.name for p in exe.parent.iterdir()) == ["control-panel.exe"]
    assert updater._held_exe is not None  # holding it again


def test_running_exe(monkeypatch, tmp_path):
    monkeypatch.delenv(updater.URL_ENV_VAR, raising=False)
    monkeypatch.setattr(updater.sys, "executable", str(tmp_path / "control-panel.exe"))
    assert updater.running_exe() is None  # running from source
    monkeypatch.setattr(updater.sys, "frozen", True, raising=False)
    monkeypatch.setattr(updater.sys, "platform", "win32")
    assert updater.running_exe() == tmp_path / "control-panel.exe"
    monkeypatch.setattr(updater.sys, "platform", "linux")
    assert updater.running_exe() is None  # releases only have a Windows exe


# --- What the app does with it ------------------------------------------------------------

@pytest.fixture
def frozen_app(monkeypatch, exe):
    """The app as control-panel.exe, with prompts answered from `answers`."""
    monkeypatch.setattr(updater, "running_exe", lambda: exe)
    record = {"confirm": True, "ran": None, "messages": [], "paused": 0}
    monkeypatch.setattr(app.ui, "confirm", lambda prompt, **k: record["confirm"])
    for kind in ["info", "success", "error"]:
        monkeypatch.setattr(app.ui, kind, record["messages"].append)
    monkeypatch.setattr(updater, "run_new_version",
                        lambda exe, args: record.update(ran=exe) or 5)
    monkeypatch.setattr(app.ui, "pause",
                        lambda *a: record.update(paused=record["paused"] + 1))
    return record


def test_offer_update_yes(github, exe, frozen_app):
    github.release()
    assert app._offer_update() == 5  # the new version's exit code
    assert exe.read_bytes() == NEW_EXE and frozen_app["ran"] == exe


def test_offer_update_no(github, exe, frozen_app):
    github.release()
    frozen_app["confirm"] = False
    assert app._offer_update() is None
    assert exe.read_bytes() == OLD_EXE and frozen_app["ran"] is None


def test_no_offer_when_up_to_date(github, exe, frozen_app, monkeypatch):
    github.release(tag="v0.0.1")
    monkeypatch.setattr(app.ui, "confirm", lambda *a, **k: pytest.fail("shouldn't ask"))
    assert app._offer_update() is None


def test_no_offer_when_running_from_source(monkeypatch):
    monkeypatch.setattr(updater, "running_exe", lambda: None)
    monkeypatch.setattr(updater, "latest_release", lambda **k: pytest.fail("shouldn't check"))
    assert app._offer_update() is None


def test_update_flag(github, exe, frozen_app):
    github.release()
    assert app.main(["--update"]) == 0
    assert exe.read_bytes() == NEW_EXE
    assert frozen_app["ran"] is None  # --update just updates
    github.release(tag="v0.0.1")
    assert app.main(["--update"]) == 0
    assert "You have the latest version" in frozen_app["messages"][-1]


def test_update_flag_from_source(monkeypatch, capsys):
    monkeypatch.setattr(updater, "running_exe", lambda: None)
    assert app.main(["--update"]) == 1
    assert "Only control-panel.exe can update itself" in capsys.readouterr().out


def test_install_keeps_the_exe_runnable(github, exe):
    exe.chmod(0o755)
    github.release()
    updater.install(updater.latest_release(), exe)
    assert exe.stat().st_mode & 0o111 or sys.platform == "win32"


def test_failed_update_waits_so_the_message_can_be_read(github, exe, frozen_app):
    github.release(digest="wrong")
    assert app._offer_update() is None
    assert "didn't match" in frozen_app["messages"][-1]
    assert frozen_app["paused"] == 1
    assert exe.read_bytes() == OLD_EXE


def test_new_version_that_wont_start_is_rolled_back(github, exe, frozen_app, monkeypatch):
    def blocked(exe, args):
        raise OSError(225, "Operation did not complete because the file contains a virus")

    monkeypatch.setattr(updater, "run_new_version", blocked)
    github.release()
    assert app._offer_update() is None  # carries on with this version
    assert exe.read_bytes() == OLD_EXE
    assert sorted(p.name for p in exe.parent.iterdir()) == ["control-panel.exe"]
    assert "wouldn't start the new version" in frozen_app["messages"][-1]
    assert frozen_app["paused"] == 1


def test_new_version_wont_start_and_old_cant_be_put_back(github, exe, frozen_app, monkeypatch):
    monkeypatch.setattr(updater, "run_new_version",
                        lambda exe, args: (_ for _ in ()).throw(OSError("blocked")))
    monkeypatch.setattr(updater, "roll_back",
                        lambda exe, old: (_ for _ in ()).throw(OSError("in use")))
    github.release()
    assert app._offer_update() == 1  # stop, rather than run from a replaced exe
    assert "download it again" in frozen_app["messages"][-1]
