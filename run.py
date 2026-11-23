"""Dev entry point: `python run.py` serves the web app on :8099."""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import uvicorn

if __name__ == "__main__":
    host = os.environ.get("CSE_HOST", "127.0.0.1")
    port = int(os.environ.get("CSE_PORT", "8099"))
    sys.stdout.reconfigure(encoding="utf-8")
    print(f"dashboard: http://{host}:{port}", flush=True)
    uvicorn.run("app.main:app", host=host, port=port, log_level="info")
