import datetime
import json

import pytest
from fakes import FakeFilesConnection

from control_panel import ui
from control_panel.config import ClockSettings
from control_panel.panels import clock
from control_panel.panels.clock import ClockError, ClockPi
from control_panel.ssh import CommandResult

THURSDAY = datetime.date(2026, 10, 22)


def period(text, start, colour="default", kind="Block"):
    return {"text": text, "colour": colour, "start": start, "end": "", "type": kind}


def usual():
    return {"description": "4-Block", "days": [1, 2, 4, 5], "values": [
        period("Start of the day", "00:00", "239, 52, 24", "Break"),
        period("A Block", "08:50"),
        period("Lunch", "11:35", "239, 52, 24", "Lunch"),
        period("School is Over", "15:00", "239, 52, 24", "Break")]}


def flex():
    return {"description": "Flex", "days": [3], "values": [
        period("Start of the day", "00:00", "239, 52, 24", "Break"),
        period("Flex Time", "08:50", "255, 255, 102")]}


def weekend():
    return {"description": "Weekend", "days": [0, 6], "values": [
        period("Weekend", "00:00", "239, 52, 24", "Break")]}


SCHEDULE_DATA = "const scheduleStr = `\n" + json.dumps([usual(), flex(), weekend()]) + "\n`\n"


class Screen:
    """Scripted answers for the questions, and a record of what was shown."""

    def __init__(self, monkeypatch, asks=(), chooses=(), confirms=(), picks=()):
        self.shown = []
        self.asks, self.chooses = iter(asks), iter(chooses)
        self.confirms, self.picks = iter(confirms), iter(picks)
        for kind in ["info", "success", "warning", "error"]:
            monkeypatch.setattr(ui, kind, lambda text, kind=kind: self.shown.append((kind, text)))
        monkeypatch.setattr(ui, "ask", lambda prompt, **k: next(self.asks))
        monkeypatch.setattr(ui, "confirm", lambda prompt, **k: next(self.confirms))
        monkeypatch.setattr(ui, "choose_many", lambda choices, question="": next(self.picks))
        monkeypatch.setattr(ui, "choose", self._choose)

    def _choose(self, choices, question="", default=None):
        answer = next(self.chooses)
        if callable(answer):  # pick by looking at the choices
            answer = answer(choices)
        offered = [c[1] if isinstance(c, tuple) else c for c in choices]
        assert answer is None or any(answer is o or answer == o for o in offered), \
            f"{answer!r} isn't one of the choices for {question!r}"
        return answer

    def finished(self):
        for left in (self.asks, self.chooses, self.confirms, self.picks):
            assert next(left, "done") == "done", "not every scripted answer was asked for"

    def text(self, kind=None):
        return "\n".join(t for k, t in self.shown if kind in (None, k))


def named(name):
    """Choose the schedule (or period) with this name."""
    return lambda choices: next(value for title, value in choices if title.startswith(name))


@pytest.fixture
def pi(monkeypatch):
    """pi-projector with the clock on it, as it is before any schedules are saved."""
    monkeypatch.setattr(clock, "_today", lambda: THURSDAY)
    connection = FakeFilesConnection()
    connection.add("/home/pi/jsclock/index.html", b"<html>")
    connection.add("/home/pi/jsclock/clock_script.js", b"... localSchedules ...")
    connection.add("/home/pi/jsclock/schedule_data.js", SCHEDULE_DATA.encode())
    return ClockPi(connection, ClockSettings())


def saved(pi):
    return clock.parse_saved(pi.connection.files["/home/pi/jsclock/schedules.js"].decode())


def restarts(pi):
    return [c for c, _ in pi.connection.commands if "launcher.sh" in c]


# --- Helpers -------------------------------------------------------------------------------

def test_reading_the_clocks_own_schedules_and_saved_ones():
    schedules = clock.parse_defaults(SCHEDULE_DATA)
    assert [s["description"] for s in schedules] == ["4-Block", "Flex", "Weekend"]
    assert clock.parse_saved(clock.render(schedules)) == schedules
    with pytest.raises(ValueError):
        clock.parse_saved("const localSchedules = [{,];")


@pytest.mark.parametrize("text, expected", [
    ("8:50", "08:50"), ("08:50", "08:50"), ("13:45", "13:45"), ("1:45 pm", "13:45"),
    ("12:05am", "00:05"), ("12:30 PM", "12:30"), ("850", "08:50"), ("0:00", "00:00"),
    ("24:00", None), ("8:60", None), ("13:00 pm", None), ("noon", None),
])
def test_parse_time(text, expected):
    assert clock.parse_time(text) == expected


