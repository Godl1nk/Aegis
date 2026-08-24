"""Reasoning-effort control: mechanism resolution, application, and parity.

`model_capabilities.py` defined the taxonomy of reasoning controls but nothing
consumed it. Reasoning was steered by three hardcoded `if` branches in llm_core
gated on `_supports_thinking()`, a substring match over 13 model-name fragments.
That gave no control at all to most models (deepseek-v4-flash, gpt-5, claude-*,
gemini-* all scored False) and the WRONG mechanism to others — Qwen3.6 on
llama.cpp was sent `/no_think` in the prompt text, ignored it, and generated
5096 tokens before a proxy killed the request.

The parity tests matter most: shipping this must not change behaviour for
anyone who never touches the setting.
"""
import pytest

from src.reasoning_control import (
    NO_CONTROL,
    PREF_AUTO,
    PREF_HIGH,
    PREF_LOW,
    PREF_MEDIUM,
    PREF_OFF,
    apply_reasoning_control,
    effective_preference,
    normalize_preference,
    resolve_reasoning_control,
)
from src.model_capabilities import (
    REASONING_CONTROL_EFFORT,
    REASONING_CONTROL_MESSAGE_DIRECTIVE,
    REASONING_CONTROL_NATIVE_BOOL,
    REASONING_CONTROL_STRUCTURED_OBJECT,
    REASONING_CONTROL_TEMPLATE_KWARG,
)


# --- parity: "auto" must reproduce the old hardcoded branches ---------------

def test_auto_still_sends_mistral_effort():
    payload = {}
    applied = apply_reasoning_control(
        payload, provider="mistral", model="magistral-small",
        url="https://api.mistral.ai/v1", preference=PREF_AUTO,
    )
    assert payload == {"reasoning_effort": "high"}
    assert applied == PREF_AUTO


def test_auto_still_disables_thinking_on_ollama():
    """Without this, tool calls get swallowed inside <think> blocks."""
    payload = {}
    apply_reasoning_control(
        payload, provider="ollama", model="qwen3-8b",
        url="http://localhost:11434/v1", preference=PREF_AUTO,
    )
    assert payload == {"think": False}


def test_auto_sends_nothing_for_everything_else():
    payload = {}
    apply_reasoning_control(
        payload, provider="openai", model="gpt-5",
        url="https://api.openai.com/v1", preference=PREF_AUTO,
    )
    assert payload == {}


def test_unknown_model_is_left_alone():
    payload = {}
    apply_reasoning_control(
        payload, provider="openai", model="some-random-7b", url="https://x.test/v1",
        preference=PREF_OFF,
    )
    assert payload == {}


# --- mechanism resolution ---------------------------------------------------

@pytest.mark.parametrize(
    "provider,model,url,mechanism",
    [
        ("anthropic", "claude-opus-4", "", REASONING_CONTROL_STRUCTURED_OBJECT),
        ("google", "gemini-3-pro", "", REASONING_CONTROL_EFFORT),
        ("openai", "gpt-5", "", REASONING_CONTROL_EFFORT),
        ("mistral", "magistral-small", "", REASONING_CONTROL_EFFORT),
        ("ollama", "qwen3-8b", "", REASONING_CONTROL_NATIVE_BOOL),
        # Self-hosted OpenAI-compatible server: chat-template kwarg.
        ("openai", "Qwen3.6-MTP-27B-IQ4_NL", "http://localhost:5802/v1", REASONING_CONTROL_TEMPLATE_KWARG),
        # Hosted OpenAI-compatible relay: graded effort, since gateways
        # generally accept reasoning_effort (falls back on a 400).
        ("openai", "deepseek-v4-flash-free", "https://api.example.com/v1", REASONING_CONTROL_EFFORT),
    ],
)
def test_mechanism_resolution(provider, model, url, mechanism):
    assert resolve_reasoning_control(provider, model, url).mechanism == mechanism


def test_the_same_weights_resolve_differently_per_serving_path():
    """Qwen3 on Ollama takes a boolean; on llama.cpp it takes a template kwarg.
    The old substring heuristic could not express that."""
    ollama = resolve_reasoning_control("ollama", "qwen3-8b", "http://localhost:11434/v1")
    local = resolve_reasoning_control("openai", "qwen3-8b", "http://localhost:5802/v1")
    assert ollama.mechanism != local.mechanism


