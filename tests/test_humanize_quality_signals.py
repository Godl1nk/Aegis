"""Quality signals the rewrite scorer leans on.

The original scorer measured spread (coefficient of variation), single-word
sentence starters and n-gram diversity. That misses three things the rewrite is
supposed to deliver:

  * rhythm is a property of the SEQUENCE, not the spread — 5,5,5,30,30,30 has a
    high CV and reads like two blocks glued together;
  * varied opening *words* are not varied opening *shapes*;
  * a fluent, well-varied rewrite that quietly drops "17%" or "TLS" is wrong,
    not stylish.
"""
import asyncio
import re
import time

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import routes.humanize_routes as humanize_routes
from routes.humanize_routes import (
    _SYSTEM_PROMPT,
    _burstiness,
    _compute_human_score,
    _fidelity_report,
    _filler_density,
    _opening_shape,
    _opening_shape_diversity,
    _revision_notes,
    _sentence_starter_diversity,
)

_BLOCKED = (
    "Short one here. Another short line. Third short line now. "
    "This sentence runs considerably longer and carries several subordinate clauses "
    "that stretch it past the point where a reader expects it to stop, continuing further. "
    "Another long one that similarly extends across many words and clauses without "
    "pausing for breath at any reasonable point along the way here."
)
_ALTERNATING = (
    "The rollout finished on Tuesday. Across the three regions that had been waiting "
    "since the pilot, the team confirmed every node reported healthy within the hour. "
    "Good. Latency dropped, though not everywhere, and the tail cases still need "
    "attention before the next window opens. It held."
)


def test_burstiness_separates_alternation_from_blocked_lengths():
    """Both texts have near-identical CV; only one actually alternates."""
    assert _burstiness(_ALTERNATING) > _burstiness(_BLOCKED)


def test_burstiness_is_zero_for_uniform_sentences():
    uniform = (
        "This is a sentence with exactly nine words. "
        "This is another sentence with nine words here. "
        "This is the third sentence with nine words."
    )
    assert _burstiness(uniform) == pytest.approx(0.0, abs=0.15)


def test_burstiness_needs_two_sentences():
    assert _burstiness("Only one sentence here.") == 0.0
    assert _burstiness("") == 0.0


@pytest.mark.parametrize(
    "sentence,shape",
    [
        ("Because the pilot lagged, we waited.", "subordinate"),
        ("Across three regions, it held.", "prepositional"),
        ("Quietly, the tail cases remain.", "adverbial"),
        ("Running late, we shipped anyway.", "participial"),
        ("The system works.", "subject"),
        ("And then it stopped.", "conjunction"),
        ("Ship it.", "other"),
    ],
)
def test_opening_shapes_are_bucketed_by_grammar(sentence, shape):
    assert _opening_shape(sentence) == shape


def test_opening_shape_catches_what_distinct_starter_words_miss():
    """Five different opening words, one monotonous shape — the case that
    motivated the metric. Starter diversity calls this perfect."""
    same_shape = (
        "In 2021 the pilot began. During the rollout latency held. "
        "Across three regions nodes stayed healthy. Within the quarter costs fell. "
        "Between releases nothing broke."
    )
    assert _sentence_starter_diversity(same_shape) == pytest.approx(1.0)
    assert _opening_shape_diversity(same_shape) < 0.35


def test_short_text_is_not_punished_for_few_shapes():
    assert _opening_shape_diversity("The build shipped. Latency fell.") == pytest.approx(1.0)


def test_filler_and_hype_cost_score():
    clichy = (
        "It is important to note that the platform plays a crucial role here. "
        "Furthermore, this cutting-edge solution will revolutionize how teams work. "
        "Moreover, when it comes to scale, it delivers."
    )
    clean = (
        "The platform matters here. It changes how teams work at scale. "
        "Early numbers back that up, though the sample is small."
    )
    assert _filler_density(clichy) > 0
    assert _filler_density(clean) == 0.0
    assert _compute_human_score(clean) > _compute_human_score(clichy)


def test_filler_penalty_is_capped():
    """A filler-dense source must not drive the score to zero — the rewrite is
    still judged on rhythm."""
    all_filler = "Furthermore moreover furthermore moreover. " * 5
    assert _compute_human_score(all_filler) >= 0.0


def test_fidelity_flags_dropped_numbers_and_acronyms():
    source = "Latency fell 17% after we moved to TLS 1.3 across all 4 regions."
    lost = "Latency improved noticeably after the protocol upgrade everywhere."
    report = _fidelity_report(source, lost)
    assert "17%" in report["missing_numbers"]
    assert "TLS" in report["missing_acronyms"]
    assert report["score"] < 0.5


