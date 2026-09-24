"""Read-only context accounting for the composer (no prompt-building side effects)."""

from src.context_compactor import resolve_compact_trigger
from src.model_context import estimate_tokens, get_context_length_known


def session_context_usage(session):
    context_length, known = get_context_length_known(session.endpoint_url, session.model)
    messages = session.get_context_messages()
    pct, token_cap = resolve_compact_trigger()
    result = {
        "model": session.model,
        "context_length": context_length if known else None,
        "context_length_known": known,
        "used_tokens": estimate_tokens(messages),
        "usage_source": "estimated",
        "basis": "history",
        "compact_threshold": pct / 100 if pct else None,
        "compact_token_cap": token_cap,
        "compacted": any((m.get("metadata") or {}).get("compacted") for m in messages),
    }
    # Only the most recent assistant request can be reused. Never sum the
    # cumulative billing input_tokens from a multi-round agent turn.
    latest = messages[-1] if messages else {}
    metadata = latest.get("metadata") or {}
    context_tokens = metadata.get("context_tokens")
    output_tokens = metadata.get("context_output_tokens", 0)
    # Stored zeros are not measurements: providers report 0/0 on empty,
    # error-adjacent or otherwise uncounted turns, and accepting them pins
    # the meter at 0 until the next turn. Fall back to the history estimate,
    # which is positive for any non-empty history.
    if (latest.get("role") == "assistant" and metadata.get("model") == session.model
            and isinstance(context_tokens, (int, float)) and context_tokens > 0
            and isinstance(output_tokens, (int, float)) and output_tokens >= 0):
        result.update(used_tokens=context_tokens + output_tokens,
                      usage_source=metadata.get("context_usage_source", metadata.get("usage_source", "real")), basis="request",
                      trimmed=bool(metadata.get("context_trimmed")))
    elif (latest.get("role") == "assistant" and metadata.get("requested_model") == session.model
          and metadata.get("model") != session.model
          and isinstance(context_tokens, (int, float)) and context_tokens > 0):
        # A fallback's request count cannot be divided by the selected model's
        # window. Keep the composer on saved history, but expose the last
        # reply's input count so the two meters can be explained together.
        result["last_request"] = {
            "input_tokens": context_tokens,
            "model": metadata.get("model"),
            "context_percent": metadata.get("context_percent"),
        }
    return result
