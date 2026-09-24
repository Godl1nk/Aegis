"""Per-endpoint API method option (auto/chat_completions/responses/anthropic/ollama).

Covers validation, provider resolution, method-aware URL/header building,
the generic Responses payload/parse helpers, and the api_method DB migration.
"""
import sqlite3

import pytest

from src import endpoint_resolver as er
from src import llm_core
from routes import model_routes


@pytest.fixture
def no_dns(monkeypatch):
    """Neutralize resolve_url so URL-building tests never touch DNS/Tailscale."""
    monkeypatch.setattr(er, "resolve_url", lambda u: u)


class _Ep:
    def __init__(self, api_method=None):
        self.api_method = api_method


def test_normalize_api_method():
    assert model_routes._normalize_api_method(None) == "auto"
    assert model_routes._normalize_api_method("") == "auto"
    assert model_routes._normalize_api_method("RESPONSES") == "responses"
    assert model_routes._normalize_api_method(" chat_completions ") == "chat_completions"
    assert model_routes._normalize_api_method("anthropic") == "anthropic"
    assert model_routes._normalize_api_method("ollama") == "ollama"
    assert model_routes._normalize_api_method("graphql") == "auto"
    assert model_routes._endpoint_api_method(_Ep(None)) == "auto"
    assert model_routes._endpoint_api_method(_Ep("responses")) == "responses"
    assert model_routes._endpoint_api_method(object()) == "auto"


def test_resolve_provider_mapping():
    assert llm_core._resolve_provider("https://x.example/v1", "responses") == "responses"
    assert llm_core._resolve_provider("https://x.example/v1", "anthropic") == "anthropic"
    assert llm_core._resolve_provider("https://x.example/v1", "ollama") == "ollama"
    assert llm_core._resolve_provider("https://x.example/v1", "chat_completions") == "openai"
    # auto / unknown fall back to URL inference
    assert llm_core._resolve_provider("https://api.anthropic.com", "auto") == "anthropic"
    assert llm_core._resolve_provider("https://x.example/v1", "bogus") == "openai"
    assert llm_core._resolve_provider("https://x.example/v1", None) == "openai"


def test_detect_provider_path_suffix():
    # Explicit-method chat URLs keep working through plain URL detection
    # (call sites that only have the chat URL need no signature changes).
    assert llm_core._detect_provider("https://x.example/v1/responses") == "responses"
    assert llm_core._detect_provider("https://x.example/v1/messages") == "anthropic"
    assert llm_core._detect_provider("https://x.example/api/chat") == "ollama"
    # Host-specific matches stay authoritative, plain hosts unchanged.
    assert llm_core._detect_provider("https://chatgpt.com/backend-api/codex/responses") == "chatgpt-subscription"
    assert llm_core._detect_provider("https://x.example/v1/chat/completions") == "openai"
    assert llm_core._detect_provider("https://notanthropic.com") == "openai"


def test_build_chat_url_per_method(no_dns):
    base = "https://x.example/v1"
    assert er.build_chat_url(base, "chat_completions") == "https://x.example/v1/chat/completions"
    assert er.build_chat_url(base, "responses") == "https://x.example/v1/responses"
    assert er.build_chat_url(base, "anthropic") == "https://x.example/v1/messages"
    assert er.build_chat_url("http://ollama-host:11434", "ollama") == "http://ollama-host:11434/api/chat"
    # auto unchanged
    assert er.build_chat_url(base) == "https://x.example/v1/chat/completions"
    # rebuilding a stored chat URL preserves the method without threading it
    assert er.build_chat_url("https://x.example/v1/responses") == "https://x.example/v1/responses"
    assert er.build_chat_url("https://x.example/v1/messages") == "https://x.example/v1/messages"


def test_build_headers_method_override():
    h = er.build_headers("sk-test", "https://x.example/v1", "anthropic")
    assert h["x-api-key"] == "sk-test"
    assert h["anthropic-version"] == "2023-06-01"
    h2 = er.build_headers("sk-test", "https://x.example/v1", "responses")
    assert h2["Authorization"] == "Bearer sk-test"
    h3 = er.build_headers("sk-test", "https://x.example/v1")
    assert h3["Authorization"] == "Bearer sk-test"


def test_build_models_url_method_override(no_dns):
    assert er.build_models_url("https://x.example/v1", "anthropic") == "https://x.example/v1/models"
    assert er.build_models_url("http://ollama-host:11434", "ollama") == "http://ollama-host:11434/api/tags"
    assert er.build_models_url("https://x.example/v1", "responses") == "https://x.example/v1/models"


def test_responses_payload_max_output_tokens():
    msgs = [
        {"role": "system", "content": "Be brief."},
        {"role": "user", "content": "Say OK"},
    ]
    codex = llm_core._build_chatgpt_responses_payload("codex-mini", msgs, 0.0, 100, stream=False)
    assert "max_output_tokens" not in codex
    assert codex["instructions"] == "Be brief."
    generic = llm_core._build_chatgpt_responses_payload(
        "gpt-5", msgs, 0.0, 100, stream=False, include_max_output_tokens=True,
    )
    assert generic["max_output_tokens"] == 100


def test_responses_payload_preserves_image_between_text_blocks():
    data_url = "data:image/png;base64,iVBORw0KGgo="
    payload = llm_core._build_chatgpt_responses_payload(
        "vision-model",
        [{"role": "user", "content": [
            {"type": "text", "text": "What is shown?"},
            {"type": "image_url", "image_url": {"url": data_url}},
            {"type": "text", "text": "Name the colors."},
        ]}],
        0.0, 100, stream=False,
    )
    assert payload["input"][0]["content"] == [
        {"type": "input_text", "text": "What is shown?"},
        {"type": "input_image", "image_url": data_url},
        {"type": "input_text", "text": "Name the colors."},
    ]


def test_parse_responses_response():
    data = {"output": [
        {"type": "reasoning", "summary": []},
        {"type": "message", "content": [{"type": "output_text", "text": "hel"}, {"type": "output_text", "text": "lo"}]},
        {"type": "function_call", "name": "x", "arguments": "{}"},
    ]}
    assert llm_core._parse_responses_response(data) == "hello"
    assert llm_core._parse_responses_response({"output": []}) == ""
    assert llm_core._parse_responses_response({}) == ""
    assert llm_core._parse_responses_response({"output": [{"type": "message", "content": [{"type": "refusal", "text": "no"}]}]}) == "no"


def test_api_method_migration(tmp_path, monkeypatch):
    import core.database as dbmod
    db_path = str(tmp_path / "mig.db")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE model_endpoints (id VARCHAR PRIMARY KEY, base_url VARCHAR)")
    conn.execute("INSERT INTO model_endpoints (id, base_url) VALUES ('e1', 'https://x.example/v1')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(dbmod, "DATABASE_URL", f"sqlite:///{db_path}")
    dbmod._migrate_add_api_method_column()
    conn = sqlite3.connect(db_path)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(model_endpoints)")]
    assert "api_method" in cols
    assert conn.execute("SELECT api_method FROM model_endpoints WHERE id='e1'").fetchone()[0] is None
    conn.close()
    # idempotent re-run
    dbmod._migrate_add_api_method_column()
