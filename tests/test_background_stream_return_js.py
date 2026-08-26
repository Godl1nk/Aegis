"""Regression checks for returning to a detached chat before it finishes."""

from pathlib import Path


CHAT_JS = (Path(__file__).resolve().parent.parent / "static/js/chat.js").read_text(encoding="utf-8")


def test_returned_background_stream_keeps_completion_for_reattach_owner():
    """The old reader must not finalize against DOM from before navigation.

    The reattached reader (or fallback poll) owns the visible response after the
    user returns. Keeping the completed map entry stops the original detached
    reader from falling through into its stale foreground-rendering path.
    """
    start = CHAT_JS.index("if (data === '[DONE]')")
    end = CHAT_JS.index("// Force-close thinking", start)
    done_branch = CHAT_JS[start:end]

    assert "if (bgDone)" in done_branch
    assert "bgDone.status = 'completed';" in done_branch
    assert "_backgroundStreams.delete(streamSessionId)" not in done_branch


def test_running_background_stream_attempts_live_reattach_before_placeholder():
    start = CHAT_JS.index("export async function checkBackgroundStream")
    end = CHAT_JS.index("// Tag short single-line code blocks", start)
    poll = CHAT_JS[start:end]

    reattach = "resumeStream(sessionId, { allowDetachedReader: true })"
    placeholder = "Response streaming in background"
    assert reattach in poll
    assert poll.index(reattach) < poll.index(placeholder)
    assert "curPoll.status !== 'running'" in poll
    assert "sessionModule.selectSession(sessionId);" in poll


def test_original_detached_reader_stays_background_owned_after_return():
    assert "const _isOtherSession = (sessionModule.getCurrentSessionId() !== streamSessionId);" in CHAT_JS
    assert "const _isBg = _isOtherSession || _backgroundStreams.has(streamSessionId);" in CHAT_JS
    assert "if (_isOtherSession && !_backgroundStreams.has(streamSessionId))" in CHAT_JS


def test_completed_reader_releases_active_stream_identity():
    assert "if (_streamSessionId === streamSessionId) _streamSessionId = null;" in CHAT_JS