@pytest.mark.parametrize("text, expected", [
    ("2026-10-30", ["2026-10-30"]),
    ("10-30, 11/2", ["2026-10-30", "2026-11-02"]),
    ("9-1", ["2027-09-01"]),  # gone by this year, so next year's
    ("today tomorrow", ["2026-10-22", "2026-10-23"]),
    ("2026-02-30", None), ("next week", None), ("", None),
])
def test_parse_dates(text, expected):
    assert clock.parse_dates(text, THURSDAY) == expected


def test_problems_that_would_stop_the_clock_showing_it():
    assert clock.problems(usual()) == []
    broken = {"description": "", "days": [], "values": [
        period("A", "08:50", "red"), period("B", "08:50"), period("", "9:5")]}
    assert clock.problems(broken) == [
        "It needs a name.", "A's colour isn't red, green, blue numbers like 239, 52, 24.",
        "A period has no name.", "A period doesn't have a start time like 08:50.",
        "The first period has to start at 00:00 (e.g. 'Start of the day'), so there's always "
        "something to show.", "More than one period starts at 08:50."]


def test_tidy_puts_periods_in_order_each_ending_when_the_next_starts():
    schedule = clock.tidy({"description": "x", "days": [3, 1, 3], "dates": [], "values": [
        period("B", "10:00"), period("Start", "00:00"), period("A", "08:50")]})
    assert [(p["text"], p["start"], p["end"]) for p in schedule["values"]] == [
        ("Start", "00:00", "08:50"), ("A", "08:50", "10:00"), ("B", "10:00", "00:00")]
    assert schedule["days"] == [1, 3] and "dates" not in schedule


def test_the_schedule_for_a_day_is_picked_as_the_clock_does():
    schedules = [usual(), flex(), weekend()]
    assert clock.for_date(schedules, THURSDAY)["description"] == "4-Block"
    assert clock.for_date(schedules, THURSDAY + datetime.timedelta(days=6))["description"] \
        == "Flex"
    schedules[1]["dates"] = ["2026-10-22"]
    assert clock.for_date(schedules, THURSDAY)["description"] == "Flex"  # dates come first


def test_describe(monkeypatch):
    monkeypatch.setattr(ui, "_COLOURS", False)
    lines = clock.describe({**flex(), "dates": ["2026-10-01", "2026-10-30"]}, THURSDAY)
    assert lines == [
        "Flex", "  Used on Wednesdays, and on Friday, October 30, 2026.",
        "    00:00  Start of the day         Break       Red",
        "    08:50  Flex Time                Block       Yellow"]


# --- On pi-projector -----------------------------------------------------------------------

def test_load_uses_the_saved_schedules_when_there_are_some(pi):
    assert [s["description"] for s in pi.load()] == ["4-Block", "Flex", "Weekend"]
    pi.connection.add("/home/pi/jsclock/schedules.js", clock.render([weekend()]).encode())
    assert [s["description"] for s in pi.load()] == ["Weekend"]
    pi.connection.add("/home/pi/jsclock/schedules.js", b"const localSchedules = [oops];")
    with pytest.raises(ClockError, match="Go back to an earlier version"):
        pi.load()


def test_without_the_clock_there(pi):
    pi.settings = ClockSettings(clock_dir="/home/pi/elsewhere")
    with pytest.raises(ClockError, match="The clock isn't in /home/pi/elsewhere"):
        pi.load()


def test_save_keeps_a_copy_of_the_schedules_from_before(pi, monkeypatch):
    stamps = iter(["20261022-090000", "20261022-091500"])
    monkeypatch.setattr(clock.datetime, "datetime", type("Now", (), {
        "now": staticmethod(lambda: type("T", (), {"strftime": lambda self, f: next(stamps)})())
    }))
    pi.save([usual(), flex()])
    pi.save([usual()])
    assert [s["description"] for s in saved(pi)] == ["4-Block"]
    assert pi.backups() == ["schedules-20261022-091500.js", "schedules-20261022-090000.js"]
    first, second = (pi.read_backup(name) for name in reversed(pi.backups()))
    assert [s["description"] for s in first] == ["4-Block", "Flex", "Weekend"]  # its own
    assert [s["description"] for s in second] == ["4-Block", "Flex"]


