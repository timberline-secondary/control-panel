"""Everything the person sees: menus, prompts and coloured messages.

They work like the old Linux control panel's:

- a menu is a numbered list: type the number, then press Enter;
- a question says what q and Enter do: "Grad year ([q]uit | [Enter] = 2027): ";
- a yes or no question is "Delete it? y/[n] ", the [ ] marking what Enter means.

Every answer needs Enter, and q always goes back (or quits, from the main menu).
"""

from __future__ import annotations

import re
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from typing import Any

from prompt_toolkit import print_formatted_text
from prompt_toolkit import prompt as _prompt
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.patch_stdout import patch_stdout as _patch_stdout
from prompt_toolkit.shortcuts import clear as clear_screen

# The old control panel's colours.
HEADER = "fg:ansimagenta"
SUCCESS = "fg:ansigreen bold"
WARNING = "fg:ansiyellow"
ERROR = "fg:ansired bold"
INPUT = "fg:ansiyellow bold"  # questions
YES_NO = "fg:ansicyan"


def clear() -> None:
    clear_screen()


def heading(title: str) -> None:
    line = "#" * 60
    _print(f"{line}\n#{title.upper().center(58)}#\n{line}\n", style=HEADER)


def info(text: str) -> None:
    _print(text)


def success(text: str) -> None:
    _print(text, style=SUCCESS)


def warning(text: str) -> None:
    _print(text, style=WARNING)


def error(text: str) -> None:
    _print(text, style=ERROR)


def _print(text: str, style: str = "") -> None:
    # prompt_toolkit's Windows output needs a real console; fall back when piped or captured.
    if sys.stdout is not None and sys.stdout.isatty():
        print_formatted_text(FormattedText([(style, text)]))
    else:
        print(text)


def _input(text: str, style: str = INPUT, *, password: bool = False,
           patch_stdout: bool = False) -> str | None:
    """What they typed (once they press Enter). None for Ctrl+C, or Ctrl+D on an empty line."""
    try:
        with _patch_stdout() if patch_stdout else nullcontext():
            return _prompt(FormattedText([(style, text)]), is_password=password)
    except (EOFError, KeyboardInterrupt):
        return None


def _hints(*hints: str) -> str:
    return f"({' | '.join(h for h in hints if h)})"


def ask(prompt: str, default: str = "", *, hint: str = "",
        patch_stdout: bool = False) -> str | None:
    """Ask for some text: "Prompt ([q]uit | [Enter] = default): ".

    Returns what they typed, or the default if they just press Enter (hint says what Enter
    means when there's no default, e.g. "[Enter] = done"). None if they type q.
    """
    enter = f"[Enter] = {default}" if default else hint
    answer = _input(f"{prompt} {_hints('[q]uit', enter)}: ", patch_stdout=patch_stdout)
    if answer is None or answer.strip().lower() == "q":
        return None
    return answer.strip() or default


def ask_password(prompt: str) -> str | None:
    """None if they type q."""
    answer = _input(f"{prompt} {_hints('[q]uit')}: ", password=True)
    return None if answer is None or answer.strip().lower() == "q" else answer


def confirm(prompt: str, default: bool = True) -> bool | None:
    """Yes or no: "Delete it? y/[n] ". None (which also counts as no) if they type q, for
    when that should mean 'stop' rather than 'no, carry on'."""
    while True:
        answer = _input(f"{prompt} {'[y]/n' if default else 'y/[n]'} ", style=YES_NO)
        if answer is None:
            return None
        answer = answer.strip().lower()
        if answer == "q":
            return None
        if not answer:
            return default
        if answer in ("y", "yes", "n", "no"):
            return answer.startswith("y")
        error("Type y or n, then press Enter (or q to go back).")


def pause(prompt: str = "Press Enter to carry on.") -> None:
    """Wait for Enter, e.g. so a message can be read before the screen is cleared."""
    _input(prompt, style="")


BACK = object()  # value of a menu's "Back"/"Quit" choice


@dataclass
class Choice:
    title: str
    value: Any = None
    disabled: str | None = None  # why it can't be chosen, if it can't
    checked: bool = False  # ticked to start with, in choose_many


