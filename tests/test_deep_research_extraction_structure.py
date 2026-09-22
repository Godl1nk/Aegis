import asyncio

from src.deep_research import DeepResearcher


def test_unstructured_extraction_is_not_published_as_a_finding(monkeypatch):
    researcher = DeepResearcher.__new__(DeepResearcher)
    researcher.max_content_chars = 15000
    researcher.extraction_timeout = 90
    researcher.urls_fetched = set()
    researcher._emit = lambda **_kwargs: None

    async def fake_llm(*_args, **_kwargs):
        return (
            "Need answer user's request. Need extract relevant info and output "
            "JSON with fields rational, evidence, summary."
        )

    researcher._llm = fake_llm
    monkeypatch.setattr(
        "src.search.fetch_webpage_content",
        lambda *_args, **_kwargs: {
            "success": True,
            "content": "Relevant market article content.",
            "title": "Market article",
        },
    )

    finding = asyncio.run(researcher._fetch_and_extract(
        "https://example.com/article",
        "Create a market briefing",
        "Market article",
    ))

    assert finding is None
