"""Composer thinking-effort chip.

Sits beside the model chip and only ever offers levels the CURRENT model can
honour — the backend resolves which wire mechanism that is. Two properties are
worth pinning because getting them wrong produces a control that lies:

  * a model whose control is boolean must not be offered low/medium/high;
  * a model with no known control must not show the chip at all.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HTML = (ROOT / "static/index.html").read_text(encoding="utf-8")
JS = (ROOT / "static/js/effortPicker.js").read_text(encoding="utf-8")
CSS = (ROOT / "static/style.css").read_text(encoding="utf-8")
APP_JS = (ROOT / "static/app.js").read_text(encoding="utf-8")
MODEL_PICKER_JS = (ROOT / "static/js/modelPicker.js").read_text(encoding="utf-8")


def _composer_chips_block() -> str:
    start = HTML.index('<div class="composer-chips"')
    depth = 0
    for m in re.finditer(r"<div\b|</div>", HTML[start:]):
        depth += 1 if m.group(0).startswith("<div") else -1
        if depth == 0:
            return HTML[start:start + m.end()]
    raise AssertionError("composer-chips is unbalanced")


def test_both_chips_share_one_positioned_container():
    """They occupy the same top-right corner; without a shared container the
    second chip would overlap the first."""
    block = _composer_chips_block()
    assert 'id="model-picker-wrap"' in block
    assert 'id="effort-picker-wrap"' in block


def test_container_owns_the_absolute_positioning():
    assert ".chat-input-top > .composer-chips {" in CSS
    rule = CSS[CSS.index(".chat-input-top > .composer-chips {"):]
    rule = rule[: rule.index("\n    }")]
    assert "position: absolute" in rule
    assert "display: flex" in rule


def test_typing_autohide_hides_both_chips():
    """app.js hides the model chip once you type. The effort chip has to go
    with it or it is left floating alone over the composer."""
    idx = APP_JS.index("model-picker-autohide")
    window = APP_JS[idx - 200: idx + 400]
    assert "composer-chips" in window
    assert ".chat-input-top > .composer-chips.model-picker-autohide {" in CSS


def test_chip_hides_when_the_model_has_no_control():
    assert "if (!_current || !_current.mechanism) { _hide(); return; }" in JS


def test_menu_only_renders_supported_levels():
    assert "supported.includes(l.value)" in JS


def test_menu_explains_a_short_list():
    """A two-item menu should not look like a bug."""
    assert "supports on/off only" in JS


def test_switching_model_refreshes_the_chip():
    """Which levels exist, and which is selected, are both per-model."""
    assert "refreshEffortPicker" in MODEL_PICKER_JS


def test_capability_lookup_includes_endpoint_identity():
    """Same model id can have different effort controls on different endpoints."""
    assert "endpointId: src.endpointId" in JS
    assert "endpoint_id=${encodeURIComponent(target.endpointId" in JS
    assert "_currentKey === targetKey" in JS
    assert "modelMeta" in Path(ROOT / "static/js/settings.js").read_text(encoding="utf-8")


def test_saving_merges_rather_than_replaces_overrides():
    """A per-model map: writing one model's choice must not drop the others."""
    save = JS[JS.index("async function _savePreference"):]
    save = save[: save.index("\n}")]
    assert "existing" in save and "..." in save
    # "auto" is the absence of an override, not a stored value.
    assert "delete existing[model]" in save


def test_init_is_idempotent():
    """A second bind would toggle the menu twice per click — the button would
    appear to do nothing."""
    assert "effortBound" in JS


def test_hiding_the_chip_closes_its_menu():
    hide = JS[JS.index("function _hide()"):]
    hide = hide[: hide.index("\n}")]
    assert "_closeIfOpen()" in hide
