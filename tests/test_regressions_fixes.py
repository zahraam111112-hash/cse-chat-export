"""Regression tests for the defects fixed in this pass.

Each test names the behaviour it protects, so a future refactor that brings
the bug back fails here instead of in production.
"""

import docx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from app import docx_export, extractor, limits, main


def _request(headers: dict[str, str], client=("127.0.0.1", 1234)) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    return Request({"type": "http", "headers": raw, "client": client,
                    "method": "GET", "path": "/", "query_string": b""})


# --- ownership cannot be forged through X-Forwarded-For ---------------------

def test_visitor_uses_the_last_forwarded_hop_behind_a_proxy():
    # A proxy appends the address it saw; the leftmost value is client-supplied.
    req = _request({"x-forwarded-for": "1.2.3.4, 198.51.100.7"})
    assert main.visitor(req) == "198.51.100.7"


def test_visitor_ignores_forwarded_header_from_an_untrusted_peer():
    # A public caller talking to the app directly cannot set their own identity.
    req = _request({"x-forwarded-for": "45.33.32.156"}, client=("8.8.8.8", 5555))
    assert main.visitor(req) == "8.8.8.8"


def test_trusted_peer_classification():
    assert main._trusted_peer("127.0.0.1")
    assert main._trusted_peer("::1")
    assert main._trusted_peer("10.1.2.3")
    assert main._trusted_peer("192.168.1.9")
    assert main._trusted_peer("testclient")   # local transport, not an IP
    assert not main._trusted_peer("8.8.8.8")
    assert not main._trusted_peer("45.33.32.156")
    assert not main._trusted_peer("")


def test_visitor_falls_back_to_socket_address():
    req = _request({})
    assert main.visitor(req) == "127.0.0.1"


# --- a refused submission does not spend the rate budget ---------------------

def test_full_queue_is_refused_without_charging_budget(monkeypatch):
    limits.reset()
    monkeypatch.setattr(main.store, "queued_count", lambda owner=None: limits.MAX_QUEUE)
    called = {"n": 0}

    def hit(key, cost=1):
        called["n"] += 1
        return True

    monkeypatch.setattr(limits, "hit", hit)
    with pytest.raises(HTTPException) as exc:
        main._admit("1.2.3.4", 1)
    assert exc.value.status_code == 503
    assert called["n"] == 0, "the rate budget was charged for a request that never ran"
    limits.reset()


def test_rate_limit_still_refuses(monkeypatch):
    limits.reset()
    monkeypatch.setattr(main.store, "queued_count", lambda owner=None: 0)
    monkeypatch.setattr(limits, "hit", lambda key, cost=1: False)
    with pytest.raises(HTTPException) as exc:
        main._admit("1.2.3.4", 1)
    assert exc.value.status_code == 429
    limits.reset()


# --- the serialized fallback is reached when the first state is empty -------

def test_find_state_prefers_a_serialized_state_with_messages(monkeypatch):
    monkeypatch.setattr(extractor, "_best", lambda html: {"messages": []})
    monkeypatch.setattr(extractor, "_from_serialized",
                        lambda html: {"messages": [{"role": "user", "content": "hi"}]})
    state = extractor.find_state("<html></html>")
    assert state["messages"], "an empty state must not shadow a populated serialized one"


def test_find_state_still_returns_a_genuinely_empty_state(monkeypatch):
    # No populated source anywhere: return the empty state, not a bogus error.
    monkeypatch.setattr(extractor, "_best", lambda html: {"messages": []})
    monkeypatch.setattr(extractor, "_from_serialized", lambda html: None)
    assert extractor.find_state("<html></html>") == {"messages": []}


# --- a table row of blank cells is data, not a separator ---------------------

def test_docx_keeps_an_all_blank_row():
    document = docx.Document()
    docx_export._table(document, [["h1", "h2"], ["", ""], ["a", "b"]])
    rows = [[c.text for c in r.cells] for r in document.tables[0].rows]
    # header + the blank row + the data row
    assert len(rows) == 3, rows


def test_docx_still_drops_a_real_separator():
    document = docx.Document()
    docx_export._table(document, [["h1", "h2"], ["---", "---"], ["a", "b"]])
    rows = [[c.text for c in r.cells] for r in document.tables[0].rows]
    assert len(rows) == 2, rows
