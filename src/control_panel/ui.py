"""Everything the person sees: menus, prompts and coloured messages."""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import questionary
from prompt_toolkit.shortcuts import clear as clear_screen

STYLE = questionary.Style([
    ("qmark", "fg:ansicyan bold"),
    ("question", "bold"),
    ("pointer", "fg:ansicyan bold"),
    ("highlighted", "fg:ansicyan bold"),
    ("answer", "fg:ansicyan"),
    ("disabled", "fg:ansibrightblack italic"),
])

QUIT_HINT = "(q to go back)"


def clear() -> None:
    clear_screen()


def heading(title: str) -> None:
    line = "#" * 60
    _print(f"{line}\n#{title.upper().center(58)}#\n{line}\n", style="fg:ansimagenta")


def info(text: str) -> None:
    _print(text)


def success(text: str) -> None:
    _print(text, style="fg:ansigreen bold")


def warning(text: str) -> None:
    _print(text, style="fg:ansiyellow")


def error(text: str) -> None:
    _print(text, style="fg:ansired bold")


def _print(text: str, style: str = "") -> None:
    # prompt_toolkit's Windows output needs a real console; fall back when piped or captured.
    if sys.stdout is not None and sys.stdout.isatty():
        questionary.print(text, style=style)
    else:
        print(text)


def ask(prompt: str, default: str = "", *, patch_stdout: bool = False) -> str | None:
    """Ask for some text. Returns None if they typed q or pressed Ctrl+C."""
    answer = _answer(questionary.text(prompt, default=default, qmark=">", style=STYLE),
                     patch_stdout=patch_stdout)
    if answer is None or answer.strip().lower() == "q":
        return None
    return answer.strip()


def ask_password(prompt: str) -> str | None:
    return _answer(questionary.password(prompt, qmark=">", style=STYLE))


def confirm(prompt: str, default: bool = True) -> bool | None:
    """Yes or no. None (which also counts as no) if they press Ctrl+C, for when that
    should mean 'stop' rather than 'no, carry on'."""
    answer = _answer(questionary.confirm(prompt, default=default, qmark="?", style=STYLE))
    return None if answer is None else bool(answer)


def pause(prompt: str = "Press Enter to carry on.") -> None:
    """Wait for Enter, e.g. so a message can be read before the screen is cleared."""
    try:
        input(prompt)
    except (EOFError, KeyboardInterrupt):
        pass


BACK = object()  # value of a menu's "Back"/"Quit" choice


def choose(prompt: str, choices: list[questionary.Choice | str]) -> Any:
    """Arrow keys or number keys to pick. Returns None for BACK or Ctrl+C."""
    answer = _answer(questionary.select(
        prompt, choices=choices, qmark="?", style=STYLE, use_shortcuts=True,
        instruction="(use arrow keys or numbers)",
    ))
    return None if answer is BACK else answer


def choose_many(prompt: str, choices: list[questionary.Choice], *,
                require_one: bool = False) -> list[Any] | None:
    """Tick boxes. Returns the ticked values, or None for Ctrl+C."""
    return _answer(questionary.checkbox(
        prompt, choices=choices, qmark="?", style=STYLE,
        instruction="(space to tick or untick, Enter when done)",
        validate=(lambda ticked: bool(ticked) or "Tick at least one.") if require_one
        else lambda ticked: True,
    ))


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


def _answer(question: questionary.Question, patch_stdout: bool = False) -> Any:
    """None if they press Ctrl+C, or Ctrl+D on an empty line (which raises EOFError)."""
    try:
        return question.ask(patch_stdout=patch_stdout, kbi_msg="")
    except EOFError:
        return None


def choice(title: str, value: Any = None, disabled: str | None = None,
           checked: bool = False) -> questionary.Choice:
    return questionary.Choice(title, value=value, disabled=disabled, checked=checked)
