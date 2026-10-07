import pytest

from control_panel import login
from control_panel.config import ThemesSettings
from control_panel.ssh import AuthenticationFailed

RIGHT = "hunter2"


class FakeConnection:
    def __init__(self, host, username, password, port=22):
        if password != RIGHT:
            raise AuthenticationFailed("wrong password")
        self.password = password


@pytest.fixture
def pi(monkeypatch):
    """Fakes out SSH, the prompts and the keychain. Returns a record of what happened."""
    record = {"typed": [], "keychain": {}, "keychain_available": True, "remember": True}
    monkeypatch.setattr(login, "PiConnection", FakeConnection)
    monkeypatch.setattr(login, "_remembered_this_run", {})
    monkeypatch.setattr(login.ui, "info", lambda text: None)
    monkeypatch.setattr(login.ui, "error", lambda text: None)
    monkeypatch.setattr(login.ui, "confirm", lambda prompt, default=True: record["remember"])

    def ask_password(prompt):
        answer = record["answers"].pop(0)
        record["typed"].append(answer)
        return answer

    monkeypatch.setattr(login.ui, "ask_password", ask_password)
    monkeypatch.setattr(login, "_keychain_available", lambda: record["keychain_available"])
    monkeypatch.setattr(login, "_load", lambda account: record["keychain"].get(account))
    monkeypatch.setattr(login, "_save", record["keychain"].__setitem__)
    monkeypatch.setattr(login, "_forget", lambda account: record["keychain"].pop(account, None))
    return record


def test_password_from_config_is_used_without_asking(pi):
    pi["answers"] = []
    assert login.connect(ThemesSettings(password=RIGHT)).password == RIGHT
    assert pi["typed"] == []


def test_asks_again_after_a_wrong_password_then_remembers_it(pi):
    pi["answers"] = ["oops", RIGHT]
    assert login.connect(ThemesSettings()).password == RIGHT
    assert pi["keychain"] == {"pi@pi-themes.hackerspace.tbl": RIGHT}
    # Second time in the same run: no questions.
    assert login.connect(ThemesSettings()).password == RIGHT
    assert pi["typed"] == ["oops", RIGHT]


def test_a_stale_saved_password_is_forgotten(pi):
    pi["keychain"]["pi@pi-themes.hackerspace.tbl"] = "old"
    pi["answers"] = [RIGHT]
    pi["remember"] = False
    assert login.connect(ThemesSettings()).password == RIGHT
    assert pi["keychain"] == {}


def test_not_offered_to_remember_without_a_keychain(pi, monkeypatch):
    pi["answers"] = [RIGHT]
    pi["keychain_available"] = False
    monkeypatch.setattr(login.ui, "confirm", lambda *a, **k: pytest.fail("shouldn't ask"))
    assert login.connect(ThemesSettings()).password == RIGHT


def test_cancelling_the_prompt(pi):
    pi["answers"] = [None]
    assert login.connect(ThemesSettings()) is None


def test_gives_up_after_too_many_wrong_passwords(pi):
    pi["answers"] = ["a", "b", "c"]
    with pytest.raises(login.ConnectionFailed):
        login.connect(ThemesSettings())