def test_endpoint_kind_overrides_url_locality_for_proxy_endpoints():
    """A LAN proxy can expose OpenAI-compatible hosted models. It must not be
    treated like llama.cpp/vLLM just because the URL is private."""
    proxy = resolve_reasoning_control(
        "openai", "deepseek-v4-flash-free", "http://192.168.1.50:8000/v1",
        endpoint_kind="proxy",
    )
    local = resolve_reasoning_control(
        "openai", "deepseek-v4-flash-free", "http://192.168.1.50:8000/v1",
        endpoint_kind="local",
    )
    assert proxy.mechanism == REASONING_CONTROL_EFFORT
    assert local.mechanism == REASONING_CONTROL_TEMPLATE_KWARG


def test_model_specific_supported_levels_are_not_faked():
    assert resolve_reasoning_control("openai", "gpt-5-pro", "").mechanism == ""
    assert PREF_OFF not in resolve_reasoning_control(
        "google", "gemini-2.5-pro", "https://generativelanguage.googleapis.com/v1beta/openai",
    ).supported
    assert PREF_OFF in resolve_reasoning_control(
        "google", "gemini-2.5-flash", "https://generativelanguage.googleapis.com/v1beta/openai",
    ).supported
    assert resolve_reasoning_control(
        "google", "gemini-2.0-flash", "https://generativelanguage.googleapis.com/v1beta/openai",
    ).mechanism == ""


def test_ollama_gpt_oss_uses_graded_think_values_without_off():
    control = resolve_reasoning_control("ollama", "gpt-oss:20b", "http://localhost:11434")
    assert control.supports_effort
    assert PREF_OFF not in control.supported
    payload = {}
    apply_reasoning_control(
        payload, provider="ollama", model="gpt-oss:20b",
        url="http://localhost:11434", preference=PREF_LOW,
    )
    assert payload == {"think": "low"}


def test_models_the_old_heuristic_missed_now_have_control():
    """These all returned _supports_thinking=False and got nothing."""
    for provider, model in [("openai", "gpt-5"), ("anthropic", "claude-opus-4"),
                            ("google", "gemini-3-pro")]:
        assert resolve_reasoning_control(provider, model, "") is not NO_CONTROL


# --- applying an explicit preference ---------------------------------------

def test_off_uses_the_template_kwarg_for_local_qwen():
    """The incident: /no_think was sent and ignored. chat_template_kwargs is the
    field the llama.cpp chat template actually reads.

    `think` rides along because a local /v1 URL could equally be Ollama, and
    nothing in the URL distinguishes them — each server ignores the field it
    does not implement. Sending only one would leave the control silently dead
    on the other."""
    payload = {}
    apply_reasoning_control(
        payload, provider="openai", model="Qwen3.6-MTP-27B-IQ4_NL",
        url="http://localhost:5802/v1", preference=PREF_OFF,
    )
    assert payload["chat_template_kwargs"] == {"enable_thinking": False}
    assert payload["think"] is False


def test_remote_template_kwarg_does_not_get_the_ollama_boolean():
    """The dual-send is scoped to local /v1 URLs; a LAN vLLM box is not Ollama."""
    payload = {}
    apply_reasoning_control(
        payload, provider="openai", model="qwen3-32b",
        url="http://192.168.1.50:8000/v1", preference=PREF_OFF,
    )
    assert payload == {"chat_template_kwargs": {"enable_thinking": False}}


def test_off_maps_to_each_provider_shape():
    cases = [
        (("openai", "gpt-5.1", ""), {"reasoning_effort": "none"}),
        (("ollama", "qwen3-8b", ""), {"think": False}),
        (("anthropic", "claude-opus-4-6", ""), {"thinking": {"type": "disabled"}}),
    ]
    for (provider, model, url), expected in cases:
        payload = {}
        apply_reasoning_control(payload, provider=provider, model=model, url=url,
                                preference=PREF_OFF)
        assert payload == expected, f"{provider}/{model}"


def test_graded_effort_reaches_effort_models():
    payload = {}
    apply_reasoning_control(payload, provider="openai", model="gpt-5", url="",
                            preference=PREF_LOW)
    assert payload == {"reasoning_effort": "low"}


