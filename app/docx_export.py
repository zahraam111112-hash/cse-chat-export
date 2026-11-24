"""Render one or more transcripts into a single .docx (RTL-aware)."""

from __future__ import annotations

import io
import re

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor

from docx.oxml import parse_xml

from . import math as math_mod
from .extractor import Transcript

# The namespace prefix `m:` has to be declared wherever an OMML fragment is
# parsed on its own; python-docx otherwise rejects the undeclared prefix.
NSDECL = (
    'xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math" '
    'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
)

FA_FONT = "Tahoma"
CODE_FONT = "Consolas"

INLINE = re.compile(r"(\*\*.+?\*\*|`[^`]+`|\*[^*\n]+?\*|\[[^\]]+\]\([^)]+\))")
RTL_CHAR = re.compile(r"[؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]")
HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
BULLET = re.compile(r"^\s*[-*+]\s+(.*)$")
ORDERED = re.compile(r"^\s*(\d+)[.)]\s+(.*)$")
FENCE = re.compile(r"^\s*```")


def _set_rtl(paragraph) -> None:
    pPr = paragraph._p.get_or_add_pPr()
    bidi = OxmlElement("w:bidi")
    bidi.set(qn("w:val"), "1")
    pPr.append(bidi)


def _font(run, name=FA_FONT, size=None, bold=None, italic=None, color=None) -> None:
    run.font.name = name
    rPr = run._element.get_or_add_rPr()
    rf = rPr.find(qn("w:rFonts"))
    if rf is None:
        rf = OxmlElement("w:rFonts")
        rPr.append(rf)
    for attr in ("w:ascii", "w:hAnsi", "w:cs"):
        rf.set(qn(attr), name)
    if size:
        run.font.size = Pt(size)
    if bold is not None:
        run.bold = bold
    if italic is not None:
        run.italic = italic
    if color:
        run.font.color.rgb = color


def _shade(paragraph, hex_fill: str) -> None:
    pPr = paragraph._p.get_or_add_pPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), hex_fill)
    pPr.append(shd)


def _inline(paragraph, text: str, size: float = 10.5, bold: bool = False, font: str = FA_FONT,
            color=None) -> None:
    if "\\" in text or "$" in text:
        if math_mod.has_math(text):
            for seg in math_mod.segments(text):
                if seg.kind == "text":
                    _inline_plain(paragraph, seg.text, size, bold, font, color)
                else:
                    _math(paragraph, seg.text, seg.display, size, font)
            return
    _inline_plain(paragraph, text, size, bold, font, color)


def _math(paragraph, latex: str, display: bool, size: float, font: str) -> None:
    xml = math_mod.omml(latex, display)
    if not xml:
        _font(paragraph.add_run(math_mod._pretty(latex)), font, size, bold=False, italic=True)
        return
    # OMML travels as a sibling of w:r inside the paragraph; append it to the
    # same parent so the equation sits where the formula appeared.
    node = parse_xml(
        f'<m:oMathPara {NSDECL}><m:oMathParaPr><m:jc m:val="center"/></m:oMathParaPr>'
        f"{xml}</m:oMathPara>"
    ) if display else parse_xml(f"<w:r {NSDECL}>{xml}</w:r>")
    paragraph._p.append(node)


def _inline_plain(paragraph, text: str, size: float = 10.5, bold: bool = False, font: str = FA_FONT,
                  color=None) -> None:
    for token in INLINE.split(text):
        if not token:
            continue
        if token.startswith("**") and token.endswith("**") and len(token) > 4:
            _font(paragraph.add_run(token[2:-2]), font, size, True, color=color)
        elif token.startswith("`") and token.endswith("`") and len(token) > 2:
            run = paragraph.add_run(token[1:-1])
            _font(run, CODE_FONT, max(size - 2.5, 6), False)
            run.font.color.rgb = RGBColor(0x8B, 0x1A, 0x1A)
        elif token.startswith("[") and "](" in token:
            run = paragraph.add_run(token[1 : token.index("]")] + " ")
            _font(run, font, size, bold)
            run.font.color.rgb = RGBColor(0x0B, 0x4F, 0x9E)
        elif token.startswith("*") and token.endswith("*") and len(token) > 2:
            _font(paragraph.add_run(token[1:-1]), font, size, bold, True, color=color)
        else:
            _font(paragraph.add_run(token), font, size, bold, color=color)


