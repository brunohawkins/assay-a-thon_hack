"""Stage 1: read the source (a protocol, a paper, or a plain-English goal) into plain text.

load_source(input_path: str | None, prompt: str | None) -> dict
  {"kind": "prompt" | "protocol" | "paper", "title": str | None, "file": str | None, "text": str,
   "prompt": str | None (the user's goal, if given alongside a file), "chars": int,
   "methods_chars": int (papers: text[:methods_chars] is the Methods section, moved to the front; else 0)}

Formats: .md .txt (any plain text), .json (a contracts/schemas/protocol.schema.json protocol is
rendered as numbered steps), .csv/.tsv and .xlsx (rendered as pipe tables), .docx (paragraphs,
numbered lists and tables), .pdf (pdftotext if installed, else pypdf if importable).
Standard library only; pdftotext and pypdf are optional.

A PDF, or a long text with paper headings (Abstract, Methods, References, ...), is a "paper": its
Methods / protocol section is moved to the front and the rest follows, so the extractor reads the
steps first. Text is capped at MAX_CHARS.
"""
from __future__ import annotations

import csv
import io
import json
import re
import shutil
import subprocess
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

MAX_CHARS = 120_000

METHODS_MARK = "[METHODS SECTION, moved to the front]"
REST_MARK = "[REST OF THE PAPER]"

# Section headings in papers (lower case, after stripping numbering like "2." or "2.1").
METHODS_HEADINGS = {
    "methods", "method", "materials and methods", "material and methods", "methods and materials",
    "experimental", "experimental procedures", "experimental section", "experimental methods",
    "online methods", "star methods", "star★methods", "method details", "protocol", "procedure",
    "procedures", "detailed protocol", "step-by-step protocol", "methods summary",
}
END_HEADINGS = {
    "results", "results and discussion", "discussion", "conclusion", "conclusions", "references",
    "bibliography", "acknowledgements", "acknowledgments", "acknowledgement", "acknowledgment",
    "author contributions", "data availability", "code availability", "funding", "competing interests",
    "conflict of interest", "conflicts of interest", "declaration of interests", "supplementary information",
    "supplementary material", "supplementary materials", "figure legends", "figures", "tables",
}
PAPER_HEADINGS = {"abstract", "introduction", "background", "results", "discussion", "references",
                  "conclusion", "conclusions", "acknowledgements", "acknowledgments", "keywords"} | METHODS_HEADINGS

_HEADING_LINE = re.compile(r"^\s*(?:#{1,6}\s*)?(?:\d+(?:\.\d+)*\.?\s+)?([A-Za-z][A-Za-z★&/ ,-]{1,60}?)\s*:?\s*$")


class IngestError(ValueError):
    """The source could not be read (missing file, unknown format, no text)."""


# ---------------------------------------------------------------------------------------------- public
def load_source(input_path: str | None, prompt: str | None) -> dict:
    """Read a file and/or a prompt into the source dict that every later stage reads."""
    prompt = (prompt or "").strip() or None
    if not input_path:
        if not prompt:
            raise IngestError("give an input file or a prompt")
        return {"kind": "prompt", "title": _prompt_title(prompt), "file": None, "text": prompt,
                "prompt": prompt, "chars": len(prompt), "methods_chars": 0}

    p = Path(input_path)
    if not p.exists():
        raise IngestError(f"no such file: {p}")
    if p.is_dir():
        raise IngestError(f"{p} is a folder; give a protocol or paper file")
    suffix = p.suffix.lower()
    reader = _READERS.get(suffix, _read_text)
    text, title = reader(p)
    text = _clean(text, pdf=suffix == ".pdf")
    if not text.strip():
        hint = " (a scanned PDF? run OCR first)" if suffix == ".pdf" else ""
        raise IngestError(f"no text found in {p}{hint}")

    kind = "paper" if suffix == ".pdf" or _looks_like_paper(text) else "protocol"
    methods_chars = 0
    if kind == "paper":
        title = title or _paper_title(text)
        text, methods_chars = _methods_first(text)
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + "\n[... truncated]"
    title = title or _guess_title(text) or p.stem
    return {"kind": kind, "title": title, "file": str(p), "text": text, "prompt": prompt,
            "chars": len(text), "methods_chars": methods_chars}