def test_gemini_openai_compat_uses_reasoning_effort():
    low, high = {}, {}
    apply_reasoning_control(low, provider="google", model="gemini-3-pro", url="",
                            preference=PREF_LOW)
    apply_reasoning_control(high, provider="google", model="gemini-3-pro", url="",
                            preference=PREF_HIGH)
    assert low["reasoning_effort"] == "low"
    assert high["reasoning_effort"] == "high"


def test_gemini_openai_compat_is_inferred_from_url_without_changing_transport():
    url = "https://generativelanguage.googleapis.com/v1beta/openai"
    control = resolve_reasoning_control("openai", "gemini-3-pro", url)
    assert control.mechanism == REASONING_CONTROL_EFFORT
    assert PREF_OFF not in control.supported


def test_anthropic_adaptive_and_manual_models_use_different_shapes():
    adaptive = {"max_tokens": 8192, "temperature": 0.4}
    manual = {"max_tokens": 8192, "temperature": 0.4}
    apply_reasoning_control(
        adaptive, provider="anthropic", model="claude-sonnet-4-6",
        preference=PREF_LOW,
    )
    apply_reasoning_control(
        manual, provider="anthropic", model="claude-sonnet-4-5",
        preference=PREF_LOW,
    )
    assert adaptive["thinking"] == {"type": "adaptive"}
    assert adaptive["output_config"] == {"effort": "low"}
    assert manual["thinking"]["type"] == "enabled"
    assert manual["thinking"]["budget_tokens"] < manual["max_tokens"]
    assert "temperature" not in adaptive and "temperature" not in manual


def test_graded_effort_on_a_boolean_model_does_not_read_as_off():
    """"Low" on a model whose only control is on/off must leave reasoning ON.
    Mapping it to `think: false` would silently turn thinking off when the user
    asked for less of it, not none."""
    payload = {}
    applied = apply_reasoning_control(
        payload, provider="ollama", model="qwen3-8b",
        url="http://localhost:11434/v1", preference=PREF_LOW,
    )
    assert applied == PREF_AUTO
    assert payload == {}


def test_unimplemented_subscription_transport_has_no_picker_control():
    assert resolve_reasoning_control("chatgpt-subscription", "gpt-5.1", "") is NO_CONTROL
    assert resolve_reasoning_control("ollama", "qwen3-8b", "").supports_effort is False


def test_message_directive_edits_the_last_user_turn():
    """Uses a rejecting endpoint so the directive is the ACTIVE mechanism."""
    import src.reasoning_control as rc
    from src.llm_core import _note_reasoning_rejection
    rc._REJECTED.clear()
    url = "https://directive-only.test/v1"
    _note_reasoning_rejection(400, "unsupported parameter: reasoning_effort", url)
    messages = [
        {"role": "system", "content": "be helpful"},
        {"role": "user", "content": "solve this"},
    ]
    payload = {}
    apply_reasoning_control(
        payload, provider="openai", model="deepseek-v4-flash-free",
        url=url, preference=PREF_OFF, messages=messages,
    )
    assert payload == {}
    assert messages[1]["content"].endswith("/no_think")
    assert messages[0]["content"] == "be helpful"


def test_message_directive_is_not_duplicated():
    messages = [{"role": "user", "content": "describe this /no_think"}]
    apply_reasoning_control(
        {}, provider="openai", model="deepseek-v4-flash-free",
        url="https://api.example.com/v1", preference=PREF_OFF, messages=messages,
    )
    assert messages[0]["content"].count("/no_think") == 1


