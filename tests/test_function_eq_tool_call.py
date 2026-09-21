"""Equator-style <function=tool> + <parameter=name> tool-call dialect.

Seen live: Qwen emitting task creation as

    <tool_call>
    <function=manage_tasks>
    <parameter=action>
    create
    </parameter>
    ...

instead of <invoke name="">. Before this fix the inner call parsed to zero
blocks (no Activity, no execution) while display stripping removed the
envelope — the call flashed mid-stream, then vanished with nothing happening.
These tests pin the normalize->parse->strip path for the dialect, including
unclosed tails and the no-false-positive contract.
"""

import time

from src.agent_tools import parse_tool_blocks, strip_tool_blocks


REPORTED = (
    "I'll set up three recurring daily tasks for you.\n"
    "<tool_call>\n"
    "<function=manage_tasks>\n"
    "<parameter=action>\n"
    "create\n"
    "</parameter>\n"
    "<parameter=title>\n"
    "US Market News Briefing\n"
    "</parameter>\n"
    "</function>\n"
    "</tool_call>"
)


def test_reported_shape_parses_to_manage_tasks():
    blocks = parse_tool_blocks(REPORTED)
    assert len(blocks) == 1
    assert blocks[0].tool_type == "manage_tasks"
    assert '"create"' in blocks[0].content
    assert "US Market News Briefing" in blocks[0].content


def test_reported_shape_strips_clean():
    cleaned = strip_tool_blocks(REPORTED)
    assert "<tool" not in cleaned
    assert "<function" not in cleaned
    assert "<parameter" not in cleaned
    assert "I'll set up three recurring daily tasks for you." in cleaned


def test_bare_function_without_wrapper_parses():
    raw = '<function=manage_tasks><parameter=action>create</parameter></function>'
    blocks = parse_tool_blocks(raw)
    assert len(blocks) == 1
    assert blocks[0].tool_type == "manage_tasks"


def test_name_attr_spelling_parses():
    raw = '<function name="manage_tasks"><parameter=action>create</parameter></function>'
    blocks = parse_tool_blocks(raw)
    assert len(blocks) == 1
    assert blocks[0].tool_type == "manage_tasks"


def test_unclosed_tail_still_parses():
    raw = (
        "<tool_call>\n"
        "<function=manage_tasks>\n"
        "<parameter=action>\n"
        "create\n"
        "</parameter>\n"
    )
    blocks = parse_tool_blocks(raw)
    assert len(blocks) == 1
    assert blocks[0].tool_type == "manage_tasks"
    assert strip_tool_blocks(raw) == ""


def test_consecutive_unclosed_calls_do_not_cannibalize():
    raw = (
        "<function=bash><parameter=command>echo one</parameter>\n"
        "<function=bash><parameter=command>echo two</parameter>\n"
    )
    blocks = parse_tool_blocks(raw)
    assert [b.tool_type for b in blocks] == ["bash", "bash"]
    assert "echo one" in blocks[0].content
    assert "echo two" in blocks[1].content
    assert "echo two" not in blocks[0].content


def test_unknown_tool_name_parses_to_nothing_but_strips():
    raw = "<function=frobnicate><parameter=action>go</parameter></function>"
    assert parse_tool_blocks(raw) == []
    assert strip_tool_blocks(raw) == ""


def test_plain_prose_function_mention_untouched():
    raw = "Use the <function> key to confirm the dialog."
    assert parse_tool_blocks(raw) == []
    assert strip_tool_blocks(raw) == raw


def test_opener_flood_without_closers_stays_fast():
    raw = "<function=bash>\n" * 2000
    start = time.monotonic()
    assert parse_tool_blocks(raw) == []
    assert strip_tool_blocks(raw) == ""
    assert time.monotonic() - start < 5
