# ChatGPT Share Exporter

Extract the **complete** text of a shared ChatGPT conversation — every message,
markdown, tables and code blocks — from a public `chatgpt.com/share/<id>` link,
then view it in a dashboard or download it as PDF / Word / Markdown / JSON.

It also runs as a Telegram bot: send a link, get the transcript back as a
`.docx` and `.json` file.

## Why a browser is required

A share page does **not** put the conversation in the DOM. There are no
`[data-message-author-role]`, `.markdown` or `article` nodes, and no JSON
endpoint — which is why naive scrapers return only the page title.

The payload is serialized into the server-rendered `window.__reactRouterContext`
stream as React's tuple wire format:

```js
{"key":"conversation-state","kind":"derived","value":
  ["object",[["messages",["array",[
      ["object",[["content",["string","…"]],["id",["string","…"]],["role",["string","user"]]]]
  ]]]]]}
```

`app/extractor.py` renders the page in headless Chromium, locates that entry
(it appears inline after hydration, or escaped inside the stream chunks before
it), decodes the tuple table, and walks out `messages[]`. That is the whole
trick — the decoder is in `decode_tuple()` and `find_state()`.

**The entry repeats.** The mobile share build streams progressively larger
snapshots, so `"conversation-state"` appears 5–7 times per page and the
**first** occurrence is a truncated early state (2 messages of a 24-message
conversation). `find_state()` decodes every occurrence and keeps the one with
the most messages. It also falls back to `serialized-conversation-state` — a
single escape-encoded JSON string of the same state — and can linearise
ChatGPT's `mapping` node tree when a build ships that instead.

Verified against the four reference links: 54 messages / 255,317 characters,
identical to a fully expanded manual read.

## Layout

```
app/
  extractor.py    Playwright fetch + tuple decoder (the core)
  store.py        SQLite jobs/users/events
  docx_export.py  Markdown → .docx renderer with RTL support
  pdf_export.py   Markdown → HTML → PDF, printed by the same Chromium
  main.py         FastAPI: dashboard, REST API, file downloads
  bot.py          Telegram long-polling bot
  auth.py         optional HTTP Basic auth
  templates/      dashboard (RTL, Persian)
  static/         CSS + JS
run.py            web entry point
run_bot.py        bot entry point
start.cmd         Windows entry point (double-click)
windows/
  start.ps1       picks the interpreter, starts the app
  desktop.py      serves on 127.0.0.1 and opens the browser
  installer.iss   Inno Setup script for the release build
tests/            decoder, docx, pdf, bot and security-regression tests (no network)
```

## Install on Windows

Download `ChatGPT-Share-Exporter-Setup.exe` from the
[Releases](../../releases) page and run it. It installs for the current user
only — no administrator rights, no system-wide changes — and adds a
*ChatGPT Share Exporter* shortcut. The app opens in your browser at a free
local port.

There is also a portable build (`ChatGPT-Share-Exporter-Windows.zip`): unpack it
anywhere and run `start.cmd`.

Python and Chromium are bundled, so nothing is downloaded on first launch. Job
history lives in `%LOCALAPPDATA%\ChatGPTShareExporter\data`, outside the install
folder, so installing a newer build keeps it.

Telegram bot, from the install folder:

```powershell
$env:TELEGRAM_BOT_TOKEN = "123456:ABC..."
python\python.exe -I run_bot.py
```

## Run locally (development)

```powershell
python -I -m pip install -r requirements.txt
python -I -m playwright install chromium
python -I run.py            # http://127.0.0.1:8099 (local dev)
```

Telegram bot:

```powershell
$env:TELEGRAM_BOT_TOKEN = "123456:ABC..."
$env:CSE_PROXY = "http://127.0.0.1:10808"   # only if api.telegram.org is blocked
python -I run_bot.py
```

