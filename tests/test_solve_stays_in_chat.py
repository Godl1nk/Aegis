"""A 'solve/answer/explain this' request must be answered in CHAT, not routed
into create_document. The old directive ('structured writing is a document by
default') made the model open a document for a multi-step math solution, which
also dragged the turn into extra agent rounds that stalled on a local-model
swap. These pin the corrected guidance so it can't silently regress."""

from pathlib import Path

AGENT_LOOP = Path("src/agent_loop.py").read_text(encoding="utf-8")


def test_old_structured_default_directive_is_gone():
    # The over-broad phrasing that treated any long/structured answer as a
    # document must not come back.
    assert "structured writing is a document by default" not in AGENT_LOOP
    assert "Long-form or structured writing is a document by default" not in AGENT_LOOP


def test_solve_and_explain_are_chat_answers():
    # Every place that talks about when to make a document now carves out
    # solve/answer/explain as chat.
    for needle in ('"Solve this"', "Solve/answer/explain", "work through"):
        assert needle in AGENT_LOOP, needle
    # And the carve-out is explicit that math/long answers still stay in chat.
    assert "even when the solution is long, multi-step, or has math" in AGENT_LOOP
    # A document is gated on an explicit artifact request.
    assert "explicitly asked you to write/create/make/generate/save as an artifact" in AGENT_LOOP
