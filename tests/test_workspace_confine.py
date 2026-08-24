"""Workspace confinement.

The agent's per-turn workspace is a single context-local binding set in
execute_tool_block. The shared path resolvers (_resolve_tool_path /
_resolve_search_root) and the subprocess cwd helper (agent_cwd) read it, so
confinement is enforced in ONE place: a tool that uses the shared helpers is
confined automatically and a new tool cannot accidentally bypass it.

Covers: the resolver helper, the central binding (the safety net), end-to-end
confinement of read/write/edit/grep/ls + subprocess cwd via execute_tool_block,
the get_workspace tool, no-leak across calls, and the admin-gated browse route.
"""
import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.tool_execution import (
    _AGENT_WORKDIR,
    _active_workspace,
    _resolve_search_root,
    _resolve_tool_path,
    _resolve_tool_path_in_workspace,
    agent_cwd,
    execute_tool_block,
    get_active_workspace,
)


def _block(tool, content=""):
    return SimpleNamespace(tool_type=tool, content=content)


@pytest.fixture
def ws():
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "a.txt"), "w") as f:
        f.write("x")
    return d


@pytest.fixture
def admin(monkeypatch):
    """Pass the public-tool gate so file tools dispatch in tests."""
    monkeypatch.setattr(
        "src.tool_execution.owner_is_admin_or_single_user", lambda owner: True
    )


# ── the resolver helper ────────────────────────────────────────────────

def test_resolver_confines(ws):
    real = os.path.realpath(os.path.join(ws, "a.txt"))
    assert _resolve_tool_path_in_workspace(ws, "a.txt") == real          # relative
    assert _resolve_tool_path_in_workspace(ws, os.path.join(ws, "a.txt")) == real  # abs inside
    outside = tempfile.mkdtemp()
    with pytest.raises(ValueError):                                       # abs outside
        _resolve_tool_path_in_workspace(ws, os.path.join(outside, "x.txt"))
    with pytest.raises(ValueError):                                       # parent escape
        _resolve_tool_path_in_workspace(ws, os.path.join("..", "..", "escape.txt"))


def test_resolver_blocks_sensitive_inside_workspace(ws):
    os.makedirs(os.path.join(ws, ".ssh"), exist_ok=True)
    with pytest.raises(ValueError):
        _resolve_tool_path_in_workspace(ws, ".ssh/authorized_keys")


# ── the central binding: the safety net ─────────────────────────────────

def test_active_binding_confines_shared_resolvers(ws):
    """ANY tool resolving paths through the shared helpers is confined while the
    binding is active, without doing anything workspace-specific itself. This is
    what stops a newly added tool from accidentally ignoring the workspace."""
    token = _active_workspace.set(ws)
    try:
        assert get_active_workspace() == ws
        assert agent_cwd() == ws
        assert _resolve_tool_path("a.txt") == os.path.realpath(os.path.join(ws, "a.txt"))
        with pytest.raises(ValueError):          # normally-allowed root, now outside ws
            _resolve_tool_path("/tmp/whatever.txt")
        assert _resolve_search_root("") == os.path.realpath(ws)
    finally:
        _active_workspace.reset(token)


def test_no_binding_uses_default_roots():
    assert get_active_workspace() is None
    assert agent_cwd() == _AGENT_WORKDIR
    with pytest.raises(ValueError):
        _resolve_tool_path("/etc/hosts")


# ── end-to-end via execute_tool_block (sets + resets the binding) ───────

