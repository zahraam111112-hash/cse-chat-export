"""Regressions for the navigation race and the PDF export."""

import pytest
from fastapi.testclient import TestClient

from app import auth, limits, main, pdf_export, store
from app.extractor import Message, Transcript


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "test.db"))
    store.init()
    limits.reset()


@pytest.fixture
def client(monkeypatch):
    # No auth configured means every caller is the admin, which is what the
    # owner check would otherwise reject.
    monkeypatch.setattr(auth, "USER", "admin")
    return TestClient(main.app)


def _transcript():
    return Transcript(url="https://chatgpt.com/share/abc", title="T", messages=[
        Message("user", "چیست مشتق؟"),
        Message("assistant", "**مشتق** نرخ تغییر است.\n\n```py\n1 + 1\n```"),
    ])


# --- the navigation race ----------------------------------------------------

def test_a_navigation_in_flight_is_retried_not_raised():
    """`page.content()` during a re-navigation used to abort the whole job.

    The share page navigates again while hydrating, so the first reads raise.
    The reader must swallow those and keep going until two reads agree.
    """

    class Page:
        def __init__(self):
            self.reads = 0
            self.loads = 0
            self.waits = 0

        def wait_for_load_state(self, state, timeout):
            self.loads += 1

        def wait_for_timeout(self, ms):
            self.waits += 1

        def content(self):
            self.reads += 1
            if self.reads <= 3:
                raise RuntimeError("Unable to retrieve content because the page is navigating")
            return '<script>{"key":"conversation-state","value":["object",[]]}</script>'

    import time

    from app.extractor import STATE_KEY, _settled

    page = Page()
    html = _settled(page, deadline=time.time() + 10)
    assert STATE_KEY in html
    assert page.reads >= 4  # three failed reads, then one that has the state


def test_the_reader_gives_up_at_the_deadline_instead_of_hanging():
    class Page:
        def __init__(self):
            self.reads = 0

        def wait_for_load_state(self, state, timeout):
            pass

        def wait_for_timeout(self, ms):
            pass

        def content(self):
            self.reads += 1
            raise RuntimeError("Unable to retrieve content because the page is navigating")

    from app.extractor import _settled

    assert _settled(Page(), deadline=0) == ""


# --- PDF export -------------------------------------------------------------

def test_pdf_html_escapes_the_conversation_and_marks_rtl():
    html = pdf_export.build_html([_transcript()])
    assert "چیست مشتق؟" in html
    assert '<div class="content">' in html
    # Persian body text must carry the rtl class, or it prints left-aligned.
    assert "rtl" in html.split("<body>")[1]
    assert "<table" not in html  # nothing to render yet


def test_pdf_html_escapes_a_title_that_looks_like_markup():
    t = _transcript()
    t.title = '<img src=x onerror="alert(1)">'
    html = pdf_export.build_html([t])
    assert "<img" not in html
    assert "&lt;img" in html


def test_pdf_download_requires_the_job_to_be_finished(client):
    from app.main import store as _store

    job_id = _store.create_job(["https://chatgpt.com/share/abc"])
    assert client.get(f"/api/jobs/{job_id}/pdf").status_code == 404


def test_pdf_download_spends_the_submission_budget(client, monkeypatch):
    """Printing costs a Chromium launch, so it is rate-limited like a submit."""
    job_id = store.create_job(["https://chatgpt.com/share/abc"])
    import json

    store.update_job(job_id, status="done", transcripts=json.dumps([
        {"url": "https://chatgpt.com/share/abc", "title": "T",
         "messages": [{"role": "user", "content": "hi", "id": "m1"}]}]))
    monkeypatch.setattr(pdf_export, "build", lambda *a, **k: b"%PDF-1.4 fake")
    monkeypatch.setattr(limits, "MAX_PER_WINDOW", 1)

    first = client.get(f"/api/jobs/{job_id}/pdf")
    assert first.status_code == 200
    assert first.content.startswith(b"%PDF")
    assert first.headers["content-type"] == "application/pdf"
    assert client.get(f"/api/jobs/{job_id}/pdf").status_code == 429


def test_a_failing_browser_is_a_503_not_a_500(client, monkeypatch):
    import json

    job_id = store.create_job(["https://chatgpt.com/share/abc"])
    store.update_job(job_id, status="done", transcripts=json.dumps([
        {"url": "https://chatgpt.com/share/abc", "title": "T",
         "messages": [{"role": "user", "content": "hi", "id": "m1"}]}]))

    def boom(*a, **k):
        raise RuntimeError("browser crashed")

    monkeypatch.setattr(pdf_export, "build", boom)
    r = client.get(f"/api/jobs/{job_id}/pdf")
    assert r.status_code == 503
    assert "PDF rendering failed" in r.json()["detail"]
