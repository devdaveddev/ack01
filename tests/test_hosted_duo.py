"""The hosted live run: two real agents work alone, then trade code through a real broker."""

import pytest
from fastapi.testclient import TestClient

from ack.hosted import api as hosted
from ack.hosted.api import create_app
from ack.hosted.duo import Duo


@pytest.fixture(scope="module")
def finished():
    duo = Duo(pace=0)
    assert duo.start()
    duo.wait(120)
    return duo.snapshot()


def kinds(snap, agent=None, kind=None):
    return [e for e in snap["events"] if (agent is None or e["agent"] == agent) and (kind is None or e["kind"] == kind)]


def test_run_finishes_and_both_agents_pass_their_own_tests_twice(finished):
    assert finished["state"] == "done", finished["error"]
    for name in ("alpha", "beta"):
        results = [e["text"] for e in kinds(finished, name, "test")]
        assert len(results) == 2 and all("passed" in r for r in results)


def test_work_is_independent_before_anyone_talks(finished):
    first_wire = next(e["n"] for e in finished["events"] if e["kind"] == "wire")
    before = [e for e in finished["events"] if e["n"] < first_wire]
    assert {e["agent"] for e in before if e["kind"] == "test"} == {"alpha", "beta"}
    assert {e["agent"] for e in before if e["kind"] == "share"} == {"alpha", "beta"}


def test_each_agent_ends_up_with_the_others_code_and_uses_it(finished):
    assert "retry.py" in finished["files"]["alpha"] and "deliver.py" in finished["files"]["alpha"]
    assert "signing.py" in finished["files"]["beta"] and "events.py" in finished["files"]["beta"]


def test_receivers_ran_the_owners_tests_and_decided(finished):
    decisions = {(d["agent"], d["part"]): d for d in finished["summary"]["decisions"]}
    assert decisions[("alpha", "beta/retry-backoff")]["decision"] == "ADOPT"
    assert decisions[("beta", "alpha/webhook-signing")]["decision"] == "ADOPT"
    assert all(d["tests_passed"] for d in decisions.values())


def test_the_private_part_is_refused(finished):
    denied = [e for e in kinds(finished, kind="check") if "DENIED" in e["text"]]
    assert denied and all("payment-keys" in e["text"] for e in denied)
    assert "payment_keys.py" not in finished["files"]["beta"]


def test_the_broker_saw_the_whole_exchange(finished):
    by_type = finished["summary"]["by_type"]
    for message in ("PAIR", "QUERY", "ANSWER", "REQUEST", "CHECK", "OFFER", "ACCEPT", "BUNDLE", "ACK"):
        assert by_type.get(message, 0) >= 1
    assert finished["summary"]["messages"] == sum(by_type.values())


def test_events_are_numbered_and_can_be_read_from_a_cursor(finished):
    numbers = [e["n"] for e in finished["events"]]
    assert numbers == list(range(1, len(numbers) + 1))
    duo = Duo(pace=0)
    duo.events = finished["events"]
    assert [e["n"] for e in duo.snapshot(after=numbers[-2])["events"]] == [numbers[-1]]


def test_a_second_start_while_running_joins_the_run(monkeypatch):
    duo = Duo(pace=0)
    assert duo.start()
    assert not duo.start()
    duo.wait(120)
    assert duo.snapshot()["state"] == "done"
    assert duo.start() and duo.snapshot()["run"] == 2  # a later start is a fresh run
    duo.wait(120)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ACK_DUO_PACE", "0")
    return TestClient(create_app(f"sqlite:///{tmp_path / 'duo.db'}"))


def test_api_starts_a_run_and_reports_it(client):
    assert client.get("/api/duo").json()["state"] == "idle"
    first = client.post("/api/duo/start")
    assert first.status_code == 200 and first.json()["state"] == "running"
    client.app.state.duo.wait(120)
    done = client.get("/api/duo").json()
    assert done["state"] == "done" and done["events"]
    assert client.get("/api/duo?after=9999").json()["events"] == []


def test_api_limits_how_often_one_address_starts_runs(tmp_path, monkeypatch):
    monkeypatch.setenv("ACK_DUO_PACE", "0")
    monkeypatch.setattr(hosted, "DUO_RUNS_PER_HOUR", 1)
    client = TestClient(create_app(f"sqlite:///{tmp_path / 'limit.db'}"))
    assert client.post("/api/duo/start").status_code == 200
    client.app.state.duo.wait(120)
    again = client.post("/api/duo/start")
    assert again.status_code == 429


def step_through(duo, limit=60):
    """Resume each card as it appears. Returns the card keys in the order the run froze on them."""
    import time
    seen, deadline = [], time.time() + limit
    while duo.snapshot()["state"] == "running" and time.time() < deadline:
        paused = duo.snapshot()["paused"]
        if paused:
            seen.append(paused)
            duo.resume()
            while duo.snapshot()["paused"] == paused:
                time.sleep(0.02)
        time.sleep(0.02)
    return seen


def test_step_mode_freezes_on_every_card_and_only_moves_when_continued():
    import time
    duo = Duo(pace=0)
    assert duo.start(step=True)
    deadline = time.time() + 30
    while duo.snapshot()["paused"] != "alone" and time.time() < deadline:
        time.sleep(0.02)
    first = duo.snapshot()
    assert first["paused"] == "alone" and first["step"]
    count = len(first["events"])
    time.sleep(0.6)
    frozen = duo.snapshot()
    # Nothing new from the agents while the card is up (the broker's own wire log may still flush).
    assert [e for e in frozen["events"][count:] if e["kind"] not in ("wire", "blob")] == []
    seen = step_through(duo)
    duo.wait(60)
    done = duo.snapshot()
    assert done["state"] == "done", done["error"]
    assert seen == ["alone", "pair", "ask", "denied", "quarantine", "decide"]
    cards = [e["card"] for e in done["events"] if e["kind"] == "explain"]
    assert [c["key"] for c in cards] == seen
    assert all(c["what"] and c["why"] and c["normal"] for c in cards)


def test_normal_mode_never_pauses(finished):
    assert finished["step"] is False and finished["paused"] is None
    assert not [e for e in finished["events"] if e["kind"] == "explain"]


def test_api_step_run_waits_for_next(tmp_path, monkeypatch):
    import time
    monkeypatch.setenv("ACK_DUO_PACE", "0")
    client = TestClient(create_app(f"sqlite:///{tmp_path / 'step.db'}"))
    assert client.post("/api/duo/start?step=true").json()["step"] is True
    deadline = time.time() + 30
    while client.get("/api/duo").json()["paused"] is None and time.time() < deadline:
        time.sleep(0.05)
    assert client.get("/api/duo").json()["paused"] == "alone"
    assert client.post("/api/duo/next").status_code == 200
    step_through(client.app.state.duo)
    client.app.state.duo.wait(60)
    assert client.get("/api/duo").json()["state"] == "done"


def test_site_still_works_when_the_live_agents_cannot_load(tmp_path, monkeypatch):
    monkeypatch.setattr(hosted, "Duo", None)
    client = TestClient(create_app(f"sqlite:///{tmp_path / 'nodeps.db'}"))
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/duo").status_code == 503
    assert client.post("/api/duo/start").status_code == 503
    assert client.post("/api/duo/next").status_code == 503
