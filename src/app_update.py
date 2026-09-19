"""Self-update engine: fetch, verify, back up, and install Aegis updates.

Source of truth is the ``main`` branch of the upstream GitHub repository
(tarball + commit metadata). No GitHub Releases are required.

Safety model (mirrors the documented manual deploy flow):
- ``data/``, ``logs/`` and the ``.env`` file are NEVER touched by an apply.
- Every apply takes a full backup first: the complete ``data/`` copy
  (live SQLite files via the online backup API), the full replaced source
  tree, ``.env``, custom orphan files, and a status manifest.
- Disk preflight refuses the apply unless backups + new tree fit twice over.
- Post-apply verification failures (and crashes: boot-time recovery) restore
  the pre-apply tree automatically from the backup — no re-download needed.
- Rollback prefers the local backup (offline-capable) and falls back to
  re-downloading the previous commit.
- Update payloads are verified (zip integrity + required tree markers,
  zip-slip neutralised) before anything is replaced, and only one update
  operation runs at a time. Applies refuse while chat streams are live
  unless forced.

Platform behaviour:
- Source installs (bare ``python app.py``, dev checkouts): full automatic
  apply + process restart.
- Frozen builds: the running executable can't be replaced while locked, so
  locked files are skipped, recorded, and reported as pending_restart.
- Docker (baked-image): replacing files inside the container would be lost
  on the next recreate, so apply STAGES the verified tree into
  ``data/update_staging/<sha>/`` (bind-mounted, host-visible) and returns
  the exact host command to finish (copy over source, rebuild).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

REPO = "Godl1nk/Aegis"
CHANNEL_BRANCH = "main"
API_COMMIT_URL = f"https://api.github.com/repos/{REPO}/commits/{CHANNEL_BRANCH}"
RAW_VERSION_URL = f"https://raw.githubusercontent.com/{REPO}/{CHANNEL_BRANCH}/src/constants.py"
ZIP_URL_TEMPLATE = "https://codeload.github.com/{repo}/zip/{ref}"

# Hosts an update download may legitimately traverse (API + tarball +
# codeload redirect targets). Anything else is refused.
ALLOWED_HOSTS = {
    "api.github.com",
    "github.com",
    "www.github.com",
    "codeload.github.com",
    "raw.githubusercontent.com",
    "objects.githubusercontent.com",
}

# A valid update tree must contain these paths at its top level.
REQUIRED_MARKERS = (
    "app.py",
    "src/constants.py",
    "docker-compose.yml",
    "static/index.html",
)

# Top-level entries an apply must never delete or overwrite.
PRESERVE_TOP = {"data", "logs", ".env", ".git"}

# data/ subtrees that are updater scratch space (never backed up: the
# backups dir itself, plus staged downloads which are re-fetchable).
DATA_BACKUP_SKIP = {"update_staging", "update_backups"}

# Old backups/staged zips pruned after each successful apply.
KEEP_BACKUPS = 2

MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024
HTTP_TIMEOUT = 30
CHECK_CACHE_SECONDS = 30 * 60

# Re-entrant: rollback() legitimately calls download_update()/apply_staged()
# while holding it; other threads still fail fast with UpdateBusy.
_op_lock = threading.RLock()
_check_cache: dict = {"at": 0.0, "result": None}


def _invalidate_check_cache() -> None:
    global _check_cache
    _check_cache = {"at": 0.0, "result": None}


class UpdateError(Exception):
    """User-facing update failure (message is safe to return via API)."""


class UpdateBusy(UpdateError):
    """Another update operation is already running."""


def _data_dir() -> str:
    from src.constants import DATA_DIR
    return DATA_DIR


def _app_root() -> str:
    from src.runtime_paths import get_app_root
    return get_app_root()


def _state_path(data_dir: str | None = None) -> str:
    return os.path.join(data_dir or _data_dir(), "update_state.json")


def _staging_dir(data_dir: str | None = None) -> str:
    path = os.path.join(data_dir or _data_dir(), "update_staging")
    os.makedirs(path, exist_ok=True)
    return path


def _backups_dir(data_dir: str | None = None) -> str:
    path = os.path.join(data_dir or _data_dir(), "update_backups")
    os.makedirs(path, exist_ok=True)
    return path


def load_state(data_dir: str | None = None) -> dict:
    try:
        with open(_state_path(data_dir), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_state(state: dict, data_dir: str | None = None) -> None:
    path = _state_path(data_dir)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, path)


def local_version() -> str:
    try:
        from src.constants import APP_VERSION
        return str(APP_VERSION)
    except Exception:
        return "unknown"


_DEPLOY_STAMP = ".deploy-commit"


def _read_deploy_stamp(root: str | None = None) -> str | None:
    """Commit stamped into the tree at pack time (see zip-deploy.bat)."""
    try:
        with open(os.path.join(root or _app_root(), _DEPLOY_STAMP), encoding="utf-8") as f:
            sha = (f.read() or "").strip()
        return sha if len(sha) == 40 and all(c in "0123456789abcdef" for c in sha) else None
    except Exception:
        return None


def installed_commit(data_dir: str | None = None) -> str | None:
    """Commit this tree was installed from: pack stamp, state file, git checkout.

    The stamp describes the files actually on disk (zip deploys carry no
    .git and never record a baseline), so it outranks the state baseline,
    which can predate a manual deploy. Updater packages never contain the
    stamp (gitignored) and apply moves custom files to backup, so it cannot
    shadow a later updater install.
    """
    stamped = _read_deploy_stamp()
    if stamped:
        return stamped
    state = load_state(data_dir)
    if state.get("installed_commit"):
        return state["installed_commit"]
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=_app_root(),
            capture_output=True, text=True, timeout=10,
        )
        sha = (out.stdout or "").strip()
        return sha if len(sha) == 40 and all(c in "0123456789abcdef" for c in sha) else None
    except Exception:
        return None


def _check_host_allowed(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return host in ALLOWED_HOSTS


def _http_get(url: str, *, accept: str = "application/json", timeout: int = HTTP_TIMEOUT) -> httpx.Response:
    if not _check_host_allowed(url):
        raise UpdateError(f"Refusing non-update host: {urlparse(url).hostname}")
    try:
        r = httpx.get(
            url,
            headers={"Accept": accept, "User-Agent": "Aegis-Updater"},
            timeout=timeout,
            follow_redirects=False,
        )
    except Exception as e:
        raise UpdateError(f"Update server unreachable: {e}")
    return r


def _follow_redirects(url: str, *, max_hops: int = 5) -> str:
    """Resolve redirect chains manually so every hop stays on the allowlist.

    Uses bodiless streaming reads: the response body is never consumed, so
    resolving the (large) tarball URL costs headers only — _http_get would
    buffer the entire body into RAM and download it twice.
    """
    from urllib.parse import urljoin
    current = url
    for _ in range(max_hops + 1):
        if not _check_host_allowed(current):
            raise UpdateError(f"Refusing non-update host: {urlparse(current).hostname}")
        try:
            with httpx.stream(
                "GET", current,
                headers={"Accept": "*/*", "User-Agent": "Aegis-Updater",
                         "Accept-Encoding": "identity"},
                timeout=HTTP_TIMEOUT, follow_redirects=False,
            ) as r:
                if r.status_code in (301, 302, 303, 307, 308):
                    location = r.headers.get("location")
                    if not location:
                        raise UpdateError("Update server returned a redirect without a location")
                    current = urljoin(current, location)
                    continue
                return current
        except UpdateError:
            raise
        except Exception as e:
            raise UpdateError(f"Update server unreachable: {e}")
    raise UpdateError("Too many redirects resolving the update")


def fetch_remote_head() -> dict:
    """Latest commit on the channel branch: {sha, message, date, url}."""
    r = _http_get(API_COMMIT_URL)
    if r.status_code == 403 and "rate limit" in r.text.lower():
        raise UpdateError("GitHub API rate limit reached — try again later")
    if r.status_code != 200:
        raise UpdateError(f"Could not read remote version (HTTP {r.status_code})")
    try:
        data = r.json()
        sha = data["sha"]
        commit = data.get("commit", {})
        return {
            "sha": sha,
            "message": str(commit.get("message", "")).splitlines()[0][:120],
            "date": ((commit.get("author") or {}).get("date") or ""),
            "url": f"https://github.com/{REPO}/commit/{sha}",
        }
    except Exception:
        raise UpdateError("Unexpected response from the update server")


_VERSION_RE = re.compile(r'APP_VERSION\s*=\s*["\']([^"\']+)["\']')


def parse_remote_version(text: str) -> str | None:
    m = _VERSION_RE.search(text or "")
    return m.group(1) if m else None


def fetch_remote_version() -> str | None:
    """APP_VERSION currently on the channel branch (best effort)."""
    try:
        r = _http_get(RAW_VERSION_URL, accept="text/plain")
        if r.status_code != 200:
            return None
        return parse_remote_version(r.text)
    except UpdateError:
        return None


def check_for_updates(force: bool = False, data_dir: str | None = None) -> dict:
    """Compare the installed tree against the channel HEAD (cached)."""
    global _check_cache
    now = time.time()
    if not force and _check_cache["result"] and now - _check_cache["at"] < CHECK_CACHE_SECONDS:
        return _check_cache["result"]
    head = fetch_remote_head()
    installed = installed_commit(data_dir)
    result = {
        "repo": REPO,
        "channel": CHANNEL_BRANCH,
        "current": {"version": local_version(), "commit": installed},
        "latest": {**head, "version": fetch_remote_version()},
        "update_available": installed != head["sha"],
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now)),
        "environment": detect_environment(),
    }
    state = load_state(data_dir)
    state["checked_at"] = result["checked_at"]
    state["latest_known"] = result["latest"]
    _save_state(state, data_dir)
    _check_cache = {"at": now, "result": result}
    return result


def detect_environment() -> dict:
    """Where the updater runs: docker / frozen / source, socket or not."""
    docker = os.path.exists("/.dockerenv")
    if not docker:
        try:
            with open("/proc/1/cgroup", encoding="utf-8", errors="replace") as f:
                docker = "docker" in f.read().lower()
        except Exception:
            pass
    frozen = bool(getattr(sys, "frozen", False))
    socket_path = os.environ.get("DOCKER_SOCK", "/var/run/docker.sock")
    try:
        has_socket = os.path.exists(socket_path)
    except Exception:
        has_socket = False
    return {
        "docker": docker,
        "frozen": frozen,
        "source": not docker and not frozen,
        "docker_socket": has_socket,
        "compose": _compose_context() if docker else None,
    }


_CID_RE = re.compile(r"[0-9a-f]{64}")


def _find_container_id(text: str) -> str | None:
    """First full container ID anywhere in cgroup content.

    cgroup v1 shows it as the trailing path segment, but cgroup v2 under
    systemd embeds it mid-segment (docker-<hex>.scope), which a
    last-segment check misses entirely. Nested containers list outer IDs
    first, so the innermost (last) match is the current container.
    """
    found = _CID_RE.findall(text or "")
    return found[-1] if found else None


def _own_container_id(cgroup_text=None, hostname=None) -> str | None:
    if cgroup_text is None:
        try:
            with open("/proc/self/cgroup", encoding="utf-8", errors="replace") as f:
                cgroup_text = f.read()
        except Exception:
            cgroup_text = ""
    found = _find_container_id(cgroup_text)
    if found:
        return found
    # NOTE: read the UTS namespace, not the HOSTNAME *variable* — Docker sets
    # the hostname but does not export it, so os.environ is empty here and an
    # env-only lookup silently disables self-inspection on every host.
    if hostname is None:
        hostname = (os.environ.get("HOSTNAME") or "").strip()
        if not hostname:
            try:
                hostname = socket.gethostname().strip()
            except Exception:
                hostname = ""
    hostname = (hostname or "").strip()
    if _CID_RE.fullmatch(hostname) or re.fullmatch(r"[0-9a-f]{12}", hostname):
        return hostname
    return None


def _compose_project_from_labels(labels: dict) -> str | None:
    """Project name from container labels. Compose uses the flat
    ``com.docker.compose.project`` key; the longer ``.project.name`` form
    is accepted as a fallback in case some version emits it."""
    labels = labels or {}
    return (labels.get("com.docker.compose.project")
            or labels.get("com.docker.compose.project.name"))


def _compose_context() -> dict | None:
    """Host compose coordinates for this container (needs the docker socket)."""
    cid = _own_container_id()
    socket_path = os.environ.get("DOCKER_SOCK", "/var/run/docker.sock")
    if not cid or not os.path.exists(socket_path):
        return None
    try:
        import json as _json
        out = subprocess.run(
            ["docker", "--host", f"unix://{socket_path}", "inspect", cid],
            capture_output=True, text=True, timeout=15,
        )
        info = _json.loads(out.stdout or "[]")
        labels = ((info[0] or {}).get("Config") or {}).get("Labels") or {}
        working_dir = labels.get("com.docker.compose.project.working_dir")
        project = _compose_project_from_labels(labels)
        image = ((info[0] or {}).get("Config") or {}).get("Image")
        if not working_dir or not project or not image:
            return None
        return {"working_dir": working_dir, "project": project, "image": image}
    except Exception as e:
        logger.debug("Compose context lookup failed: %s", e)
        return None


def _zip_ref_url(ref: str) -> str:
    ref = (ref or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.\-/]{1,128}", ref):
        raise UpdateError("Invalid update ref")
    # Dots/slashes are legal in tags, but never as path segments — a ref
    # like "../x" would escape the archive path when joined into the URL.
    segments = ref.split("/")
    if any(seg in ("", ".", "..") for seg in segments):
        raise UpdateError("Invalid update ref")
    return ZIP_URL_TEMPLATE.format(repo=REPO, ref=ref)


def download_update(ref: str | None = None, data_dir: str | None = None) -> dict:
    """Download + verify an update tarball into staging. Returns staged info."""
    if not _op_lock.acquire(blocking=False):
        raise UpdateBusy("Another update operation is already running")
    try:
        head = fetch_remote_head()
        commit = head["sha"] if not ref or ref in ("main", CHANNEL_BRANCH, "latest") else ref
        if not re.fullmatch(r"[0-9a-f]{40}", commit):
            # Resolve short SHAs / tags through the same metadata path is out
            # of scope; only full SHAs or the channel head are accepted.
            raise UpdateError("Update ref must be a full commit SHA or the channel head")
        url = _follow_redirects(_zip_ref_url(commit))
        staging = _staging_dir(data_dir)
        dest = os.path.join(staging, f"Aegis-{commit[:12]}.zip")
        _stream_download(url, dest)
        root = verify_staging(dest)
        state = load_state(data_dir)
        state["staged"] = {"commit": commit, "path": dest, "root": root, "at": time.time()}
        _save_state(state, data_dir)
        return {"staged": True, "commit": commit, "path": dest,
                "message": head["message"] if commit == head["sha"] else "",
                "url": f"https://github.com/{REPO}/commit/{commit}"}
    finally:
        _op_lock.release()


def _stream_download(url: str, dest: str) -> None:
    tmp = dest + ".part"
    read = 0
    try:
        with httpx.stream(
            "GET", url,
            headers={"User-Agent": "Aegis-Updater", "Accept-Encoding": "identity"},
            timeout=HTTP_TIMEOUT, follow_redirects=False,
        ) as r:
            if r.status_code != 200:
                raise UpdateError(f"Download failed (HTTP {r.status_code})")
            final_host = (urlparse(str(r.url)).hostname or "").lower()
            if final_host not in ALLOWED_HOSTS:
                raise UpdateError("Download left the trusted hosts")
            with open(tmp, "wb") as f:
                for chunk in r.iter_bytes(chunk_size=1024 * 256):
                    read += len(chunk)
                    if read > MAX_DOWNLOAD_BYTES:
                        raise UpdateError("Update package exceeds the 512MB limit")
                    f.write(chunk)
        os.replace(tmp, dest)
    except UpdateError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    except Exception as e:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise UpdateError(f"Download failed: {e}")


def _zip_top_root(path: str) -> str:
    try:
        with zipfile.ZipFile(path) as zf:
            bad = zf.testzip()
            if bad is not None:
                raise UpdateError(f"Update package is corrupt (bad entry: {bad})")
            names = zf.namelist()
    except zipfile.BadZipFile:
        raise UpdateError("Downloaded file is not a valid update package")
    if not names:
        raise UpdateError("Update package is empty")
    top = names[0].split("/")[0]
    if not top or ".." in names[0].split("/"):
        raise UpdateError("Update package has an unexpected layout")
    return top


def verify_staging(path: str) -> str:
    """Validate a staged zip; returns its top-level directory name."""
    if not os.path.exists(path):
        raise UpdateError("No staged update found — download first")
    top = _zip_top_root(path)
    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
    except zipfile.BadZipFile:
        raise UpdateError("Staged update package is corrupt")
    missing = [m for m in REQUIRED_MARKERS if f"{top}/{m}" not in names]
    if missing:
        raise UpdateError(f"Update package failed verification (missing: {', '.join(missing)})")
    return top


def _safe_extract(zip_path: str, dest_dir: str) -> str:
    """Extract a verified zip, neutralising zip-slip paths. Returns tree root."""
    top = _zip_top_root(zip_path)
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            target = os.path.realpath(os.path.join(dest_dir, info.filename))
            if target != os.path.realpath(dest_dir) and not target.startswith(os.path.realpath(dest_dir) + os.sep):
                raise UpdateError(f"Unsafe path in update package: {info.filename}")
        zf.extractall(dest_dir)
    return os.path.join(dest_dir, top)


def _clear_pycache(root: str) -> int:
    removed = 0
    for dirpath, dirnames, _filenames in os.walk(root):
        # Never touch embedded virtualenvs.
        if os.path.basename(dirpath) in (".venv", "venv", "node_modules", ".git"):
            dirnames[:] = []
            continue
        if os.path.basename(dirpath) == "__pycache__":
            shutil.rmtree(dirpath, ignore_errors=True)
            removed += 1
            dirnames[:] = []
    return removed


def _copy_tree_over(src: str, dest: str) -> tuple[int, list[str]]:
    """Copy src over dest (merge). Returns (files_written, skipped_locked)."""
    written = 0
    skipped: list[str] = []
    for dirpath, _dirnames, filenames in os.walk(src):
        rel = os.path.relpath(dirpath, src)
        target_dir = dest if rel == "." else os.path.join(dest, rel)
        os.makedirs(target_dir, exist_ok=True)
        for name in filenames:
            s = os.path.join(dirpath, name)
            d = os.path.join(target_dir, name)
            try:
                if os.path.exists(d) and not os.path.isfile(d):
                    shutil.rmtree(d, ignore_errors=True)
                shutil.copy2(s, d)
                written += 1
            except OSError as e:
                logger.warning("Update skipped locked file %s: %s", d, e)
                skipped.append(os.path.join(rel, name) if rel != "." else name)
    return written, skipped


def apply_staged(commit: str | None = None, data_dir: str | None = None,
                 force: bool = False) -> dict:
    """Back up, replace the source tree with the staged update, record state.

    Refuses while chat streams are live (a mid-replace import would 500
    them) unless force=True — in-flight turns belong to the user.
    """
    if not _op_lock.acquire(blocking=False):
        raise UpdateBusy("Another update operation is already running")
    try:
        state = load_state(data_dir)
        staged = state.get("staged") or {}
        if commit and commit != staged.get("commit"):
            raise UpdateError("Staged update does not match — download it first")
        if not staged.get("path") or not os.path.exists(staged["path"]):
            raise UpdateError("No staged update found — download first")
        commit = staged["commit"]
        env = detect_environment()
        if env["docker"]:
            return _apply_docker(staged, state, env, data_dir, force=force)
        if not force:
            live = active_stream_count()
            if live > 0:
                raise UpdateError(
                    f"{live} chat stream(s) active — retry when idle or force the "
                    f"apply (in-flight turns may error)")
        return _apply_files(staged, state, env, data_dir)
    finally:
        _op_lock.release()


def _dir_size(path: str, skip_top: set | None = None) -> int:
    """Best-effort recursive size in bytes (missing dirs read as 0)."""
    total = 0
    try:
        for dirpath, dirnames, filenames in os.walk(path):
            if os.path.abspath(dirpath) == os.path.abspath(path) and skip_top:
                dirnames[:] = [d for d in dirnames if d not in skip_top]
            for name in filenames:
                try:
                    total += os.path.getsize(os.path.join(dirpath, name))
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _disk_preflight(app_bytes: int, data_bytes: int, data_dir: str | None) -> None:
    """Refuse the apply when either disk can't hold its share with headroom.

    The app disk takes the replaced-tree backup + new tree; the data disk
    takes the data backup. Require 2x each plus 50MB headroom — never
    proceed blind into ENOSPC mid-replace.
    """
    checks = [(_app_root(), app_bytes * 2 + 50 * 1024 * 1024, "application"),
              ((data_dir or _data_dir()), data_bytes * 2 + 50 * 1024 * 1024, "data")]
    for path, required, label in checks:
        try:
            free = shutil.disk_usage(path).free
        except OSError as e:
            raise UpdateError(f"Could not check free {label} disk space: {e}")
        if free < required:
            raise UpdateError(
                f"Not enough free {label} disk for a safe update "
                f"(need ~{required // (1024 * 1024)}MB, have {free // (1024 * 1024)}MB). "
                f"Free space or prune data/update_backups and retry — nothing was changed."
            )


def _sqlite_online_copy(src: str, dest: str) -> None:
    """Consistent copy of a live SQLite file via the online backup API.

    A plain file copy of a database being written to can capture a torn
    transaction; the backup API gives a transaction-consistent snapshot.
    """
    import sqlite3
    src_con = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=10)
    try:
        dest_con = sqlite3.connect(dest)
        try:
            src_con.backup(dest_con)
        finally:
            dest_con.close()
    finally:
        src_con.close()


_DB_SUFFIXES = (".db", ".sqlite", ".sqlite3")


def _copy_file_for_backup(src: str, dest: str) -> None:
    """Copy one file for backup: symlinks stay links, live databases go
    through the online backup API, everything else is a plain copy."""
    if os.path.islink(src):
        try:
            if os.path.lexists(dest):
                os.remove(dest)
        except OSError:
            pass
        os.symlink(os.readlink(src), dest)
        return
    if os.path.basename(src).lower().endswith(_DB_SUFFIXES):
        try:
            _sqlite_online_copy(src, dest)
            return
        except Exception as e:
            logger.debug("SQLite backup fell back to plain copy for %s: %s", src, e)
    shutil.copy2(src, dest, follow_symlinks=False)


def _copy_any_for_backup(src: str, dest: str) -> None:
    if os.path.islink(src):
        _copy_file_for_backup(src, dest)
    elif os.path.isdir(src):
        shutil.copytree(src, dest, symlinks=True, dirs_exist_ok=True,
                        copy_function=_copy_file_for_backup)
    else:
        _copy_file_for_backup(src, dest)


def _copy_data_backup(data_dir: str, dest: str) -> int:
    """Copy data/ (minus updater scratch) into the backup. Returns bytes."""
    src = data_dir or _data_dir()
    out = os.path.join(dest, "data")
    # Transient SQLite journals are meaningless without exact coordination —
    # the main .db files go through the online backup API instead.
    ignore = shutil.ignore_patterns(*DATA_BACKUP_SKIP, "*-wal", "*-shm", "*-journal")
    if os.path.exists(out):
        shutil.rmtree(out, ignore_errors=True)
    shutil.copytree(src, out, ignore=ignore, symlinks=True,
                    copy_function=_copy_file_for_backup)
    return _dir_size(out)


def _write_manifest(dest: str, manifest: dict) -> None:
    path = os.path.join(dest, "manifest.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    os.replace(tmp, path)


def _backup_for_apply(state: dict, commit: str, data_dir: str | None, orphans: list[str],
                      root: str | None = None) -> dict:
    """Timestamped backup: manifest + .env + state copy + orphaned custom files."""
    root = root or _app_root()
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    dest = os.path.join(_backups_dir(data_dir), f"pre-update-{stamp}-{commit[:12]}")
    os.makedirs(dest, exist_ok=True)
    manifest = {
        "at": stamp,
        "status": "in-progress",
        "from_commit": state.get("installed_commit"),
        "to_commit": commit,
        "orphans": orphans,
    }
    _write_manifest(dest, manifest)
    for name in (".env",):
        src = os.path.join(root, name)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(dest, name))
    state_src = _state_path(data_dir)
    if os.path.exists(state_src):
        shutil.copy2(state_src, os.path.join(dest, "update_state.json"))
    orphans_dir = os.path.join(dest, "orphans")
    for rel in orphans:
        src = os.path.join(root, rel)
        if not os.path.exists(src) and not os.path.islink(src):
            continue
        target = os.path.join(orphans_dir, rel)
        os.makedirs(os.path.dirname(target) or orphans_dir, exist_ok=True)
        _copy_any_for_backup(src, target)
    return {"path": dest, "manifest": manifest}


def _backup_replaced_tree(root: str, dest: str, replaced: list[str]) -> int:
    """Copy every about-to-be-replaced top-level entry. Enables local restore
    without re-downloading (offline rollback, crash recovery)."""
    out = os.path.join(dest, "replaced")
    os.makedirs(out, exist_ok=True)
    for name in replaced:
        src = os.path.join(root, name)
        if not os.path.exists(src) and not os.path.islink(src):
            continue
        _copy_any_for_backup(src, os.path.join(out, name))
    return _dir_size(out)


def _verify_tree(root: str) -> None:
    """Post-apply sanity: the new tree must carry every required marker."""
    missing = [m for m in REQUIRED_MARKERS
               if not os.path.exists(os.path.join(root, m))]
    if missing:
        raise UpdateError(
            f"Applied tree failed verification (missing: {', '.join(missing)})")


def _restore_backup_tree(root: str, backup_path: str) -> None:
    """Restore the pre-apply source tree from a backup's replaced/ copy.

    The backup holds EVERY non-preserved entry from before the apply, so the
    restore first drops all current non-preserved entries (including files
    the failed update added) and then copies the backup back verbatim.
    """
    src = os.path.join(backup_path, "replaced")
    if not os.path.isdir(src):
        raise UpdateError("Backup has no replaced-tree copy to restore from")
    keep = set(os.listdir(src))
    for name in os.listdir(root):
        if name in PRESERVE_TOP or name in keep:
            continue
        target = os.path.join(root, name)
        if os.path.isdir(target) and not os.path.islink(target):
            shutil.rmtree(target, ignore_errors=True)
        elif os.path.exists(target) or os.path.islink(target):
            try:
                os.remove(target)
            except OSError:
                pass
    for name in keep:
        if name in PRESERVE_TOP:
            continue
        target = os.path.join(root, name)
        if os.path.isdir(target) and not os.path.islink(target):
            shutil.rmtree(target, ignore_errors=True)
        elif os.path.exists(target) or os.path.islink(target):
            try:
                os.remove(target)
            except OSError:
                pass
        origin = os.path.join(src, name)
        if os.path.isdir(origin) and not os.path.islink(origin):
            shutil.copytree(origin, target, dirs_exist_ok=True)
        else:
            shutil.copy2(origin, target)
    _clear_pycache(root)


def recover_interrupted_update(data_dir: str | None = None) -> dict | None:
    """Boot-time recovery: if the newest backup never completed, restore the
    pre-apply tree from it. Safe to call on every boot (no-op otherwise) and
    never raises — a failed recovery must not prevent startup.

    Only the NEWEST backup directory is eligible: restoring an older
    in-progress backup over a newer completed tree would clobber good files
    with stale ones.
    """
    try:
        entries = sorted(
            (os.path.join(_backups_dir(data_dir), d)
             for d in os.listdir(_backups_dir(data_dir))),
            key=lambda p: os.path.getmtime(p),
        )
    except OSError:
        return None
    if not entries:
        return None
    path = entries[-1]
    try:
        with open(os.path.join(path, "manifest.json"), encoding="utf-8") as f:
            manifest = json.load(f)
    except Exception:
        return None
    if manifest.get("status") != "in-progress":
        return None
    try:
        _restore_backup_tree(_app_root(), path)
        manifest["status"] = "recovered"
        manifest["recovered_at"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
        _write_manifest(path, manifest)
        logger.warning("Recovered interrupted update from %s", path)
        return {"recovered": True, "backup": path}
    except Exception as e:
        logger.error("Interrupted-update recovery failed (%s): %s", path, e)
        return {"recovered": False, "backup": path, "error": str(e)}


def _prune_old(data_dir: str | None = None) -> dict:
    """Keep the newest KEEP_BACKUPS update backups; drop older ones and any
    staged zips the state no longer references."""
    pruned_backups: list[str] = []
    try:
        entries = sorted(
            (os.path.join(_backups_dir(data_dir), d)
             for d in os.listdir(_backups_dir(data_dir))
             if os.path.isdir(os.path.join(_backups_dir(data_dir), d))),
            key=lambda p: os.path.getmtime(p),
        )
        for old in entries[:-KEEP_BACKUPS] if len(entries) > KEEP_BACKUPS else []:
            shutil.rmtree(old, ignore_errors=True)
            pruned_backups.append(os.path.basename(old))
    except OSError:
        pass
    pruned_staged = 0
    try:
        state = load_state(data_dir)
        keep = {((state.get("staged") or {}).get("path") or "")}
        staging = _staging_dir(data_dir)
        for name in os.listdir(staging):
            if not name.endswith(".zip"):
                continue
            full = os.path.join(staging, name)
            if full not in keep and os.path.isfile(full):
                try:
                    os.remove(full)
                    pruned_staged += 1
                except OSError:
                    pass
    except OSError:
        pass
    return {"pruned_backups": pruned_backups, "pruned_staged_zips": pruned_staged}


def active_stream_count() -> int:
    """Best-effort count of live chat streams (never raises)."""
    try:
        from routes import chat_routes as _cr
        return len(getattr(_cr, "_active_streams", {}) or {})
    except Exception:
        return 0


def _apply_files(staged: dict, state: dict, env: dict, data_dir: str | None,
                 *, root: str | None = None, record_installed: bool = True,
                 auto_restart: bool = True) -> dict:
    root = root or _app_root()
    with tempfile.TemporaryDirectory(prefix="aegis-update-") as tmp:
        tree = _safe_extract(staged["path"], tmp)
        incoming = set(os.listdir(tree))
        # Custom local additions (present locally, absent upstream, not
        # preserved) are moved into the backup instead of deleted.
        orphans = sorted(
            name for name in os.listdir(root)
            if name not in incoming and name not in PRESERVE_TOP
        )
        replaced = sorted(
            name for name in os.listdir(root) if name not in PRESERVE_TOP
        )
        replaced = sorted(
            name for name in os.listdir(root) if name not in PRESERVE_TOP
        )

        def _entry_bytes(name: str) -> int:
            full = os.path.join(root, name)
            if os.path.isdir(full) and not os.path.islink(full):
                return _dir_size(full)
            try:
                return os.path.getsize(full) if os.path.exists(full) else 0
            except OSError:
                return 0

        # Disk preflight BEFORE anything is written: data copy + replaced
        # copy must both fit with headroom, or nothing happens at all.
        # (The two may live on different disks — bind mounts — so check both.)
        _disk_preflight(
            sum(_entry_bytes(n) for n in replaced),
            _dir_size(data_dir or _data_dir(), DATA_BACKUP_SKIP),
            data_dir,
        )
        backup = _backup_for_apply(state, staged["commit"], data_dir, orphans, root=root)
        # An auto-restore is only valid once the replaced-tree copy below has
        # fully completed. A failure during the backup copies themselves must
        # NOT trigger a restore (the live tree is still untouched — restoring
        # from a partial copy would be the thing that destroys it).
        tree_backed_up = False
        # Frozen builds can't replace the running executable (OS lock): skip
        # exactly that file and report pending_restart instead of failing.
        locked_names = {os.path.basename(sys.executable)} if env.get("frozen") else set()
        skipped_locked: list[str] = []
        try:
            backup["manifest"]["replaced_bytes"] = _backup_replaced_tree(root, backup["path"], replaced)
            backup["manifest"]["data_bytes"] = _copy_data_backup(data_dir, backup["path"])
            tree_backed_up = True
            _write_manifest(backup["path"], backup["manifest"])
            # Replace: drop everything except preserved (orphans were already
            # copied into the backup), then copy the new tree over. Any I/O
            # error here aborts into the auto-restore below — except the
            # known-locked executable, which is skipped and reported.
            for name in os.listdir(root):
                if name in PRESERVE_TOP or name in locked_names:
                    if name in locked_names:
                        skipped_locked.append(name)
                    continue
                target = os.path.join(root, name)
                try:
                    if os.path.isdir(target) and not os.path.islink(target):
                        shutil.rmtree(target, ignore_errors=False)
                    else:
                        os.remove(target)
                except OSError as e:
                    raise UpdateError(f"Could not remove {name} during update: {e}")
            for name in incoming:
                if name in PRESERVE_TOP or name in locked_names:
                    if name in locked_names and name not in skipped_locked:
                        skipped_locked.append(name)
                    continue
                src = os.path.join(tree, name)
                dest = os.path.join(root, name)
                try:
                    if os.path.isdir(src) and not os.path.islink(src):
                        shutil.copytree(src, dest, dirs_exist_ok=True)
                    else:
                        shutil.copy2(src, dest)
                except OSError as e:
                    raise UpdateError(f"Could not install {name} during update: {e}")
            _clear_pycache(root)
            # Post-apply verification BEFORE recording success: a half-written
            # tree is restored from the backup immediately, in-process.
            _verify_tree(root)
        except Exception:
            backup["manifest"]["status"] = "failed"
            try:
                _write_manifest(backup["path"], backup["manifest"])
            except OSError:
                pass
            if not tree_backed_up:
                # Backup phase never completed: the live tree was not modified
                # yet, so restoring from a partial copy would DESTROY it.
                # Leave everything in place and report.
                raise UpdateError(
                    f"Update failed during backup ({backup['path']}) — the live "
                    f"tree and all data are untouched. Free disk space and retry.")
            try:
                _restore_backup_tree(root, backup["path"])
                backup["manifest"]["status"] = "failed-restored"
                _write_manifest(backup["path"], backup["manifest"])
            except Exception as restore_error:
                logger.error("Update auto-restore failed: %s", restore_error)
                raise UpdateError(
                    f"Update failed AND automatic restore failed — your pre-update "
                    f"tree is intact at {backup['path']}/replaced and data at "
                    f"{backup['path']}/data: {restore_error}")
            raise UpdateError(
                f"Update failed verification and was automatically rolled back "
                f"from {backup['path']} — no data was touched")
        backup["manifest"]["status"] = "complete"
        _write_manifest(backup["path"], backup["manifest"])
    previous = state.get("installed_commit")
    # Managed docker applies resolve their baseline from the stamped tree on
    # boot instead: recording here would claim the new version while the
    # rebuild may still fail, leaving a stale "up-to-date" behind it.
    if record_installed:
        state["installed_commit"] = staged["commit"]
        state["installed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    history = state.get("history") or []
    history.append({"commit": staged["commit"], "at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
                    "backup": backup["path"], "previous": previous})
    state["history"] = history[-10:]
    state["staged"] = None
    _save_state(state, data_dir)
    _invalidate_check_cache()
    pruned = _prune_old(data_dir)
    manifest = backup["manifest"]
    result: dict = {
        "applied": True,
        "mode": "source",
        "commit": staged["commit"],
        "previous": previous,
        "backup": backup["path"],
        "backup_bytes": int(manifest.get("replaced_bytes", 0)) + int(manifest.get("data_bytes", 0)),
        "verified": True,
        "orphans_moved": manifest["orphans"],
        "pruned": pruned,
    }
    if env.get("frozen") or skipped_locked:
        result["pending_restart"] = True
        result["skipped_locked"] = sorted(set(skipped_locked))
        result["note"] = ("Restart the app to finish (locked files were skipped: "
                          + ", ".join(sorted(set(skipped_locked))) + ")."
                          if skipped_locked else
                          "Restart the app to finish (the running executable was locked).")
    elif auto_restart:
        _schedule_restart()
        result["restart"] = "scheduled"
    return result


HOST_PROJECT_MOUNT = "/host-project"


def _compose_project_from_env_file(root: str) -> str | None:
    """COMPOSE_PROJECT_NAME set explicitly in the project .env, if any."""
    try:
        with open(os.path.join(root, ".env"), encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line.startswith("COMPOSE_PROJECT_NAME="):
                    continue
                val = line.split("=", 1)[1].strip().strip("'\"")
                if val and not val.startswith("$"):
                    return val
    except OSError:
        pass
    return None


def _sole_compose_project() -> str | None:
    """Project name when exactly one compose stack runs on the daemon.

    Needs no files or labels; deterministic on single-stack hosts, and
    refuses to guess when several stacks share the daemon.
    """
    try:
        out = subprocess.run(["docker", "compose", "ls", "--format", "json"],
                             capture_output=True, text=True, timeout=15)
    except Exception:
        return None
    if out.returncode != 0:
        return None
    names = []
    for line in (out.stdout or "").splitlines():
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if isinstance(obj, dict) and obj.get("Name"):
            names.append(obj["Name"])
    return names[0] if len(names) == 1 else None


def _resolve_compose_project(env: dict, root: str = HOST_PROJECT_MOUNT) -> str | None:
    direct = os.environ.get("COMPOSE_PROJECT_NAME")
    if direct:
        return direct
    try:
        ctx = _compose_context()
        if ctx and ctx.get("project"):
            return ctx["project"]
    except Exception:
        pass
    file_hit = _compose_project_from_env_file(root)
    if file_hit:
        return file_hit
    return _sole_compose_project()


def _docker_managed_skip_reason(env: dict) -> str | None:
    """Why one-click is unavailable (None = ready). Surfaced in the UI so a
    fallback never fails silently."""
    if os.environ.get("ODYSSEUS_ENABLE_HOST_DOCKER") != "true":
        return "host-docker overlay not enabled"
    if not env.get("docker"):
        return "not running in docker"
    if not env.get("docker_socket"):
        return "no docker socket mount"
    if not os.path.isfile(os.path.join(HOST_PROJECT_MOUNT, "docker-compose.yml")):
        return "no project mount"
    if _resolve_compose_project(env) is None:
        return "cannot determine compose project"
    try:
        out = subprocess.run(["docker", "compose", "version"],
                             capture_output=True, text=True, timeout=30)
    except Exception:
        return "compose plugin check failed"
    if out.returncode != 0:
        return "no working compose plugin"
    # The daemon itself must answer (a bad DOCKER_GID fails here, not later
    # after the source tree was already swapped).
    try:
        info = subprocess.run(["docker", "info"],
                              capture_output=True, text=True, timeout=15)
    except Exception:
        return "docker daemon unreachable"
    if info.returncode != 0:
        return "docker daemon unreachable"
    return None


def _docker_managed_context(env: dict) -> dict | None:
    """One-click rebuild prerequisites inside the container (see
    _docker_managed_skip_reason). Anything missing returns None and the
    caller falls back to staged+host-command.
    """
    if _docker_managed_skip_reason(env) is not None:
        return None
    project = _resolve_compose_project(env)
    if not project:
        return None
    return {"root": HOST_PROJECT_MOUNT, "project": project}


REBUILD_CLAIM_MAX_AGE_S = 90 * 60
REBUILD_CLAIM_NAME = "rebuild.inflight"


def _rebuild_claim_path(data_dir: str | None = None) -> str:
    return os.path.join(_staging_dir(data_dir), REBUILD_CLAIM_NAME)


def _read_rebuild_claim(data_dir: str | None = None) -> dict | None:
    """Live rebuild claim, or None when absent, unreadable, or stale.

    A claim is written when a detached rebuild launches and removed when its
    compose run exits; a leftover older than the max age is a dead CLI (kill
    -9, power loss) and is treated as absent — the next spawn overwrites it.
    """
    try:
        with open(_rebuild_claim_path(data_dir), encoding="utf-8") as f:
            info = json.load(f)
        if not isinstance(info, dict) or not info.get("commit"):
            return None
        age = time.time() - float(info.get("t") or 0)
    except Exception:
        return None
    return info if age < REBUILD_CLAIM_MAX_AGE_S else None


def _write_rebuild_claim(data_dir: str | None, commit: str) -> None:
    path = _rebuild_claim_path(data_dir)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"commit": commit, "t": time.time()}, f)
    os.replace(tmp, path)


def _clear_rebuild_claim(data_dir: str | None) -> None:
    try:
        os.remove(_rebuild_claim_path(data_dir))
    except OSError:
        pass


def _spawn_host_rebuild(project_root: str, project: str, commit: str,
                        data_dir: str | None = None) -> str:
    """Launch a throwaway helper container to rebuild+recreate the stack.

    A process inside the app container cannot orchestrate its own teardown:
    compose's recreate stops AND removes the old container, killing any
    in-container orchestrator mid-handoff (setsid only survives the request
    lifecycle; stop_grace_period only delays SIGKILL). So the app only swaps
    source and spawns this helper, which is a separate container: it outlives
    the app's teardown by construction, runs build+recreate, clears the
    rebuild claim, and exits (--rm). No shell interpolation anywhere below:
    the project name is validated and all inner paths are constants.
    """
    from core.platform_compat import detached_popen_kwargs
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", project):
        raise UpdateError(f"Refusing rebuild: unexpected compose project name {project!r}")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise UpdateError("Refusing rebuild: expected a full commit SHA")
    ctx = _compose_context() or {}
    working, image = ctx.get("working_dir"), ctx.get("image")
    if not working or not image or working == "/":
        raise UpdateError("Cannot inspect own container for the update helper")
    if working == HOST_PROJECT_MOUNT:
        # Poisoned generation: this container was itself created by the old
        # helper that ran compose from a mount point, so its labels point at
        # the mount instead of the real project dir. Spawning from here would
        # repeat the corruption (phantom data dirs, empty-data boot). Refuse
        # loudly; a one-time manual rebuild script run re-anchors everything.
        raise UpdateError(
            "Refusing rebuild: compose working dir points at the project mount, "
            "not the real project directory (stale labels from a mount-point "
            "rebuild). Rebuild once via the host script, then one-click is safe again.")
    helper = f"aegis-updater-{commit[:12]}"
    try:
        subprocess.run(["docker", "rm", "-f", helper],
                       capture_output=True, timeout=30)
    except Exception:
        pass
    # Mount the project at its IDENTICAL host path: relative bind sources
    # (./data) resolve against the project directory, so mounting elsewhere
    # once pointed them at phantom host dirs and booted the app on empty
    # data. Identical-path mounting makes the helper behave exactly like a
    # host-side compose run.
    # Claim/log paths below are relative to the project dir (the helper cwd),
    # whose layout (data/update_staging, logs) mirrors the shared DATA_DIR.
    # Preflight first: if the mount ever points somewhere without project
    # files, fail LOUDLY into the log instead of letting compose create
    # phantom bind dirs on the host and boot the app on empty data.
    inner = ("test -f docker-compose.yml || { echo 'update helper: no compose file "
             "in project dir, refusing rebuild' >>logs/rebuild.log 2>&1; exit 1; }; "
             "docker compose -p " + project + " up -d --build "
             ">>logs/rebuild.log 2>&1; rm -f data/update_staging/rebuild.inflight")
    cmd = ["docker", "run", "-d", "--rm", "--name", helper,
           "-v", "/var/run/docker.sock:/var/run/docker.sock",
           "-v", working + ":" + working, "-w", working,
           "--entrypoint", "sh", image,
           "-c", inner]
    log_path = os.path.join(project_root, "logs", "rebuild.log")
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    with open(log_path, "ab") as logf:
        logf.write(f"[{stamp}] one-click rebuild for update {commit} spawned helper {helper}\n".encode())
        subprocess.Popen(cmd, cwd=project_root, stdout=logf, stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL, **detached_popen_kwargs())
    return log_path


def _apply_docker(staged: dict, state: dict, env: dict, data_dir: str | None,
                  force: bool = False) -> dict:
    """Baked-image containers must be rebuilt on the host: stage the tree
    where the host can see it (bind-mounted data/) and hand back the command.
    """
    if not force:
        live = active_stream_count()
        if live > 0:
            raise UpdateError(
                f"{live} chat stream(s) active — retry when idle or force the "
                f"apply (in-flight turns may error)")
    with tempfile.TemporaryDirectory(prefix="aegis-update-") as tmp:
        tree = _safe_extract(staged["path"], tmp)
        dest = os.path.join(_staging_dir(data_dir), staged["commit"])
        if os.path.exists(dest):
            shutil.rmtree(dest, ignore_errors=True)
        shutil.copytree(tree, dest)
    # Record what the rebuilt image contains: the staged tree has no .git,
    # so without this the post-rebuild check would see a stale baseline and
    # (with auto-update on) queue another rebuild immediately.
    try:
        with open(os.path.join(dest, _DEPLOY_STAMP), "w", encoding="utf-8") as f:
            f.write(staged["commit"])
    except OSError:
        pass
    managed = _docker_managed_context(env)
    if managed is not None:
        # One-click path: same backup/replace/verify machinery as source
        # installs, but targeting the host project tree (data/, logs/, .env
        # stay in place — they are the same bind mounts), then a detached
        # rebuild. installed_commit is deliberately NOT recorded here: the
        # stamped tree resolves the baseline when the new image boots, and
        # recording now would claim success if the rebuild then fails.
        # Single-flight first: a second apply while a rebuild runs would
        # stack another stop/start cycle on the same stack (each cycle can
        # strand the previous orchestrator mid-handoff). Refuse instead.
        live_claim = _read_rebuild_claim(data_dir)
        if live_claim is not None:
            raise UpdateError(
                "A rebuild is already in progress"
                f" ({(live_claim.get('commit') or '')[:12]}); check logs/rebuild.log "
                "instead of stacking another one.")
        result = _apply_files(staged, state, env, data_dir, root=managed["root"],
                              record_installed=False, auto_restart=False)
        try:
            with open(os.path.join(managed["root"], _DEPLOY_STAMP), "w", encoding="utf-8") as f:
                f.write(staged["commit"])
        except OSError:
            pass
        _write_rebuild_claim(data_dir, staged["commit"])
        try:
            log_path = _spawn_host_rebuild(managed["root"], managed["project"],
                                           staged["commit"], data_dir)
        except Exception:
            _clear_rebuild_claim(data_dir)
            raise
        result.update({"applied": "rebuilding", "mode": "docker", "rebuild_log": log_path,
                       "note": "Rebuilding now — the page will drop for a few minutes."})
        return result
    host_cmd = "docker compose up -d --build"
    compose = env.get("compose") or {}
    if compose.get("working_dir") not in (None, "", "/", HOST_PROJECT_MOUNT):
        host_cmd = f"cd {compose['working_dir']} && docker compose -p {compose.get('project', 'odysseus')} up -d --build"
    state["staged"] = {**staged, "host_path": dest,
                       "note": "docker-staged; rebuild on the host to finish"}
    _save_state(state, data_dir)
    return {
        "applied": "staged",
        "mode": "docker",
        "commit": staged["commit"],
        "staged_path": dest,
        "restart_required": True,
        "managed_skip": _docker_managed_skip_reason(env),
        "host_command": (
            f"{host_cmd}  # then: copy {dest} over the source tree "
            "(keep data/, logs/, .env) BEFORE rebuilding, or point the build at it"
        ),
    }


def rollback(data_dir: str | None = None) -> dict:
    """Re-install the previously installed commit.

    Prefers the local backup of the current install (instant, works
    offline — the backup predates the current tree, so it IS the previous
    tree). Falls back to re-downloading the previous commit.
    """
    if not _op_lock.acquire(blocking=False):
        raise UpdateBusy("Another update operation is already running")
    try:
        state = load_state(data_dir)
        history = state.get("history") or []
        if not history:
            raise UpdateError("No previous update to roll back to")
        current = state.get("installed_commit")
        cur_entry = next(
            (e for e in reversed(history) if e.get("commit") == current),
            None,
        )
        previous = None
        for entry in reversed(history):
            if entry.get("commit") and entry["commit"] != current:
                previous = entry["commit"]
                break
        if not previous and cur_entry:
            # Single-apply history: the previous commit rides on the entry.
            previous = cur_entry.get("previous")
        if not previous:
            raise UpdateError("No previous update to roll back to")
        env = detect_environment()
        if not env["docker"]:
            cur_backup = (cur_entry or {}).get("backup")
            if cur_backup:
                try:
                    _restore_backup_tree(_app_root(), cur_backup)
                    now = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
                    state["installed_commit"] = previous
                    history.append({"commit": previous, "at": now,
                                    "backup": cur_backup,
                                    "previous": current, "rollback": True})
                    state["history"] = history[-10:]
                    state["staged"] = None
                    _save_state(state, data_dir)
                    _invalidate_check_cache()
                    _prune_old(data_dir)
                    result = {"rolled_back": True, "offline": True,
                              "commit": previous, "previous": current,
                              "backup": cur_backup}
                    if env.get("frozen"):
                        result["pending_restart"] = True
                    else:
                        _schedule_restart()
                        result["restart"] = "scheduled"
                    return result
                except Exception as e:
                    logger.warning("Local rollback failed, falling back to re-download: %s", e)
        download_update(previous, data_dir)
        return apply_staged(previous, data_dir)
    finally:
        _op_lock.release()


_restart_timer: threading.Timer | None = None


def _schedule_restart(delay_s: int = 3) -> None:
    """Restart the process after the HTTP response has been flushed."""
    global _restart_timer
    if getattr(sys, "frozen", False):
        return

    def _do_restart() -> None:
        try:
            logger.warning("Self-update installed — restarting process")
            os.execv(sys.executable, [sys.executable, *sys.argv])
        except Exception as e:
            logger.error("Self-update restart failed: %s", e)

    _restart_timer = threading.Timer(delay_s, _do_restart)
    _restart_timer.daemon = True
    _restart_timer.start()


def status(data_dir: str | None = None) -> dict:
    """Full updater status for the admin UI (never raises on IO)."""
    state = load_state(data_dir)
    latest = state.get("latest_known")
    installed = state.get("installed_commit") or installed_commit(data_dir)
    staged = state.get("staged")
    if staged and not os.path.exists(staged.get("path", "")):
        staged = None
    history = state.get("history") or []
    return {
        "repo": REPO,
        "channel": CHANNEL_BRANCH,
        "current": {"version": local_version(), "commit": installed},
        "latest": latest,
        "update_available": bool(latest and installed and latest.get("sha") != installed),
        "unknown_baseline": installed is None,
        "checked_at": state.get("checked_at"),
        "staged": ({"commit": staged.get("commit"), "at": staged.get("at")} if staged else None),
        "history": [{"commit": h.get("commit"), "at": h.get("at"),
                     "backup": h.get("backup")} for h in history[-5:]],
        "can_rollback": any(h.get("commit") and h["commit"] != installed for h in history),
        "environment": detect_environment(),
        "last_apply": state.get("last_apply"),
    }


# Accept-slot for background applies: the HTTP route must answer in
# milliseconds (proxies time out long requests with a bare 502), so the
# heavy apply runs after the response. This flag serializes accepts; the
# worker clears it when done. A stale flag (crashed worker) ages out.
_APPLY_SLOT_LOCK = threading.Lock()
_apply_accepted_at: float | None = None
APPLY_SLOT_STALE_S = 30 * 60


def _record_apply_result(data_dir: str | None, commit: str | None, ok: bool,
                         result: dict | None = None, error: str | None = None) -> None:
    try:
        state = load_state(data_dir)
        entry: dict = {"at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
                       "commit": commit, "ok": bool(ok)}
        if ok:
            entry["result"] = result or {}
        else:
            entry["error"] = error or "apply failed"
        state["last_apply"] = entry
        _save_state(state, data_dir)
    except Exception:
        pass


def try_claim_apply_slot() -> bool:
    """Reserve the single background-apply slot. False when one is live
    (or finished so recently the worker hasn't cleared yet)."""
    global _apply_accepted_at
    with _APPLY_SLOT_LOCK:
        if (_apply_accepted_at is not None
                and time.time() - _apply_accepted_at < APPLY_SLOT_STALE_S):
            return False
        _apply_accepted_at = time.time()
        return True


def _release_apply_slot() -> None:
    global _apply_accepted_at
    with _APPLY_SLOT_LOCK:
        _apply_accepted_at = None


def _apply_in_background(commit: str, data_dir: str | None, force: bool) -> None:
    """BackgroundTasks entry: run the full apply, record the outcome for the
    status poll. Never raises out of the worker. Always releases the accept
    slot the route claimed, so a later update is never wedged behind this one.
    """
    try:
        try:
            result = apply_staged(commit, data_dir, force=force)
        except UpdateBusy as e:
            _record_apply_result(data_dir, commit, False, error=str(e))
            return
        except Exception as e:
            _record_apply_result(data_dir, commit, False,
                                 error=f"{type(e).__name__}: {e}")
            logger.exception("Background apply failed")
            return
        _record_apply_result(data_dir, commit, True, result=result)
    finally:
        _release_apply_slot()


AUTO_UPDATE_TICK_SECONDS = 6 * 3600


def auto_update_settings() -> dict:
    """Hands-free update config (all defensive: the settings file is hand-editable)."""
    from src.settings import get_setting

    def _hour(key: str, default: int) -> int:
        try:
            value = int(get_setting(key, default))
        except (TypeError, ValueError):
            return default
        return value if 0 <= value <= 23 else default

    return {
        "enabled": bool(get_setting("auto_update_enabled", False)),
        "start_hour": _hour("auto_update_start_hour", 2),
        "end_hour": _hour("auto_update_end_hour", 6),
    }


def _in_maintenance_window(hour: int, start: int, end: int) -> bool:
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def _record_auto(data_dir: str | None, commit: str | None, result: str) -> None:
    try:
        state = load_state(data_dir)
        state["last_auto"] = {
            "at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time())),
            "t": time.time(),
            "commit": commit,
            "result": result,
        }
        _save_state(state, data_dir)
    except Exception:
        pass


def _auto_backoff(state: dict, cooldown_s: int = 24 * 3600) -> bool:
    """Skip this tick when the previous pass failed recently.

    A persistently failing update (full disk, broken tree) must not redo a
    backup+replace every hour; retry daily instead. Busy skips are not
    failures and never back off.
    """
    last = state.get("last_auto") or {}
    if "-failed" not in (last.get("result") or ""):
        return False
    try:
        return (time.time() - float(last.get("t") or 0)) < cooldown_s
    except (TypeError, ValueError):
        return False


def _docker_legacy_needs_host() -> bool:
    """True when docker has no one-click mounts: applying would only re-stage
    the same tree, so the auto pass should stay quiet instead."""
    try:
        env = detect_environment()
    except Exception:
        return False
    return bool(env.get("docker")) and _docker_managed_context(env) is None


def maybe_auto_update(data_dir: str | None = None, now_hour: int | None = None) -> dict:
    """One hands-free update pass: check, download, and finish like the button.

    Never raises — every failure is recorded in update state and returned.
    Honors the maintenance window and skips while chats stream; backs off a
    day after a failed pass so a broken tree is not re-applied hourly.
    """
    cfg = auto_update_settings()
    if not cfg["enabled"]:
        return {"acted": False, "reason": "disabled"}
    hour = now_hour if now_hour is not None else time.localtime().tm_hour
    if not _in_maintenance_window(hour, cfg["start_hour"], cfg["end_hour"]):
        return {"acted": False, "reason": "outside-window"}
    state = load_state(data_dir)
    if _auto_backoff(state):
        return {"acted": False, "reason": "backoff"}
    if active_stream_count() > 0:
        _record_auto(data_dir, None, "busy-streams")
        return {"acted": False, "reason": "busy-streams"}
    try:
        res = check_for_updates(force=True, data_dir=data_dir)
    except Exception as e:
        _record_auto(data_dir, None, f"check-failed: {e}")
        return {"acted": False, "reason": "check-failed"}
    latest = (res.get("latest") or {}).get("sha")
    if not latest:
        return {"acted": False, "reason": "remote-unreachable"}
    if not res.get("update_available"):
        return {"acted": False, "reason": "up-to-date"}
    state = load_state(data_dir)
    staged = state.get("staged") or {}
    if _read_rebuild_claim(data_dir) is not None:
        # A rebuild launched earlier is still running (or died without
        # cleaning up recently) — do not stack another stop/start cycle.
        return {"acted": False, "reason": "rebuild-in-flight"}
    staged_current = (staged.get("commit") == latest
                      and os.path.exists(staged.get("path", "")))
    if staged_current and _docker_legacy_needs_host():
        _record_auto(data_dir, latest, "staged-needs-host")
        return {"acted": False, "reason": "staged-needs-host"}
    if not staged_current:
        try:
            dl = download_update(latest, data_dir)
            latest = dl.get("commit") or latest
        except UpdateBusy:
            _record_auto(data_dir, latest, "busy-op")
            return {"acted": False, "reason": "busy-op"}
        except Exception as e:
            _record_auto(data_dir, latest, f"download-failed: {e}")
            return {"acted": False, "reason": "download-failed"}
    # Same finish as the update button: source installs replace+restart,
    # docker with one-click mounts rebuilds, otherwise it stages for a
    # manual rebuild. Nothing here restarts or recreates synchronously.
    try:
        ap = apply_staged(latest, data_dir, force=False)
    except UpdateBusy:
        _record_auto(data_dir, latest, "busy-op")
        return {"acted": False, "reason": "busy-op"}
    except Exception as e:
        _record_auto(data_dir, latest, f"apply-failed: {e}")
        return {"acted": False, "reason": "apply-failed"}
    _record_auto(data_dir, latest, f"applied-{ap.get('mode')}-{ap.get('applied')}")
    return {"acted": True, "reason": "applied", "commit": latest,
            "mode": ap.get("mode"), "applied": ap.get("applied")}
