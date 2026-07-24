"""Tests for foreground/background guidance in the bash tool (Hermes port)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.agent_tools.subprocess_tools import foreground_background_guidance


def test_dev_server_gets_guidance():
    assert foreground_background_guidance("npm run dev") is not None
    assert foreground_background_guidance("uvicorn app:app --port 8000") is not None
    assert foreground_background_guidance("docker compose up") is not None
    assert foreground_background_guidance("python -m http.server 8080") is not None


def test_nohup_and_amp_get_guidance():
    assert foreground_background_guidance("nohup python worker.py") is not None
    assert foreground_background_guidance("python server.py &") is not None
    assert foreground_background_guidance("sleep 5 & wait") is not None


def test_help_version_never_blocked():
    assert foreground_background_guidance("uvicorn --help") is None
    assert foreground_background_guidance("nodemon --version") is None


def test_quoted_keywords_not_false_positive():
    assert foreground_background_guidance('git commit -m "add setsid handling"') is None
    assert foreground_background_guidance("echo 'run nodemon later'") is None


def test_ordinary_commands_pass():
    assert foreground_background_guidance("ls -la") is None
    assert foreground_background_guidance("pytest tests/ -q") is None
    assert foreground_background_guidance("npm install") is None
