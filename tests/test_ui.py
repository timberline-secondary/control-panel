import pytest

from control_panel import ui


@pytest.fixture
def typed(monkeypatch):
    """typed("a", "b") makes the next input() calls return those (then Ctrl+D)."""
    prompts = []

    def script(*answers):
        answers = iter(answers)

        def fake_input(prompt=""):
            prompts.append(prompt)
            answer = next(answers, None)
            if answer is None:
                raise EOFError
            if answer is KeyboardInterrupt:
                raise KeyboardInterrupt
            return answer
        monkeypatch.setattr("builtins.input", fake_input)
        return prompts
    return script


@pytest.mark.parametrize("answers, default, expected", [
    (["  0027 "], "", "0027"),
    (["q"], "", None),
    (["Q"], "2027", None),
    ([""], "2027", "2027"),  # Enter takes the default
    ([""], "", ""),
    ([], "", None),  # Ctrl+D goes back instead of crashing
    ([KeyboardInterrupt], "", None),
])
def test_ask(typed, answers, default, expected):
    typed(*answers)
    assert ui.ask("Type a code", default=default) == expected


def test_ask_says_what_q_and_enter_do(typed):
    prompts = typed("x", "y")
    ui.ask("Type something to say")
    ui.ask("Type their grad year", default="2027")
    assert prompts == ["Type something to say, or (q)uit: ",
                       "Type their grad year (Enter = 2027), or (q)uit: "]


@pytest.mark.parametrize("answers, default, expected", [
    (["y"], False, True),
    (["Yes"], False, True),
    (["n"], True, False),
    ([""], True, True),
    ([""], False, False),
    (["q"], True, None),
    (["maybe", "y"], False, True),  # asked again
])
def test_confirm(typed, answers, default, expected):
    typed(*answers)
    assert ui.confirm("Sure?", default=default) is expected


def test_confirm_shows_what_enter_means(typed):
    prompts = typed("", "")
    ui.confirm("Delete it?", default=False)
    ui.confirm("Carry on?")
    assert prompts == ["Delete it? (y/N): ", "Carry on? (Y/n): "]


MENU = [ui.choice("One", 1), ui.choice("Two", 2)]


@pytest.mark.parametrize("answers, expected", [
    (["2"], 2),
    (["q"], None),
    (["", "1"], 1),  # no default: Enter asks again
    (["9", "1"], 1),  # not a choice, so asked again
    (["two", "2"], 2),
    (["²", "2"], 2),  # a superscript 2 isn't a number to type
])
def test_choose(typed, answers, expected):
    typed(*answers)
    assert ui.choose(MENU) == expected


def test_menus_are_numbered_with_q_to_quit(typed, capsys):
    prompts = typed("1")
    assert ui.choose(["a", "b", ui.choice("Back", ui.BACK)], "Pick one") == "a"
    shown = capsys.readouterr().out
    assert shown == "Pick one\n  1. a\n  2. b\n\n\n"  # Back isn't listed: q does it
    assert prompts == ["Choose an item or (q)uit: "]


TVS = [ui.choice("TV 1", 1), ui.choice("TV 2", 2), ui.choice("TV 3", 3), ui.choice("TV 4", 4)]


@pytest.mark.parametrize("answers, expected", [
    (["2"], [2]),
    (["3 1"], [1, 3]),
    (["1, 2-3"], [1, 2, 3]),
    (["all"], [1, 2, 3, 4]),
    (["q"], None),
    (["", "2"], [2]),  # nothing picked, so asked again
    (["5", "2"], [2]),
    (["3-1", "1"], [1]),
])
def test_choose_many(typed, answers, expected):
    typed(*answers)
    assert ui.choose_many(TVS, "Which TVs?") == expected


def test_a_panel_clears_shows_its_heading_and_waits_after_each_task(typed, capsys,
                                                                    monkeypatch):
    done, cleared = [], []
    monkeypatch.setattr(ui, "clear", lambda: cleared.append(True))
    prompts = typed("2", "", "q")
    ui.run_menu("TV Art Shrines", [("Turn TVs on", "on"), ("Turn TVs off", "off")],
                done.append)
    assert done == ["off"]
    assert len(cleared) == 2  # on the way in, and coming back after the task
    shown = capsys.readouterr().out
    assert shown.count("TV ART SHRINES") == 2
    assert "Turn TVs off\n\n" in shown  # what was chosen, before its questions
    assert prompts == ["Choose an item or (q)uit: ", "\nPress Enter to go back.",
                       "Choose an item or (q)uit: "]


def test_the_main_menu_doesnt_wait(typed, monkeypatch):
    monkeypatch.setattr(ui, "clear", lambda: None)
    prompts = typed("1", "q")
    ui.run_menu("Main", [("A panel", "panel")], lambda panel: None, pause_after=False)
    assert prompts == ["Choose an item or (q)uit: "] * 2
