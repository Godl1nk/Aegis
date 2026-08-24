"""Source-level guards for the skill card's structured editor.

skills.js needs browser globals, so it can't be imported under node — these
pin the affordances at the source level, the same way the sibling
test_skill_edit_no_collapse_on_outside_click_js.py does.

Covers three things that were wrong in the first cut of the form:

  * No way out without saving. The card click handler deliberately swallows
    clicks while editing so edits aren't lost, so without a Cancel the only
    exit was Save.
  * Fields carried the Add Skill form's overlay placeholder, which hides as
    soon as the field has content. Every field here is pre-filled, so the
    labels were invisible and Name/Title read as the same box twice.
  * The single-line inputs are column flex items and collapsed to a sliver
    when the form ran out of room.
"""
from pathlib import Path

JS = (Path(__file__).resolve().parent.parent / "static/js/skills.js").read_text(encoding="utf-8")
CSS = (Path(__file__).resolve().parent.parent / "static/style.css").read_text(encoding="utf-8")


def _rule_body(text: str) -> str:
    """The declarations of the rule `text` starts with.

    Cuts at the first line-leading `}` rather than the first `}`: comments in
    this stylesheet contain literal braces (e.g. `max-height: 30lh }`), which a
    naive cut would stop at, silently skipping the declarations after it.
    """
    return text[: text.index("\n}")]


def test_editor_has_a_cancel_that_discards():
    assert "skill-cancel-btn" in JS, "the editor must offer a Cancel"
    exit_fn = JS[JS.index("function _exitEditMode"):]
    exit_fn = exit_fn[: exit_fn.index("\n}\n")]
    # Cancel tears down BOTH editors and restores the read-only preview.
    assert ".skill-form')?.remove()" in exit_fn
    assert ".skill-md-editor')?.remove()" in exit_fn
    assert "removeProperty('display')" in exit_fn
    # It must not write anything back.
    assert "fetch(" not in exit_fn, "Cancel must discard, never save"


def test_fields_use_persistent_labels_not_the_overlay_placeholder():
    field_fn = JS[JS.index("function _skillField"):]
    field_fn = field_fn[: field_fn.index("\n}\n")]
    assert "skill-form-label" in field_fn
    assert "createElement('label')" in field_fn
    # The overlay placeholder vanishes on a filled field — it can't be the
    # only thing naming these.
    assert "skill-rich-ph" not in field_fn


def test_single_line_inputs_do_not_shrink_in_the_flex_column():
    block = CSS[CSS.index(".skill-form-input {"):]
    block = _rule_body(block)
    assert "flex: none" in block


def test_steps_field_takes_the_slack_height():
    assert ".skill-form .skill-form-field-grow {" in CSS
    grow = CSS[CSS.index(".skill-form .skill-form-field-grow textarea.skill-form-input {"):]
    grow = _rule_body(grow)
    assert "min-height" in grow
    # It GROWS into its wrapper. height:100% would overflow by the label's
    # height and paint over the next field's label ("TAGS" on top of the box).
    assert "flex: 1 1 auto" in grow
    assert "height: 100%" not in grow
    # The global `textarea { max-height: 30lh }` otherwise caps it mid-card and
    # leaves a dead gap between How and Tags.
    assert "max-height: none" in grow
    # Must come after the generic textarea min-height or source order loses.
    assert CSS.index(".skill-form textarea.skill-form-input {") < CSS.index(
        ".skill-form .skill-form-field-grow textarea.skill-form-input {"
    )


def test_grow_wrapper_cannot_shrink_below_its_content():
    """As a flex item with min-height:0 it collapsed to zero height while its
    textarea kept its floor and spilled over the field below."""
    wrap = CSS[CSS.index(".skill-form .skill-form-field-grow {"):]
    wrap = _rule_body(wrap)
    assert "min-height: auto" in wrap


def test_editing_lets_the_preview_grow():
    """The preview is flex:0 1 auto for READING (a short SKILL.md shouldn't
    leave a void above the footer). While editing that pinned the raw editor to
    its floor with the card empty below it."""
    rule = CSS[CSS.index(".skill-card.doclib-card-expanded.skill-card-editing .doclib-card-preview {"):]
    rule = _rule_body(rule)
    assert "flex: 1 1 auto" in rule
    assert "max-height: none" in rule
