"""Projector Clock: the clock on the projector, showing the time, the current block and the
time left in it. It's jsclock (https://github.com/timberline-secondary/jsclock), a web page
shown in Firefox on pi-projector.

Its schedules are kept on the Pi, in schedules.js next to the clock. This panel reads them,
changes them, and saves them there (keeping a copy of the old ones in backups/ each time),
then restarts the clock so it shows the change. Until they're first saved, the clock uses
the ones in its schedule_data.js, so that's where they're read from then.
"""

from __future__ import annotations

import datetime
import io
import json
import posixpath
import re
import shlex
from typing import Any

import paramiko

from control_panel import login, ui
from control_panel.config import ClockSettings, Config
from control_panel.ssh import CommandResult, ConnectionFailed, PiConnection

TITLE = "Projector Clock"
DOCS = "https://github.com/timberline-secondary/jsclock#schedules"

SAVED = "schedules.js"  # the schedules saved on the Pi (what the clock uses, if it's there)
DEFAULTS = "schedule_data.js"  # the clock's own schedules, used until some are saved
BACKUPS = "backups"
LAUNCHER = "launcher.sh"  # starts Firefox showing the clock

DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
WEEKDAYS = [1, 2, 3, 4, 5]
TYPES = {
    "Block": "a class",
    "Break": "e.g. before or after school",
    "Lunch": "shown as 'Time Until Lunch'",
    "Transition": "time to get to the next class",
}
COLOURS = {
    "default": "Normal (dark)",
    "239, 52, 24": "Red",
    "255, 255, 102": "Yellow",
    "60, 179, 113": "Green",
    "65, 105, 225": "Blue",
    "147, 112, 219": "Purple",
}
DEFAULT_RGB = (13, 9, 10)  # what the clock shows for "default"

Schedule = dict[str, Any]  # {"description", "days", "dates", "values": [periods]}


class ClockError(Exception):
    """Something the person can fix. The message says what."""


# --- Reading and writing the schedules files (no prompts, easy to test) -------------------

def parse_saved(text: str) -> list[Schedule]:
    """The schedules in a schedules.js: const localSchedules = [...];"""
    start, end = text.find("[", text.find("localSchedules")), text.rfind("]")
    if text.find("localSchedules") < 0 or start < 0 or end < start:
        raise ValueError("there's no list of schedules in it")
    return _schedules(json.loads(text[start:end + 1]))


def parse_defaults(text: str) -> list[Schedule]:
    """The schedules in the clock's schedule_data.js: const scheduleStr = `[...]`"""
    parts = text.split("`")
    if len(parts) < 3:
        raise ValueError("there's no list of schedules in it")
    return _schedules(json.loads(parts[1]))


def _schedules(data: Any) -> list[Schedule]:
    if not isinstance(data, list) or not all(
            isinstance(s, dict) and isinstance(s.get("values"), list) for s in data):
        raise ValueError("it isn't a list of schedules, each with periods")
    return data


def render(schedules: list[Schedule]) -> str:
    """A schedules.js for the clock."""
    return ("// The clock's schedules, saved by the Hackerspace control panel\n"
            "// (https://github.com/timberline-secondary/control-panel). If you change it by\n"
            "// hand, keep it valid JSON (e.g. no comma after the last item), or the clock\n"
            "// will go back to the schedules in schedule_data.js.\n"
            f"const localSchedules = {json.dumps(schedules, indent=2, ensure_ascii=False)};\n")


def parse_time(text: str) -> str | None:
    """'8:50', '08:50', '1:45 pm' or '13:45' -> '08:50' or '13:45'. None if it isn't a time."""
    found = re.fullmatch(r"\s*(\d{1,2})[:.]?(\d\d)\s*([ap])?\.?\s*m?\.?\s*", text.lower())
    if not found:
        return None
    hour, minute = int(found[1]), int(found[2])
    if found[3]:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if found[3] == "p" else 0)
    if hour > 23 or minute > 59:
        return None
    return f"{hour:02d}:{minute:02d}"


