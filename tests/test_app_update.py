"""Self-update engine tests (offline — network paths are stubbed)."""
import io
import json
import os
import zipfile

import pytest

from src import app_update


def _make_zip(path, top="Aegis-main", extra=None, markers=True):
    files = {}
    if markers:
        files.update({
            f"{top}/app.py": "# app",
            f"{top}/src/constants.py": 'APP_VERSION = "9.9.9"',
            f"{top}/docker-compose.yml": "services: {}",
            f"{top}/static/index.html": "<html></html>",
        })
    files.update(extra or {})
    with zipfile.ZipFile(path, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return path


def test_deploy_stamp_beats_stale_state_baseline(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    (root / ".deploy-commit").write_text("a" * 40 + "\n")
    data_dir = str(tmp_path / "data")
    os.makedirs(data_dir)
    monkeypatch.setattr(app_update, "_app_root", lambda: str(root))
    app_update._save_state({"installed_commit": "b" * 40}, data_dir)
    assert app_update.installed_commit(data_dir) == "a" * 40


def test_malformed_deploy_stamp_falls_back_to_state(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    (root / ".deploy-commit").write_text("not-a-sha")
    data_dir = str(tmp_path / "data")
    os.makedirs(data_dir)
    monkeypatch.setattr(app_update, "_app_root", lambda: str(root))
    app_update._save_state({"installed_commit": "b" * 40}, data_dir)
    assert app_update.installed_commit(data_dir) == "b" * 40


def test_host_allowlist():
    assert app_update._check_host_allowed("https://api.github.com/x")
    assert app_update._check_host_allowed("https://codeload.github.com/x/y.zip")
    assert app_update._check_host_allowed("https://objects.githubusercontent.com/x")
    assert not app_update._check_host_allowed("https://evil.example.com/x")
    assert not app_update._check_host_allowed("https://api.github.com.evil.com/x")
    assert not app_update._check_host_allowed("not a url")


def test_parse_remote_version():
    assert app_update.parse_remote_version('APP_VERSION = "1.2.3"\n') == "1.2.3"
    assert app_update.parse_remote_version("APP_VERSION='4.5.6'") == "4.5.6"
    assert app_update.parse_remote_version("no version here") is None


def test_ref_validation():
    assert app_update._zip_ref_url("main").endswith("/zip/main")
    with pytest.raises(app_update.UpdateError):
        app_update._zip_ref_url("main; rm -rf /")
    with pytest.raises(app_update.UpdateError):
        app_update._zip_ref_url("../escape")


def test_verify_markers(tmp_path):
    good = _make_zip(str(tmp_path / "good.zip"))
    assert app_update.verify_staging(good) == "Aegis-main"
    bad = _make_zip(str(tmp_path / "bad.zip"), extra={}, markers=False)
    open(bad, "ab").close()
    with zipfile.ZipFile(str(tmp_path / "thin.zip"), "w") as zf:
        zf.writestr("Aegis-main/app.py", "# only")
    with pytest.raises(app_update.UpdateError):
        app_update.verify_staging(str(tmp_path / "thin.zip"))
    with open(str(tmp_path / "junk.zip"), "wb") as f:
        f.write(b"not a zip")
    with pytest.raises(app_update.UpdateError):
        app_update.verify_staging(str(tmp_path / "junk.zip"))
    with pytest.raises(app_update.UpdateError):
        app_update.verify_staging(str(tmp_path / "missing.zip"))


def test_safe_extract_refuses_zip_slip(tmp_path):
    evil = str(tmp_path / "evil.zip")
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("Aegis-main/app.py", "# app")
        zf.writestr("Aegis-main/../../evil.txt", "pwned")
        zf.writestr("Aegis-main/src/constants.py", "x")
        zf.writestr("Aegis-main/docker-compose.yml", "x")
        zf.writestr("Aegis-main/static/index.html", "x")
    out = str(tmp_path / "out")
    os.makedirs(out)
    with pytest.raises(app_update.UpdateError):
        app_update._safe_extract(evil, out)
    assert not os.path.exists(os.path.join(str(tmp_path), "evil.txt"))


def _seed_root(root):
    os.makedirs(os.path.join(root, "data"))
    os.makedirs(os.path.join(root, "logs"))
    open(os.path.join(root, "app.py"), "w").write("# old")
    open(os.path.join(root, "keepme.txt"), "w").write("custom")
    open(os.path.join(root, "data", "auth.json"), "w").write("{}")
    open(os.path.join(root, "logs", "x.log"), "w").write("log")
    open(os.path.join(root, ".env"), "w").write("K=V")


def test_apply_preserves_and_backups_orphans(tmp_path, monkeypatch):
    root = tmp_path / "approot"
    root.mkdir()
    _seed_root(str(root))
    data_dir = str(root / "data")
    zp = _make_zip(str(tmp_path / "up.zip"),
                   extra={"Aegis-main/newfile.txt": "new", "Aegis-main/app.py": "# new"})
    sha = "a" * 40
    monkeypatch.setattr(app_update, "_app_root", lambda: str(root))
    monkeypatch.setattr(app_update, "detect_environment",
                        lambda: {"docker": False, "frozen": False, "source": True,
                                 "docker_socket": False, "compose": None})
    restarted = []
    monkeypatch.setattr(app_update, "_schedule_restart", lambda delay_s=3: restarted.append(delay_s))
    monkeypatch.setattr(app_update, "active_stream_count", lambda: 0)
    app_update._save_state({"installed_commit": "b" * 40, "staged": {
        "commit": sha, "path": zp, "root": "Aegis-main", "at": 0}}, data_dir)
    result = app_update.apply_staged(sha, data_dir)
    assert result["applied"] is True and result["mode"] == "source"
    assert open(os.path.join(str(root), "app.py")).read() == "# new"
    assert open(os.path.join(str(root), "newfile.txt")).read() == "new"
    # preserved
    assert open(os.path.join(str(root), "data", "auth.json")).read() == "{}"
    assert open(os.path.join(str(root), ".env")).read() == "K=V"
    assert os.path.exists(os.path.join(str(root), "logs", "x.log"))
    # orphan custom file moved to backup, not deleted
    assert not os.path.exists(os.path.join(str(root), "keepme.txt"))
    assert result["orphans_moved"] == ["keepme.txt"]
    assert open(os.path.join(result["backup"], "orphans", "keepme.txt")).read() == "custom"
    assert open(os.path.join(result["backup"], ".env")).read() == "K=V"
    assert restarted == [3]
    state = app_update.load_state(data_dir)
    assert state["installed_commit"] == sha
    assert state["staged"] is None
    assert state["history"][-1]["previous"] == "b" * 40
    # Full data backup + replaced-tree backup + completed manifest.
    assert open(os.path.join(result["backup"], "data", "auth.json")).read() == "{}"
    assert open(os.path.join(result["backup"], "replaced", "app.py")).read() == "# old"
    manifest = json.load(open(os.path.join(result["backup"], "manifest.json")))
    assert manifest["status"] == "complete"
    assert manifest["data_bytes"] > 0 and manifest["replaced_bytes"] > 0
    assert result["verified"] is True and result["backup_bytes"] > 0


def test_apply_requires_staged_match(tmp_path, monkeypatch):
    data_dir = str(tmp_path / "data")
    os.makedirs(data_dir)
    monkeypatch.setattr(app_update, "_app_root", lambda: str(tmp_path))
    app_update._save_state({"staged": {"commit": "a" * 40, "path": "/nonexistent.zip"}}, data_dir)
    with pytest.raises(app_update.UpdateError):
        app_update.apply_staged("b" * 40, data_dir)
    with pytest.raises(app_update.UpdateError):
        app_update.apply_staged("a" * 40, data_dir)


def test_rollback_picks_previous(tmp_path, monkeypatch):
    data_dir = str(tmp_path / "data")
    os.makedirs(data_dir)
    app_update._save_state({
        "installed_commit": "b" * 40,
        "history": [{"commit": "a" * 40, "at": "t", "backup": "/x", "previous": None},
                    {"commit": "b" * 40, "at": "t", "backup": "/y", "previous": "a" * 40}],
    }, data_dir)
    calls = []
    monkeypatch.setattr(app_update, "download_update", lambda ref, dd=None: calls.append(("dl", ref)) or {"staged": True})
    monkeypatch.setattr(app_update, "apply_staged", lambda commit=None, dd=None: calls.append(("apply", commit)) or {"applied": True})
    app_update.rollback(data_dir)
    assert calls == [("dl", "a" * 40), ("apply", "a" * 40)]


def test_rollback_without_history(tmp_path):
    data_dir = str(tmp_path / "data")
    os.makedirs(data_dir)
    app_update._save_state({"installed_commit": "b" * 40, "history": []}, data_dir)
    with pytest.raises(app_update.UpdateError):
        app_update.rollback(data_dir)


def test_op_lock_busy(tmp_path):
    # The lock is re-entrant for the owning thread (rollback calls
    # download/apply internally) but fails fast for OTHER threads.
    import threading
    held = threading.Event()
    release = threading.Event()

    def _holder():
        app_update._op_lock.acquire()
        held.set()
        release.wait(timeout=30)
        app_update._op_lock.release()

    t = threading.Thread(target=_holder, daemon=True)
    t.start()
    assert held.wait(timeout=10)
    try:
        with pytest.raises(app_update.UpdateBusy):
            app_update.download_update(None, str(tmp_path))
    finally:
        release.set()
        t.join(timeout=10)


def test_status_shape(tmp_path, monkeypatch):
    data_dir = str(tmp_path / "data")
    os.makedirs(data_dir)
    monkeypatch.setattr(app_update, "installed_commit", lambda dd=None: None)
    st = app_update.status(data_dir)
    assert st["repo"] == app_update.REPO
    assert st["current"]["version"] == app_update.local_version()
    assert st["unknown_baseline"] is True
    assert st["can_rollback"] is False
    assert st["staged"] is None
    assert set(st["environment"]) >= {"docker", "frozen", "source"}


def test_clear_pycache_skips_venvs(tmp_path):
    root = tmp_path / "r"
    (root / "pkg" / "__pycache__").mkdir(parents=True)
    (root / "pkg" / "__pycache__" / "a.pyc").write_text("x")
    (root / ".venv" / "lib" / "__pycache__").mkdir(parents=True)
    (root / ".venv" / "lib" / "__pycache__" / "b.pyc").write_text("x")
    assert app_update._clear_pycache(str(root)) == 1
    assert not (root / "pkg" / "__pycache__").exists()
    assert (root / ".venv" / "lib" / "__pycache__" / "b.pyc").exists()


def _seed_apply(tmp_path, monkeypatch, app_body="# new"):
    # Faithful layout: updater state/staging/backups live INSIDE the app
    # data dir (like production DATA_DIR), which the apply must preserve
    # while still backing up.
    root = tmp_path / "approot"
    root.mkdir(exist_ok=True)
    _seed_root(str(root))
    data_dir = str(root / "data")
    zp = _make_zip(str(tmp_path / "up.zip"),
                   extra={"Aegis-main/newfile.txt": "new", "Aegis-main/app.py": app_body})
    sha = "c" * 40
    monkeypatch.setattr(app_update, "_app_root", lambda: str(root))
    monkeypatch.setattr(app_update, "detect_environment",
                        lambda: {"docker": False, "frozen": False, "source": True,
                                 "docker_socket": False, "compose": None})
    monkeypatch.setattr(app_update, "_schedule_restart", lambda delay_s=3: None)
    monkeypatch.setattr(app_update, "active_stream_count", lambda: 0)
    app_update._save_state({"installed_commit": "b" * 40, "staged": {
        "commit": sha, "path": zp, "root": "Aegis-main", "at": 0}}, data_dir)
    return str(root), data_dir, sha


def test_preflight_refuses_without_disk(tmp_path, monkeypatch):
    _root, data_dir, sha = _seed_apply(tmp_path, monkeypatch)
    import shutil as _shutil
    from collections import namedtuple
    _usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(_shutil, "disk_usage", lambda p: _usage(10**12, 10**12 - 1024, 1024))
    with pytest.raises(app_update.UpdateError, match="Not enough free"):
        app_update.apply_staged(sha, data_dir)
    # Nothing was replaced.
    assert open(os.path.join(_root, "app.py")).read() == "# old"


def test_verify_failure_auto_restores(tmp_path, monkeypatch):
    root, data_dir, sha = _seed_apply(tmp_path, monkeypatch)
    monkeypatch.setattr(app_update, "_verify_tree",
                        lambda r: (_ for _ in ()).throw(app_update.UpdateError("simulated corruption")))
    with pytest.raises(app_update.UpdateError, match="automatically rolled back"):
        app_update.apply_staged(sha, data_dir)
    assert open(os.path.join(root, "app.py")).read() == "# old"
    # newfile.txt came only from the update; after restore it must be gone.
    assert not os.path.exists(os.path.join(root, "newfile.txt"))


def test_stream_guard_and_force(tmp_path, monkeypatch):
    _root, data_dir, sha = _seed_apply(tmp_path, monkeypatch)
    monkeypatch.setattr(app_update, "active_stream_count", lambda: 2)
    with pytest.raises(app_update.UpdateError, match="stream\\(s\\) active"):
        app_update.apply_staged(sha, data_dir)
    result = app_update.apply_staged(sha, data_dir, force=True)
    assert result["applied"] is True


def test_boot_recovery_restores_interrupted(tmp_path, monkeypatch):
    root = tmp_path / "approot"
    root.mkdir()
    (root / "app.py").write_text("# half-written")
    data_dir = str(tmp_path / "data")
    backups = os.path.join(data_dir, "update_backups", "pre-update-x")
    os.makedirs(os.path.join(backups, "replaced"))
    open(os.path.join(backups, "replaced", "app.py"), "w").write("# old")
    with open(os.path.join(backups, "manifest.json"), "w") as f:
        json.dump({"status": "in-progress", "to_commit": "c" * 40}, f)
    monkeypatch.setattr(app_update, "_app_root", lambda: str(root))
    # point backups dir at our tmp data dir
    monkeypatch.setattr(app_update, "_backups_dir", lambda dd=None: os.path.join(data_dir, "update_backups"))
    out = app_update.recover_interrupted_update(data_dir)
    assert out and out["recovered"] is True
    assert open(os.path.join(str(root), "app.py")).read() == "# old"
    manifest = json.load(open(os.path.join(backups, "manifest.json")))
    assert manifest["status"] == "recovered"


def test_boot_recovery_noop_without_in_progress(tmp_path, monkeypatch):
    data_dir = str(tmp_path / "data")
    os.makedirs(os.path.join(data_dir, "update_backups"))
    monkeypatch.setattr(app_update, "_backups_dir", lambda dd=None: os.path.join(data_dir, "update_backups"))
    assert app_update.recover_interrupted_update(data_dir) is None


def test_prune_keeps_newest_two(tmp_path):
    import time as _time
    data_dir = str(tmp_path / "data")
    backups = os.path.join(data_dir, "update_backups")
    os.makedirs(backups)
    for i in range(4):
        d = os.path.join(backups, f"pre-update-{i}")
        os.makedirs(d)
        old = _time.time() - (10 - i)
        os.utime(d, (old, old))
    os.makedirs(os.path.join(data_dir, "update_staging"))
    open(os.path.join(data_dir, "update_staging", "stale.zip"), "w").write("x")
    out = app_update._prune_old(data_dir)
    assert sorted(out["pruned_backups"]) == ["pre-update-0", "pre-update-1"]
    assert sorted(os.listdir(backups)) == ["pre-update-2", "pre-update-3"]
    assert out["pruned_staged_zips"] == 1


def test_partial_backup_never_restores(tmp_path, monkeypatch):
    # If the backup copies themselves fail, the live tree is untouched and
    # must NOT be "restored" from the partial copy (that would destroy it).
    root, data_dir, sha = _seed_apply(tmp_path, monkeypatch)
    monkeypatch.setattr(app_update, "_backup_replaced_tree",
                        lambda r, d, rep: (_ for _ in ()).throw(OSError("disk on fire")))
    with pytest.raises(app_update.UpdateError, match="live tree and all data are untouched"):
        app_update.apply_staged(sha, data_dir)
    assert open(os.path.join(root, "app.py")).read() == "# old"
    assert not os.path.exists(os.path.join(root, "newfile.txt"))


def test_sqlite_backup_is_consistent(tmp_path, monkeypatch):
    import sqlite3
    root, data_dir, sha = _seed_apply(tmp_path, monkeypatch)
    db = os.path.join(data_dir, "live.db")
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    con.executemany("INSERT INTO t (v) VALUES (?)", [(f"row{i}",) for i in range(50)])
    con.commit()
    dest = os.path.join(str(tmp_path), "copy.db")
    app_update._copy_file_for_backup(db, dest)
    con2 = sqlite3.connect(dest)
    assert con2.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 50
    con2.execute("PRAGMA integrity_check").fetchone()
    con.close()
    con2.close()


def test_offline_rollback_uses_local_backup(tmp_path, monkeypatch):
    root, data_dir, sha = _seed_apply(tmp_path, monkeypatch)
    result = app_update.apply_staged(sha, data_dir)
    assert result["applied"] is True
    # Network is dead: rollback must not need it.
    monkeypatch.setattr(app_update, "download_update",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("network used!")))
    monkeypatch.setattr(app_update, "_schedule_restart", lambda delay_s=3: None)
    out = app_update.rollback(data_dir)
    assert out.get("rolled_back") is True and out.get("offline") is True
    assert out["commit"] == "b" * 40
    assert open(os.path.join(root, "app.py")).read() == "# old"
    assert not os.path.exists(os.path.join(root, "newfile.txt"))
    state = app_update.load_state(data_dir)
    assert state["installed_commit"] == "b" * 40


def test_recovery_only_newest_backup(tmp_path, monkeypatch):
    import time as _time
    data_dir = str(tmp_path / "data")
    backups = os.path.join(data_dir, "update_backups")
    older = os.path.join(backups, "pre-update-old")
    newer = os.path.join(backups, "pre-update-new")
    os.makedirs(os.path.join(older, "replaced"))
    open(os.path.join(older, "replaced", "app.py"), "w").write("# stale")
    with open(os.path.join(older, "manifest.json"), "w") as f:
        json.dump({"status": "in-progress", "to_commit": "a" * 40}, f)
    os.makedirs(newer)
    with open(os.path.join(newer, "manifest.json"), "w") as f:
        json.dump({"status": "complete", "to_commit": "b" * 40}, f)
    now = _time.time()
    os.utime(older, (now - 100, now - 100))
    os.utime(newer, (now, now))
    root = tmp_path / "approot"
    root.mkdir()
    (root / "app.py").write_text("# current-good")
    monkeypatch.setattr(app_update, "_app_root", lambda: str(root))
    monkeypatch.setattr(app_update, "_backups_dir", lambda dd=None: backups)
    assert app_update.recover_interrupted_update(data_dir) is None
    assert open(os.path.join(str(root), "app.py")).read() == "# current-good"


def test_redirect_walk_never_reads_body(monkeypatch):
    import httpx as _httpx
    seen = {}

    class _FakeStream:
        def __init__(self, status, headers):
            self.status_code = status
            self.headers = headers

        def __enter__(self):
            seen["entered"] = True
            return self

        def __exit__(self, *a):
            return False

        def iter_bytes(self, chunk_size=None):
            raise AssertionError("body must never be read during redirect walk")

    def _fake_stream(method, url, **kwargs):
        assert kwargs.get("follow_redirects") is False
        if url == "https://codeload.github.com/x":
            return _FakeStream(302, {"location": "https://objects.githubusercontent.com/y"})
        return _FakeStream(200, {})

    monkeypatch.setattr(_httpx, "stream", _fake_stream)
    assert app_update._follow_redirects("https://codeload.github.com/x") == "https://objects.githubusercontent.com/y"
    assert seen.get("entered") is True
    with pytest.raises(app_update.UpdateError):
        app_update._follow_redirects("https://evil.example.com/x")


def _auto_settings(monkeypatch, enabled=True, start=2, end=6):
    vals = {"auto_update_enabled": enabled,
            "auto_update_start_hour": start,
            "auto_update_end_hour": end}
    monkeypatch.setattr("src.settings.get_setting",
                        lambda key, default=None: vals.get(key, default))


def _never_called(name):
    def _boom(**kwargs):
        raise AssertionError(f"{name} must not run here")
    return _boom


def _auto_nets(monkeypatch, *, streams=0, latest="a" * 40, available=True,
               downloaded="a" * 40, fail_download=None,
               apply_result=None):
    monkeypatch.setattr(app_update, "active_stream_count", lambda: streams)
    monkeypatch.setattr(app_update, "check_for_updates",
                        lambda force=False, data_dir=None: {
                            "latest": {"sha": latest}, "update_available": available})
    downloads, applies = [], []
    if fail_download:
        def _dl(ref=None, data_dir=None):
            raise fail_download
    else:
        def _dl(ref=None, data_dir=None):
            downloads.append(ref)
            return {"commit": downloaded}
    monkeypatch.setattr(app_update, "download_update", _dl)

    def _ap(commit=None, data_dir=None, force=False):
        applies.append((commit, force))
        return dict(apply_result or {"applied": True, "mode": "source"})
    monkeypatch.setattr(app_update, "apply_staged", _ap)
    return downloads, applies


def test_auto_update_disabled_does_nothing(tmp_path, monkeypatch):
    _auto_settings(monkeypatch, enabled=False)
    monkeypatch.setattr(app_update, "check_for_updates", _never_called("check"))
    out = app_update.maybe_auto_update(str(tmp_path), now_hour=3)
    assert out == {"acted": False, "reason": "disabled"}


def test_auto_update_outside_window(tmp_path, monkeypatch):
    _auto_settings(monkeypatch)
    monkeypatch.setattr(app_update, "check_for_updates", _never_called("check"))
    out = app_update.maybe_auto_update(str(tmp_path), now_hour=12)
    assert out == {"acted": False, "reason": "outside-window"}


def test_auto_update_window_wraps_midnight():
    assert app_update._in_maintenance_window(23, 22, 6) is True
    assert app_update._in_maintenance_window(5, 22, 6) is True
    assert app_update._in_maintenance_window(12, 22, 6) is False
    assert app_update._in_maintenance_window(3, 2, 6) is True
    assert app_update._in_maintenance_window(3, 3, 3) is False


def test_auto_update_skips_busy_streams(tmp_path, monkeypatch):
    _auto_settings(monkeypatch)
    _auto_nets(monkeypatch, streams=2)
    out = app_update.maybe_auto_update(str(tmp_path), now_hour=3)
    assert out == {"acted": False, "reason": "busy-streams"}
    assert app_update.load_state(str(tmp_path))["last_auto"]["result"] == "busy-streams"


def test_auto_update_up_to_date(tmp_path, monkeypatch):
    _auto_settings(monkeypatch)
    downloads, applies = _auto_nets(monkeypatch, available=False)
    out = app_update.maybe_auto_update(str(tmp_path), now_hour=3)
    assert out == {"acted": False, "reason": "up-to-date"}
    assert downloads == [] and applies == []


def test_auto_update_applies_like_button(tmp_path, monkeypatch):
    _auto_settings(monkeypatch)
    downloads, applies = _auto_nets(monkeypatch)
    out = app_update.maybe_auto_update(str(tmp_path), now_hour=3)
    assert out == {"acted": True, "reason": "applied", "commit": "a" * 40,
                   "mode": "source", "applied": True}
    assert downloads == ["a" * 40]
    assert applies == [("a" * 40, False)]
    assert app_update.load_state(str(tmp_path))["last_auto"]["result"] == "applied-source-True"


def test_auto_update_reuses_current_stage(tmp_path, monkeypatch):
    _auto_settings(monkeypatch)
    downloads, applies = _auto_nets(monkeypatch)
    staging = os.path.join(str(tmp_path), "update_staging")
    os.makedirs(staging)
    staged_path = os.path.join(staging, "pkg.zip")
    open(staged_path, "w").write("x")
    app_update._save_state({"staged": {"commit": "a" * 40, "path": staged_path}}, str(tmp_path))
    out = app_update.maybe_auto_update(str(tmp_path), now_hour=3)
    assert out["acted"] is True and out["commit"] == "a" * 40
    assert downloads == []
    assert applies == [("a" * 40, False)]


def test_auto_update_backs_off_after_failure(tmp_path, monkeypatch):
    _auto_settings(monkeypatch)
    _auto_nets(monkeypatch, fail_download=app_update.UpdateError("boom"))
    first = app_update.maybe_auto_update(str(tmp_path), now_hour=3)
    assert first == {"acted": False, "reason": "download-failed"}
    monkeypatch.setattr(app_update, "check_for_updates", _never_called("check"))
    second = app_update.maybe_auto_update(str(tmp_path), now_hour=3)
    assert second == {"acted": False, "reason": "backoff"}


def test_auto_update_legacy_docker_stays_staged(tmp_path, monkeypatch):
    _auto_settings(monkeypatch)
    downloads, applies = _auto_nets(monkeypatch)
    staging = os.path.join(str(tmp_path), "update_staging")
    os.makedirs(staging)
    staged_path = os.path.join(staging, "pkg.zip")
    open(staged_path, "w").write("x")
    app_update._save_state({"staged": {"commit": "a" * 40, "path": staged_path}}, str(tmp_path))
    monkeypatch.setattr(app_update, "detect_environment",
                        lambda: {"docker": True, "docker_socket": False})
    out = app_update.maybe_auto_update(str(tmp_path), now_hour=3)
    assert out == {"acted": False, "reason": "staged-needs-host"}
    assert downloads == [] and applies == []


def test_docker_managed_context_needs_all_parts(monkeypatch):
    assert app_update._docker_managed_context({}) is None
    assert app_update._docker_managed_context({"docker": True}) is None
    env = {"docker": True, "docker_socket": True}
    assert app_update._docker_managed_context(env) is None  # no /host-project here


def test_managed_context_resolves_via_sole_project(monkeypatch, tmp_path):
    # Mirrors a real host: overlay on, no COMPOSE_PROJECT_NAME anywhere,
    # labels unavailable, single stack on the daemon.
    monkeypatch.setenv("ODYSSEUS_ENABLE_HOST_DOCKER", "true")
    monkeypatch.delenv("COMPOSE_PROJECT_NAME", raising=False)
    monkeypatch.setattr(app_update, "HOST_PROJECT_MOUNT", str(tmp_path))
    (tmp_path / "docker-compose.yml").write_text("services: {}")
    monkeypatch.setattr(app_update, "_compose_context", lambda: None)

    def _run(cmd, **kwargs):
        from types import SimpleNamespace
        if cmd[:3] == ["docker", "compose", "ls"]:
            return SimpleNamespace(returncode=0, stdout='{"Name": "odysseus"}\n')
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(app_update.subprocess, "run", _run)
    assert app_update._docker_managed_context({"docker": True, "docker_socket": True}) == {
        "root": str(tmp_path), "project": "odysseus"}


def test_docker_managed_skip_reasons(monkeypatch, tmp_path):
    monkeypatch.delenv("ODYSSEUS_ENABLE_HOST_DOCKER", raising=False)
    assert app_update._docker_managed_skip_reason({}) == "host-docker overlay not enabled"
    monkeypatch.setenv("ODYSSEUS_ENABLE_HOST_DOCKER", "true")
    assert app_update._docker_managed_skip_reason({}) == "not running in docker"
    assert app_update._docker_managed_skip_reason({"docker": True}) == "no docker socket mount"
    assert app_update._docker_managed_skip_reason(
        {"docker": True, "docker_socket": True}) == "no project mount"


def test_resolve_compose_project_prefers_env_file(monkeypatch, tmp_path):
    monkeypatch.delenv("COMPOSE_PROJECT_NAME", raising=False)
    (tmp_path / ".env").write_text("COMPOSE_PROJECT_NAME=myproj\n")
    assert app_update._resolve_compose_project({}, str(tmp_path)) == "myproj"


def test_sole_compose_project(tmp_path, monkeypatch):
    from types import SimpleNamespace

    def _ls(out, code=0):
        monkeypatch.setattr(app_update.subprocess, "run",
                            lambda *a, **k: SimpleNamespace(returncode=code, stdout=out))

    _ls('{"Name": "odysseus", "Status": "running"}\n')
    assert app_update._sole_compose_project() == "odysseus"
    _ls('{"Name": "a"}\n{"Name": "b"}\n')
    assert app_update._sole_compose_project() is None
    _ls('', code=1)
    assert app_update._sole_compose_project() is None


def test_apply_docker_legacy_reports_skip_reason(tmp_path, monkeypatch):
    monkeypatch.delenv("ODYSSEUS_ENABLE_HOST_DOCKER", raising=False)
    zp = _make_zip(str(tmp_path / "up.zip"))
    staged = {"commit": "b" * 40, "path": zp}
    out = app_update._apply_docker(staged, {}, {"docker": True}, str(tmp_path))
    assert out["applied"] == "staged" and out["mode"] == "docker"
    assert out["managed_skip"] == "host-docker overlay not enabled"


def test_docker_managed_context_opt_in(monkeypatch, tmp_path):
    from types import SimpleNamespace
    env = {"docker": True, "docker_socket": True}
    monkeypatch.setenv("ODYSSEUS_ENABLE_HOST_DOCKER", "true")
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "odysseus")
    monkeypatch.setattr(app_update, "HOST_PROJECT_MOUNT", str(tmp_path))
    (tmp_path / "docker-compose.yml").write_text("services: {}")
    monkeypatch.setattr(app_update.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(returncode=0))
    assert app_update._docker_managed_context(env) == {
        "root": str(tmp_path), "project": "odysseus"}
    # Socket present but opt-in flag missing: never managed.
    monkeypatch.delenv("ODYSSEUS_ENABLE_HOST_DOCKER")
    assert app_update._docker_managed_context(env) is None


def test_docker_managed_context_needs_daemon(monkeypatch, tmp_path):
    from types import SimpleNamespace
    env = {"docker": True, "docker_socket": True}
    monkeypatch.setenv("ODYSSEUS_ENABLE_HOST_DOCKER", "true")
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "odysseus")
    monkeypatch.setattr(app_update, "HOST_PROJECT_MOUNT", str(tmp_path))
    (tmp_path / "docker-compose.yml").write_text("services: {}")

    def _run(cmd, **kwargs):
        if cmd[:2] == ["docker", "info"]:
            return SimpleNamespace(returncode=1)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(app_update.subprocess, "run", _run)
    assert app_update._docker_managed_context(env) is None


