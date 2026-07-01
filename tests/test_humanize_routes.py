import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

import routes.humanize_routes as humanize_routes
from routes.humanize_routes import (
    _SYSTEM_PROMPT,
    _clean_model_output,
    _clean_paragraph_output,
    _compute_human_score,
    _document_prompt,
    _extract_rewritten_paragraphs,
    _infer_paragraph_breaks,
    _join_paragraphs,
    _length_ratio,
    _needs_stronger_rewrite,
    _ngram_diversity,
    _rewrite_similarity,
    _sentence_length_stats,
    _sentence_starter_diversity,
    _split_document,
    _validate_candidate,
)

_SAMPLE_SOURCE = (
    "Artificial intelligence is one of the most important technologies in the "
    "world today. It is used across many technical fields."
)
_SAMPLE_REWRITE = (
    "Artificial intelligence is among the most significant technologies of "
    "the modern era, with applications across numerous technical domains."
)


def _submit_and_wait(app: FastAPI, payload: dict) -> tuple:
    with TestClient(app) as client:
        accepted = client.post("/api/humanize", json=payload)
        assert accepted.status_code == 202
        job_id = accepted.json()["job_id"]
        for _ in range(100):
            status = client.get(f"/api/humanize/status/{job_id}")
            assert status.status_code == 200
            if status.json()["status"] in {"done", "error"}:
                return accepted, status
            time.sleep(0.01)
    raise AssertionError("Rewrite job did not finish")


def test_rewrite_plan_is_embedded_in_system_prompt():
    assert "Preserve every fact and meaning exactly" in _SYSTEM_PROMPT
    assert "Maintain paragraph count and order of ideas" in _SYSTEM_PROMPT
    assert "Keep technical terms accurate" in _SYSTEM_PROMPT


def test_prompt_rejects_detector_optimization():
    assert "Return only the tagged rewritten paragraphs" in _SYSTEM_PROMPT


def test_clean_model_output_removes_common_wrappers():
    assert _clean_model_output("Rewrite: Changed prose.") == "Changed prose."
    assert _clean_model_output("```text\nChanged prose.\n```") == "Changed prose."
    assert _clean_model_output("<think>notes</think>\nChanged prose.") == "Changed prose."


def test_clean_model_output_strips_thinking_preamble():
    tagged = '<paragraph id="1">Rewritten text.</paragraph>'
    preamble = (
        '<paragraph id="N">...</paragraph> tags only. No extra text.\n'
        "1. **Analyze** the input\n"
        "2. **Plan** the rewrite\n\n" + tagged
    )
    assert _clean_model_output(preamble) == tagged

    # Also verify trailing reasoning after the last tag is stripped.
    trailing = tagged + "\n\nCheck: avg sentence length is 18 words."
    assert _clean_model_output(trailing) == tagged



def test_clean_paragraph_output_removes_model_added_blank_lines():
    assert _clean_paragraph_output("First sentence.\n\nSecond sentence.") == (
        "First sentence. Second sentence."
    )


def test_sample_rewrite_has_safe_length():
    ratio = _length_ratio(_SAMPLE_SOURCE, _SAMPLE_REWRITE)
    assert 0.55 < ratio < 1.35


def test_near_copy_is_rejected_as_failed_rewrite():
    source = (
        "Artificial intelligence is one of the most important technologies in "
        "the world today and it is used across many different fields."
    )
    near_copy = (
        "Artificial intelligence is one of the most important technologies in "
        "the world today, and it is used across many different fields."
    )
    assert _rewrite_similarity(source, near_copy) > 0.95
    assert _needs_stronger_rewrite([source], [near_copy])


def test_sample_rewrite_passes_distance_gate():
    _validate_candidate(_SAMPLE_SOURCE, _SAMPLE_REWRITE)
    assert not _needs_stronger_rewrite([_SAMPLE_SOURCE], [_SAMPLE_REWRITE])


def test_document_split_preserves_exact_blank_line_separators():
    paragraphs, separators = _split_document("First paragraph.\n\n\nSecond paragraph.\r\n \r\nThird.")
    assert paragraphs == ["First paragraph.", "Second paragraph.", "Third."]
    assert separators == ["\n\n\n", "\r\n \r\n"]


