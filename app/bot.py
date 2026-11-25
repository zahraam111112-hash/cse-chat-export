"""Telegram bot front-end for the exporter (long polling, no framework deps)."""

from __future__ import annotations

import html
import io
import json
import os
import re
import sys
import time
import traceback

import httpx

from . import docx_export, store
from .extractor import ExtractError, Message, Transcript, extract, normalize_url

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
PROXY = os.environ.get("CSE_PROXY") or os.environ.get("HTTPS_PROXY") or None
API = f"https://api.telegram.org/bot{TOKEN}"
MAX_TEXT = 3900

# Comma-separated chat ids allowed to use the bot. Extraction is expensive and
# the bot has no IP to rate-limit, so an empty list means nobody: a public
# bot would otherwise let any stranger start unlimited Chromium runs.
ALLOWED = {c.strip() for c in os.environ.get("TELEGRAM_CHAT_ID", "").split(",") if c.strip()}


def allowed(chat_id: str) -> bool:
    return bool(ALLOWED) and chat_id in ALLOWED

HELP = (
    "<b>ChatGPT Share Exporter</b>\n\n"
    "یک لینک اشتراکی ChatGPT بفرست تا متن کامل گفتگو را برگردانم.\n\n"
    "• /start — راهنما\n"
    "• چند لینک در یک پیام هم قبول است (هر خط یک لینک)\n"
    "• فایل .docx و .json هم برایت می‌فرستم\n\n"
    "نمونه: <code>https://chatgpt.com/share/6b00bdd9-...</code>"
)


def _client() -> httpx.Client:
    kwargs = {"timeout": 120.0}
    if PROXY:
        kwargs["proxy"] = PROXY
    return httpx.Client(**kwargs)


def call(client: httpx.Client, method: str, **payload):
    r = client.post(f"{API}/{method}", data=payload)
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"{method}: {data}")
    return data["result"]


def redact(text: str) -> str:
    # httpx puts the whole request URL in its exception text, and every URL here
    # carries the token (/bot<TOKEN>/getUpdates) — printing it stores the bot
    # token in the journal for as long as the logs live.
    return text.replace(TOKEN, "<token>") if TOKEN else text


def esc(text: str) -> str:
    # Everyone sending HTML to Telegram has to escape the payload first; a
    # conversation containing "<div>" otherwise gets the whole message rejected.
    return html.escape(text, quote=False)


# Telegram's own tag list. Only a real tag resets the "am I inside markup?"
# state, so a bare `<` the writer typed is not mistaken for the start of one.
_TAGS = {"pre", "code", "b", "strong", "i", "em", "u", "ins", "s", "strike", "del",
         "a", "tg-spoiler", "span", "blockquote"}
_TAG = re.compile(r"</?([a-zA-Z][a-zA-Z0-9-]*)(?:\s[^<>]*)?>")
# Deeper than this is a conversation that opened the same tag a hundred times,
# not formatting. Tracking it would need more closing tags than a message can
# hold, so the extra ones are dropped instead of sent and rejected.
_MAX_DEPTH = 8


def _balanced(fragment: str) -> tuple[str, list[str]]:
    """Return `fragment` as parseable HTML, plus the tags still open at its end.

    `esc()` leaves no bare `<` or `&` behind, so this is simple: iterate the
    real tags in order and keep the stack. An opening tag is left open on
    purpose — the caller continues it in the next chunk rather than leaving a
    `<pre>` unclosed, which Telegram rejects. A tag with nothing to close (a
    stray `</b>`) or one nested past `_MAX_DEPTH` is removed: both are markup,
    not words, and either one alone makes Telegram reject the message.
    """
    stack: list[str] = []
    kept: list[str] = []
    pos = 0
    for m in _TAG.finditer(fragment):
        name = m.group(1).lower()
        if name not in _TAGS:
            continue
        if m.group(0).startswith("</"):
            if name not in stack:
                kept.append(fragment[pos : m.start()])
                pos = m.end()
                continue
            # Close whatever was nested inside it; the text keeps its order
            # and Telegram only needs the markup to be well formed.
            while stack and stack.pop() != name:
                pass
            continue
        if len(stack) >= _MAX_DEPTH:
            kept.append(fragment[pos : m.start()])
            pos = m.end()
            continue
        stack.append(name)
    kept.append(fragment[pos:])
    body = "".join(kept)
    return body + "".join(f"</{t}>" for t in reversed(stack)), stack


