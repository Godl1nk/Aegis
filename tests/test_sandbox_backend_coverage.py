"""The terminal sandbox must cover every tool that can reach a shell.

`terminal_env=docker` used to isolate the foreground `bash` tool only, while
two paths kept running on the host:

  * `python` — `PythonTool` never consulted the docker settings. `-I` only
    drops env/user site-packages, so `import os; os.system(...)` reached the
    same shell `bash` did, on the host.
  * `#!bg` — `bg_jobs.launch` always spawns a host process; there is no
    containerized background backend.

Both were reachable from model-controlled content, and they compounded with
`check_command_guard`'s deliberate skip for the isolated docker backend
(command_approval.py): the guard is waived because the command "can't touch
the host", which was false for these two.

Separately, `python` was never wired to the approval guard in ANY mode, so the
hardline floor and user deny rules were one `os.system` away from a bypass.
"""
import asyncio

import pytest

from src.agent_tools import ToolBlock  # noqa: E402  (import first to avoid circular)


def _te():
    """The live src.tool_execution module.

    Resolved per call, never bound at import: other suites reload this module,
    and a module-level `from ... import execute_tool_block` would then run the
    OLD function object whose globals the fixture below never patched.
    """
    import src.tool_execution as tool_execution
    return tool_execution


def _run_block(block, **kwargs):
    return asyncio.run(_te().execute_tool_block(block, **kwargs))


@pytest.fixture(autouse=True)
def _admin_owner(monkeypatch):
    """bash/python are admin-gated on public deployments. These tests are about
    what happens AFTER that gate, so clear it."""
    monkeypatch.setattr(_te(), "_owner_is_admin", lambda owner: True)
    monkeypatch.setattr(_te(), "is_public_blocked_tool", lambda tool: False)


def _docker_on(monkeypatch, *, mount_workspace=False):
    import src.docker_env as docker_env
    monkeypatch.setattr(docker_env, "get_docker_settings", lambda: {
        "env_type": "docker",
        "image": docker_env.DEFAULT_DOCKER_IMAGE,
        "mount_workspace": mount_workspace,
    })


def test_python_runs_in_container_when_docker_backend_selected(monkeypatch):
    import src.docker_env as docker_env
    from src.agent_tools import subprocess_tools

    _docker_on(monkeypatch)
    seen = {}

    async def fake_exec_in_docker(command, session_id, *, timeout, workspace=None):
        seen["command"] = command
        return ("in-container", "", 0, False)

    monkeypatch.setattr(docker_env, "execute_in_docker", fake_exec_in_docker)

    def exploded(*a, **kw):
        raise AssertionError("python escaped the sandbox onto the host")

    monkeypatch.setattr(subprocess_tools.asyncio, "create_subprocess_exec", exploded)

    result = asyncio.run(
        subprocess_tools.PythonTool().execute("print('hi')", {"session_id": "s1"})
    )

    assert result["exit_code"] == 0
    assert result["output"] == "in-container"
    # Source rides in base64 so quotes/newlines in model-authored code cannot
    # break out of the `bash -lc` wrapper.
    assert "print('hi')" not in seen["command"]
    assert "base64 -d" in seen["command"]


def test_background_jobs_refused_under_docker_backend(monkeypatch):
    """`#!bg` has no containerized backend, so it must be refused rather than
    silently launched on the host."""
    from src import bg_jobs

    _docker_on(monkeypatch)

    def exploded(*a, **kw):
        raise AssertionError("#!bg launched a host process under docker isolation")

    monkeypatch.setattr(bg_jobs, "launch", exploded)

    desc, result = _run_block(ToolBlock("bash", "#!bg\nsleep 30"), session_id="s1")

    assert result.get("blocked") is True
    assert result["exit_code"] == 126
    assert "#!bg" in result["error"]


def test_background_jobs_still_allowed_on_local_backend(monkeypatch):
    from src import bg_jobs
    import src.docker_env as docker_env

    monkeypatch.setattr(docker_env, "get_docker_settings", lambda: {
        "env_type": "local",
        "image": docker_env.DEFAULT_DOCKER_IMAGE,
        "mount_workspace": False,
    })
    monkeypatch.setattr(bg_jobs, "launch", lambda *a, **kw: {"id": "job123"})

    desc, result = _run_block(ToolBlock("bash", "#!bg\nsleep 30"), session_id="s1")

    assert result["exit_code"] == 0
    assert result["bg_job_id"] == "job123"


def test_python_is_gated_by_the_command_guard(monkeypatch):
    """The guard's hardline floor must apply to shelled-out commands in Python
    source, not just to `bash` blocks."""
    from src.agent_tools import subprocess_tools

    def exploded(*a, **kw):
        raise AssertionError("ungated python reached the host interpreter")

    monkeypatch.setattr(subprocess_tools.asyncio, "create_subprocess_exec", exploded)

    desc, result = _run_block(
        ToolBlock("python", 'import os; os.system("rm -rf /")'), session_id="s1",
    )

    assert result.get("blocked") is True
    assert result["exit_code"] == 126
    assert desc == "python: BLOCKED"


def test_ordinary_python_is_not_blocked(monkeypatch):
    """The guard must not turn into a tax on normal code."""
    from src.agent_tools import subprocess_tools

    ran = {}

    async def fake_handler(content, ctx):
        ran["content"] = content
        return {"output": "42", "exit_code": 0}

    monkeypatch.setitem(
        __import__("src.agent_tools", fromlist=["TOOL_HANDLERS"]).TOOL_HANDLERS,
        "python", fake_handler,
    )
    assert subprocess_tools  # imported for symmetry with the blocked case

    desc, result = _run_block(ToolBlock("python", "print(6 * 7)"), session_id="s1")

    assert result["exit_code"] == 0
    assert ran["content"] == "print(6 * 7)"
