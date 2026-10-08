import socket

import paramiko
import pytest

from control_panel import ssh


@pytest.fixture
def connect_raises(monkeypatch):
    """Make paramiko's connect() fail with the given exception."""
    def install(exception):
        def connect(self, *args, **kwargs):
            raise exception
        monkeypatch.setattr(paramiko.SSHClient, "connect", connect)
    return install


@pytest.mark.parametrize("exception, expected, message", [
    (paramiko.AuthenticationException("Authentication failed."),
     ssh.AuthenticationFailed, "didn't accept that password"),
    (paramiko.AuthenticationException("Authentication timeout."),
     ssh.ConnectionFailed, "stopped answering"),
    (paramiko.AuthenticationException(
        "Authentication failed: transport shut down or saw EOF"),
     ssh.ConnectionFailed, "stopped answering"),
    (paramiko.BadHostKeyException("pi", paramiko.RSAKey.generate(1024),
                                  paramiko.RSAKey.generate(1024)),
     ssh.HostKeyChanged, "isn't the same computer"),
    (socket.gaierror(-2, "Name or service not known"), ssh.ConnectionFailed, "Couldn't find"),
    (TimeoutError("timed out"), ssh.ConnectionFailed, "plugged in and turned on"),
])
def test_connection_problems_are_explained(connect_raises, exception, expected, message):
    connect_raises(exception)
    with pytest.raises(expected, match=message) as raised:
        ssh.PiConnection("pi-themes", "pi", "pw")
    if expected is ssh.ConnectionFailed:  # and not the "wrong password" subclass
        assert type(raised.value) is ssh.ConnectionFailed


def test_known_hosts_file_is_created(connect_raises, tmp_path):
    connect_raises(TimeoutError())
    known_hosts = tmp_path / "new folder" / "known_hosts"
    with pytest.raises(ssh.ConnectionFailed):
        ssh.PiConnection("pi-themes", "pi", "pw", known_hosts=known_hosts)
    assert known_hosts.exists()


def test_forget_host_key(tmp_path):
    known_hosts = tmp_path / "known_hosts"
    keys = paramiko.HostKeys()
    key = paramiko.RSAKey.generate(1024)
    for name in ["pi-themes", "[pi-themes]:2222", "pi-tv1"]:
        keys.add(name, key.get_name(), key)
    keys.save(str(known_hosts))

    ssh.forget_host_key(known_hosts, "pi-themes")
    assert set(paramiko.HostKeys(str(known_hosts)).keys()) == {"[pi-themes]:2222", "pi-tv1"}
    ssh.forget_host_key(known_hosts, "pi-themes", port=2222)
    assert set(paramiko.HostKeys(str(known_hosts)).keys()) == {"pi-tv1"}
    ssh.forget_host_key(tmp_path / "missing", "pi-themes")  # nothing to forget is fine
