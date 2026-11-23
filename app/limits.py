"""Per-client throttling for the endpoints that cost real CPU.

Each accepted link spawns a headless Chromium for a few seconds, so the
expensive operations are submissions, not reads. The limiter is deliberately
in-process: one uvicorn worker, so a dict of counters is authoritative, and it
keeps working when nothing (no proxy, no CDN) sits in front of the app.
"""

from __future__ import annotations

import os
import threading
import time

# 20 submissions per 60 s per address: a person submitting links by hand never
# comes close, while a script cannot queue thousands of Chromium runs.
WINDOW = 60.0
MAX_PER_WINDOW = int(os.environ.get("CSE_RATE_MAX", "20"))

# The pool runs CSE_WORKERS Chromium at once; beyond this backlog a submission
# is refused rather than accepted and left waiting for hours.
MAX_QUEUE = int(os.environ.get("CSE_MAX_QUEUE", "60"))

_lock = threading.Lock()
_hits: dict[str, list[float]] = {}


def _prune(now: float) -> None:
    # Addresses that stopped calling would otherwise accumulate for ever.
    for key in [k for k, ts in _hits.items() if not ts or ts[-1] < now - WINDOW]:
        _hits.pop(key, None)


def hit(key: str, cost: int = 1) -> bool:
    """Record `cost` submissions for `key`; False when the budget is spent."""
    now = time.monotonic()
    with _lock:
        _prune(now)
        ts = [t for t in _hits.get(key, []) if t > now - WINDOW]
        if len(ts) + cost > MAX_PER_WINDOW:
            _hits[key] = ts
            return False
        ts.extend([now] * cost)
        _hits[key] = ts
        return True


def reset() -> None:
    """Test hook: forget every counter."""
    with _lock:
        _hits.clear()
