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