def test_message_directive_handles_multipart_content():
    """Vision turns carry a list of parts, not a string."""
    messages = [{"role": "user", "content": [
        {"type": "text", "text": "what is this"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,x"}},
    ]}]
    apply_reasoning_control(
        {}, provider="openai", model="deepseek-v4-flash-free",
        url="https://api.example.com/v1", preference=PREF_OFF, messages=messages,
    )
    assert messages[0]["content"][0]["text"].endswith("/no_think")
    assert messages[0]["content"][1]["type"] == "image_url"


# --- relay-served reasoning models: optimistic effort, self-healing ---------

def test_relay_served_reasoning_model_gets_graded_levels():
    """`reasoning_effort` is the de-facto standard across OpenAI-compatible
    gateways, so a relay-served reasoning model offers the full scale rather
    than the on/off the soft directive alone can express."""
    import src.reasoning_control as rc
    rc._REJECTED.clear()
    control = resolve_reasoning_control("openai", "deepseek-v4-flash-free",
                                        "https://relay.test/v1")
    assert control.mechanism == REASONING_CONTROL_EFFORT
    assert control.supports_effort
    assert set(control.supported) >= {PREF_LOW, PREF_MEDIUM, PREF_HIGH}


def test_off_on_a_relay_also_sends_the_soft_directive():
    """Belt and braces: a gateway that silently DROPS reasoning_effort (rather
    than erroring) would otherwise keep thinking with no way to stop it."""
    import src.reasoning_control as rc
    rc._REJECTED.clear()
    payload = {}
    messages = [{"role": "user", "content": "hi"}]
    apply_reasoning_control(payload, provider="openai", model="deepseek-v4-flash-free",
                            url="https://relay.test/v1", preference=PREF_OFF,
                            messages=messages)
    assert payload == {"reasoning_effort": "none"}
    assert messages[0]["content"].endswith("/no_think")


def test_a_rejecting_endpoint_falls_back_to_the_directive():
    """A strict OpenAI-shaped API 400s on unknown parameters. One failure must
    be enough to stop us sending it — not one per message."""
    import src.reasoning_control as rc
    from src.llm_core import _note_reasoning_rejection
    rc._REJECTED.clear()
    url = "https://strict.test/v1"
    assert resolve_reasoning_control("openai", "deepseek-v4", url).supports_effort

    # Exactly the call the shared error formatter makes — note: no model arg.
    _note_reasoning_rejection(
        400, '{"error":{"message":"Unsupported parameter: reasoning_effort"}}', url)

    after = resolve_reasoning_control("openai", "deepseek-v4", url)
    assert after.mechanism == REASONING_CONTROL_MESSAGE_DIRECTIVE
    assert not after.supports_effort
    # Scoped to that endpoint only.
    assert resolve_reasoning_control("openai", "deepseek-v4", "https://other.test/v1").supports_effort


def test_rejection_key_normalizes_chat_completion_suffix():
    import src.reasoning_control as rc
    rc._REJECTED.clear()
    rc.note_reasoning_param_rejected("https://strict.test/v1/chat/completions")
    control = resolve_reasoning_control("openai", "deepseek-v4", "https://strict.test/v1")
    assert control.mechanism == REASONING_CONTROL_MESSAGE_DIRECTIVE


def test_unrelated_400s_do_not_disable_effort():
    import src.reasoning_control as rc
    from src.llm_core import _note_reasoning_rejection
    rc._REJECTED.clear()
    url = "https://fine.test/v1"
    for body in ('{"error":{"message":"Invalid model"}}',
                 '{"error":{"message":"context length exceeded"}}',
                 ''):
        _note_reasoning_rejection(400, body, url)
    assert resolve_reasoning_control("openai", "deepseek-v4", url).supports_effort


def test_non_400_statuses_are_ignored():
    import src.reasoning_control as rc
    from src.llm_core import _note_reasoning_rejection
    rc._REJECTED.clear()
    url = "https://five-hundred.test/v1"
    _note_reasoning_rejection(500, 'unsupported parameter: reasoning_effort', url)
    assert resolve_reasoning_control("openai", "deepseek-v4", url).supports_effort


# --- preference plumbing ----------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("", PREF_AUTO), (None, PREF_AUTO), ("provider", PREF_AUTO),
    ("none", PREF_OFF), ("disabled", PREF_OFF), ("false", PREF_OFF),
    ("HIGH", PREF_HIGH), ("max", PREF_HIGH), ("min", PREF_LOW),
    ("nonsense", PREF_AUTO),
])
def test_preference_normalization(raw, expected):
    assert normalize_preference(raw) == expected


def test_per_model_override_beats_the_default():
    settings = {
        "reasoning_effort_default": "high",
        "reasoning_effort_by_model": {"gpt-5": "off"},
    }
    assert effective_preference("gpt-5", settings) == PREF_OFF
    assert effective_preference("claude-opus-4", settings) == PREF_HIGH


def test_per_model_override_is_case_insensitive():
    settings = {"reasoning_effort_by_model": {"GPT-5": "low"}}
    assert effective_preference("gpt-5", settings) == PREF_LOW


def test_missing_settings_degrade_to_auto():
    assert effective_preference("gpt-5", {}) == PREF_AUTO
    assert effective_preference("gpt-5", {"reasoning_effort_by_model": "bogus"}) == PREF_AUTO
