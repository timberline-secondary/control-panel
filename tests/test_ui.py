import contextlib

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from control_panel import ui


@contextlib.contextmanager
def typed(keys):
    """Run the prompts inside as if keys were typed."""
    with create_pipe_input() as pipe:
        pipe.send_text(keys)
        with create_app_session(input=pipe, output=DummyOutput()):
            yield


ENTER, BACKSPACE, CTRL_D = "\r", "\x7f", "\x04"


@pytest.mark.parametrize("keys, default, expected", [
    ("  0027 " + ENTER, "", "0027"),
    ("q" + ENTER, "", None),
    ("Q" + ENTER, "2027", None),
    (ENTER, "2027", "2027"),  # Enter takes the default
    (ENTER, "", ""),
    (CTRL_D, "", None),  # Ctrl+D goes back instead of crashing
])
def test_ask(keys, default, expected):
    with typed(keys):
        assert ui.ask("Code", default=default) == expected


def test_passwords():
    with typed("q" + ENTER):
        assert ui.ask_password("Password") is None
    with typed("hackers" + ENTER):
        assert ui.ask_password("Password") == "hackers"


@pytest.mark.parametrize("keys, default, expected", [
    ("y" + ENTER, False, True),
    ("Yes" + ENTER, False, True),
    ("n" + ENTER, True, False),
    (ENTER, True, True),
    (ENTER, False, False),
    ("q" + ENTER, True, None),
    ("y" + BACKSPACE + "n" + ENTER, True, False),  # y alone doesn't answer: Enter does
    ("maybe" + ENTER + "y" + ENTER, False, True),  # asked again
])
def test_confirm(keys, default, expected):
    with typed(keys):
        assert ui.confirm("Sure?", default=default) is expected


def test_confirm_shows_what_enter_means(capsys):
    with typed(ENTER):
        ui.confirm("Delete it?", default=False)
    with typed("maybe" + ENTER + ENTER):
        ui.confirm("Delete it?", default=False)
    assert "Type y or n" in capsys.readouterr().out


MENU = [ui.choice("One", 1), ui.choice("Two", 2), ui.choice("Back to the TV menu", ui.BACK)]


@pytest.mark.parametrize("keys, expected", [
    ("2" + ENTER, 2),
    (ENTER, 1),  # Enter takes the first
    ("q" + ENTER, None),
    ("b" + ENTER, None),
    ("9" + ENTER + "1" + ENTER, 1),  # not a choice, so asked again
    ("two" + ENTER + "2" + ENTER, 2),
])
def test_choose(keys, expected):
    with typed(keys):
        assert ui.choose("Pick", MENU) == expected


def test_menus_are_numbered_with_q_to_go_back(capsys):
    with typed(ENTER):
        ui.choose("Pick", MENU)
    with typed("q" + ENTER):  # this menu has no Back of its own
        assert ui.choose("What now?", [ui.choice("Replace", "r"), ui.choice("Keep", "k")]) \
            is None
    with typed("q" + ENTER):
        ui.choose("Choose a panel", [ui.choice("TVs", "tvs"), ui.choice("Quit", ui.BACK)])
    with typed("1" + ENTER):  # plain text choices work too
        assert ui.choose("Pick", ["a", "b"]) == "a"
    shown = capsys.readouterr().out
    for line in ["Pick", "  1. One", "  2. Two", "  [q] Back to the TV menu", "  2. Keep",
                 "  [q] Back", "  [q]uit"]:
        assert f"{line}\n" in shown


def test_menus_skip_what_cant_be_chosen(capsys):
    choices = [ui.choice("TV 1", 1, disabled="it's off"), ui.choice("TV 2", 2)]
    with typed(ENTER):
        assert ui.choose("Which TV?", choices) == 2
    assert "  1. TV 2\n" in capsys.readouterr().out


TVS = [ui.choice("TV 1", 1, checked=True), ui.choice("TV 2", 2), ui.choice("TV 3", 3),
       ui.choice("TV 4", 4, checked=True)]


@pytest.mark.parametrize("keys, expected", [
    (ENTER, [1, 4]),  # Enter takes the ones marked [x]
    ("2" + ENTER, [2]),
    ("3 1" + ENTER, [1, 3]),
    ("1, 2-3" + ENTER, [1, 2, 3]),
    ("all" + ENTER, [1, 2, 3, 4]),
    ("q" + ENTER, None),
    ("5" + ENTER + "2" + ENTER, [2]),  # not a choice, so asked again
    ("3-1" + ENTER + "1" + ENTER, [1]),
])
def test_choose_many(keys, expected):
    with typed(keys):
        assert ui.choose_many("Which TVs?", TVS) == expected


def test_choose_many_shows_whats_ticked_and_can_require_one(capsys):
    choices = [ui.choice("a.mp4", "a"), ui.choice("b.mp4", "b")]
    with typed(ENTER):
        assert ui.choose_many("Which?", choices) == []  # Enter = none
    with typed(ENTER + "2" + ENTER):
        assert ui.choose_many("Which?", choices, require_one=True) == ["b"]
    with typed(ENTER):
        ui.choose_many("Which TVs?", TVS)
    shown = capsys.readouterr().out
    assert "Pick at least one." in shown
    assert "  1. [x] TV 1\n" in shown and "  2. [ ] TV 2\n" in shown
