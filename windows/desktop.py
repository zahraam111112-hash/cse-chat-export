"""Run the exporter as a local desktop app: serve on loopback, open the browser.

Started by windows/start.ps1. The dashboard and the download endpoints are
still HTTP on 127.0.0.1, but there is nothing to deploy: the process, the
SQLite file and the browser all live on this machine.
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
import urllib.request
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

HOST = "127.0.0.1"


def _data_dir() -> str:
    # State lives outside the install directory so reinstalling or unpacking a
    # newer release never touches the job history.
    explicit = os.environ.get("CSE_DATA")
    if explicit:
        return explicit
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "ChatGPTShareExporter", "data")


def _free_port(start: int = 8099) -> int:
    # A second copy (or another dev server) must not fight over 8099.
    for port in range(start, start + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind((HOST, port))
            except OSError:
                continue
            return port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((HOST, 0))
        return int(sock.getsockname()[1])


def _open_when_ready(url: str, server: object, timeout: float = 90.0) -> None:
    # Opening the tab before uvicorn binds gives the user an error page and a
    # dead URL, so wait for a real 200 from /healthz first.
    deadline = time.time() + timeout
    probe = url.rstrip("/") + "/healthz"
    while time.time() < deadline:
        if getattr(server, "started", False):
            try:
                with urllib.request.urlopen(probe, timeout=2):
                    pass
                break
            except OSError:
                pass
        time.sleep(0.25)
    if os.environ.get("CSE_NO_BROWSER") != "1":
        webbrowser.open(url)


def main() -> int:
    # A Windows console is cp1252 by default, which cannot encode the box-drawing
    # characters the dashboard log can emit; without this the first one raises.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    if not os.environ.get("CSE_DB"):
        os.environ["CSE_DB"] = os.path.join(_data_dir(), "app.db")
    os.environ["CSE_HOST"] = HOST

    import uvicorn

    port = int(os.environ.get("CSE_PORT") or _free_port())
    url = f"http://{HOST}:{port}/"
    server = uvicorn.Server(uvicorn.Config("app.main:app", host=HOST, port=port, log_level="info"))

    threading.Thread(target=_open_when_ready, args=(url, server), daemon=True).start()
    print(f"ChatGPT Share Exporter -> {url}", flush=True)
    print(f"jobs database: {os.environ['CSE_DB']}", flush=True)
    print("Ctrl+C stops the app.", flush=True)
    try:
        server.run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
