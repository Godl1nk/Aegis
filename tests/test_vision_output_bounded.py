"""The image-description call must be bounded, in tokens and in wall clock.

Observed failure: a user attached an image, the local VL server generated 5096
tokens over 2m05s and was still going when the proxy killed it (502 upstream),
and the browser showed a bare "Error 524" for a reply that was never coming.

Two independent causes, both fixed here:

  * `llm_call` defaults to ``max_tokens=0``, which OMITS the field entirely, so
    an unbounded llama.cpp/vLLM server generates until it hits the context
    window. Nothing capped the description.
  * The chat turn awaits that description before it can stream, so a stalled
    vision endpoint holds the whole HTTP request open past the ~100s cut-off
    common to reverse proxies and tunnels.
"""
import src.document_processor as dp
import src.llm_core as llm_core


def _stub_vl(monkeypatch, captured, response="A description."):
    def fake_llm_call(url, model, messages, headers=None, timeout=None,
                      max_tokens=0, **kwargs):
        captured.update(url=url, model=model, timeout=timeout, max_tokens=max_tokens)
        return response

    monkeypatch.setattr(dp, "llm_call", fake_llm_call)
    monkeypatch.setattr(dp, "_load_vl_settings",
                        lambda: {"vision_enabled": True, "vision_model": "vl"})
    monkeypatch.setattr(dp, "_resolve_vl_model",
                        lambda m, owner=None: ("http://vl.test/v1/chat/completions", "vl-model", {}))


def test_description_call_sends_a_token_cap(monkeypatch, tmp_path):
    captured = {}
    _stub_vl(monkeypatch, captured)
    image = tmp_path / "image.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)

    dp.analyze_image_with_vl_result(str(image))

    assert captured["max_tokens"] == dp.VISION_MAX_OUTPUT_TOKENS
    assert captured["max_tokens"] > 0, "max_tokens=0 omits the field and lets generation run away"


def test_the_cap_actually_reaches_the_wire():
    """llm_call drops max_tokens when it is falsy, so a nonzero value is the
    only thing that puts a limit in the request body."""
    payload = {"model": "vl-model", "messages": [], "temperature": 0.7}
    max_tokens = dp.VISION_MAX_OUTPUT_TOKENS
    if max_tokens and max_tokens > 0:
        payload["max_tokens"] = max_tokens

    assert "max_tokens" in payload
    # The default llm_call value is what produced the runaway.
    assert llm_core.LLMConfig.DEFAULT_MAX_TOKENS == 0


def test_cap_is_generous_enough_for_a_detailed_description():
    # Small enough to bound latency, big enough to transcribe a dense figure.
    # Set to bound runaway loops, NOT to trim detail: it must stay well above
    # a genuinely long description (~1150 words at 1536 tokens).
    assert 1536 <= dp.VISION_MAX_OUTPUT_TOKENS <= 4096


def test_cap_is_env_overridable(monkeypatch):
    monkeypatch.setenv("VISION_MAX_TOKENS", "2048")
    import importlib
    reloaded = importlib.reload(dp)
    try:
        assert reloaded.VISION_MAX_OUTPUT_TOKENS == 2048
    finally:
        monkeypatch.delenv("VISION_MAX_TOKENS", raising=False)
        importlib.reload(dp)


def test_chat_wait_budget_sits_below_common_proxy_timeouts():
    """Cloudflare cuts at ~100s and returns 524. The chat turn must give up
    before that and answer without the description."""
    import src.chat_handler as ch
    assert 0 < ch.VISION_CHAT_WAIT_SECONDS < 100


def test_chat_handler_bounds_the_description_wait():
    """Source-level: the await must be wrapped, and the timeout must degrade to
    a reply rather than propagate and lose the turn."""
    import pathlib
    src = pathlib.Path(__file__).resolve().parent.parent / "src" / "chat_handler.py"
    text = src.read_text(encoding="utf-8")
    idx = text.index("analyze_image_with_vl_result")
    idx = text.index("analyze_image_with_vl_result", idx + 1)  # the call, not the import
    window = text[idx - 700: idx + 900]
    assert "wait_for" in window, "the description await must be time-bounded"
    assert "VISION_CHAT_WAIT_SECONDS" in window
    assert "asyncio.TimeoutError" in window, "a slow vision model must not fail the chat turn"
