"""Turn the LaTeX ChatGPT emits into markup each exporter can render.

A shared conversation writes maths as Markdown-embedded LaTeX: `$...$` (or
`\\(...\\)`) inline and `$$...$$` (or `\\[...\\]`) on its own lines. Every
exporter used to pass those delimiters through untouched, so the reader saw
either raw `\\frac{a}{b}` text or — worse — a Word document where the
Markdown renderer had eaten the `\\` escapes, `_` subscripts and `*`
operators, leaving something that no longer resembled the formula.

One tokenizer feeds three targets:

* HTML (PDF + dashboard): MathML. Chromium ships the MathML core the PDF is
  printed with, and the browser renders the same markup live.
* DOCX: OMML, Word's native equation format, written into the run XML.
* Markdown: left alone — `$...$` is the convention those files are read with.
"""

from __future__ import annotations

import html as html_mod
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass

# Display delimiters first: they span lines, and a block that starts with `$$`
# must not be mistaken for an inline `$`. An inline formula never spans a line
# break — otherwise an unclosed `\(` would swallow the rest of the message.
DISPLAY = re.compile(r"\$\$(.+?)\$\$|\\\[(.+?)\\\]", re.S)
# `$5` is a price, not an opening delimiter. Following Pandoc: the opening `$`
# must touch the formula and the closing one must not be preceded by a space,
# nor be the start of another amount ("$5 or $6" is money twice, not maths).
INLINE = re.compile(
    r"(?<![\\$])\$(?![\s$])([^$\n]*?[^\s\\$])\$(?![\d$])|"
    r"\\\(([^\\\n]*?(?:\\[^()\n][^\\\n]*?)*)\\\)",
    re.S,
)
# A fenced block is literal text: `$5` inside one is a price, not a formula,
# and rewriting it would corrupt the code the reader is trying to copy. The
# group also covers a fence that is never closed, which runs to the end.
FENCE = re.compile(r"^[ \t]*```.*?(?:^[ \t]*```[ \t]*$|\Z)", re.S | re.M)

MML_NS = "http://www.w3.org/1998/Math/MathML"
OMML_NS = "http://schemas.openxmlformats.org/officeDocument/2006/math"


@dataclass
class Segment:
    kind: str  # "text" | "math"
    text: str
    display: bool = False


def _split(pattern: re.Pattern[str], text: str, display: bool) -> list[Segment]:
    out: list[Segment] = []
    pos = 0
    for match in pattern.finditer(text):
        if match.start() > pos:
            out.append(Segment("text", text[pos : match.start()]))
        body = match.group(1) or match.group(2) or ""
        out.append(Segment("math", body.strip(), display))
        pos = match.end()
    if pos < len(text):
        out.append(Segment("text", text[pos:]))
    return out


def _math_in(text: str) -> list[Segment]:
    out: list[Segment] = []
    for part in _split(DISPLAY, text, True):
        if part.kind == "math":
            out.append(part)
        else:
            out.extend(_split(INLINE, part.text, False))
    return out


def segments(text: str) -> list[Segment]:
    """Split Markdown into text and maths, display blocks before inline ones."""
    text = text or ""
    out: list[Segment] = []
    pos = 0
    for fence in FENCE.finditer(text):
        if fence.start() > pos:
            out.extend(_math_in(text[pos : fence.start()]))
        out.append(Segment("text", fence.group(0)))
        pos = fence.end()
    if pos < len(text):
        out.extend(_math_in(text[pos:]))
    return out


def has_math(text: str) -> bool:
    return any(s.kind == "math" for s in segments(text))


# Elements worth keeping. `\text{<script>…}` passes through latex2mathml
# verbatim, and a child without a prefix inherits the MathML namespace — so
# checking the namespace is not enough, the name has to be on this list too.
_MML_TAGS = {
    "math", "mrow", "mi", "mn", "mo", "mtext", "ms", "mspace", "mfrac",
    "msqrt", "mroot", "mstyle", "merror", "mpadded", "mphantom", "mfenced",
    "menclose", "munder", "mover", "munderover", "mmultiscripts",
    "mprescripts", "none", "msub", "msup", "msubsup", "mtable", "mtr", "mtd",
    "mlabeledtr", "maction", "semantics", "annotation", "annotation-xml",
    "mglyph",
}