def choice(title: str, value: Any = None, disabled: str | None = None,
           checked: bool = False) -> Choice:
    return Choice(title, value, disabled, checked)


def _back_label(title: str) -> str:
    return "[q]uit" if title.strip().lower() == "quit" else f"[q] {title}"


def choose(prompt: str, choices: list[Choice | str]) -> Any:
    """A numbered menu: type a number, then Enter (just Enter for the first). q is Back (or
    Quit), which every menu has. Returns the chosen value, or None for Back."""
    items = [c if isinstance(c, Choice) else Choice(c, c) for c in choices]
    back = next((c for c in items if c.value is BACK), Choice("Back", BACK))
    options = [c for c in items if c is not back]
    _print(prompt, style="bold")
    number = 0
    numbered: dict[int, Choice] = {}
    for item in options:
        if item.disabled:
            _print(f"      {item.title} ({item.disabled})", style="fg:ansibrightblack")
        else:
            number += 1
            numbered[number] = item
            _print(f"  {number}. {item.title}")
    _print(f"  {_back_label(back.title)}")
    while True:
        answer = _input("Choose an item ([Enter] = 1): " if numbered else "Choose an item: ")
        if answer is None or answer.strip().lower() in ("q", "b"):
            return None
        answer = answer.strip()
        if not answer and numbered:
            return numbered[1].value
        if answer.isdigit() and int(answer) in numbered:
            return numbered[int(answer)].value
        error(f"That isn't one of the choices. Type its number (1 to {len(numbered)}), then "
              "press Enter." if numbered else "There's nothing to choose. Type q to go back.")


def choose_many(prompt: str, choices: list[Choice], *,
                require_one: bool = False) -> list[Any] | None:
    """A numbered list to pick some from: type their numbers (e.g. 1 3, 2-5, or all), then
    Enter (just Enter for the ones marked [x]). Returns their values, or None for q."""
    _print(prompt, style="bold")
    for number, item in enumerate(choices, start=1):
        _print(f"  {number}. [{'x' if item.checked else ' '}] {item.title}")
    ticked = [number for number, item in enumerate(choices, start=1) if item.checked]
    enter = f"[Enter] = {' '.join(map(str, ticked))}" if ticked else \
        ("" if require_one else "[Enter] = none")
    while True:
        answer = _input(f"Type their numbers, e.g. 1 3, or all {_hints('[q]uit', enter)}: ")
        if answer is None or answer.strip().lower() == "q":
            return None
        numbers = _numbers(answer, len(choices)) if answer.strip() else ticked
        if numbers is None:
            error(f"Type numbers from 1 to {len(choices)}, e.g. 1 3 or 2-4, then press Enter.")
        elif require_one and not numbers:
            error("Pick at least one.")
        else:
            return [choices[n - 1].value for n in numbers]


def _numbers(text: str, count: int) -> list[int] | None:
    """'1 3, 5-7' -> [1, 3, 5, 6, 7]; 'all' -> all of them. None if it doesn't make sense."""
    if text.strip().lower() in ("all", "a"):
        return list(range(1, count + 1))
    picked: list[int] = []
    for part in re.split(r"[\s,]+", text.strip()):
        match = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if not match:
            return None
        start, end = int(match[1]), int(match[2] or match[1])
        if not 1 <= start <= end <= count:
            return None
        picked += [n for n in range(start, end + 1) if n not in picked]
    return sorted(picked)


@contextmanager
def progress(label: str) -> Iterator[Callable[[float], None]]:
    """A progress bar on one line. Gives a function to call with the fraction done (0 to 1)."""
    shown = -1
    live = sys.stdout is not None and sys.stdout.isatty()

    def update(fraction: float) -> None:
        nonlocal shown
        percent = max(0, min(100, int(fraction * 100)))
        if percent == shown:
            return
        shown = percent
        if live:
            bar = "#" * (percent // 4)
            sys.stdout.write(f"\r  {label} [{bar:<25}] {percent:3d}%")
            sys.stdout.flush()

    if live:
        sys.stdout.write(f"  {label}...")
        sys.stdout.flush()
    try:
        yield update
    finally:
        if live:
            sys.stdout.write("\n")
        else:
            print(f"  {label}: {'done' if shown == 100 else 'stopped'}")
