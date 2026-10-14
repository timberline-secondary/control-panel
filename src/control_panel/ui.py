"""Everything the person sees: menus, prompts and coloured messages."""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import questionary
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings, merge_key_bindings
from prompt_toolkit.keys import Keys
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

# Every answer is typed and then Enter is pressed (a stray key never answers anything), and
# q, then Enter, always goes back (or quits, at the main menu).


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
    """None if they typed q or pressed Ctrl+C."""
    answer = _answer(questionary.password(prompt, qmark=">", style=STYLE,
                                          instruction=QUIT_HINT))
    return None if answer is None or answer.strip().lower() == "q" else answer


_YES, _NO = {"y", "yes"}, {"n", "no"}


def confirm(prompt: str, default: bool = True) -> bool | None:
    """Yes or no, typed and then Enter (just Enter for the default). None (which also
    counts as no) if they type q or press Ctrl+C, for when that should mean 'stop' rather
    than 'no, carry on'."""
    answer = _answer(questionary.text(
        prompt, qmark="?", style=STYLE,
        instruction=f"({'Y/n' if default else 'y/N'}, q to go back)",
        validate=lambda text: text.strip().lower() in {"", "q", *_YES, *_NO}
        or "Type y or n, then press Enter (or q to go back).",
    ))
    return None if answer is None else _yes_or_no(answer, default)


def _yes_or_no(text: str, default: bool) -> bool | None:
    text = text.strip().lower()
    if text == "q":
        return None
    return default if not text else text in _YES


def pause(prompt: str = "Press Enter to carry on.") -> None:
    """Wait for Enter, e.g. so a message can be read before the screen is cleared."""
    try:
        input(prompt)
    except (EOFError, KeyboardInterrupt):
        pass


BACK = object()  # value of a menu's "Back"/"Quit" choice


def choose(prompt: str, choices: list[questionary.Choice | str]) -> Any:
    """Arrow keys or numbers to point at one, then Enter. q is the key for Back (or Quit),
    which every menu has. Returns None for Back or Ctrl+C."""
    choices = [_back_on_q(c) for c in choices]
    back = next((c for c in choices if isinstance(c, questionary.Choice) and c.value is BACK),
                None)
    if back is None:
        back = questionary.Choice("Back", value=BACK, shortcut_key="q")
        choices.append(back)
    leave = "quit" if str(back.title).lower().startswith("quit") else "go back"
    answer = _answer(questionary.select(
        prompt, choices=choices, qmark="?", style=STYLE, use_shortcuts=True,
        instruction=f"(arrow keys or numbers, then Enter; q to {leave})",
    ))
    return None if answer is BACK else answer


def _back_on_q(item: questionary.Choice | str) -> questionary.Choice | str:
    if isinstance(item, questionary.Choice) and item.value is BACK:
        return questionary.Choice(item.title, value=BACK, disabled=item.disabled,
                                  shortcut_key="q")
    return item


def choose_many(prompt: str, choices: list[questionary.Choice], *,
                require_one: bool = False) -> list[Any] | None:
    """Tick boxes, then Enter. Returns the ticked values, or None for q (then Enter) or
    Ctrl+C."""
    question = questionary.checkbox(
        prompt, choices=choices, qmark="?", style=STYLE,
        instruction="(space to tick or untick, then Enter; q then Enter to go back)",
        validate=(lambda ticked: bool(ticked) or "Tick at least one.") if require_one
        else lambda ticked: True,
    )
    _back_on_q_then_enter(question)
    return _answer(question)


def _back_on_q_then_enter(question: questionary.Question) -> None:
    """In a tick-box list: q, then Enter, goes back (answers None). Any other key in
    between (space, the arrows...) means they didn't mean it."""
    armed = False
    original = question.application.key_bindings
    extra = KeyBindings()

    def disarming(handler: Callable) -> Callable:
        def handle(event: Any) -> Any:
            nonlocal armed
            armed = False
            return handler(event)
        return handle

    for binding in original.bindings:
        if binding.keys not in (("q",), (Keys.ControlM,), (Keys.ControlC,), (Keys.Any,)):
            extra.add(*binding.keys, filter=binding.filter, eager=binding.eager)(
                disarming(binding.handler))

    @extra.add("q", eager=True)
    def _(event: Any) -> None:
        nonlocal armed
        armed = True

    @extra.add(Keys.ControlM, eager=True, filter=Condition(lambda: armed))
    def _(event: Any) -> None:
        event.app.exit(result=None)

    question.application.key_bindings = merge_key_bindings([original, extra])


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