@pytest.mark.asyncio
async def test_read_write_edit_confined_e2e(ws, admin):
    _, r = await execute_tool_block(_block("write_file", "note.txt\nhello"), owner="a", workspace=ws)
    assert r["exit_code"] == 0 and os.path.isfile(os.path.join(ws, "note.txt"))
    _, r = await execute_tool_block(_block("read_file", "note.txt"), owner="a", workspace=ws)
    assert r["exit_code"] == 0 and r["output"] == "hello"

    _, r = await execute_tool_block(_block("write_file", "src/components/App.js\nexport default {}"), owner="a", workspace=ws)
    assert r["exit_code"] == 0 and os.path.isfile(os.path.join(ws, "src", "components", "App.js"))

    with open(os.path.join(ws, "f.txt"), "w") as f:
        f.write("foo bar")
    _, r = await execute_tool_block(
        _block("edit_file", json.dumps({"path": "f.txt", "old_string": "foo", "new_string": "baz"})),
        owner="a", workspace=ws,
    )
    assert r["exit_code"] == 0
    with open(os.path.join(ws, "f.txt")) as f:
        assert f.read() == "baz bar"

    # outside the workspace is rejected, and nothing is created
    outside = tempfile.mkdtemp()
    of = os.path.join(outside, "secret.txt")
    with open(of, "w") as f:
        f.write("nope")
    _, r = await execute_tool_block(_block("read_file", of), owner="a", workspace=ws)
    assert r["exit_code"] == 1 and "outside the workspace" in r["error"]
    escape = os.path.join(outside, "_esc.txt")
    _, r = await execute_tool_block(_block("write_file", f"{escape}\nx"), owner="a", workspace=ws)
    assert r["exit_code"] == 1 and "outside the workspace" in r["error"]
    assert not os.path.exists(escape)


@pytest.mark.asyncio
async def test_grep_and_ls_confined_e2e(ws, admin):
    with open(os.path.join(ws, "doc.txt"), "w") as f:
        f.write("hello workspace\n")
    _, r = await execute_tool_block(_block("grep", json.dumps({"pattern": "hello"})), owner="a", workspace=ws)
    assert r["exit_code"] == 0 and "doc.txt" in r["output"]
    outside = tempfile.mkdtemp()
    _, r = await execute_tool_block(_block("grep", json.dumps({"pattern": "x", "path": outside})), owner="a", workspace=ws)
    assert r["exit_code"] == 1 and "outside the workspace" in r["error"]
    _, r = await execute_tool_block(_block("ls", ""), owner="a", workspace=ws)
    assert r["exit_code"] == 0 and "doc.txt" in r["output"]
    _, r = await execute_tool_block(_block("ls", outside), owner="a", workspace=ws)
    assert r["exit_code"] == 1 and "outside the workspace" in r["error"]


@pytest.mark.asyncio
async def test_glob_confined_e2e(ws, admin):
    """glob's literal fast-path must stay inside the workspace. A pattern with
    ../ or an absolute path outside the root would otherwise leak the existence
    and full path of arbitrary host files (an oracle), even though read_file
    blocks reading them."""
    with open(os.path.join(ws, "found.py"), "w") as f:
        f.write("x")
    _, r = await execute_tool_block(_block("glob", json.dumps({"pattern": "found.py"})), owner="a", workspace=ws)
    assert r["exit_code"] == 0 and "found.py" in r["output"]

    # a secret outside the workspace must not be discoverable via glob
    outside = tempfile.mkdtemp()
    secret = os.path.join(outside, "secret.txt")
    with open(secret, "w") as f:
        f.write("nope")
    # An escaping pattern must come back as "No files" (the not-found message),
    # not as a match that returns the file's path. The not-found message echoes
    # the pattern the model supplied, so the signal is the absence of a match,
    # not the absence of the path string.
    rel = os.path.relpath(secret, os.path.realpath(ws))
    _, r = await execute_tool_block(_block("glob", json.dumps({"pattern": rel})), owner="a", workspace=ws)
    assert r["exit_code"] == 0 and "No files" in r["output"] and secret not in r["output"]
    _, r = await execute_tool_block(_block("glob", json.dumps({"pattern": secret})), owner="a", workspace=ws)
    assert r["exit_code"] == 0 and "No files" in r["output"]


