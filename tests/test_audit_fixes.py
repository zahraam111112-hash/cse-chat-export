"""Regressions for the bugs the audit found."""

import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app import auth, bot, main, store
from app.extractor import ExtractError, Message, Transcript

SHARE = "https://chatgpt.com/share/6b00be00-42a4-83ed-b3c9-ac1fba99219a"


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "test.db"))
    store.init()


@pytest.fixture
def client():
    return TestClient(main.app)


def _done(error=None):
    job_id = store.create_job([SHARE])
    store.update_job(job_id, status="done",
                     transcripts=json.dumps([{"title": "T", "chars": 3, "messages": []}]),
                     error=error)
    return job_id


def _run(job_id, **kw):
    # run_job now expects the caller to own the row, as the pool does.
    assert store.claim_job(job_id)
    main.run_job(job_id, claimed=True, **kw)


# --- bot: markup in a conversation must not abort the whole flow -------------

class RecordingClient:
    def __init__(self):
        self.sent = []
        self.documents = []

    def post(self, url, data=None, files=None):
        method = url.rsplit("/", 1)[-1]
        text = data.get("text") or data.get("caption") or ""
        # Telegram rejects a message whose HTML cannot be parsed. An unescaped
        # "<div>" is exactly that.
        if "<div>" in text or "<b>left" in text:
            return _Resp({"ok": False, "error_code": 400, "description": "can't parse entities"}, 400)
        if method == "sendDocument":
            self.documents.append(files["document"][0])
            return _Resp({"ok": True, "result": {"message_id": 1}})
        self.sent.append(text)
        return _Resp({"ok": True, "result": {"message_id": len(self.sent)}})


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            raise httpx.HTTPStatusError("400", request=None, response=self)

    def json(self):
        return self._payload


def test_bot_escapes_markup_and_still_delivers(monkeypatch):
    transcript = Transcript(url=SHARE, title="HTML <b>session</b>",
                            messages=[Message("user", "here is <div> markup"),
                                      Message("assistant", "he said <hello>")])
    monkeypatch.setattr(bot, "extract", lambda url, proxy=None: transcript)
    client = RecordingClient()
    bot.handle_link(client, "7", SHARE)

    assert len(client.documents) == 2, "docx and json must both arrive"
    joined = "\n".join(client.sent)
    assert "&lt;div&gt;" in joined
    assert "HTML &lt;b&gt;session&lt;/b&gt;" in joined
    assert store.recent_jobs(5)[0]["status"] == "done"


def test_bot_never_logs_its_token(monkeypatch, capsys):
    # httpx puts the request URL in its exception text and every URL carries
    # the token, so a plain print() wrote the bot token into journald.
    monkeypatch.setattr(bot, "TOKEN", "1234:secret-token-value", raising=True)
    monkeypatch.setattr(bot, "API", "https://api.telegram.org/bot1234:secret-token-value")

    def boom(client, method, **kw):
        if method == "deleteWebhook":
            return True
        raise RuntimeError(
            "Server error '502' for url "
            "'https://api.telegram.org/bot1234:secret-token-value/getUpdates'")

    monkeypatch.setattr(bot, "call", boom)
    monkeypatch.setattr(bot.time, "sleep", lambda s: (_ for _ in ()).throw(KeyboardInterrupt))

    with pytest.raises(KeyboardInterrupt):
        bot.main()

    captured = capsys.readouterr()
    stream = captured.out + captured.err
    assert "secret-token-value" not in stream
    assert "<token>" in stream


def test_bot_records_a_failed_extraction(monkeypatch):
    def boom(url, proxy=None):
        raise RuntimeError("network down")

    monkeypatch.setattr(bot, "extract", boom)
    bot.handle_link(RecordingClient(), "7", SHARE)
    jobs = store.recent_jobs(5)
    assert jobs and jobs[0]["status"] == "failed"
    assert "network down" in jobs[0]["error"]


# --- atomic claim -----------------------------------------------------------

