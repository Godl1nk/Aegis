"""resumeStream() must render incrementally, like the primary send path.

Observed: a long reply made the tab lag and then freeze. `resumeStream` — the
path that re-attaches to a detached run, which is what a slow turn (e.g. one
that waited on image analysis) ends up on — rebuilt the WHOLE message on every
token:

    contentDiv.innerHTML = mdToHtml(squashOutsideCode(dt))

That is a full markdown parse plus a full DOM teardown and re-highlight per
token: O(N) per token, O(N^2) over the reply, all on the main thread. Measured
in-browser against the same corpus, naive vs incremental:

      3.7k chars   103ms  vs   16ms
      7.4k chars   371ms  vs   30ms
     14.9k chars  1340ms  vs   64ms
     29.8k chars  5438ms  vs  108ms   <- ~5.4s of blocked main thread

Naive time roughly quadruples per doubling (quadratic); incremental doubles
(linear). streamingRenderer.js already solved this for the primary send path —
resumeStream just never adopted it.
"""
import re
from pathlib import Path

CHAT_JS = (Path(__file__).resolve().parent.parent / "static/js/chat.js").read_text(encoding="utf-8")


def _resume_stream_source() -> str:
    start = CHAT_JS.index("export async function resumeStream")
    # Up to the next top-level export — enough to cover the whole function.
    nxt = CHAT_JS.find("\n  export ", start + 10)
    return CHAT_JS[start: nxt if nxt > 0 else len(CHAT_JS)]


def test_resume_stream_uses_the_incremental_renderer():
    body = _resume_stream_source()
    assert "createStreamRenderer" in body, (
        "resumeStream must reuse streamingRenderer.js, not re-parse the whole "
        "message per token"
    )


def test_resume_stream_does_not_full_render_per_token():
    """The specific line that froze the tab."""
    body = _resume_stream_source()
    offending = re.search(r"contentDiv\.innerHTML\s*=\s*markdownModule\.mdToHtml", body)
    assert not offending, (
        "per-token full-document innerHTML assignment reintroduces the O(N^2) freeze"
    )


def test_renderer_is_cached_on_the_element():
    """A fresh renderer per token would freeze nothing and re-render everything —
    the instance has to persist across deltas to hold its committed prefix."""
    body = _resume_stream_source()
    assert "_streamRenderer" in body


def test_document_writing_branch_is_preserved():
    """The doc-fence status branch must still bypass the renderer, exactly as
    before — it writes its own placeholder into the same element."""
    body = _resume_stream_source()
    assert "_showDocumentWritingStatus(contentDiv)" in body


def test_resume_stream_preserves_document_language_and_lifecycle():
    body = _resume_stream_source()
    assert "json.language || json.lang || ''" in body
    assert "streamDocOpen(json.title || '', json.lang || '')" not in body
    for event, handler, argument in (
        ("doc_stream_phase", "streamDocPhase", "json.phase"),
        ("doc_stream_cancel", "streamDocCancel", "json.reason"),
    ):
        assert f"json.type === '{event}'" in body
        assert handler in body
        assert argument in body
    assert "json.type === 'doc_update'" in body
    assert "documentModule.handleDocUpdate(json)" in body
