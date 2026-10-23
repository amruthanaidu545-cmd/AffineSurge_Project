"""PDF -> hierarchical tree for the CT-200 manual.

Approach (see APPROACH.md for the full reasoning):
  * The PDF has a real text layer, so we read positioned, styled spans with PyMuPDF
    instead of OCR. OCR (PyMuPDF + Tesseract) is attempted only for pages with no
    text layer; if that is unavailable the page is reported as unsupported.
  * A line is a heading only if ALL of: numbered ("2.1", "2.1.1.1"), bold, at the
    left margin. Font size is NOT used to decide (3.2 is styled as body-size bold);
    size is only used afterwards to *report* style deviations.
  * Level = depth of the section number. Parent = longest existing numeric prefix,
    NOT the previous heading's level, so "2.1.1.1" attaches to "2.1" (level skip)
    and "3.4"-before-"3.3" keeps document order but still lands under "3".
  * Tables come from PyMuPDF's table finder (cell geometry); cell text is re-read
    from the page so wrapped cells and punctuation survive.
  * validate() proves nothing was silently lost or duplicated by comparing the
    token multiset of the raw PDF text with the token multiset of the tree.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field

import pymupdf

NUM_RE = re.compile(r"^(\d+(?:\.\d+)*)\.?\s+(\S.*)$")
LIST_RE = re.compile(r"^(\d+)\.\s+(\S.*)$")
REF_RE = re.compile(
    r"\b(?:[Ss]ections?|see)\s+(\d+(?:\.\d+)*(?:\s*(?:,|and)\s*\d+(?:\.\d+)*)*)"
)
TOKEN_RE = re.compile(r"[A-Za-z0-9]+")
BODY_SIZE = 11.0


def normalize(s: str) -> str:
    """Ligatures (fi, fl, ff), non-breaking hyphen (U+2011), NBSP, runs of spaces."""
    s = s.replace("‑", "-")
    s = unicodedata.normalize("NFKC", s)
    s = s.replace("‐", "-").replace(" ", " ")
    return re.sub(r"[ \t]+", " ", s).strip()


def content_hash(body: str) -> str:
    canon = re.sub(r"\s+", " ", normalize(body)).strip()
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def num_tuple(number: str) -> tuple[int, ...]:
    return tuple(int(p) for p in number.split("."))


@dataclass
class Line:
    page: int
    x: float
    y: float
    text: str
    size: float
    bold: bool


@dataclass
class ParsedNode:
    index: int
    number: str | None
    heading: str
    level: int
    ordinal: int
    parent: int | None
    page: int
    size: float
    path: str = ""
    blocks: list[dict] = field(default_factory=list)
    body: str = ""
    content_hash: str = ""
    references: list[str] = field(default_factory=list)


@dataclass
class ParsedDocument:
    title: str
    nodes: list[ParsedNode]
    warnings: list[dict]
    page_count: int

    def by_number(self, number: str) -> list[ParsedNode]:
        return [n for n in self.nodes if n.number == number]


# ----------------------------------------------------------------- extraction
def _page_dict(page, pno: int, warnings: list[dict]) -> dict:
    d = page.get_text("dict")
    has_text = any(b["type"] == 0 and b["lines"] for b in d["blocks"])
    if has_text:
        return d
    try:  # scanned page -> OCR (needs Tesseract installed)
        tp = page.get_textpage_ocr(full=True)
        return page.get_text("dict", textpage=tp)
    except Exception as exc:  # noqa: BLE001 - surface, never guess
        warnings.append(
            {"code": "unsupported_page", "severity": "error", "page": pno,
             "message": f"page has no text layer and OCR is unavailable ({exc}); content NOT extracted"}
        )
        return {"blocks": []}


def _extract_page(page, pno: int, warnings: list[dict]):
    """Return (lines, tables) for a page; table lines are excluded from lines."""
    tables = []
    rects = []
    try:
        for t in page.find_tables().tables:
            rows = []
            for row in t.rows:
                cells = []
                for c in row.cells:
                    if c is None:
                        cells.append("")
                        continue
                    txt = page.get_text("text", clip=pymupdf.Rect(c))
                    cells.append(normalize(txt.replace("\n", " ")))
                rows.append(cells)
            rect = pymupdf.Rect(t.bbox)
            rects.append(rect)
            tables.append({"page": pno, "y": rect.y0, "rows": rows})
    except Exception as exc:  # noqa: BLE001
        warnings.append({"code": "table_detection_failed", "severity": "warning",
                         "page": pno, "message": str(exc)})

    lines: list[Line] = []
    for b in _page_dict(page, pno, warnings)["blocks"]:
        if b["type"] == 1:
            warnings.append(
                {"code": "unsupported_image", "severity": "warning", "page": pno,
                 "message": "image block found; figures/captions are not extracted"})
            continue
        for l in b["lines"]:
            spans = [s for s in l["spans"] if s["text"].strip()]
            if not spans:
                continue
            cx = (l["bbox"][0] + l["bbox"][2]) / 2
            cy = (l["bbox"][1] + l["bbox"][3]) / 2
            if any(r.contains(pymupdf.Point(cx, cy)) for r in rects):
                continue
            text = normalize("".join(s["text"] for s in l["spans"]))
            bold = all((s["flags"] & 16) or "bold" in s["font"].lower() for s in spans)
            lines.append(Line(pno, l["bbox"][0], l["bbox"][1], text,
                              max(s["size"] for s in spans), bool(bold)))
    lines.sort(key=lambda l: (round(l.y), l.x))
    return lines, tables


# ------------------------------------------------------------------- building
class _Builder:
    """Accumulates blocks (paragraph / list / table) for the current node."""

    def __init__(self):
        self.blocks: list[dict] = []
        self.para: list[str] = []
        self.last: Line | None = None
        self.list_x: float | None = None

    def flush_para(self):
        if self.para:
            self.blocks.append({"type": "paragraph", "text": " ".join(self.para)})
        self.para, self.last = [], None

    def end_list(self):
        self.list_x = None

    def add_line(self, ln: Line):
        in_list = bool(self.blocks and self.blocks[-1]["type"] == "list" and self.list_x is not None)
        m = LIST_RE.match(ln.text)
        if not ln.bold and m and ln.x > 40:  # indented numbered line -> list item
            self.flush_para()
            if not in_list:
                self.blocks.append({"type": "list", "items": []})
            self.blocks[-1]["items"].append(f"{m.group(1)}. {m.group(2)}")
            self.list_x = ln.x
            return
        if in_list and ln.x >= self.list_x + 6:  # indented continuation of last item
            self.blocks[-1]["items"][-1] += " " + ln.text
            return
        self.end_list()
        gap_break = (self.last is not None and self.last.page == ln.page
                     and ln.y - self.last.y > 24)
        if gap_break:
            self.flush_para()
        self.para.append(ln.text)  # page-break continuation joins with a space
        self.last = ln

    def add_table(self, rows):
        self.flush_para()
        self.end_list()
        self.blocks.append({"type": "table", "rows": rows})

    def finish(self):
        self.flush_para()
        return self.blocks


def render_body(blocks: list[dict]) -> str:
    out = []
    for b in blocks:
        if b["type"] == "paragraph":
            out.append(b["text"])
        elif b["type"] == "list":
            out.append("\n".join(b["items"]))
        elif b["type"] == "table":
            rows = b["rows"]
            lines = ["| " + " | ".join(rows[0]) + " |",
                     "|" + "---|" * len(rows[0])]
            lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
            out.append("\n".join(lines))
    return "\n\n".join(out)


def _is_heading(ln: Line, margin: float) -> re.Match | None:
    m = NUM_RE.match(ln.text)
    if m and ln.bold and ln.x <= margin + 4 and ln.size >= 10.5:
        return m
    return None


def parse_pdf(path: str) -> ParsedDocument:
    doc = pymupdf.open(path)
    warnings: list[dict] = []
    elements: list[tuple] = []  # ("line", Line) | ("table", dict)
    for pno, page in enumerate(doc, start=1):
        lines, tables = _extract_page(page, pno, warnings)
        elements += [(l.y + pno * 10_000, "line", l) for l in lines]
        elements += [(t["y"] + pno * 10_000, "table", t) for t in tables]
    elements.sort(key=lambda e: e[0])
    lines_only = [e[2] for e in elements if e[1] == "line"]
    if not lines_only:
        raise ValueError("no extractable text in PDF")
    margin = min(l.x for l in lines_only)

    # Title = leading large bold lines (joined: it wraps over 3 lines)
    title_parts, i = [], 0
    while i < len(elements) and elements[i][1] == "line" and elements[i][2].size >= 20:
        title_parts.append(elements[i][2].text)
        i += 1
    title = " ".join(title_parts) or "Untitled"

    nodes: list[ParsedNode] = []
    root = ParsedNode(0, None, title, 0, 0, None, 1, 22.0)
    nodes.append(root)
    builders = {0: _Builder()}
    by_number: dict[str, ParsedNode] = {}
    cur = root
    open_heading = False  # last line was a heading: allow wrapped continuation

    for _, kind, el in elements[i:]:
        if kind == "table":
            builders[cur.index].add_table(el["rows"])
            open_heading = False
            continue
        ln: Line = el
        m = _is_heading(ln, margin)
        if m:
            number, heading = m.group(1), m.group(2).strip()
            depth = len(number.split("."))
            prefix_parent = None
            parts = number.split(".")
            for k in range(len(parts) - 1, 0, -1):
                cand = ".".join(parts[:k])
                if cand in by_number:
                    prefix_parent = by_number[cand]
                    break
            if number in by_number:
                warnings.append({"code": "duplicate_number", "severity": "error", "number": number,
                                 "message": f"section number {number} appears twice; kept as distinct nodes"})
            if prefix_parent is None:
                if depth > 1:
                    warnings.append({"code": "orphan_numbering", "severity": "error", "number": number,
                                     "message": f"{number} has no ancestor section; attached to document root"})
                parent = root
            else:
                parent = prefix_parent
                if parent.level != depth - 1:
                    warnings.append({"code": "level_skip", "severity": "warning", "number": number,
                                     "message": f"{number} (level {depth}) attached to {parent.number} "
                                                f"(level {parent.level}): intermediate level(s) missing in the document"})
            sibs = [n for n in nodes if n.parent == parent.index]
            if sibs and sibs[-1].number and num_tuple(number) < num_tuple(sibs[-1].number):
                warnings.append({"code": "out_of_order", "severity": "warning", "number": number,
                                 "message": f"{number} appears after {sibs[-1].number}; document order preserved"})
            node = ParsedNode(len(nodes), number, heading, depth, len(nodes), parent.index,
                              ln.page, ln.size)
            nodes.append(node)
            builders[node.index] = _Builder()
            by_number.setdefault(number, node)
            cur = node
            open_heading = True
            continue
        # wrapped heading continuation (bold, same size, not numbered)
        if (open_heading and cur.index != 0 and ln.bold and abs(ln.size - cur.size) < 0.2
                and not NUM_RE.match(ln.text)):
            cur.heading += " " + ln.text
            continue
        open_heading = False
        builders[cur.index].add_line(ln)

    for n in nodes:
        n.blocks = builders[n.index].finish()
        n.body = render_body(n.blocks)
        n.content_hash = content_hash(n.body)
        refs = []
        for rm in REF_RE.finditer(n.body):
            refs += re.findall(r"\d+(?:\.\d+)*", rm.group(1))
        n.references = sorted({r for r in refs if r != n.number}, key=num_tuple)
        anc, p = [], n
        while p is not None:
            if p.number:
                anc.append(p.number)
            p = nodes[p.parent] if p.parent is not None else None
        n.path = "/".join(reversed(anc))

    # report heading-style deviations vs. peers of the same depth
    sizes: dict[int, Counter] = {}
    for n in nodes[1:]:
        sizes.setdefault(n.level, Counter())[round(n.size, 1)] += 1
    for n in nodes[1:]:
        typical = sizes[n.level].most_common(1)[0][0]
        if round(n.size, 1) != typical or (n.size <= BODY_SIZE + 0.1):
            warnings.append({"code": "heading_style_deviation", "severity": "warning", "number": n.number,
                             "message": f"heading {n.number} is {n.size:.1f}pt; peers at level {n.level} "
                                        f"use {typical}pt. Accepted because it is numbered+bold at margin."})
    result = ParsedDocument(title, nodes, warnings, len(doc))
    warnings.extend(validate(result, path))
    return result


# ----------------------------------------------------------------- validation
def _tokens(s: str) -> Counter:
    return Counter(TOKEN_RE.findall(normalize(s)))


def validate(parsed: ParsedDocument, path: str) -> list[dict]:
    """Token-multiset equality between raw PDF text and the extracted tree.

    A clean-looking tree with lost/duplicated content fails here loudly.
    """
    raw = Counter()
    with pymupdf.open(path) as d:
        for p in d:
            raw += _tokens(p.get_text("text"))
    tree = _tokens(parsed.title)
    for n in parsed.nodes[1:]:
        tree += _tokens((n.number or "") + " " + n.heading + " " + n.body)
    out = []
    lost, extra = raw - tree, tree - raw
    if lost:
        out.append({"code": "content_loss", "severity": "error",
                    "message": f"tokens in PDF but not in tree: {dict(lost)}"})
    if extra:
        out.append({"code": "content_duplication", "severity": "error",
                    "message": f"tokens in tree but not in PDF: {dict(extra)}"})
    return out
