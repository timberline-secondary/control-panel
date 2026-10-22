"""Everything the person sees: menus, questions and messages, with plain input() and print().

- A panel clears the screen and shows its heading, then its numbered menu.
- A menu: type a number and press Enter, or q to go back (or quit, from the main menu).
- A question says what q does: "Grad year (Enter = 2027), or (q)uit: ".
- When something's done, "Press Enter to go back." keeps the result on screen (if there's
  anything new to read since the last question).
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from getpass import getpass
from typing import Any

BOLD, GREEN, YELLOW, RED, RESET = "\033[1m", "\033[32m", "\033[33m", "\033[31;1m", "\033[0m"
BACK = object()  # a menu choice that means "go back" (left out of the list; q does it)


def _colours_work() -> bool:
    if not (sys.stdout is not None and sys.stdout.isatty()):
        return False
    if os.name == "nt":  # turn on colours in the Windows console
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        kernel32.SetConsoleMode(handle, mode.value | 4)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    return True


_COLOURS = _colours_work()
_shown = False  # whether anything's been shown since the last answer
_asking: str | None = None  # the question waiting for an answer, if one is


def _print(text: str, colour: str = "") -> None:
    global _shown
    _shown = True
    text = f"{colour}{text}{RESET}" if colour and _COLOURS else text
    if _asking is not None and _COLOURS:  # from another thread, while a question waits
        print(f"\r\033[K{text}\n{_asking}", end="", flush=True)  # (so ask it again below)
    else:
        print(text, flush=True)


def clear() -> None:
    if not (sys.stdout is not None and sys.stdout.isatty()):
        return
    if os.name == "nt":
        os.system("cls")  # noqa: S605 (works in every Windows console)
    else:
        print("\033[2J\033[3J\033[H", end="", flush=True)


def heading(title: str) -> None:
    line = "#" * 60
    _print(f"{line}\n#{title.upper().center(58)}#\n{line}\n", BOLD)


def info(text: str) -> None:
    _print(text)


def success(text: str) -> None:
    _print(text, GREEN)


def warning(text: str) -> None:
    _print(text, YELLOW)


def error(text: str) -> None:
    _print(text, RED)


def _input(text: str) -> str | None:
    """What they typed, once they press Enter (after a blank line, so the question stands
    out). None for Ctrl+C or Ctrl+D."""
    global _shown, _asking
    print()
    _asking = text
    try:
        return input(text)
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    finally:
        _asking, _shown = None, False


def ask(prompt: str, default: str = "") -> str | None:
    """'Prompt (Enter = default), or (q)uit: '. What they typed (the default for just
    Enter), or None for q."""
    enter = f" (Enter = {default})" if default else ""
    answer = _input(f"{prompt}{enter}, or (q)uit: ")
    if answer is None or answer.strip().lower() == "q":
        return None
    return answer.strip() or default


def ask_password(prompt: str) -> str | None:
    global _shown
    print()
    try:
        answer = getpass(f"{prompt}, or (q)uit: ")
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    finally:
        _shown = False
    return None if answer.strip().lower() == "q" else answer


def confirm(prompt: str, default: bool = True) -> bool | None:
    """'Prompt (Y/n): ' - the capital is what Enter means. None for q (which also counts as
    no), for when that should mean 'stop' rather than 'no, carry on'."""
    while True:
        answer = _input(f"{prompt} ({'Y/n' if default else 'y/N'}): ")
        if answer is None or answer.strip().lower() == "q":
            return None
        answer = answer.strip().lower()
        if not answer:
            return default
        if answer in ("y", "yes", "n", "no"):
            return answer.startswith("y")
        error("Type y or n.")


def pause(prompt: str = "Press Enter to go back.") -> None:
    _input(prompt)


def choice(title: str, value: Any = None) -> tuple[str, Any]:
    return title, value


def _items(choices: list) -> list[tuple[str, Any]]:
    items = [c if isinstance(c, tuple) else (c, c) for c in choices]
    return [(title, value) for title, value in items if value is not BACK]


def choose(choices: list, question: str = "", default: Any = None) -> Any:
    """A numbered menu. Returns the chosen value (default's, for just Enter, if it's one of
    them), or None for q."""
    items = _items(choices)
    values = [value for _, value in items]
    enter = f" (Enter = {values.index(default) + 1})" if default in values else ""
    if question:
        print()
        _print(question, BOLD)
    for number, (title, _) in enumerate(items, start=1):
        _print(f"  {number}. {title}")
    while True:
        answer = _input(f"Choose an item{enter}, or (q)uit: " if enter
                        else "Choose an item or (q)uit: ")
        if answer is None or answer.strip().lower() == "q":
            return None
        answer = answer.strip()
        if not answer and enter:
            return default
        if answer.isascii() and answer.isdigit() and 1 <= int(answer) <= len(items):
            return items[int(answer) - 1][1]
        error(f"Type a number from 1 to {len(items)}." if items else "Type q to go back.")


def choose_many(choices: list, question: str = "") -> list[Any] | None:
    """A numbered list to pick some from: '1 3', '2-4' or 'all'. None for q."""
    items = _items(choices)
    if question:
        print()
        _print(question, BOLD)
    for number, (title, _) in enumerate(items, start=1):
        _print(f"  {number}. {title}")
    while True:
        answer = _input("Type their numbers (e.g. 1 3, or all), or (q)uit: ")
        if answer is None or answer.strip().lower() == "q":
            return None
        numbers = _numbers(answer, len(items))
        if numbers:
            return [items[n - 1][1] for n in numbers]
        error(f"Type numbers from 1 to {len(items)}, e.g. 1 3 or 2-4.")


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


def run_menu(title: str, actions: list[tuple[str, Any]], do: Callable[[Any], None], *,
             pause_after: bool = True) -> None:
    """A panel: clear the screen, show its heading and menu, and do what's chosen (showing
    its name first, so it's clear what the questions are about), until q. Afterwards it
    waits for Enter, unless nothing's been shown since the last question was answered."""
    while True:
        clear()
        heading(title)
        picked = choose([(name, (name, action)) for name, action in actions])
        if picked is None:
            return
        name, action = picked
        print()
        _print(name, BOLD)
        do(action)
        if pause_after and _shown:
            pause()


@contextmanager
def progress(label: str) -> Iterator[Callable[[float], None]]:
    """A progress bar on one line. Gives a function to call with the fraction done (0 to 1)."""
    global _shown
    _shown = True
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