# Attributes worth keeping. Everything else — `href`, `xlink:href`, any `on*`
# handler, and whatever else a LaTeX command smuggles into the output — is
# dropped.
_SAFE_ATTRS = {
    "display", "displaystyle", "scriptlevel", "mathvariant", "mathsize",
    "stretchy", "fence", "separator", "lspace", "rspace", "largeop",
    "movablelimits", "symmetric", "minsize", "maxsize", "form", "notation",
    "columnalign", "rowalign", "columnspan", "rowspan", "framespacing",
    "rowlines", "columnlines", "linethickness", "width", "height", "depth",
    "accent", "accentunder", "selection",
}


def _emit(el: ET.Element) -> str:
    tag = el.tag
    if not isinstance(tag, str) or not tag.startswith("{" + MML_NS + "}"):
        # `\text{<script>}` really does come back as an element. It is not
        # MathML, so it is removed rather than re-serialised.
        return ""
    name = tag.split("}", 1)[1]
    if name not in _MML_TAGS:
        return ""
    attrs = "".join(
        f' {k}="{html_mod.escape(v, quote=True)}"'
        for k, v in el.attrib.items()
        if k in _SAFE_ATTRS
    )
    inner = html_mod.escape(el.text) if el.text else ""
    for child in el:
        inner += _emit(child)
        if child.tail:
            inner += html_mod.escape(child.tail)
    return f"<{name}{attrs}>{inner}</{name}>"


def mathml(latex: str, display: bool = False) -> str:
    """Convert one formula to sanitised MathML, or "" when it cannot.

    latex2mathml chokes on renders its table does not cover; a failed
    conversion must degrade to the formula's source rather than break the
    whole document, so the caller falls back to escaped text.
    """
    try:
        import latex2mathml.converter
    except ImportError:  # pragma: no cover - optional dependency
        return ""
    try:
        raw = latex2mathml.converter.convert(
            latex, display="block" if display else "inline"
        )
        root = ET.fromstring(raw)
    except Exception:  # noqa: BLE001 - any parser failure is a fallback, not a crash
        return ""
    if root.tag != f"{{{MML_NS}}}math":
        return ""
    body = "".join(
        _emit(child) + (html_mod.escape(child.tail) if child.tail else "")
        for child in root
    )
    if not body:
        return ""
    display_attr = ' display="block"' if display else ""
    return f'<math xmlns="{MML_NS}"{display_attr}>{body}</math>'


# Placeholders carry the private-use characters U+E000/U+E001, which no
# conversation contains and which the Markdown renderer copies through
# untouched — so a formula keeps the paragraph, list item or table cell it was
# written in, and the maths is dropped in only after the text around it has
# been rendered.
#
# Markdown turns `[x](javascript:\u2026)` into a real `href`, and the dashboard
# shows the rendered HTML with `innerHTML` while Chromium prints the same markup
# into the PDF. A shared conversation is somebody else's text, so a scheme that
# can run code is neutralised here rather than trusted from the source.
_URL_ATTR = re.compile(r'\b(href|src)="([^"]*)"', re.I)
_ENTITY = re.compile(r"&(?:#[xX][0-9a-fA-F]+|#\d+|[a-zA-Z][a-zA-Z0-9]*);")
# Transports that point somewhere other than a web page. A relative link and a
# plain `https:`/`http:`/`mailto:` stay; a wrapped one is unwrapped so what the
# browser executes is what was checked.
_SCHEME = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.\-]*):")
_ALLOWED = {"http", "https", "mailto"}
_IMAGE = re.compile(r"^data:image/(?:png|jpe?g|gif|webp|bmp);", re.I)
# The characters a browser drops before it parses a URL.
_STRIP = re.compile(r"[\x00-\x20\x7f]+")


def _clean_url(value: str, is_image: bool = False) -> str:
    # Entities are decoded repeatedly: `&amp;#x6a;avascript:` reaches the
    # browser as `&#x6a;avascript:`, which it then decodes for itself. A
    # control character does the same job (`java&#9;script:`).
    plain = value
    for _ in range(4):
        step = _ENTITY.sub(lambda m: html_mod.unescape(m.group(0)), plain)
        if step == plain:
            break
        plain = step
    plain = _STRIP.sub("", plain)
    scheme = _SCHEME.match(plain)
    if not scheme:
        return plain  # relative, or a bare `//host/path`
    name = scheme.group(1).lower()
    if name == "data":
        # An embedded picture is what a conversation may legitimately carry;
        # `data:text/html` is a page, and that is not an image.
        return plain if is_image and _IMAGE.match(plain) else ""
    return plain if name in _ALLOWED else ""