# ---------------------------------------------------------------------------------------------- readers
def _read_text(p: Path) -> tuple[str, str | None]:
    raw = p.read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16"), None
    try:
        return raw.decode("utf-8-sig"), None
    except UnicodeDecodeError:
        return raw.decode("latin-1"), None


def _read_json(p: Path) -> tuple[str, str | None]:
    try:
        obj = json.loads(_read_text(p)[0])
    except json.JSONDecodeError as e:
        raise IngestError(f"{p} is not valid JSON: {e}") from e
    if isinstance(obj, dict) and isinstance(obj.get("steps"), list) and obj["steps"] and isinstance(obj["steps"][0], dict):
        return _render_protocol_json(obj), obj.get("name") or obj.get("id")
    title = (obj.get("name") or obj.get("title")) if isinstance(obj, dict) else None
    return json.dumps(obj, indent=2), title


def _render_protocol_json(obj: dict) -> str:
    """A protocol.schema.json protocol (or a protocol IR) as numbered steps the extractor can read."""
    lines = [f"# {obj.get('name') or obj.get('id') or 'Protocol'}", ""]
    src = obj.get("source")
    if isinstance(src, str) and src:
        lines += [f"Source: {src}", ""]
    elif isinstance(src, dict) and (src.get("citation") or src.get("title")):
        lines += [f"Source: {src.get('citation') or src.get('title')}", ""]
    if obj.get("description") or obj.get("goal"):
        lines += [str(obj.get("description") or obj.get("goal")), ""]
    for i, s in enumerate(obj["steps"], 1):
        n = s.get("step") or s.get("n") or i
        text = s.get("note") or s.get("text") or s.get("operation") or s.get("action") or "step"
        extra = []
        op = s.get("operation") or s.get("action")
        if op:
            extra.append(f"operation: {op}")
        reagents = [s["reagent"]] if s.get("reagent") else list(s.get("reagents") or [])
        if reagents:
            extra.append("reagent: " + ", ".join(str(r).replace("_", " ") for r in reagents))
        if s.get("volume_ul") is not None:
            extra.append(f"{s['volume_ul']} µL")
        if s.get("duration_s"):
            extra.append(f"{s['duration_s']} s")
        if s.get("temp_c") is not None:
            extra.append(f"{s['temp_c']} °C")
        if s.get("washes"):
            extra.append(f"{s['washes']} washes")
        if s.get("repeats"):
            extra.append(f"{s['repeats']} times")
        if s.get("flow_ul_min"):
            extra.append(f"{s['flow_ul_min']} µL/min")
        if s.get("wavelength_nm"):
            extra.append(f"{s['wavelength_nm']} nm")
        tgt = s.get("target") or ", ".join(s.get("targets") or [])
        if tgt:
            extra.append(f"target: {tgt}")
        lines.append(f"{n}. {str(text).rstrip('.')}." + (f" ({'; '.join(extra)})" if extra else ""))
    return "\n".join(lines) + "\n"


def _table_text(rows: list[list[str]], title: str | None = None) -> str:
    """Rows as a markdown pipe table (first non-empty row is the header)."""
    rows = [[(c or "").replace("|", "/").replace("\n", " ").strip() for c in r] for r in rows]
    rows = [r for r in rows if any(r)]
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    keep = [j for j in range(width) if any(r[j] for r in rows)]   # drop columns empty everywhere
    rows = [[r[j] for j in keep] for r in rows]
    out = [f"## {title}", ""] if title else []
    out.append("| " + " | ".join(rows[0]) + " |")
    out.append("|" + "|".join(" --- " for _ in rows[0]) + "|")
    out += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(out) + "\n"


def _read_csv(p: Path) -> tuple[str, str | None]:
    text, _ = _read_text(p)
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel_tab if p.suffix.lower() == ".tsv" else csv.excel
    rows = list(csv.reader(io.StringIO(text), dialect))
    return _table_text(rows), p.stem.replace("_", " ")


