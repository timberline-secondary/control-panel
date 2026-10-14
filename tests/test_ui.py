import contextlib

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from control_panel import ui


class Question:
    def __init__(self, answer=None, raises=None):
        self.answer, self.raises = answer, raises

    def ask(self, **kwargs):
        if self.raises:
            raise self.raises
        return self.answer


@pytest.mark.parametrize("answer, expected", [("  0027 ", "0027"), ("q", None), ("Q", None),
                                              (None, None)])
def test_ask(monkeypatch, answer, expected):
    monkeypatch.setattr(ui.questionary, "text", lambda *a, **k: Question(answer))
    assert ui.ask("Code") == expected


def test_ctrl_d_goes_back_instead_of_crashing(monkeypatch):
    for name in ["text", "password", "confirm", "select"]:
        monkeypatch.setattr(ui.questionary, name, lambda *a, **k: Question(raises=EOFError()))
    assert ui.ask("Code") is None
    assert ui.ask_password("Password") is None
    assert ui.confirm("Sure?") is None  # which counts as no
    assert ui.choose("Pick", ["a"]) is None


def test_choose_back(monkeypatch):
    monkeypatch.setattr(ui.questionary, "select", lambda *a, **k: Question(ui.BACK))
    assert ui.choose("Pick", ["a"]) is None


# --- Real key presses ------------------------------------------------------------------------

@contextlib.contextmanager
def typed(keys):
    """Run the prompts inside as if keys were typed."""
    with create_pipe_input() as pipe:
        pipe.send_text(keys)
        with create_app_session(input=pipe, output=DummyOutput()):
            yield


ENTER, BACKSPACE = "\r", "\x7f"


@pytest.mark.parametrize("keys, default, expected", [
    ("y" + ENTER, False, True),
    ("Yes" + ENTER, False, True),
    ("n" + ENTER, True, False),
    (ENTER, True, True),
    (ENTER, False, False),
    ("q" + ENTER, True, None),
    ("y" + BACKSPACE + "n" + ENTER, True, False),  # y alone doesn't answer: Enter does
    ("maybe" + ENTER + BACKSPACE * 5 + "y" + ENTER, False, True),  # asked again
])
def test_confirm_needs_enter(keys, default, expected):
    with typed(keys):
        assert ui.confirm("Sure?", default=default) is expected


def test_menus_need_enter_and_q_goes_back():
    choices = [ui.choice("One", 1), ui.choice("Two", 2), ui.choice("Back", ui.BACK)]
    with typed("2" + ENTER):
        assert ui.choose("Pick", choices) == 2
    with typed("2" + "1" + ENTER):  # a number only points at it
        assert ui.choose("Pick", choices) == 1
    with typed("q" + ENTER):
        assert ui.choose("Pick", choices) is None
    with typed("q" + "1" + ENTER):  # changed their mind
        assert ui.choose("Pick", choices) == 1


def test_every_menu_can_be_left_with_q():
    with typed("q" + ENTER):  # this menu has no Back of its own
        assert ui.choose("What now?", [ui.choice("Replace", "r"), ui.choice("Keep", "k")]) \
            is None


def test_tick_boxes_need_enter_and_q_goes_back():
    def choices():
        return [ui.choice("TV 1", 1), ui.choice("TV 2", 2, checked=True)]
    with typed(" " + ENTER):
        assert ui.choose_many("Which?", choices()) == [1, 2]
    with typed("q" + ENTER):
        assert ui.choose_many("Which?", choices(), require_one=True) is None
    with typed("q" + " " + ENTER):  # anything after q means they didn't mean it
        assert ui.choose_many("Which?", choices()) == [1, 2]


def test_text_and_passwords_need_enter_and_q_goes_back():
    with typed("q" + ENTER):
        assert ui.ask("Name") is None
    with typed("q" + ENTER):
        assert ui.ask_password("Password") is None
    with typed("hackers" + ENTER):
        assert ui.ask_password("Password") == "hackers"