def _table(doc: Document, rows: list[list[str]]) -> None:
    if not rows:
        return
    header = rows[0]
    body = rows[1:]
    # The separator is `---`-style cells between header and rows. A row whose
    # cells are all blank is data, not a separator: `all()` over the empty
    # generator is True, so it must be excluded explicitly.
    if body and any(c.strip() for c in body[0]) and all(
            re.fullmatch(r":?-{2,}:?", c.strip()) for c in body[0] if c.strip()):
        body = body[1:]
    ncol = max([len(header)] + [len(r) for r in body] or [0])
    if ncol == 0:
        return
    table = doc.add_table(rows=1, cols=ncol)
    table.style = "Table Grid"
    for i in range(ncol):
        cell = table.rows[0].cells[i]
        cell.text = ""
        p = cell.paragraphs[0]
        p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        _set_rtl(p)
        _inline(p, (header[i] if i < len(header) else "").strip(), 9, True)
        for run in p.runs:
            run.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
        shd = OxmlElement("w:shd")
        shd.set(qn("w:val"), "clear")
        shd.set(qn("w:color"), "auto")
        shd.set(qn("w:fill"), "1F3864")
        cell._tc.get_or_add_tcPr().append(shd)
    for n, row in enumerate(body):
        cells = table.add_row().cells
        for i in range(ncol):
            cell = cells[i]
            cell.text = ""
            p = cell.paragraphs[0]
            p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
            _set_rtl(p)
            _inline(p, (row[i] if i < len(row) else "").strip(), 9)
    doc.add_paragraph()


def _display_block(lines: list[str], i: int) -> tuple[str, int] | None:
    """Read a display formula starting at line `i`; return (latex, next index).

    The delimiters are `\\[...\\]`, `$$...$$`, or a fence line holding both.
    Returns None when line `i` does not open a display formula.
    """
    line = lines[i].strip()
    if not (line.startswith("\\[") or line.startswith("$$")):
        return None
    opener = line[:2]
    closer = "\\]" if opener == "\\[" else "$$"
    body = line[2:]
    if closer in body:
        return body[: body.index(closer)].strip(), i + 1
    parts = [body] if body.strip() else []
    for j in range(i + 1, len(lines)):
        if closer in lines[j]:
            parts.append(lines[j][: lines[j].index(closer)])
            return "\n".join(parts).strip(), j + 1
        parts.append(lines[j])
    # Never closed: leave the lines to the text renderer rather than eat the
    # rest of the message into a formula that was not there.
    return None


def _expand(lines: list[str]) -> list[str | tuple[str, ...]]:
    """Fold each display formula into one item so the renderer sees it whole.

    Without this a `\\[` line and its `\\]` line reach the inline renderer
    separately, and the formula comes out as backslashes and braces — the bug
    this module exists to fix. Fenced code is left verbatim.
    """
    out: list[str | tuple[str, ...]] = []
    i = 0
    fenced = False
    while i < len(lines):
        if FENCE.match(lines[i]):
            fenced = not fenced
            out.append(lines[i])
            i += 1
            continue
        if not fenced:
            block = _display_block(lines, i)
            if block is not None:
                out.append(("display", block[0]))
                i = block[1]
                continue
        out.append(lines[i])
        i += 1
    return out


def _display(paragraph, latex: str, size: float, font: str) -> None:
    xml = math_mod.omml(latex, True)
    if not xml:
        _font(paragraph.add_run(math_mod._pretty(latex)), font, size, bold=False, italic=True)
        return
    paragraph._p.append(parse_xml(
        f'<m:oMathPara {NSDECL}><m:oMathParaPr><m:jc m:val="center"/></m:oMathParaPr>'
        f"{xml}</m:oMathPara>"
    ))