def _safe_urls(html: str) -> str:
    def fix(match: re.Match[str]) -> str:
        value = _clean_url(match.group(2), match.group(1).lower() == "src")
        return f'{match.group(1)}="{html_mod.escape(value, quote=True)}"'

    return _URL_ATTR.sub(fix, html)


PH_OPEN, PH_CLOSE = "\ue000", "\ue001"
_PH = re.compile(re.escape(PH_OPEN) + r"(\d+)" + re.escape(PH_CLOSE))
_PRE = re.compile(r"<pre\b.*?</pre>", re.S | re.I)


def _swap(html: str, rendered: list[str]) -> str:
    """Replace placeholders, leaving anything inside a code block alone."""
    out: list[str] = []
    pos = 0
    for pre in _PRE.finditer(html):
        out.append(_PH.sub(lambda m: rendered[int(m.group(1))], html[pos : pre.start()]))
        out.append(pre.group(0))
        pos = pre.end()
    out.append(_PH.sub(lambda m: rendered[int(m.group(1))], html[pos:]))
    return "".join(out)


def render_markdown(text: str) -> str:
    """The Markdown → HTML step both HTML targets share."""
    import markdown as md

    return md.markdown(
        text, extensions=["fenced_code", "tables", "sane_lists", "nl2br"]
    )


def to_html(text: str, render=None) -> str:
    """Interleave rendered text and MathML for the HTML exporters.

    `render` turns Markdown into HTML. The text is escaped before Markdown
    sees it: a shared conversation is somebody else's content, and letting
    `<img onerror=…>` or `<script>` through would make the dashboard preview
    and the printed PDF execute it.
    """
    render = render or render_markdown
    rendered: list[str] = []
    parts: list[str] = []
    for seg in segments(text):
        if seg.kind == "text":
            # A fence is Markdown's own literal block: it escapes what is
            # inside it, so escaping here as well would make the reader see
            # `&amp;amp;` in code they are trying to copy.
            parts.append(
                seg.text if FENCE.fullmatch(seg.text)
                else html_mod.escape(seg.text, quote=False)
            )
            continue
        xml = mathml(seg.text, seg.display)
        if not xml:
            escaped = html_mod.escape(seg.text, quote=False)
            xml = (f"<p class=tex>{escaped}</p>" if seg.display
                   else f"<code>{escaped}</code>")
        parts.append(f"{PH_OPEN}{len(rendered)}{PH_CLOSE}")
        rendered.append(xml)
    if not rendered:
        return _safe_urls(render("".join(parts)))
    return _safe_urls(_swap(render("".join(parts)), rendered))


# mathml2omml mis-closes `<m:groupChrPr>` (it writes `</m:groupChr>`), which
# makes the whole fragment unparseable — and `\underbrace` is exactly how a
# limit argument gets labelled. The property element never nests, so closing
# on the next `</m:groupChr>` is unambiguous.
_GROUPCHR = re.compile(r"(<m:groupChrPr>(?:(?!</m:groupChr).)*?)</m:groupChr>", re.S)


def _repair(xml: str) -> str:
    return _GROUPCHR.sub(r"\1</m:groupChrPr>", xml)


def _well_formed(xml: str) -> bool:
    try:
        ET.fromstring(xml.replace("<m:oMath>", f'<m:oMath xmlns:m="{OMML_NS}">', 1))
    except ET.ParseError:
        return False
    return True


def omml(latex: str, display: bool = False) -> str:
    """Word's native equation markup for one formula, or "" when unsupported."""
    try:
        import mathml2omml
    except ImportError:  # pragma: no cover - optional dependency
        return ""
    xml = mathml(latex, display)
    if not xml:
        return ""
    try:
        # mathml2omml writes `ns0:` prefixes, which Word accepts inside a
        # run's document.xml — python-docx only needs well-formed XML.
        out = _repair(mathml2omml.convert(xml))
    except Exception:  # noqa: BLE001
        return ""
    # A fragment that does not parse would make the whole document unopenable.
    # Better one formula in readable plain text than a corrupt file.
    return out if _well_formed(out) else ""


