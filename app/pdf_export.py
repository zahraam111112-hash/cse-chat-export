"""Render transcripts to PDF by printing the same HTML to Chromium.

The Word renderer has to describe formatting to python-docx. A PDF does not:
the extractor already produces Markdown, `markdown` turns that into HTML, and
Chromium — which is installed anyway for extraction — prints it. One
stylesheet, one layout engine, and Persian text gets the RTL direction it
needs without per-run font juggling.
"""

from __future__ import annotations

import html as html_mod
import re

import markdown as md

from . import browser
from . import math as math_mod
from .extractor import Transcript

CSS = """
@page { size: A4; margin: 18mm 15mm; }
* { box-sizing: border-box; }
body { font-family: Tahoma, "Segoe UI", Arial, sans-serif; font-size: 10.5pt;
       line-height: 1.55; color: #1a1a1a; margin: 0; }
h1 { font-size: 20pt; color: #1f3864; margin: 0 0 2mm; }
.sub { color: #666; font-size: 9pt; margin: 0 0 6mm; }
.conv { page-break-after: always; }
.conv:last-child { page-break-after: auto; }
.url { color: #777; font-size: 8.5pt; margin: 0 0 5mm; word-break: break-all; }
.msg { margin: 0 0 5mm; }
.who { font-size: 9pt; font-weight: 700; margin-bottom: 1.5mm; }
.user .who { color: #0b4f9e; }
.assistant .who { color: #1e6b34; }
.content { border-left: 2px solid #d7dde8; padding-left: 4mm; }
.assistant .content { border-left-color: #cfe3d4; }
.content > :first-child { margin-top: 0; }
.content > :last-child { margin-bottom: 0; }
.rtl { direction: rtl; text-align: right; }
.rtl .content { border-left: none; border-right: 2px solid #d7dde8; padding-left: 0; padding-right: 4mm; }
.rtl.assistant .content { border-right-color: #cfe3d4; }
pre { background: #f4f4f4; padding: 2.5mm 3mm; border-radius: 2px; overflow-x: auto;
      font-family: Consolas, monospace; font-size: 8pt; white-space: pre-wrap;
      word-break: break-word; }
code { font-family: Consolas, monospace; font-size: 8.5pt; color: #8b1a1a; }
pre code { color: inherit; font-size: inherit; }
table { border-collapse: collapse; width: 100%; margin: 2mm 0; font-size: 9pt; }
th, td { border: 1px solid #b9c2d0; padding: 1.5mm 2mm; text-align: start; }
th { background: #1f3864; color: #fff; }
blockquote { margin: 2mm 0; padding: 1mm 3mm; border-left: 3px solid #8eaadb; color: #444; }
img { max-width: 100%; }
/* Chromium ships MathML core; the display block needs its own centring
   because MathML does not inherit the paragraph's RTL alignment. */
math { font-size: 11pt; }
math[display="block"] { display: block; margin: 2mm auto; }
.tex { direction: ltr; text-align: left; font-family: Consolas, monospace; font-size: 9pt; }
"""

RTL_CHAR = re.compile(r"[؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]")


def _body(transcript: Transcript) -> str:
    parts = []
    for n, message in enumerate(transcript.messages, 1):
        who = "User" if message.role == "user" else "ChatGPT"
        css = "user" if message.role == "user" else "assistant"
        rtl = " rtl" if RTL_CHAR.search(message.content) else ""
        rendered = math_mod.to_html(
            message.content,
            lambda chunk: md.markdown(
                chunk, extensions=["fenced_code", "tables", "sane_lists", "nl2br"]
            ),
        )
        parts.append(
            f'<div class="msg {css}{rtl}">'
            f'<div class="who">{who} · message {n}</div>'
            f'<div class="content">{rendered}</div></div>'
        )
    return "\n".join(parts)


def build_html(transcripts: list[Transcript]) -> str:
    total = sum(len(t.messages) for t in transcripts)
    chars = sum(t.chars for t in transcripts)
    sections = []
    for transcript in transcripts:
        rtl = " rtl" if RTL_CHAR.search(transcript.title) else ""
        sections.append(
            f'<section class="conv{rtl}">'
            f'<h1>{html_mod.escape(transcript.title)}</h1>'
            f'<p class="url">{html_mod.escape(transcript.url)} · '
            f'{len(transcript.messages)} messages</p>'
            f'{_body(transcript)}</section>'
        )
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        f"<style>{CSS}</style></head><body>"
        f"<h1>ChatGPT Shared Conversations</h1>"
        f'<p class="sub">{len(transcripts)} conversation(s) · {total} messages · '
        f"{chars:,} characters</p>"
        + "".join(sections)
        + "</body></html>"
    )


def build(transcripts: list[Transcript], proxy: str | None = None) -> bytes:
    """Print the rendered HTML to PDF with the same Chromium extraction uses."""
    return browser.print_pdf(build_html(transcripts), proxy)
