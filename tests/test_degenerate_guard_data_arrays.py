"""The degenerate-stream guard must abort true token/phrase loops but NOT a
legitimate repetitive data STRUCTURE — e.g. a CSS wallpaper array where every
row is `css: 'linear-gradient(135deg, …)'`. That false positive killed a valid
browser-OS generation mid-stream ("repeated phrase 'css linear gradient deg'").
"""

import src.agent_tools  # noqa: F401  (resolve circular init before importing)
from src.llm_core import _DegenerateStreamGuard


def _fed(guard, chunks):
    for c in chunks:
        if guard.check(c):
            return True
    return False


def test_gradient_wallpaper_array_is_not_flagged():
    g = _DegenerateStreamGuard("Qwen3.6-27B")
    names = ["Aurora", "Sunset", "Ocean", "Neon", "Forest", "Arctic",
             "Ember", "Cyber", "Cosmos", "Midnight", "Dawn", "Dusk",
             "Rose", "Jade", "Slate", "Coral"]
    hexes = ["0f0c29", "302b63", "fa709a", "fee140", "0f2027", "203a43",
             "0d0d0d", "1a0030", "134e5e", "71b280", "e6dada", "274046",
             "abc123", "def456", "789abc", "fedcba"]
    rows = [
        f"{{ name: '{names[i]}', css: 'linear-gradient(135deg,#{hexes[i]},#{hexes[(i+1) % 16]})' }},\n"
        for i in range(len(names))
    ]
    assert _fed(g, rows) is False


def test_true_single_phrase_loop_still_aborts():
    g = _DegenerateStreamGuard("Qwen3.6-27B")
    assert _fed(g, ["css linear gradient deg "] * 40) is True


def test_true_prose_phrase_loop_still_aborts():
    g = _DegenerateStreamGuard("m")
    assert _fed(g, ["also be a software developer mode "] * 40) is True


def test_single_token_runaway_still_aborts():
    g = _DegenerateStreamGuard("m")
    assert _fed(g, ["Var "] * 40) is True