def test_auto_update_download_failure_recorded(tmp_path, monkeypatch):
    _auto_settings(monkeypatch)
    _auto_nets(monkeypatch, fail_download=app_update.UpdateError("boom"))
    out = app_update.maybe_auto_update(str(tmp_path), now_hour=3)
    assert out == {"acted": False, "reason": "download-failed"}
    assert "boom" in app_update.load_state(str(tmp_path))["last_auto"]["result"]


def test_auto_update_check_failure_never_raises(tmp_path, monkeypatch):
    _auto_settings(monkeypatch)
    monkeypatch.setattr(app_update, "active_stream_count", lambda: 0)

    def _offline(**kwargs):
        raise app_update.UpdateError("offline")

    monkeypatch.setattr(app_update, "check_for_updates", _offline)
    out = app_update.maybe_auto_update(str(tmp_path), now_hour=3)
    assert out == {"acted": False, "reason": "check-failed"}


def test_find_container_id_cgroup_variants():
    full = "a" * 64
    assert app_update._find_container_id(f"12:devices:/docker/{full}\n") == full
    assert app_update._find_container_id(
        f"0::/system.slice/docker-{full}.scope\n") == full
    assert app_update._find_container_id("0::/\n") is None
    assert app_update._find_container_id("") is None


def test_auto_endpoints_roundtrip(monkeypatch, tmp_path):
    import src.settings as settings_mod
    import routes.admin_update_routes as aur
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", str(tmp_path / "settings.json"))
    settings_mod._invalidate_caches()
    monkeypatch.setattr(aur, "require_admin", lambda request: None)
    app = FastAPI()
    app.include_router(aur.setup_admin_update_routes())
    client = TestClient(app, raise_server_exceptions=False)

    got = client.get("/api/admin/updates/auto")
    assert got.status_code == 200
    assert got.json()["enabled"] is False
    assert (got.json()["start_hour"], got.json()["end_hour"]) == (2, 6)

    saved = client.post("/api/admin/updates/auto",
                        json={"enabled": True, "start_hour": 1, "end_hour": 5})
    assert saved.status_code == 200
    assert saved.json() == {"enabled": True, "start_hour": 1, "end_hour": 5}
    assert client.get("/api/admin/updates/auto").json()["enabled"] is True

    for bad in ({"enabled": True, "start_hour": 9, "end_hour": 9},
                {"enabled": True, "start_hour": 24, "end_hour": 6}):
        assert client.post("/api/admin/updates/auto", json=bad).status_code == 400


