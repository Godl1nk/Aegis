import asyncio

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.agent_tools.document_tools import (
    _is_pdf_annotation_only_edit, _pdf_form_source_upload_id,
    _strip_pdf_editor_markers, _validate_pdf_form_edit,
)


def test_pdf_ai_edit_derivative_strips_pdf_plumbing():
    raw = """<!-- pdf_source upload_id="0123456789abcdef0123456789abcdef.pdf" -->

# Contract

- **Name:** Felix <!-- field=Name type=text -->
- Hello world <!-- annotation id=a1 page=1 x=1 y=2 w=3 h=4 kind=text -->
"""

    assert _strip_pdf_editor_markers(raw) == "# Contract\n\n- **Name:** Felix\n- Hello world"


def test_pdf_ai_edit_derivative_strips_form_source_marker():
    raw = """<!-- pdf_form_source upload_id="0123456789abcdef0123456789abcdef.pdf" fields="12" -->

# Form
"""

    assert _strip_pdf_editor_markers(raw) == "# Form"


def test_pdf_form_edit_keeps_source_and_field_anchors():
    raw = ('<!-- pdf_form_source upload_id="0123456789abcdef0123456789abcdef.pdf" fields="1" -->\n'
           '# Form\n- **Name:** _(empty)_ <!-- field=Name type=text -->\n')
    changed = raw.replace('_(empty)_', 'Felix')
    assert _pdf_form_source_upload_id(raw) == '0123456789abcdef0123456789abcdef.pdf'
    assert _validate_pdf_form_edit(raw, changed) is None
    assert _validate_pdf_form_edit(raw, changed.replace('field=Name', 'field=Other'))
    assert _validate_pdf_form_edit(raw, changed.replace('pdf_form_source', 'pdf_source'))


def test_existing_pdf_annotation_text_can_change_without_detaching_pdf():
    raw = ('<!-- pdf_source upload_id="0123456789abcdef0123456789abcdef.pdf" -->\n'
           '# PDF\n- Old <!-- annotation id=a1 page=1 x=1 y=2 w=3 h=4 kind=text -->\n')
    assert _is_pdf_annotation_only_edit(raw, raw.replace('Old', 'New'))
    assert not _is_pdf_annotation_only_edit(raw, raw.replace('x=1', 'x=9'))
    assert not _is_pdf_annotation_only_edit(raw, raw.replace('# PDF', '# Changed'))


def test_pdf_form_ai_update_stays_pdf_backed(monkeypatch):
    from core.database import Base, Document, DocumentVersion
    from src import database as database_alias
    from src.agent_tools.document_tools import UpdateDocumentTool

    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(database_alias, 'SessionLocal', sessions)
    monkeypatch.setattr(database_alias, 'Document', Document, raising=False)
    monkeypatch.setattr(database_alias, 'DocumentVersion', DocumentVersion, raising=False)
    original = ('<!-- pdf_form_source upload_id="0123456789abcdef0123456789abcdef.pdf" fields="1" -->\n'
                '# Form\n- **Name:** _(empty)_ <!-- field=Name type=text -->\n')
    with sessions() as db:
        db.add(Document(id='form', title='Form', language='markdown', current_content=original,
                        owner='alice', version_count=1, is_active=True))
        db.commit()
    result = asyncio.run(UpdateDocumentTool().execute(
        original.replace('_(empty)_', 'Felix'), {'doc_id': 'form', 'owner': 'alice'}
    ))
    assert result['action'] == 'update'
    assert result['doc_id'] == 'form'
    with sessions() as db:
        doc = db.get(Document, 'form')
        assert 'pdf_form_source' in doc.current_content
        assert 'Felix' in doc.current_content
