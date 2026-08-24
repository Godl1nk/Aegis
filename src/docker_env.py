"""Docker execution backend for the agent's bash and python tools.

Ported from Hermes (tools/environments/docker.py), scoped to the Docker-only
backend: security-hardened persistent per-session containers (cap-drop ALL,
no-new-privileges, PID limits, tmpfs mounts), container reuse across
commands, and idle reaping. The Modal/SSH/Singularity/Daytona backends were
deliberately not ported.

Settings (settings.json):
  terminal_env: "local" (default) | "docker"
  docker_image: image for agent containers
      (default: nikolaik/python-nodejs:python3.11-nodejs20 — Hermes default)
  docker_mount_workspace: bool (default False). When True the agent's working
      directory is bind-mounted at /workspace — the sandbox can then reach
      host files, so the dangerous-command approval layer stays ACTIVE
      (mirrors Hermes ``has_host_access``). When False the container is fully
      isolated and the approval layer is skipped (nothing it runs can touch
      the host).

Because the approval layer is skipped for the isolated backend, every tool
that can reach a shell must route through here when terminal_env=docker —
otherwise it runs on the host with the guard switched off. `bash` and
`python` both do (see agent_tools/subprocess_tools.py). `#!bg` background
jobs have no containerized backend, so the dispatcher refuses them under
terminal_env=docker rather than silently running them on the host.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
import time
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# Common Docker Desktop install paths checked when 'docker' is not in PATH.
_DOCKER_SEARCH_PATHS = [
    "/usr/local/bin/docker",
    "/opt/homebrew/bin/docker",
    "/Applications/Docker.app/Contents/Resources/bin/docker",
]

_docker_executable: Optional[str] = None  # resolved once, cached

DEFAULT_DOCKER_IMAGE = "nikolaik/python-nodejs:python3.11-nodejs20"

# Security profile — copied from Hermes _build_security_args.
_BASE_SECURITY_ARGS = [
    "--cap-drop", "ALL",
    "--cap-add", "DAC_OVERRIDE",
    "--cap-add", "CHOWN",
    "--cap-add", "FOWNER",
    "--security-opt", "no-new-privileges",
    "--tmpfs", "/tmp:rw,nosuid,size=512m",
    "--tmpfs", "/var/tmp:rw,noexec,nosuid,size=256m",
    "--tmpfs", "/run:rw,noexec,nosuid,size=64m",
]

# Extra caps needed when the container starts as root and an init/entrypoint
# must drop privileges (via `s6-setuidgid`, `gosu`, `su`, or similar).
_PRIVDROP_CAP_ARGS = [
    "--cap-add", "SETUID",
    "--cap-add", "SETGID",
]

# Default per-container PID limit.
_DEFAULT_PIDS_LIMIT = "256"

# Containers idle longer than this are removed (Hermes _cleanup_inactive_envs).
IDLE_LIFETIME_SECONDS = 300

_LABEL_VALUE_OK_RE = re.compile(r"[^A-Za-z0-9_.-]")

# container name → last-used timestamp (in-process; orphans from a previous
# process are reaped by the label sweep in reap_orphan_containers()).
_last_used: Dict[str, float] = {}
_container_lock = asyncio.Lock()


def find_docker() -> Optional[str]:
    """Locate the docker executable (PATH, then Docker Desktop paths)."""
    global _docker_executable
    if _docker_executable:
        return _docker_executable
    exe = shutil.which("docker")
    if not exe:
        for candidate in _DOCKER_SEARCH_PATHS:
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                exe = candidate
                break
    _docker_executable = exe
    return exe


def docker_available() -> bool:
    """True when the docker CLI exists and the daemon answers."""
    exe = find_docker()
    if not exe:
        return False
    try:
        result = subprocess.run(
            [exe, "info", "--format", "{{.ServerVersion}}"],
            capture_output=True, text=True, timeout=10,
            stdin=subprocess.DEVNULL,
        )
        return result.returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


def _sanitize_label_value(value: str) -> str:
    """Coerce *value* into a Docker label-safe form (alnum + ``_.-``, ≤63 chars)."""
    if not isinstance(value, str) or not value:
        return "unknown"
    cleaned = _LABEL_VALUE_OK_RE.sub("-", value)[:63]
    return cleaned or "unknown"


def get_docker_settings() -> Dict[str, object]:
    """Read the terminal backend settings."""
    try:
        from src.settings import get_setting
        return {
            "env_type": str(get_setting("terminal_env", "local") or "local").lower(),
            "image": str(get_setting("docker_image", DEFAULT_DOCKER_IMAGE) or DEFAULT_DOCKER_IMAGE),
            "mount_workspace": bool(get_setting("docker_mount_workspace", False)),
        }
    except Exception:
        return {"env_type": "local", "image": DEFAULT_DOCKER_IMAGE, "mount_workspace": False}


def _container_name(session_id: str) -> str:
    return f"aegis-term-{_sanitize_label_value(session_id or 'default')[:24]}"


async def _run_docker(args: list, timeout: float = 60) -> tuple:
    """Run a docker CLI command; returns (exit_code, stdout, stderr)."""
    exe = find_docker()
    proc = await asyncio.create_subprocess_exec(
        exe, *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return (124, "", f"docker command timed out after {timeout}s")
    return (
        proc.returncode or 0,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )


async def _ensure_container(session_id: str, image: str, mount_workspace: bool,
                            workspace: Optional[str]) -> Optional[str]:
    """Return the name of a running container for this session, creating or
    restarting it as needed. None on failure."""
    name = _container_name(session_id)
    async with _container_lock:
        code, out, _ = await _run_docker(
            ["ps", "-a", "--filter", f"name=^{name}$", "--format", "{{.State}}"]
        )
        state = out.strip().splitlines()[0].strip() if code == 0 and out.strip() else ""
        if state == "running":
            _last_used[name] = time.time()
            return name
        if state:  # exists but stopped — restart it
            code, _, err = await _run_docker(["start", name])
            if code == 0:
                _last_used[name] = time.time()
                return name
            logger.warning("Docker: failed to restart %s (%s); recreating", name, err.strip())
            await _run_docker(["rm", "-f", name])

        run_args = [
            "run", "-d", "--name", name,
            "--label", "aegis.terminal=1",
            "--label", f"aegis.session={_sanitize_label_value(session_id or 'default')}",
            "--pids-limit", _DEFAULT_PIDS_LIMIT,
            *_BASE_SECURITY_ARGS,
            *_PRIVDROP_CAP_ARGS,
            "-w", "/workspace",
        ]
        if mount_workspace and workspace and os.path.isdir(workspace):
            run_args += ["-v", f"{workspace}:/workspace"]
        run_args += [image, "sleep", "infinity"]
        code, _, err = await _run_docker(run_args, timeout=300)  # first run may pull
        if code != 0:
            logger.warning("Docker: container create failed: %s", err.strip()[:400])
            return None
        # A fresh unmounted container needs /workspace to exist.
        if not (mount_workspace and workspace):
            await _run_docker(["exec", name, "mkdir", "-p", "/workspace"])
        _last_used[name] = time.time()
        return name


async def execute_in_docker(command: str, session_id: str, *,
                            timeout: float, workspace: Optional[str] = None):
    """Execute *command* inside the session's container.

    Returns (stdout, stderr, exit_code, timed_out) — same shape as the local
    streaming runner — or an error dict when the backend is unusable.
    """
    settings = get_docker_settings()
    if not docker_available():
        return {"error": (
            "terminal_env=docker but Docker is not available. Start Docker "
            "(or set terminal_env back to 'local' in settings)."
        ), "exit_code": 1}

    name = await _ensure_container(
        session_id, str(settings["image"]),
        bool(settings["mount_workspace"]), workspace,
    )
    if not name:
        return {"error": "Docker container could not be created.", "exit_code": 1}

    code, out, err = await _run_docker(
        ["exec", "-w", "/workspace", name, "bash", "-lc", command],
        timeout=timeout,
    )
    _last_used[name] = time.time()
    await reap_idle_containers()
    return (out, err, code, code == 124)


async def reap_idle_containers(lifetime_seconds: int = IDLE_LIFETIME_SECONDS) -> None:
    """Remove session containers idle past their lifetime (Hermes idle reap)."""
    now = time.time()
    for name, last in list(_last_used.items()):
        if now - last > lifetime_seconds:
            _last_used.pop(name, None)
            await _run_docker(["rm", "-f", name])
            logger.info("Docker: reaped idle terminal container %s", name)


def reap_orphan_containers() -> int:
    """Remove aegis-labeled terminal containers left over from a previous
    process (Hermes reap_orphan_containers). Called at startup; sync."""
    exe = find_docker()
    if not exe:
        return 0
    try:
        result = subprocess.run(
            [exe, "ps", "-aq", "--filter", "label=aegis.terminal=1"],
            capture_output=True, text=True, timeout=15,
            stdin=subprocess.DEVNULL,
        )
        ids = [c for c in (result.stdout or "").split() if c]
        if not ids:
            return 0
        subprocess.run([exe, "rm", "-f", *ids], capture_output=True,
                       timeout=60, stdin=subprocess.DEVNULL)
        logger.info("Docker: reaped %d orphan terminal container(s)", len(ids))
        return len(ids)
    except (subprocess.SubprocessError, OSError) as e:
        logger.debug("Docker orphan reap skipped: %s", e)
        return 0