def test_apply_docker_managed_rebuilds_detached(tmp_path, monkeypatch):
    host, spawned = _managed_env(monkeypatch, tmp_path)
    applied = {}
    monkeypatch.setattr(
        app_update, "_apply_files",
        lambda *a, **k: applied.update(root=k.get("root")) or {"applied": True, "mode": "source"})
    zp = _make_zip(str(tmp_path / "up.zip"))
    staged = {"commit": "a" * 40, "path": zp}
    out = app_update._apply_docker(staged, {}, {"docker": True}, str(tmp_path))
    assert out["applied"] == "rebuilding" and out["mode"] == "docker"
    assert applied["root"] == str(host)
    _assert_helper_shape(spawned, host)
    assert open(host / ".deploy-commit").read() == "a" * 40
    assert (host / "logs" / "rebuild.log").exists()
    import json as _json
    claim = _json.loads(open(os.path.join(
        str(tmp_path), "update_staging", "rebuild.inflight")).read())
    assert claim["commit"] == "a" * 40


def _managed_env(monkeypatch, tmp_path):
    host = tmp_path / "host"
    (host / "logs").mkdir(parents=True)
    monkeypatch.setattr(app_update, "_docker_managed_context",
                        lambda env: {"root": str(host), "project": "odysseus"})
    monkeypatch.setattr(app_update, "_compose_context",
                        lambda: {"working_dir": "/h", "project": "odysseus",
                                 "image": "odysseus-odysseus:latest"})
    monkeypatch.setattr(
        app_update, "_apply_files",
        lambda *a, **k: {"applied": True, "mode": "source"})
    monkeypatch.setattr(app_update, "_compose_context",
                        lambda: {"working_dir": "/h", "project": "odysseus",
                                 "image": "odysseus-odysseus:latest"})
    spawned = {}

    class _P:
        def __init__(self, *a, **k):
            spawned.update(args=a, kwargs=k)

    monkeypatch.setattr(app_update.subprocess, "Popen", _P)
    return host, spawned