@pytest.mark.asyncio
async def test_glob_skips_sensitive_files_in_workspace(ws, admin):
    """glob must not enumerate deny-listed sensitive files that live inside the
    workspace. read_file/write_file/edit_file refuse them and grep skips them,
    so glob surfacing their paths is an enumeration oracle for prompt-injection.
    """
    with open(os.path.join(ws, "keep.py"), "w") as f:
        f.write("x")
    with open(os.path.join(ws, ".env"), "w") as f:
        f.write("AWS_SECRET=xxx")
    with open(os.path.join(ws, "id_rsa"), "w") as f:  # non-dotfile key at root
        f.write("KEY")
    os.makedirs(os.path.join(ws, ".ssh"), exist_ok=True)
    with open(os.path.join(ws, ".ssh", "authorized_keys"), "w") as f:
        f.write("ssh-rsa AAAA")

    # A recursive wildcard returns ordinary files but none of the sensitive
    # ones. The pattern "**/*" contains no secret names, so a secret basename
    # appearing in the output is a real leak (not the echoed not-found pattern).
    _, r = await execute_tool_block(_block("glob", json.dumps({"pattern": "**/*"})), owner="a", workspace=ws)
    assert r["exit_code"] == 0
    assert "keep.py" in r["output"]
    for leak in (".env", "id_rsa", "authorized_keys"):
        assert leak not in r["output"], f"glob leaked sensitive file: {leak}"

    # Directly targeting a sensitive file (literal fast-path and wildcard) must
    # come back as the not-found message, never a match with the file's path.
    for pat in (".env", "**/id_rsa", "**/authorized_keys"):
        _, r = await execute_tool_block(_block("glob", json.dumps({"pattern": pat})), owner="a", workspace=ws)
        assert r["exit_code"] == 0 and "No files" in r["output"]


@pytest.mark.asyncio
async def test_subprocess_cwd_is_workspace_e2e(ws, admin):
    """python tool runs with cwd = workspace (OS-agnostic probe)."""
    _, r = await execute_tool_block(_block("python", "import os; print(os.getcwd())"), owner="a", workspace=ws)
    assert r["exit_code"] == 0
    assert os.path.realpath(r["output"].strip()) == os.path.realpath(ws)


# ── get_workspace tool ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_workspace_tool(ws, admin):
    _, r = await execute_tool_block(_block("get_workspace", ""), owner="a", workspace=ws)
    assert r["exit_code"] == 0 and r["output"].startswith(ws) and "not sandboxed" in r["output"]
    _, r = await execute_tool_block(_block("get_workspace", ""), owner="a")  # none active
    assert r["exit_code"] == 0 and "No workspace" in r["output"]


# ── no leak across calls ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_binding_does_not_leak(ws, admin):
    await execute_tool_block(_block("ls", ""), owner="a", workspace=ws)
    assert get_active_workspace() is None


# ── tool selection: an active workspace is the file-work signal ─────────
# A vague ("low-signal") message like "look at the local project" matches no
# domain keywords, so retrieval is normally skipped. When a workspace is set it
# must still surface the file tools, otherwise the agent says it has no file
# access (the bug this guards against).

def _sent_tool_names(monkeypatch, *, workspace, message="look at the local project"):
    import asyncio
    import src.agent_loop as al

    monkeypatch.setattr(al, "get_setting", lambda key, default=None: default, raising=False)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(al, "estimate_tokens", lambda *a, **k: 10, raising=False)
    # Isolate the selection logic from owner gating (tested separately).
    monkeypatch.setattr(al, "blocked_tools_for_owner", lambda owner: set(), raising=False)

    captured = []

    async def _fake_stream(_candidates, messages, **kwargs):
        captured.append(kwargs.get("tools"))
        yield "data: " + json.dumps({"delta": "ok"}) + "\n\n"
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)

    async def _run():
        gen = al.stream_agent_loop(
            "https://api.openai.com/v1", "gpt-test",
            [{"role": "user", "content": message}],
            max_rounds=1, relevant_tools=None, owner="admin", workspace=workspace,
        )
        return [c async for c in gen]

    asyncio.run(_run())
    schemas = captured[0] or []
    return {t["function"]["name"] for t in schemas if isinstance(t, dict) and "function" in t}


def test_low_signal_with_workspace_surfaces_readonly_file_tools(monkeypatch):
    names = _sent_tool_names(monkeypatch, workspace="/tmp")
    # read-only nav tools surface so the agent can explore
    assert "read_file" in names
    assert "get_workspace" in names
    assert "grep" in names
    # write/shell tools do NOT surface on a vague message
    assert "write_file" not in names
    assert "edit_file" not in names
    assert "bash" not in names
    assert "python" not in names


