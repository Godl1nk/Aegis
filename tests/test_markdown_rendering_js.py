"""Regression coverage for the browser markdown renderer."""

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_HAS_NODE = shutil.which("node") is not None


@pytest.fixture(scope="module")
def node_available():
    if not _HAS_NODE:
        pytest.skip("node binary not on PATH")


def _run_markdown_case(markdown: str, render_expr: str = "mod.mdToHtml(input)", katex_expr: str = "null"):
    script = textwrap.dedent(
        r"""
        import fs from 'node:fs';

        globalThis.window = { location: { origin: 'http://localhost' }, katex: __KATEX_EXPR__ };
        globalThis.katex = globalThis.window.katex;
        globalThis.document = {
          readyState: 'loading',
          addEventListener() {},
          createElement(tag) {
            if (tag !== 'template') throw new Error(`unsupported element: ${tag}`);
            return {
              _html: '',
              content: { querySelectorAll() { return []; } },
              set innerHTML(value) { this._html = value; },
              get innerHTML() { return this._html; },
            };
          },
        };
        globalThis.MutationObserver = class { observe() {} };

        let source = fs.readFileSync('./static/js/markdown.js', 'utf8');
        source = source.replace(
          /import uiModule from ['"]\.\/ui\.js['"];/,
          ''
        );
        source = source.replace(
          /import \{ splitTableRow \} from ['"]\.\/markdown\/tableRow\.js['"];/,
          `function splitTableRow(row) {
            return (row || '').replace(/^\\s*\\|/, '').replace(/\\|\\s*$/, '').split('|').map(c => c.trim());
          }`
        );
        // markdown.js imports the emoji-shortcode helpers relatively (issue #345),
        // which a data: URL module can't resolve. Inline the REAL helpers (minus
        // their export keywords) so the renderer's shortcode pass behaves exactly
        // as it does in the browser.
        const emojiSource = fs.readFileSync('./static/js/emojiShortcodes.js', 'utf8')
          .replace(/^export default .*$/m, '')
          .replace(/export const /g, 'const ')
          .replace(/export function /g, 'function ');
        source = source.replace(
          /import \{ replaceEmojiShortcodes, hasEmojiShortcode \} from ['"]\.\/emojiShortcodes\.js['"];/,
          () => emojiSource
        );
        source = source.replace(
          /var escapeHtml = uiModule\.esc;/,
          `var escapeHtml = (value) => String(value ?? '')
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');`
        );

        const moduleUrl = 'data:text/javascript;base64,' + Buffer.from(source).toString('base64');
        const mod = await import(moduleUrl);
        const input = JSON.parse(process.argv[1]);
        console.log(JSON.stringify({ html: __RENDER_EXPR__ }));
        """
    ).replace("__RENDER_EXPR__", render_expr).replace("__KATEX_EXPR__", katex_expr)
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, json.dumps(markdown)],
        cwd=_REPO,
        capture_output=True,
        timeout=15,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        raise AssertionError(f"node failed:\nSTDERR:\n{result.stderr}\nSTDOUT:\n{result.stdout}")
    return json.loads(result.stdout.splitlines()[-1])["html"]


def test_ordered_lists_render_as_one_unwrapped_ol(node_available):
    html = _run_markdown_case(
        "Before\n\n"
        "1. **Check against the home page** — that's the visual reference for how things should feel.\n"
        "2. **Open DevTools** and inspect the element — check fonts, colors, and spacing against this guide.\n"
        "3. **Flag it** — note the page, the section, what's wrong, and what CSS rule you suspect.\n"
        "4. **Small fixes** — if you know the fix (e.g. wrong CSS variable, wrong font), go ahead and change it in the CSS Module file.\n"
        "5. **Big changes** — Talk it through before making wide changes across many pages.\n\n"
        "After"
    )

    assert html.count("<ol>") == 1
    assert html.count("</ol>") == 1
    assert html.count("<li>") == 5
    assert "<ul>" not in html
    assert "<oli>" not in html
    assert "<uli>" not in html
    assert "<p><ol>" not in html
    assert "<p><li>" not in html
    assert "<p>Before</p>" in html
    assert "<p>After</p>" in html


