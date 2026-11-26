"""Extract full conversation text from a shared ChatGPT link.

ChatGPT share pages do not render conversation content into the DOM as
`[data-message-author-role]` / `.markdown` nodes, and they expose no JSON
endpoint. The content is serialized into the server-rendered
`window.__reactRouterContext` stream as React's tuple wire format,
under the entry {"key":"conversation-state", ... "value": ...}.

The stream escapes every quote (\\"), so we unescape the captured script
bodies, join the enqueued chunks, and then walk the flat string table
decoding `[\"string\", \"...\"]` style tuples.
"""

from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

from . import browser

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

STATE_KEY = "conversation-state"


class ExtractError(Exception):
    """Raised when a share link cannot be turned into a transcript."""


@dataclass
class Message:
    role: str
    content: str
    id: str = ""

    @property
    def label(self) -> str:
        return "user" if self.role == "user" else "assistant"


@dataclass
class Transcript:
    url: str
    title: str
    messages: list[Message] = field(default_factory=list)

    @property
    def chars(self) -> int:
        return sum(len(m.content) for m in self.messages)

    def as_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "message_count": len(self.messages),
            "chars": self.chars,
            "messages": [
                {"role": m.role, "id": m.id, "content": m.content}
                for m in self.messages
            ],
        }

    def as_markdown(self) -> str:
        out = [f"# {self.title}", "", f"Source: {self.url}", ""]
        for i, m in enumerate(self.messages, 1):
            who = "User" if m.role == "user" else "ChatGPT"
            out += [f"## {i}. {who}", "", m.content.strip(), ""]
        return "\n".join(out)

    def as_text(self) -> str:
        parts = []
        for i, m in enumerate(self.messages, 1):
            who = "USER" if m.role == "user" else "CHATGPT"
            parts.append(f"=== {i}. {who} ===\n{m.content.strip()}\n")
        return "\n".join(parts)


# --------------------------------------------------------------------------
# decoding
# --------------------------------------------------------------------------

_TAGGED = {
    "string", "number", "boolean", "undefined", "null", "array", "object",
    "bigint", "date", "map", "set", "symbol", "function",
}


def decode_tuple(node: Any) -> Any:
    """Decode React Router's serialized tuple format.

    Scalar-ish tags may appear as a single element (`["null"]`) when they
    carry no value; anything else is `[tag, value]`.
    """
    if not isinstance(node, list):
        return node
    if node and isinstance(node[0], str) and node[0] in _TAGGED and len(node) <= 2:
        tag = node[0]
        val = node[1] if len(node) == 2 else None
        if tag in ("string", "number", "boolean"):
            return val
        if tag in ("undefined", "null", "function", "symbol"):
            return None
        if tag == "array":
            return [decode_tuple(x) for x in (val or [])]
        if tag == "object":
            return {k: decode_tuple(v) for k, v in (val or [])}
        return {"__type": tag, "value": val}
    return [decode_tuple(x) for x in node]


def _unescape_stream(chunk: str) -> str:
    """Each enqueued chunk is a JSON string literal minus its outer quotes."""
    try:
        return json.loads('"' + chunk + '"')
    except json.JSONDecodeError:
        return chunk.replace('\\"', '"').replace("\\\\", "\\")


_ENQUEUE = re.compile(
    r'streamController\.enqueue\(\s*"((?:[^"\\]|\\.)*)"\s*\)', re.S
)

DIRECT_KEY = '"key":"conversation-state"'
ESCAPED_KEY = r'\"key\":\"conversation-state\"'
_KEY_RE = re.compile(r'"key"\s*:\s*"conversation-state"')
_SERIALIZED_RE = re.compile(
    r'"key"\s*:\s*"serialized-conversation-state"\s*,\s*"kind"\s*:\s*"[^"]*"\s*,\s*"value"\s*:\s*\[',
)