def test_save_refuses_a_schedule_the_clock_cant_show(pi):
    broken = {**flex(), "values": [period("Flex Time", "08:50")]}
    with pytest.raises(ClockError, match="has to start at 00:00"):
        pi.save([usual(), broken])
    assert "/home/pi/jsclock/schedules.js" not in pi.connection.files


# --- Menu actions --------------------------------------------------------------------------

def test_see_the_schedules(pi, monkeypatch):
    screen = Screen(monkeypatch)
    clock.see_schedules(pi)
    assert "Today (Thursday, October 22, 2026) the clock shows 4-Block." in screen.text()
    assert clock.name({"description": "Weekend."}) == "Weekend"


def test_use_a_schedule_on_certain_dates(pi, monkeypatch):
    screen = Screen(monkeypatch, asks=["today, 10-30"], chooses=[named("Flex")])
    clock.set_dates(pi)
    screen.finished()
    flex_saved = next(s for s in saved(pi) if s["description"] == "Flex")
    assert flex_saved["dates"] == ["2026-10-22", "2026-10-30"]
    assert len(restarts(pi)) == 1  # today's changed, so it's restarted without asking

    # Giving one of those dates to another schedule takes it off Flex.
    screen = Screen(monkeypatch, asks=["10-30"], chooses=[named("Weekend")], confirms=[False])
    clock.set_dates(pi)
    screen.finished()
    by_name = {s["description"]: s for s in saved(pi)}
    assert by_name["Flex"]["dates"] == ["2026-10-22"]
    assert by_name["Weekend"]["dates"] == ["2026-10-30"]
    assert "Took those dates off Flex." in screen.text("info")


def test_change_a_periods_time_and_colour(pi, monkeypatch):
    screen = Screen(monkeypatch, asks=["A Block", "9:00"],
                    chooses=[named("4-Block"), "period", named("08:50"), "Block", "other",
                             "save"],
                    confirms=[])
    screen.asks = iter(["A Block", "9:00", "10, 20, 30"])
    clock.change_schedule(pi)
    screen.finished()
    a_block = next(p for p in saved(pi)[0]["values"] if p["text"] == "A Block")
    assert (a_block["start"], a_block["colour"]) == ("09:00", "10, 20, 30")
    assert len(restarts(pi)) == 1  # it's today's schedule, so it's restarted


def test_saving_a_broken_schedule_says_why_and_lets_it_be_fixed(pi, monkeypatch):
    screen = Screen(monkeypatch, chooses=[named("Flex"), "remove", named("00:00"), "save",
                                          None], confirms=[True])
    clock.change_schedule(pi)
    screen.finished()
    assert "has to start at 00:00" in screen.text("error")
    assert "/home/pi/jsclock/schedules.js" not in pi.connection.files  # left without saving


def test_make_a_new_schedule_from_a_copy(pi, monkeypatch):
    screen = Screen(monkeypatch, asks=["Early dismissal", "Out early", "13:00"],
                    chooses=[named("4-Block"), "add", "Break", "239, 52, 24", "days", "save"],
                    picks=[[5]], confirms=[False])
    clock.new_schedule(pi)
    screen.finished()
    by_name = {s["description"]: s for s in saved(pi)}
    new = by_name["Early dismissal"]
    assert new["days"] == [5]
    assert [p["start"] for p in new["values"]] == ["00:00", "08:50", "11:35", "13:00", "15:00"]
    assert by_name["4-Block"]["days"] == [1, 2, 4]  # Friday moved to the new one
    assert "Friday moved from 4-Block." in screen.text("info")
    assert restarts(pi) == []  # they said not now


def test_remove_a_schedule(pi, monkeypatch):
    Screen(monkeypatch, chooses=[named("Weekend")], confirms=[True, False])
    clock.remove_schedule(pi)
    assert [s["description"] for s in saved(pi)] == ["4-Block", "Flex"]


def test_go_back_to_an_earlier_version(pi, monkeypatch):
    pi.save([weekend()])
    (backup,) = pi.backups()
    Screen(monkeypatch, chooses=[backup], confirms=[True])
    clock.go_back(pi)
    assert [s["description"] for s in saved(pi)] == ["4-Block", "Flex", "Weekend"]
    assert len(restarts(pi)) == 1


