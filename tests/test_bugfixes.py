"""One regression test per bug fixed in the review pass (letters match the list in docs/EXPLAINED.md)."""

import time

import pytest

from ack import bundle
from ack.agent import ANSWER_TTL, Agent
from ack.audit import audit
from ack.broker import start_in_thread
from ack.client import BrokerClient, BrokerError
from ack.crypto import Identity, canonical, seal_content, sha256_hex
from ack.demo import RETRY_V2, pump, write
from ack.memory import Entry
from tests.test_flow import decisions, events, pair  # noqa: F401  (pair is a fixture)

INTENT = "wrap webhook delivery calls"


def signed_manifest(path="m.py", source_id="m"):
    owner, peer = Identity.generate(), Identity.generate()
    key = owner.pair_key(peer.public())
    ct = seal_content(key, b"x = 1\n")
    manifest = {"kind": bundle.MANIFEST_KIND, "owner": "owner", "source_id": source_id, "version": 1,
                "files": [{"path": path, "sha256": sha256_hex(b"x = 1\n"), "size": 6, "blob": sha256_hex(ct)}],
                "deps": [], "tests": {"cmd": ["python", "-c", "pass"], "status": "passed"}, "issued": 0}
    manifest["sig"] = owner.sign(canonical(manifest))
    return owner, manifest


# A: hidden paths could plant .git hooks or CI workflows, or overwrite the agent's own state
@pytest.mark.parametrize("path", [".git/hooks/pre-commit", ".github/workflows/ci.yml", ".ack/state.json", "a/.env"])
def test_a_hidden_paths_rejected(path, tmp_path):
    owner, manifest = signed_manifest(path=path)
    with pytest.raises(bundle.BundleError, match="hidden"):
        bundle.verify_manifest(manifest, "owner", owner.public())


# B: the part id becomes a folder name under vendor/
@pytest.mark.parametrize("sid", ["../../etc", "a/b", "", ".hidden"])
def test_b_bad_part_ids_rejected(sid):
    owner, manifest = signed_manifest(source_id=sid)
    with pytest.raises(bundle.BundleError, match="part id"):
        bundle.verify_manifest(manifest, "owner", owner.public())


def test_b_owner_cannot_share_bad_ids_or_paths(pair):
    make, _ = pair
    alpha, beta = make()
    with pytest.raises(ValueError):
        beta.share(Entry(id="../x", topic="t", summary="s", files=["retry/retry.py"]))
    with pytest.raises(bundle.BundleError):
        beta.share(Entry(id="ok", topic="t", summary="s", files=[".git/config"]))


# C: rejecting an update used to overwrite the registry record of the version still held
def test_c_rejected_update_keeps_registry_record(pair):
    make, tmp = pair
    answers = iter([True, True, True, False])  # v1: run, install; v2: run, then refuse install
    alpha, beta = make(alpha_approver=lambda action, detail: next(answers))
    alpha.request("beta", "retry-backoff", "webhook sender drops events", INTENT)
    pump(alpha, beta)
    write(tmp / "beta", {"retry/retry.py": RETRY_V2})
    beta.publish_update("retry-backoff")
    pump(alpha, beta)
    assert decisions(alpha) == [("retry-backoff", "ADOPT"), ("retry-backoff", "REJECT")]
    kinds = {f.kind for f in audit(alpha)}
    assert kinds == {"stale"}  # behind, as it should be; no false "mismatch"


# D: files an older version installed, and the new one no longer has, used to be left behind
def test_d_update_removes_dropped_files(pair):
    make, tmp = pair
    write(tmp / "beta", {"retry/helper.py": "HELP = 1\n"})
    alpha, beta = make()
    entry = beta.memory.get("retry-backoff")
    entry.files = [*entry.files, "helper.py"]
    beta.share(entry)
    alpha.request("beta", "retry-backoff", "webhook sender drops events", INTENT)
    pump(alpha, beta)
    assert (tmp / "alpha" / "helper.py").exists()

    entry.files = [f for f in entry.files if f != "helper.py"]
    beta.memory.save()
    beta.publish_update("retry-backoff")
    pump(alpha, beta)
    assert decisions(alpha)[-1] == ("retry-backoff", "ADOPT")
    assert not (tmp / "alpha" / "helper.py").exists()


