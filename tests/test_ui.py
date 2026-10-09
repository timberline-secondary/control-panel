import pytest

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
