"""Unattended task shell tools are explicit opt-in.

New tasks run without bash/python/write_file/edit_file unless allow_shell
is set; legacy rows (NULL) keep the old behaviour. Covers the composer,
the manage_tasks tool create/edit paths, and the calendar auto-extract
create-only policy.
"""
import json
import sys
import types
from types import SimpleNamespace

import pytest

from src.task_scheduler import (
    compose_task_relevant_tools,
    TASK_DEFAULT_SHELL_TOOLS,
    TASK_SHELL_WRITE_TOOLS,
)


@pytest.fixture
def isolated_db(monkeypatch):
    """Hermetic core.database: other test modules (e.g. companion) replace
    sys.modules['core.database'] with stubs and never restore them, so
    resolving the real module here is order-dependent. Install a fake with a
    kwarg-honoring ScheduledTask instead; do_manage_tasks only needs
    SessionLocal + the model constructor."""
    real_entry = sys.modules.get("core.database")

    class _Col:
        """Column stand-in: class access returns self (so == and .desc()
        chains never raise); missing instance attrs read as None."""

        def __get__(self, obj, cls=None):
            return self if obj is None else None

        def __eq__(self, other):
            return True

        def desc(self):
            return self

    class _Task:
        # Class-level columns so query filters/order_by don't raise.
        id = owner = created_at = _Col()

        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

        def __getattr__(self, name):
            # Only reached when normal lookup fails (kwargs not passed):
            # unread columns read as None, like a fresh NULL row.
            return None

    fake_mod = types.ModuleType("core.database")
    fake_db = _FakeDB()
    fake_mod.SessionLocal = lambda: fake_db
    fake_mod.ScheduledTask = _Task
    sys.modules["core.database"] = fake_mod
    try:
        yield fake_db
    finally:
        if real_entry is not None:
            sys.modules["core.database"] = real_entry
        else:
            sys.modules.pop("core.database", None)


def test_compose_legacy_none_keeps_shell():
    tools = compose_task_relevant_tools({"read_file"}, set(), set(), None)
    assert TASK_SHELL_WRITE_TOOLS <= tools


def test_compose_true_keeps_shell():
    tools = compose_task_relevant_tools(set(), set(), set(), True)
    assert TASK_SHELL_WRITE_TOOLS <= tools


def test_compose_false_strips_shell_writes_keeps_reads():
    tools = compose_task_relevant_tools(
        {"read_file", "bash", "send_email"}, {"manage_memory"}, set(), False)
    assert not (tools & TASK_SHELL_WRITE_TOOLS)
    assert {"read_file", "grep", "ls", "send_email", "manage_memory"} <= tools


def test_compose_respects_disabled_tools():
    tools = compose_task_relevant_tools(set(), set(), {"bash"}, True)
    assert "bash" not in tools
    assert "python" in tools


class _FakeQuery:
    def __init__(self, store):
        self._store = store

    def filter(self, *a):
        return self

    def order_by(self, *a):
        return self

    def all(self):
        return list(self._store)

    def first(self):
        return self._store[0] if self._store else None


class _FakeDB:
    def __init__(self):
        self.tasks = []

    def query(self, model):
        return _FakeQuery(self.tasks)

    def add(self, t):
        self.tasks.append(t)

    def commit(self):
        pass

    def close(self):
        pass


def _create(extra=None, owner="admin"):
    import asyncio
    from src.tools.system import do_manage_tasks
    args = {"action": "create", "prompt": "summarize inbox",
            "schedule": "daily", "scheduled_time": "09:00"}
    args.update(extra or {})
    out = asyncio.run(do_manage_tasks(json.dumps(args), owner=owner))
    assert out["exit_code"] == 0, out
    return out


def test_tool_create_defaults_shell_off(isolated_db):
    _create()
    assert isolated_db.tasks[-1].allow_shell is False


def test_function_equals_false_does_not_enable_task_shell(isolated_db):
    import asyncio
    from src.agent_tools import parse_tool_blocks
    from src.tools.system import do_manage_tasks

    raw = ('<function=manage_tasks><parameter=action>create</parameter>'
           '<parameter=prompt>summarize inbox</parameter>'
           '<parameter=schedule>daily</parameter>'
           '<parameter=allow_shell>false</parameter></function>')
    block, = parse_tool_blocks(raw)
    out = asyncio.run(do_manage_tasks(block.content, owner="admin"))
    assert out["exit_code"] == 0, out
    assert isolated_db.tasks[-1].allow_shell is False


def test_tool_create_shell_opt_in(isolated_db):
    _create({"allow_shell": True})
    assert isolated_db.tasks[-1].allow_shell is True


def test_tool_create_shell_flag_roundtrip(isolated_db):
    import asyncio
    from src.tools.system import do_manage_tasks
    _create()
    assert isolated_db.tasks[-1].allow_shell is False
    task_id = isolated_db.tasks[-1].id
    out = asyncio.run(do_manage_tasks(
        json.dumps({"action": "edit", "task_id": task_id, "allow_shell": True}),
        owner="admin"))
    assert out["exit_code"] == 0, out
    assert isolated_db.tasks[-1].allow_shell is True
    listed = asyncio.run(do_manage_tasks(json.dumps({"action": "list"}), owner="admin"))
    assert listed["tasks"][-1]["allow_shell"] is True


def test_cal_auto_policy_create_only():
    from routes.email_pollers import _cal_auto_op_allowed
    assert _cal_auto_op_allowed("create") is True
    assert _cal_auto_op_allowed("Create") is True
    for op in ("update", "cancel", "noop", "", None, "delete", "CREATE TABLE"):
        assert _cal_auto_op_allowed(op) is False, op


def test_middleware_direct_loopback():
    from core.middleware import _is_direct_loopback

    def req(host, headers=None):
        return SimpleNamespace(client=SimpleNamespace(host=host), headers=headers or {})

    assert _is_direct_loopback(req("127.0.0.1")) is True
    assert _is_direct_loopback(req("::1")) is True
    assert _is_direct_loopback(req("10.1.2.3")) is False
    assert _is_direct_loopback(req("tailscale.example")) is False
    # Tunnel-forwarded remotes present as loopback but carry proxy headers.
    assert _is_direct_loopback(req("127.0.0.1", {"x-forwarded-for": "1.2.3.4"})) is False
    assert _is_direct_loopback(req("127.0.0.1", {"cf-connecting-ip": "1.2.3.4"})) is False
    assert _is_direct_loopback(SimpleNamespace(client=None, headers={})) is False