def test_d_locally_edited_leftovers_are_kept(pair):
    make, tmp = pair
    write(tmp / "beta", {"retry/helper.py": "HELP = 1\n"})
    alpha, beta = make()
    entry = beta.memory.get("retry-backoff")
    entry.files = [*entry.files, "helper.py"]
    beta.share(entry)
    alpha.request("beta", "retry-backoff", "webhook sender drops events", INTENT)
    pump(alpha, beta)
    (tmp / "alpha" / "helper.py").write_text("HELP = 2  # our edit\n")
    entry.files = [f for f in entry.files if f != "helper.py"]
    beta.memory.save()
    beta.publish_update("retry-backoff")
    pump(alpha, beta)
    assert (tmp / "alpha" / "helper.py").read_text() == "HELP = 2  # our edit\n"


# E: one failing handler used to abort poll() and lose the rest of the drained queue
def test_e_one_failure_does_not_lose_other_messages(pair):
    make, _ = pair
    alpha, beta = make()
    real, calls = beta._on_query, []

    def flaky(msg):
        calls.append(msg["body"]["text"])
        if len(calls) == 1:
            raise RuntimeError("broker hiccup")
        return real(msg)

    beta._on_query = flaky
    alpha.ask("beta", "first question about retries")
    alpha.ask("beta", "second question about slugs")
    beta.poll()
    assert len(calls) == 2
    assert any("RuntimeError" in t for t in events(beta, "error"))
    alpha.poll()
    assert alpha.last_answer("beta", "second question about slugs") is not None


# F: an unreachable broker used to surface as a raw traceback
def test_f_unreachable_broker_is_a_broker_error(tmp_path):
    client = BrokerClient("http://127.0.0.1:9", "x", tmp_path / "c.json")
    with pytest.raises(BrokerError) as e:
        client.register({"kx": "a", "sig": "b"})
    assert e.value.status == 503


# G: every agent used to see the whole audit log and the whole registry
def test_g_broker_only_shows_what_concerns_you(tmp_path):
    server, url = start_in_thread(tmp_path / "broker")
    try:
        a, b, c = (BrokerClient(url, n, tmp_path / f"{n}.json") for n in "abc")
        for client in (a, b, c):
            client.register({"kx": "x", "sig": "y"})
        a.post_message({"from": "a", "to": "b", "id": "1", "type": "QUERY"})
        b.record_holding(owner="b", source_id="p", version=2, manifest_hash="h", status="OWNER")
        a.record_holding(owner="b", source_id="p", version=1, manifest_hash="h", status="ADOPT")
        assert not [e for e in c.audit() if e.get("action") == "message"]
        assert c.holdings() == []
        assert {(h["holder"], h["version"]) for h in a.holdings()} == {("a", 1), ("b", 2)}
        assert {h["holder"] for h in b.holdings()} == {"a", "b"}  # owners see who holds their part
    finally:
        server.shutdown()


# I: quick answers used to be cached forever
def test_i_cached_answers_expire(pair):
    make, _ = pair
    alpha, beta = make()
    q = "how do you retry flaky calls?"
    alpha.ask("beta", q)
    pump(alpha, beta)
    assert alpha.ask("beta", q) is not None  # fresh: served from cache
    alpha.state["answers"][f"beta|{' '.join(q.lower().split())}"]["received"] = time.time() - ANSWER_TTL - 1
    assert alpha.ask("beta", q) is None  # expired: asks again


# J: a part with no tests used to be reported as "owner tests are failing"
def test_j_part_without_tests_says_so(pair):
    make, _ = pair
    alpha, beta = make()
    beta.share(Entry(id="untested", topic="t", summary="s", base="retry", files=["retry.py"], allow=["alpha"]))
    alpha.request("beta", "untested", "need the helper for retries", INTENT)
    pump(alpha, beta)
    assert "no tests" in events(alpha, "check")[-1]
