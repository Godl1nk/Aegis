"""Tasks can opt into execution while Aegis has foreground activity."""

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from tests.helpers.import_state import clear_fake_database_modules

clear_fake_database_modules()

import core.database as cdb
import routes.task_routes as task_routes
import src.interactive_gate as interactive_gate
from core.database import ScheduledTask, TaskRun
from src.task_scheduler import TaskScheduler
from src.tools.system import do_manage_tasks


_REAL_DATABASE_ATTRS = {
    "Base": cdb.Base,
    "SessionLocal": cdb.SessionLocal,
    "ScheduledTask": ScheduledTask,
    "TaskRun": TaskRun,
}
if hasattr(cdb, "engine"):
    _REAL_DATABASE_ATTRS["engine"] = cdb.engine


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.fixture()
def task_db(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "core.database", cdb)
    for attr, value in _REAL_DATABASE_ATTRS.items():
        monkeypatch.setattr(cdb, attr, value, raising=False)
    engine = create_engine(
        f"sqlite:///{tmp_path / 'tasks.db'}",
        connect_args={"check_same_thread": False},
        poolclass=NullPool,
    )
    cdb.Base.metadata.create_all(engine)
    testing_session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(cdb, "SessionLocal", testing_session)
    monkeypatch.setattr(task_routes, "SessionLocal", testing_session)
    return testing_session


def _seed_task(session_factory, task_id, *, run_when_busy):
    db = session_factory()
    try:
        db.add(ScheduledTask(
            id=task_id,
            owner="alice",
            name=task_id,
            prompt="{}",
            task_type="action",
            action="summarize_emails",
            trigger_type="event",
            status="active",
            output_target="session",
            next_run=_utcnow() - timedelta(minutes=1),
            run_when_busy=run_when_busy,
        ))
        db.commit()
    finally:
        db.close()


def _endpoint(method, path):
    router = task_routes.setup_task_routes(MagicMock())
    for route in router.routes:
        if getattr(route, "path", None) == path and method in getattr(route, "methods", set()):
            return route.endpoint
    raise RuntimeError(f"{method} {path} not found")


@pytest.mark.asyncio
async def test_task_api_roundtrips_run_when_busy(task_db):
    create_task = _endpoint("POST", "/api/tasks")
    update_task = _endpoint("PUT", "/api/tasks/{task_id}")
    request = SimpleNamespace(state=SimpleNamespace(current_user="alice"))

    created = await create_task(request, task_routes.TaskCreate(
        name="Always-on summary",
        prompt="Summarize the inbox",
        schedule="daily",
        trigger_type="schedule",
        allow_shell=True,
        run_when_busy=True,
    ))
    assert created["allow_shell"] is True
    assert created["run_when_busy"] is True

    updated = await update_task(
        request,
        created["id"],
        task_routes.TaskUpdate(allow_shell=False, run_when_busy=False),
    )
    assert updated["allow_shell"] is False
    assert updated["run_when_busy"] is False


@pytest.mark.asyncio
async def test_manage_tasks_tool_roundtrips_run_when_busy(task_db):
    created = await do_manage_tasks(json.dumps({
        "action": "create",
        "name": "Always-on tool task",
        "prompt": "Summarize the inbox",
        "schedule": "daily",
        "run_when_busy": True,
    }), owner="alice")
    assert created["exit_code"] == 0

    listed = await do_manage_tasks(json.dumps({"action": "list"}), owner="alice")
    task = next(item for item in listed["tasks"] if item["id"] == created["task_id"])
    assert task["run_when_busy"] is True

    edited = await do_manage_tasks(json.dumps({
        "action": "edit",
        "task_id": created["task_id"],
        "run_when_busy": False,
    }), owner="alice")
    assert edited["exit_code"] == 0

    listed = await do_manage_tasks(json.dumps({"action": "list"}), owner="alice")
    task = next(item for item in listed["tasks"] if item["id"] == created["task_id"])
    assert task["run_when_busy"] is False