_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _docx_numbering(z: zipfile.ZipFile) -> dict[tuple[str, str], str]:
    """(numId, ilvl) -> numFmt ('bullet', 'decimal', ...) from word/numbering.xml."""
    try:
        root = ET.fromstring(z.read("word/numbering.xml"))
    except KeyError:
        return {}
    abstract = {}
    for an in root.iter(_W + "abstractNum"):
        aid = an.get(_W + "abstractNumId")
        for lvl in an.iter(_W + "lvl"):
            fmt = lvl.find(_W + "numFmt")
            abstract[(aid, lvl.get(_W + "ilvl"))] = fmt.get(_W + "val") if fmt is not None else "decimal"
    out = {}
    for num in root.iter(_W + "num"):
        a = num.find(_W + "abstractNumId")
        if a is None:
            continue
        for (aid, ilvl), fmt in abstract.items():
            if aid == a.get(_W + "val"):
                out[(num.get(_W + "numId"), ilvl)] = fmt
    return out


def _docx_runs(el) -> str:
    parts = []
    for node in el.iter():
        if node.tag == _W + "t" and node.text:
            parts.append(node.text)
        elif node.tag == _W + "tab":
            parts.append(" ")
        elif node.tag in (_W + "br", _W + "cr"):
            parts.append("\n")
    return "".join(parts)


def _read_docx(p: Path) -> tuple[str, str | None]:
    try:
        z = zipfile.ZipFile(p)
        root = ET.fromstring(z.read("word/document.xml"))
    except (zipfile.BadZipFile, KeyError, ET.ParseError) as e:
        raise IngestError(f"{p} is not a readable .docx: {e}") from e
    numfmt = _docx_numbering(z)
    title = None
    try:
        t = ET.fromstring(z.read("docProps/core.xml")).find("{http://purl.org/dc/elements/1.1/}title")
        if t is not None and (t.text or "").strip():
            title = t.text.strip()
    except (KeyError, ET.ParseError):
        pass
    body = root.find(_W + "body")
    lines: list[str] = []
    counters: dict[str, int] = {}
    for el in list(body) if body is not None else []:
        if el.tag == _W + "tbl":
            rows = [[_docx_runs(tc).strip() for tc in tr.findall(_W + "tc")] for tr in el.iter(_W + "tr")]
            lines += ["", _table_text(rows), ""]
            continue
        if el.tag != _W + "p":
            continue
        text = _docx_runs(el).strip()
        if not text:
            lines.append("")
            continue
        ppr = el.find(_W + "pPr")
        style, num = "", None
        if ppr is not None:
            ps = ppr.find(_W + "pStyle")
            style = (ps.get(_W + "val") or "") if ps is not None else ""
            npr = ppr.find(_W + "numPr")
            if npr is not None:
                nid, ilvl = npr.find(_W + "numId"), npr.find(_W + "ilvl")
                num = (nid.get(_W + "val") if nid is not None else "0", ilvl.get(_W + "val") if ilvl is not None else "0")
        m = re.match(r"(?i)heading\s*(\d)", style)
        if style.lower() == "title":
            title = title or text
            lines += ["", f"# {text}", ""]
        elif m:
            lines += ["", "#" * min(6, int(m.group(1)) + 1) + f" {text}", ""]
        elif num and num[0] != "0":
            if numfmt.get(num, "decimal") == "bullet" or num[1] != "0":
                lines.append(("  " * int(num[1])) + f"- {text}")
            else:
                counters[num[0]] = counters.get(num[0], 0) + 1
                lines.append(f"{counters[num[0]]}. {text}")
        elif re.match(r"(?i)list\s*number", style):          # numbering that comes from the paragraph style
            counters[style] = counters.get(style, 0) + 1
            lines.append(f"{counters[style]}. {text}")
        elif re.match(r"(?i)list\s*(?:bullet|paragraph)", style):
            lines.append(f"- {text}")
        else:
            lines.append(text)
    return "\n".join(lines), title


_X = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


def _col_index(ref: str) -> int:
    n = 0
    for ch in re.match(r"[A-Z]+", ref).group(0):
        n = n * 26 + ord(ch) - 64
    return n - 1


