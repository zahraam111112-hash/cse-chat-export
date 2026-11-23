# ChatGPT Share Exporter

Extract the **complete** text of a shared ChatGPT conversation — every message,
markdown, tables and code blocks — from a public `chatgpt.com/share/<id>` link,
then view it in a dashboard or download it as Word / Markdown / JSON.

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
  main.py         FastAPI: dashboard, REST API, file downloads
  bot.py          Telegram long-polling bot
  auth.py         optional HTTP Basic auth
  templates/      dashboard (RTL, Persian)
  static/         CSS + JS
run.py            web entry point
run_bot.py        bot entry point
deploy/
  setup-service.sh        systemd user services + venv + Chromium
  install-update-timer.sh pull origin/main every 10 min, self-reverting
  update-service.sh       the update step itself (run by the timer)
  caddy-share.conf        reverse-proxy site block
tests/            decoder, docx, bot and security-regression tests (no network)
```

## Run locally

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

Public mode is the reason `visitor()` reads `X-Forwarded-For`: behind a reverse
proxy the socket address is always the proxy's. The header is only used to
group jobs and count rate — forging it can only shed one caller's own budget,
and the queue ceiling still caps total work.

The Telegram bot is **closed by default**: an empty `TELEGRAM_CHAT_ID` means
nobody, not everybody. A public bot would let any stranger start unlimited
Chromium runs.

## Deployment

The server runs it as an **unprivileged user service** (no docker, no sudo),
straight out of a git clone, so `main` on GitHub *is* what runs there:

```bash
# on the server, as the dev user
git clone https://github.com/<owner>/<repo>.git ~/cse-repo
bash ~/cse-repo/deploy/setup-service.sh
bash ~/cse-repo/deploy/install-update-timer.sh
```

The clone authenticates with a **read-only deploy key** (`~/.ssh/cse_deploykey`,
pinned to `github.com` in `~/.ssh/config`), so the server can pull but never
push.

`setup-service.sh` creates `.venv`, installs Chromium once, writes
`~/.config/chatgpt-share-export.env` (mode 600) and enables both services on
`127.0.0.1:8098`.

`install-update-timer.sh` adds `cse-update.timer`, which polls `origin/main`
every 10 minutes. When it moves, the timer fetches, hard-resets, reinstalls
`requirements.txt` only if that file changed, restarts both services, and
waits for `/healthz`. **If health does not come back it resets to the previous
commit and restarts** — so a bad push self-reverts within one cycle.

`data/` is gitignored local state and is never touched by the updater, so
jobs survive updates. To roll back by hand:

```bash
cd ~/cse-repo
git log --oneline -5
git reset --hard <good-sha>
systemctl --user restart chatgpt-share-export.service chatgpt-share-export-bot.service
```

Public access needs an A record first:

```
cse.<your-domain> -> <origin-ip>
```

then append `deploy/caddy-share.conf` to `/etc/caddy/Caddyfile` and
`systemctl restart caddy` (Caddy runs with `admin off`, so reload does not work;
a restart is required — verify the other sites afterwards).

Tips from the first deploy:

- Resolvers cache the *absence* of a subdomain for the zone's negative TTL
  (300 s). `nslookup` can keep reporting "no A record" for a few minutes after
  the record exists — confirm against the authoritative NS or DoH
  (`https://cloudflare-dns.com/dns-query?name=…&type=A`) before touching Caddy.
- `caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile` catches
  syntax errors *before* the restart that would take every site down.
- A site block with no DNS record never gets a certificate and spams the log;
  delete it rather than leaving it commented out partially.
- `/healthz` is intentionally public (no auth) for liveness probes and returns
  only `{"ok":true}` — counters stay behind auth on `/api/stats`.

Auth lives **inside the app** (`CSE_AUTH_USER` / `CSE_AUTH_PASS` in the env
file), not in the shared basic-auth wall, so this subdomain gets its own
password.

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
