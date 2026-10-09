import pytest

from control_panel import __version__, app


def test_version(capsys):
    with pytest.raises(SystemExit) as exit_info:
        app.main(["--version"])
    assert exit_info.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_bad_config_file_is_reported(tmp_path, capsys):
    path = tmp_path / "config.toml"
    path.write_text("[themes]\nnope = 1\n")
    assert app.main(["--config", str(path)]) == 1
    assert "nope" in capsys.readouterr().out


def test_self_test(capsys):
    assert app.main(["--self-test"]) == 0
    assert "passed" in capsys.readouterr().out