def test_table_separator_row_not_rendered_as_data(node_available):
    html = _run_markdown_case("| A | B |\n|---|---|\n| 1 | 2 |")

    assert html.count("<tr>") == 2
    assert "<th" in html
    assert "<td" in html
    assert "---" not in html


_LLAMA_SWAP_BANNER = (
    "————————\n"
    "llama-swap loading model: GRM2.6-27B\n\n"
    "Compressing optimism into FP16 ........\n\n"
    "Done! (7.26s)\n\n"
    "————————\n\n"
)


def test_llama_swap_banner_does_not_leak_untagged_reasoning_into_body(node_available):
    """llama-swap prepends a model-load banner as ORDINARY content. It made the
    text start with the banner instead of a reasoning phrase, so untagged
    reasoning failed detection and rendered as the visible reply (user saw the
    loading lines + 'The user wants…' in chat). Banner and reasoning must both
    end up inside the collapsed thinking section."""
    html = _run_markdown_case(
        _LLAMA_SWAP_BANNER
        + "The user wants a browser-based OS in a single HTML file.\n\n"
        + "NovaOS.html has been saved and is ready to open in Chrome.",
        "mod.processWithThinking(input)",
    )

    assert "thinking-section" in html
    # Reasoning + banner are collapsed, not part of the visible reply.
    think_start = html.index("thinking-section")
    think_end = html.index("NovaOS.html has been saved")
    assert "The user wants a browser-based OS" in html[think_start:think_end]
    assert "llama-swap loading model" in html[think_start:think_end]
    # The actual reply still renders.
    assert "NovaOS.html has been saved" in html


def test_llama_swap_banner_collapsed_even_without_reasoning(node_available):
    """Banner is infrastructure noise — it never belongs in the visible reply,
    even when the model replies directly with no reasoning."""
    html = _run_markdown_case(
        _LLAMA_SWAP_BANNER + "Here is your answer.",
        "mod.processWithThinking(input)",
    )
    assert "thinking-section" in html
    assert "Here is your answer." in html
    assert html.index("llama-swap loading model") < html.index("Here is your answer.")


def test_ordinary_text_mentioning_llama_swap_is_not_stripped(node_available):
    """Only the real banner (loading model: … Done! (Ns)) is folded away —
    prose that merely mentions the words must render normally."""
    html = _run_markdown_case(
        "Here is how llama-swap works: it swaps models on demand.",
        "mod.processWithThinking(input)",
    )
    assert "thinking-section" not in html
    assert "it swaps models on demand" in html


def test_process_with_thinking_handles_gemma4_thought_channel(node_available):
    html = _run_markdown_case(
        "<|channel>thought\ninternal reasoning<channel|>Final answer.",
        "mod.processWithThinking(input)",
    )

    assert "thinking-section" in html
    assert "internal reasoning" in html
    assert "Final answer." in html
    assert "&lt;|channel&gt;" not in html
    assert "<|channel>" not in html


def test_process_with_thinking_strips_empty_gemma4_thought_channel(node_available):
    html = _run_markdown_case(
        "<|channel>thought\n<channel|>Final answer.",
        "mod.processWithThinking(input)",
    )

    assert "thinking-section" not in html
    assert "Final answer." in html
    assert "&lt;|channel&gt;" not in html
    assert "<|channel>" not in html


def test_process_with_thinking_unwraps_gemma4_response_channel(node_available):
    html = _run_markdown_case(
        "<|channel>thought\ninternal reasoning<channel|><|channel>response\nFinal answer.<channel|>",
        "mod.processWithThinking(input)",
    )

    assert "thinking-section" in html
    assert "internal reasoning" in html
    assert "Final answer." in html
    assert "&lt;|channel&gt;" not in html
    assert "<|channel>" not in html


def test_extract_thinking_blocks_handles_thought_tag(node_available):
    result = _run_markdown_case(
        "<thought>internal reasoning</thought>Final answer.",
        "mod.extractThinkingBlocks(input)",
    )

    assert result["thinkingBlocks"] == ["internal reasoning"]
    assert result["content"] == "Final answer."