def _assert_helper_shape(spawned, host):
    args = spawned["args"][0]
    assert args[:6] == ["docker", "run", "-d", "--rm", "--name",
                        "aegis-updater-aaaaaaaaaaaa"]
    assert "--entrypoint" in args and "sh" in args
    assert args[-2:] == ["-c", args[-1]]
    assert "up -d --build" in args[-1] and "rm -f" in args[-1]
    assert "-v" in args and "/var/run/docker.sock:/var/run/docker.sock" in args
    assert "/h:/host-project" in args
    assert spawned["kwargs"]["cwd"] == str(host)
    assert "start_new_session" in spawned["kwargs"] or "creationflags" in spawned["kwargs"]


def test_apply_docker_refuses_busy_streams_without_force(tmp_path, monkeypatch):
    host, spawned = _managed_env(monkeypatch, tmp_path)
    monkeypatch.setattr(app_update, "active_stream_count", lambda: 2)
    zp = _make_zip(str(tmp_path / "up.zip"))
    with pytest.raises(app_update.UpdateError, match=r"stream\(s\) active"):
        app_update._apply_docker({"commit": "a" * 40, "path": zp},
                                 {}, {"docker": True}, str(tmp_path))
    assert spawned == {}


def test_apply_docker_force_proceeds_despite_streams(tmp_path, monkeypatch):
    host, spawned = _managed_env(monkeypatch, tmp_path)
    monkeypatch.setattr(app_update, "active_stream_count", lambda: 3)
    zp = _make_zip(str(tmp_path / "up.zip"))
    out = app_update._apply_docker({"commit": "a" * 40, "path": zp},
                                   {}, {"docker": True}, str(tmp_path), force=True)
    assert out["applied"] == "rebuilding"
    _assert_helper_shape(spawned, host)


