"""Exercise the bot's message handling against a fake Telegram client."""

import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app import bot, store
from app.extractor import Message, Transcript


class FakeClient:
    def __init__(self):
        self.sent = []
        self.documents = []

    def post(self, url, data=None, files=None):
        method = url.rsplit("/", 1)[-1]
        if method == "sendDocument":
            self.documents.append((data.get("chat_id"), files["document"][0], data.get("caption")))
            return _Resp({"ok": True, "result": {"message_id": 1}})
        self.sent.append((method, data))
        return _Resp({"ok": True, "result": {"message_id": len(self.sent)}})


class _Resp:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "test.db"))
    store.init()


@pytest.fixture(autouse=True)
def allow_test_chat(monkeypatch):
    # The bot is closed unless the chat is allowlisted, so the tests have to
    # say who they are — the same way an operator configures it in production.
    monkeypatch.setattr(bot, "ALLOWED", {"7"})


@pytest.fixture
def fake_extract(monkeypatch):
    transcript = Transcript(
        url="https://chatgpt.com/share/abc",
        title="Session Title",
        messages=[Message("user", "hello"), Message("assistant", "hi there")],
    )

    def _fake(url, proxy=None):
        return transcript

    monkeypatch.setattr(bot, "extract", _fake)
    return transcript


def test_start_sends_help():
    client = FakeClient()
    bot.handle_update(client, {"message": {"chat": {"id": 7}, "text": "/start"}})
    assert client.sent[0][0] == "sendMessage"
    assert "ChatGPT Share Exporter" in client.sent[0][1]["text"]


def test_link_produces_docx_and_json(fake_extract):
    client = FakeClient()
    bot.handle_update(
        client,
        {"message": {"chat": {"id": 7}, "text": "https://chatgpt.com/share/6b00be00-42a4-83ed-b3c9-ac1fba99219a"}},
    )
    names = [d[1] for d in client.documents]
    assert len(names) == 2
    assert names[0].endswith(".docx")
    assert names[1].endswith(".json")
    assert any("پیام‌ها" in str(s[1].get("text", "")) for s in client.sent)
    jobs = store.recent_jobs(5)
    assert jobs and jobs[0]["status"] == "done"


def test_no_link_replies_with_hint():
    client = FakeClient()
    bot.handle_update(client, {"message": {"chat": {"id": 7}, "text": "سلام"}})
    assert "لینک" in client.sent[-1][1]["text"]


def test_invalid_link_reports_error():
    client = FakeClient()
    bot.handle_update(client, {"message": {"chat": {"id": 7}, "text": "https://example.com/share/x"}})
    assert "Only public share links" in client.sent[-1][1]["text"]


def test_extraction_failure_is_reported(monkeypatch):
    def boom(url, proxy=None):
        raise RuntimeError("page gone")

    monkeypatch.setattr(bot, "extract", boom)
    client = FakeClient()
    bot.handle_update(
        client,
        {"message": {"chat": {"id": 7}, "text": "https://chatgpt.com/share/6b00be00-42a4-83ed-b3c9-ac1fba99219a"}},
    )
    assert "page gone" in client.sent[-1][1]["text"]
    assert not client.documents


def test_slug_is_filename_safe():
    assert bot._slug("توضیح پروژه OpenNoMark") != ""
    assert "/" not in bot._slug("a/b/c")
