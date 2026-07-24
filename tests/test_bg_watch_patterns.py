"""Tests for bg-job watch_patterns (Hermes process_registry port) and the
extended `#!bg watch=` marker."""

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.tool_execution import _split_bg_marker
from src import bg_jobs


class TestBgMarkerParsing:
    def test_plain_marker(self):
        is_bg, cmd, watch = _split_bg_marker("#!bg\npip install torch")
        assert is_bg and cmd == "pip install torch" and watch is None

    def test_marker_with_watch(self):
        is_bg, cmd, watch = _split_bg_marker(
            "#!bg watch=Ready, listening on\npython server.py"
        )
        assert is_bg
        assert cmd == "python server.py"
        assert watch == ["Ready", "listening on"]

    def test_non_marker_content_untouched(self):
        is_bg, cmd, watch = _split_bg_marker("echo hi")
        assert not is_bg and cmd == "echo hi" and watch is None

    def test_alt_marker_with_watch(self):
        is_bg, cmd, watch = _split_bg_marker("#bg watch=done\nmake build")
        assert is_bg and cmd == "make build" and watch == ["done"]


@pytest.fixture
def jobs_env(tmp_path, monkeypatch):
    """Point bg_jobs at a temp store + jobs dir."""
    monkeypatch.setattr(bg_jobs, "_JOBS_DIR", tmp_path / "bg_jobs")
    monkeypatch.setattr(bg_jobs, "_STORE", tmp_path / "bg_jobs.json")
    (tmp_path / "bg_jobs").mkdir()
    return tmp_path


def _mk_watched_job(jobs_env, patterns, log_lines=""):
    log = jobs_env / "bg_jobs" / "j1.log"
    log.write_text(log_lines, encoding="utf-8")
    rec = {
        "id": "j1",
        "session_id": "s1",
        "command": "python server.py",
        "status": "running",
        "pid": 999999,
        "started_at": time.time(),
        "ended_at": None,
        "exit_code": None,
        "max_runtime_s": 3600,
        "followed_up": False,
        "log_path": str(log),
        "exit_path": str(jobs_env / "bg_jobs" / "j1.exit"),
        "watch_patterns": list(patterns),
        "watch_offset": 0,
        "watch_cooldown_until": 0.0,
        "watch_strikes": 0,
        "watch_strike_candidate": False,
        "watch_disabled": False,
        "watch_suppressed": 0,
    }
    bg_jobs._save({"j1": rec})
    return log


class TestWatchEvents:
    def test_match_emits_event_and_starts_cooldown(self, jobs_env):
        log = _mk_watched_job(jobs_env, ["Ready"], "boot...\nServer Ready on :8000\n")
        events = bg_jobs.check_watch_events()
        assert len(events) == 1
        assert events[0]["type"] == "watch_match"
        assert events[0]["pattern"] == "Ready"
        assert "Server Ready" in events[0]["output"]
        rec = bg_jobs._load()["j1"]
        assert rec["watch_cooldown_until"] > time.time()

    def test_no_match_no_event(self, jobs_env):
        _mk_watched_job(jobs_env, ["Ready"], "still booting\n")
        assert bg_jobs.check_watch_events() == []

    def test_offset_prevents_rescan(self, jobs_env):
        log = _mk_watched_job(jobs_env, ["Ready"], "Ready\n")
        assert len(bg_jobs.check_watch_events()) == 1
        # Same content, nothing new appended → no duplicate event even after
        # cooldown (offset advanced past the match).
        rec = bg_jobs._load()["j1"]
        rec["watch_cooldown_until"] = 0.0
        bg_jobs._save({"j1": rec})
        assert bg_jobs.check_watch_events() == []

    def test_rate_limit_suppresses_within_cooldown(self, jobs_env):
        log = _mk_watched_job(jobs_env, ["ERROR"], "ERROR one\n")
        assert len(bg_jobs.check_watch_events()) == 1
        # New match inside the cooldown window → suppressed, one strike.
        with open(log, "a", encoding="utf-8") as f:
            f.write("ERROR two\n")
        assert bg_jobs.check_watch_events() == []
        rec = bg_jobs._load()["j1"]
        assert rec["watch_strikes"] == 1
        assert rec["watch_suppressed"] >= 1

    def test_three_strikes_disable_watch(self, jobs_env):
        log = _mk_watched_job(jobs_env, ["ERROR"], "ERROR 0\n")
        assert len(bg_jobs.check_watch_events()) == 1
        events = []
        for i in range(1, 4):
            # Each iteration: new match arrives inside a fresh cooldown window.
            with open(log, "a", encoding="utf-8") as f:
                f.write(f"ERROR {i}\n")
            # Force strike-candidate reset per window while keeping cooldown
            # active, emulating consecutive rate-limit windows.
            rec = bg_jobs._load()["j1"]
            rec["watch_strike_candidate"] = False
            rec["watch_cooldown_until"] = time.time() + 15
            bg_jobs._save({"j1": rec})
            events.extend(bg_jobs.check_watch_events())
        rec = bg_jobs._load()["j1"]
        assert rec["watch_disabled"] is True
        assert any(e["type"] == "watch_disabled" for e in events)
        # Disabled job produces no further events.
        with open(log, "a", encoding="utf-8") as f:
            f.write("ERROR again\n")
        assert bg_jobs.check_watch_events() == []

    def test_clean_window_resets_strikes(self, jobs_env):
        log = _mk_watched_job(jobs_env, ["hit"], "hit 1\n")
        assert len(bg_jobs.check_watch_events()) == 1
        rec = bg_jobs._load()["j1"]
        rec["watch_strikes"] = 2
        rec["watch_strike_candidate"] = False
        rec["watch_cooldown_until"] = time.time() - 1  # expired, clean window
        bg_jobs._save({"j1": rec})
        with open(log, "a", encoding="utf-8") as f:
            f.write("hit 2\n")
        events = bg_jobs.check_watch_events()
        assert len(events) == 1
        assert bg_jobs._load()["j1"]["watch_strikes"] == 0

    def test_unwatched_job_ignored(self, jobs_env):
        log = _mk_watched_job(jobs_env, ["Ready"], "Ready\n")
        rec = bg_jobs._load()["j1"]
        del rec["watch_patterns"]
        bg_jobs._save({"j1": rec})
        assert bg_jobs.check_watch_events() == []