def test_fidelity_passes_a_faithful_reorder():
    source = "Latency fell 17% after we moved to TLS 1.3 across all 4 regions."
    kept = "After moving to TLS 1.3 in all 4 regions, latency fell 17%."
    report = _fidelity_report(source, kept)
    assert report == {"missing_numbers": [], "missing_acronyms": [], "score": 1.0}


def test_fidelity_is_neutral_when_there_is_nothing_to_anchor():
    report = _fidelity_report("A short line of prose.", "Some prose, rewritten.")
    assert report["score"] == 1.0


def test_revision_notes_name_the_actual_failure():
    source = "The service handled 17% more traffic after the TLS upgrade."
    candidate = (
        "It is important to note that the service handled more traffic. "
        "It is important to note that capacity rose. "
        "It is important to note that users noticed."
    )
    notes = " ".join(_revision_notes(source, candidate, [candidate], [source]))
    assert "17%" in notes and "TLS" in notes          # dropped anchors named
    assert "important to note" in notes.lower()        # filler quoted back
    assert "openings repeat" in notes.lower()          # repetition called out


def test_revision_notes_are_empty_for_a_good_rewrite():
    source = "The rollout finished on Tuesday across three regions."
    good = (
        "Tuesday brought the rollout to a close. Across all three regions, "
        "engineers confirmed the last node had come up clean and stayed that way. "
        "Nothing broke."
    )
    assert _revision_notes(source, good, [good], [source]) == []


def test_prompt_asks_for_distribution_not_a_hard_cap():
    """The old prompt capped sentences at 24 words, which suppressed the very
    variation it was asking for and read choppy."""
    assert "Maximum sentence length is 24 words" not in _SYSTEM_PROMPT
    assert "30-40 word sentence is fine" in _SYSTEM_PROMPT
    # And it must not mandate a fixed lexical tic — that IS token predictability.
    assert "Back then" not in _SYSTEM_PROMPT


def test_prompt_keeps_the_fidelity_contract():
    assert "Preserve every fact and meaning exactly" in _SYSTEM_PROMPT
    assert "number, unit, date, proper noun, acronym" in _SYSTEM_PROMPT


# --- route level -----------------------------------------------------------

def _submit_and_wait(app, payload):
    with TestClient(app) as client:
        accepted = client.post("/api/humanize", json=payload)
        assert accepted.status_code == 202
        job_id = accepted.json()["job_id"]
        for _ in range(200):
            status = client.get(f"/api/humanize/status/{job_id}")
            if status.json()["status"] in {"done", "error"}:
                return status.json()
            time.sleep(0.01)
    raise AssertionError("Rewrite job did not finish")


def _wire(monkeypatch, rewrite):
    monkeypatch.setattr(humanize_routes, "require_user", lambda request: "alice")
    monkeypatch.setattr(
        humanize_routes,
        "resolve_endpoint_by_id",
        lambda endpoint_id, model, owner: ("http://model.test/chat", model, {}),
    )
    monkeypatch.setattr(humanize_routes, "llm_call_async", rewrite)
    app = FastAPI()
    app.include_router(humanize_routes.setup_humanize_routes())
    return app


def test_retry_prompt_carries_the_specific_failures(monkeypatch):
    """The retry used to say only 'make it substantially different', leaving the
    model to guess which axis failed."""
    source = "Throughput rose 23% once the ACL cache landed in build 9."
    prompts = []

    async def rewrite(**kwargs):
        content = kwargs["messages"][1]["content"]
        prompts.append(content)
        if "previous attempt was rejected" in content:
            return f'<paragraph id="1">Once the ACL cache reached build 9, throughput climbed 23%. It stuck.</paragraph>'
        # First attempt: fluent, but it silently drops every anchor.
        return '<paragraph id="1">It is important to note that speed improved after the change.</paragraph>'

    result = _submit_and_wait(
        _wire(monkeypatch, rewrite),
        {"text": source, "endpoint_id": "e1", "model": "m1"},
    )

    assert result["status"] == "done"
    assert len(prompts) == 2, "a failing first attempt must trigger the retry"
    retry = prompts[1]
    assert "23%" in retry and "ACL" in retry     # dropped anchors named back
    assert "important to note" in retry.lower()  # filler quoted back


def test_dropped_anchors_are_reported_to_the_caller(monkeypatch):
    """A rewrite that loses a figure still returns — but the caller can see it."""
    source = "Error rates fell 12% across the CDN edge nodes."

    async def rewrite(**kwargs):
        return '<paragraph id="1">Failures dropped sharply at the edge. Nobody complained.</paragraph>'

    result = _submit_and_wait(
        _wire(monkeypatch, rewrite),
        {"text": source, "endpoint_id": "e1", "model": "m1"},
    )

    metrics = result["result"]["metrics"]
    assert "12%" in metrics["missing_anchors"]
    assert "CDN" in metrics["missing_anchors"]
    assert metrics["fidelity"] < 1.0
    # The rewrite is still delivered rather than failing the job.
    assert result["result"]["text"]


