"""Build the PDF fixture for S0, with the standard library only.

    python assay2chip/fixtures/make_pdf_fixture.py

Writes `assay2chip/fixtures/pdf_methods_excerpt.pdf`: two pages of generic, invented
methods text in a monospaced font, including a whitespace-aligned step table and a table
footnote that carries a volume the table cells omit. The PDF holds no creation date and no
producer string, so re-running this writes a byte-identical file and S0's output over it
stays byte-identical too.

The text is synthetic, written for this repo. Nothing here comes from any lab's records.
"""

from __future__ import annotations

from pathlib import Path

OUT = Path(__file__).resolve().parent / "pdf_methods_excerpt.pdf"

PAGES = [
    [
        "Methods excerpt: continuous-flow wash of encapsulated cells",
        "",
        "Capsules carrying cells were held in the chamber while wash_buffer was",
        "perfused at a constant rate. The wash was run as three passes; the",
        "parameters of each pass are given in Table 2. Volumes are the volume",
        "delivered per pass and were set at the pump.",
        "",
        "Table 2. Wash passes",
        "",
        "Pass   Reagent        Volume (uL)   Flow (uL/min)   Time (min)   Temp (C)",
        "1      wash_buffer    150           25              6            37",
        "2      wash_buffer    150           25              6            37",
        "3      wash_buffer    *             10              6            25",
        "",
        "* The third pass uses 75 uL, delivered at the reduced flow rate.",
        "",
        "The chamber was not drained between passes.",
    ],
    [
        "Recovery",
        "",
        "1. Stop the perfusion and let the capsules settle for 10 min at 25 C.",
        "2. Draw off the supernatant from the upper port and discard it.",
        "3. Resuspend the capsules in 100 uL of wash_buffer and transfer them to",
        "   a chilled vessel.",
        "",
        "Capsule diameter was 70 um nominal. The recovered fraction was not",
        "quantified in this excerpt.",
    ],
]


def escape(text: str) -> bytes:
    out = text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
    return out.encode("latin-1", "replace")


def content_stream(lines: list[str]) -> bytes:
    parts = [b"BT", b"/F1 10 Tf", b"12 TL", b"1 0 0 1 56 760 Tm"]
    for line in lines:
        parts.append(b"(" + escape(line) + b") Tj")
        parts.append(b"T*")
    parts.append(b"ET")
    return b"\n".join(parts) + b"\n"


def build() -> bytes:
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>")
    pages_id = len(objects) + 2 * len(PAGES) + 1  # the pages tree follows the page objects
    page_ids: list[int] = []
    for lines in PAGES:
        stream = content_stream(lines)
        content = add(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n"
                      + stream + b"endstream")
        page_ids.append(add(
            b"<< /Type /Page /Parent " + str(pages_id).encode() + b" 0 R "
            b"/MediaBox [0 0 595 842] /Resources << /Font << /F1 "
            + str(font).encode() + b" 0 R >> >> /Contents "
            + str(content).encode() + b" 0 R >>"))
    kids = b" ".join(str(pid).encode() + b" 0 R" for pid in page_ids)
    tree = add(b"<< /Type /Pages /Kids [" + kids + b"] /Count "
               + str(len(page_ids)).encode() + b" >>")
    assert tree == pages_id, (tree, pages_id)
    catalog = add(b"<< /Type /Catalog /Pages " + str(tree).encode() + b" 0 R >>")
    info = add(b"<< /Title (Methods excerpt: continuous-flow wash of encapsulated cells) >>")

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += str(number).encode() + b" 0 obj\n" + body + b"\nendobj\n"
    xref_at = len(out)
    out += b"xref\n0 " + str(len(objects) + 1).encode() + b"\n"
    out += b"0000000000 65535 f \n"
    for offset in offsets[1:]:
        out += ("%010d 00000 n \n" % offset).encode()
    out += (b"trailer\n<< /Size " + str(len(objects) + 1).encode()
            + b" /Root " + str(catalog).encode() + b" 0 R"
            + b" /Info " + str(info).encode() + b" 0 R >>\nstartxref\n"
            + str(xref_at).encode() + b"\n%%EOF\n")
    return bytes(out)


if __name__ == "__main__":
    OUT.write_bytes(build())
    print("wrote %s (%d bytes)" % (OUT.name, OUT.stat().st_size))
