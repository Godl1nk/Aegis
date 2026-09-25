"""Auto-create a Document row from an Office attachment.

When a .docx (and friends) lands in chat, the full extracted text is stored
as a Document so the agent can page through it with `manage_documents
action=read offset=…` even after the inline chat payload was capped. Mirrors
the PDF auto-doc pattern in `src.pdf_form_doc`.
"""

import logging
import uuid
from typing import Optional

logger = logging.getLogger(__name__)


def create_office_document(
    session_id: str,
    upload_id: str,
    title: str,
    body_text: Optional[str] = None,
) -> Optional[str]:
    """Create a markdown Document for an Office attachment and set it active.

    Returns the new doc_id, or None on failure / empty body. The full
    extracted body lives in `current_content`, so the agent can fetch
    arbitrary windows via `manage_documents action=read` even when the
    inline chat copy was truncated.
    """
    from src.database import (
        SessionLocal,
        Document,
        DocumentVersion,
        Session as DbSession,
    )
    from src.agent_tools.document_tools import set_active_document

    if not body_text or not body_text.strip():
        return None

    db = SessionLocal()
    try:
        doc_id = str(uuid.uuid4())
        ver_id = str(uuid.uuid4())
        sess = db.query(DbSession).filter(DbSession.id == session_id).first()
        content = body_text
        language = "markdown"
        if upload_id.lower().endswith(".docx") and sess:
            # Chat DOCX attachments should retain their source package just
            # like Library imports. Other Office formats remain text copies.
            from src.constants import UPLOAD_DIR
            from src.upload_handler import UploadHandler
            from src.word_document import import_content
            import os

            handler = UploadHandler(os.path.dirname(UPLOAD_DIR), UPLOAD_DIR)
            source = handler.resolve_upload(upload_id, owner=sess.owner)
            if source:
                try:
                    with open(source["path"], "rb") as word_file:
                        content = import_content(word_file.read(), upload_id)
                    language = "docx"
                except Exception:
                    logger.warning("Could not retain DOCX source for %s; using extracted text", upload_id, exc_info=True)
        doc = Document(
            id=doc_id,
            session_id=session_id,
            title=title,
            language=language,
            current_content=content,
            version_count=1,
            is_active=True,
            owner=sess.owner if sess else None,
        )
        ver = DocumentVersion(
            id=ver_id,
            document_id=doc_id,
            version_number=1,
            content=content,
            summary="Imported from Office attachment",
            source="upload",
        )
        db.add(doc)
        db.add(ver)
        db.commit()
        set_active_document(doc_id)
        return doc_id
    except Exception as e:
        db.rollback()
        logger.error("Failed to create office document: %s", e)
        return None
    finally:
        db.close()
