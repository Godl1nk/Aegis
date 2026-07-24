"""Opt-in post-generation syntax check for code documents.

Catches SYNTAX errors (not runtime/logic) after the model writes a
python/js/html document, so it can self-correct via edit_document. Gated
behind agent_code_syntax_check (default off) and capped so an unfixable
error can't loop."""

from pathlib import Path

import src.agent_tools  # noqa: F401


def test_setting_registered_default_off():
    from src.settings import DEFAULT_SETTINGS
    assert DEFAULT_SETTINGS.get("agent_code_syntax_check") is False


def test_python_syntax_detection():
    from src.agent_loop import _check_code_syntax
    assert _check_code_syntax("python", "def f(:\n pass").startswith("Python syntax error")
    assert _check_code_syntax("python", "def f():\n    return 1") == ""
    assert _check_code_syntax("py", "x = [1, 2,") != ""


def test_js_and_html_syntax_detection():
    import shutil
    if not shutil.which("node"):
        return  # node-dependent; skipped where absent (mirrors checker's own guard)
    from src.agent_loop import _check_code_syntax
    assert _check_code_syntax("javascript", "function f() { return }}") != ""
    assert _check_code_syntax("javascript", "const x = () => 1;") == ""
    # ESM is checked as a module (no false positive on top-level import).
    assert _check_code_syntax("javascript", "import x from 'y';\nconst z = x + 1;") == ""
    # HTML: only inline <script> bodies are checked; a bad one is reported.
    assert "inline <script>" in _check_code_syntax("html", "<div></div><script>function(){</script>")
    assert _check_code_syntax("html", "<script src='cdn'></script><script>const a=1;</script>") == ""


def test_checker_never_raises_on_junk():
    from src.agent_loop import _check_code_syntax
    assert _check_code_syntax("python", "") == ""
    assert _check_code_syntax("", "whatever") == ""
    assert _check_code_syntax("css", "body { color: }") == ""  # unsupported lang -> skip


def test_loop_wiring_gated_and_capped():
    src = Path("src/agent_loop.py").read_text(encoding="utf-8")
    assert "_syntax_check_enabled = bool(get_setting(\"agent_code_syntax_check\"" in src
    assert "_syntax_check_count < 2" in src            # capped
    assert "_pending_syntax_doc = (result.get(\"language\"), result.get(\"content\"))" in src
    assert "Fix ONLY this syntax error using" in src   # steers to edit_document


def test_toggle_lives_in_builtin_tools_code_category():
    """The toggle belongs with the Code tools it governs (Settings → Tools →
    Built-in Tools → Code), not in the Agent card. It is a SETTING, so it must
    use data-setting-id — the tool save/counter selectors key off data-tool-id
    and would otherwise clobber it or miscount the category."""
    admin_js = Path("static/js/admin.js").read_text(encoding="utf-8")
    index_html = Path("static/index.html").read_text(encoding="utf-8")
    settings_js = Path("static/js/settings.js").read_text(encoding="utf-8")

    assert "if (cat === 'Code') {" in admin_js
    assert 'data-setting-id="agent_code_syntax_check"' in admin_js
    # Loads/saves via settings API, not /api/tools.
    assert "input[data-setting-id]" in admin_js
    assert "chk.dataset.settingId" in admin_js
    # Old Agent-card home fully removed (no duplicate/stale control).
    assert "set-agentCodeSyntaxCheck" not in index_html
    assert "syntaxInput" not in settings_js


def test_setting_row_counts_toward_its_category():
    """The Code category showed 5 rows but read '4/4' because the counter and
    master toggle only selected data-tool-id. A setting row is a visible option
    in the category, so it must be counted and follow the master toggle — while
    still saving through the settings API, not /api/tools."""
    admin_js = Path("static/js/admin.js").read_text(encoding="utf-8")

    # Counter includes setting rows.
    assert "input[data-tool-id], input[data-setting-id]" in admin_js
    # Master toggle flips setting rows too, persisting each via the settings API.
    assert 'catEl.querySelectorAll(\'input[data-setting-id]\')' in admin_js
    assert "_saveSettingRow(s)" in admin_js
    # Header is refreshed once real setting state is known (initial HTML is
    # rendered from tool counts before the settings fetch resolves).
    assert "_updateCatCounter(catEl))" in admin_js
    # Tool saves still exclude settings (settings are not tools).
    assert "const allChecks = list.querySelectorAll('input[data-tool-id]');" in admin_js
