import pytest

from control_panel import config


@pytest.fixture(autouse=True)
def no_config_env_var(monkeypatch):
    monkeypatch.delenv(config.CONFIG_ENV_VAR, raising=False)


def test_defaults_when_there_is_no_file(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "default_path", lambda: tmp_path / "config.toml")
    settings = config.load()
    assert settings.themes.host == "pi-themes.hackerspace.tbl"
    assert settings.themes.songs_dir == "/mnt/usb0"
    assert settings.themes.password is None


def test_a_chosen_file_that_is_missing_is_an_error(tmp_path, monkeypatch):
    with pytest.raises(config.ConfigError, match="Couldn't find the config file"):
        config.load(tmp_path / "typo.toml")
    monkeypatch.setenv(config.CONFIG_ENV_VAR, str(tmp_path / "typo.toml"))
    with pytest.raises(config.ConfigError, match="Couldn't find the config file"):
        config.load()


def test_overrides_only_what_is_given(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[themes]\nhost = "10.0.0.5"\nport = 2222\npassword = "s3cret"\n')
    themes = config.load(path).themes
    assert (themes.host, themes.port, themes.password) == ("10.0.0.5", 2222, "s3cret")
    assert themes.username == "pi"


def test_env_var_points_at_config(tmp_path, monkeypatch):
    path = tmp_path / "lab.toml"
    path.write_text('[themes]\nusername = "admin"\n')
    monkeypatch.setenv(config.CONFIG_ENV_VAR, str(path))
    assert config.load().themes.username == "admin"


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "utf-16"])
def test_files_saved_by_notepad_or_powershell(tmp_path, encoding):
    path = tmp_path / "config.toml"
    path.write_bytes('[themes]\nhost = "10.0.0.5"  # café\n'.encode(encoding))
    assert config.load(path).themes.host == "10.0.0.5"


@pytest.mark.parametrize("text, message", [
    ('[themes]\nhots = "x"\n', "Unknown setting.*hots"),
    ("[tv]\n", "Unknown section.*tv"),
    ('[themes]\nport = "22"\n', "port.*whole number"),
    ("[themes]\nhost = 5\n", "host.*text"),
    ("[themes]\nport = true\n", "port.*whole number"),
    ("[themes]\nport = 70000\n", "port.*between 1 and 65535"),
    ("[themes]\nport = 0\n", "port.*between 1 and 65535"),
    ("themes = 3\n", "should be a section"),
    ("[themes\n", "Couldn't read"),
])
def test_bad_config_explains_the_problem(tmp_path, text, message):
    path = tmp_path / "config.toml"
    path.write_text(text)
    with pytest.raises(config.ConfigError, match=message):
        config.load(path)


def test_file_in_another_encoding_explains_the_problem(tmp_path):
    path = tmp_path / "config.toml"
    path.write_bytes('[themes]\nhost = "café"\n'.encode("cp1252"))
    with pytest.raises(config.ConfigError, match="save it as UTF-8"):
        config.load(path)
