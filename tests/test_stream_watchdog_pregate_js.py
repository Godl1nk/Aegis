"""Tab-recovery / stale-stream watchdogs must NOT fire before the stream has
delivered a single byte.

The pre-first-byte phase is legitimately silent for minutes: vision
preprocessing of uploaded photos and llama-swap model load run BEFORE the
response starts streaming, so no keepalives flow yet. The visibilitychange
recovery treated that silence as a dead stream and removed BOTH the user's
message bubble and the processing bubble mid-turn (nothing re-renderable from
the DB yet — the user message is only persisted after preprocessing). The hard
response timeout still covers genuinely hung requests with a visible error."""

from pathlib import Path

CHAT_JS = Path("static/js/chat.js").read_text(encoding="utf-8")


def test_reader_ever_active_flag_lifecycle():
    # Declared, reset at stream start, set on first read.
    assert "let _readerEverActive = false;" in CHAT_JS
    start_idx = CHAT_JS.index("_lastReaderActivity = Date.now();\n")
    assert "_readerEverActive = false;" in CHAT_JS[start_idx:start_idx + 600]
    read_idx = CHAT_JS.index("const { done, value } = await reader.read();")
    assert "_readerEverActive = true;" in CHAT_JS[read_idx:read_idx + 200]


def test_both_watchdogs_gated_on_first_byte():
    # visibilitychange tab-recovery
    vis_idx = CHAT_JS.index("document.addEventListener('visibilitychange'")
    assert "if (!_readerEverActive) return;" in CHAT_JS[vis_idx:vis_idx + 900]
    # stale-local server probe
    probe_idx = CHAT_JS.index("async function _probeStaleLocalStream()")
    assert "if (!_readerEverActive) return;" in CHAT_JS[probe_idx:probe_idx + 600]


def test_image_attachments_get_vision_aware_wait_spinner():
    """The vision-describe pass runs BEFORE the stream starts and can take
    minutes on local models (model swap + image encode + description
    generation at ~24 t/s). The wait spinner said 'pre-filling context' —
    wrong and alarming. With image attachments it must say what is actually
    happening."""
    assert "_hasImageAttach" in CHAT_JS
    assert "Analyzing image with vision model" in CHAT_JS
    assert "Vision model is reading the image" in CHAT_JS
    assert "scheduleFirstTokenWaitMessages(true)" in CHAT_JS
    # Non-image sends keep the original texts.
    assert "Large local model is pre-filling context" in CHAT_JS


def test_vision_describe_skips_thinking():
    """Thinking-enabled Qwen burned 1000+ <think> tokens before the image
    description — a minute of invisible pre-stream latency per image. The
    describe prompt steers reasoning off (/no_think = Qwen3 soft switch; other
    VL models see a stray token, harmless). Deliberately NO max_tokens cap —
    the user wants full description detail preserved."""
    src = Path("src/document_processor.py").read_text(encoding="utf-8")
    assert "/no_think" in src
    assert "no reasoning" in src
    # No hard cap on the describe call (detail > speed, per user).
    idx = src.index("VISION_ANALYSIS_TIMEOUT_SECONDS")
    assert "max_tokens" not in src[idx:idx + 300]


def test_dead_fetch_reconnects_instead_of_reprompting_when_run_alive():
    """Mobile browsers kill in-flight fetches when the app is backgrounded
    (send a photo → switch apps → return). The run is DETACHED server-side and
    still generating — auto-recover used to inject a 'continue' prompt into
    the session anyway, causing double generation and interleaved replies.
    The catch must first probe /api/chat/stream_status and reconnect when the
    server run is alive; the re-prompt handshake only fires when it is gone."""
    idx = CHAT_JS.index("Stream died unexpectedly")
    block = CHAT_JS[idx:idx + 2600]
    assert "/api/chat/stream_status/" in block
    assert "_serverStillRunning = _d.status === 'streaming';" in block
    # Reconnect path re-enters the session (resumeStream reattaches).
    assert "sessionModule.selectSession(_reSid);" in block
    # Re-prompt handshake is the else-branch, gated on the run being gone.
    assert "else if (!(_isRecoverableStreamErr(err) && _tryAutoRecover" in block


def test_stream_registered_before_preprocessing_with_self_heal():
    """The stream must be visible to /api/chat/stream_status DURING
    preprocessing (vision describe runs minutes before the generator
    registers). Otherwise a mobile-killed fetch in that window probed 'idle'
    and re-prompted — duplicating the turn. setdefault never clobbers a live
    record; a stale 'preparing' entry self-heals so a phantom stream can't
    lock the session forever."""
    routes_src = Path("routes/chat_routes.py").read_text(encoding="utf-8")

    reg = routes_src.index('"phase": "preparing"')
    # chat_stream's build_chat_context (the photo/vision path) — not the first
    # occurrence in the file, which belongs to a different route.
    ctx = routes_src.index("# Build shared context (stream path uses enhanced_message")
    assert reg < ctx, "early registration must precede chat_stream preprocessing"
    assert "_active_streams.setdefault(session, {" in routes_src

    # Self-heal in stream_status for orphaned preparing entries.
    heal = routes_src.index('rec.get("phase") == "preparing"')
    assert 'rec.get("ts", 0) > 900' in routes_src[heal:heal + 200]