def render_markdown(doc: Document, markdown: str) -> None:
    lines = _expand((markdown or "").split("\n"))
    table_rows: list[list[str]] = []
    code: list[str] = []
    in_code = False

    for line in lines:
        if isinstance(line, tuple):
            if table_rows:
                _table(doc, table_rows)
                table_rows = []
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            p.paragraph_format.space_before = Pt(4)
            p.paragraph_format.space_after = Pt(4)
            _display(p, line[1], 11, FA_FONT)
            continue
        if FENCE.match(line):
            if in_code:
                for code_line in code:
                    p = doc.add_paragraph()
                    p.alignment = WD_ALIGN_PARAGRAPH.LEFT
                    _shade(p, "F4F4F4")
                    _font(p.add_run(code_line or " "), CODE_FONT, 8, color=RGBColor(0x1B, 0x1B, 0x1B))
                    p.paragraph_format.space_after = Pt(0)
                    p.paragraph_format.space_before = Pt(0)
                doc.add_paragraph()
                code = []
                in_code = False
            else:
                if table_rows:
                    _table(doc, table_rows)
                    table_rows = []
                in_code = True
            continue
        if in_code:
            code.append(line)
            continue
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            table_rows.append([c for c in stripped.strip("|").split("|")])
            continue
        if table_rows:
            _table(doc, table_rows)
            table_rows = []
        if not stripped:
            continue

        match = HEADING.match(stripped)
        if match:
            level = len(match.group(1))
            text = re.sub(r"\*\*|`", "", match.group(2).strip())
            p = doc.add_paragraph()
            rtl = bool(RTL_CHAR.search(text))
            _set_rtl(p) if rtl else None
            p.alignment = WD_ALIGN_PARAGRAPH.RIGHT if rtl else WD_ALIGN_PARAGRAPH.LEFT
            # A heading can carry a formula (`### ۱. برای \(x>x_0\)`), so it
            # goes through the same inline renderer the body does.
            _inline(p, text, {1: 17, 2: 14, 3: 12, 4: 11, 5: 10.5, 6: 10}.get(level, 10.5),
                    True, FA_FONT, color=RGBColor(0x1F, 0x38, 0x64))
            p.paragraph_format.space_before = Pt(12 if level <= 2 else 8)
            p.paragraph_format.space_after = Pt(4)
            p.paragraph_format.keep_with_next = True
            continue

        match = BULLET.match(line)
        if match:
            p = doc.add_paragraph(style="List Bullet")
            _set_rtl(p)
            p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
            _inline(p, match.group(1), 10.5)
            p.paragraph_format.space_after = Pt(2)
            continue

        match = ORDERED.match(line)
        if match:
            p = doc.add_paragraph(style="List Number")
            _set_rtl(p)
            p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
            _inline(p, match.group(2), 10.5)
            p.paragraph_format.space_after = Pt(2)
            continue

        if re.fullmatch(r"[-—=]{3,}", stripped):
            p = doc.add_paragraph()
            pPr = p._p.get_or_add_pPr()
            pbdr = OxmlElement("w:pBdr")
            bottom = OxmlElement("w:bottom")
            bottom.set(qn("w:val"), "single")
            bottom.set(qn("w:sz"), "6")
            bottom.set(qn("w:space"), "1")
            bottom.set(qn("w:color"), "AAAAAA")
            pbdr.append(bottom)
            pPr.append(pbdr)
            continue

        if stripped.startswith(">"):
            p = doc.add_paragraph()
            _set_rtl(p)
            p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
            p.paragraph_format.left_indent = Inches(0.3)
            p.paragraph_format.right_indent = Inches(0.3)
            pPr = p._p.get_or_add_pPr()
            pbdr = OxmlElement("w:pBdr")
            left = OxmlElement("w:left")
            left.set(qn("w:val"), "single")
            left.set(qn("w:sz"), "18")
            left.set(qn("w:space"), "8")
            left.set(qn("w:color"), "8EAADB")
            pbdr.append(left)
            pPr.append(pbdr)
            _inline(p, stripped.lstrip()[1:].strip(), 10.5)
            continue

        p = doc.add_paragraph()
        rtl = bool(RTL_CHAR.search(stripped))
        _set_rtl(p) if rtl else None
        p.alignment = WD_ALIGN_PARAGRAPH.RIGHT if rtl else WD_ALIGN_PARAGRAPH.LEFT
        _inline(p, stripped, 10.5)
        p.paragraph_format.space_after = Pt(4)
        p.paragraph_format.line_spacing = 1.15

    if table_rows:
        _table(doc, table_rows)
    if code:
        for code_line in code:
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.LEFT
            _shade(p, "F4F4F4")
            _font(p.add_run(code_line or " "), CODE_FONT, 8)


