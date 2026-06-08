"""Tests for image context collection (agent_loop._collect_image_context).

The old chat_routes image bypass routing functions (_is_image_generation_session,
_looks_like_image_turn, etc.) have been removed — image generation now goes
through the normal agent tool loop. These tests cover the surviving
_collect_image_context helper that gathers reference images for follow-up edits.
"""

from src.agent_loop import _collect_image_context


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


def test_collect_image_context_always_uses_current_upload():
    messages = [
        {"role": "user", "content": "edit this", "metadata": {"attachments": [{
            "id": "up1",
            "mime": "image/png",
        }]}},
    ]

    assert _collect_image_context(messages, "edit this") == ["upload:up1"]
