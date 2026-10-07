import pytest

from control_panel import config


def test_defaults_when_there_is_no_file(tmp_path):
    settings = config.load(tmp_path / "missing.toml")
    assert settings.themes.host == "pi-themes.hackerspace.tbl"
    assert settings.themes.songs_dir == "/mnt/usb0"
    assert settings.themes.password is None


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


@pytest.mark.parametrize("text, message", [
    ('[themes]\nhots = "x"\n', "Unknown setting.*hots"),
    ("[tv]\n", "Unknown section.*tv"),
    ('[themes]\nport = "22"\n', "port.*whole number"),
    ("[themes]\nhost = 5\n", "host.*text"),
    ("[themes]\nport = true\n", "port.*whole number"),
    ("themes = 3\n", "should be a section"),
    ("[themes\n", "Couldn't read"),
])
def test_bad_config_explains_the_problem(tmp_path, text, message):
    path = tmp_path / "config.toml"
    path.write_text(text)
    with pytest.raises(config.ConfigError, match=message):
        config.load(path)
