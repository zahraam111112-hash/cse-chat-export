"""Drive a Chromium engine for the two jobs the exporters need.

Extraction renders a share page and reads its HTML; the PDF export prints
HTML. Both used to need the Chromium that `playwright install chromium`
downloads — 272 MB of headless shell plus 433 MB of full Chromium, and the
113 MB Playwright package that launches them. On a metered connection that is
the whole cost of the app, for a program whose own code is under a megabyte.

Every Windows 10 and 11 machine already has Microsoft Edge, and Edge *is*
Chromium: same DevTools protocol, same request blocking, same PDF printer.
So the release carries neither a browser nor Playwright, and this module
drives whichever Chromium the machine already has:

* Playwright, when it happens to be installed — that is how the server runs,
  and it keeps that behaviour exactly.
* Otherwise the DevTools protocol over a WebSocket, against Edge, Chrome, or
  any Chromium named by `CSE_BROWSER`.

Nothing here renders anything by itself; the two backends only differ in how
they start a browser and talk to it.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# The DOM only carries the conversation once the server-rendered stream has
# been parsed; the page then re-navigates while its client bundle hydrates.
STATE_KEY = "conversation-state"

# A4 in inches, and the margins the PDF stylesheet asks for (18 mm / 15 mm).
# printToPDF wants them in inches and does not read the CSS.
PAPER_W, PAPER_H = 8.27, 11.69
MARGIN_TB, MARGIN_LR = 18 / 25.4, 15 / 25.4
FOOTER = (
    '<div style="font-size:8px;color:#888;width:100%;'
    'padding:0 15mm;display:flex;justify-content:space-between;">'
    "<span>ChatGPT Share Exporter</span>"
    '<span class="pageNumber"></span>/<span class="totalPages"></span>'
    "</div>"
)


class BrowserError(RuntimeError):
    """No Chromium could be started, or it stopped answering."""


class BrowserMissing(BrowserError):
    """This machine has no Chromium this module can drive."""


# --------------------------------------------------------------------------
# which engine
# --------------------------------------------------------------------------

_WINDOWS = [
    r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
    r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
    r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
]

_POSIX = [
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/microsoft-edge",
    "/snap/bin/chromium",
]


def find_browser() -> str:
    """The Chromium executable to drive, or raise `BrowserMissing`."""
    override = os.environ.get("CSE_BROWSER")
    if override:
        if os.path.isfile(override):
            return override
        raise BrowserMissing(
            f"CSE_BROWSER points at {override}, which is not a file."
        )

    # A portable Chromium dropped beside the app still wins: somebody who put
    # one there meant it to be used.
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for name in ("chrome.exe", "chromium.exe",
                 os.path.join("chrome-win", "chrome.exe")):
        bundled = os.path.join(root, "chromium", name)
        if os.path.isfile(bundled):
            return bundled

    for candidate in (_WINDOWS if sys.platform.startswith("win") else _POSIX):
        path = os.path.expandvars(candidate)
        if os.path.isfile(path):
            return path

    raise BrowserMissing(
        "No Chromium-based browser was found. Microsoft Edge ships with "
        "Windows and Google Chrome works too; install either one, or set "
        "CSE_BROWSER to the path of a Chromium executable."
    )


def _have_playwright() -> bool:
    if os.environ.get("CSE_NO_PLAYWRIGHT") == "1":
        return False
    try:
        import playwright.sync_api  # noqa: F401
    except Exception:  # noqa: BLE001 - any import failure means "not usable"
        return False
    return True


def engine() -> str:
    """A short name for whichever backend `render()` would use."""
    if _have_playwright():
        return "playwright"
    return find_browser()


# --------------------------------------------------------------------------
# the DevTools-protocol backend
# --------------------------------------------------------------------------


class _Idle(Exception):
    """No message arrived yet; the caller should service its own queue."""


class _Session:
    """One page target inside a headless Chromium, spoken to over CDP."""

    def __init__(self, ws: Any) -> None:
        self._ws = ws
        self._next_id = 0
        self._page: str | None = None
        self._paused: list[tuple[Any, str, str]] = []

    # -- plumbing ---------------------------------------------------------

    def send(self, method: str, params: dict | None = None,
             session: str | None = None, timeout: float = 60.0) -> dict:
        self._next_id += 1
        self._write(self._next_id, method, params, session)
        return self._await(self._next_id, timeout)

    def _write(self, mid: int, method: str, params: dict | None,
               session: str | None) -> None:
        message: dict[str, Any] = {"id": mid, "method": method}
        if params:
            message["params"] = params
        target = session if session is not None else self._page
        if target:
            message["sessionId"] = target
        self._ws.send(json.dumps(message))

    def _await(self, mid: int, timeout: float) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            self._flush()
            try:
                message = self._read(deadline)
            except _Idle:
                continue
            if message.get("id") == mid:
                error = message.get("error")
                if error:
                    raise BrowserError(str(error.get("message") or error))
                return message.get("result") or {}
            self._handle(message)

    def _read(self, deadline: float) -> dict:
        left = deadline - time.monotonic()
        if left <= 0:
            raise BrowserError("the browser stopped answering")
        try:
            raw = self._ws.recv(timeout=min(left, 1.0))
        except TimeoutError:
            raise _Idle from None
        except Exception as exc:  # noqa: BLE001 - a closed socket is fatal here
            raise BrowserError(f"lost the browser connection: {exc}") from exc
        return json.loads(raw)

    def _handle(self, message: dict) -> None:
        if message.get("method") == "Fetch.requestPaused":
            params = message.get("params") or {}
            self._paused.append((params.get("requestId"),
                                 str(params.get("resourceType") or "").lower(),
                                 message.get("sessionId") or ""))

    def pump(self, seconds: float) -> None:
        """Wait, but keep answering the browser.

        A request paused by `Fetch` waits for this side to continue it, so a
        plain `sleep` here stalls the page — the navigation the extractor is
        waiting for would never arrive.
        """
        deadline = time.monotonic() + seconds
        while True:
            self._flush()
            left = deadline - time.monotonic()
            if left <= 0:
                return
            try:
                self._handle(self._read(deadline))
            except _Idle:
                continue
            except BrowserError:
                return

    def _flush(self) -> None:
        # Answered without waiting for their replies: a paused request is
        # answered on the next turn of whichever loop is running, never from
        # inside the read that discovered it. Replying inline made a page with
        # many requests in flight recurse once per request — a real share page
        # blew the stack before its document had even arrived.
        while self._paused:
            request_id, kind, session = self._paused.pop(0)
            if not request_id:
                continue
            self._next_id += 1
            if kind in ("image", "font", "media"):
                # Interception happens before the network stack, so this is
                # where a picture or a web font is refused: the extractor only
                # needs the server-rendered HTML, and downloading media costs
                # a user data they did not ask to spend.
                self._write(self._next_id, "Fetch.failRequest",
                            {"requestId": request_id,
                             "errorReason": "BlockedByClient"}, session)
            else:
                self._write(self._next_id, "Fetch.continueRequest",
                            {"requestId": request_id}, session)

    # -- page ---------------------------------------------------------------

    def open_page(self, user_agent: str, locale: str = "en-US") -> None:
        target = self.send("Target.createTarget", {"url": "about:blank"},
                           session="")
        self._page = self.send(
            "Target.attachToTarget",
            {"targetId": target["targetId"], "flatten": True},
            session="",
        )["sessionId"]

        self.send("Page.enable")
        self.send("Runtime.enable")
        self.send("Emulation.setUserAgentOverride",
                  {"userAgent": user_agent, "acceptLanguage": "en-US,en;q=0.9"})
        try:
            self.send("Emulation.setLocaleOverride", {"locale": locale}, timeout=10)
        except BrowserError:
            # The override is cosmetic; a locale the build does not carry must
            # not stop a render.
            pass
        self.send("Emulation.setDeviceMetricsOverride",
                  {"width": 1280, "height": 720, "deviceScaleFactor": 1,
                   "mobile": False})
        self.send("Fetch.enable", {"patterns": [{"urlPattern": "*"}]})

    def navigate(self, url: str, timeout_ms: int) -> None:
        result = self.send("Page.navigate", {"url": url},
                           timeout=timeout_ms / 1000.0)
        if result.get("errorText"):
            raise BrowserError(f"the page did not load: {result['errorText']}")

    def evaluate(self, expression: str, timeout: float = 5.0,
                 await_promise: bool = False) -> Any:
        result = self.send(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True,
             "awaitPromise": await_promise},
            timeout=timeout,
        )
        if result.get("exceptionDetails"):
            raise BrowserError("the page could not be read")
        return (result.get("result") or {}).get("value")

    def content(self) -> str:
        return self.evaluate(
            "document.documentElement ? document.documentElement.outerHTML : ''"
        ) or ""

    def ready(self) -> bool:
        return self.evaluate(
            "document.readyState === 'interactive' "
            "|| document.readyState === 'complete'"
        ) is True

    def title(self) -> str:
        try:
            return str(self.evaluate("document.title") or "")
        except BrowserError:
            return ""

    def set_content(self, html: str) -> None:
        tree = self.send("Page.getFrameTree")
        frame = ((tree.get("frameTree") or {}).get("frame") or {}).get("id")
        if not frame:
            raise BrowserError("the browser opened no frame")
        self.send("Page.setDocumentContent", {"frameId": frame, "html": html})
        # Nothing is fetched — the stylesheet is inline — but the document
        # still has to lay out and resolve its system fonts before printing.
        # A page that never settles must not hang the request, hence the cap.
        self.evaluate(
            "new Promise(r => {"
            "  const go = () => requestAnimationFrame(() => requestAnimationFrame(r));"
            "  (document.fonts ? document.fonts.ready : Promise.resolve()).then(go, go);"
            "})",
            timeout=20,
            await_promise=True,
        )

    def print_pdf(self, timeout: float = 120.0) -> bytes:
        result = self.send("Page.printToPDF", {
            "printBackground": True,
            "paperWidth": PAPER_W,
            "paperHeight": PAPER_H,
            "marginTop": MARGIN_TB,
            "marginBottom": MARGIN_TB,
            "marginLeft": MARGIN_LR,
            "marginRight": MARGIN_LR,
            "displayHeaderFooter": True,
            "headerTemplate": "<span></span>",
            "footerTemplate": FOOTER,
            "preferCSSPageSize": False,
        }, timeout=timeout)
        data = result.get("data")
        if not data:
            raise BrowserError("the browser returned no PDF")
        return base64.b64decode(data)

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:  # noqa: BLE001 - closing is best-effort
            pass


class _Chromium:
    """A headless Chromium process plus the session that talks to it."""

    def __init__(self, exe: str, proxy: str | None = None) -> None:
        self.exe = exe
        self.proxy = proxy
        self.proc: subprocess.Popen | None = None
        self.session: _Session | None = None
        self.profile = ""
        self._log = None

    def __enter__(self) -> _Session:
        self._start()
        assert self.session is not None
        return self.session

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _start(self) -> None:
        from websockets.sync.client import connect

        self.profile = tempfile.mkdtemp(prefix="cse-chromium-")
        args = [
            self.exe,
            "--headless=new",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--disable-sync",
            "--disable-background-networking",
            "--disable-component-update",
            "--disable-features=Translate,MediaRouter,OptimizationHints",
            "--mute-audio",
            "--hide-scrollbars",
            f"--user-data-dir={self.profile}",
            "--remote-debugging-port=0",
            "about:blank",
        ]
        if self.proxy:
            args.insert(1, f"--proxy-server={self.proxy}")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self._log = open(os.path.join(self.profile, "stderr.log"), "wb")
        try:
            self.proc = subprocess.Popen(args, stdin=subprocess.DEVNULL,
                                         stdout=subprocess.DEVNULL,
                                         stderr=self._log, creationflags=flags)
        except OSError as exc:
            raise BrowserError(f"could not start {self.exe}: {exc}") from exc

        port, path = self._debugging_port()
        ws = connect(f"ws://127.0.0.1:{port}{path}", open_timeout=30,
                     max_size=None, close_timeout=5)
        self.session = _Session(ws)

    def _debugging_port(self) -> tuple[int, str]:
        marker = os.path.join(self.profile, "DevToolsActivePort")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            assert self.proc is not None
            if self.proc.poll() is not None:
                raise BrowserError(
                    f"{os.path.basename(self.exe)} exited at once"
                    + self._log_tail()
                )
            try:
                with open(marker, "r", encoding="utf-8") as handle:
                    lines = handle.read().splitlines()
            except OSError:
                lines = []
            if len(lines) >= 2 and lines[0].strip().isdigit():
                return int(lines[0]), lines[1].strip()
            time.sleep(0.05)
        raise BrowserError("the browser exposed no debugging port in 30 s")

    def _log_tail(self) -> str:
        try:
            if self._log:
                self._log.flush()
            with open(os.path.join(self.profile, "stderr.log"), "rb") as handle:
                text = handle.read()[-800:].decode("utf-8", "replace").strip()
            return f": {text}" if text else ""
        except OSError:
            return ""

    def close(self) -> None:
        if self.session is not None:
            try:
                self.session.send("Browser.close", session="", timeout=2)
            except Exception:  # noqa: BLE001 - closing is best-effort
                pass
            self.session.close()
            self.session = None
        if self.proc is not None:
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
            self.proc = None
        if self._log is not None:
            try:
                self._log.close()
            except OSError:
                pass
            self._log = None
        # The profile holds the browser's cache; leaving it behind would litter
        # %TEMP% with tens of megabytes per job.
        shutil.rmtree(self.profile, ignore_errors=True)


# --------------------------------------------------------------------------
# the operations both backends provide
# --------------------------------------------------------------------------


class _CDPPage:
    """The handful of page operations `_settled` needs, over CDP."""

    def __init__(self, session: _Session) -> None:
        self._session = session

    def ready(self) -> bool:
        return self._session.ready()

    def content(self) -> str:
        return self._session.content()

    def title(self) -> str:
        return self._session.title()

    def sleep(self, ms: int) -> None:
        # Waits must keep serving the browser: a paused request would
        # otherwise freeze the very navigation this loop is waiting for.
        self._session.pump(ms / 1000.0)


class _PWPage:
    def __init__(self, page: Any) -> None:
        self._page = page

    def ready(self) -> bool:
        self._page.wait_for_load_state("domcontentloaded", timeout=5_000)
        return True

    def content(self) -> str:
        return self._page.content()

    def title(self) -> str:
        return self._page.title()

    def sleep(self, ms: int) -> None:
        self._page.wait_for_timeout(ms)


def _settled(page: Any, deadline: float) -> str:
    """Read the page once it has stopped moving.

    The share page re-navigates while its client bundle hydrates (full load,
    then a client-side re-render). A read taken during one of those either
    raises or returns a half-built document — the failure users saw. So a
    failed read is retried rather than propagated, and the page is only
    accepted once it holds the conversation state, or once two reads agree on
    its absence (a genuinely expired link).
    """
    last, html = None, ""
    while time.time() < deadline:
        try:
            if not page.ready():
                page.sleep(400)
                continue
            html = page.content()
        except Exception:  # noqa: BLE001 - a navigation in flight is expected
            last = None
            page.sleep(400)
            continue
        if STATE_KEY in html:
            # The state is only rendered once the server has sent the whole
            # conversation, so a stable read that has it is final. Waiting for
            # a second identical read here cost ~1 s on every job.
            return html
        if html == last:
            return html  # two quiet reads without it: treat the page as final
        last = html
        page.sleep(600)
    return html


def _launch_args(proxy: str | None) -> dict:
    launch: dict[str, Any] = {"headless": True, "args": ["--no-sandbox"]}
    if proxy:
        launch["proxy"] = {"server": proxy}
    return launch


def _render_playwright(url: str, timeout_ms: int, proxy: str | None,
                       user_agent: str) -> tuple[str, str]:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(**_launch_args(proxy))
        try:
            ctx = browser.new_context(locale="en-US", user_agent=user_agent)
            page = ctx.new_page()
            page.route(
                "**/*",
                lambda r: r.abort()
                if r.request.resource_type in ("image", "font", "media")
                else r.continue_(),
            )
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            html = _settled(_PWPage(page), time.time() + 40)
            try:
                title = page.title()
            except Exception:  # noqa: BLE001 - title is cosmetic
                title = ""
            return html, title
        finally:
            browser.close()


def _render_cdp(url: str, timeout_ms: int, proxy: str | None,
                user_agent: str) -> tuple[str, str]:
    with _Chromium(find_browser(), proxy) as session:
        session.open_page(user_agent)
        session.navigate(url, timeout_ms)
        page = _CDPPage(session)
        html = _settled(page, time.time() + 40)
        return html, page.title()


def render(url: str, timeout_ms: int = 90_000, proxy: str | None = None,
           user_agent: str = UA) -> tuple[str, str]:
    """Load `url` in a headless Chromium and return (html, title)."""
    if _have_playwright():
        return _render_playwright(url, timeout_ms, proxy, user_agent)
    return _render_cdp(url, timeout_ms, proxy, user_agent)


def _pdf_playwright(html: str, proxy: str | None) -> bytes:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(**_launch_args(proxy))
        try:
            page = browser.new_page()
            page.set_content(html, wait_until="load")
            # Range requests and print paths need the fonts resolved first;
            # the markup has no images but a conversation can embed any.
            return page.pdf(
                format="A4",
                print_background=True,
                margin={"top": "18mm", "bottom": "18mm",
                        "left": "15mm", "right": "15mm"},
                display_header_footer=True,
                header_template="<span></span>",
                footer_template=FOOTER,
            )
        finally:
            browser.close()


def _pdf_cdp(html: str, proxy: str | None) -> bytes:
    with _Chromium(find_browser(), proxy) as session:
        # A page target is needed before a document can be set on it, but
        # nothing is fetched: the report is a string in memory.
        session.open_page(UA)
        session.set_content(html)
        return session.print_pdf()


def print_pdf(html: str, proxy: str | None = None) -> bytes:
    """Print one HTML document to PDF with the same engine `render()` uses."""
    if _have_playwright():
        return _pdf_playwright(html, proxy)
    return _pdf_cdp(html, proxy)