def parse_dates(text: str, today: datetime.date) -> list[str] | None:
    """Dates typed as 2026-10-30, 10-30 (the next one), today or tomorrow, separated by spaces
    or commas -> ['2026-10-30', ...]. None if one of them isn't a date."""
    dates = []
    for word in re.split(r"[\s,]+", text.strip().lower()):
        if word in ("today", "tomorrow"):
            day = today + datetime.timedelta(days=word == "tomorrow")
        else:
            found = re.fullmatch(r"(?:(\d{4})[-/])?(\d{1,2})[-/](\d{1,2})", word)
            if not found:
                return None
            try:
                day = datetime.date(int(found[1] or today.year), int(found[2]), int(found[3]))
            except ValueError:
                return None
            if not found[1] and day < today:
                day = day.replace(year=day.year + 1)
        if day.isoformat() not in dates:
            dates.append(day.isoformat())
    return dates or None


def rgb(colour: str) -> tuple[int, int, int] | None:
    """'239, 52, 24' -> (239, 52, 24); 'default' -> the clock's dark colour. None if it
    isn't a colour."""
    if colour == "default":
        return DEFAULT_RGB
    parts = [p.strip() for p in str(colour).split(",")]
    if len(parts) != 3 or not all(p.isdigit() and int(p) <= 255 for p in parts):
        return None
    return int(parts[0]), int(parts[1]), int(parts[2])


def problems(schedule: Schedule) -> list[str]:
    """What's wrong with a schedule that would stop the clock showing it properly."""
    found = []
    if not str(schedule.get("description", "")).strip():
        found.append("It needs a name.")
    periods = schedule.get("values", [])
    if not periods:
        found.append("It needs at least one period.")
    starts = [p.get("start") for p in periods]
    for period in periods:
        name = period.get("text") or "A period"
        if not str(period.get("text", "")).strip():
            found.append("A period has no name.")
        if parse_time(str(period.get("start", ""))) != period.get("start"):
            found.append(f"{name} doesn't have a start time like 08:50.")
        if rgb(period.get("colour", "")) is None:
            found.append(f"{name}'s colour isn't red, green, blue numbers like 239, 52, 24.")
    if periods and "00:00" not in starts:
        found.append("The first period has to start at 00:00 (e.g. 'Start of the day'), so "
                     "there's always something to show.")
    for start in sorted({s for s in starts if starts.count(s) > 1 and s}):
        found.append(f"More than one period starts at {start}.")
    return found


def tidy(schedule: Schedule) -> Schedule:
    """Its periods in order, each ending when the next starts (the last at midnight)."""
    periods = sorted(schedule.get("values", []), key=lambda p: p.get("start", ""))
    for period, after in zip(periods, periods[1:] + [{"start": "00:00"}], strict=True):
        period["end"] = after["start"]
    schedule["values"] = periods
    schedule["days"] = sorted(set(schedule.get("days", [])))
    if "dates" in schedule:
        schedule["dates"] = sorted(set(schedule["dates"]))
        if not schedule["dates"]:
            del schedule["dates"]
    return schedule


def for_date(schedules: list[Schedule], day: datetime.date) -> Schedule | None:
    """The schedule the clock shows on a day, as the clock picks it: the first with that
    date, or else the first with that day of the week."""
    weekday = (day.weekday() + 1) % 7  # the clock counts from Sunday
    return (next((s for s in schedules if day.isoformat() in s.get("dates", [])), None)
            or next((s for s in schedules if weekday in s.get("days", [])), None))


def name(schedule: Schedule) -> str:
    """A schedule's name, to use in a sentence."""
    return str(schedule.get("description", "")).strip().rstrip(".") or "(no name)"


def days_text(days: list[int]) -> str:
    days = sorted(set(days))
    if not days:
        return "no days of the week"
    if days == WEEKDAYS:
        return "Monday to Friday"
    if len(days) == 1:
        return f"{DAYS[days[0]]}s"
    return ", ".join(DAYS[d][:3] for d in days)