def _seed_claim(data_dir, commit="a" * 40, age_s=0):
    import json as _json
    import time as _time
    staging = os.path.join(data_dir, "update_staging")
    os.makedirs(staging, exist_ok=True)
    with open(os.path.join(staging, "rebuild.inflight"), "w") as f:
        _json.dump({"commit": commit, "t": _time.time() - age_s}, f)


def test_apply_docker_refuses_second_rebuild_while_in_flight(tmp_path, monkeypatch):
    host, spawned = _managed_env(monkeypatch, tmp_path)
    monkeypatch.setattr(app_update, "active_stream_count", lambda: 0)
    _seed_claim(str(tmp_path))
    zp = _make_zip(str(tmp_path / "up.zip"))
    with pytest.raises(app_update.UpdateError, match="already in progress"):
        app_update._apply_docker({"commit": "a" * 40, "path": zp},
                                 {}, {"docker": True}, str(tmp_path))
    assert spawned == {}


def test_spawn_rejects_bad_project_name(tmp_path):
    with pytest.raises(app_update.UpdateError, match="project name"):
        app_update._spawn_host_rebuild(str(tmp_path), "a;b", "a" * 40, str(tmp_path))


def test_spawn_failure_clears_claim(tmp_path, monkeypatch):
    # A launch failure must not leave a claim blocking retries; the apply
    # layer owns that cleanup (spawn itself only guarantees the wrapper
    # removes the claim once the compose run exits).
    host = tmp_path / "host"
    (host / "logs").mkdir(parents=True)
    monkeypatch.setattr(app_update, "_docker_managed_context",
                        lambda env: {"root": str(host), "project": "odysseus"})
    monkeypatch.setattr(app_update, "_compose_context",
                        lambda: {"working_dir": "/h", "project": "odysseus",
                                 "image": "odysseus-odysseus:latest"})
    monkeypatch.setattr(app_update, "active_stream_count", lambda: 0)
    monkeypatch.setattr(
        app_update, "_apply_files",
        lambda *a, **k: {"applied": True, "mode": "source"})

    def _boom(*a, **k):
        raise OSError("no exec")

    monkeypatch.setattr(app_update.subprocess, "Popen", _boom)
    zp = _make_zip(str(tmp_path / "up.zip"))
    with pytest.raises(OSError):
        app_update._apply_docker({"commit": "a" * 40, "path": zp},
                                 {}, {"docker": True}, str(tmp_path))
    assert not os.path.exists(os.path.join(
        str(tmp_path), "update_staging", "rebuild.inflight"))