def _read_xlsx(p: Path) -> tuple[str, str | None]:
    try:
        z = zipfile.ZipFile(p)
        wb = ET.fromstring(z.read("xl/workbook.xml"))
    except (zipfile.BadZipFile, KeyError, ET.ParseError) as e:
        raise IngestError(f"{p} is not a readable .xlsx: {e}") from e
    names = set(z.namelist())
    shared: list[str] = []
    if "xl/sharedStrings.xml" in names:
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iter(_X + "si"):
            shared.append("".join(t.text or "" for t in si.iter(_X + "t")))
    rels = {}
    if "xl/_rels/workbook.xml.rels" in names:
        for r in ET.fromstring(z.read("xl/_rels/workbook.xml.rels")):
            rels[r.get("Id")] = r.get("Target")
    out = []
    for i, sh in enumerate(wb.iter(_X + "sheet"), 1):
        target = rels.get(sh.get(_R + "id"), f"worksheets/sheet{i}.xml").lstrip("/")
        path = target if target.startswith("xl/") else "xl/" + target
        if path not in names:
            continue
        rows = []
        for row in ET.fromstring(z.read(path)).iter(_X + "row"):
            cells: dict[int, str] = {}
            for j, c in enumerate(row.findall(_X + "c")):
                ref = c.get("r")
                t = c.get("t")
                v = c.find(_X + "v")
                if t == "s" and v is not None and v.text:
                    k = int(v.text)
                    val = shared[k] if k < len(shared) else ""
                elif t == "inlineStr":
                    val = "".join(x.text or "" for x in c.iter(_X + "t"))
                elif t == "b" and v is not None:
                    val = "TRUE" if v.text == "1" else "FALSE"
                else:
                    val = v.text if v is not None and v.text else ""
                    if re.fullmatch(r"-?\d+\.0+", val):
                        val = val.split(".")[0]
                cells[_col_index(ref) if ref else j] = val
            if cells:
                rows.append([cells.get(k, "") for k in range(max(cells) + 1)])
        if rows:
            out.append(_table_text(rows, title=f"Sheet: {sh.get('name')}"))
    return "\n".join(out), p.stem.replace("_", " ")


def _two_column(text: str) -> bool:
    """True when pdftotext -layout put two text columns side by side (wide gaps mid-line)."""
    lines = [ln for ln in text.splitlines() if len(ln.strip()) > 30]
    if len(lines) < 20:
        return False
    gapped = sum(1 for ln in lines if re.search(r"\S {4,}\S", ln.strip()) and len(ln) > 70)
    return gapped / len(lines) > 0.4


def _read_pdf(p: Path) -> tuple[str, str | None]:
    if shutil.which("pdftotext"):
        try:
            r = subprocess.run(["pdftotext", "-layout", "-enc", "UTF-8", str(p), "-"],
                               capture_output=True, text=True, timeout=180)
            if r.returncode == 0 and r.stdout.strip():
                if not _two_column(r.stdout):
                    return r.stdout, None
                # Two-column layout: the default (reading-order) mode reads each column in turn.
                r2 = subprocess.run(["pdftotext", "-enc", "UTF-8", str(p), "-"], capture_output=True, text=True, timeout=180)
                return (r2.stdout if r2.returncode == 0 and r2.stdout.strip() else r.stdout), None
        except (OSError, subprocess.TimeoutExpired):
            pass
    try:
        import pypdf  # optional
    except ImportError:
        raise IngestError(f"can't read {p.name}: install poppler-utils (pdftotext) or `pip install pypdf`") from None
    try:
        reader = pypdf.PdfReader(str(p))
        text = "\n\n".join(page.extract_text() or "" for page in reader.pages)
        meta = reader.metadata
        title = meta.title if meta and meta.title else None
    except Exception as e:  # pypdf raises many kinds of errors on broken files
        raise IngestError(f"can't read {p.name}: {e}") from e
    return text, title


_READERS = {".md": _read_text, ".markdown": _read_text, ".txt": _read_text, ".json": _read_json,
            ".csv": _read_csv, ".tsv": _read_csv, ".docx": _read_docx, ".xlsx": _read_xlsx, ".xlsm": _read_xlsx,
            ".pdf": _read_pdf}


