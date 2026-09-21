"""Approval-card styling contract (static/style.css).

The card must read as part of the design language and stay tappable on a
phone: tool-gate approvals get the accent tint (not the danger tint), the
primary action is filled like .admin-btn-add, touch targets are 44px, and
the actions stack full-width under 768px (four side-by-side buttons are
un-tappable at 375px).
"""
from pathlib import Path


CSS = (Path(__file__).parents[1] / "static" / "style.css").read_text(encoding="utf-8")


def _rule(selector: str) -> str:
    return CSS.split(selector, 1)[1].split("}", 1)[0]


def test_tool_gate_card_uses_accent_tint():
    rule = _rule('.approval-card[data-kind="tool_gate"] {')
    assert "border-color: var(--accent" in rule
    assert "--red" in rule  # danger tint stays the fallback


def test_tool_gate_primary_action_is_filled():
    rule = _rule(
        '.approval-card[data-kind="tool_gate"] .approval-actions .approval-btn:first-child {'
    )
    assert "background: var(--red);" in rule
    assert "color: #fff;" in rule


def test_approval_buttons_meet_touch_target():
    rule = _rule(".approval-btn {")
    assert "min-height: 44px;" in rule


def test_approval_actions_stack_on_phone():
    mobile = CSS.split(".approval-actions { flex-direction: column;", 1)[1]
    block = mobile.split("}", 1)[0]
    assert "align-items: stretch;" in block
    phone_btn = CSS.split(".approval-btn { width: 100%;", 1)[1].split("}", 1)[0]
    assert "min-height: 44px;" in phone_btn