def date_text(date: str) -> str:
    day = datetime.date.fromisoformat(date)
    return f"{DAYS[(day.weekday() + 1) % 7]}, {day:%B} {day.day}, {day.year}"


def colour_text(colour: str) -> str:
    name = COLOURS.get(colour, colour)
    shown = rgb(colour)
    return f"{ui.swatch(shown)} {name}".strip() if shown else name


def describe(schedule: Schedule, today: datetime.date) -> list[str]:
    """A schedule as lines to show: its name, when it's used, then its periods."""
    lines = [f"{schedule.get('description', '(no name)')}",
             f"  Used on {days_text(schedule.get('days', []))}"]
    upcoming = [d for d in schedule.get("dates", []) if d >= today.isoformat()]
    if upcoming:
        lines[-1] += ", and on " + "; ".join(date_text(d) for d in upcoming)
    lines[-1] += "."
    for period in sorted(schedule.get("values", []), key=lambda p: p.get("start", "")):
        lines.append(f"    {period.get('start', '?'):>5}  {period.get('text', ''):<24} "
                     f"{period.get('type', ''):<11} {colour_text(period.get('colour', ''))}")
    return lines


# --- Talking to pi-projector ---------------------------------------------------------------

class ClockPi:
    """What we can do on pi-projector, given an open connection."""

    def __init__(self, connection: PiConnection, settings: ClockSettings):
        self.connection = connection
        self.settings = settings

    def _path(self, *parts: str) -> str:
        return posixpath.join(self.settings.clock_dir, *parts)

    def _read(self, path: str) -> str:
        with self.connection.open(path) as f:
            return f.read().decode("utf-8")

    def check(self) -> None:
        if not self.connection.exists(self._path("index.html")):
            raise ClockError(f"The clock isn't in {self.settings.clock_dir} on "
                             f"{self.settings.host}.")

    def can_use_saved(self) -> bool:
        """Whether this version of the clock reads the schedules saved in schedules.js."""
        try:
            return "localSchedules" in self._read(self._path("clock_script.js"))
        except FileNotFoundError:
            return False

    def load(self) -> list[Schedule]:
        """The clock's schedules: the saved ones, or else its own."""
        self.check()
        for name, parse in ((SAVED, parse_saved), (DEFAULTS, parse_defaults)):
            path = self._path(name)
            if self.connection.exists(path):
                try:
                    return parse(self._read(path))
                except (ValueError, UnicodeDecodeError) as e:
                    raise ClockError(f"Couldn't read the schedules in {path} ({e}). Go back "
                                     "to an earlier version, or fix it by hand.") from e
        raise ClockError(f"There are no schedules in {self.settings.clock_dir}.")

    def save(self, schedules: list[Schedule]) -> str | None:
        """Save the schedules for the clock, keeping a copy of the old ones. Returns where
        the copy is."""
        schedules = [tidy(s) for s in schedules]
        for schedule in schedules:
            if found := problems(schedule):
                raise ClockError(f"{name(schedule)}: {' '.join(found)}")
        backup = self._backup()
        self.connection.upload(io.BytesIO(render(schedules).encode("utf-8")),
                               self._path(SAVED))
        return backup

    def _backup(self) -> str | None:
        """Keep a copy of the schedules the clock has now (in backups/, by date)."""
        old = self._path(SAVED)
        if self.connection.exists(old):
            text = self._read(old)
        else:
            text = render(self.load())  # its own, the first time
        self.connection.mkdir(self._path(BACKUPS))
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        path = self._path(BACKUPS, f"schedules-{stamp}.js")
        self.connection.upload(io.BytesIO(text.encode("utf-8")), path)
        return path

    def backups(self) -> list[str]:
        """The saved copies, newest first."""
        path = self._path(BACKUPS)
        if not self.connection.exists(path):
            return []
        names = self.connection.list_folder(path)[1]
        return sorted((n for n in names if re.fullmatch(r"schedules-\d{8}-\d{6}\.js", n)),
                      reverse=True)

    def read_backup(self, name: str) -> list[Schedule]:
        try:
            return parse_saved(self._read(self._path(BACKUPS, name)))
        except (ValueError, UnicodeDecodeError) as e:
            raise ClockError(f"Couldn't read the schedules in {name} ({e}).") from e

    def restart(self) -> CommandResult:
        """Close Firefox and start the clock again, as at startup."""
        launcher = shlex.quote(self._path(LAUNCHER))
        # ([f]irefox matches Firefox however it's named, but not this command itself.)
        return self.connection.run(
            "pkill -f '[f]irefox'; "
            "for i in $(seq 20); do pgrep -f '[f]irefox' >/dev/null || break; sleep 0.5; done; "
            f"DISPLAY=:0 setsid nohup bash {launcher} >/dev/null 2>&1 </dev/null & "
            "sleep 5; pgrep -f '[f]irefox' >/dev/null", timeout=60)

    def update_code(self) -> CommandResult:
        """Get the latest clock from GitHub. Local changes to schedule_data.js are dropped:
        the schedules are kept in schedules.js."""
        folder = shlex.quote(self.settings.clock_dir)
        return self.connection.run(
            f"cd {folder} && git checkout -- {DEFAULTS} && git pull --ff-only 2>&1",
            timeout=120)

    def changed_code(self) -> list[str] | None:
        """The clock's files that were changed on the Pi (besides its schedules), or None if
        it isn't a copy from GitHub."""
        result = self.connection.run(
            f"cd {shlex.quote(self.settings.clock_dir)} && git status --porcelain")
        if not result.ok:
            return None
        return [line[3:] for line in result.output.splitlines()
                if not line.startswith("??") and line[3:] != DEFAULTS]

    def keep_schedules(self) -> bool:
        """Before an update replaces schedule_data.js: save the schedules in it (as they are
        on the Pi) in schedules.js, if none are saved there yet. True if they were."""
        if self.connection.exists(self._path(SAVED)):
            return False
        self.connection.upload(io.BytesIO(render(self.load()).encode("utf-8")),
                               self._path(SAVED))
        return True

    def reboot(self) -> CommandResult:
        return self.connection.run("shutdown -r now", sudo=True)


