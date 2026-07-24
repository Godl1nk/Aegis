"""Tests for src/env_scrub.py — subprocess env secret scrubbing (Hermes port)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.env_scrub import scrub_subprocess_env, _is_internal_secret


def test_provider_keys_stripped():
    env = {
        "PATH": "/usr/bin",
        "OPENAI_API_KEY": "sk-secret",
        "ANTHROPIC_API_KEY": "sk-ant",
        "EMBEDDING_API_KEY": "x",
    }
    out = scrub_subprocess_env(env)
    assert out["PATH"] == "/usr/bin"
    assert "OPENAI_API_KEY" not in out
    assert "ANTHROPIC_API_KEY" not in out
    assert "EMBEDDING_API_KEY" not in out


def test_tier1_secrets_stripped():
    env = {
        "GITHUB_TOKEN": "gh",
        "TELEGRAM_BOT_TOKEN": "tg",
        "EMAIL_PASSWORD": "pw",
        "DATABASE_URL": "postgres://u:pw@host/db",
        "EDITOR": "vim",
    }
    out = scrub_subprocess_env(env)
    assert out == {"EDITOR": "vim"}


def test_internal_dynamic_secrets_stripped():
    assert _is_internal_secret("AEGIS_WEBHOOK_TOKEN")
    assert _is_internal_secret("ODYSSEUS_VAULT_SECRET")
    assert _is_internal_secret("AEGIS_UTILITY_API_KEY")
    assert not _is_internal_secret("AEGIS_DATA_DIR")
    assert not _is_internal_secret("MY_PROJECT_API_KEY")  # not Aegis-managed

    out = scrub_subprocess_env({"AEGIS_FOO_TOKEN": "x", "AEGIS_DATA_DIR": "d"})
    assert "AEGIS_FOO_TOKEN" not in out
    assert out["AEGIS_DATA_DIR"] == "d"


def test_venv_markers_stripped():
    # Leaked VIRTUAL_ENV makes uv/poetry clobber the server venv (Hermes #23473).
    out = scrub_subprocess_env({"VIRTUAL_ENV": "/srv/venv", "CONDA_PREFIX": "/c", "PATH": "p"})
    assert "VIRTUAL_ENV" not in out
    assert "CONDA_PREFIX" not in out
    assert out["PATH"] == "p"


def test_ordinary_user_env_preserved():
    env = {"LANG": "en_US.UTF-8", "TERM": "xterm", "MY_APP_SETTING": "1", "JAVA_HOME": "/j"}
    assert scrub_subprocess_env(env) == env


def test_passthrough_optin_wins(monkeypatch):
    import src.env_scrub as es
    monkeypatch.setattr(es, "_passthrough_names", lambda: frozenset({"OPENAI_API_KEY"}))
    out = scrub_subprocess_env({"OPENAI_API_KEY": "sk", "GITHUB_TOKEN": "gh"})
    assert out.get("OPENAI_API_KEY") == "sk"
    assert "GITHUB_TOKEN" not in out
