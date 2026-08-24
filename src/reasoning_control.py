"""Resolve and apply a model's reasoning/thinking control.

`model_capabilities.py` already defines the taxonomy — seven mechanisms, because
vendors do not agree on one wire format — but nothing consumed it. Reasoning was
steered by three hardcoded special cases in `llm_core` gated on
`_supports_thinking()`, a substring match over 13 model-name fragments. That
misses most models outright (deepseek-v4-flash, gpt-5, claude-*, gemini-* all
scored False, so they got no control at all) and picks the WRONG mechanism for
others: Qwen3.6 served by llama.cpp was sent `/no_think` in the prompt text,
ignored it, and generated 5096 tokens.

This module maps (provider, model, url) -> the mechanism that model actually
honours, and applies a user preference through it.

Preference values:
    auto             leave the serving path's own default (see AUTO below)
    off              ask for no reasoning at all
    low/medium/high  graded effort, for models that grade it

AUTO is deliberately not "send nothing". It reproduces the behaviour that was
hardcoded before this module existed, so upgrading changes nothing until a user
picks a level:
  * Ollama's OpenAI-compat /v1 gets `think: false` for thinking models — without
    it, tool calls get swallowed inside <think> blocks.
  * Mistral thinking models get their configured effort (default "high").
Everything else sends nothing under auto, exactly as before.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from src.model_capabilities import (
    REASONING_CONTROL_BUDGET,
    REASONING_CONTROL_EFFORT,
    REASONING_CONTROL_MESSAGE_DIRECTIVE,
    REASONING_CONTROL_NATIVE_BOOL,
    REASONING_CONTROL_STRUCTURED_OBJECT,
    REASONING_CONTROL_TEMPLATE_KWARG,
)

# User-facing preference values.
PREF_AUTO = "auto"
PREF_OFF = "off"
PREF_LOW = "low"
PREF_MEDIUM = "medium"
PREF_HIGH = "high"
PREFERENCES = (PREF_AUTO, PREF_OFF, PREF_LOW, PREF_MEDIUM, PREF_HIGH)
_GRADED = (PREF_LOW, PREF_MEDIUM, PREF_HIGH)

_MISTRAL_EFFORT_DEFAULT = os.getenv("ODYSSEUS_MISTRAL_REASONING_EFFORT", "high")


@dataclass(frozen=True)
class ReasoningControl:
    """How one model accepts reasoning control."""

    mechanism: str = ""
    # Which preferences this model can actually honour. A boolean mechanism
    # cannot grade effort, and the UI must not offer levels it will ignore.
    supported: tuple = field(default_factory=tuple)
    # Populated for REASONING_CONTROL_BUDGET: token budget per graded level.
    budgets: Optional[Dict[str, int]] = None
    # Also append the /no_think soft directive when switching reasoning off, for
    # gateways that silently drop reasoning_effort instead of honouring it.
    directive_fallback: bool = False

    @property
    def supports_effort(self) -> bool:
        return any(p in self.supported for p in _GRADED)

    @property
    def can_disable(self) -> bool:
        return PREF_OFF in self.supported

    def to_dict(self) -> dict:
        return {
            "mechanism": self.mechanism,
            "supported": list(self.supported),
            "supports_effort": self.supports_effort,
            "can_disable": self.can_disable,
        }


NO_CONTROL = ReasoningControl()

# Graded effort, native to the API.
_EFFORT_ONLY = ReasoningControl(
    mechanism=REASONING_CONTROL_EFFORT,
    supported=(PREF_AUTO, PREF_OFF, PREF_LOW, PREF_MEDIUM, PREF_HIGH),
)
_EFFORT_NO_OFF = ReasoningControl(
    mechanism=REASONING_CONTROL_EFFORT,
    supported=(PREF_AUTO, PREF_LOW, PREF_MEDIUM, PREF_HIGH),
)
# On/off only — no gradations exist on the wire.
_BOOL_ONLY = ReasoningControl(
    mechanism=REASONING_CONTROL_NATIVE_BOOL,
    supported=(PREF_AUTO, PREF_OFF),
)
_TEMPLATE_BOOL = ReasoningControl(
    mechanism=REASONING_CONTROL_TEMPLATE_KWARG,
    supported=(PREF_AUTO, PREF_OFF),
)
_DIRECTIVE_BOOL = ReasoningControl(
    mechanism=REASONING_CONTROL_MESSAGE_DIRECTIVE,
    supported=(PREF_AUTO, PREF_OFF),
)
_ANTHROPIC_MANUAL = ReasoningControl(
    mechanism=REASONING_CONTROL_STRUCTURED_OBJECT,
    supported=(PREF_AUTO, PREF_OFF, PREF_LOW, PREF_MEDIUM, PREF_HIGH),
    budgets={PREF_LOW: 4096, PREF_MEDIUM: 16384, PREF_HIGH: 32768},
)
_ANTHROPIC_ADAPTIVE = ReasoningControl(
    mechanism=REASONING_CONTROL_STRUCTURED_OBJECT,
    supported=(PREF_AUTO, PREF_OFF, PREF_LOW, PREF_MEDIUM, PREF_HIGH),
)
_ANTHROPIC_ALWAYS_ON = ReasoningControl(
    mechanism=REASONING_CONTROL_STRUCTURED_OBJECT,
    supported=(PREF_AUTO, PREF_LOW, PREF_MEDIUM, PREF_HIGH),
)
_OLLAMA_LEVELS = ReasoningControl(
    mechanism=REASONING_CONTROL_NATIVE_BOOL,
    supported=(PREF_AUTO, PREF_LOW, PREF_MEDIUM, PREF_HIGH),
)
# Relay-served reasoning models: send reasoning_effort (widely accepted by
# OpenAI-compatible gateways) and ALSO the soft /no_think directive when the
# choice is "off", so the intent lands even if the gateway drops the field.
_EFFORT_WITH_DIRECTIVE_FALLBACK = ReasoningControl(
    mechanism=REASONING_CONTROL_EFFORT,
    supported=(PREF_AUTO, PREF_OFF, PREF_LOW, PREF_MEDIUM, PREF_HIGH),
    directive_fallback=True,
)

# Model-name families that reason, keyed to the mechanism their SERVING PATH
# accepts. Substring match, like the heuristic it replaces, but the mechanism is
# resolved from the provider first — the same Qwen3 weights take a template
# kwarg on vLLM/llama.cpp and `think` on Ollama.
_THINKING_FAMILIES = (
    "qwen3", "qwq", "deepseek-r1", "deepseek-reasoner", "deepseek-v3", "deepseek-v4",
    "minimax", "m2-reap", "gemma", "stepfun", "step-3", "step3",
    "magistral", "mistral-small", "mistral-medium", "glm", "kimi", "ernie",
    "nemotron", "exaone", "phi-4-reasoning", "granite",
)

# OpenAI-style graded effort.
_EFFORT_FAMILIES = ("gpt-5", "gpt-6", "o1", "o3", "o4", "grok-3", "grok-4", "grok-5")


def _is_family(model: str, families) -> bool:
    m = (model or "").lower()
    return any(f in m for f in families)


def _is_google_openai_compat(url: str, model: str) -> bool:
    """Gemini served through Google's OpenAI-compatible API.

    The HTTP transport must remain the generic OpenAI path, but reasoning
    controls still follow Gemini's model-specific capabilities.
    """
    if "gemini" not in (model or "").lower():
        return False
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url or "")
    except Exception:
        return False
    host = (parsed.hostname or "").lower()
    path_parts = {part.lower() for part in (parsed.path or "").split("/") if part}
    return host == "generativelanguage.googleapis.com" and "openai" in path_parts


def _anthropic_control(model: str) -> ReasoningControl:
    m = (model or "").lower()
    if not m.startswith("claude-"):
        return NO_CONTROL
    if any(name in m for name in ("fable-5", "mythos-5", "mythos-preview")):
        return _ANTHROPIC_ALWAYS_ON
    if re.search(r"claude-(?:opus|sonnet)-(?:5(?:\D|$)|4[-.]?(?:6|7|8)(?:\D|$))", m):
        return _ANTHROPIC_ADAPTIVE
    if "3-7" in m or "3.7" in m or re.search(r"claude-(?:opus|sonnet|haiku)-4", m):
        return _ANTHROPIC_MANUAL
    return NO_CONTROL


def _google_control(model: str) -> ReasoningControl:
    m = (model or "").lower()
    if "gemini-2.5" not in m and not re.search(r"gemini-(?:3|4)(?:\D|$)", m):
        return NO_CONTROL
    # Gemini's OpenAI-compatible endpoint accepts reasoning_effort. Gemini 2.5
    # Flash variants can disable thinking; 2.5 Pro and Gemini 3+ cannot.
    if "gemini-2.5" in m and "flash" in m:
        return _EFFORT_ONLY
    return _EFFORT_NO_OFF


def _openai_effort_control(model: str) -> ReasoningControl:
    m = (model or "").lower()
    if "gpt-5-pro" in m:
        return NO_CONTROL
    if re.search(r"gpt-(?:[6-9](?:\D|$)|5[.-][1-9](?:\D|$))", m):
        return _EFFORT_ONLY
    return _EFFORT_NO_OFF


def _is_ollama_openai_compat_url(url: str) -> bool:
    """Local Ollama's OpenAI-compatible /v1 surface.

    Mirrors llm_core's helper of the same name. Note it also matches any OTHER
    local server on /v1 (llama.cpp, vLLM) — the URL alone cannot tell them
    apart. That imprecision is deliberate and pre-existing: `think` is simply
    ignored by a server that does not implement it.
    """
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url or "")
    except Exception:
        return False
    host = parsed.hostname or ""
    path = (parsed.path or "").rstrip("/")
    local = host in {"localhost", "127.0.0.1", "0.0.0.0", "::1"} or parsed.port == 11434
    return local and (path == "/v1" or path.startswith("/v1/"))


def _looks_local_openai_compat(url: str) -> bool:
    """A self-hosted OpenAI-compatible server (vLLM, llama.cpp, LM Studio, TGI).

    These take chat-template kwargs; hosted APIs reject unknown body fields.
    """
    u = (url or "").lower()
    if not u:
        return False
    if re.search(r"//(localhost|127\.0\.0\.1|0\.0\.0\.0|host\.docker\.internal|\[::1\])[:/]", u):
        return True
    # RFC1918 / link-local — a LAN box serving models.
    return bool(re.search(r"//(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|169\.254\.)", u))


def _is_self_hosted_openai_compat(url: str, endpoint_kind: str = "auto") -> bool:
    kind = str(endpoint_kind or "auto").strip().lower()
    if kind == "local":
        return True
    if kind in ("api", "proxy"):
        return False
    return _looks_local_openai_compat(url)


# Endpoints that answered 400 because they don't accept the reasoning parameter
# we sent. Keyed by ENDPOINT, not endpoint+model: whether `reasoning_effort` is
# a recognised field is a property of the API surface, and the error path that
# records it (llm_core._format_upstream_error) has the URL but not the model.
# The resolver then stops offering graded effort there, so a wrong guess
# self-corrects after one failure instead of breaking every message.
_REJECTED: set = set()


def _endpoint_key(url: str) -> str:
    key = str(url or "").rstrip("/").lower()
    for suffix in ("/chat/completions", "/completions", "/v1/messages", "/messages"):
        if key.endswith(suffix):
            key = key[: -len(suffix)].rstrip("/")
            break
    return key


def note_reasoning_param_rejected(url: str, model: str = "") -> None:
    _REJECTED.add(_endpoint_key(url))


def reasoning_param_rejected(url: str, model: str = "") -> bool:
    return _endpoint_key(url) in _REJECTED


def resolve_reasoning_control(
    provider: str,
    model: str,
    url: str = "",
    endpoint_kind: str = "auto",
) -> ReasoningControl:
    """Which reasoning-control mechanism this (provider, model, url) honours."""
    provider = (provider or "").lower()

    if provider == "chatgpt-subscription":
        return NO_CONTROL
    if provider == "anthropic":
        return _anthropic_control(model)
    if provider in ("google", "gemini") or _is_google_openai_compat(url, model):
        return _google_control(model)
    if provider == "mistral":
        return _EFFORT_ONLY if _is_family(model, _THINKING_FAMILIES) else NO_CONTROL
    if provider == "ollama":
        # Ollama accepts a top-level boolean on both its native and /v1 routes.
        if "gpt-oss" in (model or "").lower():
            return _OLLAMA_LEVELS
        return _BOOL_ONLY if _is_family(model, _THINKING_FAMILIES) else NO_CONTROL

    # NOTE: this check must come BEFORE the `provider == "openai"` branch.
    # _detect_provider() labels every OpenAI-compatible endpoint "openai",
    # including a llama.cpp or vLLM server on localhost — so keying purely off
    # the provider sends a self-hosted Qwen down the hosted-OpenAI path and it
    # ends up with no control at all. Self-hosted servers pass
    # chat_template_kwargs through to the chat template, which is how
    # Qwen/GLM/DeepSeek actually switch thinking off.
    if _is_self_hosted_openai_compat(url, endpoint_kind) and _is_family(model, _THINKING_FAMILIES):
        return _TEMPLATE_BOOL

    # Deliberately not an early return for the non-effort case: "openai" is also
    # what _detect_provider reports for third-party OpenAI-compatible relays,
    # which serve Qwen/DeepSeek/GLM. Returning NO_CONTROL here would leave every
    # relay-served thinking model uncontrollable — the deepseek-v4-flash case.
    if _is_family(model, _EFFORT_FAMILIES):
        return _openai_effort_control(model)
    if _is_family(model, _THINKING_FAMILIES):
        # Relay-served reasoning model. `reasoning_effort` is the de-facto
        # standard across OpenAI-compatible gateways (OpenRouter, LiteLLM, vLLM)
        # so offer the graded levels — but strictly OpenAI-shaped APIs answer
        # 400 to parameters they don't know, so back off to the soft directive
        # once an endpoint has actually rejected it.
        if provider == "openai" and not reasoning_param_rejected(url, model):
            return _EFFORT_WITH_DIRECTIVE_FALLBACK
        return _DIRECTIVE_BOOL
    return NO_CONTROL


def normalize_preference(value: Any) -> str:
    token = str(value or "").strip().lower()
    if token in ("", "default", "provider"):
        return PREF_AUTO
    if token in ("none", "disabled", "no", "false"):
        return PREF_OFF
    if token in ("min", "minimal"):
        return PREF_LOW
    if token in ("max", "maximum"):
        return PREF_HIGH
    return token if token in PREFERENCES else PREF_AUTO


def effective_preference(model: str, settings: Optional[dict] = None) -> str:
    """The user's choice for `model`: per-model override, else global default."""
    if settings is None:
        try:
            from src.settings import load_settings
            settings = load_settings()
        except Exception:
            settings = {}
    per_model = settings.get("reasoning_effort_by_model") or {}
    if isinstance(per_model, dict):
        for key, value in per_model.items():
            if key and str(key).lower() == str(model or "").lower():
                return normalize_preference(value)
    return normalize_preference(settings.get("reasoning_effort_default"))


