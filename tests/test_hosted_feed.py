"""Visitors create agents, agents post to a shared feed, and every agent sees the same history."""

import pytest
from fastapi.testclient import TestClient

from ack.hosted import api as hosted
from ack.hosted.api import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(hosted, "AGENTS_PER_DAY", 1000)
    monkeypatch.setattr(hosted, "EVENTS_PER_MINUTE", 1000)
    monkeypatch.setattr(hosted, "POSTS_PER_MINUTE", 1000)
    return TestClient(create_app(f"sqlite:///{tmp_path / 'feed.db'}"))


def make_agent(client, name):
    res = client.post("/api/agents", json={"name": name})
    assert res.status_code == 201, res.text
    return res.json()["token"]


def post(client, token, body, kind="note"):
    return client.post("/api/events", json={"kind": kind, "body": body},
                       headers={"Authorization": f"Bearer {token}"})


def test_creating_an_agent_returns_a_token_once(client):
    res = client.post("/api/agents", json={"name": "Retry-Bot"})
    assert res.status_code == 201
    assert res.json()["name"] == "retry-bot"  # names are lowercased
    assert len(res.json()["token"]) >= 30
    names = [a["name"] for a in client.get("/api/agents").json()["agents"]]
    assert names == ["retry-bot"]
    assert "token" not in client.get("/api/agents").json()["agents"][0]


@pytest.mark.parametrize("name", ["ab", "-bad", "has space", "way-too-long-" + "x" * 30, "under_score"])
def test_bad_agent_names_are_rejected(client, name):
    assert client.post("/api/agents", json={"name": name}).status_code in (422,)


def test_a_taken_name_is_refused(client):
    make_agent(client, "alpha-two")
    assert client.post("/api/agents", json={"name": "alpha-two"}).status_code == 409


def test_one_address_can_only_create_a_few_agents_a_day(tmp_path, monkeypatch):
    monkeypatch.setattr(hosted, "AGENTS_PER_DAY", 2)
    client = TestClient(create_app(f"sqlite:///{tmp_path / 'agents.db'}"))
    make_agent(client, "agent-one")
    make_agent(client, "agent-two")
    assert client.post("/api/agents", json={"name": "agent-three"}).status_code == 429


def test_posting_needs_a_valid_token(client):
    make_agent(client, "agent-one")
    assert post(client, "not-a-token", "hello").status_code == 401
    assert client.post("/api/events", json={"kind": "note", "body": "hi"}).status_code == 401


def test_every_agent_sees_the_same_shared_history(client):
    alice = make_agent(client, "agent-alice")
    bob = make_agent(client, "agent-bob")
    post(client, alice, "I have a retry helper")
    post(client, bob, "Can I see it?", kind="question")
    post(client, alice, "Sent. Check the summary.", kind="answer")
    feed = client.get("/api/feed").json()["events"]
    assert [(e["agent"], e["kind"]) for e in feed] == [
        ("agent-alice", "answer"), ("agent-bob", "question"), ("agent-alice", "note")]
    assert client.get("/api/feed?limit=1").json()["events"][0]["body"] == "Sent. Check the summary."


def test_unknown_kind_and_empty_message_are_rejected(client):
    token = make_agent(client, "agent-one")
    assert post(client, token, "hi", kind="gossip").status_code == 422
    assert post(client, token, "").status_code == 422
    assert post(client, token, "\x00\x01").status_code == 422  # nothing left once control chars are removed


def test_control_characters_are_stripped_from_messages(client):
    token = make_agent(client, "agent-one")
    event = post(client, token, "hi\x00 there\x07").json()
    assert event["body"] == "hi there"


def test_one_agent_is_limited_per_minute(tmp_path, monkeypatch):
    monkeypatch.setattr(hosted, "EVENTS_PER_MINUTE", 2)
    client = TestClient(create_app(f"sqlite:///{tmp_path / 'events.db'}"))
    token = make_agent(client, "agent-one")
    assert post(client, token, "1").status_code == 201
    assert post(client, token, "2").status_code == 201
    assert post(client, token, "3").status_code == 429


def test_feed_survives_a_restart(tmp_path):
    url = f"sqlite:///{tmp_path / 'restart.db'}"
    first = TestClient(create_app(url))
    token = make_agent(first, "agent-one")
    post(first, token, "kept")
    second = TestClient(create_app(url))
    assert [e["body"] for e in second.get("/api/feed").json()["events"]] == ["kept"]


def test_an_agent_can_delete_its_own_message_but_not_someone_elses(client):
    alice = make_agent(client, "agent-alice")
    bob = make_agent(client, "agent-bob")
    mine = post(client, alice, "mine").json()["id"]
    assert client.delete(f"/api/events/{mine}", headers={"Authorization": f"Bearer {bob}"}).status_code == 403
    assert client.delete(f"/api/events/{mine}", headers={"Authorization": f"Bearer {alice}"}).status_code == 200
    assert client.get("/api/feed").json()["events"] == []


def test_admin_key_deletes_agents_with_their_messages(client, monkeypatch):
    monkeypatch.setenv("ACK_ADMIN_KEY", "test-admin-key")
    token = make_agent(client, "agent-doomed")
    post(client, token, "gone soon")
    assert client.delete("/api/agents/agent-doomed").status_code == 403  # no key
    assert client.delete("/api/agents/agent-doomed", headers={"Authorization": "Bearer wrong"}).status_code == 403
    admin = {"Authorization": "Bearer test-admin-key"}
    assert client.delete("/api/agents/agent-doomed", headers=admin).status_code == 200
    assert client.get("/api/feed").json()["events"] == []
    assert client.get("/api/agents").json()["agents"] == []


def test_admin_actions_are_off_without_a_configured_key(client, monkeypatch):
    monkeypatch.delenv("ACK_ADMIN_KEY", raising=False)
    make_agent(client, "agent-kept")
    assert client.delete("/api/agents/agent-kept", headers={"Authorization": "Bearer "}).status_code == 403
    assert len(client.get("/api/agents").json()["agents"]) == 1


def test_admin_can_delete_a_handoff(client, monkeypatch):
    monkeypatch.setenv("ACK_ADMIN_KEY", "test-admin-key")
    created = client.post("/api/handoffs", json={"title": "t", "body": "b"}).json()
    assert client.delete(f"/api/handoffs/{created['id']}").status_code == 403
    assert client.delete(f"/api/handoffs/{created['id']}",
                         headers={"Authorization": "Bearer test-admin-key"}).status_code == 200
    assert client.get("/api/handoffs").json()["handoffs"] == []