def test_budget_scales_with_document_size():
    """A flat 28s budget is what made a reasoning model fail on a long
    document: the first attempt blew the deadline and the retry inherited the
    1s floor."""
    short = humanize_routes._budget_seconds("word " * 20)
    long_doc = humanize_routes._budget_seconds("word " * 2000)
    assert long_doc > short
    assert short >= humanize_routes._TIMEOUT_BASE_S
    # And it can't run away on a huge paste.
    assert humanize_routes._budget_seconds("word " * 200_000) <= humanize_routes._TIMEOUT_MAX_S


def _shrink_budget(monkeypatch, seconds=0.6):
    monkeypatch.setattr(humanize_routes, "_TIMEOUT_BASE_S", seconds)
    monkeypatch.setattr(humanize_routes, "_TIMEOUT_PER_1K_CHARS_S", 0.0)
    monkeypatch.setattr(humanize_routes, "_TIMEOUT_MAX_S", seconds)
    monkeypatch.setattr(humanize_routes, "_MIN_ATTEMPT_S", 0.01)


def test_slow_whole_document_call_falls_back_to_batches(monkeypatch):
    """The user-visible failure: a slow model returned nothing at all. Now the
    remaining budget goes on smaller calls and the work that lands is kept."""
    source = "First para here.\n\nSecond para here.\n\nThird para here.\n\nFourth para here."
    _shrink_budget(monkeypatch, 1.5)
    seen = []

    async def rewrite(**kwargs):
        content = kwargs["messages"][1]["content"]
        n_paras = content.count("<paragraph id=")
        seen.append(n_paras)
        if n_paras > 2:            # whole-document attempt: too slow
            await asyncio.sleep(5)
        # Batch calls answer promptly.
        ids = re.findall(r'<paragraph id="(\d+)">', content)
        return "".join(
            f'<paragraph id="{i}">Rewritten body number {i} reads quite differently now.</paragraph>'
            for i in ids
        )

    result = _submit_and_wait(
        _wire(monkeypatch, rewrite),
        {"text": source, "endpoint_id": "e1", "model": "m1"},
    )

    assert result["status"] == "done", result.get("error")
    assert max(seen) > 2 and min(seen) <= 2, "expected a whole-doc attempt then batches"
    metrics = result["result"]["metrics"]
    assert metrics["paragraphs_rewritten"] >= 1
    assert metrics["paragraphs_total"] == 4
    assert "Rewritten body" in result["result"]["text"]


def test_total_timeout_reports_something_actionable(monkeypatch):
    """When even the batches can't land, the error must say what to change."""
    _shrink_budget(monkeypatch, 0.3)

    async def rewrite(**kwargs):
        await asyncio.sleep(5)

    result = _submit_and_wait(
        _wire(monkeypatch, rewrite),
        {"text": "A short paragraph to rewrite.", "endpoint_id": "e1", "model": "m1"},
    )

    assert result["status"] == "error"
    assert "HUMANIZE_TIMEOUT_S" in result["error"]
    assert "faster model" in result["error"]


def test_inner_call_timeout_tracks_the_shared_budget(monkeypatch):
    """A fixed inner timeout either capped the shared budget or outlived it."""
    _shrink_budget(monkeypatch, 30.0)
    seen = {}

    async def rewrite(**kwargs):
        seen["timeout"] = kwargs.get("timeout")
        return '<paragraph id="1">A rewritten line that differs from the source entirely. It lands.</paragraph>'

    _submit_and_wait(
        _wire(monkeypatch, rewrite),
        {"text": "Source line to rewrite here.", "endpoint_id": "e1", "model": "m1"},
    )

    assert seen["timeout"] is not None
    assert seen["timeout"] <= 30.0


def test_progress_is_reported_through_the_phases(monkeypatch):
    """A multi-minute rewrite used to sit on one status line the whole time."""
    source = "First para here.\n\nSecond para here.\n\nThird para here."
    _shrink_budget(monkeypatch, 1.5)
    updates = []

    async def rewrite(**kwargs):
        content = kwargs["messages"][1]["content"]
        if content.count("<paragraph id=") > 2:
            await asyncio.sleep(5)   # force the batched path
        ids = re.findall(r'<paragraph id="(\d+)">', content)
        return "".join(
            f'<paragraph id="{i}">Quite a different line entirely, rewritten here. It holds.</paragraph>'
            for i in ids
        )

    monkeypatch.setattr(humanize_routes, "llm_call_async", rewrite)
    asyncio.run(
        humanize_routes._rewrite_document(
            source,
            url="http://model.test/chat",
            model="m1",
            headers={},
            on_progress=lambda step, detail: updates.append((step, detail)),
        )
    )

    assert len(updates) >= 2, "expected more than a single status for a batched run"
    assert any("batch" in d.lower() for _, d in updates)
    # The batch phase names which paragraphs are in flight.
    assert any("paragraph" in d.lower() for _, d in updates)


