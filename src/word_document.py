"""Lossless-source Word imports with deliberately limited, safe text edits."""

from __future__ import annotations

import io
import re

from docx import Document as WordDocument
from docx.text.paragraph import Paragraph

from src.upload_handler import is_valid_upload_id


_SOURCE = re.compile(r'^<!-- word_source upload_id="([^"]+)" -->\n')


class UnsafeWordEdit(ValueError):
    """The requested change cannot preserve the imported Word layout."""


def source_upload_id(content: str) -> str | None:
    match = _SOURCE.match(content or "")
    if not match or not is_valid_upload_id(match.group(1)):
        return None
    return match.group(1)


def _paragraphs(document):
    return [Paragraph(node, document) for node in document.element.body.xpath('.//w:p')]


def import_content(data: bytes, upload_id: str) -> str:
    if not is_valid_upload_id(upload_id):
        raise ValueError("Invalid Word source ID")
    document = WordDocument(io.BytesIO(data))
    paragraphs = _paragraphs(document)
    return f'<!-- word_source upload_id="{upload_id}" -->\n' + '\n'.join(p.text for p in paragraphs)


def render_edited_word(source: bytes, original_content: str, edited_content: str) -> bytes:
    """Change only text within an existing run; preserve the OOXML package.

    Changes to paragraph structure, fields, or text spanning styled runs are
    rejected instead of silently replacing formatting or document objects.
    """
    source_id = source_upload_id(original_content)
    if not source_id or source_upload_id(edited_content) != source_id:
        raise UnsafeWordEdit("The Word source reference must stay intact")
    if edited_content == original_content:
        return source
    document = WordDocument(io.BytesIO(source))
    paragraphs = _paragraphs(document)
    original = original_content.split('\n', 1)[1]
    edited = edited_content.split('\n', 1)[1]
    original_lines = original.split('\n')
    edited_lines = edited.split('\n')
    if len(original_lines) != len(paragraphs) or len(edited_lines) != len(paragraphs):
        raise UnsafeWordEdit("Adding or removing Word paragraphs could change the layout")
    if [p.text for p in paragraphs] != original_lines:
        raise UnsafeWordEdit("The Word source no longer matches the imported text")
    for paragraph, before, after in zip(paragraphs, original_lines, edited_lines):
        if before == after:
            continue
        start = 0
        while start < min(len(before), len(after)) and before[start] == after[start]:
            start += 1
        suffix = 0
        while (suffix < len(before) - start and suffix < len(after) - start
               and before[-suffix - 1] == after[-suffix - 1]):
            suffix += 1
        end = len(before) - suffix
        replacement_end = len(after) - suffix
        runs = paragraph.runs
        offset = 0
        matching_run = None
        matching_start = 0
        for run in runs:
            run_end = offset + len(run.text)
            if offset <= start and end <= run_end and (start < run_end or run is runs[-1]):
                matching_run, matching_start = run, offset
                break
            offset = run_end
        if matching_run is None or matching_run._element.xpath('.//w:br | .//w:tab | .//w:drawing | .//w:fldChar'):
            raise UnsafeWordEdit("The edit crosses formatted text or a Word object")
        local_start, local_end = start - matching_start, end - matching_start
        matching_run.text = matching_run.text[:local_start] + after[start:replacement_end] + matching_run.text[local_end:]
    result = io.BytesIO()
    document.save(result)
    return result.getvalue()


def render_document_edit(db, doc, edited_content: str, upload_handler, owner: str | None, auth_manager=None) -> bytes:
    """Validate an edit against the untouched first version and owned upload."""
    from core.database import DocumentVersion

    original = db.query(DocumentVersion).filter(
        DocumentVersion.document_id == doc.id,
        DocumentVersion.version_number == 1,
    ).first()
    if not original:
        raise UnsafeWordEdit("The original Word version is unavailable")
    upload_id = source_upload_id(original.content)
    if not upload_id or source_upload_id(edited_content) != upload_id:
        raise UnsafeWordEdit("The Word source reference must stay intact")
    resolved = upload_handler.resolve_upload(upload_id, owner=owner, auth_manager=auth_manager)
    if not resolved:
        raise UnsafeWordEdit("The original Word file is unavailable")
    with open(resolved['path'], 'rb') as file:
        return render_edited_word(file.read(), original.content, edited_content)