def apply_reasoning_control(
    payload: dict,
    *,
    provider: str,
    model: str,
    url: str = "",
    endpoint_kind: str = "auto",
    preference: Optional[str] = None,
    messages: Optional[List[dict]] = None,
) -> str:
    """Apply the resolved control to `payload` in place.

    Returns the preference that was applied ("auto" when nothing was written by
    an explicit choice). `messages` is only needed for the message-directive
    mechanism, which edits the last user turn.
    """
    pref = normalize_preference(preference if preference is not None else effective_preference(model))
    control = resolve_reasoning_control(provider, model, url, endpoint_kind)

    if pref == PREF_AUTO or not control.mechanism:
        _apply_auto_defaults(
            payload,
            provider=provider,
            model=model,
            url=url,
            endpoint_kind=endpoint_kind,
            control=control,
        )
        return PREF_AUTO

    # Never send a value outside this model's actual control surface. For
    # example, GPT-5 predates `none`, Gemini Pro cannot turn thinking off, and
    # boolean-only models cannot honour low/medium/high.
    if pref not in control.supported:
        return PREF_AUTO

    mech = control.mechanism
    if mech == REASONING_CONTROL_EFFORT:
        payload["reasoning_effort"] = "none" if pref == PREF_OFF else pref
        if control.directive_fallback and pref == PREF_OFF:
            _apply_message_directive(messages, on=False)
    elif mech == REASONING_CONTROL_NATIVE_BOOL:
        payload["think"] = pref if control.supports_effort else pref != PREF_OFF
    elif mech == REASONING_CONTROL_TEMPLATE_KWARG:
        kwargs = payload.setdefault("chat_template_kwargs", {})
        if isinstance(kwargs, dict):
            kwargs["enable_thinking"] = pref != PREF_OFF
        # A local /v1 URL could be llama.cpp/vLLM (template kwarg) or Ollama
        # (`think` boolean) — nothing in the URL distinguishes them. Send both;
        # each server ignores the field it doesn't implement, and the
        # alternative is the control silently doing nothing on one of them.
        if _is_ollama_openai_compat_url(url):
            payload["think"] = pref != PREF_OFF
    elif mech == REASONING_CONTROL_STRUCTURED_OBJECT:
        if control is _ANTHROPIC_ADAPTIVE or control is _ANTHROPIC_ALWAYS_ON:
            if pref == PREF_OFF:
                payload["thinking"] = {"type": "disabled"}
            else:
                payload["thinking"] = {"type": "adaptive"}
                payload.setdefault("output_config", {})["effort"] = pref
                payload.pop("temperature", None)
        elif pref == PREF_OFF:
            # Manual extended thinking is off when the field is absent.
            payload.pop("thinking", None)
        else:
            budget = (control.budgets or {}).get(pref, 8192)
            max_tokens = int(payload.get("max_tokens") or 4096)
            budget = min(budget, max(1024, max_tokens - 1024))
            payload["thinking"] = {"type": "enabled", "budget_tokens": budget}
            payload.pop("temperature", None)
    elif mech == REASONING_CONTROL_BUDGET:
        budget = 0 if pref == PREF_OFF else (control.budgets or {}).get(pref, 8192)
        payload["thinking_budget"] = budget
    elif mech == REASONING_CONTROL_MESSAGE_DIRECTIVE:
        _apply_message_directive(messages, on=pref != PREF_OFF)
    return pref


