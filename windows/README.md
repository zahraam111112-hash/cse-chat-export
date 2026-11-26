# Windows build

The app ships as a self-contained Windows folder: a Python runtime, the
dependencies and a launcher. Nothing is installed system-wide and no
administrator rights are needed.

```
start.cmd                  double-click entry point
windows\start.ps1          picks the interpreter and runs the app
windows\desktop.py         serves on 127.0.0.1 and opens the browser
python\python.exe          bundled interpreter and dependencies
app\                       the application
```

`start.cmd` also works in a plain developer checkout: it falls back to `.venv\`
and then to the system `python`, so a contributor can run the same entry point
after `pip install -r requirements-windows.txt`.

## The browser it drives

No browser is bundled. Extraction and PDF export run in headless Chromium, and
the app drives the one Windows already has — Microsoft Edge — so the download
stays small and nothing is fetched from a CDN at install time or on first
launch. Google Chrome is used if Edge is missing.

The engine is chosen at startup and printed in the console window:

```
browser: C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe
```

If nothing is found the line says so and extraction reports the same thing,
rather than failing later with an error about a missing executable.

To use a specific executable instead, set `CSE_BROWSER` to its full path. A path
that does not exist is an error rather than a silent fallback, so a wrong value
is obvious instead of mysterious.

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

`.github/workflows/release.yml` runs the test suite, assembles the payload from
`requirements-windows.txt`, and attaches `ChatGPT-Share-Exporter-Setup.exe` and
`ChatGPT-Share-Exporter-Windows.zip` to the GitHub Release.

## Telegram bot

The bot is not part of the tray app: it needs a token and a chat allowlist, so
it stays a foreground process. From the install folder:

```powershell
$env:TELEGRAM_BOT_TOKEN = "123456:ABC..."
$env:TELEGRAM_CHAT_ID   = "your-chat-id"
python\python.exe -I run_bot.py
```