def test_auto_update_skips_rebuild_in_flight(tmp_path, monkeypatch):
    _auto_settings(monkeypatch)
    downloads, applies = _auto_nets(monkeypatch)
    _seed_claim(str(tmp_path))
    out = app_update.maybe_auto_update(str(tmp_path), now_hour=3)
    assert out == {"acted": False, "reason": "rebuild-in-flight"}
    assert downloads == [] and applies == []


def test_auto_update_up_to_date_ignores_leftover_claim(tmp_path, monkeypatch):
    _auto_settings(monkeypatch)
    _auto_nets(monkeypatch, available=False)
    _seed_claim(str(tmp_path))
    out = app_update.maybe_auto_update(str(tmp_path), now_hour=3)
    assert out == {"acted": False, "reason": "up-to-date"}


def test_auto_update_retries_stuck_rebuild(tmp_path, monkeypatch):
    _auto_settings(monkeypatch)
    downloads, applies = _auto_nets(monkeypatch)
    _seed_claim(str(tmp_path), age_s=100 * 3600)
    out = app_update.maybe_auto_update(str(tmp_path), now_hour=3)
    assert out["acted"] is True and out["reason"] == "applied"
    assert downloads == ["a" * 40]
    assert applies == [("a" * 40, False)]


def _update_client(monkeypatch):
    import routes.admin_update_routes as aur
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    monkeypatch.setattr(aur, "require_admin", lambda request: None)
    app = FastAPI()
    app.include_router(aur.setup_admin_update_routes())
    return TestClient(app, raise_server_exceptions=False)


