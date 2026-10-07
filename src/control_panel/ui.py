"""Everything the person sees: menus, prompts and coloured messages."""

from __future__ import annotations

import sys
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
    answer = questionary.text(prompt, default=default, qmark=">", style=STYLE).ask(
        patch_stdout=patch_stdout, kbi_msg=""
    )
    if answer is None or answer.strip().lower() == "q":
        return None
    return answer.strip()


def ask_password(prompt: str) -> str | None:
    return questionary.password(prompt, qmark=">", style=STYLE).ask(kbi_msg="")


def confirm(prompt: str, default: bool = True) -> bool:
    return bool(questionary.confirm(prompt, default=default, qmark="?", style=STYLE).ask(
        kbi_msg=""
    ))


BACK = object()  # value of a menu's "Back"/"Quit" choice


def choose(prompt: str, choices: list[questionary.Choice | str]) -> Any:
    """Arrow keys or number keys to pick. Returns None for BACK or Ctrl+C."""
    answer = questionary.select(
        prompt, choices=choices, qmark="?", style=STYLE, use_shortcuts=True,
        instruction="(use arrow keys or numbers)",
    ).ask(kbi_msg="")
    return None if answer is BACK else answer


def choice(title: str, value: Any = None, disabled: str | None = None) -> questionary.Choice:
    return questionary.Choice(title, value=value, disabled=disabled)
