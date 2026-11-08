"""S0 ingest: a wet-lab assay document in, ``assay2chip/work/<id>/raw.json`` out.

    python assay2chip/stages/s0_ingest.py --paper assay2chip/fixtures/bead_capture_assay.md --id bead_capture
    python assay2chip/stages/s0_ingest.py --paper 10.1038/nmeth.1322 --id wsp
    python assay2chip/stages/s0_ingest.py --paper https://example.org/methods --id web --stdout

Accepted inputs (``source.kind`` in the spine): ``pdf``, ``md``, ``txt``, ``doi``, ``url``.
A local file is ingested **offline and deterministically**: no model call, no network, no
timestamp anywhere in the artefact, so the same bytes in give a byte-identical file out and
a diff in ``raw.json`` always means a real change. ``--offline`` refuses network work
outright, so a DOI or URL fails loudly instead of quietly reaching out.

Output shape (``assay2chip/README.md`` is authoritative)::

    { "id": ..., "source": {"kind": ..., "ref": ..., "sha256": ...}, "title": ...,
      "blocks": [ {"kind": "heading|paragraph|table|list", "text": ..., "page": 3} ] }

``source.sha256`` is the SHA-256 of the exact bytes ingested: the file for a local source,
the fetched payload for a DOI or URL. Local paths are recorded repo-relative, and a path
outside the repo is reduced to its file name, because CONFIDENTIALITY.md forbids committing
lab paths; the hash still pins the bytes.

An optional top-level ``note`` says how completely the source was ingested - "Source was
NOT fully ingested" with the reason when a DOI or URL turned out to be paywalled, a
metadata-only record, or unreachable. The same sentence is repeated as the first block, so
neither a header reader nor a block reader can miss it.

Blocks carry extra, optional keys. Consumers may ignore them, but S1 should not:

``block``            1-based ordinal of the block in the document, for citation.
``line``             1-based line in the source where the block starts (text/markdown).
``level``            heading depth, 1-6.
``rows``             a table as a list of rows of cell strings - the *structure*, not a
                     flattened string. ``text`` holds a pipe-rendered copy of the same
                     table so a text-only consumer still sees something.
``columns``          header cells, when the table declares a header row.
``items``            list items, one string each.
``footnotes``        footnote lines found directly under a table. Lab tables hide real
                     values down there ("* 25 uL for the second wash"), so they stay
                     attached to the table they qualify and are repeated in ``text``.
``needs_attention``  true when this block was parsed with less than full confidence.
``note``             why, in one sentence. Always present when ``needs_attention`` is.

A table that could not be parsed into clean cells is still emitted as ``kind: "table"``
with its raw text, ``needs_attention: true`` and a ``note`` - never silently dropped and
never promoted to trustworthy. Nothing here guesses a missing number: S1 owns that, with
``confidence`` and ``unresolved``.

Page numbers are mandatory downstream (every later claim must cite one). PDFs carry real
pages. Markdown and text have none, so they default to page 1 and honour two explicit
markers: a form feed (``\\f``) starts the next page, and ``<!-- page 7 -->`` sets the page
number for everything after it.

The artefact is written with ``assay2chip.lib.io.write_stage(id, "raw", raw)`` - validated
and atomic. That import is late and guarded only so that a fresh clone which has not run
``pip install -r requirements.txt`` can still ingest a file; the local fallback writer
produces the same bytes.

Dependencies: standard library, plus ``pypdf`` (pure Python) for PDF text. If pypdf is not
installed the PDF path falls back to a small zlib-based extractor in this file and flags
every block it produces, so a fresh clone still runs before ``pip install``.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
import re
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import zlib
from html import unescape
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORK = ROOT / "assay2chip" / "work"

# S0 writes nothing outside assay2chip/work/<id>/, so the orchestrator has nothing to guard.
WRITES: list = []

SOURCE_KINDS = ("pdf", "md", "txt", "doi", "url")
BLOCK_KINDS = ("heading", "paragraph", "table", "list")

# Key order inside a block. Fixed so the JSON is stable across runs and readable in a diff.
BLOCK_KEYS = (
    "kind", "text", "page", "block", "line", "level",
    "columns", "rows", "items", "footnotes", "needs_attention", "note",
)

USER_AGENT = "assay2chip-s0-ingest/1 (python-urllib; research prototype, one request per run)"
FETCH_TIMEOUT = 30

DOI_RE = re.compile(r"^(?:doi:\s*|https?://(?:dx\.)?doi\.org/)?(10\.\d{4,9}/\S+)$", re.I)
MD_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
SETEXT_RE = re.compile(r"^\s{0,3}(=+|-{2,})\s*$")
PAGE_IN_COMMENT_RE = re.compile(r"^\s*page\s*[:=]?\s*(\d{1,5})\s*$", re.I)
FENCE_RE = re.compile(r"^\s{0,3}(```+|~~~+)")
BULLET_RE = re.compile(r"^(\s{0,6})([-*+\u2022\u00b7])\s+(\S.*)$")
# "1." / "2)" / "3 -" / "4b." / "iv)" / "a)"  - a separator is required so that a line
# like "100 uL of wash_buffer" is not mistaken for step 100.
ORDERED_RE = re.compile(r"^(\s{0,6})(\d{1,3}[a-z]?|[ivxlcIVXLC]{1,6}|[a-zA-Z])\s*[.)\]\u2013\u2014:-]\s+(\S.*)$")
# "Step 3 -", "Stage 2b:" - the separator is still required, because "step 7 and the
# supernatant is discarded" is a sentence, not the start of a list.
STEP_WORD_RE = re.compile(r"^(\s{0,6})(?:step|stage)\s+(\d{1,3}[a-z]?|[ivxlcIVXLC]{1,6})\s*[.)\u2013\u2014:-]\s+(\S.*)$", re.I)
FOOTNOTE_RE = re.compile(
    r"^\s{0,6}(?:"
    r"[*\u2020\u2021\u00a7\u00b6#]{1,3}\s*(?=\S)"          # *, dagger, section, ...
    r"|\[\^[^\]]{1,20}\]\s*:"                              # [^1]:
    r"|\(?[a-z]\)\s+(?=\S)"                                # a)  (a)
    r"|[\u00b9\u00b2\u00b3\u2070-\u2079]\s*(?=\S)"         # superscript digits
    r"|(?:foot)?notes?\s*[:.]\s*(?=\S)"                    # Note: / Footnote:
    r")"
)
CELL_GAP_RE = re.compile(r"\s{2,}")
PAYWALL_MARKERS = (
    "access through your institution", "buy article", "get access", "purchase pdf",
    "purchase access", "subscribe to journal", "sign in to read", "institutional login",
    "this article is available to subscribers", "paywall", "rent this article",
    "you have no access", "check access",
)


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------

def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def slug(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").lower()
    return text or "untitled"


def tidy(text: str) -> str:
    """One canonical spelling of any run of whitespace, so output does not wobble."""
    return re.sub(r"[\s\u00a0]+", " ", unicodedata.normalize("NFC", text)).strip()


def normalise_source_text(data: bytes) -> str:
    text = data.decode("utf-8", errors="replace")
    if text.startswith("\ufeff"):
        text = text[1:]
    return text.replace("\r\n", "\n").replace("\r", "\n")


class Blocks:
    """Accumulator that keeps the spine shape and the canonical key order."""

    def __init__(self) -> None:
        self.items: list[dict] = []

    def add(self, kind: str, text: str, page: int, **extra) -> dict:
        if kind not in BLOCK_KINDS:
            raise ValueError("block kind %r is not in the spine" % kind)
        block = {"kind": kind, "text": text, "page": max(1, int(page))}
        for key, value in extra.items():
            if key not in BLOCK_KEYS:
                raise ValueError("block key %r is not declared in BLOCK_KEYS" % key)
            if value not in (None, [], ""):
                block[key] = value
        if block.get("needs_attention") and not block.get("note"):
            raise ValueError("needs_attention without a note")
        self.items.append(block)
        return block

    def flag(self, block: dict, note: str) -> None:
        block["needs_attention"] = True
        block["note"] = note if not block.get("note") else block["note"] + " " + note

    def finalise(self) -> list[dict]:
        out = []
        for index, block in enumerate(self.items, 1):
            if not block["text"].strip():
                continue
            block["block"] = index
            out.append({k: block[k] for k in BLOCK_KEYS if k in block})
        # Re-number after dropping empties so `block` is a contiguous citation key.
        for index, block in enumerate(out, 1):
            block["block"] = index
        return out


def render_rows(rows: list[list[str]]) -> str:
    return "\n".join("| " + " | ".join(cell for cell in row) + " |" for row in rows)


def table_text(rows: list[list[str]], footnotes: list[str], raw: str = "") -> str:
    parts = [render_rows(rows)] if rows else ([raw.strip()] if raw.strip() else [])
    parts += ["Footnote: " + note for note in footnotes]
    return "\n".join(part for part in parts if part)


# --------------------------------------------------------------------------------------
# plain text / markdown
# --------------------------------------------------------------------------------------

def strip_comment(raw: str, state: dict) -> str:
    """Drop HTML comments. A `<!-- page N -->` comment sets the page from here on."""
    kept: list[str] = []
    rest = raw
    while rest:
        if state["open"]:
            end = rest.find("-->")
            if end < 0:
                break
            state["open"] = False
            rest = rest[end + 3:]
            continue
        start = rest.find("<!--")
        if start < 0:
            kept.append(rest)
            break
        kept.append(rest[:start])
        body = rest[start + 4:]
        end = body.find("-->")
        if end < 0:
            state["open"] = True
            break
        marker = PAGE_IN_COMMENT_RE.match(body[:end])
        if marker:
            state["page"] = max(1, int(marker.group(1)))
        rest = body[end + 3:]
    return "".join(kept)


def paginate(text: str) -> list[tuple[str, int, int]]:
    """(line, page, line number). Form feeds and `<!-- page N -->` set the page."""
    lines: list[tuple[str, int, int]] = []
    state = {"open": False, "page": 1}
    for lineno, raw in enumerate(text.split("\n"), 1):
        page = state["page"]
        raw = strip_comment(raw, state)
        if "\f" in raw:
            for offset, piece in enumerate(raw.split("\f")):
                if offset:
                    state["page"] += 1
                    page = state["page"]
                lines.append((piece.expandtabs(4).rstrip(), page, lineno))
            continue
        if not raw.strip():
            lines.append(("", state["page"], lineno))
            continue
        lines.append((raw.expandtabs(4).rstrip(), page, lineno))
    return lines


def is_pipe_row(line: str) -> bool:
    stripped = line.strip()
    return stripped.count("|") >= 2 and not stripped.startswith("||")


def is_delimiter_row(line: str) -> bool:
    cells = split_pipe_row(line)
    return bool(cells) and all(re.fullmatch(r":?-{2,}:?", cell.strip()) for cell in cells if cell.strip() != "")


def split_pipe_row(line: str) -> list[str]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|") and not stripped.endswith("\\|"):
        stripped = stripped[:-1]
    return [tidy(cell) for cell in re.split(r"(?<!\\)\|", stripped)]


def looks_tabular(lines: list[str]) -> bool:
    """Whitespace-aligned columns: PDFs and pasted tables arrive this way."""
    candidates = [line for line in lines if line.strip()]
    if len(candidates) < 2:
        return False
    counts = [len(CELL_GAP_RE.findall(line.strip())) for line in candidates]
    wide = [count for count in counts if count >= 2]
    return len(wide) >= 2 and len(wide) >= len(candidates) - 1


def split_space_row(line: str) -> list[str]:
    return [tidy(cell) for cell in CELL_GAP_RE.split(line.strip()) if tidy(cell)]


def harvest_footnotes(lines: list[tuple[str, int, int]], start: int, table_raw: str) -> tuple[list[str], int]:
    """Footnote lines sitting directly under a table (at most one blank line between)."""
    notes: list[str] = []
    index = start
    blanks = 0
    while index < len(lines):
        line = lines[index][0]
        if not line.strip():
            blanks += 1
            if blanks > 1 or notes:
                break
            index += 1
            continue
        if MD_HEADING_RE.match(line) or is_pipe_row(line):
            break
        if not FOOTNOTE_RE.match(line):
            break
        if line.strip()[0] in "*\u2022" and " " in line.strip()[:2] and "*" not in table_raw:
            break  # a bullet list, not a footnote marker
        note = tidy(line)
        index += 1
        while index < len(lines):  # a footnote may wrap
            nxt = lines[index][0]
            if (not nxt.strip() or FOOTNOTE_RE.match(nxt) or MD_HEADING_RE.match(nxt)
                    or is_pipe_row(nxt) or BULLET_RE.match(nxt) or ORDERED_RE.match(nxt)):
                break
            note += " " + tidy(nxt)
            index += 1
        notes.append(note)
        blanks = 0
    return notes, index


def marker_warning(markers: list[str]) -> str:
    """A note when step numbering is inconsistent - S1 must not trust the order blindly."""
    if len(markers) < 2:
        return ""
    styles = set()
    for marker in markers:
        if re.fullmatch(r"\d{1,3}", marker):
            styles.add("number")
        elif re.fullmatch(r"\d{1,3}[a-z]", marker):
            styles.add("number+letter")
        elif re.fullmatch(r"[ivxlcIVXLC]{1,6}", marker):
            styles.add("roman")
        elif re.fullmatch(r"[a-zA-Z]", marker):
            styles.add("letter")
        else:
            styles.add("other")
    problems = []
    if len(styles) > 1:
        problems.append("mixed numbering styles (%s)" % ", ".join(sorted(styles)))
    numbers = [int(re.match(r"\d+", m).group(0)) for m in markers if re.match(r"\d+", m)]
    if len(numbers) >= 2 and any(b - a not in (0, 1) for a, b in zip(numbers, numbers[1:])):
        problems.append("non-sequential numbers (%s)" % ", ".join(markers))
    elif len(numbers) >= 2 and any(b == a for a, b in zip(numbers, numbers[1:])):
        problems.append("repeated numbers (%s)" % ", ".join(markers))
    if not problems:
        return ""
    return ("Ordered list numbering is irregular: %s; step order and step count need "
            "checking before use." % "; ".join(problems))


def parse_lines(lines: list[tuple[str, int, int]], blocks: Blocks, pdf: bool = False,
                carry_note: str = "") -> None:
    """The one line-oriented parser, shared by markdown, plain text and PDF page text."""
    index = 0
    total = len(lines)
    while index < total:
        line, page, lineno = lines[index]
        if not line.strip():
            index += 1
            continue

        fence = FENCE_RE.match(line)
        if fence:
            marker = fence.group(1)[:3]
            body: list[str] = []
            index += 1
            while index < total and not lines[index][0].strip().startswith(marker):
                body.append(lines[index][0])
                index += 1
            index += 1 if index < total else 0
            if looks_tabular(body):
                rows = [split_space_row(item) for item in body if item.strip()]
                block = blocks.add("table", table_text(rows, [], "\n".join(body)), page,
                                   line=lineno, rows=rows)
                blocks.flag(block, "Table came from a fenced/preformatted block: S0 could "
                                   "not parse the columns with certainty, they were split "
                                   "on whitespace.")
            else:
                block = blocks.add("paragraph", "\n".join(item for item in body if item.strip()),
                                   page, line=lineno)
                if block["text"]:
                    blocks.flag(block, "Preformatted block kept verbatim; it may be a table "
                                       "or a listing rather than prose.")
            continue

        heading = MD_HEADING_RE.match(line)
        if heading:
            blocks.add("heading", tidy(heading.group(2)), page,
                       line=lineno, level=len(heading.group(1)))
            index += 1
            continue

        # Setext heading: text on one line, ==== or ---- under it.
        if (index + 1 < total and SETEXT_RE.match(lines[index + 1][0])
                and not is_pipe_row(line) and len(line.strip()) <= 120):
            blocks.add("heading", tidy(line), page, line=lineno,
                       level=1 if lines[index + 1][0].strip().startswith("=") else 2)
            index += 2
            continue

        if is_pipe_row(line):
            raw_lines = []
            while index < total and is_pipe_row(lines[index][0]):
                raw_lines.append(lines[index][0])
                index += 1
            raw = "\n".join(raw_lines)
            parsed = [split_pipe_row(item) for item in raw_lines]
            delimiter_at = next((i for i, item in enumerate(raw_lines) if is_delimiter_row(item)), None)
            columns: list[str] = []
            if delimiter_at == 1:
                columns = parsed[0]
                rows = [parsed[0]] + parsed[2:]
            else:
                rows = [row for i, row in enumerate(parsed) if not is_delimiter_row(raw_lines[i])]
            widths = {len(row) for row in rows}
            notes, index = harvest_footnotes(lines, index, raw)
            block = blocks.add("table", table_text(rows, notes, raw), page, line=lineno,
                               columns=columns, rows=rows, footnotes=notes)
            if delimiter_at is None:
                blocks.flag(block, "Pipe table has no header delimiter row, so the header "
                                   "and column meanings are unverified.")
            elif delimiter_at != 1:
                blocks.flag(block, "Header delimiter row is not the second row; the header "
                                   "is unverified.")
            if len(widths) > 1:
                blocks.flag(block, "Rows have differing cell counts (%s); cells may be "
                                   "misaligned." % ", ".join(str(w) for w in sorted(widths)))
            if notes:
                blocks.flag(block, "Table carries %d footnote(s), which in lab tables often "
                                   "hold a value the cells omit; resolve them before "
                                   "trusting the row." % len(notes))
            if carry_note:
                blocks.flag(block, carry_note)
            continue

        # Whitespace-aligned table (PDF text, pasted tables).
        run_end = index
        while run_end < total and lines[run_end][0].strip():
            run_end += 1
        run = [lines[i][0] for i in range(index, run_end)]
        if looks_tabular(run):
            rows = [split_space_row(item) for item in run]
            notes, after = harvest_footnotes(lines, run_end, "\n".join(run))
            block = blocks.add("table", table_text(rows, notes, "\n".join(run)), page,
                               line=lineno, rows=rows, footnotes=notes)
            blocks.flag(block, "Table was reconstructed from whitespace alignment%s: S0 "
                               "could not parse the cell boundaries with certainty, so the "
                               "cells need checking."
                        % (" in extracted PDF text" if pdf else ""))
            if notes:
                blocks.flag(block, "It also carries %d footnote(s) that may hold values the "
                                   "cells omit." % len(notes))
            if carry_note:
                blocks.flag(block, carry_note)
            index = after
            continue

        bullet = BULLET_RE.match(line)
        ordered = ORDERED_RE.match(line) or STEP_WORD_RE.match(line)
        if bullet or ordered:
            items: list[str] = []
            markers: list[str] = []
            kind_is_ordered = ordered is not None
            while index < total:
                current = lines[index][0]
                if not current.strip():
                    # One blank line inside a list is still the same list; two end it.
                    if index + 1 < total and (BULLET_RE.match(lines[index + 1][0])
                                              or ORDERED_RE.match(lines[index + 1][0])
                                              or STEP_WORD_RE.match(lines[index + 1][0])):
                        index += 1
                        continue
                    break
                match = BULLET_RE.match(current)
                omatch = ORDERED_RE.match(current) or STEP_WORD_RE.match(current)
                if match and not kind_is_ordered:
                    items.append(tidy(match.group(3)))
                elif omatch and kind_is_ordered:
                    markers.append(omatch.group(2))
                    items.append(tidy(omatch.group(3)))
                elif match or omatch or MD_HEADING_RE.match(current) or is_pipe_row(current):
                    break
                elif items:
                    items[-1] += " " + tidy(current)  # a step wrapped onto the next line
                else:
                    break
                index += 1
            text = "\n".join(("%s. %s" % (markers[i], items[i])) if kind_is_ordered and i < len(markers)
                             else items[i] for i in range(len(items)))
            block = blocks.add("list", text, page, line=lineno, items=items)
            warning = marker_warning(markers) if kind_is_ordered else ""
            if warning:
                blocks.flag(block, warning)
            if carry_note:
                blocks.flag(block, carry_note)
            continue

        # Paragraph: soft-wrapped lines are rejoined; sentence splitting belongs to S1.
        para: list[str] = []
        while index < total:
            current = lines[index][0]
            if (not current.strip() or MD_HEADING_RE.match(current) or is_pipe_row(current)
                    or FENCE_RE.match(current) or BULLET_RE.match(current)
                    or ORDERED_RE.match(current) or STEP_WORD_RE.match(current)):
                break
            if index + 1 < total and SETEXT_RE.match(lines[index + 1][0]):
                break
            para.append(current)
            index += 1
        if para:
            block = blocks.add("paragraph", tidy(" ".join(para)), page, line=lineno)
            if carry_note:
                blocks.flag(block, carry_note)
        else:
            index += 1


def ingest_text(data: bytes, kind: str, fallback_title: str) -> tuple[str, list[dict]]:
    text = normalise_source_text(data)
    blocks = Blocks()
    parse_lines(paginate(text), blocks)
    done = blocks.finalise()
    title = next((b["text"] for b in done if b["kind"] == "heading"), "")
    if not title:
        title = next((b["text"][:120] for b in done if b["kind"] == "paragraph"), "")
    return (title or fallback_title), done


# --------------------------------------------------------------------------------------
# PDF
# --------------------------------------------------------------------------------------

def pdf_pages_pypdf(data: bytes) -> tuple[str, list[str], str]:
    from pypdf import PdfReader  # optional dependency, see module docstring

    reader = PdfReader(io.BytesIO(data))
    pages: list[str] = []
    note = ""
    for page in reader.pages:
        try:
            text = page.extract_text(extraction_mode="layout") or ""
        except Exception:
            text = page.extract_text() or ""
            note = ("PDF text was extracted without layout mode, so table columns may be "
                    "collapsed.")
        pages.append(text.replace("\r\n", "\n").replace("\r", "\n"))
    title = ""
    try:
        title = tidy((reader.metadata or {}).get("/Title") or "")
    except Exception:
        title = ""
    return title, pages, note


def _decode_pdf_string(raw: str) -> str:
    if raw.startswith("<"):
        hexdigits = re.sub(r"[^0-9A-Fa-f]", "", raw[1:-1])
        if len(hexdigits) % 2:
            hexdigits += "0"
        try:
            return bytes.fromhex(hexdigits).decode("utf-16-be" if hexdigits[:4].lower() == "feff"
                                                  else "latin-1", errors="replace")
        except ValueError:
            return ""
    body = raw[1:-1]
    out: list[str] = []
    i = 0
    escapes = {"n": "\n", "r": "\n", "t": "\t", "b": "", "f": "", "(": "(", ")": ")", "\\": "\\"}
    while i < len(body):
        char = body[i]
        if char == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            if nxt in escapes:
                out.append(escapes[nxt])
                i += 2
                continue
            octal = re.match(r"[0-7]{1,3}", body[i + 1:])
            if octal:
                out.append(chr(int(octal.group(0), 8)))
                i += 1 + len(octal.group(0))
                continue
            i += 2
            continue
        out.append(char)
        i += 1
    return "".join(out)


def pdf_pages_stdlib(data: bytes) -> list[str]:
    """Last-resort PDF text with zlib only, for a clone that has not pip-installed yet.

    Content streams are read in file order, which is usually but not always page order,
    and no font encoding is honoured. Every block built from this is flagged.
    """
    pages: list[str] = []
    token_re = re.compile(rb"\((?:\\.|[^()\\])*\)|<[0-9A-Fa-f\s]+>|T[jJdDm]|T\*|ET|'|\"")
    for match in re.finditer(rb"stream\r?\n?(.*?)endstream", data, re.S):
        chunk = match.group(1)
        for candidate in (chunk, chunk.strip(b"\r\n")):
            try:
                chunk = zlib.decompress(candidate)
                break
            except zlib.error:
                continue
        if b"BT" not in chunk or (b"Tj" not in chunk and b"TJ" not in chunk):
            continue
        out: list[str] = []
        for token in token_re.findall(chunk):
            text = token.decode("latin-1")
            if text[0] in "(<":
                out.append(_decode_pdf_string(text))
            elif text in ("Td", "TD", "Tm", "T*", "ET", "'", '"'):
                out.append("\n")
        # Internal runs of spaces are kept: they are the only trace of table columns left.
        page = "\n".join(unicodedata.normalize("NFC", line).rstrip()
                         for line in "".join(out).split("\n"))
        pages.append(re.sub(r"\n{3,}", "\n\n", page))
    return pages


def ingest_pdf(data: bytes, fallback_title: str) -> tuple[str, list[dict]]:
    carry = ""
    meta_title = ""
    if importlib.util.find_spec("pypdf") is not None:
        meta_title, pages, note = pdf_pages_pypdf(data)
        carry = note
    else:
        pages = pdf_pages_stdlib(data)
        carry = ("Extracted without pypdf (zlib fallback): text order, spacing and page "
                 "numbers are approximate - install pypdf and re-run S0 before relying on "
                 "this.")
    blocks = Blocks()
    for number, page_text in enumerate(pages, 1):
        lines = [(line.expandtabs(4).rstrip(), number, 0) for line in page_text.split("\n")]
        parse_lines(lines, blocks, pdf=True, carry_note=carry)
    done = blocks.finalise()
    for block in done:
        block.pop("line", None)
    title = meta_title or next((b["text"] for b in done if b["kind"] == "heading"), "")
    if not title:
        title = next((b["text"][:120] for b in done if b["kind"] == "paragraph"), "")
    return (title or fallback_title), done


# --------------------------------------------------------------------------------------
# HTML
# --------------------------------------------------------------------------------------

class HtmlBlocks(HTMLParser):
    """Headings, paragraphs, lists and - crucially - real tables out of an HTML page."""

    SKIP = {"script", "style", "noscript", "svg", "head", "nav", "footer", "form", "button",
            "aside", "menu", "dialog", "template", "select", "option", "label", "textarea",
            "video", "audio", "canvas", "iframe"}
    BLOCKISH = {"p", "div", "section", "article", "blockquote", "dd", "dt", "figcaption", "caption"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks = Blocks()
        self.skip_depth = 0
        self.title = ""
        self.in_title = False
        self.buffer: list[str] = []
        self.heading_level = 0
        self.list_items: list[str] = []
        self.in_list = 0
        self.in_item = False
        self.table_depth = 0
        self.rows: list[list[str]] = []
        self.row: list[str] = []
        self.header_row: list[str] = []
        self.in_cell = False
        self.cell_is_header = False

    # -- text sink ---------------------------------------------------------------
    def handle_data(self, data: str) -> None:
        if self.skip_depth:
            return
        if self.in_title:
            self.title += data
        else:
            self.buffer.append(data)

    def _take(self) -> str:
        text = tidy("".join(self.buffer))
        self.buffer = []
        return text

    # -- structure ---------------------------------------------------------------
    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in self.SKIP:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if tag == "title":
            self.in_title = True
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.flush_paragraph()
            self.heading_level = int(tag[1])
            self.buffer = []
        elif tag == "table":
            self.flush_paragraph()
            self.table_depth += 1
            if self.table_depth == 1:
                self.rows, self.row, self.header_row = [], [], []
        elif tag == "tr" and self.table_depth:
            self.row = []
        elif tag in ("td", "th") and self.table_depth:
            self.in_cell = True
            self.cell_is_header = tag == "th"
            self.buffer = []
        elif tag in ("ul", "ol"):
            self.flush_paragraph()
            self.in_list += 1
            if self.in_list == 1:
                self.list_items = []
        elif tag == "li" and self.in_list:
            self.in_item = True
            self.buffer = []
        elif tag == "br":
            self.buffer.append(" ")
        elif tag in self.BLOCKISH:
            self.flush_paragraph()

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP:
            self.skip_depth = max(0, self.skip_depth - 1)
            return
        if self.skip_depth:
            return
        if tag == "title":
            self.in_title = False
            self.title = tidy(self.title)
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6") and self.heading_level:
            text = self._take()
            if text:
                self.blocks.add("heading", text, 1, level=self.heading_level)
            self.heading_level = 0
        elif tag in ("td", "th") and self.in_cell:
            cell = self._take()
            self.row.append(cell)
            if self.cell_is_header:
                self.header_row.append(cell)
            self.in_cell = False
        elif tag == "tr" and self.table_depth:
            if any(cell for cell in self.row):
                self.rows.append(self.row)
            self.row = []
        elif tag == "table" and self.table_depth:
            self.table_depth -= 1
            if self.table_depth == 0:
                self.flush_table()
        elif tag == "li" and self.in_item:
            item = self._take()
            if item:
                self.list_items.append(item)
            self.in_item = False
        elif tag in ("ul", "ol"):
            self.in_list = max(0, self.in_list - 1)
            if self.in_list == 0 and self.list_items:
                self.blocks.add("list", "\n".join(self.list_items), 1, items=self.list_items)
                self.list_items = []
        elif tag in self.BLOCKISH:
            self.flush_paragraph()

    def flush_paragraph(self) -> None:
        if self.in_cell or self.in_item or self.heading_level or self.table_depth:
            return
        text = self._take()
        if len(text) >= 2:
            self.blocks.add("paragraph", text, 1)

    def flush_table(self) -> None:
        rows = [row for row in self.rows if any(cell for cell in row)]
        if not rows:
            return
        widths = {len(row) for row in rows}
        block = self.blocks.add("table", table_text(rows, []), 1,
                                columns=self.header_row, rows=rows)
        if not self.header_row:
            self.blocks.flag(block, "HTML table declares no header cells; column meanings "
                                    "are unverified.")
        if len(widths) > 1:
            self.blocks.flag(block, "Rows have differing cell counts (%s), probably merged "
                                    "cells; cells may be misaligned."
                             % ", ".join(str(w) for w in sorted(widths)))
        self.rows, self.header_row = [], []


MAIN_RE = re.compile(r"<(main|article)\b[^>]*>(.*?)</\1\s*>", re.S | re.I)


def main_region(text: str) -> str:
    """The article body, when the page marks one. Publisher pages are mostly chrome."""
    best = ""
    for match in MAIN_RE.finditer(text):
        if len(match.group(2)) > len(best):
            best = match.group(2)
    return best if len(best) > 400 else text


def ingest_html(data: bytes, charset: str = "utf-8") -> tuple[str, list[dict], str]:
    text = data.decode(charset or "utf-8", errors="replace")
    parser = HtmlBlocks()
    title_match = re.search(r"<title\b[^>]*>(.*?)</title\s*>", text, re.S | re.I)
    if title_match:
        parser.title = tidy(unescape(re.sub(r"<[^>]+>", " ", title_match.group(1))))
    parser.feed(main_region(text))
    parser.flush_paragraph()
    blocks = parser.blocks.finalise()
    title = parser.title or next((b["text"] for b in blocks if b["kind"] == "heading"), "")
    body = " ".join(b["text"] for b in blocks if b["kind"] in ("paragraph", "list", "table"))
    lowered = text.lower()
    hits = [marker for marker in PAYWALL_MARKERS if marker in lowered]
    verdict = ""
    if hits:
        verdict = ("The page shows access-control wording (%s), so the full text is very "
                   "likely paywalled." % ", ".join(sorted(hits)[:3]))
    elif len(body) < 1500:
        verdict = ("Only %d characters of body text were reachable, which is far short of a "
                   "methods section; assume the full text is behind a paywall or rendered "
                   "by JavaScript." % len(body))
    return title, blocks, verdict


# --------------------------------------------------------------------------------------
# network: URL and DOI
# --------------------------------------------------------------------------------------

def fetch(url: str, accept: str = "*/*") -> tuple[bytes, str, str, str]:
    """-> (body, content type, charset, error). Never raises for an HTTP failure."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    try:
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT) as response:
            body = response.read()
            ctype = (response.headers.get_content_type() or "").lower()
            charset = response.headers.get_content_charset() or ""
            return body, ctype, charset, ""
    except urllib.error.HTTPError as error:
        try:
            body = error.read()
        except Exception:
            body = b""
        reason = "HTTP %s %s" % (error.code, error.reason)
        if error.code in (401, 402, 403):
            reason += " (access denied - paywall or login wall)"
        return body, (error.headers.get_content_type() if error.headers else ""), "", reason
    except Exception as error:  # DNS, TLS, timeout, redirect loop
        return b"", "", "", "%s: %s" % (type(error).__name__, error)


