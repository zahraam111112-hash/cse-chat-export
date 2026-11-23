"""Telegram bot front-end for the exporter (long polling, no framework deps)."""

from __future__ import annotations

import html
import io
import json
import os
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


def send(client: httpx.Client, chat_id, text: str, **extra):
    # `text` is already HTML: callers escape their data with esc() first.
    for start in range(0, len(text), MAX_TEXT):
        call(client, "sendMessage", chat_id=chat_id, text=text[start : start + MAX_TEXT],
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

    json_bytes = json.dumps(transcript.as_dict(), ensure_ascii=False, indent=2).encode()
    upload_document(client, chat_id, f"{_slug(transcript.title)}.json", json_bytes,
                    caption="JSON · پیام‌ها و نقش‌ها")

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
