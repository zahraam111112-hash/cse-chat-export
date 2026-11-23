"""Deleting a history entry removes its data, and a repeat submission re-extracts."""

import json

import pytest
from fastapi.testclient import TestClient

from app import main, store


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "test.db"))
    store.init()


@pytest.fixture
def client():
    return TestClient(main.app)


def _done_job(transcripts=None):
    job_id = store.create_job(["https://chatgpt.com/share/abc"], source="web")
    store.update_job(
        job_id,
        status="done",
        transcripts=json.dumps(transcripts if transcripts is not None else [{"title": "T", "chars": 5, "messages": []}]),
    )
    return job_id


def test_delete_job_removes_row_and_transcript():
    job_id = _done_job()
    assert store.delete_job(job_id) is True
    assert store.get_job(job_id) is None
    assert store.recent_jobs(10) == []


def test_delete_job_unknown_id_returns_false():
    assert store.delete_job("nope") is False


def test_delete_job_logs_event():
    job_id = _done_job()
    store.delete_job(job_id)
    events = store.recent_events(5)
    assert any(e["kind"] == "job.deleted" and e["detail"] == job_id for e in events)


def test_delete_leaves_other_jobs_alone():
    keep = _done_job()
    drop = _done_job()
    assert store.delete_job(drop) is True
    assert store.get_job(keep) is not None
    assert [j["id"] for j in store.recent_jobs(10)] == [keep]


def test_delete_endpoint_returns_200(client):
    job_id = _done_job()
    r = client.delete(f"/api/jobs/{job_id}")
    assert r.status_code == 200
    assert r.json() == {"deleted": job_id}
    assert store.get_job(job_id) is None


def test_delete_endpoint_returns_404(client):
    r = client.delete("/api/jobs/doesnotexist")
    assert r.status_code == 404


def test_stats_drop_after_delete(client):
    _done_job([{"title": "T", "chars": 100, "messages": []}])
    before = client.get("/api/stats").json()
    assert before["jobs"] == 1 and before["chars"] == 100
    job_id = store.recent_jobs(5)[0]["id"]
    client.delete(f"/api/jobs/{job_id}")
    after = client.get("/api/stats").json()
    assert after["jobs"] == 0 and after["chars"] == 0


def test_same_link_twice_extracts_twice(client, monkeypatch):
    """A repeated link must run a full fresh extraction, never reuse stored data."""
    calls = []

    def fake_extract(url, proxy=None):
        calls.append(url)
        from app.extractor import Message, Transcript

        return Transcript(url=url, title=f"run {len(calls)}",
                          messages=[Message("user", f"hello {len(calls)}")])

    monkeypatch.setattr(main, "extract", fake_extract)

    url = "https://chatgpt.com/share/6b00be00-42a4-83ed-b3c9-ac1fba99219a"
    first = client.post("/api/export", json={"url": url}).json()["job_id"]
    main.run_job(first)
    second = client.post("/api/export", json={"url": url}).json()["job_id"]
    main.run_job(second)

    assert first != second
    assert calls == [url, url], "each submission must call the extractor"
    j1, j2 = store.get_job(first), store.get_job(second)
    assert j1["transcripts"][0]["title"] == "run 1"
    assert j2["transcripts"][0]["title"] == "run 2"