def test_low_signal_without_workspace_excludes_file_tools(monkeypatch):
    names = _sent_tool_names(monkeypatch, workspace=None)
    assert "read_file" not in names
    assert "get_workspace" not in names


def test_multi_file_project_surfaces_file_tools_not_document_tools(monkeypatch, ws):
    names = _sent_tool_names(
        monkeypatch,
        workspace=ws,
        message="Create a multi-file React app in this workspace",
    )
    assert {"get_workspace", "read_file", "write_file", "edit_file", "ls"} <= names
    assert "create_document" not in names
    assert "edit_document" not in names


# ── browse route is admin-gated ─────────────────────────────────────────

def test_browse_is_admin_gated(monkeypatch):
    from fastapi import HTTPException
    import routes.workspace_routes as wr

    router = wr.setup_workspace_routes()
    browse = next(r.endpoint for r in router.routes if r.path == "/api/workspace/browse")

    monkeypatch.setattr(wr, "get_current_user", lambda req: "bob")
    monkeypatch.setattr(wr, "owner_is_admin_or_single_user", lambda owner: False)
    with pytest.raises(HTTPException) as ei:
        browse(request=object(), path="/")
    assert ei.value.status_code == 403

    monkeypatch.setattr(wr, "owner_is_admin_or_single_user", lambda owner: True)
    out = browse(request=object(), path=os.path.expanduser("~"))
    assert "dirs" in out and "path" in out
    assert all("name" in d and "path" in d for d in out["dirs"])


# ── bind-time vetting of the workspace root ─────────────────────────────

def test_vet_workspace_accepts_normal_dir(ws):
    from src.tool_execution import vet_workspace
    assert vet_workspace(ws) == os.path.realpath(ws)


def test_vet_workspace_rejects_sensitive_root(tmp_path):
    # The resolver deny-lists sensitive paths inside the workspace, but the
    # empty-path search root is the workspace itself - a sensitive root must
    # be rejected before it is bound or `ls` with no path would list it.
    from src.tool_execution import vet_workspace
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    assert vet_workspace(str(ssh_dir)) is None


def test_vet_workspace_rejects_nondir_and_empty(ws):
    from src.tool_execution import vet_workspace
    assert vet_workspace(os.path.join(ws, "a.txt")) is None  # file, not dir
    assert vet_workspace("/nonexistent/path/xyz") is None
    assert vet_workspace("") is None
    assert vet_workspace("   ") is None


def test_vet_workspace_rejects_filesystem_root():
    # Binding / would make every absolute path "inside" the workspace,
    # collapsing confinement into host-wide file access.
    from src.tool_execution import vet_workspace
    assert vet_workspace("/") is None


def test_browse_marks_root_unselectable_and_vet_endpoint(monkeypatch):
    import routes.workspace_routes as wr

    router = wr.setup_workspace_routes()
    browse = next(r.endpoint for r in router.routes if r.path == "/api/workspace/browse")
    vet = next(r.endpoint for r in router.routes if r.path == "/api/workspace/vet")

    monkeypatch.setattr(wr, "get_current_user", lambda req: "admin")
    monkeypatch.setattr(wr, "owner_is_admin_or_single_user", lambda owner: True)

    out = browse(request=object(), path="/")
    assert out["selectable"] is False
    out = browse(request=object(), path=os.path.expanduser("~"))
    assert out["selectable"] is True

    assert vet(request=object(), path="/") == {"ok": False, "path": None}
    home = os.path.realpath(os.path.expanduser("~"))
    assert vet(request=object(), path="~") == {"ok": True, "path": home}

    from fastapi import HTTPException
    monkeypatch.setattr(wr, "owner_is_admin_or_single_user", lambda owner: False)
    with pytest.raises(HTTPException) as ei:
        vet(request=object(), path="/tmp")
    assert ei.value.status_code == 403


