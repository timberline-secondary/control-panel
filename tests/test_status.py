import pytest
from fakes import FakeFilesConnection

from control_panel import ui
from control_panel.config import Config
from control_panel.panels import status
from control_panel.ssh import CommandResult

DF = "/dev/sda1 {blocks} 10 {free} 1% {mount}"


def pi_named(name):
    return next(pi for pi in status.pis(Config()) if pi.name == name)


@pytest.mark.parametrize("name, output, well, said", [
    ("TV 1", "up 3 days, 2 hours, 5 minutes\n" + DF.format(blocks=9, free=5_000_000, mount="/")
     + "\nyes\n", True, "up 3 days, 2 hours, 5.1 GB free on its SD card, slideshow running"),
    ("TV 2", "up 1 hour\n" + DF.format(blocks=9, free=100_000, mount="/") + "\n", False,
     "up 1 hour, 102.4 MB free on its SD card (nearly full), slideshow NOT running"),
    ("pi-files", "up 9 days\n" + DF.format(blocks=9, free=900_000_000, mount="/mnt/ssd")
     + "\n", True, "up 9 days, 921.6 GB free on its SSD"),
    ("pi-themes", "up 2 days\n" + DF.format(blocks=9, free=9_000_000, mount="/") + "\nyes\n",
     False, "up 2 days, its USB stick isn't plugged in (or mounted), keypad theme player running"),
    ("pi-projector", "up 2 days\n\n", False,
     "up 2 days, couldn't see /home/pi/jsclock, clock NOT running"),
])
def test_what_each_pi_says(name, output, well, said):
    assert status.parse(output, pi_named(name)) == (well, said)


def test_check_the_pis(monkeypatch):
    shown = []
    for kind in ["info", "success", "warning", "error"]:
        monkeypatch.setattr(ui, kind, lambda text, kind=kind: shown.append((kind, text)))
    monkeypatch.setattr(ui, "pause", lambda *a: None)
    monkeypatch.setattr(ui, "clear", lambda: None)
    monkeypatch.setattr(status.tvs, "_find_host", lambda host, port: host)
    monkeypatch.setattr(status.tvs, "_reach_problem", lambda host, port: (
        "couldn't reach pi-tv3.hackerspace.tbl. Is it plugged in and turned on?"
        if "tv3" in host else None))

    class Pi(FakeFilesConnection):
        def run(self, command, **k):
            return CommandResult(0, "up 1 day\n" + DF.format(blocks=9, free=9_000_000,
                                                             mount="/mnt") + "\nyes\n")
    monkeypatch.setattr(status.login, "connect", lambda settings: Pi())
    status.run(Config())
    lines = [(kind, text.split()[0] + " " + text.split()[1]) for kind, text in shown[1:]]
    assert len(lines) == 7
    assert ("error", "TV 3") in lines
    assert ("success", "TV 1") in lines and ("success", "pi-projector up") in lines
