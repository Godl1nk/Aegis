"""llama-swap proxy status chatter must never reach the reply.

With sendLoadingState, llama-swap injects loading/queue lines into the chat
stream while a model swap is in flight:

    ___
    llama-swap loading model: <name>
    Queue position: #1 .............

These are transport artifacts, not model output. The stream parser used to
feed non-JSON `data:` payloads straight into reply content, so the status
block rendered as the assistant's message. Rare cases pinned here: chunk
splits mid-status-line, status-only streams, flush tails, reasoning-channel
injection, and false-positive guards (model prose about queues, mid-reply
horizontal rules).
"""
import asyncio
import json

from src import llm_core


class _FakeResp:
    status_code = 200

    def __init__(self, lines):
        self._lines = lines

    async def aiter_lines(self):
        for ln in self._lines:
            yield ln

    async def aread(self):
        return b""


class _FakeStreamCtx:
    def __init__(self, lines):
        self._lines = lines

    async def __aenter__(self):
        return _FakeResp(self._lines)

    async def __aexit__(self, *exc):
        return False


class _FakeClient:
    def __init__(self, lines):
        self._lines = lines

    def stream(self, *args, **kwargs):
        return _FakeStreamCtx(self._lines)


def _content_line(text):
    return "data: " + json.dumps({"choices": [{"delta": {"content": text}}]})


def _reasoning_line(text):
    return "data: " + json.dumps({"choices": [{"delta": {"reasoning_content": text}}]})


def _run_stream(lines, monkeypatch):
    """Drive stream_llm against a faked upstream; return (content, thinking)
    with each side's delta texts joined in arrival order."""
    monkeypatch.setattr(llm_core, "_get_http_client", lambda: _FakeClient(lines))
    monkeypatch.setattr(llm_core, "_is_host_dead", lambda u: False)
    monkeypatch.setattr(llm_core, "note_model_activity", lambda *a, **k: None)
    monkeypatch.setattr(llm_core, "_clear_host_dead", lambda *a, **k: None)

    async def _go():
        out = []
        async for chunk in llm_core.stream_llm(
            "http://llama-swap:8080/v1/chat/completions",
            "test-model",
            [{"role": "user", "content": "hi"}],
        ):
            out.append(chunk)
        return out

    content_parts, thinking_parts = [], []
    for chunk in asyncio.run(_go()):
        for raw in chunk.splitlines():
            raw = raw.strip()
            if not raw.startswith("data:"):
                continue
            payload = raw[5:].strip()
            if not payload.startswith("{"):
                continue
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if "delta" not in obj:
                continue
            (thinking_parts if obj.get("thinking") else content_parts).append(obj["delta"])
    return "".join(content_parts), "".join(thinking_parts)


STATUS_LINES = [
    "data: ___",
    "data: llama-swap loading model: Qwen3.5-27B",
    "data: Queue position: #1 .............",
]


def test_non_json_status_lines_dropped_reply_intact(monkeypatch):
    content, _ = _run_stream(
        STATUS_LINES + [_content_line("Hello there."), "data: [DONE]"],
        monkeypatch,
    )
    assert "llama-swap" not in content
    assert "Queue position" not in content
    assert "___" not in content
    assert "Hello there." in content


def test_json_content_status_chunks_dropped(monkeypatch):
    content, _ = _run_stream(
        [
            _content_line("___\n"),
            _content_line("llama-swap loading model: Qwen3.5-27B\n"),
            _content_line("Queue position: #1 .............\n"),
            _content_line("Real reply."),
            "data: [DONE]",
        ],
        monkeypatch,
    )
    assert content == "Real reply."


def test_status_split_across_chunk_boundary(monkeypatch):
    content, _ = _run_stream(
        [
            _content_line("Queue posi"),
            _content_line("tion: #1 ....\n"),
            _content_line("Real reply."),
            "data: [DONE]",
        ],
        monkeypatch,
    )
    assert "Queue posi" not in content
    assert content == "Real reply."


def test_status_only_stream_yields_no_content(monkeypatch):
    content, thinking = _run_stream(STATUS_LINES + ["data: [DONE]"], monkeypatch)
    assert content == ""
    assert thinking == ""


def test_queue_prose_without_shape_kept(monkeypatch):
    # No colon, no dot-run: model prose about queues must survive.
    content, _ = _run_stream(
        [_content_line("Queue position matters for throughput."), "data: [DONE]"],
        monkeypatch,
    )
    assert "Queue position matters for throughput." in content


def test_queue_prose_with_colon_but_words_kept(monkeypatch):
    # Colon present but non-queue text after it: still model prose.
    content, _ = _run_stream(
        [_content_line("queue position: 5 things to check"), "data: [DONE]"],
        monkeypatch,
    )
    assert "queue position: 5 things to check" in content


def test_split_think_tag_still_routes_to_thinking(monkeypatch):
    # The filter must not hold newline-free model text: think-tag splitting
    # downstream depends on seeing every chunk in order.
    content, thinking = _run_stream(
        [
            _content_line("<think>step one "),
            _content_line("step two"),
            _content_line("</think>Final answer"),
            "data: [DONE]",
        ],
        monkeypatch,
    )
    assert "step one" in thinking and "step two" in thinking
    assert "Final answer" in content
    assert "<think>" not in content


def test_mid_reply_horizontal_rule_kept(monkeypatch):
    content, _ = _run_stream(
        [_content_line("Part one\n___\nPart two"), "data: [DONE]"],
        monkeypatch,
    )
    assert content == "Part one\n___\nPart two"


def test_reasoning_channel_status_dropped_real_reasoning_kept(monkeypatch):
    _, thinking = _run_stream(
        [
            _reasoning_line("Queue position: #1 .............\n"),
            _reasoning_line("weighing options"),
            "data: [DONE]",
        ],
        monkeypatch,
    )
    assert "Queue position" not in thinking
    assert "weighing options" in thinking


def test_legit_partial_tail_flushed(monkeypatch):
    content, _ = _run_stream([_content_line("Hello wo"), "data: [DONE]"], monkeypatch)
    assert content == "Hello wo"


def test_status_tail_without_newline_dropped_at_done(monkeypatch):
    content, _ = _run_stream(
        [_content_line("Queue position: #1 ...."), "data: [DONE]"], monkeypatch
    )
    assert content == ""


def test_normal_stream_byte_identical(monkeypatch):
    content, _ = _run_stream(
        [_content_line("Hi there.\nSecond line."), "data: [DONE]"], monkeypatch
    )
    assert content == "Hi there.\nSecond line."
