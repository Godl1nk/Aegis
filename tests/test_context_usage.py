from core.models import ChatMessage, Session
from src import context_usage
from src.model_context import estimate_tokens
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock
from pathlib import Path
import ast

import pytest
from fastapi import APIRouter, HTTPException


def session(history):
    return Session(id="test", name="Test", endpoint_url="http://local", model="qwen", history=history)


def test_history_uses_shared_estimator_and_excludes_slash(monkeypatch):
    monkeypatch.setattr(context_usage, "get_context_length_known", lambda *a: (32768, True))
    s = session([ChatMessage("user", "hello"), ChatMessage("assistant", "setup" * 1000, {"source": "slash"})])
    before = list(s.history)
    result = context_usage.session_context_usage(s)
    assert result["used_tokens"] == estimate_tokens(s.get_context_messages())
    assert result["context_length"] == 32768
    assert result["basis"] == "history"
    assert result["usage_source"] == "estimated"
    assert s.history == before


def test_unknown_window_is_not_presented_as_verified(monkeypatch):
    monkeypatch.setattr(context_usage, "get_context_length_known", lambda *a: (128000, False))
    result = context_usage.session_context_usage(session([]))
    assert result["context_length"] is None
    assert result["context_length_known"] is False
    assert result["used_tokens"] == 0


def test_stored_zero_counts_fall_back_to_history_estimate(monkeypatch):
    # Providers report 0/0 on empty, error-adjacent or uncounted turns; those
    # zeros must not pin the meter — the history estimate takes over.
    monkeypatch.setattr(context_usage, "get_context_length_known", lambda *a: (32768, True))
    s = session([ChatMessage("user", "hello there, how are you doing today?"),
                 ChatMessage("assistant", "I am doing well, thanks!", {
                     "model": "qwen", "context_tokens": 0, "context_output_tokens": 0,
                     "usage_source": "real"})])
    result = context_usage.session_context_usage(s)
    assert result["basis"] == "history"
    assert result["used_tokens"] == estimate_tokens(s.get_context_messages())
    assert result["used_tokens"] > 0


def test_stored_zero_output_with_real_input_is_kept(monkeypatch):
    monkeypatch.setattr(context_usage, "get_context_length_known", lambda *a: (32768, True))
    s = session([ChatMessage("assistant", "answer", {
        "model": "qwen", "context_tokens": 6000, "context_output_tokens": 0,
        "usage_source": "real"})])
    result = context_usage.session_context_usage(s)
    assert result["used_tokens"] == 6000
    assert result["basis"] == "request"


def test_compact_trigger_fields_follow_settings(monkeypatch):
    # Defaults: 85% gate, no token cap — the meter contract.
    monkeypatch.setattr(context_usage, "get_context_length_known", lambda *a: (32768, True))
    s = session([ChatMessage("user", "hello")])
    result = context_usage.session_context_usage(s)
    assert result["compact_threshold"] == 0.85
    assert result["compact_token_cap"] == 0


def test_reload_uses_latest_request_not_cumulative_billing(monkeypatch):
    monkeypatch.setattr(context_usage, "get_context_length_known", lambda *a: (32768, True))
    s = session([ChatMessage("assistant", "answer", {
        "model": "qwen", "context_tokens": 6000, "context_output_tokens": 500,
        "input_tokens": 50000, "output_tokens": 10000, "usage_source": "real",
    })])
    result = context_usage.session_context_usage(s)
    assert result["used_tokens"] == 6500
    assert result["basis"] == "request"
    assert result["usage_source"] == "real"
    s.model = "other"
    assert context_usage.session_context_usage(s)["basis"] == "history"
    s.model = "qwen"
    s.history.append(ChatMessage("user", "new question"))
    assert context_usage.session_context_usage(s)["basis"] == "history"


def test_compaction_snapshot_uses_reduced_history(monkeypatch):
    monkeypatch.setattr(context_usage, "get_context_length_known", lambda *a: (32768, True))
    s = session([ChatMessage("system", "summary", {"compacted": True}), ChatMessage("user", "continue")])
    result = context_usage.session_context_usage(s)
    assert result["compacted"] is True
    assert result["compact_threshold"] == 0.85
    assert result["used_tokens"] == estimate_tokens(s.get_context_messages())


@pytest.mark.asyncio
async def test_context_route_checks_owner_and_offloads_probe(monkeypatch):
    from routes import session_routes
    monkeypatch.setattr(session_routes, "router", APIRouter(prefix="/api"))
    manager = MagicMock()
    manager.get_session.return_value = session([])
    router = session_routes.setup_session_routes(manager, {})
    handler = next(r.endpoint for r in router.routes if r.path.endswith('/context_info'))
    request = SimpleNamespace()
    events = []
    loop_thread = threading.get_ident()

    def allow(*args):
        events.append('owner')

    def probe(s):
        assert threading.get_ident() != loop_thread
        events.append('probe')
        return {'used_tokens': 1, 'context_length': 10000}

    monkeypatch.setattr(session_routes, '_verify_session_owner', allow)
    monkeypatch.setattr(context_usage, 'session_context_usage', probe)
    result = await handler(request, 'test')
    assert result['used_tokens'] == 1
    assert events == ['owner', 'probe']

    def deny(*args):
        raise HTTPException(404, 'not found')

    monkeypatch.setattr(session_routes, '_verify_session_owner', deny)
    events.clear()
    with pytest.raises(HTTPException) as exc:
        await handler(request, 'other')
    assert exc.value.status_code == 404
    assert events == []


def test_chat_route_forwards_agent_context_events():
    source = (Path(__file__).resolve().parents[1] / 'routes/chat_routes.py').read_text(encoding='utf-8')
    tree = ast.parse(source)
    branches = [node for node in ast.walk(tree) if isinstance(node, ast.If)
                and isinstance(node.test, ast.Compare)
                and any(isinstance(op, ast.In) for op in node.test.ops)
                and 'context_usage' in ast.unparse(node.test)]
    assert branches, 'Agent context events must pass through the route SSE allow-list'
    assert any(isinstance(node, ast.Yield) and isinstance(node.value, ast.Name)
               and node.value.id == 'chunk' for branch in branches
               for statement in branch.body for node in ast.walk(statement))
