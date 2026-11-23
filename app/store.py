"""SQLite-backed job store shared by the web UI and the Telegram bot."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Any, Iterator

DB_PATH = os.environ.get("CSE_DB", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "app.db"))

# VACUUM rewrites the whole file and blocks every other writer while it runs,
# so it must never sit inside a delete request. Tests turn it off entirely.
VACUUM_ENABLED = os.environ.get("CSE_VACUUM", "1") != "0"
EVENT_KEEP = 5000
_vacuum_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    status      TEXT NOT NULL,
    source      TEXT NOT NULL DEFAULT 'web',
    chat_id     TEXT,
    urls        TEXT NOT NULL,
    error       TEXT,
    transcripts TEXT
);
CREATE TABLE IF NOT EXISTS users (
    chat_id     TEXT PRIMARY KEY,
    username    TEXT,
    first_seen  REAL NOT NULL,
    last_seen   REAL NOT NULL,
    runs        INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          REAL NOT NULL,
    kind        TEXT NOT NULL,
    detail      TEXT
);
"""

STATUSES = ("queued", "running", "done", "failed")


def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


@contextmanager
def conn() -> Iterator[sqlite3.Connection]:
    c = _connect()
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init() -> None:
    with conn() as c:
        c.executescript(SCHEMA)
        # Older rows predate the column; every job extracted exactly one link,
        # so backfilling 1/1 makes them report their progress truthfully.
        cols = {r["name"] for r in c.execute("PRAGMA table_info(jobs)")}
        if "done_count" not in cols:
            c.execute("ALTER TABLE jobs ADD COLUMN done_count INTEGER NOT NULL DEFAULT 0")
            c.execute("UPDATE jobs SET done_count=1 WHERE status IN ('done','failed')")
        if "url_count" not in cols:
            c.execute("ALTER TABLE jobs ADD COLUMN url_count INTEGER NOT NULL DEFAULT 1")
            # Old batches held several links in one row; the real count is in
            # the urls JSON, so the bar does not claim 1/1 for a 4-link job.
            c.execute("UPDATE jobs SET url_count=json_array_length(urls)"
                      " WHERE json_valid(urls) AND json_array_length(urls) > 0")
            # A finished old job finished all of its links, whatever the row
            # once held — otherwise a done 4-link job would show 1/4 for ever.
            c.execute("UPDATE jobs SET done_count=url_count"
                      " WHERE status IN ('done','failed')")
        if "batch" not in cols:
            c.execute("ALTER TABLE jobs ADD COLUMN batch TEXT")
        if "owner" not in cols:
            # Rows from before there was more than one visitor belong to nobody
            # in particular; only the admin view shows them.
            c.execute("ALTER TABLE jobs ADD COLUMN owner TEXT")


def create_job(urls: list[str], source: str = "web", chat_id: str | None = None,
               status: str = "queued", batch: str | None = None,
               owner: str | None = None) -> str:
    # The bot inserts 'running': it extracts the job itself, and a row that
    # starts queued is claimed by the web pool first — two Chromiums, one job.
    job_id = uuid.uuid4().hex[:12]
    now = time.time()
    with conn() as c:
        c.execute(
            "INSERT INTO jobs (id, created_at, updated_at, status, source, chat_id,"
            " urls, url_count, batch, owner) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (job_id, now, now, status, source, chat_id, json.dumps(urls), len(urls), batch, owner),
        )
    log_event("job.created", f"{job_id} source={source} urls={len(urls)}")
    return job_id

def create_jobs(urls: list[str], source: str = "web", chat_id: str | None = None,
                status: str = "queued", owner: str | None = None) -> tuple[str, list[str]]:
    """One job per link, all sharing a batch id.

    A batch used to be a single row: every link's text landed in one file and
    nothing showed until the slowest link finished. Splitting the row is what
    lets each link arrive on its own, with its own Word/MD/JSON download.
    """
    batch = uuid.uuid4().hex[:12]
    return batch, [create_job([u], source=source, chat_id=chat_id, status=status,
                              batch=batch, owner=owner) for u in urls]

def progress(job_id: str, done: int) -> None:
    """Record how many links of this job have finished, for the progress bar."""
    with conn() as c:
        c.execute("UPDATE jobs SET done_count=?, updated_at=? WHERE id=?",
                  (done, time.time(), job_id))

