"""Esc during a streaming generation must ONLY stop the stream — never also
close/minimize the hovered or topmost modal (the Brain "flashed and closed by
itself" bug: kb.cancel aborts the request AND ui.js's Escape arbiter closed
the modal on the same keypress).

The arbiter guard lives in ui.js behind window.chatModule.isResponseStreaming;
chat.js must expose that probe on the module object. Full-DOM execution of
these modules isn't practical under node, so these tests pin the wiring at
source level — each assertion names the exact contract that must hold.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UI = (ROOT / "static/js/ui.js").read_text(encoding="utf-8")
CHAT = (ROOT / "static/js/chat.js").read_text(encoding="utf-8")


def test_chat_exports_isResponseStreaming():
    assert "export function isResponseStreaming()" in CHAT
    # It reflects both the streaming flag and an in-flight abort controller.
    body = CHAT.split("export function isResponseStreaming()", 1)[1][:200]
    assert "isStreaming" in body and "currentAbort" in body


def test_chat_module_object_exposes_probe():
    # ui.js reaches it via window.chatModule.isResponseStreaming — the export
    # alone isn't enough; it must be on the chatModule object.
    assert re.search(r"^\s*isResponseStreaming,\s*$", CHAT, re.M)


def test_escape_arbiter_yields_while_streaming():
    # The guard must run inside the Escape arbiter BEFORE _closeHoveredWindow.
    arbiter = UI.split("if (e.key !== 'Escape' || e.defaultPrevented) return;", 1)[1]
    guard_pos = arbiter.find("isResponseStreaming")
    close_pos = arbiter.find("_closeHoveredWindow()")
    assert guard_pos != -1, "streaming guard missing from Escape arbiter"
    assert close_pos != -1
    assert guard_pos < close_pos, "guard must run before the hovered-window close"


def test_space_minimize_blocked_while_typing_anywhere():
    # Companion fix: typing anywhere blocks the hover-Space minimize; the old
    # guard only blocked typing inside the hovered window, so a space typed in
    # the chat input minimized the hovered Brain.
    m = re.search(r"function _spaceIsBlocked\(e, surface\) \{(.*?)\n\}", UI, re.S)
    assert m, "_spaceIsBlocked not found"
    body = m.group(1)
    assert re.search(r"if \(_isTextEditingTarget\(target\)\) return true;", body), (
        "typing anywhere must block the hover-Space toggle"
    )


def test_space_minimize_requires_header_hover():
    # Space over a modal's scrollable BODY must scroll, not minimize — hijacking
    # it made the Brain "flash and close by itself" while reading/scrolling. The
    # minimize gesture is gated to the window header (title bar).
    m = re.search(r"function _pointerOverWindowChrome\(win\) \{(.*?)\n\}", UI, re.S)
    assert m, "_pointerOverWindowChrome helper missing"
    helper = m.group(1)
    assert "modal-header" in helper, "chrome check must key off the modal header"

    # And the Space keydown handler must gate minimize behind that check.
    kd = UI.split("if (e.code !== 'Space' || e.repeat) return;", 1)[1]
    guard_pos = kd.find("_pointerOverWindowChrome(hoveredToggleWindow)")
    min_pos = kd.find("Modals.minimize(id)")
    assert guard_pos != -1, "header-hover gate missing from Space handler"
    assert min_pos != -1
    assert guard_pos < min_pos, "gate must run before Modals.minimize"