def test_single_paragraph_never_falls_back_to_a_pointless_batch(monkeypatch):
    """A one-paragraph "batch" IS the whole document. Capping the first attempt
    to reserve budget for that fallback just doubled the wait — the observed
    "Working through paragraph 1-1 of 1" at nearly two minutes."""
    limits = []
    real_ask_budget = {}

    async def rewrite(**kwargs):
        limits.append(kwargs.get("timeout"))
        return '<paragraph id="1">A wholly different line, rebuilt from scratch here. It lands.</paragraph>'

    result = _submit_and_wait(
        _wire(monkeypatch, rewrite),
        {"text": "One single paragraph of source text to rewrite.", "endpoint_id": "e1", "model": "m1"},
    )

    assert result["status"] == "done"
    assert len(limits) == 1, "a single-paragraph document must not be re-run as a batch"
    # The lone attempt gets the whole budget, not 55% of it.
    budget = humanize_routes._budget_seconds("One single paragraph of source text to rewrite.")
    assert limits[0] > budget * humanize_routes._FIRST_ATTEMPT_SHARE


def test_transient_endpoint_error_is_retried_not_surfaced(monkeypatch):
    """The "click Rewrite, it fails, click again, it works" case: one blip used
    to abort the whole job because only asyncio.TimeoutError was caught."""
    calls = []

    async def rewrite(**kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise ConnectionResetError("connection reset by peer")
        return '<paragraph id="1">Rebuilt from scratch, this line reads differently. It holds.</paragraph>'

    result = _submit_and_wait(
        _wire(monkeypatch, rewrite),
        {"text": "A paragraph that should survive one transient blip.", "endpoint_id": "e1", "model": "m1"},
    )

    assert result["status"] == "done", result.get("error")
    assert len(calls) == 2
    assert "Rebuilt from scratch" in result["result"]["text"]


def test_auth_failures_are_not_retried(monkeypatch):
    """A 401 fails identically every time — retrying just burns the budget."""
    calls = []

    async def rewrite(**kwargs):
        calls.append(1)
        raise HTTPException(401, "Invalid API key")

    result = _submit_and_wait(
        _wire(monkeypatch, rewrite),
        {"text": "Some source text here.", "endpoint_id": "e1", "model": "m1"},
    )

    assert result["status"] == "error"
    assert len(calls) == 1, "auth failures must surface immediately"
    assert "Invalid API key" in result["error"]


def test_job_carries_an_append_only_phase_log(monkeypatch):
    """The UI keeps a log across runs; the server has to report phases that
    were already superseded by the time the client next polls."""
    async def rewrite(**kwargs):
        return '<paragraph id="1">Another take on the line, freshly built. It sticks.</paragraph>'

    result = _submit_and_wait(
        _wire(monkeypatch, rewrite),
        {"text": "Source text for the log check.", "endpoint_id": "e1", "model": "m1"},
    )

    log = result["log"]
    assert [e["step"] for e in log][0] == "Queued"
    assert log[-1]["step"] == "Complete"
    assert all("t" in e and "detail" in e for e in log)
    assert "budget" in log[0]["detail"]


def test_log_records_the_failure_reason(monkeypatch):
    async def rewrite(**kwargs):
        raise HTTPException(401, "Invalid API key")

    result = _submit_and_wait(
        _wire(monkeypatch, rewrite),
        {"text": "Source text.", "endpoint_id": "e1", "model": "m1"},
    )

    assert result["log"][-1]["step"] == "Error"
    assert "Invalid API key" in result["log"][-1]["detail"]


def test_metrics_accompany_a_clean_rewrite(monkeypatch):
    source = "The migration finished on Tuesday after three weeks of staged rollout."

    async def rewrite(**kwargs):
        return (
            '<paragraph id="1">Tuesday closed out the migration. After three weeks of '
            "staged rollout, with every batch watched by hand, the last cohort moved "
            "without incident. Quietly done.</paragraph>"
        )

    result = _submit_and_wait(
        _wire(monkeypatch, rewrite),
        {"text": source, "endpoint_id": "e1", "model": "m1"},
    )

    metrics = result["result"]["metrics"]
    assert metrics["fidelity"] == 1.0
    assert metrics["missing_anchors"] == []
    assert 0.0 <= metrics["human_score"] <= 1.0
    for key in ("burstiness", "sentence_length_cv", "opening_diversity", "filler_per_100_words"):
        assert key in metrics
