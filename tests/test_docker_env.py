"""Tests for src/docker_env.py (Hermes Docker backend port) — no daemon needed."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import docker_env
from src.docker_env import (
    _BASE_SECURITY_ARGS,
    _container_name,
    _sanitize_label_value,
    get_docker_settings,
)


def test_security_profile_matches_hermes():
    # The hardened container profile is copied from Hermes; a drift here
    # weakens the sandbox silently.
    joined = " ".join(_BASE_SECURITY_ARGS)
    assert "--cap-drop ALL" in joined
    assert "no-new-privileges" in joined
    assert "/tmp:rw,nosuid" in joined
    assert "/run:rw,noexec,nosuid" in joined


def test_label_sanitize():
    assert _sanitize_label_value("sess-123_ok.x") == "sess-123_ok.x"
    assert _sanitize_label_value("weird session!/id") == "weird-session--id"
    assert _sanitize_label_value("") == "unknown"
    assert len(_sanitize_label_value("x" * 200)) <= 63


def test_container_name_stable_and_safe():
    assert _container_name("abc") == "aegis-term-abc"
    assert _container_name("") == "aegis-term-default"
    assert _container_name("a/b c") == "aegis-term-a-b-c"


def test_settings_default_local(monkeypatch):
    import src.settings as settings_mod
    monkeypatch.setattr(settings_mod, "get_setting",
                        lambda key, default=None: default)
    s = get_docker_settings()
    assert s["env_type"] == "local"
    assert s["mount_workspace"] is False
    assert s["image"] == docker_env.DEFAULT_DOCKER_IMAGE


async def _noop_emit(evt):
    pass


def test_guard_wiring_docker_isolated_vs_mounted():
    """Dispatcher semantics: isolated docker skips the approval layer,
    a workspace mount restores it (Hermes has_host_access)."""
    import asyncio
    from src.command_approval import check_command_guard

    isolated = asyncio.run(check_command_guard(
        "rm -rf build/", session_id="d1", env_type="docker",
    ))
    assert isolated["approved"] is True

    mounted = asyncio.run(check_command_guard(
        "rm -rf build/", session_id="d1", env_type="docker",
        has_host_access=True,
    ))
    assert mounted["approved"] is False  # fails closed without emit_event
