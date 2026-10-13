"""The hosted API stores handoffs in a database and signs them with the real digest rule."""

import pytest
from fastapi.testclient import TestClient

from ack.hosted import api as hosted
from ack.hosted.api import create_app, digest


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(hosted, "POSTS_PER_MINUTE", 1000)  # rate limit has its own test
    return TestClient(create_app(f"sqlite:///{tmp_path / 'test.db'}"))


def post(client, **body):
    return client.post("/api/handoffs", json=body)


def test_health_reports_the_database(client):
    assert client.get("/api/health").json() == {"ok": True, "database": "sqlite"}


def test_clean_handoff_is_signed_by_every_subscriber_and_sealed(client):
    res = post(client, title="retry fix", body="def retry(): pass")
    assert res.status_code == 201
    handoff = res.json()
    assert handoff["digest"] == digest("def retry(): pass")
    assert handoff["state"] == "sealed"
    assert [v["verdict"] for v in handoff["votes"]] == ["ACK", "ACK", "ACK"]


def test_one_altered_copy_is_refused_and_the_handoff_is_held(client):
    handoff = post(client, title="retry fix", body="def retry(): pass", altered_for=["alpha"]).json()
    assert handoff["state"] == "held"
    verdicts = {v["agent"]: v["verdict"] for v in handoff["votes"]}
    assert verdicts == {"alpha": "NAK", "gamma": "ACK", "delta": "ACK"}


def test_handoffs_are_stored_and_listed_newest_first(client):
    post(client, title="first", body="one")
    post(client, title="second", body="two")
    titles = [h["title"] for h in client.get("/api/handoffs").json()["handoffs"]]
    assert titles == ["second", "first"]


def test_one_handoff_can_be_read_back_by_id(client):
    created = post(client, title="kept", body="x").json()
    fetched = client.get(f"/api/handoffs/{created['id']}").json()
    assert fetched == created
    assert client.get("/api/handoffs/999999").status_code == 404


def test_data_survives_a_new_app_on_the_same_database(tmp_path):
    url = f"sqlite:///{tmp_path / 'persist.db'}"
    first = TestClient(create_app(url))
    post(first, title="persisted", body="still here")
    second = TestClient(create_app(url))
    assert [h["title"] for h in second.get("/api/handoffs").json()["handoffs"]] == ["persisted"]


@pytest.mark.parametrize("payload", [
    {"title": "", "body": "x"},
    {"title": "x", "body": ""},
    {"title": "x" * 121, "body": "x"},
    {"title": "x", "body": "x" * 20_001},
    {"title": "x", "body": "x", "altered_for": ["mallory"]},
])
def test_invalid_handoffs_are_rejected(client, payload):
    assert client.post("/api/handoffs", json=payload).status_code == 422


def test_posting_is_rate_limited(tmp_path, monkeypatch):
    monkeypatch.setattr(hosted, "POSTS_PER_MINUTE", 2)
    client = TestClient(create_app(f"sqlite:///{tmp_path / 'rate.db'}"))
    assert post(client, title="a", body="a").status_code == 201
    assert post(client, title="b", body="b").status_code == 201
    res = post(client, title="c", body="c")
    assert res.status_code == 429
    assert "too many" in res.json()["error"]


def test_rate_limit_uses_the_address_the_proxy_added(tmp_path, monkeypatch):
    # A visitor can put anything in front of X-Forwarded-For; the proxy appends the real address last.
    monkeypatch.setattr(hosted, "POSTS_PER_MINUTE", 2)
    client = TestClient(create_app(f"sqlite:///{tmp_path / 'proxy.db'}"))
    def from_(addr, spoofed):
        return client.post("/api/handoffs", json={"title": "t", "body": "b"},
                           headers={"X-Forwarded-For": f"{spoofed}, {addr}"})
    assert from_("198.51.100.1", "1.1.1.1").status_code == 201
    assert from_("198.51.100.1", "2.2.2.2").status_code == 201
    assert from_("198.51.100.1", "3.3.3.3").status_code == 429  # spoofing the first hop doesn't help
    assert from_("198.51.100.2", "1.1.1.1").status_code == 201  # a different visitor is not blocked