# A formula Word cannot take is still worth reading. This is not LaTeX: it
# flattens fractions to `(a)/(b)`, spells scripts as `_x` / `^2` and swaps the
# common commands for their symbols, so the line keeps its mathematical meaning
# instead of showing backslashes and braces.
_SYMBOLS = [
    (r"\\leq(?![a-zA-Z])|\\le(?![a-zA-Z])", "≤"),
    (r"\\geq(?![a-zA-Z])|\\ge(?![a-zA-Z])", "≥"),
    (r"\\neq(?![a-zA-Z])|\\ne(?![a-zA-Z])", "≠"),
    (r"\\times\b", "×"), (r"\\cdot\b", "·"), (r"\\to\b", "→"),
    (r"\\infty\b", "∞"), (r"\\pm\b", "±"), (r"\\approx\b", "≈"),
    (r"\\in\b", "∈"), (r"\\sum\b", "Σ"), (r"\\int\b", "∫"),
    (r"\\partial\b", "∂"), (r"\\Delta\b", "Δ"), (r"\\delta\b", "δ"),
    (r"\\alpha\b", "α"), (r"\\beta\b", "β"), (r"\\gamma\b", "γ"),
    (r"\\pi\b", "π"), (r"\\theta\b", "θ"), (r"\\lambda\b", "λ"),
    (r"\\lim\b", "lim"), (r"\\sin\b", "sin"), (r"\\cos\b", "cos"),
    (r"\\tan\b", "tan"), (r"\\log\b", "log"), (r"\\ln\b", "ln"),
]


def _group(latex: str, i: int) -> tuple[str, int]:
    """Read the `{...}` (or single token) starting at `i`; return it, next index."""
    while i < len(latex) and latex[i].isspace():
        i += 1
    if i >= len(latex):
        return "", i
    if latex[i] == "{":
        depth = 0
        for j in range(i, len(latex)):
            if latex[j] == "{":
                depth += 1
            elif latex[j] == "}":
                depth -= 1
                if depth == 0:
                    return latex[i + 1 : j], j + 1
        return latex[i + 1 :], len(latex)
    return latex[i], i + 1


def _unwrap(token: str) -> str:
    return token[1:-1] if token.startswith("{") and token.endswith("}") else token


def _pretty_atom(group: str) -> str:
    text = _pretty(group)
    # A multi-character script needs parentheses, or `x^{2+1}` would read as
    # `x` to the power of `2+1` and `x^{10}` as `x^1` followed by `0`.
    return f"({text})" if len(text) > 1 else text


def _script(match: str, body: str) -> str:
    marker = "_" if "_" in match[: match.index("{")] else "^"
    # `\lim_{x\to a}` is the approach condition, not a subscript on `lim`;
    # writing `lim _(x→a)` reads as if the underscore were part of it.
    if marker == "_" and "→" in body:
        return f"({_pretty(body)})"
    return marker + _pretty_atom(body)


def _pretty(latex: str) -> str:
    s = " ".join(latex.split())
    s = s.replace("\\\\", " ")
    # Fractions and roots nest, so unwrap them innermost-first.
    while re.search(r"\\frac\s*\{", s):
        s = re.sub(
            r"\\frac\s*(\{[^{}]*\}|[^{}\s])\s*(\{[^{}]*\}|[^{}\s])",
            lambda m: f"({_unwrap(m.group(1))})/({_unwrap(m.group(2))})", s)
    s = re.sub(r"\\sqrt\s*(\{[^{}]*\}|[^{}\s])", lambda m: f"√({_unwrap(m.group(1))})", s)
    s = re.sub(r"\\(?:boxed|text|mathrm|mathbf)\s*\{([^{}]*)\}", r"\1", s)
    s = re.sub(r"\\(?:left|right|big|Big)\b", "", s)
    for pattern, symbol in _SYMBOLS:
        s = re.sub(pattern, symbol, s)
    # Scripts: `_{...}` / `^{...}` keep their content, braces dropped, so a
    # reader still sees which part the script applies to.
    s = re.sub(r"\\(lim|sin|cos|tan|log|ln)\s*[_^]\s*\{([^{}]*)\}",
               lambda m: m.group(1) + " " + _script(m.group(0), m.group(2)), s)
    # `x^{10}` has to read as a power of ten, not as `x^1` followed by `0`.
    s = re.sub(r"[_^]\s*\{([^{}]*)\}", lambda m: _script(m.group(0), m.group(1)), s)
    s = s.replace("{", "").replace("}", "")
    return re.sub(r"\s+", " ", s).strip()
