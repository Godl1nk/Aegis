"""Render an imported Word document for viewing without changing its source."""

import shutil
import subprocess
import tempfile
from pathlib import Path


class WordPreviewUnavailable(RuntimeError):
    pass


def render_word_pdf(word_bytes: bytes) -> bytes:
    executable = shutil.which("libreoffice") or shutil.which("soffice")
    if not executable:
        raise WordPreviewUnavailable("Word preview requires LibreOffice on the server")

    with tempfile.TemporaryDirectory(prefix="aegis-word-preview-") as directory:
        root = Path(directory)
        source = root / "document.docx"
        output = root / "document.pdf"
        source.write_bytes(word_bytes)
        try:
            result = subprocess.run(
                [executable, f"-env:UserInstallation={(root / 'profile').as_uri()}",
                 "--headless", "--convert-to", "pdf:writer_pdf_Export",
                 "--outdir", str(root), str(source)],
                capture_output=True, text=True, timeout=90, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise WordPreviewUnavailable("Word preview timed out") from exc
        if result.returncode != 0 or not output.is_file():
            raise WordPreviewUnavailable("Could not render the Word document")
        data = output.read_bytes()
        if not data.startswith(b"%PDF-"):
            raise WordPreviewUnavailable("Word preview did not produce a valid PDF")
        return data