def _decode_entry(text: str, key_at: int) -> dict[str, Any]:
    """raw_decode the JSON object whose `key` starts at `key_at`."""
    start = text.rfind("{", 0, key_at)
    if start < 0:
        raise ExtractError("Malformed conversation entry.")
    obj, _ = json.JSONDecoder().raw_decode(text[start:])
    decoded = decode_tuple(obj.get("value"))
    if not isinstance(decoded, dict):
        raise ExtractError("Conversation entry could not be decoded.")
    return decoded


def _best(payload: str) -> dict[str, Any] | None:
    """Decode every conversation-state entry and keep the most complete one.

    The mobile share experience streams the conversation in progressively
    larger snapshots, so the same key appears several times and the first
    occurrence is usually a truncated early state. The one with the most
    messages wins; ties break toward the later entry.
    """
    best: dict[str, Any] | None = None
    best_n = -1
    for match in _KEY_RE.finditer(payload):
        try:
            state = _decode_entry(payload, match.start())
        except Exception:  # noqa: BLE001 - a malformed snapshot is not fatal
            continue
        n = len(state.get("messages") or [])
        if n >= best_n:
            best, best_n = state, n
    return best


def _from_serialized(payload: str) -> dict[str, Any] | None:
    """Fall back to `serialized-conversation-state`, a JSON string of the state.

    This build serializes the whole state into one escaped JSON string; other
    builds send the `mapping` node tree instead. Handle both.
    """
    best: dict[str, Any] | None = None
    best_n = -1
    for match in _SERIALIZED_RE.finditer(payload):
        try:
            arr, _ = json.JSONDecoder().raw_decode(payload, match.end() - 1)
        except json.JSONDecodeError:
            continue
        if not (isinstance(arr, list) and len(arr) == 2 and arr[0] == "string"):
            continue
        try:
            inner = json.loads(arr[1])
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(inner, dict):
            continue
        state = inner if isinstance(inner.get("messages"), list) else _from_mapping(inner)
        if not state:
            continue
        n = len(state.get("messages") or [])
        if n >= best_n:
            best, best_n = state, n
    return best


def _from_mapping(state: dict[str, Any]) -> dict[str, Any] | None:
    """Linearise ChatGPT's `mapping` node tree into ordinary messages."""
    mapping = state.get("mapping") or {}
    node = state.get("currentNode") or state.get("current_node")
    if not mapping:
        return None
    if not node:
        parents = {v.get("parent") for v in mapping.values() if isinstance(v, dict)}
        node = next((k for k in mapping if k not in parents), None)
    chain, seen = [], set()
    while node and node in mapping and node not in seen:
        seen.add(node)
        chain.append(mapping[node])
        node = mapping[node].get("parent")
    chain.reverse()
    messages = []
    for item in chain:
        msg = item.get("message") if isinstance(item, dict) else None
        if not isinstance(msg, dict):
            continue
        author = (msg.get("author") or {}).get("role")
        content = msg.get("content") or {}
        parts = content.get("parts") if isinstance(content, dict) else None
        text = content.get("text") if isinstance(content, dict) else None
        if isinstance(parts, list):
            body = "\n\n".join(str(p) for p in parts if isinstance(p, str))
        elif isinstance(text, str):
            body = text
        else:
            body = ""
        if body.strip():
            messages.append({"role": author or "assistant", "content": body,
                             "id": msg.get("id") or ""})
    return {"messages": messages} if messages else None