def test_url_inside_inline_code_is_not_autolinked(node_available):
    # A URL inside a backtick span is preceded by a space, so the bare-URL
    # autolink used to wrap it in an <a> tag (then swap it for an
    # ___ALLOWED_HTML_ placeholder), corrupting the command shown to the user.
    html = _run_markdown_case("Run `$j = irm http://127.0.0.1:3000/x` to fetch.")

    assert "<code>$j = irm http://127.0.0.1:3000/x</code>" in html
    assert "___ALLOWED_HTML_" not in html
    assert "<a " not in html
    assert 'href="http://127.0.0.1:3000/x"' not in html


def test_url_outside_inline_code_is_still_autolinked(node_available):
    # Inline code must not disable autolinking for bare URLs elsewhere in the
    # same line.
    html = _run_markdown_case("Use `irm` then visit https://example.com/page now.")

    assert "<code>irm</code>" in html
    assert 'href="https://example.com/page"' in html


def test_inline_code_content_is_html_escaped(node_available):
    # Inline code is now extracted before the global escape pass, so it must be
    # escaped at extraction time (matching the fenced-code-block handling).
    html = _run_markdown_case("Render `<b>$1 & 'q'</b>` literally.")

    assert "<code>&lt;b&gt;$1 &amp; &#39;q&#39;&lt;/b&gt;</code>" in html
    assert "<b>" not in html


def test_dotted_python_import_paths_are_not_autolinked(node_available):
    html = _run_markdown_case(
        "from imblearn.combine import SMOTETomek\n"
        "from sklearn.metrics import f1_score\n"
        "from sklearn.compose import ColumnTransformer\n\n"
        "See example.com/docs for normal domain autolinking."
    )

    assert "___ALLOWED_HTML_" not in html
    assert "imblearn.combine" in html
    assert "sklearn.metrics" in html
    assert "sklearn.compose" in html
    assert 'href="https://imblearn.com' not in html
    assert 'href="https://sklearn.me' not in html
    assert 'href="https://example.com/docs"' in html


def test_bare_cdot_commands_render_without_math_delimiters(node_available):
    html = _run_markdown_case(
        "c_p = 468 J/kg\\cdotp\u00b0C, k = 13.4 W/m\\cdot\u00b0C and `\\cdot` stays code",
        katex_expr="{ renderToString() { return '<span class=\"katex-error\">\\\\cdotp</span>'; } }",
    )

    assert "kg&centerdot;\u00b0C" in html
    assert "m&centerdot;\u00b0C" in html
    assert "katex-error" not in html
    assert "<code>\\cdot</code>" in html


def test_cdotp_inside_math_is_normalized_before_katex(node_available):
    html = _run_markdown_case(
        "$c_p = 468 J/kg\\cdotp{}^\\circ C$",
        katex_expr="{ renderToString(math, options) { if (options?.macros?.['\\\\cdotp'] !== '\\\\cdot') throw new Error('missing cdotp macro'); return '<span class=\"katex\">' + math.replace('\\\\cdotp', options.macros['\\\\cdotp']) + '</span>'; } }",
    )

    assert "\\cdot" in html
    assert "\\cdotp" not in html


def test_katex_error_span_for_cdot_is_repaired(node_available):
    html = _run_markdown_case(
        "$J/kg\\cdot{}^\\circ C$",
        katex_expr="{ renderToString() { return '<span class=\"katex-error\" style=\"color:#cc0000\" title=\"x\">\\\\cdot</span>'; } }",
    )

    assert "&centerdot;" in html
    assert "katex-error" not in html


def test_two_currency_dollar_signs_on_one_line_do_not_pair_as_math(node_available):
    """Two unrelated dollar amounts on one line ('~$96.35 ... ($110...') were
    being paired as a $...$ math span, swallowing the whole sentence between
    them (markdown syntax and all) into KaTeX — which ignores markdown and
    collapses whitespace, rendering the prose as squished italic math text.
    Regression for the exact financial-assistant report seen in production."""
    html = _run_markdown_case(
        "The stock is at ~$96.35 *as of July 13 — recovering from the May "
        "crash* ($110 before the drop), so sentiment remains sensitive.",
        katex_expr="{ renderToString() { throw new Error('must not be called'); } }",
    )

    assert "MATH_BLOCK" not in html
    assert "$96.35" in html
    assert "$110" in html
    assert "<em>as of July" in html