def test_create_workspace_folder(monkeypatch, tmp_path):
    from fastapi import HTTPException
    import routes.workspace_routes as wr

    router = wr.setup_workspace_routes()
    create = next(r.endpoint for r in router.routes if r.path == "/api/workspace/create")
    monkeypatch.setattr(wr, "get_current_user", lambda req: "admin")
    monkeypatch.setattr(wr, "owner_is_admin_or_single_user", lambda owner: True)

    payload = wr.WorkspaceCreateRequest(parent=str(tmp_path), name="new-project")
    out = create(request=object(), payload=payload)
    assert out == {"path": os.path.realpath(tmp_path / "new-project")}
    assert os.path.isdir(out["path"])

    with pytest.raises(HTTPException) as existing:
        create(request=object(), payload=payload)
    assert existing.value.status_code == 409

    for name in ("../escape", ".hidden", "nested/folder", r"nested\folder", "id_rsa"):
        with pytest.raises(HTTPException) as invalid:
            create(
                request=object(),
                payload=wr.WorkspaceCreateRequest(parent=str(tmp_path), name=name),
            )
        assert invalid.value.status_code == 400
    assert not (tmp_path / "id_rsa").exists()

    monkeypatch.setattr(wr, "owner_is_admin_or_single_user", lambda owner: False)
    with pytest.raises(HTTPException) as forbidden:
        create(
            request=object(),
            payload=wr.WorkspaceCreateRequest(parent="/does/not/exist", name="x"),
        )
    assert forbidden.value.status_code == 403


def test_workspace_picker_creates_and_selects_folder():
    source = (Path(__file__).resolve().parents[1] / "static/js/workspace.js").read_text(
        encoding="utf-8"
    )
    assert 'id="workspace-create"' in source
    assert "/api/workspace/create" in source
    assert "method: 'POST'" in source
    assert "setWorkspace(data.path);" in source


def test_chat_json_body_can_bind_workspace():
    source = (Path(__file__).resolve().parents[1] / "routes/chat_routes.py").read_text(
        encoding="utf-8"
    )
    assert 'form_data.get("workspace") or (body or {}).get("workspace")' in source


def _workspace_content_endpoints(router):
    return {
        (method, route.path): route.endpoint
        for route in router.routes
        for method in route.methods
        if route.path in {
            "/api/workspace/entries",
            "/api/workspace/file",
            "/api/workspace/entry",
        }
    }


def test_workspace_content_routes_gate_before_path_resolution(monkeypatch):
    from fastapi import HTTPException
    import routes.workspace_routes as wr

    endpoints = _workspace_content_endpoints(wr.setup_workspace_routes())
    monkeypatch.setattr(wr, "get_current_user", lambda req: "bob")
    monkeypatch.setattr(wr, "owner_is_admin_or_single_user", lambda owner: False)
    monkeypatch.setattr(
        wr,
        "_resolve_workspace_entry",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("path resolution must happen after the admin gate")
        ),
    )

    calls = (
        lambda: endpoints[("GET", "/api/workspace/entries")](
            request=object(), workspace="/secret", path=""
        ),
        lambda: endpoints[("GET", "/api/workspace/file")](
            request=object(), workspace="/secret", path="file.py"
        ),
        lambda: endpoints[("PUT", "/api/workspace/file")](
            request=object(),
            payload=wr.WorkspaceFileUpdateRequest(
                workspace="/secret", path="file.py", content="x", revision="old"
            ),
        ),
        lambda: endpoints[("DELETE", "/api/workspace/entry")](
            request=object(), workspace="/secret", path="file.py"
        ),
    )
    for call in calls:
        with pytest.raises(HTTPException) as forbidden:
            call()
        assert forbidden.value.status_code == 403


