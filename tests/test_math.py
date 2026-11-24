"""Regressions for the LaTeX renderer.

A shared conversation writes formulas as `$...$` / `\\(...\\)` inline and
`$$...$$` / `\\[...\\]` as blocks. Both HTML targets must show them typeset
(MathML), Word must get OMML, Markdown must keep the source, and nothing in
somebody else's text may reach the page as markup or as a runnable link.
"""

import json

import pytest

from app import math as mm


def _math(segments):
    return [s.text for s in segments if s.kind == "math"]


# --- segmentation -----------------------------------------------------------

def test_display_blocks_are_found_before_inline_ones():
    segs = mm.segments("قبل\n$$\na^2+b^2=c^2\n$$\nبعد $x$")
    assert [s.display for s in segs if s.kind == "math"] == [True, False]
    assert _math(segs) == ["a^2+b^2=c^2", "x"]


def test_bracket_delimiters_are_display_too():
    segs = mm.segments("\\[\n\\frac{a}{b}\n\\] و \\(y\\)")
    maths = [s for s in segs if s.kind == "math"]
    assert [m.display for m in maths] == [True, False]
    assert maths[0].text == "\\frac{a}{b}"


def test_a_price_is_not_a_formula():
    """`$5` opens nothing: the earlier regexes ate half the sentence."""
    text = "قیمت $5 و $6 دلار است و $7,000 هم نه"
    assert mm.segments(text) == [mm.Segment("text", text)]


def test_a_placeholderless_formula_still_matches():
    segs = mm.segments("مساحت $$\\pi r^2$$ است")
    assert _math(segs) == ["\\pi r^2"]
    assert [s.display for s in segs if s.kind == "math"] == [True]


def test_fenced_code_is_left_alone():
    text = "```\nprice = $5\nx = \\frac{a}{b}\n```"
    assert mm.segments(text) == [mm.Segment("text", text)]
    assert not mm.has_math(text)


def test_an_unclosed_fence_runs_to_the_end():
    text = "نوشته\n```\n$x$"
    assert not mm.has_math(text)


def test_an_unclosed_inline_delimiter_does_not_swallow_the_message():
    text = "یک \\( نیمه‌کاره و بعدش چند خط\n\nمتن دیگر"
    assert not mm.has_math(text)


# --- MathML -----------------------------------------------------------------

def test_mathml_is_well_formed_and_namespaced():
    import xml.etree.ElementTree as ET

    xml = mm.mathml("\\frac{a}{b}")
    assert xml.startswith('<math xmlns="http://www.w3.org/1998/Math/MathML"')
    assert "<mfrac>" in xml and "<mi>a</mi>" in xml
    ET.fromstring(xml)  # the dashboard inserts this into the DOM
    assert 'display="block"' in mm.mathml("x", display=True)


def test_mathml_drops_markup_smuggled_through_text():
    """`\\text{<script>}` comes back as a real element in the MathML namespace.

    A namespace check alone accepts it, so the element name has to be on an
    allowlist as well.
    """
    xml = mm.mathml("\\text{<script>alert(1)</script>}")
    assert "<script" not in xml
    assert "onerror" not in mm.mathml("\\text{<img src=x onerror=alert(1)>}")


def test_mathml_falls_back_to_empty_when_the_converter_chokes():
    assert mm.mathml("\\thiscommanddoesnotexist{") == ""


# --- OMML -------------------------------------------------------------------

def test_omml_underbrace_is_repaired_into_parseable_xml():
    """mathml2omml mis-closes groupChrPr; unparseable XML breaks the .docx."""
    xml = mm.omml("\\underbrace{a+b}_{n}")
    assert xml, "underbrace should still convert"
    assert mm._well_formed(xml)


def test_omml_rejects_a_fragment_word_cannot_open():
    assert mm.omml("\\thiscommanddoesnotexist{") == ""


# --- the HTML pipeline ------------------------------------------------------

