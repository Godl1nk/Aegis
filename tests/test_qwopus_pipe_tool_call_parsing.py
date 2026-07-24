"""Qwopus / Qwen3-derived local finetunes emit native tool calls with plain
ASCII pipe tokens and an <arg_value> separator, e.g.:

    <|tool_call_begin|>edit_document<arg_value>{"find": "...", "replace": "..."}<|tool_call_end|>
    <|tool_calls_end|>

llama.cpp/Ollama pass these through as visible text, so without normalization
the raw markup floods the chat and the call never executes. These tests pin the
normalize->parse->strip path and the flat single-edit edit_document shape.
"""

from src.agent_tools import parse_tool_blocks, strip_tool_blocks


def test_ascii_pipe_edit_document_parses_and_strips():
    raw = (
        "Here's the change.\n"
        '<|tool_call_begin|>edit_document<arg_value>'
        '{"file_path": "Apple.svelte", '
        '"find": " background: #ff6b5f;", '
        '"replace": " background: #a8e063;"}'
        "<|tool_call_end|>\n<|tool_calls_end|>"
    )

    blocks = parse_tool_blocks(raw)

    assert len(blocks) == 1
    assert blocks[0].tool_type == "edit_document"
    # Flat {find, replace} (no `edits` wrapper) still produces a real edit block.
    assert "<<<FIND>>>\n background: #ff6b5f;\n<<<REPLACE>>>\n background: #a8e063;\n<<<END>>>" in blocks[0].content
    # The raw tool markup must not survive into what the user sees.
    assert strip_tool_blocks(raw).strip() == "Here's the change."
    assert "tool_call_begin" not in strip_tool_blocks(raw)


def test_ascii_pipe_with_tool_sep_separator_parses():
    raw = (
        '<|tool_calls_begin|><|tool_call_begin|>web_search<|tool_sep|>'
        '{"query": "sweden news"}<|tool_call_end|><|tool_calls_end|>'
    )

    blocks = parse_tool_blocks(raw)

    assert len(blocks) == 1
    assert blocks[0].tool_type == "web_search"
    assert blocks[0].content == "sweden news"
    assert strip_tool_blocks(raw).strip() == ""


def test_flat_edit_document_json_still_edits():
    # Direct native-call conversion of the flat shape (no edits wrapper).
    from src.tool_schemas import function_call_to_tool_block

    block = function_call_to_tool_block(
        "edit_document",
        '{"find": "red", "replace": "green"}',
    )

    assert block is not None
    assert block.tool_type == "edit_document"
    assert "<<<FIND>>>\nred\n<<<REPLACE>>>\ngreen\n<<<END>>>" in block.content


def test_plain_arg_value_token_without_tool_call_is_untouched():
    # A stray "<arg_value>" outside a tool-call block must not be rewritten.
    raw = "The function takes an <arg_value> placeholder in its docs."
    assert strip_tool_blocks(raw) == raw
    assert parse_tool_blocks(raw) == []