def _apply_auto_defaults(payload: dict, *, provider: str, model: str, url: str,
                         endpoint_kind: str = "auto",
                         control: ReasoningControl) -> None:
    """Reproduce the behaviour that was hardcoded in llm_core before this module.

    Kept so that shipping this feature changes nothing for users who never touch
    the setting.
    """
    if provider == "mistral" and control.mechanism == REASONING_CONTROL_EFFORT:
        payload.setdefault("reasoning_effort", _MISTRAL_EFFORT_DEFAULT)
        return
    # Keyed off the URL, not the provider: Ollama's /v1 surface is reported as
    # provider "openai" by _detect_provider, so a provider check silently
    # dropped `think: false` — and without it tool calls get swallowed inside
    # <think> blocks. This is the exact condition llm_core used before.
    if (
        _is_ollama_openai_compat_url(url)
        and _is_self_hosted_openai_compat(url, endpoint_kind)
        and _is_family(model, _THINKING_FAMILIES)
    ):
        payload.setdefault("think", False)
    elif (
        provider == "ollama"
        and control.mechanism == REASONING_CONTROL_NATIVE_BOOL
        and control.can_disable
    ):
        payload.setdefault("think", False)


def _apply_message_directive(messages: Optional[List[dict]], *, on: bool) -> None:
    """Append /think or /no_think to the last user message."""
    if not messages:
        return
    directive = "/think" if on else "/no_think"
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            if "/no_think" in content or "/think" in content:
                return
            msg["content"] = f"{content.rstrip()} {directive}"
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    text = part.get("text") or ""
                    if "/no_think" in text or "/think" in text:
                        return
                    part["text"] = f"{text.rstrip()} {directive}"
                    break
        return