def test_workspace_entries_view_and_revision_checked_update(monkeypatch, tmp_path):
    from fastapi import HTTPException
    import stat
    import routes.workspace_routes as wr

    project = tmp_path / "project"
    project.mkdir()
    (project / "src").mkdir()
    source = project / "app.py"
    source.write_bytes(b"print('one')\r\n")
    (project / ".gitignore").write_text("dist\n", encoding="utf-8")
    (project / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    (project / "id_rsa").write_text("secret\n", encoding="utf-8")
    original_mode = stat.S_IMODE(os.stat(source).st_mode)

    endpoints = _workspace_content_endpoints(wr.setup_workspace_routes())
    monkeypatch.setattr(wr, "get_current_user", lambda req: "admin")
    monkeypatch.setattr(wr, "owner_is_admin_or_single_user", lambda owner: True)

    listed = endpoints[("GET", "/api/workspace/entries")](
        request=object(), workspace=str(project), path=""
    )
    assert listed["workspace"] == os.path.realpath(project)
    assert listed["path"] == "" and listed["parent"] is None
    assert [(row["name"], row["type"]) for row in listed["entries"]] == [
        ("src", "directory"),
        (".gitignore", "file"),
        ("app.py", "file"),
    ]

    opened = endpoints[("GET", "/api/workspace/file")](
        request=object(), workspace=str(project), path="app.py"
    )
    assert opened["content"] == "print('one')\r\n"
    assert len(opened["revision"]) == 64

    saved = endpoints[("PUT", "/api/workspace/file")](
        request=object(),
        payload=wr.WorkspaceFileUpdateRequest(
            workspace=str(project),
            path="app.py",
            content="print('two')\n",
            revision=opened["revision"],
        ),
    )
    assert source.read_bytes() == b"print('two')\r\n"
    assert saved["revision"] != opened["revision"]
    assert stat.S_IMODE(os.stat(source).st_mode) == original_mode

    with pytest.raises(HTTPException) as stale:
        endpoints[("PUT", "/api/workspace/file")](
            request=object(),
            payload=wr.WorkspaceFileUpdateRequest(
                workspace=str(project),
                path="app.py",
                content="clobber\n",
                revision=opened["revision"],
            ),
        )
    assert stale.value.status_code == 409
    assert source.read_bytes() == b"print('two')\r\n"


def test_workspace_file_update_rechecks_revision_before_replace(monkeypatch, tmp_path):
    from fastapi import HTTPException
    import routes.workspace_routes as wr

    project = tmp_path / "project"
    project.mkdir()
    source = project / "app.py"
    source.write_bytes(b"print('opened')\n")

    endpoints = _workspace_content_endpoints(wr.setup_workspace_routes())
    monkeypatch.setattr(wr, "get_current_user", lambda req: "admin")
    monkeypatch.setattr(wr, "owner_is_admin_or_single_user", lambda owner: True)
    opened = endpoints[("GET", "/api/workspace/file")](
        request=object(), workspace=str(project), path="app.py"
    )

    real_fsync = wr.os.fsync
    changed = False

    def fsync_then_external_edit(fd):
        nonlocal changed
        real_fsync(fd)
        if not changed:
            changed = True
            source.write_bytes(b"print('agent edit')\n")

    monkeypatch.setattr(wr.os, "fsync", fsync_then_external_edit)
    with pytest.raises(HTTPException) as stale:
        endpoints[("PUT", "/api/workspace/file")](
            request=object(),
            payload=wr.WorkspaceFileUpdateRequest(
                workspace=str(project),
                path="app.py",
                content="print('sidebar edit')\n",
                revision=opened["revision"],
            ),
        )
    assert stale.value.status_code == 409
    assert source.read_bytes() == b"print('agent edit')\n"


def test_workspace_content_rejects_escape_sensitive_binary_and_links(monkeypatch, tmp_path):
    from fastapi import HTTPException
    import routes.workspace_routes as wr

    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("outside\n", encoding="utf-8")
    (project / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    (project / "binary.dat").write_bytes(b"a\x00b")
    link = project / "linked.py"
    linked = False
    try:
        link.symlink_to(outside)
        linked = True
    except OSError:
        pass

    endpoints = _workspace_content_endpoints(wr.setup_workspace_routes())
    monkeypatch.setattr(wr, "get_current_user", lambda req: "admin")
    monkeypatch.setattr(wr, "owner_is_admin_or_single_user", lambda owner: True)

    listed = endpoints[("GET", "/api/workspace/entries")](
        request=object(), workspace=str(project), path=""
    )
    names = {row["name"] for row in listed["entries"]}
    assert ".env" not in names
    if linked:
        assert "linked.py" not in names

    unsafe_reads = ("../outside.py", str(outside), ".env", "binary.dat")
    for path in unsafe_reads:
        with pytest.raises(HTTPException) as rejected:
            endpoints[("GET", "/api/workspace/file")](
                request=object(), workspace=str(project), path=path
            )
        assert rejected.value.status_code in {400, 415}

    if linked:
        with pytest.raises(HTTPException) as rejected_link:
            endpoints[("GET", "/api/workspace/file")](
                request=object(), workspace=str(project), path="linked.py"
            )
        assert rejected_link.value.status_code == 400

    with pytest.raises(HTTPException) as root_delete:
        endpoints[("DELETE", "/api/workspace/entry")](
            request=object(), workspace=str(project), path=""
        )
    assert root_delete.value.status_code == 400
    assert project.is_dir()


def test_workspace_file_size_limit_and_safe_delete(monkeypatch, tmp_path):
    from fastapi import HTTPException
    import routes.workspace_routes as wr

    project = tmp_path / "project"
    project.mkdir()
    doomed = project / "doomed.txt"
    doomed.write_text("delete me", encoding="utf-8")
    empty = project / "empty"
    empty.mkdir()
    nonempty = project / "nonempty"
    nonempty.mkdir()
    (nonempty / "keep.txt").write_text("keep", encoding="utf-8")
    large = project / "large.txt"
    large.write_text("12345", encoding="utf-8")

    endpoints = _workspace_content_endpoints(wr.setup_workspace_routes())
    monkeypatch.setattr(wr, "get_current_user", lambda req: "admin")
    monkeypatch.setattr(wr, "owner_is_admin_or_single_user", lambda owner: True)
    monkeypatch.setattr(wr, "_MAX_WORKSPACE_FILE_BYTES", 4)

    with pytest.raises(HTTPException) as too_large:
        endpoints[("GET", "/api/workspace/file")](
            request=object(), workspace=str(project), path="large.txt"
        )
    assert too_large.value.status_code == 413

    deleted_file = endpoints[("DELETE", "/api/workspace/entry")](
        request=object(), workspace=str(project), path="doomed.txt"
    )
    assert deleted_file["type"] == "file" and not doomed.exists()
    deleted_dir = endpoints[("DELETE", "/api/workspace/entry")](
        request=object(), workspace=str(project), path="empty"
    )
    assert deleted_dir["type"] == "directory" and not empty.exists()

    with pytest.raises(HTTPException) as not_empty:
        endpoints[("DELETE", "/api/workspace/entry")](
            request=object(), workspace=str(project), path="nonempty"
        )
    assert not_empty.value.status_code == 409
    assert nonempty.is_dir()


# ── send-time privilege gate (no path oracle for non-admins) ────────────

def test_request_workspace_gate(ws, monkeypatch):
    """Non-admin chat callers must get a uniform drop with no vetting: the
    workspace_rejected signal would otherwise reveal which host paths exist."""
    import routes.chat_routes as cr

    monkeypatch.setattr(cr, "get_current_user", lambda req: "bob")
    vet_calls = []
    import src.tool_execution as te
    real_vet = te.vet_workspace
    monkeypatch.setattr(te, "vet_workspace", lambda p: vet_calls.append(p) or real_vet(p))

    import src.tool_security as ts
    monkeypatch.setattr(ts, "owner_is_admin_or_single_user", lambda owner: False)
    # Valid and invalid paths are indistinguishable for a non-admin: both
    # drop silently, and the path never reaches the filesystem.
    assert cr._resolve_request_workspace(object(), ws) == ("", "")
    assert cr._resolve_request_workspace(object(), "/nonexistent/xyz") == ("", "")
    assert vet_calls == []

    monkeypatch.setattr(ts, "owner_is_admin_or_single_user", lambda owner: True)
    assert cr._resolve_request_workspace(object(), ws) == (os.path.realpath(ws), "")
    assert cr._resolve_request_workspace(object(), "/nonexistent/xyz") == ("", "/nonexistent/xyz")