def strip_jats(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    return tidy(text)


def crossref_metadata(doi: str) -> tuple[dict, bytes, str]:
    body, _ctype, _charset, error = fetch("https://api.crossref.org/works/" + urllib.parse.quote(doi),
                                          accept="application/json")
    if error or not body:
        return {}, body, error or "empty response from Crossref"
    try:
        return json.loads(body.decode("utf-8", "replace")).get("message", {}), body, ""
    except Exception as parse_error:
        return {}, body, "Crossref response was not JSON: %s" % parse_error


def ingest_remote(ref: str, kind: str) -> tuple[str, list[dict], bytes, list[str], bool]:
    """Fetch a DOI or URL.

    Returns (title, blocks, bytes that were hashed, retrieval notes, incomplete). The notes
    are always written into the artefact, so a reader can see exactly how much of the source
    was reachable; `incomplete` is true when the full text was not, which is the case the
    later stages must not mistake for "the method has no such step".
    """
    notes: list[str] = []
    payload = b""
    title = ""
    blocks: list[dict] = []
    target = ref
    incomplete = False

    if kind == "doi":
        doi = DOI_RE.match(ref).group(1)
        meta, body, error = crossref_metadata(doi)
        payload += body
        if error:
            incomplete = True
            notes.append("Crossref metadata could not be retrieved (%s)." % error)
        else:
            title = tidy((meta.get("title") or [""])[0])
            meta_bits = []
            for label, value in (("journal", (meta.get("container-title") or [""])[0]),
                                 ("type", meta.get("type", "")),
                                 ("publisher", meta.get("publisher", ""))):
                if value:
                    meta_bits.append("%s: %s" % (label, tidy(str(value))))
            year = ((meta.get("issued") or {}).get("date-parts") or [[None]])[0][0]
            if year:
                meta_bits.append("year: %s" % year)
            meta_bits.append("doi: %s" % doi)
            abstract = strip_jats(meta.get("abstract", "") or "")
            builder = Blocks()
            if title:
                builder.add("heading", title, 1, level=1)
            builder.add("paragraph", "Source metadata (Crossref): " + "; ".join(meta_bits), 1)
            if abstract:
                builder.add("paragraph", "Abstract: " + abstract, 1)
            else:
                notes.append("The publisher deposited no abstract with Crossref.")
            blocks = builder.finalise()
        target = "https://doi.org/" + doi

    body, ctype, charset, error = fetch(target, accept="text/html,application/pdf;q=0.9,*/*;q=0.1")
    if error:
        incomplete = True
        notes.append("Full text was not retrieved from %s (%s). Only the metadata above was "
                     "ingested." % (target, error))
    elif "pdf" in (ctype or ""):
        payload += body
        pdf_title, pdf_blocks = ingest_pdf(body, title or ref)
        title = title or pdf_title
        blocks = blocks + renumber(pdf_blocks, len(blocks))
        notes.append("Full text was retrieved as a PDF from %s." % target)
    elif body:
        payload += body
        html_title, html_blocks, verdict = ingest_html(body, charset)
        title = title or html_title
        blocks = blocks + renumber(html_blocks, len(blocks))
        if verdict:
            incomplete = True
            notes.append(verdict)
        else:
            notes.append("Full text (or at least the article body) was retrieved as HTML "
                         "from %s." % target)
    else:
        incomplete = True
        notes.append("The response from %s had no body." % target)

    if not blocks:
        incomplete = True
        notes.append("No readable blocks could be built from %s." % ref)
    return title, blocks, payload, notes, incomplete


def renumber(blocks: list[dict], offset: int) -> list[dict]:
    for index, block in enumerate(blocks, offset + 1):
        block["block"] = index
    return blocks


# --------------------------------------------------------------------------------------
# source resolution
# --------------------------------------------------------------------------------------

def detect_kind(ref: str) -> str:
    if DOI_RE.match(ref.strip()):
        return "doi"
    if re.match(r"^https?://", ref.strip(), re.I):
        return "url"
    path = Path(ref).expanduser()
    if not path.is_file():
        raise FileNotFoundError(
            "%s is not a readable file, a URL or a DOI. Give --paper a local .pdf/.md/.txt, "
            "an http(s) URL, or a DOI like 10.1038/nmeth.1322." % ref)
    try:
        magic = path.open("rb").read(5)
    except OSError as error:
        raise FileNotFoundError("%s cannot be read: %s" % (ref, error))
    if magic == b"%PDF-":  # trust the bytes over the extension
        return "pdf"
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return "pdf"
    if suffix in (".md", ".markdown", ".mdown"):
        return "md"
    return "txt"


def source_ref(ref: str, kind: str) -> str:
    """How the source is recorded. Local paths are repo-relative; paths outside the repo
    are reduced to a file name, because CONFIDENTIALITY.md forbids committing lab paths.
    The sha256 still pins the exact bytes."""
    if kind in ("doi", "url"):
        return ref.strip()
    path = Path(ref).expanduser().resolve()
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.name


# --------------------------------------------------------------------------------------
# the stage
# --------------------------------------------------------------------------------------

def ingest(ref: str, assay_id: str = "", offline: bool = False) -> dict:
    """Build the raw.json payload. No file writing, no clock, no randomness."""
    kind = detect_kind(ref)
    notes: list[str] = []
    incomplete = False

    if kind in ("doi", "url"):
        if offline:
            raise RuntimeError("--offline was given but %r needs the network. Download the "
                               "paper and pass the local file instead." % ref)
        title, blocks, payload, notes, incomplete = ingest_remote(ref, kind)
        fallback_title = ref.strip()
    else:
        data = Path(ref).expanduser().read_bytes()
        payload = data
        fallback_title = re.sub(r"[_-]+", " ", Path(ref).stem).strip() or "untitled"
        if kind == "pdf":
            title, blocks = ingest_pdf(data, fallback_title)
        else:
            title, blocks = ingest_text(data, kind, fallback_title)
        if not blocks:
            incomplete = True
            notes.append("No text could be extracted from this file; it may be a scan with "
                         "no text layer, in which case S1 has nothing to work from.")

    if notes:
        banner = Blocks()
        block = banner.add("paragraph", "Retrieval note: " + " ".join(notes), 1)
        if incomplete:
            banner.flag(block, "S0 could not retrieve the whole source, so a step may be "
                               "missing from these blocks entirely; downstream stages must "
                               "not read silence here as the method not having the step.")
        blocks = renumber(banner.finalise() + blocks, 0)

    raw = {
        "id": assay_id or slug(Path(ref).stem if kind not in ("doi", "url") else (title or ref)),
        "source": {"kind": kind, "ref": source_ref(ref, kind), "sha256": sha256_hex(payload)},
        "title": title or fallback_title,
    }
    if notes:
        # Also stated at the top level, so "the full text was paywalled" cannot be missed
        # by anything that reads the header and not the blocks.
        raw["note"] = ("Source was fully ingested. " if not incomplete
                       else "Source was NOT fully ingested. ") + " ".join(notes)
    raw["blocks"] = blocks
    return raw


def validate_shape(raw: dict) -> list[str]:
    """Check the S0 contract from assay2chip/README.md. Returns a list of problems."""
    problems: list[str] = []
    allowed = {"id", "source", "title", "blocks", "note"}
    for key in ("id", "source", "title", "blocks"):
        if key not in raw:
            problems.append("missing top-level key %r" % key)
    if set(raw) - allowed:
        problems.append("unexpected top-level keys: %s" % ", ".join(sorted(set(raw) - allowed)))
    if not isinstance(raw.get("id"), str) or not raw.get("id"):
        problems.append("id must be a non-empty string")
    if not isinstance(raw.get("title"), str):
        problems.append("title must be a string")
    if "note" in raw and not (isinstance(raw["note"], str) and raw["note"].strip()):
        problems.append("note, when present, must be a non-empty string")
    source = raw.get("source")
    if not isinstance(source, dict):
        problems.append("source must be an object")
    else:
        if set(source) != {"kind", "ref", "sha256"}:
            problems.append("source keys must be exactly kind, ref, sha256 (got %s)"
                            % ", ".join(sorted(source)))
        if source.get("kind") not in SOURCE_KINDS:
            problems.append("source.kind %r not in %s" % (source.get("kind"), "|".join(SOURCE_KINDS)))
        if not re.fullmatch(r"[0-9a-f]{64}", str(source.get("sha256", ""))):
            problems.append("source.sha256 must be 64 hex characters")
        if not isinstance(source.get("ref"), str) or not source.get("ref"):
            problems.append("source.ref must be a non-empty string")
    blocks = raw.get("blocks")
    if not isinstance(blocks, list):
        problems.append("blocks must be a list")
        return problems
    for index, block in enumerate(blocks):
        where = "blocks[%d]" % index
        if not isinstance(block, dict):
            problems.append("%s must be an object" % where)
            continue
        if block.get("kind") not in BLOCK_KINDS:
            problems.append("%s.kind %r not in %s" % (where, block.get("kind"), "|".join(BLOCK_KINDS)))
        if not isinstance(block.get("text"), str) or not block.get("text").strip():
            problems.append("%s.text must be a non-empty string" % where)
        if not isinstance(block.get("page"), int) or isinstance(block.get("page"), bool) \
                or block.get("page", 0) < 1:
            problems.append("%s.page must be an integer >= 1" % where)
        unknown = set(block) - set(BLOCK_KEYS)
        if unknown:
            problems.append("%s has undeclared keys: %s" % (where, ", ".join(sorted(unknown))))
        if block.get("kind") == "table" and not isinstance(block.get("rows", []), list):
            problems.append("%s.rows must be a list of rows" % where)
        for row in block.get("rows", []) or []:
            if not isinstance(row, list) or not all(isinstance(cell, str) for cell in row):
                problems.append("%s.rows must hold lists of strings" % where)
                break
        if block.get("needs_attention") and not block.get("note"):
            problems.append("%s is flagged needs_attention but carries no note" % where)
        if block.get("block") != index + 1:
            problems.append("%s.block should be %d" % (where, index + 1))
    return problems


def serialise(raw: dict) -> str:
    """One canonical byte-for-byte rendering. No timestamps, ever."""
    return json.dumps(raw, indent=2, ensure_ascii=False, sort_keys=False) + "\n"


def stage_io():
    """`assay2chip.lib.io`, the shared stage writer, or None if it cannot be imported.

    It is imported late and defensively on purpose: it pulls in jsonschema, so a fresh
    clone that has not run `pip install -r requirements.txt` yet would otherwise be unable
    to run S0 at all. The local writer below produces the same bytes.
    """
    try:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from assay2chip.lib import io as module
    except Exception:
        return None
    return module


def write_raw(raw: dict, out: "Path | None" = None) -> Path:
    """Write raw.json through assay2chip.lib.io. `out` overrides the work-dir path."""
    library = stage_io()
    if library is not None:
        try:
            if out is None or Path(out) == WORK / raw["id"] / "raw.json":
                return Path(library.write_stage(raw["id"], "raw", raw))
            return Path(library.write_json(out, raw))
        except Exception:
            pass  # library unusable (fresh clone, no jsonschema); use the local writer
    out = out or (WORK / raw["id"] / "raw.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(serialise(raw), encoding="utf-8")
    return out


def run(paper: str = "", assay_id: str = "", *, id: str = "", source: str = "",
        work_root: "Path | str | None" = None, offline: bool = False,
        write: bool = True, out: "Path | str | None" = None, **_ignored) -> dict:
    """Stage entry point for assay2chip/run.py.

    `paper` (or `source`) is a path, URL or DOI; `assay_id` (or `id`) names the work
    directory. Returns the raw.json payload and, unless write=False, writes it with
    `assay2chip.lib.io.write_stage(id, "raw", ...)`. S0 never asks for a re-run: there is
    no earlier stage to re-run.
    """
    ref = paper or source
    if not ref:
        raise ValueError("s0_ingest.run needs a paper: a local .pdf/.md/.txt, a URL or a DOI")
    raw = ingest(ref, assay_id or id, offline=offline)
    problems = validate_shape(raw)
    if problems:
        raise RuntimeError("S0 produced an artefact that breaks the spine: "
                           + "; ".join(problems[:5]))
    if write:
        if out:
            write_raw(raw, Path(out))
        elif work_root:
            write_raw(raw, Path(work_root) / raw["id"] / "raw.json")
        else:
            write_raw(raw)
    return raw


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(
        prog="s0_ingest",
        description="S0: ingest a protocol (PDF, markdown, text, URL or DOI) into raw.json")
    parser.add_argument("paper", nargs="?", help="path, URL or DOI (same as --paper)")
    parser.add_argument("--paper", dest="paper_opt", metavar="PAPER",
                        help="path to a .pdf/.md/.txt, a URL, or a DOI")
    parser.add_argument("--id", default="", help="assay id; defaults to a slug of the file name")
    parser.add_argument("--out", help="write here instead of assay2chip/work/<id>/raw.json")
    parser.add_argument("--work-root", help="override the work directory root")
    parser.add_argument("--offline", action="store_true",
                        help="refuse any network access (a DOI or URL then fails loudly)")
    parser.add_argument("--stdout", action="store_true", help="print the artefact, write nothing")
    parser.add_argument("--check", action="store_true",
                        help="validate against the S0 contract and report, exit 1 on a problem")
    args = parser.parse_args(argv)

    ref = args.paper_opt or args.paper
    if not ref:
        parser.error("give a paper: a local .pdf/.md/.txt, an http(s) URL, or a DOI")

    try:
        raw = ingest(ref, args.id, offline=args.offline)
    except (FileNotFoundError, RuntimeError, ValueError) as error:
        print("s0_ingest: %s" % error, file=sys.stderr)
        return 2

    problems = validate_shape(raw)
    if problems:
        for problem in problems:
            print("s0_ingest: contract: %s" % problem, file=sys.stderr)
        return 1

    if args.stdout:
        sys.stdout.write(serialise(raw))
    else:
        root = Path(args.work_root) if args.work_root else WORK
        out = write_raw(raw, Path(args.out) if args.out else root / raw["id"] / "raw.json")
        flagged = sum(1 for block in raw["blocks"] if block.get("needs_attention"))
        tables = sum(1 for block in raw["blocks"] if block["kind"] == "table")
        pages = sorted({block["page"] for block in raw["blocks"]})
        print("s0_ingest: %s -> %s" % (raw["source"]["ref"],
                                       out.resolve().relative_to(ROOT)
                                       if ROOT in out.resolve().parents else out))
        print("  id=%s kind=%s pages=%s" % (raw["id"], raw["source"]["kind"],
                                            "%d-%d" % (pages[0], pages[-1]) if pages else "-"))
        print("  blocks=%d (tables=%d, needs_attention=%d)"
              % (len(raw["blocks"]), tables, flagged))
    if args.check:
        print("  contract: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
