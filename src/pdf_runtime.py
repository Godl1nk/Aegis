"""Small helpers for PDF runtime dependencies."""

PDF_VIEWER_PYMUPDF_MISSING = (
    "PDF viewer requires PyMuPDF. Install dependencies with "
    "`pip install -r requirements.txt` (PyMuPDF is AGPL-3.0)."
)


def load_pymupdf_for_pdf_viewer():
    """Return the PyMuPDF module, or raise a user-facing setup hint."""
    try:
        import fitz  # PyMuPDF
    except ImportError as exc:
        raise RuntimeError(PDF_VIEWER_PYMUPDF_MISSING) from exc
    return fitz