def batch_jobs(batch: str) -> list[dict[str, Any]]:
    with conn() as c:
        rows = c.execute(
            "SELECT id, status, error, done_count, url_count FROM jobs"
            " WHERE batch=? ORDER BY created_at ASC", (batch,)
        ).fetchall()
    return [dict(r) for r in rows]


def update_job(job_id: str, **fields: Any) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [time.time(), job_id]
    with conn() as c:
        c.execute(f"UPDATE jobs SET {cols}, updated_at=? WHERE id=?", vals)


def claim_job(job_id: str) -> bool:
    """Take ownership of a queued job. Only one caller can win."""
    with conn() as c:
        cur = c.execute(
            "UPDATE jobs SET status='running', updated_at=? WHERE id=? AND status='queued'",
            (time.time(), job_id),
        )
        return cur.rowcount > 0



def requeue_job(job_id: str) -> bool:
    """Put a failed, partly failed or orphaned job back in the queue.

    The old error goes with the old transcript: `run_job` reads the error as the
    batch's rejected links, so leaving it would replay it as a failure of the
    retry and the job could never recover from partial.
    """
    with conn() as c:
        cur = c.execute(
            "UPDATE jobs SET status='queued', transcripts=NULL, error=NULL, done_count=0,"
            " updated_at=? WHERE id=? AND status IN ('failed','done','running')",
            (time.time(), job_id),
        )
        return cur.rowcount > 0


def job_exists(job_id: str) -> bool:
    with conn() as c:
        return c.execute("SELECT 1 FROM jobs WHERE id=?", (job_id,)).fetchone() is not None


def get_job(job_id: str) -> dict[str, Any] | None:
    with conn() as c:
        row = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        return None
    job = dict(row)
    job["urls"] = json.loads(job["urls"])
    job["transcripts"] = json.loads(job["transcripts"]) if job["transcripts"] else None
    return job