def find_state(html: str) -> dict[str, Any]:
    """Locate the conversation-state entry in either hydrated or stream form.

    After hydration the entry is inline JSON. Before hydration it only exists
    as escaped chunks pushed into a ReadableStream, so fall back to joining
    those chunks and searching the unescaped payload. Every snapshot is tried
    and the richest one is returned.
    """
    state = _best(html)
    if state and state.get("messages"):
        return state
    # `_best` can return a state that carries no messages. The serialized form
    # often holds the real one, so try it — but only let it win when it has
    # messages, or an empty state would be replaced by None and every later
    # fallback skipped, reporting a good link as empty.
    serialized = _from_serialized(html)
    if serialized and serialized.get("messages"):
        return serialized

    chunks = _ENQUEUE.findall(html)
    if chunks:
        payload = "".join(_unescape_stream(c) for c in chunks)
        found = _best(payload) or _from_serialized(payload)
        if found:
            return found

    if ESCAPED_KEY in html:
        payload = html.replace('\\"', '"').replace("\\\\", "\\")
        found = _best(payload) or _from_serialized(payload)
        if found:
            return found

    if state:
        return state

    raise ExtractError(
        "Conversation data not present in the page. The link may be expired, "
        "revoked, or not publicly shared."
    )


def messages_from_state(state: dict[str, Any]) -> list[Message]:
    raw = state.get("messages") or []
    out: list[Message] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        content = _coerce_content(item.get("content"))
        role = str(item.get("role") or "assistant")
        out.append(Message(role=role, content=content, id=str(item.get("id") or "")))

    # A message with an empty body carries no information (e.g. tool-only turns).
    return [m for m in out if m.content.strip()]


def _coerce_content(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        parts = value.get("parts")
        if isinstance(parts, list):
            return "\n\n".join(str(p) for p in parts if isinstance(p, str))
        text = value.get("text") or value.get("content")
        return text if isinstance(text, str) else ""
    if isinstance(value, list):
        return "\n\n".join(str(p) for p in value if isinstance(p, str))
    return str(value)


# --------------------------------------------------------------------------
# fetching
# --------------------------------------------------------------------------

def normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        raise ExtractError("Empty link.")
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    m = re.match(
        r"^https?://(?:www\.|chat\.)?(chatgpt\.com|chat\.openai\.com)/share/([\w-]+)",
        url,
        re.I,
    )
    if not m:
        raise ExtractError(
            "Only public share links are supported "
            "(https://chatgpt.com/share/<id>)."
        )
    host = "chatgpt.com"
    return f"https://{host}/share/{m.group(2)}"


def fetch_html(url: str, timeout_ms: int = 90_000, proxy: str | None = None) -> tuple[str, str]:
    """Render the share page in headless Chromium and return (html, title).

    The browser itself is `app.browser`'s business: it drives the Chromium the
    machine already has (Edge on Windows) unless Playwright is installed, and
    it owns the "has the page stopped moving" loop that used to live here.
    """
    try:
        return browser.render(url, timeout_ms, proxy, UA)
    except browser.BrowserMissing as exc:
        raise ExtractError(str(exc)) from exc


def extract(url: str, proxy: str | None = None) -> Transcript:
    target = normalize_url(url)
    html, title = fetch_html(target, proxy=proxy)
    state = find_state(html)
    msgs = messages_from_state(state)
    if not msgs:
        raise ExtractError("No messages found — the share may be empty or revoked.")
    title = (title or "").replace("ChatGPT - ", "").strip() or "ChatGPT share"
    return Transcript(url=target, title=title, messages=msgs)


def extract_many(urls: Iterable[str], proxy: str | None = None) -> tuple[list[Transcript], list[dict[str, str]]]:
    ok: list[Transcript] = []
    failed: list[dict[str, str]] = []
    for u in urls:
        try:
            ok.append(extract(u, proxy=proxy))
        except Exception as exc:  # noqa: BLE001 - reported to the caller
            failed.append({"url": u, "error": f"{type(exc).__name__}: {exc}"})
    return ok, failed


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python -m app.extractor <share-url> [out.json]")
        raise SystemExit(2)
    t = extract(sys.argv[1])
    if len(sys.argv) > 2:
        with open(sys.argv[2], "w", encoding="utf-8") as fh:
            json.dump(t.as_dict(), fh, ensure_ascii=False, indent=2)
        print(f"saved {sys.argv[2]}: {len(t.messages)} messages, {t.chars} chars")
    else:
        print(t.as_text())
