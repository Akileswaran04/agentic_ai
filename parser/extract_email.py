"""Extract readable text from a trial-confirmation email or plain text file.

Supports:
- ``.eml`` — parsed with ``mailparser``; sender/subject/date + text body.
- ``.txt``  — read verbatim (e.g. a pasted email or chat transcript).

HTML bodies are reduced to plain text with a small stdlib HTML parser so
promotional markup doesn't pollute the extraction prompt.
"""
from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path


class _TextFromHTML(HTMLParser):
    """Collect visible text from an HTML fragment (no external deps)."""

    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if text:
            self._chunks.append(text)

    def text(self) -> str:
        return "\n".join(self._chunks)


def _html_to_text(html: str) -> str:
    parser = _TextFromHTML()
    parser.feed(html)
    return parser.text()


def _fmt(value) -> str:
    """Render a mailparser header value (str, tuple(name, addr), or list)."""
    if isinstance(value, tuple):
        parts = list(value[:2]) + [""] * (2 - len(value))
        name, addr = str(parts[0] or "").strip(), str(parts[1] or "").strip()
        if name and addr:
            return f"{name} <{addr}>"
        return name or addr
    if isinstance(value, (list, tuple)):
        return ", ".join(part for part in (_fmt(v) for v in value) if part)
    return str(value or "").strip()


def _mail_to_text(mail) -> str:
    """Serialize a parsed mailparser object into one readable block."""
    headers: list[str] = []
    for label, value in (
        ("From", getattr(mail, "from_", None) or []),
        ("To", getattr(mail, "to", None) or []),
        ("Subject", mail.subject if mail.subject else []),
        ("Date", mail.date if mail.date else []),
    ):
        rendered = _fmt(value)
        if rendered:
            headers.append(f"{label}: {rendered}")

    body: str = ""
    text_parts = getattr(mail, "text_plain", None) or []
    if text_parts:
        body = "\n".join(str(p) for p in text_parts if str(p).strip())
    if not body.strip():
        html_parts = getattr(mail, "text_html", None) or []
        if html_parts:
            body = _html_to_text("\n".join(str(p) for p in html_parts))

    return "\n".join([*headers, "--- body ---", body]).strip()


def extract_text(path: str | Path) -> str:
    """Return header + body text for ``.eml``/``.txt`` files.

    Raises:
        FileNotFoundError: if *path* does not exist.
        ValueError: for any other file extension.
    """
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    suffix = file_path.suffix.lower()
    if suffix == ".txt":
        return file_path.read_text(encoding="utf-8", errors="replace").strip()
    if suffix == ".eml":
        import mailparser  # imported lazily so `--help` works before install

        mail = mailparser.parse_from_file(str(file_path))
        return _mail_to_text(mail)
    raise ValueError(
        f"Unsupported email file type {suffix!r}; expected '.eml' or '.txt'."
    )