`python -m app.bot` does **not** work under `-I`: the flag implies `-E` and
`-s`, so neither `PYTHONPATH` nor the working directory reaches `sys.path`.
`run_bot.py` inserts its own directory before importing, which is why both
entry points exist.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/api/export` | `{"url": "..."}` → `{job_id}` |
| POST | `/api/batch` | `{"urls": ["...", "..."]}` → `{job_id}` |
| GET | `/api/jobs/{id}` | job state + transcripts |
| GET | `/api/jobs/{id}/docx` | Word file |
| GET | `/api/jobs/{id}/md` | Markdown file |
| GET | `/api/jobs/{id}/json` | raw messages |
| GET | `/healthz` | liveness only (`{"ok":true}`, public) |

Extraction is asynchronous: the POST returns immediately, the worker fills the
job in, and the dashboard polls `/api/jobs/{id}`.

## Configuration

All settings are environment variables.

| Variable | Default | Purpose |
| --- | --- | --- |
| `CSE_HOST` / `CSE_PORT` | `127.0.0.1` / `8099` | Bind address (keep it on loopback behind a proxy) |
| `CSE_WORKERS` | `3` | Concurrent Chromium extractions |
| `CSE_AUTH_USER` / `CSE_AUTH_PASS` | unset | HTTP Basic wall over the whole dashboard |
| `CSE_PUBLIC` | unset | `1` = open to anonymous visitors (see below) |
| `CSE_TRUST_PROXY` | `127.0.0.1` | Comma-separated proxy addresses whose `X-Forwarded-For` is believed |
| `CSE_RATE_MAX` | `20` | Links accepted per address per rolling 60 s window |
| `CSE_MAX_QUEUE` | `60` | Queued jobs before new requests get `503` |
| `CSE_DB` | `data/app.db` | SQLite file; point it elsewhere to keep state off the checkout |
| `CSE_VACUUM` | `1` | `0` disables the periodic SQLite `VACUUM` |
| `TELEGRAM_BOT_TOKEN` | unset | Enables the bot |
| `TELEGRAM_CHAT_ID` | unset | Comma-separated chat ids allowed to use the bot |
| `CSE_PROXY` | unset | HTTP proxy for the extractor and the bot (`HTTPS_PROXY` also works) |

### Three ways to run it

1. **Private, one operator** (default). No auth env set: every request is
   treated as the admin, so the full job history and counters are visible.
2. **Password-protected.** Set `CSE_AUTH_USER`/`CSE_AUTH_PASS`. The browser
   asks for the Basic credentials; the dashboard then shows *all* jobs.
3. **Public service.** Set `CSE_PUBLIC=1`. There is no wall, and every visitor
   sees **only the jobs submitted from their own IP** — the dashboard filters
   by an `owner` column, and a foreign job id returns `404` rather than `403`
   so ids cannot be probed. Global counters (`/api/stats`) and the Events card
   become admin-only, and the rate limit and queue ceiling above are what keep
   one caller from exhausting the service.

Public mode is the reason `visitor()` looks at `X-Forwarded-For`: behind a
reverse proxy the socket address is always the proxy's. But that header also
decides which jobs a visitor may read, so it is trusted only when the request
actually comes from a local or private address (the proxy, a container
network), and only its **rightmost** (proxy-written) hop is used — a
client-supplied value in front of it is ignored. A public caller writing the
header directly is ignored and keyed by its socket address instead, so a forged
header cannot reach someone else's transcripts. `CSE_TRUST_PROXY` adds extra
trusted addresses beyond loopback and the private ranges.

The Telegram bot is **closed by default**: an empty `TELEGRAM_CHAT_ID` means
nobody, not everybody. A public bot would let any stranger start unlimited
Chromium runs.

`/healthz` is intentionally public (no auth) and returns only `{"ok":true}` so
a launcher can tell the server is up; counters stay behind auth on `/api/stats`.

## How updates work

Releases are built by GitHub Actions from a `v*` tag and attached to a GitHub
Release. Download the newer installer and run it: the previous version is
replaced in place while `%LOCALAPPDATA%\ChatGPTShareExporter\data` — the job
history — is left alone.

To uninstall, use *Apps & features* or the shortcut's uninstaller in the Start
menu; the data folder stays until you delete it yourself.

## Known constraints

- Only **public** share links work. Expired, revoked or private links raise a
  clear `ExtractError`.
- One extraction takes roughly 3–15 s (headless page render); extraction runs
  in a background thread per request.
- Rendered page weight is large (~2–3 MB) because the whole conversation is in
  the document; images and fonts are blocked in the browser context to cut it
  down.
- The Telegram bot needs `api.telegram.org` reachable. Where DNS blocks it, set
  `CSE_PROXY` to a working HTTP proxy.
