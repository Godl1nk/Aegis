"""A turn that produces ONLY reasoning must not end silently.

Reasoning models sometimes finish a turn having emitted just a <think> block:
no answer text, no tool call. The agent loop broke out of the round loop there,
so the turn ended as a collapsed "Thinking (N lines)" section with nothing
after it — indistinguishable from a hang (user report: two web_searches, then
'Thinking (3 lines)' and no reply). One bounded nudge asks for the answer."""

from pathlib import Path

import src.agent_tools  # noqa: F401

AGENT_LOOP = Path("src/agent_loop.py").read_text(encoding="utf-8")


def test_thinking_only_response_has_no_visible_answer():
    """The detection primitive: stripping think blocks from a reasoning-only
    turn leaves nothing, while a real answer survives."""
    from src.agent_loop import _strip_think_blocks

    assert _strip_think_blocks("<think>reasoning</think>").strip() == ""
    assert _strip_think_blocks("<think>r</think>The movie is X.").strip() == "The movie is X."


def test_empty_answer_nudge_wired_and_bounded():
    assert "_empty_answer_nudges = 0" in AGENT_LOOP
    assert "_empty_answer_nudges < 1" in AGENT_LOOP          # bounded
    assert "_empty_answer_nudges += 1" in AGENT_LOOP
    assert "only internal reasoning" in AGENT_LOOP
    # Checked against the WHOLE turn, so a round adding nothing after an
    # earlier reply does not re-trigger.
    assert "not _strip_think_blocks(full_response).strip()" in AGENT_LOOP


def test_nudge_runs_before_the_no_tools_break():
    """It must fire ahead of `break  # no tools — done`, otherwise the turn
    ends before the nudge can be applied."""
    nudge = AGENT_LOOP.index("_empty_answer_nudges += 1")
    brk = AGENT_LOOP.index("break  # no tools — done")
    assert nudge < brk
