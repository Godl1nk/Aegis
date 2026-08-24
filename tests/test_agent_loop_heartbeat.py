"""Unit tests for the tool execution keep-alive heartbeat in stream_agent_loop.
Checks that when a tool execution takes longer than `_TOOL_PROGRESS_TIMEOUT`,
the generator yields periodic `: heartbeat\\n\\n` comments to keep the SSE connection alive.
"""

import asyncio
import json
import pytest

import src.agent_loop as al


def _patch_common(monkeypatch, exec_sleep_time=0.4):
    monkeypatch.setattr(al, "get_setting", lambda key, default=None: default, raising=False)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(al, "estimate_tokens", lambda *a, **k: 10, raising=False)
    monkeypatch.setattr(al, "_TOOL_PROGRESS_TIMEOUT", 0.1)

    async def _fake_exec(block, *a, **k):
        # Simulate a long-running tool execution
        await asyncio.sleep(exec_sleep_time)
        return ("bash", {"output": "ok", "exit_code": 0})
    monkeypatch.setattr(al, "execute_tool_block", _fake_exec, raising=False)


@pytest.mark.asyncio
async def test_tool_execution_yields_heartbeats(monkeypatch):
    _patch_common(monkeypatch, exec_sleep_time=0.35)

    async def _fake_stream(_candidates, messages, **kwargs):
        # Round 1: Ask LLM to run a tool
        yield f'data: {json.dumps({"delta": "```bash\nsleep 1\n```"})}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)

    gen = al.stream_agent_loop(
        "http://x/v1", "m",
        [{"role": "user", "content": "do a slow task"}],
        max_rounds=1,
        relevant_tools={"bash"},
    )

    chunks = []
    async for chunk in gen:
        chunks.append(chunk)

    # We expect at least one heartbeat to be yielded because timeout is 0.1s and tool execution is 0.35s.
    heartbeats = [c for c in chunks if c == ": heartbeat\n\n"]
    assert len(heartbeats) >= 2, f"Expected at least 2 heartbeats, got: {chunks}"


@pytest.mark.asyncio
async def test_cancelling_agent_stream_cancels_running_tool(monkeypatch):
    monkeypatch.setattr(al, "get_setting", lambda key, default=None: default, raising=False)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(al, "estimate_tokens", lambda *a, **k: 10, raising=False)
    monkeypatch.setattr(al, "_TOOL_PROGRESS_TIMEOUT", 0.1)

    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def _fake_exec(block, *args, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def _fake_stream(_candidates, messages, **kwargs):
        yield f'data: {json.dumps({"delta": "```bash\nsleep 60\n```"})}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "execute_tool_block", _fake_exec, raising=False)
    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)

    gen = al.stream_agent_loop(
        "http://x/v1", "m",
        [{"role": "user", "content": "run a long task"}],
        max_rounds=1,
        relevant_tools={"bash"},
    )

    consumer = asyncio.create_task(_consume(gen))
    await asyncio.sleep(0)
    await asyncio.wait_for(started.wait(), timeout=5)
    consumer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consumer
    await asyncio.wait_for(cancelled.wait(), timeout=5)


async def _consume(gen):
    async for _ in gen:
        pass
