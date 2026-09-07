"""Extract raw text from an invoice PDF using ``pdfplumber``.

Only text-layer PDFs are supported. Scanned/image-only PDFs contain no
extractable text and raise :class:`PdfExtractionError` — OCR is out of
scope for the MVP (the doc lists it as a known limitation).
"""
from __future__ import annotations

from pathlib import Path


class PdfExtractionError(Exception):
    """Raised when a PDF exists but yields no usable text."""


def extract_text(path: str | Path) -> str:
    """Return the per-page text of *path*, one page per block.

    Raises:
        FileNotFoundError: if *path* does not exist.
        PdfExtractionError: if the PDF has no text layer (e.g. scanned).
    """
    pdf_path = Path(path)
    if not pdf_path.exists():
        raise FileNotFoundError(f"File not found: {pdf_path}")

    import pdfplumber  # imported lazily so `--help` works before install

    pages: list[str] = []
    with pdfplumber.open(pdf_path) as pdf:
        for index, page in enumerate(pdf.pages, start=1):
            text = (page.extract_text() or "").strip()
            if text:
                pages.append(f"--- page {index} ---\n{text}")

    raw = "\n\n".join(pages)
    if not raw.strip():
        raise PdfExtractionError(
            f"No extractable text found in {pdf_path.name}. This usually means the "
            "PDF is scanned (image-only). OCR is not supported in the MVP — try "
            "exporting a text-based PDF, or use a .eml/.txt copy instead."
        )
    return raw
