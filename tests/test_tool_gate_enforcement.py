"""Post-external-context integrity gate enforcement tests.

Gate ON here (conftest disables it suite-wide via ODYSSEUS_TOOL_GATE=off;
these tests opt back in). Covers: reads stay free after arming, effectful
tools deny, dialog approval (once/session) resumes execution, headless runs
fail closed without waiting, timeout denies fast.
"""
import asyncio
import json

import src.agent_loop as al
from src import command_approval
from src.tool_capabilities import ToolRunSecurityContext


def _types(chunks):
    out = []
    for c in chunks:
        if c.startswith("data: ") and not c.startswith("data: [DONE]"):
            try:
                parsed = json.loads(c[6:])
            except Exception:
                continue
            if isinstance(parsed, dict):
                out.append(parsed)
    return out


def _patch_common(monkeypatch, calls):
    monkeypatch.setenv("ODYSSEUS_TOOL_GATE", "on")
    monkeypatch.setattr(al, "get_setting", lambda key, default=None: default, raising=False)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(al, "estimate_tokens", lambda *a, **k: 10, raising=False)

    async def _fake_exec(block, *a, **k):
        calls.append(block.tool_type)
        if block.tool_type == "read_file":
            return ("read_file", {"output": "file contents here", "exit_code": 0})
        if block.tool_type == "edit_file":
            return ("edit_file", {"output": "edited ok", "exit_code": 0})
        return (block.tool_type, {"output": "ok", "exit_code": 0})

    monkeypatch.setattr(al, "execute_tool_block", _fake_exec, raising=False)
    monkeypatch.setattr(al, "_human_stream_active", lambda sid: True, raising=False)
    al._SESSION_GATE_STATE.clear()


def _round_texts(texts):
    state = {"i": 0}

    async def _fake_stream(_candidates, messages, **kwargs):
        text = texts[min(state["i"], len(texts) - 1)]
        state["i"] += 1
        yield f'data: {json.dumps({"delta": text})}\n\n'
        yield "data: [DONE]\n\n"

    return _fake_stream


def _run_loop(monkeypatch, texts, session_id="gate-sess", max_rounds=4):
    monkeypatch.setattr(al, "stream_llm_with_fallback", _round_texts(texts), raising=False)
    gen = al.stream_agent_loop(
        "http://x/v1", "m",
        [{"role": "user", "content": "work on the file"}],
        max_rounds=max_rounds,
        session_id=session_id,
        relevant_tools={"read_file", "edit_file"},
    )

    async def _run():
        return [c async for c in gen]

    return _types(asyncio.run(_run()))


def _pump_with_approval(monkeypatch, texts, choice, session_id="gate-sess-ap"):
    """Drive the loop while resolving the first gate dialog with `choice`."""
    monkeypatch.setattr(al, "stream_llm_with_fallback", _round_texts(texts), raising=False)
    gen = al.stream_agent_loop(
        "http://x/v1", "m",
        [{"role": "user", "content": "work on the file"}],
        max_rounds=4,
        session_id=session_id,
        relevant_tools={"read_file", "edit_file"},
    )
    chunks = []

    async def _pump():
        async for c in gen:
            chunks.append(c)

    async def _main():
        task = asyncio.create_task(_pump())
        approval_id = None
        for _ in range(400):
            await asyncio.sleep(0.02)
            for c in chunks:
                if c.startswith("data: ") and not c.startswith("data: [DONE]"):
                    try:
                        e = json.loads(c[6:])
                    except Exception:
                        continue
                    if e.get("type") == "approval_request" and e.get("approval_id"):
                        approval_id = e["approval_id"]
                        break
            if approval_id or task.done():
                break
        assert approval_id, "expected an approval_request event"
        assert command_approval.resolve_approval(approval_id, choice, owner="")
        await asyncio.wait_for(task, timeout=30)

    asyncio.run(_main())
    return _types(chunks)