# --- Questions -----------------------------------------------------------------------------

def _today() -> datetime.date:
    return datetime.date.today()


def _choose_schedule(schedules: list[Schedule], question: str) -> Schedule | None:
    return ui.choose([(f"{name(s)} ({days_text(s.get('days', []))})", s) for s in schedules],
                     question)


def _ask_days(current: list[int]) -> list[int] | None:
    shown = ", ".join(DAYS[d] for d in sorted(current)) or "none"
    picked = ui.choose_many([(day, n) for n, day in enumerate(DAYS)] +
                            [("No days of the week: only on dates I choose", -1)],
                            f"Which days of the week is it for? (Now: {shown}.)")
    if picked is None:
        return None
    return [] if -1 in picked else picked


def _ask_time(prompt: str, default: str = "") -> str | None:
    while True:
        text = ui.ask(prompt, default=default)
        if text is None:
            return None
        if time := parse_time(text):
            return time
        ui.error("Type a time like 8:50, 13:45 or 1:45 pm.")


def _ask_colour(current: str = "default") -> str | None:
    choices = [(colour_text(c), c) for c in COLOURS] + [("Another colour (type it)", "other")]
    colour = ui.choose(choices, "What colour should be behind the clock?",
                       default=current if current in COLOURS else None)
    while colour == "other":
        text = ui.ask("Type its red, green and blue (0 to 255), e.g. 239, 52, 24")
        if text is None:
            return None
        if rgb(text) and text.strip() != "default":
            colour = ", ".join(str(n) for n in rgb(text))
        else:
            ui.error("Type three numbers from 0 to 255, separated by commas.")
    return colour


