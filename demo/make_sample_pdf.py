"""Generate ``sample_invoice.pdf`` - a minimal, valid single-page invoice.

Run:  python demo/make_sample_pdf.py
The PDF is built by hand (no external libs) so it works anywhere.
"""
from __future__ import annotations

from pathlib import Path

OUT = Path(__file__).resolve().parent / "sample_invoice.pdf"

LINES = [
    "Streamify Premium - Monthly Invoice",
    "Invoice number: INV-2026-0842",
    "Invoice date: September 1, 2026",
    "",
    "Amount due: 9.99 USD",
    "Billing period: October 15, 2026 to November 14, 2026",
    "Next automatic renewal: October 15, 2026",
    "",
    "Account and billing management:",
    "https://example.com/streamify/account",
    "",
    "Thank you for choosing Streamify!",
]


def _pdf_bytes(lines: list[str]) -> bytes:
    content = bytearray(b"BT\n/F1 12 Tf\n14 720 Td\n")
    for i, raw in enumerate(lines):
        if i:
            content += b"0 -18 Td\n"
        safe = raw.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        content += b"(" + safe.encode("ascii") + b") Tj\n"
    content += b"ET"

    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), bytes(content)),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for num, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % num + body + b"\nendobj\n"
    xref_pos = len(out)
    out += b"xref\n0 %d\n" % (len(objs) + 1)
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF" % (
        len(objs) + 1,
        xref_pos,
    )
    return bytes(out)


if __name__ == "__main__":
    OUT.write_bytes(_pdf_bytes(LINES))
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")