def recent_jobs(limit: int = 50, q: str | None = None, status: str | None = None,
                source: str | None = None, owner: str | None = None) -> list[dict[str, Any]]:
    # The transcripts column holds the titles and every message, so a plain
    # LIKE over its text finds both. json_extract would be tidier but it
    # raises on malformed JSON, and one bad row must not blank the table.
    clause, params = _filters(q, status, source, owner)
    with conn() as c:
        rows = c.execute(
            "SELECT j.id, j.created_at, j.status, j.source, j.urls, j.error,"
            " json_array_length(j.transcripts) AS conversations,"
            " (SELECT json_group_array(json_extract(e.value, '$.title'))"
            "    FROM json_each(j.transcripts) AS e) AS titles"
            " FROM jobs AS j" + clause + " ORDER BY j.created_at DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["urls"] = json.loads(item["urls"])
        # json_* return NULL on no or malformed JSON — SQLite does not raise, so a
        # half-written transcript must not break the whole list.
        try:
            item["titles"] = [t for t in json.loads(item["titles"]) if t] if item["titles"] else []
        except (TypeError, ValueError):
            item["titles"] = []
        item["conversations"] = item["conversations"] or 0
        item["title"] = item["titles"][0] if item["titles"] else None
        out.append(item)
    return out


def _filters(q: str | None, status: str | None, source: str | None,
             owner: str | None = None) -> tuple[str, list[Any]]:
    where: list[str] = []
    params: list[Any] = []
    # None means "no ownership restriction" (the admin view). A visitor always
    # passes their own key, so they never see a row that is not theirs.
    if owner is not None:
        where.append("owner=?")
        params.append(owner)
    if q:
        needle = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        where.append(
            "id LIKE ? ESCAPE '\\' OR urls LIKE ? ESCAPE '\\'"
            " OR transcripts LIKE ? ESCAPE '\\'"
        )
        params += [needle, needle, needle]
    if status == "partial":
        where.append("status='done' AND error IS NOT NULL")
    elif status == "done":
        where.append("status='done' AND error IS NULL")
    elif status:
        where.append("status=?")
        params.append(status)
    if source:
        where.append("source=?")
        params.append(source)
    return (" WHERE " + " AND ".join(f"({w})" for w in where)) if where else "", params


def count_jobs(q: str | None = None, status: str | None = None,
               source: str | None = None, owner: str | None = None) -> int:
    clause, params = _filters(q, status, source, owner)
    with conn() as c:
        return c.execute("SELECT COUNT(*) n FROM jobs" + clause, params).fetchone()["n"]


def delete_job(job_id: str) -> bool:
    """Drop a job and its transcript, then reclaim the space it held."""
    with conn() as c:
        cur = c.execute("DELETE FROM jobs WHERE id=?", (job_id,))
        deleted = cur.rowcount > 0
    if deleted:
        log_event("job.deleted", job_id)
        _vacuum_soon()
    return deleted


def _vacuum_soon() -> None:
    # DELETE only frees pages inside the file; VACUUM hands them back to disk.
    # Run it off the request path: on a large database it takes seconds and
    # blocks every other writer while it does.
    if not VACUUM_ENABLED:
        return

    def run() -> None:
        if not _vacuum_lock.acquire(blocking=False):
            return  # a vacuum is already running; it will compact this delete too
        try:
            with conn() as c:
                c.execute("VACUUM")
        except sqlite3.OperationalError:
            pass
        finally:
            _vacuum_lock.release()

    threading.Thread(target=run, daemon=True).start()


def queued_count(owner: str | None = None) -> int:
    """Jobs waiting for a worker, optionally only for one owner."""
    sql = "SELECT COUNT(*) n FROM jobs WHERE status='queued'"
    params: list[Any] = []
    if owner is not None:
        sql += " AND owner=?"
        params.append(owner)
    with conn() as c:
        return c.execute(sql, params).fetchone()["n"]


def owner_of(job_id: str) -> str | None:
    with conn() as c:
        row = c.execute("SELECT owner FROM jobs WHERE id=?", (job_id,)).fetchone()
    return row["owner"] if row else None


def pending_jobs() -> list[str]:
    with conn() as c:
        rows = c.execute(
            "SELECT id FROM jobs WHERE status='queued' ORDER BY created_at ASC"
        ).fetchall()
    return [r["id"] for r in rows]


def orphaned_jobs() -> list[str]:
    """Jobs a previous process was running when it stopped.

    Nothing resumes these by itself, so without this they show 'running'
    for ever and the dashboard polls them until it times out.
    """
    with conn() as c:
        rows = c.execute(
            "SELECT id FROM jobs WHERE status='running' ORDER BY created_at ASC"
        ).fetchall()
    return [r["id"] for r in rows]


def touch_user(chat_id: str, username: str | None) -> None:
    now = time.time()
    with conn() as c:
        c.execute(
            "INSERT INTO users (chat_id, username, first_seen, last_seen, runs)"
            " VALUES (?,?,?,?,0) ON CONFLICT(chat_id) DO UPDATE SET"
            " username=excluded.username, last_seen=excluded.last_seen",
            (chat_id, username, now, now),
        )


def count_run(chat_id: str) -> None:
    with conn() as c:
        c.execute("UPDATE users SET runs=runs+1, last_seen=? WHERE chat_id=?", (time.time(), chat_id))


def stats() -> dict[str, Any]:
    with conn() as c:
        rows = c.execute("SELECT status, error, transcripts FROM jobs").fetchall()
        users = c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
    total = len(rows)
    done = sum(1 for r in rows if r["status"] == "done")
    failed = sum(1 for r in rows if r["status"] == "failed")
    # Some links in a batch failed but the rest were extracted: the job carries
    # both output and an error, and it is not the same thing as "done".
    partial = sum(1 for r in rows if r["status"] == "done" and r["error"])
    chars = 0
    for r in rows:
        if r["status"] != "done":
            continue
        try:
            chars += sum(t.get("chars", 0) for t in json.loads(r["transcripts"]))
        except (TypeError, ValueError):
            continue
    return {
        "jobs": total,
        "done": done - partial,
        "partial": partial,
        "failed": failed,
        "queued": max(total - done - failed, 0),  # done includes partial
        "users": users,
        "chars": chars,
    }


def log_event(kind: str, detail: str = "") -> None:
    with conn() as c:
        c.execute(
            "INSERT INTO events (at, kind, detail) VALUES (?,?,?)",
            (time.time(), kind, detail),
        )
        # Only the newest events are ever shown, so nothing is lost by trimming
        # the tail — otherwise the table grows without bound.
        c.execute(
            "DELETE FROM events WHERE id <= (SELECT MAX(id) FROM events) - ?",
            (EVENT_KEEP,),
        )


def recent_events(limit: int = 30) -> list[dict[str, Any]]:
    with conn() as c:
        rows = c.execute(
            "SELECT at, kind, detail FROM events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]