def test_an_old_clock_is_updated_before_schedules_are_saved(pi, monkeypatch):
    pi.connection.files["/home/pi/jsclock/clock_script.js"] = b"old clock"
    monkeypatch.setattr(clock.login, "connect", lambda settings: pi.connection)
    monkeypatch.setattr(clock, "ClockPi", lambda connection, settings: pi)
    commands = []
    monkeypatch.setattr(pi.connection, "run", lambda command, **k: commands.append(command)
                        or CommandResult(0, "" if "git status" in command else "Fast-forward"))
    screen = Screen(monkeypatch, confirms=[True], chooses=[None])  # then on to the dates
    clock._do(clock.set_dates, ClockSettings())
    screen.finished()
    assert "needs updating" in screen.text("warning")
    # Its schedules were kept first, as the update replaces schedule_data.js.
    assert [s["description"] for s in saved(pi)] == ["4-Block", "Flex", "Weekend"]
    assert any("git pull --ff-only" in c for c in commands)
    assert any("launcher.sh" in c for c in commands)  # then restarted


TOKEN_URL = "https://someone:github_pat_SECRET123@https://github.com/timberline-secondary/jsclock.git/"


def test_secrets_in_web_addresses_are_hidden():
    message = f"fatal: unable to access '{TOKEN_URL}': Could not resolve host: https"
    assert "SECRET" not in clock.hide_secrets(message)
    assert clock.hide_secrets("https://github.com/x.git") == "https://github.com/x.git"


@pytest.mark.parametrize("url, plain", [
    (TOKEN_URL, "https://github.com/timberline-secondary/jsclock.git"),
    ("https://name:pw@github.com/x/y.git", "https://github.com/x/y.git"),
    ("git@github.com:timberline-secondary/jsclock.git",
     "https://github.com/timberline-secondary/jsclock.git"),
    ("ssh://git@github.com/x/y.git", "https://github.com/x/y.git"),
    ("https://github.com/x/y.git", None),
    ("", None),
])
def test_the_plain_github_address(url, plain):
    found = clock.plain_address(url)
    assert (found[0] if found else None) == plain


def test_an_update_takes_a_login_out_of_the_clocks_github_address(pi, monkeypatch):
    commands = []

    def run(command, **k):
        commands.append(command)
        if "get-url" in command:
            return CommandResult(0, TOKEN_URL + "\n")
        if "git pull" in command:
            return CommandResult(1, f"fatal: unable to access '{TOKEN_URL}': nope")
        return CommandResult(0, "")
    monkeypatch.setattr(pi.connection, "run", run)
    screen = Screen(monkeypatch, confirms=[True])
    with pytest.raises(ClockError) as failed:
        clock._update(pi)
    screen.finished()
    assert any("set-url origin https://github.com/timberline-secondary/jsclock.git" in c
               for c in commands)
    assert "SECRET" not in str(failed.value) + screen.text()  # never shown


def test_an_update_switches_an_ssh_github_address_to_the_web_one(pi, monkeypatch):
    commands = []

    def run(command, **k):
        commands.append(command)
        if "get-url" in command:
            return CommandResult(0, "git@github.com:timberline-secondary/jsclock.git\n")
        return CommandResult(0, "")
    monkeypatch.setattr(pi.connection, "run", run)
    screen = Screen(monkeypatch, confirms=[True])
    clock._update(pi)
    screen.finished()
    assert "uses SSH" in screen.text("warning")
    set_url = [c for c in commands if "set-url" in c]
    assert set_url and set_url[0].endswith(
        "set-url origin https://github.com/timberline-secondary/jsclock.git")
    assert commands.index(set_url[0]) < next(i for i, c in enumerate(commands) if "pull" in c)


def test_an_update_leaves_a_clock_changed_on_the_pi_alone(pi, monkeypatch):
    monkeypatch.setattr(pi.connection, "run", lambda command, **k: CommandResult(
        0, " M styles.css\n M schedule_data.js\n?? schedules.js\n"))
    with pytest.raises(ClockError, match=r"changed on .* \(styles.css\)"):
        clock._update(pi)


def test_the_menu():
    assert [title for title, _ in clock.ACTIONS] == [
        "See the schedules", "Use a schedule on certain dates (e.g. an assembly)",
        "Make a new schedule", "Change a schedule (its periods, times, colours or days)",
        "Remove a schedule", "Go back to an earlier version of the schedules",
        "Restart the clock, update it, or reboot pi-projector",
        "Projector Clock documentation"]