def _ask_period(period: dict[str, str] | None = None) -> dict[str, str] | None:
    period = dict(period or {})
    text = ui.ask("What should the clock say? e.g. A Block", default=period.get("text", ""))
    if not text:
        return None
    start = _ask_time("When does it start?", period.get("start", ""))
    if start is None:
        return None
    kind = ui.choose([(f"{t} ({why})", t) for t, why in TYPES.items()], "What is it?",
                     default=period.get("type") if period.get("type") in TYPES else None)
    if kind is None:
        return None
    colour = _ask_colour(period.get("colour", "default"))
    if colour is None:
        return None
    return {**period, "text": text, "start": start, "type": kind, "colour": colour}


def _claim_days(schedules: list[Schedule], schedule: Schedule, days: list[int]) -> None:
    """Give days to a schedule, taking them from any other (the clock uses the first it
    finds, so a day is only on one)."""
    for other in schedules:
        if other is not schedule and (taken := set(other.get("days", [])) & set(days)):
            other["days"] = [d for d in other.get("days", []) if d not in taken]
            ui.info(f"{', '.join(DAYS[d] for d in sorted(taken))} moved from {name(other)}.")
    schedule["days"] = sorted(days)


def _edit(schedules: list[Schedule], schedule: Schedule) -> bool:
    """Change a schedule (a copy of it) until they save it or leave. True if saved."""
    work = json.loads(json.dumps(schedule))
    while True:
        ui.info("")
        for line in describe(tidy(work), _today()):
            ui.info(line)
        periods = work["values"]
        what = ui.choose([
            ("Save it", "save"),
            ("Change a period", "period"),
            ("Add a period", "add"),
            ("Remove a period", "remove"),
            ("Change its name", "name"),
            ("Change its days of the week", "days"),
        ], "What do you want to change?")
        if what is None:
            if ui.confirm("Leave without saving the changes?", default=False):
                return False
        elif what == "save":
            if found := problems(work):
                for problem in found:
                    ui.error(problem)
                continue
            schedule.clear()
            schedule.update(work)
            if not any(s is schedule for s in schedules):
                schedules.append(schedule)
            return True
        elif what == "name":
            name = ui.ask("Type its name", default=work.get("description", ""))
            if name:
                work["description"] = name
        elif what == "days":
            days = _ask_days(work.get("days", []))
            if days is not None:
                work["days"] = days  # taken from other schedules when it's saved
        elif what == "add":
            if period := _ask_period({"colour": "default", "type": "Block"}):
                periods.append(period)
        elif what in ("period", "remove") and periods:
            picked = ui.choose([(f"{p['start']}  {p.get('text', '')}", p) for p in periods],
                               "Which period?")
            if picked is None:
                continue
            if what == "remove":
                periods.remove(picked)
            elif changed := _ask_period(picked):
                periods[periods.index(picked)] = changed


# --- Menu actions --------------------------------------------------------------------------

def see_schedules(pi: ClockPi) -> None:
    schedules = pi.load()
    today = _today()
    showing = for_date(schedules, today)
    for schedule in schedules:
        ui.info("")
        for line in describe(schedule, today):
            ui.info(line)
    ui.info("")
    if showing:
        ui.success(f"Today ({date_text(today.isoformat())}) the clock shows {name(showing)}.")
    else:
        ui.warning("Today the clock has no schedule, so it only shows the time.")


def set_dates(pi: ClockPi) -> None:
    schedules = pi.load()
    ui.info("On the dates you choose, the clock uses that schedule instead of the usual one "
            "for the day of the week, e.g. an assembly or early dismissal.")
    schedule = _choose_schedule(schedules, "Which schedule?")
    if schedule is None:
        return
    today = _today()
    upcoming = [d for d in schedule.get("dates", []) if d >= today.isoformat()]
    if upcoming:
        ui.info(f"It's set for: {'; '.join(date_text(d) for d in upcoming)}.")
    how = ui.choose([("Add dates", "add"), ("Remove dates", "remove")],
                    "Add dates, or remove them?") if upcoming else "add"
    if how is None:
        return
    while True:
        text = ui.ask("Type the dates, e.g. 2026-10-30, 11-02 or tomorrow")
        if text is None:
            return
        if (dates := parse_dates(text, today)) is not None:
            break
        ui.error("Type dates like 2026-10-30 or 10-30 (month-day), or today or tomorrow.")
    # Dates gone by are dropped, so the list doesn't grow forever.
    kept = [d for d in schedule.get("dates", []) if d >= today.isoformat()]
    if how == "add":
        for other in schedules:
            if other is not schedule and set(other.get("dates", [])) & set(dates):
                other["dates"] = [d for d in other["dates"] if d not in dates]
                ui.info(f"Took those dates off {name(other)}.")
        schedule["dates"] = sorted(set(kept) | set(dates))
    else:
        schedule["dates"] = [d for d in kept if d not in dates]
    _save(pi, schedules, restart_now=today.isoformat() in dates)
    shown = "; ".join(date_text(d) for d in dates)
    ui.success(f"{name(schedule)}: {'added' if how == 'add' else 'removed'} {shown}.")


