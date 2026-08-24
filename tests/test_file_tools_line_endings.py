"""Agent file edits must not reflow a file's line endings.

Python text-mode writes translate "\\n" to os.linesep, so on Windows every
write_file/edit_file silently converted an LF file to CRLF. A one-line edit
rewrote EVERY line: the real change vanished inside a whole-file diff, and for
shell scripts it reintroduced the CRLF shebang that breaks Docker with
"exec entrypoint.sh: no such file or directory" — the exact failure
.gitattributes was added to prevent (issues #150, #77).

1347 files in this repo are stored LF, so the blast radius was most of the tree.
"""
import asyncio
import json

import pytest

from src.agent_tools.filesystem_tools import EditFileTool, WriteFileTool


@pytest.fixture(autouse=True)
def _no_confinement(monkeypatch):
    """These tests are about byte-level output, not path policy."""
    import src.tool_execution as te
    monkeypatch.setattr(te, "_resolve_tool_path", lambda p: p)


def _edit(path, old, new):
    return asyncio.run(EditFileTool().execute(
        json.dumps({"path": str(path), "old_string": old, "new_string": new}), {}))


def _write(path, content):
    return asyncio.run(WriteFileTool().execute(
        json.dumps({"path": str(path), "content": content}), {}))


def _counts(path):
    raw = path.read_bytes()
    crlf = raw.count(b"\r\n")
    return crlf, raw.count(b"\n") - crlf


def test_editing_an_lf_file_keeps_lf(tmp_path):
    f = tmp_path / "mod.py"
    f.write_bytes(b"line one\nline two\nline three\n")

    _edit(f, "line two", "line 2")

    assert _counts(f) == (0, 3)
    assert f.read_bytes() == b"line one\nline 2\nline three\n"


def test_editing_a_crlf_file_keeps_crlf(tmp_path):
    """The mirror case: a genuinely CRLF file must not be flattened to LF."""
    f = tmp_path / "mod.py"
    f.write_bytes(b"line one\r\nline two\r\nline three\r\n")

    _edit(f, "line two", "line 2")

    assert _counts(f) == (3, 0)


def test_a_one_line_edit_changes_one_line(tmp_path):
    """The reviewability property: the diff must be the edit, not the file."""
    f = tmp_path / "mod.py"
    before = b"alpha\nbeta\ngamma\ndelta\n"
    f.write_bytes(before)

    _edit(f, "beta", "BETA")

    after = f.read_bytes()
    changed = sum(1 for a, b in zip(before.split(b"\n"), after.split(b"\n")) if a != b)
    assert changed == 1


def test_shell_script_shebang_survives_an_edit(tmp_path):
    """A CRLF shebang makes the kernel look for an interpreter named
    "/bin/sh\\r" — the documented Docker startup failure."""
    f = tmp_path / "entrypoint.sh"
    f.write_bytes(b"#!/bin/sh\necho hello\n")

    _edit(f, "echo hello", "echo goodbye")

    assert f.read_bytes().split(b"\n")[0] == b"#!/bin/sh"
    assert b"\r" not in f.read_bytes()


def test_overwriting_preserves_the_existing_convention(tmp_path):
    crlf = tmp_path / "crlf.py"
    crlf.write_bytes(b"a\r\nb\r\n")
    _write(crlf, "x\ny\n")
    assert _counts(crlf) == (2, 0)

    lf = tmp_path / "lf.py"
    lf.write_bytes(b"a\nb\n")
    _write(lf, "x\ny\n")
    assert _counts(lf) == (0, 2)


def test_crlf_input_is_not_doubled(tmp_path):
    f = tmp_path / "crlf.py"
    f.write_bytes(b"a\r\nb\r\n")
    _write(f, "x\r\ny\r\n")
    assert f.read_bytes() == b"x\r\ny\r\n"


def test_a_new_file_gets_lf(tmp_path):
    """No existing convention to follow — LF is what .gitattributes normalises
    to and what shell scripts require."""
    f = tmp_path / "brand_new.py"
    _write(f, "fresh\nfile\n")
    assert _counts(f) == (0, 2)


def test_replace_all_still_preserves_endings(tmp_path):
    f = tmp_path / "many.py"
    f.write_bytes(b"x\nx\nx\n")

    asyncio.run(EditFileTool().execute(json.dumps({
        "path": str(f), "old_string": "x", "new_string": "y", "replace_all": True}), {}))

    assert _counts(f) == (0, 3)
    assert f.read_bytes() == b"y\ny\ny\n"


def test_content_without_a_trailing_newline_is_untouched(tmp_path):
    f = tmp_path / "no_trailing.py"
    f.write_bytes(b"only line")
    _edit(f, "only", "ONLY")
    assert f.read_bytes() == b"ONLY line"