def _seed_staged(tmp_path, commit="a" * 40):
    data_dir = str(tmp_path / "data")
    os.makedirs(data_dir, exist_ok=True)
    zp = _make_zip(str(tmp_path / "up.zip"))
    app_update._save_state({"staged": {"commit": commit, "path": zp}}, data_dir)
    return data_dir


def test_background_worker_records_success_and_releases(tmp_path, monkeypatch):
    data_dir = str(tmp_path / "data")
    os.makedirs(data_dir, exist_ok=True)
    monkeypatch.setattr(app_update, "apply_staged",
                        lambda commit=None, data_dir=None, force=False: {"applied": True})
    assert app_update.try_claim_apply_slot() is True
    app_update._apply_in_background("a" * 40, data_dir, False)
    last = app_update.load_state(data_dir)["last_apply"]
    assert last["ok"] is True and last["commit"] == "a" * 40
    assert app_update.try_claim_apply_slot() is True
    app_update._release_apply_slot()


def test_background_worker_records_failure_and_releases(tmp_path, monkeypatch):
    data_dir = str(tmp_path / "data")
    os.makedirs(data_dir, exist_ok=True)

    def _boom(commit=None, data_dir=None, force=False):
        raise app_update.UpdateError("disk gone")

    monkeypatch.setattr(app_update, "apply_staged", _boom)
    assert app_update.try_claim_apply_slot() is True
    app_update._apply_in_background("a" * 40, data_dir, False)
    last = app_update.load_state(data_dir)["last_apply"]
    assert last["ok"] is False and "disk gone" in last["error"]
    assert app_update.try_claim_apply_slot() is True
    app_update._release_apply_slot()


