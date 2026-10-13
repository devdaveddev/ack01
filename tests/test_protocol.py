import socket

import pytest

from ack import schema, wire
from ack.agent import Agent
from ack.broker import start_in_thread, tally
from ack.client import BrokerError
from ack.crypto import canonical


@pytest.fixture(params=["wire", "http"])
def net(tmp_path, request):
    server, url = start_in_thread(tmp_path / "broker", transport=request.param)
    agents = {n: Agent(n, tmp_path / n, url, log=None) for n in ("beta", "alpha", "gamma")}
    for a in agents.values():
        a.register()
    for n in ("alpha", "gamma"):
        agents[n].subscribe("beta")
    yield agents
    server.shutdown()


def settle(agents):
    for n in ("alpha", "gamma", "beta"):
        agents[n].poll()


def test_wire_url_scheme_and_frames(tmp_path):
    server, url = start_in_thread(tmp_path / "b", transport="wire")
    assert url.startswith("ack://")
    host, port = wire.parse_url(url)
    with socket.create_connection((host, port)) as s:
        wire.write_frame(s, {"op": "GET", "path": "/messages"})
        header, body = wire.read_frame(s, 1 << 20)
        assert header["status"] == 401
        # Same connection carries the next request.
        wire.write_frame(s, {"op": "GET", "path": "/"})
        header, body = wire.read_frame(s, 1 << 20)
        assert header["status"] == 200 and header["raw"] and b"Ack broker" in body
    with socket.create_connection((host, port)) as s:
        s.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")  # not Ack wire: dropped
        s.settimeout(2)
        assert s.recv(10) == b""
    server.shutdown()


def test_open_announcement_seals_on_quorum(net):
    rec = net["beta"].announce("charge fix", {"billing/charge.py": "amount = cents / 100\n"})
    assert rec["status"]["state"] == "pending" and rec["status"]["waiting"] == ["alpha", "gamma"]
    settle(net)
    mine = net["beta"].state["published"][rec["statement"]["id"]]
    assert mine["status"]["state"] == "sealed" and set(mine["votes"]) == {"alpha", "gamma"}
    inbox = net["alpha"].state["inbox"][rec["statement"]["id"]]
    assert inbox["check"]["match"] and inbox["files"]["billing/charge.py"].startswith("amount")
    # Received content is data: nothing lands in the project.
    assert not (net["alpha"].root / "billing").exists()


def test_tampered_copy_is_refused_and_held(net):
    rec = net["beta"].announce("fix", {"a.py": "x = 1\n"}, tamper=["alpha"])
    settle(net)
    st = net["beta"].state["published"][rec["statement"]["id"]]["status"]
    assert st["state"] == "held" and st["naks"] == 1 and st["acks"] == 1
    vote = net["alpha"].state["inbox"][rec["statement"]["id"]]["vote"]
    assert vote["verdict"] == "NAK" and vote["digest_seen"] != rec["statement"]["digest"]


def test_lower_quorum_still_seals_with_one_refusal(net):
    rec = net["beta"].announce("fix", {"a.py": "x = 1\n"}, tamper=["alpha"], quorum=1)
    settle(net)
    assert net["beta"].state["published"][rec["statement"]["id"]]["status"]["state"] == "sealed"


def test_encrypted_needs_pairing_and_copies_differ(net):
    with pytest.raises(ValueError, match="pairing"):
        net["beta"].announce("secret", {"a.py": "k = 2\n"}, encrypted=True)
    for n in ("alpha", "gamma"):
        net[n].join(net["beta"].invite())
    net["beta"].poll()
    rec = net["beta"].announce("secret", {"a.py": "k = 2\n"}, encrypted=True)
    assert len(set(rec["copies"].values())) == 2  # one sealed copy per subscriber
    blob = net["beta"].broker.get_blob(rec["copies"]["alpha"])
    assert b"k = 2" not in blob
    settle(net)
    assert net["beta"].state["published"][rec["statement"]["id"]]["status"]["state"] == "sealed"
    assert net["gamma"].state["inbox"][rec["statement"]["id"]]["files"] == {"a.py": "k = 2\n"}


def test_manual_policy_waits_for_a_person(net):
    net["gamma"].set_sign_policy("manual")
    rec = net["beta"].announce("fix", {"a.py": "x = 1\n"})
    settle(net)
    aid = rec["statement"]["id"]
    assert net["beta"].state["published"][aid]["status"]["waiting"] == ["gamma"]
    status = net["gamma"].vote(aid, "ACK", "read it, looks right")
    assert status["state"] == "sealed"
    with pytest.raises(ValueError, match="already"):
        net["gamma"].vote(aid, "NAK")


def test_broker_refuses_forged_or_misplaced_votes(net):
    rec = net["beta"].announce("fix", {"a.py": "x = 1\n"})
    aid = rec["statement"]["id"]
    alpha = net["alpha"]
    vote = {"announcement": aid, "signer": "alpha", "verdict": "ACK", "digest_seen": "", "reason": "", "ts": 0}
    with pytest.raises(BrokerError) as e:
        alpha.broker.sign(aid, vote, net["gamma"].identity.sign(canonical(vote)))  # someone else's signature
    assert e.value.status == 403
    with pytest.raises(BrokerError) as e:
        net["beta"].broker.sign(aid, vote | {"signer": "beta"}, net["beta"].identity.sign(canonical(vote)))
    assert e.value.status == 404  # the publisher is not a signatory
    with pytest.raises(BrokerError) as e:
        alpha.broker.post_message(schema.header(schema.make("QUERY", "alpha", "beta", {"text": "x"})) | {"type": "ANNOUNCE"})
    assert e.value.status == 403  # only the broker fans out announcements


def test_announce_only_to_subscribers(net):
    with pytest.raises(BrokerError) as e:
        net["alpha"].announce("x", {"a.py": "1"}, subscribers=["beta"])
    assert e.value.status == 403


def test_tally_math():
    def rec(votes, k, n=3):
        subs = ["a", "b", "c"][:n]
        return {"statement": {"subscribers": subs, "quorum": k},
                "votes": {s: {"vote": {"verdict": v}} for s, v in votes.items()}}
    assert tally(rec({}, 2))["state"] == "pending"
    assert tally(rec({"a": "ACK", "b": "ACK"}, 2))["state"] == "sealed"
    assert tally(rec({"a": "NAK"}, 3))["state"] == "held"
    assert tally(rec({"a": "NAK"}, 2))["state"] == "pending"
    assert tally(rec({"a": "NAK", "b": "NAK"}, 2))["state"] == "held"
