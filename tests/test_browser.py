"""The browser layer: which engine is picked, and what it refuses to fetch.

None of these start a browser. The parts worth pinning down are the ones that
cost a user money or break silently: the executable that is chosen, the two
backends' precedence, and the rule that a share page never downloads its
pictures, fonts or video.
"""

import json

import pytest

from app import browser


# --- which engine -----------------------------------------------------------

def test_cse_browser_wins_over_the_installed_one(tmp_path, monkeypatch):
    fake = tmp_path / "chrome.exe"
    fake.write_bytes(b"")
    monkeypatch.setenv("CSE_BROWSER", str(fake))
    assert browser.find_browser() == str(fake)


def test_a_wrong_cse_browser_is_an_error_not_a_fallback(tmp_path, monkeypatch):
    """Someone who names an engine meant that engine; silently using another
    would send a debugging session after the wrong binary."""
    monkeypatch.setenv("CSE_BROWSER", str(tmp_path / "gone.exe"))
    with pytest.raises(browser.BrowserMissing, match="CSE_BROWSER"):
        browser.find_browser()


def test_no_browser_at_all_says_what_to_install(tmp_path, monkeypatch):
    monkeypatch.delenv("CSE_BROWSER", raising=False)
    monkeypatch.setattr(browser, "_WINDOWS", [])
    monkeypatch.setattr(browser, "_POSIX", [])
    monkeypatch.setattr(browser.sys, "platform", "win32")
    monkeypatch.setattr(browser.os.path, "dirname",
                        lambda p: str(tmp_path / "app") if p.endswith("browser.py")
                        else "")
    with pytest.raises(browser.BrowserMissing) as caught:
        browser.find_browser()
    assert "Edge" in str(caught.value)


def test_playwright_is_used_when_it_is_installed(monkeypatch):
    calls = []
    monkeypatch.setattr(browser, "_have_playwright", lambda: True)
    monkeypatch.setattr(browser, "_render_playwright",
                        lambda *a: calls.append("pw") or ("html", "title"))
    assert browser.render("https://example.test/") == ("html", "title")
    assert calls == ["pw"]


def test_without_playwright_the_installed_browser_is_driven(monkeypatch):
    calls = []
    monkeypatch.setattr(browser, "_have_playwright", lambda: False)
    monkeypatch.setattr(browser, "_render_cdp",
                        lambda *a: calls.append("cdp") or ("html", "title"))
    assert browser.render("https://example.test/") == ("html", "title")
    assert calls == ["cdp"]


def test_no_playwright_can_be_forced_off(monkeypatch):
    """`CSE_NO_PLAYWRIGHT=1` is how a developer reproduces the release here."""
    monkeypatch.setenv("CSE_NO_PLAYWRIGHT", "1")
    assert browser._have_playwright() is False


# --- what a share page is allowed to download -------------------------------

class _Recorder:
    def __init__(self):
        self.sent = []

    def send(self, raw):
        self.sent.append(json.loads(raw))

    def recv(self, timeout=None):  # pragma: no cover - must not be called
        raise AssertionError("answering a paused request must not wait for a reply")


def _session():
    ws = _Recorder()
    return browser._Session(ws), ws


def _pause(session, kind, rid="r1"):
    session._handle({"method": "Fetch.requestPaused",
                     "sessionId": "S1",
                     "params": {"requestId": rid, "resourceType": kind}})


@pytest.mark.parametrize("kind", ["Image", "Font", "Media"])
def test_media_is_refused(kind):
    session, ws = _session()
    _pause(session, kind)
    session._flush()
    (message,) = ws.sent
    assert message["method"] == "Fetch.failRequest"
    assert message["params"]["errorReason"] == "BlockedByClient"
    assert message["sessionId"] == "S1"


@pytest.mark.parametrize("kind", ["Document", "Script", "XHR", "Stylesheet"])
def test_everything_else_goes_through(kind):
    session, ws = _session()
    _pause(session, kind)
    session._flush()
    (message,) = ws.sent
    assert message["method"] == "Fetch.continueRequest"
    assert message["params"]["requestId"] == "r1"


def test_paused_requests_are_answered_without_waiting_for_the_reply():
    """Answering inline recursed once per request and blew the stack on a real
    share page, before its document had even arrived."""
    session, ws = _session()
    for i in range(500):
        _pause(session, "Image", rid=f"r{i}")
    session._flush()
    assert len(ws.sent) == 500
    assert [m["params"]["requestId"] for m in ws.sent[:2]] == ["r0", "r1"]


def test_a_paused_request_without_an_id_is_dropped():
    session, ws = _session()
    session._handle({"method": "Fetch.requestPaused", "params": {"resourceType": "Image"}})
    session._flush()
    assert ws.sent == []


# --- the PDF printer's paper -------------------------------------------------

def test_the_footer_carries_the_page_numbers():
    """The app's name and `pageNumber`/`totalPages` are the only reason
    `displayHeaderFooter` is on; Chromium fills those spans itself."""
    assert "ChatGPT Share Exporter" in browser.FOOTER
    assert "pageNumber" in browser.FOOTER and "totalPages" in browser.FOOTER


def test_the_page_is_a4_with_the_stylesheets_margins():
    assert round(browser.PAPER_W, 2) == 8.27
    assert round(browser.PAPER_H, 2) == 11.69
    assert round(browser.MARGIN_TB * 25.4) == 18
    assert round(browser.MARGIN_LR * 25.4) == 15
