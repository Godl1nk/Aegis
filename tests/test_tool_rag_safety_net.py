"""Safety-net tool selection: no turn should ever send ALL schemas.

When every selection path leaves _relevant_tools None (empty retrieval
query, skipped index), the send site used to fall back to all ~70 schemas
(~15k tokens). _safety_net_tool_selection seeds the same focused
ingredients instead. Guide-only turns bypass it (legacy behaviour kept).
"""
import json

from src.agent_loop import _safety_net_tool_selection
from src.tool_index import ALWAYS_AVAILABLE


def test_empty_input_is_exactly_always_available():
    assert _safety_net_tool_selection(()) == set(ALWAYS_AVAILABLE)
    assert _safety_net_tool_selection(None) == set(ALWAYS_AVAILABLE)


def test_unknown_domains_ignored():
    assert _safety_net_tool_selection(("nope", None, 123)) == set(ALWAYS_AVAILABLE)


def test_domain_union():
    tools = _safety_net_tool_selection(("email",))
    assert {"list_emails", "send_email", "read_email"} <= tools
    assert set(ALWAYS_AVAILABLE) <= tools
    # unrelated domains stay out
    assert "serve_model" not in tools
    assert "manage_calendar" not in tools


def test_sticky_admin_doc_unions():
    tools = _safety_net_tool_selection(
        ("ui",),
        sticky={"grep", "read_file"},
        needs_admin=True,
        doc_editable=True,
    )
    assert {"grep", "read_file", "ui_control"} <= tools
    assert {"edit_document", "update_document", "suggest_document"} <= tools
    from src.agent_loop import _ADMIN_TOOLS
    assert set(_ADMIN_TOOLS) <= tools


def test_no_admin_no_doc_by_default():
    tools = _safety_net_tool_selection(("files",))
    assert {"read_file", "grep", "bash"} <= tools
    assert "edit_document" not in tools


def test_safety_net_costs_fraction_of_send_all():
    from src.agent_loop import FUNCTION_TOOL_SCHEMAS
    by_name = {t.get("function", {}).get("name"): t for t in FUNCTION_TOOL_SCHEMAS}
    net = _safety_net_tool_selection(("email", "files"), sticky={"grep"})
    net_chars = len(json.dumps([by_name[n] for n in net if n in by_name]))
    all_chars = len(json.dumps(FUNCTION_TOOL_SCHEMAS))
    assert len(net) < len(FUNCTION_TOOL_SCHEMAS) // 2
    assert net_chars < all_chars // 2