def test_short_legit_inline_math_still_renders(node_available):
    """The prose guard must not swallow real short inline math on the same
    line as other $ pairs."""
    html = _run_markdown_case(
        "We know $a + b = 10$ and $a - b = 4$.",
        katex_expr="{ renderToString(math) { return '<span class=\"katex\">' + math + '</span>'; } }",
    )

    assert html.count('class="katex"') == 2
    assert "a + b = 10" in html
    assert "a - b = 4" in html
    assert "\\cdot" not in html


def test_dom_cdot_repair_handles_cached_text_nodes(node_available):
    result = _run_markdown_case(
        "",
        """(() => {
          const node = {
            nodeType: 3,
            nodeValue: 'J/kg\\\\cdot C and W/m\\\\cdotp C',
            parentElement: { closest() { return false; } },
          };
          document.createTreeWalker = () => ({
            currentNode: null,
            _done: false,
            nextNode() {
              if (this._done) return false;
              this._done = true;
              this.currentNode = node;
              return true;
            },
          });
          mod.repairCdotCommandsInDom({ nodeType: 1, querySelectorAll() { return []; } });
          return node.nodeValue;
        })()""",
    )

    assert result == "J/kg\u00b7 C and W/m\u00b7 C"


def test_dom_cdot_repair_replaces_cached_katex_error_span(node_available):
    result = _run_markdown_case(
        "",
        """(() => {
          const replaced = [];
          const parent = {
            classList: { contains(name) { return name === 'katex-error'; } },
            closest() { return null; },
            replaceWith(node) { replaced.push(node.nodeValue); },
          };
          const node = {
            nodeType: 3,
            nodeValue: '\\\\cdot',
            parentElement: parent,
          };
          document.createTextNode = (value) => ({ nodeType: 3, nodeValue: value });
          document.createTreeWalker = () => ({
            currentNode: null,
            _done: false,
            nextNode() {
              if (this._done) return false;
              this._done = true;
              this.currentNode = node;
              return true;
            },
          });
          mod.repairCdotCommandsInDom({ nodeType: 1, querySelectorAll() { return []; } });
          return replaced[0];
        })()""",
    )

    assert result == "\u00b7"


def test_blank_line_separated_ordered_items_keep_counting_up(node_available):
    """A "loose" list (blank lines between items) renders as one <ol> per item.
    That is fine structurally, but every list restarted at 1, so the UI showed
    "1. 1. 1." — reproduced live as 5 sibling <ol>s with a single <li> each.
    Each list must carry the number the author actually wrote.

    The lists are deliberately NOT merged into one <ol>: merging makes a
    streaming cut's safety depend on text that has not arrived yet, which
    breaks the segmenter freeze invariant in tests/streaming/invariant.test.mjs.
    A `start` attribute is derived locally, so it stays stream-safe.
    """
    import re as _re

    html = _run_markdown_case(
        "\n".join([
            "1. **First** item here.",
            "",
            "2. **Second** item here.",
            "",
            "3. **Third** item here.",
        ])
    )
    starts = [m or "1" for m in _re.findall(r'<ol(?: start="(\d+)")?>', html)]
    assert starts == ["1", "2", "3"], f"markers would render as {starts}: {html}"


def test_interrupted_ordered_list_resumes_at_the_authored_number(node_available):
    """Indented sub-bullets split the list; items after the interruption must
    continue the count rather than restarting at 1."""
    import re as _re

    html = _run_markdown_case(
        "\n".join([
            "1. First.",
            "",
            "2. Second.",
            "",
            "3. Smart move: set orders in tranches:",
            "   - $95-100 zone: start small",
            "   - $85-90 zone: add more",
            "",
            "4. Fourth.",
            "",
            "5. Fifth.",
        ])
    )
    # The indented bullets become a real <ul>, not literal "- " paragraph text.
    assert html.count("<ul>") == 1
    assert not _re.search(r"<p>\s*-\s", html), f"literal dash left: {html}"
    starts = [m or "1" for m in _re.findall(r'<ol(?: start="(\d+)")?>', html)]
    assert starts == ["1", "2", "3", "4", "5"], f"got {starts}: {html}"


def test_indented_bullets_render_as_bullets(node_available):
    """Up to 3 leading spaces is still a list marker per CommonMark."""
    html = _run_markdown_case("Intro:\n\n   - alpha\n   - beta")
    assert "<ul>" in html
    assert "<li>alpha</li>" in html
    assert "<li>beta</li>" in html


