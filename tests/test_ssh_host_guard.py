"""ssh_command/run_script must refuse non-routable hosts (link-local cloud
metadata, multicast, reserved, unspecified) without starting a process;
loopback, LAN, public and hostname targets keep working."""
import asyncio

from src.builtin_actions import (
    _ssh_host_blocked,
    action_run_script,
    action_ssh_command,
)


def test_non_routable_hosts_blocked():
    for host in ("169.254.169.254", "169.254.10.20", "224.0.0.1",
                 "0.0.0.0", "240.0.0.1", "fe80::1", "::"):
        assert _ssh_host_blocked(host), host


def test_legit_hosts_allowed():
    for host in ("localhost", "127.0.0.1", "::1", "192.168.1.50",
                 "10.0.0.5", "93.184.216.34", "example.com", "", None):
        assert _ssh_host_blocked(host) is None, host


def test_ssh_command_blocked_host_runs_nothing():
    out, ok = asyncio.run(action_ssh_command("admin", "whoami", host="169.254.169.254"))
    assert ok is False
    assert "refusing" in out


def test_run_script_blocked_host_runs_nothing(monkeypatch):
    monkeypatch.delenv("ODYSSEUS_SCRIPT_HOST", raising=False)
    out, ok = asyncio.run(action_run_script("admin", "whoami", host="169.254.169.254"))
    assert ok is False
    assert "refusing" in out
