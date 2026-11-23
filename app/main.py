"""Web app: REST API + dashboard for the ChatGPT share exporter."""

from __future__ import annotations

import io
import ipaddress
import json
import os
import threading
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from . import auth, docx_export, limits, pdf_export, store
from .extractor import ExtractError, Transcript, extract, normalize_url

BASE = os.path.dirname(os.path.abspath(__file__))
PROXY = os.environ.get("CSE_PROXY") or None

app = FastAPI(title="ChatGPT Share Exporter", version="1.0.0")


@app.middleware("http")
async def require_auth(request: Request, call_next):
    try:
        auth.check(request)
    except HTTPException as exc:
        return Response(
            str(exc.detail),
            status_code=exc.status_code,
            headers=exc.headers or {},
            media_type="text/plain",
        )
    return await call_next(request)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    # The app must be safe on its own too, not only when something else
    # happens to be in front of it.
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("X-Frame-Options", "DENY")
    return response


app.mount("/static", StaticFiles(directory=os.path.join(BASE, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE, "templates"))


def _asset_version() -> str:
    # Browsers cache static/app.* hard. Stamp the URL with the files' newest mtime
    # so a deploy invalidates it without asking users to hard-refresh.
    newest = max(
        (os.path.getmtime(os.path.join(BASE, "static", f)) for f in ("app.css", "app.js")),
        default=0,
    )
    return str(int(newest))

store.init()


class ExportRequest(BaseModel):
    url: str = Field(min_length=8, max_length=400)
    download: bool = True


class MultiRequest(BaseModel):
    urls: list[str] = Field(min_length=1, max_length=20)


TRUSTED_PROXIES = {
    p.strip() for p in os.environ.get("CSE_TRUST_PROXY", "127.0.0.1,::1").split(",")
    if p.strip()
}


def _trusted_peer(addr: str) -> bool:
    """A peer allowed to say who the caller is: the proxy, or anything local.

    Loopback and private ranges are the operator's own networks — the proxy, a
    dev run, a container bridge. A public, random client is never trusted with
    a forwarded header, because that is exactly the impersonation we forbid.
    """
    if not addr:
        return False
    if addr in TRUSTED_PROXIES:
        return True
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        # Not an IP at all: a unix socket or the test client, i.e. a local
        # transport that no remote caller can reach.
        return True
    return ip.is_loopback or ip.is_private


def visitor(request: Request) -> str:
    """Who is asking, as an identity that cannot be forged.

    Behind the reverse proxy the socket address is always the proxy's, so the
    real client has to come from a forwarded header. That header decides which
    jobs a visitor may read, so a peer that is not a trustable proxy is ignored
    when it sends one; a peer that is local or private is taken at its word
    (an operator running the app directly, a container network), which also
    keeps the loopback test client working.
    """
    socket_addr = request.client.host if request.client else ""
    if _trusted_peer(socket_addr):
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            # A proxy appends the address it saw to whatever the client sent,
            # so the trustworthy hop is the LAST one. The leftmost entry is
            # exactly what a caller writes to impersonate someone else.
            return fwd.split(",")[-1].strip()[:64]
        real = request.headers.get("x-real-ip")
        if real:
            return real.strip()[:64]
    return socket_addr or "unknown"


def _admit(key: str, count: int) -> None:
    """Accept a submission, or refuse it without charging the caller.

    The queue ceiling is checked first on purpose: a refused submission never
    runs, so spending rate budget on it would let a caller who is already
    blocked burn their whole window on requests the server rejected.
    """
    # Refusing beats accepting work that would sit in the queue for hours.
    if store.queued_count() + count > limits.MAX_QUEUE:
        raise HTTPException(503, "The queue is full. Try again in a few minutes.")
    if not limits.hit(key, count):
        raise HTTPException(429, f"Too many requests: {limits.MAX_PER_WINDOW} links "
                                 f"per {int(limits.WINDOW)}s. Try again in a minute.")


def _mine_or_404(job_id: str, request: Request) -> dict:
    """Fetch a job, but only if this visitor is allowed to see it."""
    job = store.get_job(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    if not auth.is_admin(request) and job.get("owner") != visitor(request):
        # Same 404 as a missing row: a 403 would confirm the id exists.
        raise HTTPException(404, "job not found")
    return job


def _build_transcripts(raw: list[str]) -> list[Transcript]:
    out: list[Transcript] = []
    for key, item in enumerate(raw):
        out.append(
            Transcript(
                url=item.get("url", ""),
                title=item.get("title") or f"Conversation {key + 1}",
                messages=[
                    _msg(m) for m in item.get("messages", [])
                ],
            )
        )
    return out


def _msg(payload: dict):
    from .extractor import Message

    return Message(role=payload.get("role", "assistant"), content=payload.get("content", ""),
                   id=payload.get("id", ""))


def run_job(job_id: str, claimed: bool = False) -> None:
    # Claiming the row is what makes a job run once: with a plain status update
    # two workers both pass a "not queued yet" check and extract twice. A worker
    # that already won the claim while picking the job must not claim it again —
    # it is 'running' by then and the second claim would lose.
    if not claimed and not store.claim_job(job_id):
        return
    job = store.get_job(job_id)
    if not job:
        return  # deleted between the claim and the read
    results = []
    errors = []
    for n, url in enumerate(job["urls"], 1):
        if not store.job_exists(job_id):
            # Deleted from the dashboard mid-run: stop paying for extra pages.
            return
        try:
            transcript = extract(url, proxy=PROXY)
            results.append(transcript.as_dict())
            store.log_event("job.extracted", f"{job_id} {len(transcript.messages)} msgs")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{url}: {type(exc).__name__}: {exc}")
        # Written after every link, so a batch's progress survives a restart
        # that kills the worker mid-way.
        store.progress(job_id, n)
    if results:
        # A partly-failed batch keeps the rows it did extract, but the error
        # stays on the row so the dashboard can show it as partial, not done.
        store.update_job(
            job_id,
            status="done",
            transcripts=json.dumps(results, ensure_ascii=False),
            error="; ".join(errors) or None,
        )
    else:
        store.update_job(job_id, status="failed", error="; ".join(errors) or "no data")


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    admin = auth.is_admin(request)
    owner = None if admin else visitor(request)
    return templates.TemplateResponse(
        request,
        "index.html",
        {"stats": store.stats(), "jobs": store.recent_jobs(20, owner=owner),
         "events": store.recent_events(15) if admin else [],
         "admin": admin, "asset_v": _asset_version()},
    )


@app.get("/healthz")
def healthz(deep: int = 0):
    # Public (no auth) so a liveness probe can reach it — keep it free of counters.
    if not deep:
        return {"ok": True}
    # `?deep=1` is what the deploy updater calls: it proves the template engine
    # and SQLite both work, without depending on any job rows existing.
    from jinja2 import TemplateNotFound

    template = True
    try:
        templates.get_template("index.html")
    except TemplateNotFound:
        template = False
    db = True
    try:
        store.recent_jobs(1)
    except Exception:  # noqa: BLE001
        db = False
    ok = template and db
    return JSONResponse({"ok": ok, "template": template, "db": db},
                        status_code=200 if ok else 503)


@app.get("/api/stats")
def api_stats(request: Request):
    # Global counters are admin-only: job and user totals are not a visitor's
    # business, and they are a free side-channel for anyone probing the site.
    if not auth.is_admin(request):
        raise HTTPException(403, "admin only")
    return store.stats()


@app.get("/api/jobs")
def api_jobs(request: Request, limit: int = 30, q: str | None = None,
             status: str | None = None, source: str | None = None):
    # X-Total is how many rows match the filters: without it the dashboard
    # cannot tell "these are all of them" from "there are more".
    rf = {"q": (q or "").strip()[:200] or None, "status": status, "source": source}
    if not auth.is_admin(request):
        rf["owner"] = visitor(request)
    jobs = store.recent_jobs(max(1, min(limit, 200)), **rf)
    return JSONResponse(jobs, headers={"X-Total": str(store.count_jobs(**rf))})


@app.get("/api/jobs/{job_id}")
def api_job(job_id: str, request: Request):
    return _mine_or_404(job_id, request)


@app.post("/api/export")
def api_export(payload: ExportRequest, request: Request):
    who = visitor(request)
    try:
        url = normalize_url(payload.url)
    except ExtractError as exc:
        raise HTTPException(400, str(exc)) from exc
    # Charged only after the link is accepted — same rule as the batch route.
    _admit(who, 1)
    job_id = store.create_job([url], source="web", owner=who)
    return {"job_id": job_id, "status": "queued"}


@app.post("/api/batch")
def api_batch(payload: MultiRequest, request: Request):
    # One bad link must not cost the user the good ones: extract what is valid
    # and report the rest back instead of rejecting the whole submission.
    who = visitor(request)
    urls, rejected = [], []
    for raw in payload.urls:
        try:
            urls.append(normalize_url(raw))
        except ExtractError as exc:
            rejected.append({"url": raw, "reason": str(exc)})
    if not urls:
        raise HTTPException(400, "; ".join(f"{r['url']}: {r['reason']}" for r in rejected))
    # Charged only for links that are actually accepted: a rejected link never
    # costs a Chromium run, so it must not spend the caller's budget either.
    _admit(who, len(urls))
    batch, job_ids = store.create_jobs(urls, source="web", owner=who)
    return {"batch": batch, "job_ids": job_ids, "count": len(urls),
            "rejected": rejected, "status": "queued"}

@app.get("/api/batches/{batch}")
def api_batch_progress(batch: str, request: Request):
    rows = store.batch_jobs(batch)
    if not rows:
        raise HTTPException(404, "batch not found")
    if not auth.is_admin(request):
        # Every row of a batch shares one owner, so one check covers the set.
        if store.owner_of(rows[0]["id"]) != visitor(request):
            raise HTTPException(404, "batch not found")
    done = sum(1 for r in rows if r["status"] in ("done", "failed"))
    return {"batch": batch, "total": len(rows), "finished": done, "jobs": rows}


@app.post("/api/jobs/{job_id}/retry")
def api_retry_job(job_id: str, request: Request):
    job = _mine_or_404(job_id, request)
    # A retry costs another Chromium run, so it spends the same budget — but
    # only once it is actually going back in the queue (see the 409s below).
    if job["status"] == "done" and not job["error"]:
        raise HTTPException(409, "job already completed")
    # Only startup may adopt a 'running' row (its worker died with the process);
    # a live one must not be stolen from under its worker.
    if job["status"] == "running":
        raise HTTPException(409, "job is running; wait for it to finish")
    _admit(visitor(request), 1)
    if not store.requeue_job(job_id):
        raise HTTPException(409, f"job is {job['status']}; wait for it to finish")
    return {"job_id": job_id, "status": "queued"}


@app.delete("/api/jobs/{job_id}")
def api_delete_job(job_id: str, request: Request):
    _mine_or_404(job_id, request)
    if not store.delete_job(job_id):
        raise HTTPException(404, "job not found")
    return {"deleted": job_id}


@app.get("/api/jobs/{job_id}/json")
def download_json(job_id: str, request: Request):
    job = _mine_or_404(job_id, request)
    if not job["transcripts"]:
        raise HTTPException(404, "no transcript yet")
    body = json.dumps(job["transcripts"], ensure_ascii=False, indent=2)
    return Response(body, media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{job_id}.json"'})


@app.get("/api/jobs/{job_id}/docx")
def download_docx(job_id: str, request: Request):
    job = _mine_or_404(job_id, request)
    if not job["transcripts"]:
        raise HTTPException(404, "no transcript yet")
    data = docx_export.build(_build_transcripts(job["transcripts"]))
    return Response(
        data,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="chatgpt-{job_id}.docx"'},
    )


@app.get("/api/jobs/{job_id}/pdf")
def download_pdf(job_id: str, request: Request):
    job = _mine_or_404(job_id, request)
    if not job["transcripts"]:
        raise HTTPException(404, "no transcript yet")
    # Printing costs a Chromium launch, so it spends the same rate budget as a
    # submission — otherwise the queue limit would be trivially bypassed.
    if not limits.hit(visitor(request), 1):
        raise HTTPException(429, f"Too many requests: {limits.MAX_PER_WINDOW} links "
                                 f"per {int(limits.WINDOW)}s. Try again in a minute.")
    try:
        data = pdf_export.build(_build_transcripts(job["transcripts"]), proxy=PROXY)
    except Exception as exc:  # noqa: BLE001 - a browser failure is not a 500
        raise HTTPException(503, f"PDF rendering failed: {exc}") from exc
    return Response(
        data,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="chatgpt-{job_id}.pdf"'},
    )


@app.get("/api/jobs/{job_id}/md")
def download_md(job_id: str, request: Request):
    job = _mine_or_404(job_id, request)
    if not job["transcripts"]:
        raise HTTPException(404, "no transcript yet")
    parts = []
    for item in job["transcripts"]:
        parts.append(f"# {item['title']}\n\nSource: {item['url']}\n")
        for n, m in enumerate(item["messages"], 1):
            who = "User" if m["role"] == "user" else "ChatGPT"
            parts.append(f"\n## {n}. {who}\n\n{m['content']}\n")
        parts.append("\n---\n")
    return Response("\n".join(parts), media_type="text/markdown",
                    headers={"Content-Disposition": f'attachment; filename="{job_id}.md"'})


MAX_WORKERS = int(os.environ.get("CSE_WORKERS", "3"))

def _worker() -> None:
    # A Chromium per queued job does not scale past a handful; a fixed pool
    # keeps memory bounded and lets jobs wait their turn instead of all at once.
    # An exception escaping this loop ends the thread for good — nothing
    # restarts it, so a single locked database would quietly cost the pool a
    # worker until there are none and every job sits queued for ever.
    while True:
        try:
            job_id = next_job()
            if job_id is None:
                time.sleep(1.0)
                continue
            run_job(job_id, claimed=True)
        except Exception as exc:  # noqa: BLE001 - one bad job must not end the worker
            try:
                store.log_event("worker.error", f"{type(exc).__name__}: {exc}")
            except Exception:  # noqa: BLE001 - logging is best effort here
                pass
            time.sleep(1.0)


def next_job() -> str | None:
    with _queue_lock:
        for job_id in store.pending_jobs():
            if store.claim_job(job_id):
                return job_id
    return None


_pool_started = False
_queue_lock = threading.Lock()


@app.on_event("startup")
def resume_queue() -> None:
    global _pool_started
    # Whatever was 'running' when the previous process stopped has no worker
    # behind it any more, so put it back in the queue rather than leaving a
    # row that shows 'running' for ever.
    for job_id in store.orphaned_jobs():
        store.requeue_job(job_id)
    if _pool_started:
        return
    _pool_started = True
    for _ in range(MAX_WORKERS):
        threading.Thread(target=_worker, daemon=True).start()
