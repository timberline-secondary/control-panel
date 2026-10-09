import pytest

from control_panel import login
from control_panel.config import ThemesSettings
from control_panel.ssh import AuthenticationFailed, ConnectionFailed, HostKeyChanged

RIGHT = "hunter2"
ACCOUNT = "pi@pi-themes.hackerspace.tbl"
SHARED = "pi@any-hackerspace-pi"


@pytest.fixture
def pi(monkeypatch, tmp_path):
    """Fakes out SSH, the prompts and the keychain. Returns a record of what happened."""
    record = {"answers": [], "typed": [], "tried": [], "keychain": {}, "errors": [],
              "keychain_available": True, "remember": True, "trust": True,
              "host_key_changed": False, "forgot_host": [], "confirms": []}

    class FakeConnection:
        def __init__(self, host, username, password, port=22, known_hosts=None):
            record["tried"].append(password)
            if record["host_key_changed"]:
                raise HostKeyChanged("it changed")
            if password == "slow":
                raise ConnectionFailed("stopped answering")
            if password != RIGHT:
                raise AuthenticationFailed("wrong password")
            self.password = password

    def ask_password(prompt):
        answer = record["answers"].pop(0)
        record["typed"].append(answer)
        return answer

    def confirm(prompt, default=True):
        record["confirms"].append(prompt)
        return record["trust"] if "Trust" in prompt else record["remember"]

    def forget_host_key(known_hosts, host, port):
        record["forgot_host"].append(host)
        record["host_key_changed"] = False

    monkeypatch.setattr(login, "PiConnection", FakeConnection)
    monkeypatch.setattr(login, "forget_host_key", forget_host_key)
    monkeypatch.setattr(login.config, "known_hosts_path", lambda: tmp_path / "known_hosts")
    monkeypatch.setattr(login, "_remembered_this_run", {})
    monkeypatch.setattr(login, "_rejected_this_run", set())
    monkeypatch.setattr(login.ui, "info", lambda text: None)
    monkeypatch.setattr(login.ui, "warning", lambda text: None)
    monkeypatch.setattr(login.ui, "error", record["errors"].append)
    monkeypatch.setattr(login.ui, "confirm", confirm)
    monkeypatch.setattr(login.ui, "ask_password", ask_password)
    monkeypatch.setattr(login, "_keychain_available", lambda: record["keychain_available"])
    monkeypatch.setattr(login, "_load", lambda account: record["keychain"].get(account))
    monkeypatch.setattr(login, "_save", record["keychain"].__setitem__)
    monkeypatch.setattr(login, "_forget", lambda account: record["keychain"].pop(account, None))
    return record


def test_password_from_config_is_used_without_asking(pi):
    assert login.connect(ThemesSettings(password=RIGHT)).password == RIGHT
    assert pi["typed"] == []


def test_asks_again_after_a_wrong_password_then_remembers_it(pi):
    pi["answers"] = ["oops", RIGHT]
    assert login.connect(ThemesSettings()).password == RIGHT
    assert pi["keychain"] == {ACCOUNT: RIGHT, SHARED: RIGHT}
    # Second time in the same run: no questions, and no second offer to remember it.
    assert login.connect(ThemesSettings()).password == RIGHT
    assert pi["typed"] == ["oops", RIGHT]
    assert len(pi["confirms"]) == 1


def test_a_stale_saved_password_is_forgotten(pi):
    pi["keychain"][ACCOUNT] = "old"
    pi["answers"] = [RIGHT]
    pi["remember"] = False
    assert login.connect(ThemesSettings()).password == RIGHT
    assert pi["keychain"] == {}


def test_a_wrong_config_password_is_tried_once_per_run(pi):
    pi["keychain"][ACCOUNT] = RIGHT
    settings = ThemesSettings(password="stale")
    assert login.connect(settings).password == RIGHT  # falls through to the keychain
    assert login.connect(settings).password == RIGHT
    assert pi["tried"] == ["stale", RIGHT, RIGHT]
    assert pi["typed"] == []
    assert pi["keychain"] == {ACCOUNT: RIGHT}  # not the keychain's fault, so it's kept
    assert "config file" in pi["errors"][0]


def test_a_slow_pi_does_not_forget_the_saved_password(pi):
    pi["keychain"][ACCOUNT] = "slow"
    with pytest.raises(ConnectionFailed):
        login.connect(ThemesSettings())
    assert pi["keychain"] == {ACCOUNT: "slow"}


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
    with pytest.raises(ConnectionFailed):
        login.connect(ThemesSettings())


def test_trusting_a_reinstalled_pi(pi):
    pi["host_key_changed"] = True
    assert login.connect(ThemesSettings(password=RIGHT)).password == RIGHT
    assert pi["forgot_host"] == ["pi-themes.hackerspace.tbl"]


def test_not_trusting_a_changed_pi_sends_no_password(pi):
    pi["host_key_changed"] = True
    pi["trust"] = False
    assert login.connect(ThemesSettings(password=RIGHT)) is None
    assert pi["forgot_host"] == []
    assert pi["tried"] == [RIGHT]  # the attempt that found the changed key; nothing more


def test_a_password_that_worked_on_another_pi_is_tried_first(pi):
    pi["answers"] = [RIGHT]
    login.connect(ThemesSettings())
    tv = ThemesSettings(host="pi-tv1.hackerspace.tbl")
    assert login.connect(tv).password == RIGHT
    assert pi["typed"] == [RIGHT]  # asked once, for the first Pi only


def test_another_pis_password_that_fails_is_tried_quietly(pi):
    login._remembered_this_run["pi@pi-tv1.hackerspace.tbl"] = "different"
    pi["answers"] = [RIGHT]
    assert login.connect(ThemesSettings()).password == RIGHT
    assert pi["tried"] == ["different", RIGHT]
    assert pi["errors"] == []  # it was only a guess


def test_the_shared_saved_password_works_for_a_new_pi(pi):
    pi["keychain"][SHARED] = RIGHT
    assert login.connect(ThemesSettings(host="pi-tv4.hackerspace.tbl")).password == RIGHT
    assert pi["typed"] == []