def test_apply_slot_blocks_second_accept(tmp_path):
    assert app_update.try_claim_apply_slot() is True
    try:
        assert app_update.try_claim_apply_slot() is False
    finally:
        app_update._release_apply_slot()
    assert app_update.try_claim_apply_slot() is True
    app_update._release_apply_slot()


def test_apply_route_accepts_and_validates(monkeypatch, tmp_path):
    client = _update_client(monkeypatch)
    data_dir = _seed_staged(tmp_path)
    state = app_update.load_state(data_dir)
    monkeypatch.setattr(app_update, "load_state", lambda dd=None: dict(state))
    monkeypatch.setattr(app_update, "_read_rebuild_claim", lambda dd=None: None)
    monkeypatch.setattr(app_update, "active_stream_count", lambda: 0)
    calls = []
    monkeypatch.setattr(app_update, "_apply_in_background",
                        lambda *a, **k: calls.append((a, k)))
    try:
        r = client.post("/api/admin/updates/apply", json={"commit": "a" * 40})
        assert r.status_code == 200, r.text
        assert r.json() == {"accepted": True, "commit": "a" * 40}
        assert calls and calls[0][0][:2] == ("a" * 40, None)
        # Second immediate accept is refused while the slot is held.
        r2 = client.post("/api/admin/updates/apply", json={"commit": "a" * 40})
        assert r2.status_code == 409
        # Mismatched or missing stage is rejected without touching the worker.
        calls.clear()
        assert client.post("/api/admin/updates/apply",
                           json={"commit": "b" * 40}).status_code == 502
        assert calls == []
    finally:
        app_update._release_apply_slot()


def test_apply_route_streams_check(monkeypatch, tmp_path):
    client = _update_client(monkeypatch)
    data_dir = _seed_staged(tmp_path)
    state = app_update.load_state(data_dir)
    monkeypatch.setattr(app_update, "load_state", lambda dd=None: dict(state))
    monkeypatch.setattr(app_update, "_read_rebuild_claim", lambda dd=None: None)
    monkeypatch.setattr(app_update, "active_stream_count", lambda: 2)
    ran = []
    monkeypatch.setattr(app_update, "_apply_in_background",
                        lambda *a, **k: ran.append(True))
    try:
        r = client.post("/api/admin/updates/apply", json={"commit": "a" * 40})
        assert r.status_code == 502 and "stream(s) active" in r.text
        assert ran == []
        r = client.post("/api/admin/updates/apply",
                        json={"commit": "a" * 40, "force": True})
        assert r.status_code == 200
        assert ran == [True]
    finally:
        app_update._release_apply_slot()


def test_status_includes_last_apply(tmp_path):
    data_dir = str(tmp_path / "data")
    os.makedirs(data_dir, exist_ok=True)
    app_update._save_state({"last_apply": {"commit": "a" * 40, "ok": True}}, data_dir)
    assert app_update.status(data_dir)["last_apply"]["commit"] == "a" * 40