def new_schedule(pi: ClockPi) -> None:
    schedules = pi.load()
    start = _choose_schedule(schedules, "Which schedule should the new one start as a copy of?")
    if start is None:
        return
    new_name = ui.ask("Type its name, e.g. Early dismissal")
    if not new_name:
        return
    schedule = {**json.loads(json.dumps(start)), "description": new_name, "days": []}
    schedule.pop("dates", None)
    ui.info("It's used on no days of the week yet: choose its days, or choose dates for it "
            "with 'Use a schedule on certain dates'.")
    if _edit(schedules, schedule):
        _claim_days(schedules, schedule, schedule["days"])
        _save(pi, schedules)
        ui.success(f"Saved {new_name}.")


def change_schedule(pi: ClockPi) -> None:
    schedules = pi.load()
    schedule = _choose_schedule(schedules, "Which schedule?")
    if schedule is not None and _edit(schedules, schedule):
        _claim_days(schedules, schedule, schedule["days"])
        _save(pi, schedules, restart_now=for_date(schedules, _today()) is schedule)
        ui.success(f"Saved {name(schedule)}.")


def remove_schedule(pi: ClockPi) -> None:
    schedules = pi.load()
    schedule = _choose_schedule(schedules, "Which schedule?")
    if schedule is None:
        return
    if len(schedules) == 1:
        ui.error("That's the only schedule, so it can't be removed.")
        return
    used = days_text(schedule.get("days", []))
    if not ui.confirm(f"Remove {name(schedule)}? (It's used on {used}; the "
                      "clock will show just the time then, unless another schedule is.)",
                      default=False):
        return
    showing = for_date(schedules, _today()) is schedule
    schedules.remove(schedule)
    _save(pi, schedules, restart_now=showing)
    ui.success(f"Removed {name(schedule)}.")


def go_back(pi: ClockPi) -> None:
    names = pi.backups()
    if not names:
        ui.info("There are no earlier versions yet: one's kept each time the schedules are "
                "saved.")
        return
    picked = ui.choose([(_backup_time(n), n) for n in names[:20]],
                       "Which version? (Each is the schedules from just before that change.)")
    if picked is None:
        return
    schedules = pi.read_backup(picked)
    today = _today()
    for schedule in schedules:
        ui.info("")
        for line in describe(schedule, today):
            ui.info(line)
    if ui.confirm("Go back to these schedules? (The ones now are kept as another version.)",
                  default=False):
        _save(pi, schedules, restart_now=True)
        ui.success("Done: the clock has those schedules again.")


def _backup_time(name: str) -> str:
    stamp = datetime.datetime.strptime(name[len("schedules-"):-len(".js")], "%Y%m%d-%H%M%S")
    return f"{date_text(stamp.date().isoformat())} at {stamp:%I:%M %p}".replace(" 0", " ")


def restart_or_reboot(pi: ClockPi) -> None:
    what = ui.choose([
        ("Restart the clock (it reads its schedules again)", "restart"),
        ("Update the clock from GitHub, then restart it", "update"),
        ("Reboot pi-projector", "reboot"),
    ], "What do you want to do?")
    if what == "restart":
        _restart(pi)
    elif what == "update":
        _update(pi)
    elif what == "reboot":
        _reboot(pi)


