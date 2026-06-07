import json
import sys
from types import SimpleNamespace

_endpoint_resolver = sys.modules.get("src.endpoint_resolver")
if _endpoint_resolver is not None and not getattr(_endpoint_resolver, "__file__", None):
    sys.modules.pop("src.endpoint_resolver", None)
    sys.modules.pop("routes.model_routes", None)
    sys.modules.pop("routes.chat_routes", None)

from routes import chat_routes


class _FakeQuery:
    def __init__(self, rows):
        self.rows = rows

    def filter(self, *conditions):
        return self

    def all(self):
        return list(self.rows)


class _FakeDb:
    def __init__(self, rows):
        self.rows = rows
        self.closed = False

    def query(self, model):
        return _FakeQuery(self.rows)

    def close(self):
        self.closed = True


def _session(model="qwen3.5:latest", endpoint_url="http://localhost:11434/v1/chat/completions"):
    return SimpleNamespace(model=model, endpoint_url=endpoint_url)


def _message(role, metadata=None):
    return SimpleNamespace(role=role, metadata=metadata or {})


def _endpoint(base_url, model_type="image", models=None):
    cached_models = None if models is None else json.dumps(models)
    return SimpleNamespace(
        base_url=base_url,
        model_type=model_type,
        is_enabled=True,
        cached_models=cached_models,
    )


def test_image_model_prefix_routes_to_image_generation_without_endpoint_lookup(monkeypatch):
    def fail_if_called():
        raise AssertionError("prefixed image models should not need a DB lookup")

    monkeypatch.setattr(chat_routes, "SessionLocal", fail_if_called)

    assert chat_routes._is_image_generation_session(_session(model="dall-e-3"))


def test_image_endpoint_does_not_catch_text_model_on_different_path(monkeypatch):
    db = _FakeDb([
        _endpoint("http://localhost:11434/v1/images", models=["sdxl-local"]),
    ])
    monkeypatch.setattr(chat_routes, "SessionLocal", lambda: db)

    assert not chat_routes._is_image_generation_session(_session())
    assert db.closed


def test_image_endpoint_cache_must_contain_selected_model(monkeypatch):
    db = _FakeDb([
        _endpoint("http://localhost:11434/v1", models=["sdxl-local"]),
    ])
    monkeypatch.setattr(chat_routes, "SessionLocal", lambda: db)

    assert not chat_routes._is_image_generation_session(_session(model="qwen3.5:latest"))


def test_matching_image_endpoint_routes_selected_image_model(monkeypatch):
    db = _FakeDb([
        _endpoint("http://localhost:11434/v1", models=["sdxl-local"]),
    ])
    monkeypatch.setattr(chat_routes, "SessionLocal", lambda: db)

    assert chat_routes._is_image_generation_session(_session(model="sdxl-local"))


def test_sticky_image_followup_routes_edit_intent_to_image():
    assert chat_routes._looks_like_image_turn("the words on top r abit off", [], True)
    assert chat_routes._looks_like_image_turn("make it brighter", [], True)
    assert chat_routes._looks_like_image_turn("another variation", [], True)


def test_sticky_image_followup_returns_plain_chat_for_non_image_text():
    assert not chat_routes._looks_like_image_turn("what is 2+2?", [], True)
    assert not chat_routes._looks_like_image_turn("make a table", [], True)
    assert not chat_routes._looks_like_image_turn("summarize this", [], True)
    assert not chat_routes._looks_like_image_turn("what do you think of it?", [], True)


def test_image_intent_is_not_sticky_when_no_image_context():
    assert not chat_routes._looks_like_image_turn("the words on top r abit off", [], False)
    assert chat_routes._looks_like_image_turn("draw a mountain logo", [], False)


def test_last_image_generation_event_preserves_previous_chat_model():
    sess = _session(model="gpt-image-1")
    sess.history = [
        _message("assistant", {"tool_events": [{
            "tool": "generate_image",
            "image_url": "/api/generated-image/abc.png",
            "image_previous_model": "gpt-4.1",
            "image_previous_endpoint_url": "https://api.openai.com/v1/chat/completions",
        }]}),
    ]

    ev = chat_routes._last_image_generation_event(sess)

    assert ev["image_previous_model"] == "gpt-4.1"
    assert ev["image_previous_endpoint_url"].endswith("/chat/completions")


def test_collect_image_context_uses_previous_image_for_referential_edit():
    messages = [
        {"role": "assistant", "content": "", "metadata": {"tool_events": [{
            "tool": "generate_image",
            "image_url": "/api/generated-image/abc.png",
        }]}},
        {"role": "user", "content": "the words on top r abit off", "metadata": {}},
    ]

    assert chat_routes._collect_image_context(messages, "the words on top r abit off") == ["/api/generated-image/abc.png"]


def test_collect_image_context_does_not_reuse_previous_image_for_fresh_request():
    messages = [
        {"role": "assistant", "content": "", "metadata": {"tool_events": [{
            "tool": "generate_image",
            "image_url": "/api/generated-image/abc.png",
        }]}},
        {"role": "user", "content": "draw a clean mountain logo", "metadata": {}},
    ]

    assert chat_routes._collect_image_context(messages, "draw a clean mountain logo") == []


def test_collect_image_context_always_uses_current_upload():
    messages = [
        {"role": "user", "content": "edit this", "metadata": {"attachments": [{
            "id": "up1",
            "mime": "image/png",
        }]}},
    ]

    assert chat_routes._collect_image_context(messages, "edit this") == ["upload:up1"]