def test_two_workers_extract_a_job_only_once(monkeypatch):
    calls = []

    def slow(url, proxy=None):
        calls.append(url)
        time.sleep(0.2)
        return Transcript(url=url, title="t", messages=[Message("user", "hi")])

    monkeypatch.setattr(main, "extract", slow)
    job_id = store.create_job([SHARE])

    def try_run(job_id):
        # Losing the claim is the outcome this test wants; asserting inside the
        # thread would surface it as an unhandled exception instead.
        if store.claim_job(job_id):
            main.run_job(job_id, claimed=True)

    threads = [threading.Thread(target=try_run, args=(job_id,)) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert calls == [SHARE]


def test_pool_claims_a_job_and_still_runs_it(monkeypatch):
    # The worker claims while picking, then hands the job to run_job. A second
    # claim inside run_job loses (the row is already 'running') and the job was
    # left stuck in running for ever.
    calls = []
    monkeypatch.setattr(main, "extract", lambda url, proxy=None: (
        calls.append(url), Transcript(url=url, title="t", messages=[Message("user", "hi")]))[1])
    job_id = store.create_job([SHARE])
    assert store.claim_job(job_id) is True

    main.run_job(job_id, claimed=True)

    assert calls == [SHARE]
    assert store.get_job(job_id)["status"] == "done"


def test_run_job_still_refuses_a_job_someone_else_claimed(monkeypatch):
    monkeypatch.setattr(main, "extract", lambda url, proxy=None: pytest.fail("must not extract"))
    job_id = store.create_job([SHARE])
    assert store.claim_job(job_id) is True
    main.run_job(job_id)  # no `claimed` flag: it has to win the claim itself
    assert store.get_job(job_id)["status"] == "running"


def test_claim_job_is_exclusive():
    job_id = store.create_job([SHARE])
    assert store.claim_job(job_id) is True
    assert store.claim_job(job_id) is False


def test_pool_does_not_steal_a_job_the_bot_is_running():
    # The bot creates the job already 'running' and then extracts it itself.
    # A row left 'queued' is taken by the pool too — two Chromiums, one job.
    job_id = store.create_job([SHARE], source="telegram", status="running")
    assert main.next_job() is None
    assert store.get_job(job_id)["status"] == "running"


# --- request paths enqueue, the pool runs ----------------------------------

def test_export_does_not_extract_inside_the_request(client, monkeypatch):
    # BackgroundTasks runs in the threadpool after the response, so it was a
    # second, unbounded source of Chromium runs next to the worker pool.
    monkeypatch.setattr(main, "extract", lambda *a, **k: pytest.fail("ran inside the request"))
    r = client.post("/api/export", json={"url": SHARE})
    assert r.status_code == 200
    assert store.get_job(r.json()["job_id"])["status"] == "queued"


def test_retry_starts_no_worker_of_its_own(client, monkeypatch):
    made = []

    class NoThread:
        def __init__(self, *a, **kw):
            made.append(kw)

        def start(self):
            pass

    monkeypatch.setattr(main.threading, "Thread", NoThread)
    job_id = _done(error="one link failed")
    assert client.post(f"/api/jobs/{job_id}/retry").status_code == 200
    assert made == [], "retry must leave the job to the pool"


def test_batch_starts_no_worker_of_its_own(client, monkeypatch):
    made = []

    class NoThread:
        def __init__(self, *a, **kw):
            made.append(kw)

        def start(self):
            pass

    monkeypatch.setattr(main.threading, "Thread", NoThread)
    r = client.post("/api/batch", json={"urls": [SHARE]})
    assert r.status_code == 200
    assert made == []


# --- stuck jobs -------------------------------------------------------------

def test_orphaned_running_jobs_are_found_and_requeued():
    job_id = store.create_job([SHARE])
    store.claim_job(job_id)
    assert store.orphaned_jobs() == [job_id]
    assert store.requeue_job(job_id) is True
    assert job_id in store.pending_jobs()
    assert store.orphaned_jobs() == []


def test_resume_queue_requeues_orphans_without_fanning_out(monkeypatch):
    started = []

    class FakeThread:
        def __init__(self, target=None, args=(), daemon=None):
            started.append(target)

        def start(self):
            pass

    monkeypatch.setattr(main.threading, "Thread", FakeThread)
    monkeypatch.setattr(main, "_pool_started", False)
    for _ in range(40):
        job_id = store.create_job([SHARE])
        store.claim_job(job_id)

    main.resume_queue()

    assert len(started) == main.MAX_WORKERS
    assert store.orphaned_jobs() == []


# --- retry ------------------------------------------------------------------

def test_retry_requeues_a_failed_job(client):
    job_id = _done()
    store.update_job(job_id, status="failed", transcripts=None, error="boom")
    r = client.post(f"/api/jobs/{job_id}/retry")
    assert r.status_code == 200
    job = store.get_job(job_id)
    assert job["status"] in ("queued", "running")
    assert job["transcripts"] is None


def test_retry_refuses_a_completed_job(client):
    job_id = _done()
    r = client.post(f"/api/jobs/{job_id}/retry")
    assert r.status_code == 409


def test_retry_unknown_job_is_404(client):
    assert client.post("/api/jobs/nope/retry").status_code == 404


def test_retry_is_offered_for_partial_jobs(client):
    job_id = _done(error="one link failed")
    assert client.post(f"/api/jobs/{job_id}/retry").status_code == 200


def test_retry_clears_the_previous_error(client, monkeypatch):
    # A retry that succeeds used to keep the old error, so the job stayed
    # 'partial' for ever and each retry appended the errors of the last one.
    job_id = _done(error="one link failed")

    assert client.post(f"/api/jobs/{job_id}/retry").status_code == 200
    assert store.get_job(job_id)["error"] is None

    def ok(url, proxy=None):
        return Transcript(url=url, title="t", messages=[Message("user", "hi")])

    monkeypatch.setattr(main, "extract", ok)
    _run(job_id)
    job = store.get_job(job_id)
    assert job["status"] == "done" and job["error"] is None
    assert store.stats()["partial"] == 0


# --- partial batches --------------------------------------------------------

def test_partial_batch_marks_status_and_retry(client, monkeypatch):
    def flaky(url, proxy=None):
        if "bbbb" in url:
            raise RuntimeError("boom")
        return Transcript(url=url, title="ok", messages=[Message("user", "hi")])

    monkeypatch.setattr(main, "extract", flaky)
    good = SHARE.replace("6b00be00", "aaaa0000")
    bad = SHARE.replace("6b00be00", "bbbb0000")
    job_id = store.create_job([good, bad])
    _run(job_id)

    job = store.get_job(job_id)
    assert job["status"] == "done"
    assert "boom" in job["error"]
    stats = store.stats()
    assert stats["partial"] == 1 and stats["done"] == 0
    html = client.get("/").text
    assert 'class="badge partial"' in html


def test_batch_keeps_valid_links_and_reports_rejected(client, monkeypatch):
    seen = []
    monkeypatch.setattr(main, "extract", lambda url, proxy=None: (
        seen.append(url), Transcript(url=url, title="t", messages=[Message("user", "hi")]))[1])

    r = client.post("/api/batch", json={"urls": [SHARE, "https://example.com/share/x"]})
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 1
    assert data["rejected"][0]["url"] == "https://example.com/share/x"
    assert len(data["job_ids"]) == 1, "a rejected link gets no job of its own"

    # The endpoint only enqueues; the pool is what runs it.
    job_id = data["job_ids"][0]
    assert store.get_job(job_id)["status"] == "queued"
    assert seen == []

    _run(job_id)
    job = store.get_job(job_id)
    assert job["status"] == "done"
    assert seen == [SHARE]


# --- one job and one file per link -----------------------------------------

def test_batch_creates_one_job_per_link(client):
    other = SHARE.replace("6b00be00", "aaaa0000")
    r = client.post("/api/batch", json={"urls": [SHARE, other]})
    data = r.json()

    assert len(data["job_ids"]) == 2
    ids = data["job_ids"]
    assert len(set(ids)) == 2, "each link must be its own job"
    rows = [store.get_job(i) for i in ids]
    assert [r["urls"] for r in rows] == [[SHARE], [other]]
    assert {r["batch"] for r in rows} == {data["batch"]}, "but they share a batch"


def test_each_link_of_a_batch_gets_its_own_file(client, monkeypatch):
    # Every link used to land in one transcript, so a batch downloaded as a
    # single merged Word file with somebody else's conversation inside.
    monkeypatch.setattr(main, "extract", lambda url, proxy=None:
                        Transcript(url=url, title=f"title {url[-4:]}",
                                   messages=[Message("user", "hi")]))
    urls = [SHARE.replace("6b00be00", f"{i:08d}") for i in range(3)]
    ids = client.post("/api/batch", json={"urls": urls}).json()["job_ids"]
    for job_id in ids:
        _run(job_id)

    for job_id in ids:
        job = store.get_job(job_id)
        assert len(job["transcripts"]) == 1
        assert job["urls"] == [job["transcripts"][0]["url"]]
        docx = client.get(f"/api/jobs/{job_id}/docx")
        assert docx.status_code == 200
        assert job_id in docx.headers["content-disposition"]


def test_batch_progress_counts_finished_links(client, monkeypatch):
    monkeypatch.setattr(main, "extract", lambda url, proxy=None:
                        Transcript(url=url, title="t", messages=[Message("user", "hi")]))
    body = client.post("/api/batch", json={"urls": [SHARE, SHARE.replace("6b00", "aaaa")]}).json()
    batch = body["batch"]

    assert client.get(f"/api/batches/{batch}").json()["finished"] == 0
    _run(body["job_ids"][0])
    p = client.get(f"/api/batches/{batch}").json()
    assert (p["finished"], p["total"]) == (1, 2)


def test_batch_progress_404_for_unknown_batch(client):
    assert client.get("/api/batches/nope").status_code == 404


def test_a_link_of_a_batch_reports_its_own_progress(client, monkeypatch):
    # The bar reads done_count/url_count; a stuck counter would show a job
    # finished before its single link ever was.
    monkeypatch.setattr(main, "extract", lambda url, proxy=None:
                        Transcript(url=url, title="t", messages=[Message("user", "hi")]))
    job_id = client.post("/api/export", json={"url": SHARE}).json()["job_id"]
    assert store.get_job(job_id)["done_count"] == 0
    _run(job_id)
    job = store.get_job(job_id)
    assert (job["done_count"], job["url_count"]) == (1, 1)


def test_retry_resets_the_progress_counter(client, monkeypatch):
    monkeypatch.setattr(main, "extract", lambda url, proxy=None:
                        Transcript(url=url, title="t", messages=[Message("user", "hi")]))
    job_id = client.post("/api/export", json={"url": SHARE}).json()["job_id"]
    _run(job_id)
    store.update_job(job_id, status="failed", transcripts=None, error="boom")
    assert client.post(f"/api/jobs/{job_id}/retry").status_code == 200
    assert store.get_job(job_id)["done_count"] == 0


def test_old_rows_backfill_their_progress(tmp_path, monkeypatch):
    # Rows written before the column existed have no done_count; init() must
    # give them 1/1 rather than leave the bar at zero for a finished job.
    db = tmp_path / "old.db"
    import sqlite3
    c = sqlite3.connect(db)
    c.executescript(
        "CREATE TABLE jobs (id TEXT PRIMARY KEY, created_at REAL, updated_at REAL,"
        " status TEXT, source TEXT, chat_id TEXT, urls TEXT, error TEXT, transcripts TEXT);"
        "INSERT INTO jobs VALUES ('old','1','1','done','web',NULL,'[\"u\"]',NULL,NULL);"
    )
    c.commit()
    c.close()

    monkeypatch.setattr(store, "DB_PATH", str(db))
    store.init()
    job = store.get_job("old")
    assert (job["done_count"], job["url_count"]) == (1, 1)


def test_old_multi_link_rows_backfill_from_their_urls(tmp_path, monkeypatch):
    # A pre-split batch held 4 links in one finished row. Reading it as 1/1
    # would show a job finished that had never processed links 2..4.
    db = tmp_path / "old.db"
    import sqlite3
    c = sqlite3.connect(db)
    c.executescript(
        "CREATE TABLE jobs (id TEXT PRIMARY KEY, created_at REAL, updated_at REAL,"
        " status TEXT, source TEXT, chat_id TEXT, urls TEXT, error TEXT, transcripts TEXT);"
        "INSERT INTO jobs VALUES ('batch','1','1','done','web',NULL,"
        "'[\"a\",\"b\",\"c\",\"d\"]',NULL,NULL);"
        "INSERT INTO jobs VALUES ('bad','1','1','done','web',NULL,'not json',NULL,NULL);"
    )
    c.commit()
    c.close()

    monkeypatch.setattr(store, "DB_PATH", str(db))
    store.init()
    batch = store.get_job("batch")
    assert (batch["done_count"], batch["url_count"]) == (4, 4)
    # Unparseable urls must not stop init() from finishing (get_job itself
    # rejects the row, which is why the check is at the SQL level).
    with store.conn() as c:
        bad = c.execute("SELECT done_count, url_count FROM jobs WHERE id='bad'").fetchone()
    assert (bad["done_count"], bad["url_count"]) == (1, 1)


def test_batch_rejects_when_every_link_is_bad(client):
    r = client.post("/api/batch", json={"urls": ["https://example.com/share/x"]})
    assert r.status_code == 400


# --- search and filters -----------------------------------------------------

def _job_with(title, source="web", error=None, status="done"):
    job_id = store.create_job([SHARE], source=source)
    store.update_job(job_id, status=status,
                     transcripts=json.dumps([{"title": title, "chars": 3,
                                              "messages": [{"role": "user",
                                                            "content": title}]}]),
                     error=error)
    return job_id


def test_search_matches_a_title(client):
    want = _job_with("Kubernetes migration notes")
    _job_with("Gardening tips")
    rows = client.get("/api/jobs", params={"q": "kubernetes"}).json()
    assert [r["id"] for r in rows] == [want]


def test_search_matches_inside_a_message(client):
    want = _job_with("Meeting summary")
    with store.conn() as c:
        c.execute("UPDATE jobs SET transcripts=? WHERE id=?",
                  (json.dumps([{"title": "Meeting summary", "chars": 5, "messages": [
                      {"role": "assistant", "content": "we discussed the rhinoceros"}]}]), want))
    rows = client.get("/api/jobs", params={"q": "rhinoceros"}).json()
    assert [r["id"] for r in rows] == [want]


def test_search_ignores_like_wildcards(client):
    _job_with("100% done")
    _job_with("plain")
    assert len(client.get("/api/jobs", params={"q": "100%"}).json()) == 1
    assert client.get("/api/jobs", params={"q": "%"}).json() == [
        r for r in client.get("/api/jobs").json() if "100%" in (r["titles"] or [""])[0]
    ]


def test_search_survives_a_malformed_transcript(client):
    # json_extract raises on bad JSON; one corrupt row must not blank the table.
    good = _job_with("findable text")
    broken = store.create_job([SHARE])
    with store.conn() as c:
        c.execute("UPDATE jobs SET status='done', transcripts='not json' WHERE id=?", (broken,))
    r = client.get("/api/jobs", params={"q": "findable"})
    assert r.status_code == 200
    assert [x["id"] for x in r.json()] == [good]


def test_status_filter_separates_partial_from_done(client):
    clean = _job_with("clean")
    partial = _job_with("half", error="one link failed")
    assert [r["id"] for r in client.get("/api/jobs", params={"status": "done"}).json()] == [clean]
    assert [r["id"] for r in client.get("/api/jobs", params={"status": "partial"}).json()] == [partial]


def test_source_filter(client):
    web = _job_with("from the web", source="web")
    tg = _job_with("from telegram", source="telegram")
    assert [r["id"] for r in client.get("/api/jobs", params={"source": "telegram"}).json()] == [tg]
    assert [r["id"] for r in client.get("/api/jobs", params={"source": "web"}).json()] == [web]


def test_filters_combine(client):
    _job_with("alpha report", source="web")
    want = _job_with("alpha chat", source="telegram")
    rows = client.get("/api/jobs", params={"q": "alpha", "source": "telegram"}).json()
    assert [r["id"] for r in rows] == [want]


def test_total_reflects_filters_not_the_page(client):
    for i in range(7):
        _job_with(f"report {i}")
    r = client.get("/api/jobs", params={"q": "report", "limit": 3})
    assert len(r.json()) == 3
    assert r.headers["x-total"] == "7"
    assert client.get("/api/jobs").headers["x-total"] == "7"


def test_unknown_status_filter_returns_nothing(client):
    _job_with("x")
    assert client.get("/api/jobs", params={"status": "banana"}).json() == []


# --- deleted mid-run --------------------------------------------------------

def test_deleted_job_stops_processing_remaining_urls(monkeypatch):
    calls = []

    def extract_and_delete_first(url, proxy=None):
        calls.append(url)
        if len(calls) == 1:
            store.delete_job(job_id)
        return Transcript(url=url, title="t", messages=[Message("user", "hi")])

    job_id = store.create_job([SHARE, SHARE.replace("6b00", "second")])
    monkeypatch.setattr(main, "extract", extract_and_delete_first)
    _run(job_id)
    assert len(calls) == 1, "no more pages should be fetched after a delete"


# --- health -----------------------------------------------------------------

def test_health_liveness_is_public():
    from fastapi.testclient import TestClient as TC
    assert TC(main.app).get("/healthz").json() == {"ok": True}


def test_deep_health_does_not_depend_on_job_rows(client):
    assert store.recent_jobs(1) == []
    body = client.get("/healthz?deep=1").json()
    assert body == {"ok": True, "template": True, "db": True}


def test_deep_health_reports_a_missing_template(client, monkeypatch):
    from jinja2 import TemplateNotFound

    def gone(*a, **kw):
        raise TemplateNotFound("index.html")

    monkeypatch.setattr(main.templates, "get_template", gone)
    r = client.get("/healthz?deep=1")
    assert r.status_code == 503
    assert r.json()["template"] is False


# --- security headers + auth ------------------------------------------------

def test_security_headers_are_set_by_the_app_itself(client):
    h = client.get("/healthz").headers
    assert h["x-content-type-options"] == "nosniff"
    assert h["referrer-policy"] == "strict-origin-when-cross-origin"
    assert h["x-frame-options"] == "DENY"


def test_auth_rejects_a_wrong_password(monkeypatch):
    monkeypatch.setattr(auth, "USER", "u")
    monkeypatch.setattr(auth, "PASSWORD", "p")
    r = TestClient(main.app).get("/", auth=("u", "wrong"))
    assert r.status_code == 401
    assert TestClient(main.app).get("/healthz").status_code == 200


# --- event retention --------------------------------------------------------

def test_events_are_trimmed():
    monkeypatch_keep = store.EVENT_KEEP
    store.EVENT_KEEP = 20
    try:
        for i in range(50):
            store.log_event("probe", str(i))
        with store.conn() as c:
            n = c.execute("SELECT COUNT(*) n FROM events").fetchone()["n"]
        assert n <= 21
        assert store.recent_events(1)[0]["detail"] == "49"
    finally:
        store.EVENT_KEEP = monkeypatch_keep


# --- public mode: isolation, rate limits, queue ceiling ---------------------

@pytest.fixture
def public(monkeypatch):
    """Nobody is an admin, exactly as with CSE_PUBLIC=1 on a public site."""
    monkeypatch.setenv("CSE_PUBLIC", "1")


def test_visitors_only_see_their_own_jobs(monkeypatch, public):
    from app import limits
    limits.reset()
    a = TestClient(main.app, headers={"x-forwarded-for": "203.0.113.1"})
    b = TestClient(main.app, headers={"x-forwarded-for": "203.0.113.2"})
    monkeypatch.setattr(main, "extract", lambda url, proxy=None:
                        Transcript(url=url, title="t", messages=[Message("user", "hi")]))

    mine = a.post("/api/export", json={"url": SHARE}).json()["job_id"]
    theirs = b.post("/api/export", json={"url": SHARE}).json()["job_id"]

    listed_a = [j["id"] for j in a.get("/api/jobs").json()]
    assert mine in listed_a and theirs not in listed_a
    # A direct id must not bypass the list filter.
    assert b.get(f"/api/jobs/{mine}").status_code == 404
    assert b.get(f"/api/jobs/{mine}/docx").status_code == 404
    assert b.delete(f"/api/jobs/{mine}").status_code == 404
    assert a.get(f"/api/jobs/{mine}").status_code == 200


def test_a_visitor_cannot_delete_someone_elses_job(monkeypatch, public):
    from app import limits
    limits.reset()
    a = TestClient(main.app, headers={"x-forwarded-for": "203.0.113.9"})
    b = TestClient(main.app, headers={"x-forwarded-for": "203.0.113.10"})
    monkeypatch.setattr(main, "extract", lambda url, proxy=None:
                        Transcript(url=url, title="t", messages=[Message("user", "hi")]))
    job = a.post("/api/export", json={"url": SHARE}).json()["job_id"]
    _run(job)

    assert b.delete(f"/api/jobs/{job}").status_code == 404
    assert store.get_job(job) is not None, "the row must survive a foreign delete"
    assert a.delete(f"/api/jobs/{job}").status_code == 200


def test_rate_limit_stops_a_burst(monkeypatch):
    from app import limits
    limits.reset()
    monkeypatch.setattr(limits, "MAX_PER_WINDOW", 3)
    monkeypatch.setattr(main, "extract", lambda url, proxy=None:
                        Transcript(url=url, title="t", messages=[Message("user", "hi")]))
    c = TestClient(main.app, headers={"x-forwarded-for": "203.0.113.20"})

    codes = [c.post("/api/export", json={"url": SHARE}).status_code for _ in range(4)]
    assert codes[:3] == [200, 200, 200]
    assert codes[3] == 429
    # A different address still has its own budget.
    other = TestClient(main.app, headers={"x-forwarded-for": "203.0.113.21"})
    assert other.post("/api/export", json={"url": SHARE}).status_code == 200


def test_rate_limit_counts_links_not_requests(monkeypatch):
    from app import limits
    limits.reset()
    monkeypatch.setattr(limits, "MAX_PER_WINDOW", 3)
    c = TestClient(main.app, headers={"x-forwarded-for": "203.0.113.30"})
    # One batch of 3 links uses the whole budget, so a second link is refused.
    body = {"urls": [SHARE, SHARE.replace("6b00", "aaa1"), SHARE.replace("6b00", "bbb2")]}
    assert c.post("/api/batch", json=body).status_code == 200
    assert c.post("/api/export", json={"url": SHARE}).status_code == 429


def test_queue_ceiling_refuses_instead_of_growing(monkeypatch):
    from app import limits
    limits.reset()
    monkeypatch.setattr(limits, "MAX_QUEUE", 1)
    c = TestClient(main.app, headers={"x-forwarded-for": "203.0.113.40"})
    assert c.post("/api/export", json={"url": SHARE}).status_code == 200
    assert c.post("/api/export", json={"url": SHARE}).status_code == 503


def test_invalid_links_do_not_spend_the_budget(monkeypatch):
    from app import limits
    limits.reset()
    monkeypatch.setattr(limits, "MAX_PER_WINDOW", 1)
    c = TestClient(main.app, headers={"x-forwarded-for": "203.0.113.50"})
    # A rejected link never ran a Chromium, so it must not cost the one slot —
    # on either route. The single-link one used to charge before validating.
    assert c.post("/api/export", json={"url": "https://example.com/nope"}).status_code == 400
    assert c.post("/api/batch", json={"urls": ["https://example.com/nope"]}).status_code == 400
    assert c.post("/api/export", json={"url": SHARE}).status_code == 200


def test_stats_are_admin_only(monkeypatch):
    from app import limits
    limits.reset()
    monkeypatch.setattr(auth, "USER", "admin")
    monkeypatch.setattr(auth, "PASSWORD", "pw")
    anon = TestClient(main.app, headers={"x-forwarded-for": "203.0.113.60"})
    assert anon.get("/api/stats", auth=("admin", "pw")).status_code == 200


def test_bot_is_closed_when_no_allowlist_is_configured(monkeypatch):
    monkeypatch.setattr(bot, "ALLOWED", set())
    client = RecordingClient()
    bot.handle_update(client, {"message": {"chat": {"id": 7},
                                           "text": "https://chatgpt.com/share/abc"}})
    assert client.documents == []
    assert any("⛔" in text for text in client.sent)


def test_bot_rejects_a_stranger_and_serves_an_allowed_chat(monkeypatch):
    monkeypatch.setattr(bot, "ALLOWED", {"7"})
    monkeypatch.setattr(bot, "extract", lambda url, proxy=None:
                        Transcript(url=url, title="t", messages=[Message("user", "hi")]))
    stranger = RecordingClient()
    bot.handle_update(stranger, {"message": {"chat": {"id": 99},
                                             "text": "https://chatgpt.com/share/abc"}})
    assert stranger.documents == []

    known = RecordingClient()
    bot.handle_update(known, {"message": {"chat": {"id": 7},
                                          "text": "https://chatgpt.com/share/abc"}})
    assert known.documents
