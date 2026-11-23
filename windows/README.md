# Windows build

The app ships as a self-contained Windows folder: a Python runtime, the
dependencies, Chromium, and a launcher. Nothing is installed system-wide and
no administrator rights are needed.

```
start.cmd                  double-click entry point
windows\start.ps1          picks the interpreter and runs the app
windows\desktop.py         serves on 127.0.0.1 and opens the browser
python\Scripts\python.exe  bundled runtime, dependencies and Chromium
app\                       the application
```

`start.cmd` also works in a plain developer checkout: it falls back to `.venv\`
and then to the system `python`, so a contributor can run the same entry point
after `pip install -r requirements.txt`.

## Where state lives

The jobs database is written to `%LOCALAPPDATA%\ChatGPTShareExporter\data\app.db`,
never inside the install folder, so unpacking a newer release over an older one
keeps the history. Set `CSE_DATA` to override the location.

## Rebuilding a release

Tag a commit on `main`:

```powershell
git tag v1.0.0
git push origin v1.0.0
```

`.github/workflows/release.yml` runs the test suite, assembles the payload,
bundles Chromium, and attaches `ChatGPT-Share-Exporter-Setup.exe` and
`ChatGPT-Share-Exporter-Windows.zip` to the GitHub Release.

Chromium is bundled rather than fetched on first launch because `playwright
install` downloads from a CDN that is blocked in some regions; a release that
depended on it would install but never extract.

## Telegram bot

The bot is not part of the tray app: it needs a token and a chat allowlist, so
it stays a foreground process. From the install folder:

```powershell
$env:TELEGRAM_BOT_TOKEN = "123456:ABC..."
$env:TELEGRAM_CHAT_ID   = "your-chat-id"
python\Scripts\python.exe -I run_bot.py
```