@pytest.mark.asyncio
async def test_due_busy_task_dispatches_while_idle_only_task_is_deferred(
    monkeypatch, task_db
):
    _seed_task(task_db, "busy-ok", run_when_busy=True)
    _seed_task(task_db, "idle-only", run_when_busy=False)
    monkeypatch.setattr(interactive_gate, "has_foreground_activity", lambda: True)

    scheduler = TaskScheduler.__new__(TaskScheduler)
    scheduler._executing = set()
    scheduler._executing_lock = asyncio.Lock()
    dispatched = []

    def fake_create_task(coro):
        dispatched.append(coro.cr_frame.f_locals["task_id"])
        coro.close()
        return SimpleNamespace()

    monkeypatch.setattr("src.task_scheduler.asyncio.create_task", fake_create_task)
    await scheduler._check_due_tasks()

    assert dispatched == ["busy-ok"]
    assert scheduler._executing == {"busy-ok"}
    db = task_db()
    try:
        idle_only = db.query(ScheduledTask).filter(ScheduledTask.id == "idle-only").first()
        assert idle_only.next_run >= _utcnow() + timedelta(minutes=14)
    finally:
        db.close()


@pytest.mark.asyncio
async def test_busy_task_skips_idle_gate_and_completes(monkeypatch, task_db):
    _seed_task(task_db, "busy-ok", run_when_busy=True)
    db = task_db()
    try:
        db.add(TaskRun(id="run-1", task_id="busy-ok", status="queued"))
        db.commit()
    finally:
        db.close()

    async def fail_if_waited(*_args, **_kwargs):
        raise AssertionError("run_when_busy task waited for foreground idle")

    monkeypatch.setattr(interactive_gate, "wait_for_interactive_quiet", fail_if_waited)
    scheduler = TaskScheduler.__new__(TaskScheduler)
    scheduler._task_defer_counts = {}
    scheduler._last_run_model = None
    scheduler._task_handles = {}
    scheduler.add_notification = lambda *_args, **_kwargs: None
    scheduler._log_to_assistant = lambda *_args, **_kwargs: None

    async def execute_action(_task, *, run_id):
        return "done", True

    async def deliver_result(*_args, **_kwargs):
        return None

    scheduler._execute_action = execute_action
    scheduler._deliver_task_result = deliver_result
    await scheduler._execute_task_locked(
        "busy-ok",
        "run-1",
        gate_foreground=True,
        release_executing=False,
    )

    db = task_db()
    try:
        run = db.query(TaskRun).filter(TaskRun.id == "run-1").first()
        assert run.status == "success"
        assert run.result == "done"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_foreground_stop_leaves_busy_tasks_running(monkeypatch, task_db):
    _seed_task(task_db, "busy-ok", run_when_busy=True)
    _seed_task(task_db, "idle-only", run_when_busy=False)

    class Handle:
        def __init__(self):
            self.cancelled = False

        def done(self):
            return False

        def cancel(self):
            self.cancelled = True

    busy_handle = Handle()
    idle_handle = Handle()
    scheduler = TaskScheduler.__new__(TaskScheduler)
    scheduler._executing = {"busy-ok", "idle-only"}
    scheduler._executing_lock = asyncio.Lock()
    scheduler._task_handles = {
        "busy-ok": busy_handle,
        "idle-only": idle_handle,
    }
    aborted = []
    scheduler._mark_run_aborted = lambda task_id: aborted.append(task_id) or True

    await scheduler.stop_background_tasks_for_foreground()

    assert busy_handle.cancelled is False
    assert idle_handle.cancelled is True
    assert aborted == ["idle-only"]


def test_run_when_busy_migration_defaults_existing_rows_off(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE scheduled_tasks (id VARCHAR PRIMARY KEY)"))
        conn.execute(text("INSERT INTO scheduled_tasks (id) VALUES ('legacy')"))
    monkeypatch.setattr(cdb, "engine", engine)

    cdb._migrate_add_task_run_when_busy_column()
    cdb._migrate_add_task_run_when_busy_column()

    with engine.connect() as conn:
        columns = {row[1]: row for row in conn.execute(text(
            "PRAGMA table_info(scheduled_tasks)"
        ))}
        value = conn.execute(text(
            "SELECT run_when_busy FROM scheduled_tasks WHERE id='legacy'"
        )).scalar_one()
    assert "run_when_busy" in columns
    assert columns["run_when_busy"][3] == 1
    assert value == 0


def test_task_form_exposes_and_submits_run_when_busy():
    source = (Path(__file__).parents[1] / "static" / "js" / "tasks.js").read_text(
        encoding="utf-8"
    )
    assert 'id="task-form-run-busy"' in source
    assert "existing?.run_when_busy ? 'checked' : ''" in source
    assert "payload.run_when_busy = !!runBusyEl.checked" in source
