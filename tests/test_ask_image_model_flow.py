"""Ask-before-generate: with the `ask_image_model` setting ON, image
generate/edit tool calls pause and show a model-picker card; the pick runs
via POST /api/chat/image-choice/<session> with no LLM round-trip."""

import json
from pathlib import Path

import src.agent_tools  # noqa: F401  (resolve circular init)

AGENT_LOOP = Path("src/agent_loop.py").read_text(encoding="utf-8")
CHAT_ROUTES = Path("routes/chat_routes.py").read_text(encoding="utf-8")
CHAT_JS = Path("static/js/chat.js").read_text(encoding="utf-8")
SETTINGS_JS = Path("static/js/settings.js").read_text(encoding="utf-8")
INDEX_HTML = Path("static/index.html").read_text(encoding="utf-8")


def test_setting_registered_and_per_user():
    from src.settings import DEFAULT_SETTINGS, _PER_USER_KEYS
    assert DEFAULT_SETTINGS.get("ask_image_model") is False
    assert "ask_image_model" in _PER_USER_KEYS


def test_agent_loop_pauses_image_tools_when_enabled():
    assert 'get_user_setting("ask_image_model"' in AGENT_LOOP
    assert '"type": "image_model_choice"' in AGENT_LOOP
    # The pause ends the turn like ask_user — no execution this round.
    idx = AGENT_LOOP.index('"type": "image_model_choice"')
    assert "_awaiting_user = True" in AGENT_LOOP[idx:idx + 1500]


def test_confirm_route_and_frontend_wiring_exist():
    assert "/api/chat/image-choice/{session_id}" in CHAT_ROUTES
    assert "pop_pending_image_request" in CHAT_ROUTES
    assert "image_model_choice" in CHAT_JS
    assert "chatRenderer.renderImageChoiceCard" in CHAT_JS
    assert "set-imgAskToggle" in SETTINGS_JS and "ask_image_model" in SETTINGS_JS
    assert 'id="set-imgAskToggle"' in INDEX_HTML
    # API add-form type selector restored (admin.js reads adm-epType).
    assert 'id="adm-epType"' in INDEX_HTML


def test_apply_image_model_choice_round_trips():
    from src.ai_interaction import apply_image_model_choice
    out = json.loads(apply_image_model_choice("a red apple\nauto\n512x512\nhigh", "flux@Local"))
    assert out["prompt"] == "a red apple"
    assert out["model"] == "flux@Local"
    assert out["size"] == "512x512"


def test_pending_request_is_owner_scoped():
    from src.ai_interaction import stash_pending_image_request, pop_pending_image_request
    stash_pending_image_request("sess-x", "generate_image", "cat", "alice")
    assert pop_pending_image_request("sess-x", "mallory") is None
    assert pop_pending_image_request("sess-x", "alice")["tool"] == "generate_image"
    assert pop_pending_image_request("sess-x", "alice") is None


def test_per_model_image_marks_supported_end_to_end():
    """Mixed endpoints serve chat AND image models, so marks must be
    per-model: DB column, PATCH key, models-list flag, admin UI button, and
    the options builder consuming the marks."""
    db_src = Path("core/database.py").read_text(encoding="utf-8")
    routes_src = Path("routes/model_routes.py").read_text(encoding="utf-8")
    admin_js = Path("static/js/admin.js").read_text(encoding="utf-8")
    ai_src = Path("src/ai_interaction.py").read_text(encoding="utf-8")

    assert "image_models = Column(Text" in db_src
    assert 'ALTER TABLE model_endpoints ADD COLUMN image_models TEXT' in db_src
    assert '"is_image": m in image_set' in routes_src
    assert '"image_models" in body' in routes_src
    assert "data-ep-model-img" in admin_js
    assert "image_models: marked" in admin_js
    # Options builder consumes per-model marks (not just endpoint model_type).
    assert 'getattr(ep, "image_models", None)' in ai_src


def test_image_options_builder_handles_marked_models():
    from src.ai_interaction import list_image_model_options
    # Read-only smoke: never crashes, always offers auto, and every option is
    # a {spec,label} pair.
    opts = list_image_model_options(None)
    assert any(o["spec"] == "auto" for o in opts)
    assert all(isinstance(o.get("spec"), str) and isinstance(o.get("label"), str) for o in opts)


def test_explicit_image_request_synthesizes_call():
    """The real fix: local reasoning models decide to call generate_image
    INSIDE their <think> block then stop without emitting the fenced call, so
    no image and no picker. For an unambiguous request the loop must synthesize
    the generate_image call from the user's message instead of depending on the
    model to volunteer it."""
    from src.agent_loop import _is_explicit_image_request, _extract_image_prompt

    # Detection: explicit requests fire, explanatory questions don't.
    assert _is_explicit_image_request("gen me an image of KLCC")
    assert _is_explicit_image_request("make me a picture of the sunset")
    assert _is_explicit_image_request("draw a logo for my startup")
    assert not _is_explicit_image_request("how does image generation work")
    assert not _is_explicit_image_request("what image format is best")
    assert not _is_explicit_image_request("the picture is blurry")

    # Prompt extraction keeps the subject, drops the imperative lead-in.
    assert _extract_image_prompt("gen me an image of KLCC") == "KLCC"
    assert _extract_image_prompt("make me a picture of the sunset") == "the sunset"
    assert _extract_image_prompt("draw a logo for my startup") == "logo for my startup"
    # Subject-less request falls back to the whole message, never a bare noun.
    assert _extract_image_prompt("generate an image") == "generate an image"


