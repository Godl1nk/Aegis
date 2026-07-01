import sys
from collections import namedtuple
from pathlib import Path
from unittest.mock import MagicMock


original_agent_tools = sys.modules.get("src.agent_tools")
original_tool_parsing = sys.modules.get("src.tool_parsing")
agent_tools_stub = MagicMock()
agent_tools_stub.ToolBlock = namedtuple("ToolBlock", ["tool_type", "content"])
agent_tools_stub.TOOL_TAGS = {
    "bash", "python", "web_search", "create_document", "update_document",
    "edit_document", "write_file", "edit_file",
}
sys.modules["src.agent_tools"] = agent_tools_stub
sys.modules.pop("src.tool_parsing", None)

from src.tool_parsing import parse_tool_blocks, strip_tool_blocks

if original_agent_tools is None:
    sys.modules.pop("src.agent_tools", None)
else:
    sys.modules["src.agent_tools"] = original_agent_tools
if original_tool_parsing is None:
    sys.modules.pop("src.tool_parsing", None)
else:
    sys.modules["src.tool_parsing"] = original_tool_parsing


DOCUMENT_TOOLS = (
    Path(__file__).resolve().parents[1] / "src/agent_tools/document_tools.py"
).read_text(encoding="utf-8")


def _hybrid_call(content_parameter: str = "") -> str:
    content = (
        f"<parameter=content>\n{content_parameter}\n</parameter>\n"
        if content_parameter
        else ""
    )
    return (
        "```create_document\n\n"
        "<parameter=language>\nhtml\n</parameter>\n"
        "<parameter=title>\nBrowserOS\n</parameter>\n"
        f"{content}"
        "</function>\n</tool_call>"
    )


def test_incomplete_hybrid_document_call_is_not_executed():
    assert parse_tool_blocks(_hybrid_call()) == []


def test_complete_hybrid_document_call_is_normalized():
    blocks = parse_tool_blocks(_hybrid_call("<!doctype html><title>OS</title>"))

    assert len(blocks) == 1
    assert blocks[0].tool_type == "create_document"
    assert blocks[0].content == (
        "BrowserOS\nhtml\n<!doctype html><title>OS</title>"
    )


def test_hybrid_document_envelope_is_removed_from_visible_reply():
    cleaned = strip_tool_blocks(
        "Starting.\n" + _hybrid_call() + "\n",
        skip_fenced=True,
    )

    assert cleaned == "Starting."


def test_document_executor_has_defense_in_depth_validation():
    assert "Malformed create_document call" in DOCUMENT_TOOLS
    assert "Cannot create an empty document" in DOCUMENT_TOOLS
