"""Tests for image context collection (agent_loop._collect_image_context).

The old chat_routes image bypass routing functions (_is_image_generation_session,
_looks_like_image_turn, etc.) have been removed — image generation now goes
through the normal agent tool loop. These tests cover the surviving
_collect_image_context helper that gathers reference images for follow-up edits.
"""

import pytest

from src.agent_loop import _collect_image_context, _is_explicit_image_ref
from src.tool_execution import execute_tool_block, _strip_generate_image_references


class _Block:
    tool_type = "generate_image"

    def __init__(self, content):
        self.content = content


def test_collect_image_context_uses_previous_image_for_referential_edit():
    messages = [
        {"role": "assistant", "content": "", "metadata": {"tool_events": [{
            "tool": "generate_image",
            "image_url": "/api/generated-image/abc.png",
        }]}},
        {"role": "user", "content": "the words on top r abit off", "metadata": {}},
    ]

    assert _collect_image_context(messages, "the words on top r abit off") == ["/api/generated-image/abc.png"]


def test_collect_image_context_does_not_reuse_previous_image_for_fresh_request():
    messages = [
        {"role": "assistant", "content": "", "metadata": {"tool_events": [{
            "tool": "generate_image",
            "image_url": "/api/generated-image/abc.png",
        }]}},
        {"role": "user", "content": "draw a clean mountain logo", "metadata": {}},
    ]

    assert _collect_image_context(messages, "draw a clean mountain logo") == []


def test_collect_image_context_does_not_reuse_previous_image_for_another_request():
    messages = [
        {"role": "assistant", "content": "", "metadata": {"tool_events": [{
            "tool": "generate_image",
            "image_url": "/api/generated-image/abc.png",
        }]}},
        {"role": "user", "content": "generate another image of british short hair cat", "metadata": {}},
    ]

    assert _collect_image_context(messages, "generate another image of british short hair cat") == []


def test_collect_image_context_always_uses_current_upload():
    messages = [
        {"role": "user", "content": "edit this", "metadata": {"attachments": [{
            "id": "up1",
            "mime": "image/png",
        }]}},
    ]

    assert _collect_image_context(messages, "edit this") == ["upload:up1"]


def test_collect_image_context_uses_previous_upload_for_followup_edit():
    messages = [
        {"role": "user", "content": "read", "metadata": {"attachments": [{
            "id": "up1",
            "mime": "image/png",
        }]}},
        {"role": "assistant", "content": "It is a benchmark chart.", "metadata": {}},
        {"role": "user", "content": "edit the image and make qwen's score higher", "metadata": {}},
    ]

    assert _collect_image_context(messages, "make the dark purple bar higher") == ["upload:up1"]


def test_hyphenated_gallery_uuid_is_explicit_image_ref():
    assert _is_explicit_image_ref("550e8400-e29b-41d4-a716-446655440000")


def test_generate_image_references_are_stripped_from_json_args():
    content = (
        '{"prompt":"a british shorthair cat","model":"auto","size":"1024x1024",'
        '"quality":"high","reference_image_urls":["/api/generated-image/abc.png"]}'
    )

    stripped = _strip_generate_image_references(content)

    assert "reference_image_urls" not in stripped
    assert "a british shorthair cat" in stripped


@pytest.mark.asyncio
async def test_execute_generate_image_ignores_image_context(monkeypatch):
    captured = {}

    async def fake_generate(content, **kwargs):
        captured.update(kwargs)
        return {"image_url": "/api/generated-image/new.png"}

    monkeypatch.setattr("src.ai_interaction.do_generate_image", fake_generate)

    _, result = await execute_tool_block(
        _Block('{"prompt":"a new cat","reference_image_urls":["/api/generated-image/old.png"]}'),
        image_context=["/api/generated-image/old.png"],
    )

    assert result["image_url"] == "/api/generated-image/new.png"
    assert captured["reference_image_urls"] is None
