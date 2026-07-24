"""Mobile UX regressions found by auditing the live UI at a 375px viewport.

Each of these was reproduced in-browser before the fix and re-verified after.
"""

from pathlib import Path

CSS = Path("static/style.css").read_text(encoding="utf-8")
UI_JS = Path("static/js/ui.js").read_text(encoding="utf-8")
SEARCH_JS = Path("static/js/search-chat.js").read_text(encoding="utf-8")


def test_modal_close_x_only_hidden_when_swipe_is_armed():
    """The mobile CSS hides the modal × because swipe-down replaces it — but
    that CSS keyed off VIEWPORT WIDTH while the swipe handler keys off TOUCH
    SUPPORT. A narrow non-touch window got neither (verified live:
    closeBtnHidden=true, hasTouch=false) leaving all 7 modals closable only by
    Escape. The × must only be hidden when the gesture actually exists."""
    # The handler marks the root exactly where it binds, so they cannot diverge.
    assert "document.documentElement.classList.add('has-touch')" in UI_JS
    # Anchor on the SWIPE block specifically (ui.js has several
    # 'ontouchstart' guards) — the class must be set where swipe binds.
    idx = UI_JS.index("Mobile swipe-down-to-dismiss")
    assert "has-touch" in UI_JS[idx:idx + 1200]

    # Every close/minimize hide rule is gated on that class.
    for sel in (
        "html.has-touch .modal-content .modal-close",
        "html.has-touch .modal-content .close-btn",
        "html.has-touch .memory-modal-content .modal-close",
        "html.has-touch .settings-modal-content .modal-close",
        "html.has-touch .minimize-btn",
    ):
        assert sel in CSS, sel
    # No ungated rule may hide them again.
    assert "\n      .modal-content .modal-close,\n" not in CSS


def test_search_overlay_collapses_sidebar_on_mobile():
    """Search is opened FROM the mobile sidebar, which is a full-height
    overlay — without collapsing it the sidebar sat on top of the search UI
    (verified live: search-overlay open at 375px wide, sidebar still visible).
    Modal panels already collapse it; the search overlay is not a .modal so it
    never did."""
    assert "import { collapseSidebarToRail } from './modalSnap.js';" in SEARCH_JS
    open_fn = SEARCH_JS[SEARCH_JS.index("export function openSearch()"):]
    open_fn = open_fn[:open_fn.index("export function closeSearch()")]
    assert "window.innerWidth <= 768" in open_fn
    assert "collapseSidebarToRail()" in open_fn
    # Autofocus (and its keyboard) is desktop-only so results stay visible.
    assert "if (window.innerWidth > 768) input.focus();" in open_fn


def test_message_role_row_keeps_timestamp_on_screen():
    """Long model-route labels are nowrap, so .role grew to 395px inside a
    343px bubble and pushed the timestamp fully off-screen (right 417 > 375).
    Wrapping keeps both readable."""
    idx = CSS.index("Mobile readability fixes")
    block = CSS[idx:]
    assert ".msg .role {" in block
    assert "flex-wrap: wrap;" in block
    assert "white-space: normal;" in block


def test_thinking_text_is_readable_on_mobile():
    """0.85em on top of the reduced mobile base landed at ~10.4px."""
    idx = CSS.index("Mobile readability fixes")
    assert ".thinking-content-inner { font-size: 12px; }" in CSS[idx:]


def test_hover_only_controls_rest_visible_on_touch():
    """Touch devices have no hover, so hover-revealed controls were
    functionally invisible."""
    # The codebase already has hover:none blocks; assert on the one that
    # restores these two specific controls.
    idx = CSS.index(".adm-model-row:hover .adm-model-img-btn")
    block = CSS[idx:idx + 500]
    assert "@media (hover: none)" in block
    assert ".adm-model-img-btn { opacity" in block
    assert ".folder-delete-btn { opacity" in block


DOC_JS = Path("static/js/document.js").read_text(encoding="utf-8")
SIDEBAR_JS = Path("static/js/sidebar-layout.js").read_text(encoding="utf-8")
INDEX_HTML = Path("static/index.html").read_text(encoding="utf-8")


def test_doc_view_has_a_mobile_exit():
    """Documents view was a hard trap on a phone. Verified live at 375px with
    touch simulated: hamburger hidden, icon rail hidden, .doc-mobile-footer
    display:none !important, and a fresh panel renders a ghost tab that carries
    no × at all -> "TRAPPED - no on-screen exit". Only Escape got you out, and
    a phone has no Escape key. The agent auto-opens this view when it writes a
    code document, so a user could land in it without asking."""
    # The button is rendered into the tab bar (which IS on screen in doc-view).
    assert 'id="doc-tab-back"' in DOC_JS
    assert 'class="doc-tab-back"' in DOC_JS

    # ...and wired to the function that actually leaves the view.
    idx = DOC_JS.index("Wire mobile back-to-chat")
    wiring = DOC_JS[idx:idx + 400]
    assert "doc-tab-back" in wiring
    assert "closePanel()" in wiring

    # Desktop hides it (hamburger + rail already exist there); mobile shows it.
    assert ".doc-tab-back { display: none; }" in CSS
    mobile_idx = CSS.index(".doc-mobile-footer { display: none !important; }")
    assert ".doc-tab-back" in CSS[mobile_idx:mobile_idx + 700]
    assert "display: flex" in CSS[mobile_idx:mobile_idx + 700]


