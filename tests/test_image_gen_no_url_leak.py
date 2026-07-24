"""After a successful image generation, the /api/generated-image/<id> URL must
NOT be fed back into the model's history — otherwise the model imitates it and,
on the next "another one" turn, writes a fabricated image link instead of
calling generate_image again (image never generates, link 404s).
"""

from pathlib import Path

import src.agent_tools  # noqa: F401  (resolve circular init)
from src.tool_schemas import FUNCTION_TOOL_SCHEMAS

AGENT_LOOP = Path("src/agent_loop.py").read_text(encoding="utf-8")


def test_image_tool_result_is_scrubbed_of_url_before_feedback():
    # The fed-back result text for image tools is replaced with a URL-free
    # confirmation that steers the model back to the tool.
    assert 'block.tool_type in ("generate_image", "ai_edit_image")' in AGENT_LOOP
    assert "already displayed to the user" in AGENT_LOOP
    assert "Do NOT write an image URL" in AGENT_LOOP
    assert "call the generate_image" in AGENT_LOOP


def test_generate_image_description_forbids_writing_urls():
    desc = next(
        s["function"]["description"]
        for s in FUNCTION_TOOL_SCHEMAS
        if s.get("function", {}).get("name") == "generate_image"
    )
    assert "NEVER write an image URL" in desc
    assert "/api/generated-image/" in desc
    # Follow-ups like "another one" must still route to the tool.
    assert "another one" in desc
