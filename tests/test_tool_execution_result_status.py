from types import SimpleNamespace

import pytest

import src.tool_execution as tool_execution


@pytest.mark.asyncio
async def test_tool_error_without_exit_code_is_normalized_as_failure(monkeypatch):
    async def fake_execute(*args, **kwargs):
        return "demo", {"error": "boom"}

    monkeypatch.setattr(tool_execution, "_execute_tool_block_impl", fake_execute)
    _, result = await tool_execution.execute_tool_block(SimpleNamespace(tool_type="demo"))

    assert result["exit_code"] == 1


@pytest.mark.asyncio
async def test_tool_success_without_exit_code_is_normalized_as_success(monkeypatch):
    async def fake_execute(*args, **kwargs):
        return "demo", {"output": "ok"}

    monkeypatch.setattr(tool_execution, "_execute_tool_block_impl", fake_execute)
    _, result = await tool_execution.execute_tool_block(SimpleNamespace(tool_type="demo"))

    assert result["exit_code"] == 0
