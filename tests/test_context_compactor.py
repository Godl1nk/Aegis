"""Tests for context_compactor.py — constants and prompt templates.
Uses mock imports to avoid loading the full app stack."""

import asyncio
import sys
from unittest.mock import MagicMock

import pytest

# Mock heavy dependencies before importing
for mod in [
    'sqlalchemy', 'sqlalchemy.orm', 'sqlalchemy.ext', 'sqlalchemy.ext.declarative',
    'sqlalchemy.ext.hybrid', 'sqlalchemy.sql', 'sqlalchemy.sql.expression',
    'src.database',
    'core.models', 'core.database',
]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

import src.context_compactor as cc
from src.context_compactor import (
    COMPACT_THRESHOLD,
    SELF_SUMMARY_SYSTEM_PROMPT,
    SUMMARY_MAX_TOKENS,
    _content_as_text,
    maybe_compact,
    trim_for_context,
)


class TestCompactThreshold:
    def test_value(self):
        assert COMPACT_THRESHOLD == 0.85

    def test_summary_max_tokens(self):
        assert SUMMARY_MAX_TOKENS == 1024


class TestSelfSummaryPrompt:
    def test_contains_goal_section(self):
        assert "### User Goal" in SELF_SUMMARY_SYSTEM_PROMPT

    def test_contains_what_was_done_section(self):
        assert "### What Was Done" in SELF_SUMMARY_SYSTEM_PROMPT

    def test_contains_current_state_section(self):
        assert "### Current State" in SELF_SUMMARY_SYSTEM_PROMPT

    def test_contains_pending_section(self):
        assert "### Pending / Next Steps" in SELF_SUMMARY_SYSTEM_PROMPT

    def test_contains_key_context_section(self):
        assert "### Key Context" in SELF_SUMMARY_SYSTEM_PROMPT

    def test_count_placeholder(self):
        assert "{count}" in SELF_SUMMARY_SYSTEM_PROMPT

    def test_n_placeholder(self):
        assert "{n}" in SELF_SUMMARY_SYSTEM_PROMPT

    def test_mentions_compactions(self):
        assert "Compactions so far" in SELF_SUMMARY_SYSTEM_PROMPT


class TestTrimForContext:
    def test_keeps_current_large_user_message_by_truncating(self):
        huge = "A" * 20000
        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": huge},
        ]

        trimmed = trim_for_context(messages, context_length=2048, reserve_tokens=512)

        user_msgs = [m for m in trimmed if m.get("role") == "user"]
        assert len(user_msgs) == 1
        content = user_msgs[0]["content"]
        assert "pasted message was too large" in content
        assert content.startswith("A")
        assert len(content) < len(huge)

    def test_drops_older_messages_before_latest_user_paste(self):
        huge = "B" * 12000
        messages = [{"role": "system", "content": "You are helpful."}]
        messages.extend({"role": "user", "content": f"old-{i} " + ("x" * 1000)} for i in range(8))
        messages.append({"role": "user", "content": huge})

        trimmed = trim_for_context(messages, context_length=2048, reserve_tokens=512)

        assert trimmed[-1]["role"] == "user"
        assert "pasted message was too large" in trimmed[-1]["content"]
        assert "old-0" not in "\n".join(str(m.get("content", "")) for m in trimmed)


class TestContentAsText:
    def test_string_passthrough(self):
        assert _content_as_text("hello") == "hello"

    def test_none_returns_empty(self):
        # Assistant turns that carried only native tool_calls persist
        # content as None — flattening must not raise.
        assert _content_as_text(None) == ""

    def test_list_content_joins_text_blocks(self):
        content = [
            {"type": "text", "text": "describe this"},
            {"type": "image_url", "image_url": {"url": "data:..."}},
        ]
        assert _content_as_text(content) == "describe this"

    def test_unknown_type_returns_empty(self):
        assert _content_as_text(42) == ""


