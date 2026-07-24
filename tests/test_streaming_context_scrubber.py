"""Unit tests for StreamingContextScrubber (src/context_scrubber.py).

Ported from Hermes' scrubber tests — guard-fenced context spans split
across stream deltas must not leak payload to the UI.  The one-shot
sanitize_context() regex can't survive chunk boundaries, so stream_llm
routes deltas through a stateful scrubber.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.context_scrubber import StreamingContextScrubber, sanitize_context
from src.prompt_security import GUARD_OPEN, GUARD_CLOSE


class TestStreamingContextScrubberBasics:
    def test_empty_input_returns_empty(self):
        s = StreamingContextScrubber()
        assert s.feed("") == ""
        assert s.flush() == ""

    def test_plain_text_passes_through(self):
        s = StreamingContextScrubber()
        assert s.feed("hello world") == "hello world"
        assert s.flush() == ""

    def test_complete_block_in_single_delta(self):
        s = StreamingContextScrubber()
        leaked = (
            f"{GUARD_OPEN}\n"
            "Source: saved memory: pinned user facts\n"
            "Core facts about the user:\n- stale memory\n"
            f"{GUARD_CLOSE}\n\nVisible answer"
        )
        out = s.feed(leaked) + s.flush()
        assert out == "\n\nVisible answer"

    def test_open_and_close_in_separate_deltas_strips_payload(self):
        """The real streaming case: tag pair split across deltas."""
        s = StreamingContextScrubber()
        deltas = [
            "Hello\n",
            f"{GUARD_OPEN}\npayload ",
            "more payload\n",
            f"{GUARD_CLOSE} world",
        ]
        out = "".join(s.feed(d) for d in deltas) + s.flush()
        assert out == "Hello\n world"
        assert "payload" not in out

    def test_realistic_fragmented_chunks_strip_memory_payload(self):
        """Open tag, source label, payload, and close tag each arrive in
        their own delta because providers emit 1-80 char chunks."""
        s = StreamingContextScrubber()
        deltas = [
            f"{GUARD_OPEN}\nSource: saved",
            " memory: retrieved context\n\n",
            "Memory context. Do not reference\nstale memory\n",
            f"{GUARD_CLOSE}\n\nVisible answer",
        ]
        out = "".join(s.feed(d) for d in deltas) + s.flush()
        assert out == "\n\nVisible answer"
        assert "Source:" not in out
        assert "stale memory" not in out

    def test_open_tag_split_across_two_deltas(self):
        s = StreamingContextScrubber()
        out = (
            s.feed("pre \n" + GUARD_OPEN[:12])
            + s.feed(GUARD_OPEN[12:] + f"\nleak{GUARD_CLOSE} post")
            + s.flush()
        )
        assert out == "pre \n post"
        assert "leak" not in out

    def test_open_tag_waits_for_newline_confirmation_across_deltas(self):
        """A boundary tag is only a leaked block when the next char is a newline."""
        s = StreamingContextScrubber()
        out = (
            s.feed(f"pre \n{GUARD_OPEN}")
            + s.feed(f"\nleak{GUARD_CLOSE} post")
            + s.flush()
        )
        assert out == "pre \n post"
        assert "leak" not in out

    def test_close_tag_split_across_two_deltas(self):
        s = StreamingContextScrubber()
        out = (
            s.feed(f"pre \n{GUARD_OPEN}\nleak" + GUARD_CLOSE[:10])
            + s.feed(GUARD_CLOSE[10:] + " post")
            + s.flush()
        )
        assert out == "pre \n post"
        assert "leak" not in out


class TestStreamingContextScrubberPartialTagFalsePositives:
    def test_partial_open_tag_tail_emitted_on_flush(self):
        """A bare tag prefix at end of stream is not really a guard tag."""
        s = StreamingContextScrubber()
        out = s.feed("hello <<<UNT") + s.feed("IL tomorrow") + s.flush()
        assert out == "hello <<<UNTIL tomorrow"

    def test_partial_tag_released_when_disambiguated(self):
        s = StreamingContextScrubber()
        # '< ' should not look like the start of any tag.
        out = s.feed("price < ") + s.feed("10 dollars") + s.flush()
        assert out == "price < 10 dollars"

    def test_mid_sentence_guard_tag_mention_is_not_scrubbed(self):
        """Only block-like guard spans are treated as leaked context."""
        s = StreamingContextScrubber()
        out = s.feed(f"The {GUARD_OPEN} tag name is documented here.") + s.flush()
        assert out == f"The {GUARD_OPEN} tag name is documented here."

    def test_line_start_guard_mention_without_close_is_not_scrubbed(self):
        """A plain-text line that starts with the tag name must be preserved."""
        s = StreamingContextScrubber()
        out = (
            s.feed("Visible intro\n")
            + s.feed(f"{GUARD_OPEN} is the literal tag name mentioned here.")
            + s.flush()
        )
        assert out == f"Visible intro\n{GUARD_OPEN} is the literal tag name mentioned here."


class TestStreamingContextScrubberUnterminatedSpan:
    def test_unterminated_span_drops_payload(self):
        """Stream drops close tag — better to lose output than to leak."""
        s = StreamingContextScrubber()
        out = s.feed(f"pre \n{GUARD_OPEN}\nsecret never closed") + s.flush()
        assert out == "pre \n"
        assert "secret" not in out

    def test_reset_clears_hung_span(self):
        s = StreamingContextScrubber()
        s.feed(f"pre \n{GUARD_OPEN}\nhalf")
        s.reset()
        out = s.feed("clean text") + s.flush()
        assert out == "clean text"


class TestStreamingContextScrubberCaseInsensitivity:
    def test_lowercase_tags_still_scrubbed(self):
        s = StreamingContextScrubber()
        out = (
            s.feed(GUARD_OPEN.lower() + "\nsecret")
            + s.feed(GUARD_CLOSE.lower() + "visible")
            + s.flush()
        )
        assert out == "visible"
        assert "secret" not in out


class TestSanitizeContext:
    def test_whole_block_still_sanitized(self):
        leaked = (
            f"{GUARD_OPEN}\n"
            "Source: saved memory\n"
            "payload\n"
            f"{GUARD_CLOSE}\nVisible"
        )
        out = sanitize_context(leaked).strip()
        assert out == "Visible"


class TestScrubStreamChunk:
    """Integration shape: the llm_core chunk router feeds only visible
    deltas through the scrubber and flushes ahead of [DONE]."""

    def _chunks(self, deltas):
        from src.llm_core import _scrub_stream_chunk

        s = StreamingContextScrubber()
        out = []
        for d in deltas:
            out.extend(_scrub_stream_chunk(s, f'data: {json.dumps({"delta": d})}\n\n'))
        out.extend(_scrub_stream_chunk(s, "data: [DONE]\n\n"))
        return out

    def _visible(self, chunks):
        text = ""
        for c in chunks:
            if c.startswith("data: ") and not c.startswith("data: [DONE]"):
                text += json.loads(c[6:]).get("delta", "")
        return text

    def test_guard_block_stripped_across_chunks(self):
        chunks = self._chunks([
            "Answer:\n",
            f"{GUARD_OPEN}\nleaked memory\n",
            f"{GUARD_CLOSE}done",
        ])
        assert self._visible(chunks) == "Answer:\ndone"
        assert chunks[-1] == "data: [DONE]\n\n"

    def test_held_tail_flushed_before_done(self):
        chunks = self._chunks(["text ends with <<<UNT"])
        assert self._visible(chunks) == "text ends with <<<UNT"
        assert chunks[-1] == "data: [DONE]\n\n"

    def test_thinking_and_typed_chunks_pass_through(self):
        from src.llm_core import _scrub_stream_chunk

        s = StreamingContextScrubber()
        think = f'data: {json.dumps({"delta": GUARD_OPEN, "thinking": True})}\n\n'
        assert _scrub_stream_chunk(s, think) == [think]
        usage = f'data: {json.dumps({"type": "usage", "data": {}})}\n\n'
        assert _scrub_stream_chunk(s, usage) == [usage]
        heartbeat = ": heartbeat\n\n"
        assert _scrub_stream_chunk(s, heartbeat) == [heartbeat]