def test_to_html_escapes_raw_html():
    out = mm.to_html('<img src=x onerror="alert(1)">')
    assert "<img" not in out
    assert "&lt;img" in out


def test_to_html_keeps_code_blocks_literal():
    out = mm.to_html("```\n$a & b <c>\n```")
    assert "&lt;c&gt;" in out and "&lt;script&gt;" not in out
    assert "&#x26;" not in out  # escaped once, not twice


def test_to_html_substitutes_every_formula():
    out = mm.to_html("شکل $a^2$ و\n$$\nb^2\n$$")
    assert "<math" in out
    assert "<sup>" in out or "<msup>" in out
    assert mm.PH_OPEN not in out and mm.PH_CLOSE not in out
    assert "\\(" not in out and "$$" not in out


def test_to_html_marks_the_direction_of_an_html_fragment():
    # The PDF wraps each message in `.rtl` when it contains Persian.
    assert "نرخ" in mm.to_html("نرخ تغییر")


# --- links ------------------------------------------------------------------

@pytest.mark.parametrize("bad", [
    "[x](javascript:alert(1))",
    "[x](JaVaScRiPt:alert(1))",
    "[x](&#x6a;avascript:alert(1))",
    "[x](java&#9;script:alert(1))",
    "[x](vbscript:msgbox)",
    "[x](data:text/html,<script>)",
    "[x](file:///etc/passwd)",
])
def test_a_runnable_link_is_neutralised(bad):
    out = mm.to_html(bad)
    # The label survives, the destination does not: an empty href cannot run.
    assert 'href=""' in out
    assert "href=\"javascript" not in out.lower()


def test_a_normal_link_survives():
    out = mm.to_html("[x](https://e.example/a?b=1&c=2)")
    assert 'href="https://e.example/a?b=1&amp;c=2"' in out


def test_an_embedded_image_is_kept_but_a_page_is_not():
    assert 'src="data:image/png;base64,AA"' in mm.to_html("![i](data:image/png;base64,AA)")
    assert 'src=""' in mm.to_html("![i](data:text/html,<b>)")


# --- the plain-text fallback for Word ---------------------------------------

def test_pretty_flattens_fractions_and_symbols():
    text = mm._pretty("\\frac{a+b}{c} \\leq \\sqrt{x}")
    assert "(a+b)/(c)" in text
    assert "≤" in text and "√(x)" in text
    assert "\\" not in text


def test_pretty_keeps_a_multi_character_script_parenthesised():
    assert mm._pretty("x^{10}") == "x^(10)"
    assert mm._pretty("x^2") == "x^2"


def test_to_html_does_not_double_escape_a_fence():
    out = mm.to_html("```\na & b <c>\n```")
    assert "&amp;amp;" not in out
    assert "a &amp; b &lt;c&gt;" in out

# --- the dashboard endpoint -------------------------------------------------

def test_the_preview_endpoint_returns_the_same_render(tmp_path, monkeypatch):
    """The viewer asks for one message's HTML; it must be the PDF's renderer."""
    from fastapi.testclient import TestClient
    from app import auth, limits, main, store

    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "test.db"))
    store.init()
    limits.reset()
    monkeypatch.setattr(auth, "USER", "admin")

    job_id = store.create_job(["https://chatgpt.com/share/abc"], source="web", owner="t")
    content = "مشتق $f'(x)$ است\n\n$$\n\frac{dy}{dx}\n$$"
    store.update_job(job_id, status="done", transcripts=json.dumps(
        [{"url": "https://chatgpt.com/share/abc", "title": "T",
          "messages": [{"role": "assistant", "content": content}]}],
        ensure_ascii=False))

    client = TestClient(main.app)
    r = client.get(f"/api/jobs/{job_id}/html/0/0")
    assert r.status_code == 200
    assert "<math" in r.text and 'display="block"' in r.text
    assert "$" not in r.text
    assert client.get(f"/api/jobs/{job_id}/html/0/9").status_code == 404
    assert client.get(f"/api/jobs/{job_id}/html/4/0").status_code == 404