def test_document_prompt_marks_paragraphs_and_sentence_counts():
    prompt = _document_prompt(["First sentence. Second sentence. Third sentence.", "Last."])
    assert '<paragraph id="1">' in prompt
    assert '<paragraph id="2">' in prompt
    assert "3 source sentence(s)" in prompt
    assert "Output only the tags" in prompt


def test_tagged_output_restores_paragraphs_in_source_order():
    source = ["First source paragraph.", "Second source paragraph."]
    raw = (
        '<paragraph id="2">Second rewritten paragraph.</paragraph>'
        '<paragraph id="1">First rewritten paragraph.</paragraph>'
    )
    rewritten = _extract_rewritten_paragraphs(raw, source)
    assert _join_paragraphs(rewritten, ["\n\n"]) == (
        "First rewritten paragraph.\n\nSecond rewritten paragraph."
    )


def test_flat_output_is_repartitioned_without_another_model_call():
    source = [
        "First source sentence. Another sentence belongs in the opening paragraph.",
        "The final paragraph has one sentence.",
    ]
    flat = (
        "A rewritten opening sentence appears here. "
        "Its related sentence remains beside it. "
        "A separate closing thought finishes the text."
    )
    rewritten = _extract_rewritten_paragraphs(flat, source)
    assert len(rewritten) == 2
    assert rewritten[0].endswith("beside it.")
    assert rewritten[1] == "A separate closing thought finishes the text."


def test_route_uses_owned_endpoint_without_custom_generation_params(monkeypatch):
    calls = []
    monkeypatch.setattr(humanize_routes, "require_user", lambda request: "alice")

    def resolve(endpoint_id, model, owner):
        calls.append(("resolve", endpoint_id, model, owner))
        return "http://model.test/v1/chat/completions", model, {"Authorization": "secret"}

    async def rewrite(**kwargs):
        calls.append(("llm", kwargs))
        return "This rewritten passage uses different wording and remains complete."

    monkeypatch.setattr(humanize_routes, "resolve_endpoint_by_id", resolve)
    monkeypatch.setattr(humanize_routes, "llm_call_async", rewrite)

    app = FastAPI()
    app.include_router(humanize_routes.setup_humanize_routes())
    accepted, response = _submit_and_wait(
        app,
        {
            "text": "This source passage has enough words to test a rewritten result.",
            "endpoint_id": "endpoint-1",
            "model": "writer-model",
        },
    )

    assert accepted.status_code == 202
    assert response.json()["status"] == "done"
    assert calls[0] == ("resolve", "endpoint-1", "writer-model", "alice")
    llm_calls = [c for c in calls if c[0] == "llm"]
    assert 1 <= len(llm_calls) <= 2  # first attempt may suffice or retry
    assert all(c[1]["omit_generation_params"] is True for c in llm_calls)
    assert all("temperature" not in c[1] for c in llm_calls)
    assert all("top_p" not in c[1] for c in llm_calls)
    assert all("max_tokens" not in c[1] for c in llm_calls)
    assert all(c[1]["messages"][0]["content"] == _SYSTEM_PROMPT for c in llm_calls)


def test_infer_paragraph_breaks_inserts_separator_on_opener():
    text = (
        "AI was used for automation and data analysis. "
        "It could also classify images and detect patterns. "
        "The development of AI nowadays is progressing rapidly."
    )
    result = _infer_paragraph_breaks(text)
    assert "\n\n" in result
    paras, _ = _split_document(result)
    assert len(paras) == 2


def test_infer_paragraph_breaks_skips_when_blank_lines_exist():
    text = "First paragraph.\n\nThe development is rapid."
    assert _infer_paragraph_breaks(text) == text


def test_infer_paragraph_breaks_skips_short_text():
    text = "One sentence. Another sentence."
    assert _infer_paragraph_breaks(text) == text