def build(transcripts: list[Transcript]) -> bytes:
    doc = Document()
    section = doc.sections[0]
    section.page_width = Inches(8.27)
    section.page_height = Inches(11.69)
    for attr in ("top_margin", "bottom_margin", "left_margin", "right_margin"):
        setattr(section, attr, Inches(0.7))

    normal = doc.styles["Normal"]
    normal.font.name = FA_FONT
    normal.font.size = Pt(10.5)
    rf = normal.element.rPr.rFonts
    for attr in ("w:ascii", "w:hAnsi", "w:cs"):
        rf.set(qn(attr), FA_FONT)

    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _font(p.add_run("ChatGPT Shared Conversations"), FA_FONT, 24, True, color=RGBColor(0x1F, 0x38, 0x64))
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    total = sum(len(t.messages) for t in transcripts)
    _font(p.add_run(f"{len(transcripts)} conversation(s) · {total} messages · "
                    f"{sum(t.chars for t in transcripts):,} characters"),
          FA_FONT, 11, color=RGBColor(0x55, 0x55, 0x55))
    if any(RTL_CHAR.search(t.title) for t in transcripts):
        _set_rtl(p)
    doc.add_paragraph()

    if len(transcripts) > 1:
        _table(doc, [["#", "Title", "Messages", "Chars", "Source"]] +
               [[str(i), t.title, str(len(t.messages)), f"{t.chars:,}", t.url]
                for i, t in enumerate(transcripts, 1)])

    for idx, transcript in enumerate(transcripts, 1):
        if idx > 1:
            doc.add_page_break()
        p = doc.add_paragraph()
        title_rtl = bool(RTL_CHAR.search(transcript.title))
        if title_rtl:
            _set_rtl(p)
        p.alignment = WD_ALIGN_PARAGRAPH.RIGHT if title_rtl else WD_ALIGN_PARAGRAPH.LEFT
        _font(p.add_run(transcript.title), FA_FONT, 19, True, color=RGBColor(0x1F, 0x38, 0x64))
        p = doc.add_paragraph()
        _font(p.add_run(f"{transcript.url} · {len(transcript.messages)} messages"),
              FA_FONT, 8.5, color=RGBColor(0x77, 0x77, 0x77))
        doc.add_paragraph()
        for n, message in enumerate(transcript.messages, 1):
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.LEFT
            who = "User" if message.role == "user" else "ChatGPT"
            colour = RGBColor(0x0B, 0x4F, 0x9E) if message.role == "user" else RGBColor(0x1E, 0x6B, 0x34)
            _font(p.add_run(f"{who} · message {n}"), FA_FONT, 9.5, True, color=colour)
            p.paragraph_format.space_before = Pt(10)
            p.paragraph_format.keep_with_next = True
            render_markdown(doc, message.content)
            doc.add_paragraph()

    buffer = io.BytesIO()
    doc.save(buffer)
    return buffer.getvalue()