def test_dismissing_tap_does_not_also_hit_the_element_underneath():
    """The sidebar backdrop that would absorb the dismissing tap is disabled
    (#sidebar-backdrop is display:none !important because it renders above the
    icon rail, which has no z-index). So a tap meant to close the sidebar also
    activated whatever was under it. The close-on-outside-click handler runs in
    capture phase and swallows that tap instead."""
    idx = SIDEBAR_JS.index("Click outside sidebar / icon rail to close")
    block = SIDEBAR_JS[idx:idx + 2500]
    assert "e.stopPropagation()" in block
    assert "e.preventDefault()" in block
    assert "}, true);" in block, "listener must be registered in capture phase"


def test_sidebar_js_breakpoint_matches_the_css_breakpoint():
    """The CSS mobile breakpoint is 768px but three JS guards used 700px, so at
    700-767px the sidebar was laid out as a mobile overlay while close-on-
    outside-click AND the auto-close-when-a-tool-opens handler both bailed out
    -- tool panels opened *behind* the sidebar in that band."""
    assert "innerWidth >= 700" not in SIDEBAR_JS
    assert "innerWidth < 700" not in SIDEBAR_JS


def test_static_asset_version_is_consistent():
    """Server had the new CSS but browsers kept serving the cached copy because
    the ?v= cache-buster was never bumped -- the fixes were invisible to anyone
    who had loaded the page before. All asset refs must share one version so a
    single bump actually invalidates them."""
    import re
    versions = set(re.findall(r"\?v=(\d+)", INDEX_HTML))
    assert len(versions) == 1, f"mixed asset versions: {versions}"


def test_model_picker_menu_is_clamped_to_the_viewport():
    """The menu is position:absolute right:0 against a wrap sitting ~22px from
    the right edge, with max-width:360px. On a 375px screen that put its LEFT
    edge at -7px: the first row's model name ran off-screen and the provider
    logos were clipped, while ~22px sat unused on the right. Verified live
    before (left:-7) and after (left:+9) the clamp."""
    idx = CSS.index("Clamping to the viewport keeps the")
    block = CSS[idx:idx + 400]
    assert ".model-picker-menu" in block
    assert "max-width: calc(100vw - 32px)" in block


def test_touch_target_rule_is_not_defeated_by_the_plus_button_lock():
    """style.css already asks for 44x44 touch targets via
    `.section-header-btn { min-height: 44px }`, but the sidebar's "manage" and
    "compose" buttons carry .list-item-plus-btn too, which sets
    `height: 14px !important; min-height: 0 !important` — !important beat the
    un-important touch rule, so both rendered 14px tall. "manage" is the only
    way to reach per-chat actions on a phone, since .session-menu-btn is
    display:none under 768px."""
    # The lock that caused it still exists (this test is about overriding it).
    assert "height: 14px !important" in CSS
    assert "min-height: 0 !important" in CSS

    # The mobile override must be !important or it loses to that lock.
    idx = CSS.index("so the touch\n     rule was silently defeated")
    block = CSS[idx:idx + 700]
    assert ".list-item-plus-btn" in block
    assert "height: 32px !important" in block
    assert "min-height: 32px !important" in block

    # ...and it has to live inside the mobile breakpoint, not globally.
    mobile_start = CSS.rindex("@media (max-width: 768px) {", 0, idx)
    assert mobile_start < idx


def test_per_session_menu_button_is_hidden_on_mobile():
    """Documents the pairing the test above depends on: the per-row menu is
    deliberately hidden on phones, which is what makes "manage" load-bearing."""
    assert ".session-menu-btn" in CSS
    idx = CSS.index(".session-menu-btn")
    assert "display: none !important" in CSS[idx:idx + 3000]


MARKDOWN_JS = Path("static/js/markdown.js").read_text(encoding="utf-8")


def test_mermaid_is_not_loaded_eagerly():
    """mermaid.min.js is 3.5 MB. Measured on the live app it took 4546ms with
    transferSize 0 -- pure main-thread parse/compile of an already-cached file
    -- while DOMContentLoaded landed at 4693ms. It was effectively the entire
    startup cost, and a phone is far slower still. It must not be in the
    document head; markdown.js fetches it only when a diagram exists."""
    assert "mermaid.min.js" not in INDEX_HTML, "mermaid must not be an eager <script>"
    assert 'id="mermaid-script"' not in INDEX_HTML
    # No dangling onload hook left behind either.
    assert "odysseusInitMermaid()" not in INDEX_HTML


def test_render_mermaid_checks_for_work_before_loading():
    """The 3.5 MB fetch must sit BEHIND the "is there a diagram?" check —
    otherwise every session pays for it regardless."""
    fn = MARKDOWN_JS[MARKDOWN_JS.index("export function renderMermaid"):]
    fn = fn[:fn.index("\nconst markdownModule")]
    guard = fn.index("pre.mermaid:not([data-processed])")
    load = fn.index("loadMermaid()")
    assert guard < load, "must check for pending diagrams before loading mermaid"
    assert "length === 0) return" in fn[:load], "must early-return when there is nothing to draw"


def test_mermaid_loader_is_cached_and_retryable():
    """One in-flight fetch shared by concurrent callers; a failed load resets
    so a later diagram can retry rather than being permanently broken."""
    loader = MARKDOWN_JS[MARKDOWN_JS.index("function loadMermaid()"):]
    loader = loader[:loader.index("function initMermaid()")]
    assert "if (window.mermaid) return Promise.resolve" in loader
    assert "if (_mermaidLoader) return _mermaidLoader" in loader
    assert "_mermaidLoader = null;" in loader, "a failed load must be retryable"
