"""Subprocess environment scrubbing for agent-spawned shells.

Ported from Hermes (tools/environments/local.py ``_sanitize_subprocess_env``
+ code_execution's secret rules). The agent's bash/python subprocesses used
to inherit the FULL server process environment — every provider API key,
bot token, and .env secret was one ``env`` command away from a model-authored
shell. This module strips Aegis-managed secrets while leaving ordinary user
environment intact, so normal builds/tools keep working.

Rules (order matters):
  1. Passthrough vars (settings ``bash_env_passthrough`` list) always pass —
     the explicit opt-in escape hatch for workflows that need a key.
  2. Tier-1 exact-name secrets are stripped unconditionally.
  3. Well-known LLM/provider credential names are stripped.
  4. Dynamic-name internal secrets (``*_API_KEY``-style names on Aegis
     prefixes, DSNs) are stripped.
  5. Active-virtualenv markers are stripped: the server runs inside its own
     venv, and a leaked VIRTUAL_ENV/CONDA_PREFIX makes uv/poetry build OTHER
     projects' dependencies into the server venv (Hermes #23473).
"""

from __future__ import annotations

import logging
import os
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# Tier-1 secrets: stripped from EVERY spawned subprocess unconditionally.
# These are not something a model-authored shell command legitimately needs,
# and they are the highest-value secrets to keep out of a compromised
# dependency's reach.
_ALWAYS_STRIP_KEYS = frozenset({
    # GitHub auth
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GITHUB_APP_ID",
    "GITHUB_APP_PRIVATE_KEY_PATH",
    "GITHUB_APP_INSTALLATION_ID",
    # Messaging bot tokens
    "TELEGRAM_BOT_TOKEN",
    "DISCORD_BOT_TOKEN",
    "SLACK_BOT_TOKEN",
    "SLACK_APP_TOKEN",
    "SLACK_SIGNING_SECRET",
    # Email + infrastructure
    "EMAIL_PASSWORD",
    "DATABASE_URL",       # may carry a DSN password; sqlite paths lose nothing
    "MODAL_TOKEN_ID",
    "MODAL_TOKEN_SECRET",
    "DAYTONA_API_KEY",
})

# Well-known LLM / provider credential names. Hermes derives this from its
# provider registry; Aegis endpoints keep keys in the encrypted DB, but users
# commonly export these into .env / the shell that launches the server.
_PROVIDER_KEY_NAMES = frozenset({
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENROUTER_API_KEY",
    "GROQ_API_KEY",
    "MISTRAL_API_KEY",
    "DEEPSEEK_API_KEY",
    "TOGETHER_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "XAI_API_KEY",
    "COHERE_API_KEY",
    "FIREWORKS_API_KEY",
    "PERPLEXITY_API_KEY",
    "HUGGINGFACE_TOKEN",
    "HF_TOKEN",
    "REPLICATE_API_TOKEN",
    "ELEVENLABS_API_KEY",
    "FAL_KEY",
    "EMBEDDING_API_KEY",
})

# Active-virtualenv markers that must NOT leak into agent subprocesses.
_ACTIVE_VENV_MARKER_VARS = ("VIRTUAL_ENV", "CONDA_PREFIX")

_SECRET_SUFFIXES = ("_API_KEY", "_SECRET", "_TOKEN", "_PASSWORD", "_DSN")
_INTERNAL_PREFIXES = ("AEGIS_", "ODYSSEUS_")


def _is_internal_secret(key: str) -> bool:
    """Dynamic-name internal secrets no static list can enumerate.

    Any Aegis/Odysseus-prefixed var with a credential suffix is stripped
    unconditionally — mirrors Hermes ``_is_hermes_internal_secret``, which
    closes the gap between the name-based blocklist and secrets injected
    under runtime-constructed names.
    """
    upper = key.upper()
    if upper.startswith(_INTERNAL_PREFIXES) and upper.endswith(_SECRET_SUFFIXES):
        return True
    return False


def _passthrough_names() -> frozenset:
    """Explicit opt-in names from settings ``bash_env_passthrough``."""
    try:
        from src.settings import get_setting
        names = get_setting("bash_env_passthrough", None) or []
        return frozenset(str(n).strip() for n in names if str(n).strip())
    except Exception:
        return frozenset()


def scrub_subprocess_env(base_env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Build a sanitized environment dict for an agent-spawned subprocess.

    Use this instead of copying ``os.environ`` directly.
    """
    env = dict(base_env if base_env is not None else os.environ)
    passthrough = _passthrough_names()

    sanitized: Dict[str, str] = {}
    stripped = []
    for key, value in env.items():
        if key in passthrough:
            sanitized[key] = value
            continue
        if key in _ALWAYS_STRIP_KEYS or key in _PROVIDER_KEY_NAMES:
            stripped.append(key)
            continue
        if _is_internal_secret(key):
            stripped.append(key)
            continue
        sanitized[key] = value

    for marker in _ACTIVE_VENV_MARKER_VARS:
        sanitized.pop(marker, None)

    if stripped:
        logger.debug(
            "Stripped %d secret env var(s) from agent subprocess env (%s). "
            "Declare a name in settings bash_env_passthrough if a workflow "
            "legitimately needs it.",
            len(stripped), ", ".join(sorted(stripped)),
        )
    return sanitized