def _safe_cut(text: str, room: int) -> int:
    """The largest index up to `room` that is not inside a tag or an entity."""
    cut = min(room, len(text))
    if cut >= len(text):
        return cut
    # Prefer ending on a line break: it reads better than a cut mid-sentence.
    nl = text.rfind("\n", cut // 2, cut)
    if nl > 0:
        cut = nl + 1
    # `esc()` writes a typed `<` as `&lt;`, so a raw `<` here is a real tag.
    lt = text.rfind("<", 0, cut)
    if lt != -1 and text.find(">", lt, cut) == -1:
        cut = lt
    # Likewise any bare `&` is half of `&amp;`/`&lt;` when no `;` follows. An
    # entity at the very start of the room is longer than the room: its `&` is
    # the cut, and `0` tells the caller there is nothing it can send.
    amp = text.rfind("&", 0, cut)
    if amp != -1 and text.find(";", amp, cut) == -1:
        cut = amp
    return cut


def _chunks(text: str, limit: int) -> list[str]:
    """Split already-escaped HTML into pieces Telegram will parse.

    A plain `text[i:i+limit]` cuts wherever the count lands: through `&amp;`,
    through `<pre>`, and it leaves a chunk made of `t;div&gt;`. Telegram then
    answers 400 "can't parse entities" and `handle_link` unwinds with the job
    still 'running'. So cut at a line break when one is near the limit, never
    inside an entity or a tag, and carry any open markup into the next chunk.

    Every returned piece is well formed and within `limit`. Markup that cannot
    be closed inside the budget — a stray `</b>`, a nest past `_MAX_DEPTH`, a
    tag longer than a whole message — is dropped rather than sent: it carries
    no words, and it alone is enough for Telegram to reject the message.
    """
    out: list[str] = []
    reopen: list[str] = []
    while text:
        before = len(text)
        while True:
            prefix = "".join(f"<{t}>" for t in reopen)
            if len(prefix) < limit:
                break
            # The carried markup alone would fill a message; send it without.
            # Clearing it leaves `prefix` empty, so the next pass gets here.
            reopen = []
        room = limit - len(prefix)
        cut = _safe_cut(text, room)
        if cut <= 0:
            # Nothing in the room can be cut cleanly: a `<` starts at once and
            # its `>` is past the room. The carry is what ate the room, so try
            # again without it; if that was not it, the tag itself is longer
            # than a whole message and can never be sent at all.
            if reopen:
                reopen = []
                continue
            end = text.find(">")
            # Drop the tag and keep the words after it. A `<` with no `>` at
            # all is markup too, so only its own character goes.
            text = text[end + 1 :] if end != -1 else text[1:]
            continue
        piece, text = text[:cut], text[cut:]
        closed, still = _balanced(prefix + piece)
        # Closing what the cut left open can push the piece past `limit`. Give
        # that many characters back rather than dropping them — and give them
        # back through `_safe_cut`, because trimming blindly is what tears
        # `&amp;` in half, and no closing tag repairs a half-sent entity.
        while len(closed) > limit:
            keep = _safe_cut(piece, len(piece) - (len(closed) - limit))
            if keep <= 0 or keep >= len(piece):
                break
            text = piece[keep:] + text
            piece = piece[:keep]
            closed, still = _balanced(prefix + piece)
        if len(closed) > limit:
            # A limit too small to balance even one tag. The words still have
            # to arrive, so they are sent without the markup around them.
            plain = _TAG.sub("", piece)[:limit]
            if plain:
                out.append(plain)
            still = []
        elif closed:
            # Closing tags with nothing to close leave an empty message, which
            # Telegram rejects just as firmly as a malformed one.
            out.append(closed)
        reopen = still
        if len(text) >= before:
            # Every path above advances the text, so this is a guard against a
            # loop that would otherwise hang the poll thread for ever.
            text = text[1:]
    return out


def send(client: httpx.Client, chat_id, text: str, **extra):
    # `text` is already HTML: callers escape their data with esc() first.
    for chunk in _chunks(text, MAX_TEXT):
        call(client, "sendMessage", chat_id=chat_id, text=chunk,
             parse_mode="HTML", disable_web_page_preview="true", **extra)


def upload_document(client: httpx.Client, chat_id, filename: str, data: bytes, caption: str = ""):
    files = {"document": (filename, io.BytesIO(data), "application/octet-stream")}
    payload = {"chat_id": chat_id, "caption": esc(caption[:1000]), "parse_mode": "HTML"}
    r = client.post(f"{API}/sendDocument", data=payload, files=files)
    if r.status_code >= 400:
        raise RuntimeError(f"sendDocument {r.status_code}: {r.text[:200]}")
    if not r.json().get("ok"):
        raise RuntimeError(f"sendDocument: {r.text[:200]}")


def handle_link(client: httpx.Client, chat_id: str, url: str) -> None:
    try:
        target = normalize_url(url)
    except ExtractError as exc:
        send(client, chat_id, esc(f"❌ {exc}"))
        return

    # Record the attempt before doing any work, so a failure still shows in the
    # dashboard's history instead of vanishing.
    job_id = store.create_job([target], source="telegram", chat_id=chat_id, status="running")

    send(client, chat_id, f"⏳ در حال استخراج…\n<code>{esc(target)}</code>")
    try:
        transcript = extract(target, proxy=PROXY)
    except Exception as exc:  # noqa: BLE001
        store.update_job(job_id, status="failed", error=f"{type(exc).__name__}: {exc}")
        send(client, chat_id, f"❌ خطا: <code>{esc(type(exc).__name__)}</code>\n{esc(str(exc))}")
        return

    header = (
        f"✅ <b>{esc(transcript.title)}</b>\n"
        f"پیام‌ها: {len(transcript.messages)} · کاراکترها: {transcript.chars:,}"
    )
    send(client, chat_id, header)

    body = transcript.as_text()
    send(client, chat_id, f"<pre>{esc(body[:MAX_TEXT - 40])}</pre>")
    if len(body) > MAX_TEXT:
        send(client, chat_id, "… متن کامل در فایل‌های پیوست است.")

    try:
        docx_bytes = docx_export.build([transcript])
        upload_document(client, chat_id, f"{_slug(transcript.title)}.docx", docx_bytes,
                        caption="Word · متن کامل")
    except Exception as exc:  # noqa: BLE001
        send(client, chat_id, f"⚠️ ساخت فایل ورد ناموفق: {esc(redact(str(exc)))}")

    # Both uploads are best effort: Telegram can refuse a file (size, flood
    # control), and the extraction itself has already succeeded. Letting an
    # upload raise would unwind to the poll loop with the row still 'running'.
    try:
        json_bytes = json.dumps(transcript.as_dict(), ensure_ascii=False, indent=2).encode()
        upload_document(client, chat_id, f"{_slug(transcript.title)}.json", json_bytes,
                        caption="JSON · پیام‌ها و نقش‌ها")
    except Exception as exc:  # noqa: BLE001
        send(client, chat_id, f"⚠️ ارسال فایل JSON ناموفق: {esc(redact(str(exc)))}")

    store.update_job(job_id, status="done",
                     transcripts=json.dumps([transcript.as_dict()], ensure_ascii=False))
    store.count_run(chat_id)


def _slug(title: str) -> str:
    keep = [c if c.isalnum() else "-" for c in title][:60]
    return ("".join(keep).strip("-") or "chatgpt-share")[:60]


def handle_update(client: httpx.Client, update: dict) -> None:
    message = update.get("message") or update.get("edited_message")
    if not message:
        return
    chat_id = str(message["chat"]["id"])
    user = message.get("from") or {}
    store.touch_user(chat_id, user.get("username"))

    text = (message.get("text") or "").strip()
    if not text:
        return
    if text.startswith("/start") or text.startswith("/help"):
        send(client, chat_id, HELP)
        return
    if text.startswith("/stats"):
        if not allowed(chat_id):
            send(client, chat_id, "⛔ /stats is restricted.")
            return
        s = store.stats()
        send(client, chat_id,
             f"jobs={s['jobs']} done={s['done']} partial={s['partial']}"
             f" failed={s['failed']} users={s['users']}")
        return

    if not allowed(chat_id):
        # No allowlist configured means closed, not open. Saying so plainly
        # beats a silent bot that looks broken to its own operator.
        send(client, chat_id,
             "⛔ این ربات در دسترس عمومی نیست.\n"
             "ربات هیچ گفت‌وگویی را استخراج نکرد." if ALLOWED else
             "⛔ این ربات هنوز تنظیم نشده است (TELEGRAM_CHAT_ID خالی است).")
        print(f"bot: rejected chat {chat_id} (not in TELEGRAM_CHAT_ID)", file=sys.stderr, flush=True)
        return

    links = [part for part in text.split() if "share/" in part or part.startswith("chatgpt")]
    if not links:
        send(client, chat_id, "لینک اشتراکی ChatGPT پیدا نکردم.\nنمونه:\n"
                              "<code>https://chatgpt.com/share/&lt;id&gt;</code>")
        return
    for link in links[:5]:
        handle_link(client, chat_id, link)


def main() -> int:
    if not TOKEN:
        print("TELEGRAM_BOT_TOKEN is not set", file=sys.stderr)
        return 2
    store.init()
    print("bot: polling", flush=True)
    offset = None
    with _client() as client:
        call(client, "deleteWebhook", drop_pending_updates="false")
        while True:
            try:
                payload = {"timeout": 50, "allowed_updates": '["message"]'}
                if offset:
                    payload["offset"] = offset
                updates = call(client, "getUpdates", **payload)
                for update in updates:
                    offset = update["update_id"] + 1
                    try:
                        handle_update(client, update)
                    except Exception:  # noqa: BLE001
                        print(redact(traceback.format_exc()), file=sys.stderr, flush=True)
                        try:
                            send(client, str(update["message"]["chat"]["id"]),
                                 "⚠️ خطای داخلی رخ داد.")
                        except Exception:  # noqa: BLE001
                            pass
            except KeyboardInterrupt:
                return 0
            except Exception as exc:  # noqa: BLE001
                print("poll error:", redact(str(exc)), file=sys.stderr, flush=True)
                time.sleep(5)


if __name__ == "__main__":
    raise SystemExit(main())