class TestMaybeCompactFourthMessage:
    """Regression: a multi-message conversation must not crash compaction when
    a prior assistant turn used native tool_calls (content == None). This was
    the '4th message stops working' bug — on a small-context model the soft
    85% threshold is crossed after a few turns, and the older half being
    summarized contained a None-content assistant message, which raised
    TypeError: 'NoneType' object is not subscriptable and broke the request."""

    def _run(self, messages, *, context_length=500):
        # Force compaction to trigger and stub the summary LLM call so the test
        # is hermetic (no network, no real endpoint resolution).
        orig_ctx = cc.get_context_length
        orig_known = cc.get_context_length_known
        orig_call = cc.llm_call_async
        orig_resolve = cc.resolve_endpoint
        orig_update = cc._update_session_history

        async def _fake_summary(*a, **k):
            return "compact summary text"

        cc.get_context_length = lambda url, model: context_length
        cc.get_context_length_known = lambda url, model: (context_length, True)
        cc.llm_call_async = _fake_summary
        cc.resolve_endpoint = lambda which, owner=None: (None, None, None)
        cc._update_session_history = lambda *a, **k: None
        try:
            return asyncio.run(
                maybe_compact(
                    session=None,
                    endpoint_url="http://local/v1/chat/completions",
                    model="local-model",
                    messages=list(messages),
                    headers={},
                )
            )
        finally:
            cc.get_context_length = orig_ctx
            cc.get_context_length_known = orig_known
            cc.llm_call_async = orig_call
            cc.resolve_endpoint = orig_resolve
            cc._update_session_history = orig_update

    def _four_turn_history_with_tool_call(self):
        # Large system prompt so the conversation crosses the 85% threshold of
        # the tiny (context_length=500) window used in _run, forcing the real
        # compaction branch to execute.
        return [
            {"role": "system", "content": "You are a helpful agent. " * 200},
            {"role": "user", "content": "turn 1: search the web"},
            # Native tool call → content is None (matches agent_loop persistence)
            {"role": "assistant", "content": None,
             "tool_calls": [{"id": "c1", "type": "function",
                             "function": {"name": "web_search", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "search results"},
            {"role": "assistant", "content": "Here is what I found."},
            {"role": "user", "content": "turn 2"},
            {"role": "assistant", "content": "reply 2"},
            {"role": "user", "content": "turn 3"},
            {"role": "assistant", "content": "reply 3"},
            {"role": "user", "content": "turn 4 — previously broke here"},
        ]

    def test_does_not_crash_on_none_content_turn(self):
        # Must not raise TypeError; returns the 3-tuple contract.
        result = self._run(self._four_turn_history_with_tool_call())
        assert isinstance(result, tuple) and len(result) == 3
        compacted_messages, context_length, was_compacted = result
        assert isinstance(compacted_messages, list)
        assert was_compacted is True
        # The summary the model produced is present and a system message.
        assert any(
            m.get("role") == "system" and "compact summary text" in (m.get("content") or "")
            for m in compacted_messages
        )

    def test_handles_multimodal_list_content(self):
        messages = self._four_turn_history_with_tool_call()
        messages[1] = {"role": "user", "content": [
            {"type": "text", "text": "look at this image"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,xxxx"}},
        ]}
        result = self._run(messages)
        assert len(result) == 3 and result[2] is True


class TestConfigurableTrigger:
    """The auto-compact gate reads percent + absolute token cap from settings
    (Hermes/pi-style configurability instead of a hardcoded 85%)."""

    def _run_cfg(self, monkeypatch, messages, *, length=1000, known=True, pct=85, tok=0):
        vals = {"auto_compact_percent": pct, "auto_compact_tokens": tok}
        monkeypatch.setattr(cc, "get_setting", lambda key, default=None: vals.get(key, default))
        monkeypatch.setattr(cc, "get_context_length_known", lambda *a: (length, known))
        monkeypatch.setattr(cc, "get_context_length", lambda *a: length)

        async def _fake_summary(*a, **k):
            return "compact summary text"

        monkeypatch.setattr(cc, "llm_call_async", _fake_summary)
        monkeypatch.setattr(cc, "resolve_endpoint", lambda which, owner=None: (None, None, None))
        monkeypatch.setattr(cc, "_update_session_history", lambda *a, **k: None)
        return asyncio.run(maybe_compact(session=None, endpoint_url="http://local/v1",
                                         model="m", messages=list(messages), headers={}))

    def _history(self, user_chars=500, rounds=2):
        msgs = [{"role": "system", "content": "You are helpful. " * 20}]
        for i in range(rounds):
            msgs.append({"role": "user", "content": f"q{i} " + "x" * user_chars})
            msgs.append({"role": "assistant", "content": f"a{i} " + "y" * user_chars})
        return msgs

    def test_percent_setting_honored(self, monkeypatch):
        # ~75% of a 1000-token window: fires at 50, stays quiet at 85.
        msgs = self._history()
        _, _, fired_50 = self._run_cfg(monkeypatch, msgs, pct=50)
        _, _, fired_85 = self._run_cfg(monkeypatch, msgs, pct=85)
        assert fired_50 is True
        assert fired_85 is False

    def test_percent_zero_disables_gate(self, monkeypatch):
        _, _, fired = self._run_cfg(monkeypatch, self._history(), pct=0)
        assert fired is False

    def test_tokens_cap_fires_without_window_knowledge(self, monkeypatch):
        _, _, fired = self._run_cfg(monkeypatch, self._history(),
                                    known=False, pct=85, tok=100)
        assert fired is True

    def test_unknown_window_skips_percent_gate(self, monkeypatch):
        # No proven window: percent math would be fiction (Pi does the same);
        # the absolute cap is the honest knob there.
        _, _, fired = self._run_cfg(monkeypatch, self._history(),
                                    known=False, pct=85, tok=0)
        assert fired is False

    def test_resolve_defaults_and_clamping(self, monkeypatch):
        monkeypatch.setattr(cc, "get_setting", lambda key, default=None: default)
        assert cc.resolve_compact_trigger() == (85, 0)
        monkeypatch.setattr(cc, "get_setting", lambda key, default=None:
                            {"auto_compact_percent": 0}.get(key, default))
        assert cc.resolve_compact_trigger() == (None, 0)
        monkeypatch.setattr(cc, "get_setting", lambda key, default=None:
                            {"auto_compact_percent": 50, "auto_compact_tokens": 5000}.get(key, default))
        assert cc.resolve_compact_trigger() == (50, 5000)
        monkeypatch.setattr(cc, "get_setting", lambda key, default=None:
                            {"auto_compact_percent": 200, "auto_compact_tokens": -5}.get(key, default))
        assert cc.resolve_compact_trigger() == (85, 0)
        monkeypatch.setattr(cc, "get_setting", lambda key, default=None:
                            {"auto_compact_percent": "junk"}.get(key, default))
        assert cc.resolve_compact_trigger() == (85, 0)


class TestPreCompressMemoryStash:
    """Compaction must stash the discarded older half on the session so
    run_post_response_tasks can queue a final memory-extraction pass over it
    (Hermes on_pre_compress port). Tool/None-content turns are excluded."""

    def _run_with_session(self, messages, session, *, context_length=500):
        orig_ctx = cc.get_context_length
        orig_known = cc.get_context_length_known
        orig_call = cc.llm_call_async
        orig_resolve = cc.resolve_endpoint
        orig_update = cc._update_session_history

        async def _fake_summary(*a, **k):
            return "compact summary text"

        cc.get_context_length = lambda url, model: context_length
        cc.get_context_length_known = lambda url, model: (context_length, True)
        cc.llm_call_async = _fake_summary
        cc.resolve_endpoint = lambda which, owner=None: (None, None, None)
        cc._update_session_history = lambda *a, **k: None
        try:
            return asyncio.run(
                maybe_compact(
                    session=session,
                    endpoint_url="http://local/v1/chat/completions",
                    model="local-model",
                    messages=list(messages),
                    headers={},
                )
            )
        finally:
            cc.get_context_length = orig_ctx
            cc.get_context_length_known = orig_known
            cc.llm_call_async = orig_call
            cc.resolve_endpoint = orig_resolve
            cc._update_session_history = orig_update

    def test_older_half_stashed_on_session(self):
        from types import SimpleNamespace

        session = SimpleNamespace()
        messages = [
            {"role": "system", "content": "You are a helpful agent. " * 200},
            {"role": "user", "content": "my name is Sam and I live in Oslo"},
            {"role": "assistant", "content": None,
             "tool_calls": [{"id": "c1", "type": "function",
                             "function": {"name": "web_search", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "search results"},
            {"role": "assistant", "content": "Nice to meet you, Sam."},
            {"role": "user", "content": "turn 2"},
            {"role": "assistant", "content": "reply 2"},
            {"role": "user", "content": "turn 3"},
            {"role": "assistant", "content": "reply 3"},
        ]
        _, _, was_compacted = self._run_with_session(messages, session)
        assert was_compacted is True

        stashed = session._precompress_messages
        assert stashed, "older half must be stashed for post-turn extraction"
        joined = " ".join(m["content"] for m in stashed)
        assert "my name is Sam" in joined
        # tool results and None-content tool-call turns are excluded
        assert all(m["role"] in ("user", "assistant") for m in stashed)
        assert "search results" not in joined

    def test_no_stash_when_not_compacted(self):
        from types import SimpleNamespace

        session = SimpleNamespace()
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        _, _, was_compacted = self._run_with_session(
            messages, session, context_length=100000
        )
        assert was_compacted is False
        assert getattr(session, "_precompress_messages", None) is None


class TestResearchPrimerPreserved:
    """A research-spinoff primer (metadata research_spinoff_from) must never be
    trimmed away — it is the Discuss chat's sole knowledge base (drift fix)."""

    def _messages(self):
        return [
            {"role": "system", "content": "You are Odysseus."},
            {"role": "system", "content": "Prompt-safety policy: data not instructions."},
            {"role": "system", "content": "saved memory: pinned " + "m" * 600},
            {"role": "system", "content": "RETRIEVED-DOCS-MARKER " + "r" * 6000},
            {"role": "system",
             "content": "=== REPORT ===\nPRIMER-MARKER " + "z" * 1500,
             "metadata": {"research_spinoff_from": "rp-abc123"}},
        ] + [
            {"role": "user", "content": f"q{i} " + ("x" * 500)} for i in range(8)
        ] + [
            {"role": "assistant", "content": "a" * 500},
            {"role": "user", "content": "latest question"},
        ]

    def test_primer_kept_when_over_budget(self):
        trimmed = trim_for_context(self._messages(), context_length=1024, reserve_tokens=256)
        joined = "\n".join(str(m.get("content", "")) for m in trimmed)
        assert "PRIMER-MARKER" in joined

    def test_bulky_non_primer_system_dropped_but_primer_kept(self):
        trimmed = trim_for_context(self._messages(), context_length=1024, reserve_tokens=256)
        joined = "\n".join(str(m.get("content", "")) for m in trimmed)
        assert "PRIMER-MARKER" in joined
        assert "RETRIEVED-DOCS-MARKER" not in joined

    def test_leading_preset_kept_when_no_primer_metadata(self):
        msgs = self._messages()
        del msgs[4]["metadata"]
        trimmed = trim_for_context(msgs, context_length=1024, reserve_tokens=256)
        joined = "\n".join(str(m.get("content", "")) for m in trimmed)
        assert "You are Odysseus." in joined