def _restart(pi: ClockPi) -> None:
    ui.info("Restarting the clock...")
    if pi.restart().ok:
        ui.success("The clock has restarted. It takes a few seconds to appear.")
    else:
        ui.error("The clock didn't start again. Try 'Reboot pi-projector'.")


def _update(pi: ClockPi) -> None:
    pi.check()
    changed = pi.changed_code()
    if changed is None:
        raise ClockError(f"{pi.settings.clock_dir} on {pi.settings.host} isn't a copy of the "
                         "clock from GitHub, so it can't be updated from there.")
    if changed:
        raise ClockError(f"Some of the clock's files were changed on {pi.settings.host} "
                         f"({', '.join(changed)}), so it isn't updated in case they'd be "
                         "lost. Put them on GitHub first.")
    if pi.keep_schedules():
        ui.info(f"Saved its schedules, as they are on {pi.settings.host}, in {SAVED}.")
    ui.info("Updating the clock from GitHub...")
    result = pi.update_code()
    if not result.ok:
        raise ClockError(f"Couldn't update it: {result.output.strip()}")
    ui.success("Updated the clock to its latest version.")
    _restart(pi)


def _reboot(pi: ClockPi) -> None:
    if not ui.confirm("Reboot pi-projector? The clock is back in a minute or two.",
                      default=False):
        return
    try:
        result = pi.reboot()
    except (OSError, paramiko.SSHException):
        result = CommandResult(-1, "")  # it hung up on us, because it's going down
    if result.exit_status in (0, -1):
        ui.success("Rebooting...")
    else:
        ui.error(f"Couldn't reboot: {result.output.strip() or result.exit_status}")


def _save(pi: ClockPi, schedules: list[Schedule], *, restart_now: bool = False) -> None:
    """Save the schedules, then restart the clock so it shows them (or offer to: otherwise
    it reads them again at midnight)."""
    if pi.save(schedules):
        ui.info("(The schedules from before are kept as an earlier version.)")
    if restart_now or ui.confirm("Restart the clock now, so it uses them? (Otherwise it "
                                 "will at midnight.)", default=True):
        _restart(pi)


def docs() -> None:
    ui.info(f"The clock is jsclock: {DOCS}")
    ui.info("Its README says how the schedules work, and how it's set up on pi-projector.")


ACTIONS = [
    ("See the schedules", see_schedules),
    ("Use a schedule on certain dates (e.g. an assembly)", set_dates),
    ("Make a new schedule", new_schedule),
    ("Change a schedule (its periods, times, colours or days)", change_schedule),
    ("Remove a schedule", remove_schedule),
    ("Go back to an earlier version of the schedules", go_back),
    ("Restart the clock, update it, or reboot pi-projector", restart_or_reboot),
    ("Projector Clock documentation", docs),
]
SAVES = {set_dates, new_schedule, change_schedule, remove_schedule, go_back}


def run(config: Config) -> None:
    ui.run_menu(TITLE, ACTIONS, lambda action: _do(action, config.clock))


def _do(action, settings: ClockSettings) -> None:
    if action is docs:
        docs()
        return
    try:
        connection = login.connect(settings)
        if connection is None:
            return
        with connection:
            pi = ClockPi(connection, settings)
            if action in SAVES and not pi.can_use_saved():
                pi.check()
                ui.warning("The clock on pi-projector needs updating before it can use "
                           "schedules saved from here.")
                if not ui.confirm("Update it from GitHub now?", default=True):
                    return
                _update(pi)
            action(pi)
    except (ConnectionFailed, ClockError) as e:
        ui.error(str(e))
    except TimeoutError:
        ui.error(f"{settings.host} stopped answering. Try again, or reboot it if it keeps "
                 "happening.")
    except (OSError, paramiko.SSHException) as e:
        ui.error(f"Lost the connection to {settings.host}: {e}")
    except KeyboardInterrupt:
        ui.warning("Cancelled.")