def test_route_rewrites_paragraphs_separately_and_restores_separator(monkeypatch):
    calls = []
    monkeypatch.setattr(humanize_routes, "require_user", lambda request: "alice")
    monkeypatch.setattr(
        humanize_routes,
        "resolve_endpoint_by_id",
        lambda endpoint_id, model, owner: ("http://model.test/chat", model, {}),
    )

    async def rewrite(**kwargs):
        calls.append(kwargs)
        return (
            '<paragraph id="1">The opening section has been recast with distinctly '
            'different language.</paragraph>'
            '<paragraph id="2">A separate closing section now uses another sentence '
            "structure.</paragraph>"
        )

    monkeypatch.setattr(humanize_routes, "llm_call_async", rewrite)
    app = FastAPI()
    app.include_router(humanize_routes.setup_humanize_routes())
    accepted, response = _submit_and_wait(
        app,
        {
            "text": (
                "This opening paragraph contains enough words for a complete rewrite."
                "\n\n\n"
                "This closing paragraph remains separate and also contains enough words."
            ),
            "endpoint_id": "endpoint-1",
            "model": "writer-model",
        },
    )

    assert accepted.status_code == 202
    assert response.json()["result"]["text"] == (
        "The opening section has been recast with distinctly different language."
        "\n\n\n"
        "A separate closing section now uses another sentence structure."
    )
    assert len(calls) == 1  # first attempt passes both checks


def test_sentence_length_stats_uniform():
    stats = _sentence_length_stats("Short. Medium length here. Longer sentence with more words.")
    assert stats["cv"] > 0.3


def test_sentence_length_stats_empty():
    stats = _sentence_length_stats("")
    assert stats["mean"] == 0.0


def test_ngram_diversity_identical():
    text = "the the the the the"
    assert _ngram_diversity(text, 2) < 0.3


def test_ngram_diversity_highly_varied():
    text = "the quick brown fox jumps over the lazy dog"
    assert _ngram_diversity(text, 2) > 0.5


def test_sentence_starter_diversity_all_different():
    text = "Apples are tasty. Bananas are yellow. Cherries are sweet."
    assert _sentence_starter_diversity(text) == 1.0


def test_sentence_starter_diversity_repeated():
    text = "It is warm. It is sunny. It is nice."
    assert _sentence_starter_diversity(text) < 0.6


def test_compute_human_score_highly_varied():
    varied = (
        "Go. A truly varied sentence uses unique words. "
        "Short phrases make writing feel natural and alive. "
        "It is important to mix things up. "
        "Longer flowing structures occasionally appear in human writing, "
        "but they should never dominate the overall texture. "
        "Punch. Rhythm. Flow."
    )
    score = _compute_human_score(varied)
    assert score > 0.5


def test_compute_human_score_monotone():
    monotone = (
        "This is a sentence with exactly nine words. "
        "This is another sentence with nine words. "
        "This is the third sentence with nine words. "
        "This is a fourth sentence with nine words. "
        "This is the fifth sentence with nine words."
    )
    score = _compute_human_score(monotone)
    assert score < 0.5


def test_route_runs_parallel_candidates_and_picks_best(monkeypatch):
    calls = []
    source = (
        "Artificial intelligence is one of the most important technologies in "
        "the world today and supports work across many different fields."
    )
    good_rewrite = (
        '<paragraph id="1">Across numerous professional domains, modern '
        "AI has emerged as a crucial technology shaping how people work "
        "throughout the world.</paragraph>"
    )
    monkeypatch.setattr(humanize_routes, "require_user", lambda request: "alice")
    monkeypatch.setattr(
        humanize_routes,
        "resolve_endpoint_by_id",
        lambda endpoint_id, model, owner: ("http://model.test/chat", model, {}),
    )

    async def rewrite(**kwargs):
        calls.append(kwargs)
        user_content = kwargs["messages"][1]["content"]
        # Divergent call returns good rewrite; normal call returns near-copy
        if "substantially different" in user_content:
            return good_rewrite
        return f'<paragraph id="1">{source}</paragraph>'

    monkeypatch.setattr(humanize_routes, "llm_call_async", rewrite)
    app = FastAPI()
    app.include_router(humanize_routes.setup_humanize_routes())
    accepted, response = _submit_and_wait(
        app,
        {"text": source, "endpoint_id": "endpoint-1", "model": "writer-model"},
    )

    assert accepted.status_code == 202
    assert response.json()["result"]["text"].startswith("Across numerous professional domains")
    assert len(calls) == 2