def test_image_synthesis_wired_into_loop():
    src = Path("src/agent_loop.py").read_text(encoding="utf-8")
    # Guarded to fire at most once per turn (no infinite re-synthesis).
    assert "_image_request_synthesized" in src
    assert 'ToolBlock("generate_image", _synth_prompt)' in src
    # Gated on availability + not disabled, mirrors the nudge gate.
    assert '"generate_image" not in (disabled_tools or set())' in src
    assert "_is_explicit_image_request(_last_user)" in src


def test_image_picker_is_persisted_and_durable():
    """The picker vanished because it was only rendered live and never
    persisted, so the turn-end thread re-render wiped it. It must be persisted
    on the tool event and re-rendered from history — exactly like ask_user."""
    agent_src = Path("src/agent_loop.py").read_text(encoding="utf-8")
    renderer_src = Path("static/js/chatRenderer.js").read_text(encoding="utf-8")
    chat_src = Path("static/js/chat.js").read_text(encoding="utf-8")

    # Backend persists the picker payload on the tool event.
    assert '_pending_image_choice_event' in agent_src
    assert 'tool_event["image_choice"] = _pending_image_choice_event' in agent_src

    # Renderer re-creates the card from the persisted event, mirroring ask_user.
    assert "export function renderImageChoiceCard" in renderer_src
    assert "if (ev.image_choice) pendingImageChoice = ev.image_choice;" in renderer_src
    assert "renderImageChoiceCard(pendingImageChoice" in renderer_src
    assert "renderImageChoiceCard," in renderer_src  # exported

    # Live path routes through the SAME shared renderer (no divergent local copy).
    assert "chatRenderer.renderImageChoiceCard(json" in chat_src


def test_image_choice_streams_with_keepalive_not_blocking():
    """Root cause of 'click generate, nothing happens': the image-choice POST
    blocked ~1min with no keepalive, so the Cloudflare tunnel killed the idle
    connection before the image came back. It must stream through
    _sse_keepalive (the app's pattern for every long op) and the frontend must
    read the SSE stream, not await a single JSON body."""
    routes_src = Path("routes/chat_routes.py").read_text(encoding="utf-8")
    renderer_src = Path("static/js/chatRenderer.js").read_text(encoding="utf-8")

    # Backend streams the image-choice response with keepalives.
    assert "_image_choice_stream" in routes_src
    assert "_sse_keepalive(_image_choice_stream())" in routes_src
    assert 'media_type="text/event-stream"' in routes_src

    # Frontend reads the SSE stream (getReader) rather than res.json().
    assert "res.body.getReader()" in renderer_src
    assert "if (line.startsWith('data:'))" in renderer_src


def test_image_model_choice_stream_forwarding():
    """Ensure that 'image_model_choice' is allowed through the SSE stream parser in chat_routes.py,
    clears timeouts on the frontend in chat.js, and that cancel and placeholder states exist."""
    routes_src = Path("routes/chat_routes.py").read_text(encoding="utf-8")
    chat_src = Path("static/js/chat.js").read_text(encoding="utf-8")
    renderer_src = Path("static/js/chatRenderer.js").read_text(encoding="utf-8")

    assert '"image_model_choice"' in routes_src
    assert "json.type === 'image_model_choice'" in chat_src
    # Assert cancel persistence block exists on the backend
    assert 'action == "cancel"' in routes_src
    assert '_hit.pop("image_choice", None)' in routes_src
    # Assert loading placeholder wrap is created on click
    assert 'generated-image-loading-wrap' in renderer_src




def test_confirm_retries_survive_lost_stash():
    """The pending stash is in-memory and popped on first use — after a failed
    attempt or backend restart the persisted picker card remained but every
    retry 404'd. The card echoes prompt/tool in the confirm body and the route
    rebuilds from them when the stash is gone; cancel is idempotent."""
    routes_src = Path("routes/chat_routes.py").read_text(encoding="utf-8")
    renderer_src = Path("static/js/chatRenderer.js").read_text(encoding="utf-8")

    # Route falls back to body prompt/tool instead of hard-404ing.
    assert 'raw_content = (pending or {}).get("content")' in routes_src
    assert 'raw_content = str((body or {}).get("prompt") or "").strip()' in routes_src
    # Only 404 when there is truly nothing to generate from.
    idx = routes_src.index('raw_content = str((body or {}).get("prompt")')
    assert "No pending image request" in routes_src[idx:idx + 400]

    # Card echoes its own prompt/tool back on confirm.
    assert "prompt: pl.prompt || ''" in renderer_src
    assert "tool: pl.tool || 'generate_image'" in renderer_src


def test_synthesis_does_not_duplicate_a_model_made_image():
    """When the model called generate_image itself in round 1, its tool-less
    confirmation round must NOT trigger the synthesis fallback — that produced
    a duplicate second image (caption 'f1') on every successful request."""
    src = Path("src/agent_loop.py").read_text(encoding="utf-8")
    idx = src.index("_synth_prompt = _extract_image_prompt(_last_user)")
    gate = src[idx - 1200:idx]
    assert 'ev.get("tool") in ("generate_image", "ai_edit_image")' in gate
    assert "for ev in tool_events" in gate


def test_marked_image_models_hidden_from_chat_picker():
    """Models marked as image-generation (per-model image_models list, or a
    whole model_type='image' endpoint) are not chat models — they must not
    appear in the CHAT model picker (/api/models). The Settings image pickers
    read /api/model-endpoints, which still lists everything."""
    src = Path("routes/model_routes.py").read_text(encoding="utf-8")
    idx = src.index("none belong in the CHAT")
    block = src[idx - 400:idx + 1200]
    assert 'if ep_model_type == "image":' in block
    assert "continue" in block
    assert '_normalize_model_ids(getattr(ep, "image_models", None))' in block
    assert "m for m in model_ids if m not in _img_marks" in block