READ = "```read_file\n{\"path\": \"x.py\"}\n```"
EDIT = "```edit_file\n{\"path\": \"x.py\", \"old\": \"a\", \"new\": \"b\"}\n```"
DONE = "All done."


def test_unit_reads_free_writes_denied_after_arm():
    ctx = ToolRunSecurityContext()
    assert ctx.decision_for("read_file", "").allowed
    assert ctx.decision_for("edit_file", "").allowed  # nothing seen yet
    ctx.observe_tool_result("read_file", {"output": "data", "exit_code": 0}, "")
    assert ctx.external_untrusted_context_seen
    assert ctx.decision_for("grep", "").allowed  # reads stay free
    denied = ctx.decision_for("edit_file", "")
    assert not denied.allowed
    assert "edit_file" in (denied.reason or "")
    # bypass lifts everything for the run
    ctx.approval_gate_bypassed = True
    assert ctx.decision_for("send_email", "").allowed


def test_unit_approval_placeholders_do_not_arm():
    ctx = ToolRunSecurityContext()
    ctx.observe_tool_result("edit_file", {"approval_required": True, "error": "x"}, "")
    assert not ctx.external_untrusted_context_seen


def test_unit_unknown_tools_fail_high():
    ctx = ToolRunSecurityContext()
    ctx.observe_tool_result("web_search", {"output": "hits", "exit_code": 0}, "")
    denied = ctx.decision_for("mystery_tool_xyz", "")
    assert not denied.allowed


def test_read_then_edit_prompts_and_once_resumes(monkeypatch):
    calls = []
    _patch_common(monkeypatch, calls)
    events = _pump_with_approval(monkeypatch, [READ, EDIT, DONE], "once")
    assert any(e.get("type") == "approval_request" for e in events), events
    assert calls == ["read_file", "edit_file"], calls


def test_session_choice_bypasses_rest_of_chat(monkeypatch):
    calls = []
    _patch_common(monkeypatch, calls)
    events = _pump_with_approval(monkeypatch, [READ, EDIT, DONE], "session",
                                 session_id="gate-sess-persist")
    assert any(e.get("type") == "approval_request" for e in events)
    # Second run in the same session: no new dialog, edit runs straight through.
    calls.clear()
    events2 = _run_loop(monkeypatch, [READ, EDIT, DONE], session_id="gate-sess-persist")
    assert not any(e.get("type") == "approval_request" for e in events2), events2
    assert "edit_file" in calls, calls


def test_headless_fails_closed_without_wait(monkeypatch):
    calls = []
    _patch_common(monkeypatch, calls)
    monkeypatch.setattr(al, "_human_stream_active", lambda sid: False, raising=False)
    import time
    start = time.time()
    events = _run_loop(monkeypatch, [READ, EDIT, DONE], session_id="gate-sess-headless")
    assert time.time() - start < 20, "headless deny must not wait for a dialog"
    assert not any(e.get("type") == "approval_request" for e in events)
    assert calls == ["read_file"], calls  # edit never executed
    outputs = [e for e in events if e.get("type") == "tool_output"]
    assert any("authoriz" in json.dumps(o).lower() for o in outputs), events


def test_dialog_timeout_denies_fast(monkeypatch):
    calls = []
    _patch_common(monkeypatch, calls)
    monkeypatch.setattr(command_approval, "_get_approval_timeout", lambda: 0)
    events = _run_loop(monkeypatch, [READ, EDIT, DONE], session_id="gate-sess-timeout")
    assert any(e.get("type") == "approval_request" for e in events)
    assert calls == ["read_file"], calls


def test_gate_disabled_sends_everything(monkeypatch):
    calls = []
    _patch_common(monkeypatch, calls)
    monkeypatch.setenv("ODYSSEUS_TOOL_GATE", "off")
    events = _run_loop(monkeypatch, [READ, EDIT, DONE], session_id="gate-sess-off")
    assert not any(e.get("type") == "approval_request" for e in events)
    assert calls == ["read_file", "edit_file"], calls