# ---------------------------------------------------------------------------------------------- text helpers
_LIGATURES = {"ﬁ": "fi", "ﬂ": "fl", "ﬀ": "ff", "ﬃ": "ffi", "ﬄ": "ffl", "­": ""}


def _clean(text: str, pdf: bool = False) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x0c", "\n\n")
    for a, b in _LIGATURES.items():
        text = text.replace(a, b)
    lines = [ln.rstrip() for ln in text.split("\n")]
    if pdf:
        # pdftotext -layout indents and justifies: drop the margin, squeeze runs of spaces.
        margin = min((len(ln) - len(ln.lstrip()) for ln in lines if ln.strip()), default=0)
        lines = [re.sub(r"(?<=\S) {2,}(?=\S)", " ", ln[margin:]) for ln in lines]
        text = "\n".join(lines)
        text = re.sub(r"([a-z])-\n\s*([a-z])", r"\1\2", text)   # re-join hyphenated line breaks
    else:
        text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n"


def heading_of(line: str) -> str | None:
    """The heading name (lower case) if the line looks like a section heading, else None."""
    if len(line.strip()) > 70:
        return None
    m = _HEADING_LINE.match(line)
    if not m:
        return None
    return re.sub(r"\s+", " ", m.group(1)).strip(" :").lower()


def _looks_like_paper(text: str) -> bool:
    if len(text) < 4000:
        return False
    heads = {h for h in (heading_of(ln) for ln in text.splitlines()) if h in PAPER_HEADINGS}
    return len(heads) >= 3 or ("abstract" in heads and ("references" in heads or bool(heads & METHODS_HEADINGS)))


def _methods_first(text: str) -> tuple[str, int]:
    """Move the Methods section to the front. Returns (text, length of the methods block or 0)."""
    lines = text.split("\n")
    heads = [heading_of(ln) for ln in lines]
    best = None
    for s, h in enumerate(heads):
        if h not in METHODS_HEADINGS:
            continue
        e = next((i for i in range(s + 1, len(lines)) if heads[i] in END_HEADINGS), len(lines))
        if best is None or (e - s) > (best[1] - best[0]):
            best = (s, e)
    if best is None or best[1] - best[0] < 3:
        return text, 0
    s, e = best
    methods = "\n".join(lines[s:e]).strip()
    before = "\n".join(lines[:s]).strip()
    after_lines = lines[e:]
    block = f"{METHODS_MARK}\n{methods[:MAX_CHARS - 2000]}\n\n"
    rest = f"{REST_MARK}\n{before}\n\n" + "\n".join(after_lines).strip() + "\n"
    if len(block) + len(rest) > MAX_CHARS:
        # References are the first thing to go when the text is too long.
        ref = next((i for i, h in enumerate(heads[e:]) if h in ("references", "bibliography")), None)
        if ref is not None:
            rest = f"{REST_MARK}\n{before}\n\n" + "\n".join(after_lines[:ref]).strip() + "\n[references omitted]\n"
    return block + rest, len(block)


def _guess_title(text: str) -> str | None:
    for ln in text.splitlines():
        m = re.match(r"^\s*#{1,3}\s+(.+?)\s*#*\s*$", ln)
        if m:
            return m.group(1).strip()
    for ln in text.splitlines():
        s = ln.strip().strip("|").strip()
        if s and not s.startswith("[") and len(s) <= 120:
            return s
    return None


def _paper_title(text: str) -> str | None:
    """First substantial line of a paper; joins a title that wraps onto a second line."""
    lines = [ln.strip() for ln in text.splitlines()]
    for i, ln in enumerate(lines):
        if len(ln) < 8 or heading_of(ln) in PAPER_HEADINGS or re.match(r"(?i)^(doi|https?:|vol\.|page|received)", ln):
            continue
        title = ln
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if nxt and len(title) > 40 and not title.endswith(".") and len(nxt) < 80 and nxt[:1].islower():
            title += " " + nxt
        return title[:200]
    return None


def _prompt_title(prompt: str) -> str:
    first = re.split(r"(?<=[.!?])\s|\n", prompt.strip(), maxsplit=1)[0].strip()
    return first if len(first) <= 80 else first[:77].rsplit(" ", 1)[0] + "..."
