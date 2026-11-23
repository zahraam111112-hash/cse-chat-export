"""Offline tests for the decoder — no network, no browser."""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app.extractor import (
    ExtractError,
    decode_tuple,
    find_state,
    messages_from_state,
    normalize_url,
)


def test_decode_scalars():
    assert decode_tuple(["string", "hi"]) == "hi"
    assert decode_tuple(["number", 3]) == 3
    assert decode_tuple(["boolean", True]) is True
    assert decode_tuple(["null"]) is None
    assert decode_tuple(["undefined"]) is None


def test_decode_nested():
    payload = ["object", [["a", ["string", "x"]], ["b", ["array", [["number", 1]]]]]]
    assert decode_tuple(payload) == {"a": "x", "b": [1]}


def test_decode_messages_shape():
    state = {
        "messages": [
            {"content": "hello", "id": "m1", "role": "user"},
            {"content": "hi there", "id": "m2", "role": "assistant"},
        ]
    }
    msgs = messages_from_state(state)
    assert [m.role for m in msgs] == ["user", "assistant"]
    assert msgs[0].label == "user"


def test_empty_message_dropped():
    state = {"messages": [{"content": "  ", "role": "user"}, {"content": "ok", "role": "assistant"}]}
    assert len(messages_from_state(state)) == 1


def test_parts_content():
    state = {"messages": [{"content": {"parts": ["one", "two"]}, "role": "assistant"}]}
    assert messages_from_state(state)[0].content == "one\n\ntwo"


def test_normalize_accepts_share_link():
    url = normalize_url("https://chatgpt.com/share/6b00bdd9-d3e8-83eb-9289-81e2b3eb2dcc")
    assert url == "https://chatgpt.com/share/6b00bdd9-d3e8-83eb-9289-81e2b3eb2dcc"


def test_normalize_rejects_other_sites():
    with pytest.raises(ExtractError):
        normalize_url("https://example.com/share/abc")


def test_find_state_inline_json():
    value = ["object", [["messages", ["array", []]]]]
    entry = {"key": "conversation-state", "kind": "derived", "value": value}
    html = f"<script>x={json.dumps([{'k': 1}, entry])}</script>"
    assert find_state(html) == {"messages": []}


def _entry(messages):
    value = [
        "object",
        [["messages", ["array", [
            ["object", [["role", ["string", r]], ["content", ["string", c]]]]
            for r, c in messages
        ]]]],
    ]
    return {"key": "conversation-state", "kind": "derived", "value": value}


def test_find_state_picks_the_longest_snapshot():
    """The mobile share page streams progressively larger snapshots; the first
    occurrence is usually a truncated early state and must not win."""
    short = _entry([("user", "hi")])
    full = _entry([("user", "hi"), ("assistant", "there"), ("user", "more")])
    html = "<html><body>" + json.dumps(short) + "<div></div>" + json.dumps(full) + "</body></html>"
    state = find_state(html)
    assert len(state["messages"]) == 3
    assert state["messages"][-1]["content"] == "more"


def test_find_state_falls_back_to_serialized_string():
    inner = json.dumps({"messages": [{"role": "user", "content": "ok"}], "sharedSource": {}})
    value = ["string", inner]
    entry = {"key": "serialized-conversation-state", "kind": "derived", "value": value}
    html = f"<script>x={json.dumps([entry])}</script>"
    state = find_state(html)
    assert state["messages"][0]["content"] == "ok"


def test_find_state_missing():
    with pytest.raises(ExtractError):
        find_state("<html>nope</html>")