def test_ordered_list_starting_above_one_keeps_its_number(node_available):
    html = _run_markdown_case("4. four\n5. five")
    assert '<ol start="4">' in html
    assert html.count("<li>") == 2


# KaTeX stub that tags whatever it is asked to render, so a test can tell
# whether a $...$ span was treated as math or left as literal currency text.
_KATEX_TAGGING_STUB = "{ renderToString: (s) => '<<MATH:' + s + '>>' }"


def _math_spans(markdown):
    html = _run_markdown_case(markdown, katex_expr=_KATEX_TAGGING_STUB)
    return [seg.split(">>", 1)[0] for seg in html.split("<<MATH:")[1:]]


def test_currency_pair_is_not_swallowed_as_inline_math(node_available):
    """Two dollar amounts on one line were paired as a $...$ span, feeding the
    prose between them to KaTeX — it ignores markdown and collapses spaces, so
    "$110.18** (already crossed $110)" rendered as run-together italic math.
    Reproduced live from a real reply. The 3-word prose guard missed it
    ("already crossed" is only 2 words); the markdown ** is the tell."""
    assert _math_spans("**Today's high:** $110.18** OK (already crossed $110)") == []


def test_two_bare_currency_amounts_stay_literal(node_available):
    """"$95 to $110" paired "95 to" into KaTeX. Currency after a number is a
    word; math after a number is an operator."""
    assert _math_spans("Range $95 to $110 today.") == []
    assert _math_spans("It hit $110 and $106 today.") == []


def test_real_inline_math_still_renders(node_available):
    """The currency guards must not swallow genuine math — including arithmetic
    that opens with a digit and tight algebra like $3x$."""
    assert _math_spans("The identity $2 + 2 = 4$ holds.") == ["2 + 2 = 4"]
    assert _math_spans("Let $x + y$ be the sum.") == ["x + y"]
    assert _math_spans("Solve $3x$ for x.") == ["3x"]
    assert _math_spans("Energy $E = mc^2$ is famous.") == ["E = mc^2"]


# ---------------------------------------------------------------------------
# <br> inside table cells
# ---------------------------------------------------------------------------
# GFM tables cannot contain block-level markdown, so <br> is the only way to
# break a line inside a cell — and models use it constantly for multi-item
# cells. The renderer escaped it, printing a literal "<br>" through the middle
# of the text. Reproduced live from a real reply (machining defects table).

_BR_TABLE = (
    "| Defect | Fix |\n"
    "| --- | --- |\n"
    "| Chatter | *If chatter marks:*<br>\u2022 Reduce **cutting speed**"
    "<br>\u2022 Increase **rigidity** |"
)


def test_br_in_table_cell_becomes_a_real_line_break(node_available):
    html = _run_markdown_case(_BR_TABLE)
    assert "&lt;br&gt;" not in html, "the literal tag leaked into the output"
    assert html.count("<br>") == 2


def test_the_surrounding_table_still_renders(node_available):
    """The first fix routed <br> through the ___ALLOWED_HTML_ placeholder, but
    the table converter bails on any block containing that prefix — so the
    table stopped rendering entirely, which is worse than the literal tag."""
    html = _run_markdown_case(_BR_TABLE)
    assert "<table" in html
    assert "<td" in html


def test_self_closing_br_forms_are_handled(node_available):
    html = _run_markdown_case("| a | b |\n| --- | --- |\n| x | one<br/>two<br />three |")
    assert "&lt;br" not in html
    assert html.count("<br>") == 2


def test_br_outside_a_table_still_breaks(node_available):
    assert "<br>" in _run_markdown_case("line one<br>line two")


def test_a_br_with_attributes_is_still_escaped(node_available):
    """Only the bare tag is allowed through. Anything carrying attributes is
    not a line break — it is an injection attempt and stays escaped."""
    html = _run_markdown_case("text<br onload=alert(1)>more")
    assert "&lt;br onload" in html
    assert "<br onload" not in html


def test_script_tags_are_still_escaped_alongside_br(node_available):
    html = _run_markdown_case("a<br><script>alert(1)</script>")
    assert html.count("<br>") == 1
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
