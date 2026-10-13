"""Two agents end to end, including the failure paths."""

from pathlib import Path

import pytest

from ack.agent import Agent
from ack.broker import start_in_thread
from ack.demo import BETA_FILES, BETA_PARTS, RETRY_V2, pump, write
from ack.memory import Entry
from ack.pairing import Invite


def yes(action, detail):
    return True


@pytest.fixture
def pair(tmp_path):
    server, url = start_in_thread(tmp_path / "broker")

    def make(alpha_approver=yes):
        write(tmp_path / "beta", BETA_FILES)
        beta = Agent("beta", tmp_path / "beta", url, log=None)
        alpha = Agent("alpha", tmp_path / "alpha", url, approver=alpha_approver, log=None)
        alpha.register()
        beta.register()
        for entry in BETA_PARTS:
            beta.share(Entry(**vars(entry)))
        alpha.join(Invite.from_uri(beta.invite().to_uri()))
        pump(alpha, beta)
        return alpha, beta

    yield make, tmp_path
    server.shutdown()


def decisions(agent):
    return [(r["source_id"], r["decision"]) for r in agent.provenance() if r.get("role") == "receiver"]


def events(agent, kind):
    return [text for k, text in agent.events if k == kind]


def test_adopt_then_update_ships_only_changed_layer(pair):
    make, tmp = pair
    alpha, beta = make()
    alpha.request("beta", "retry-backoff", "webhook sender drops events", "wrap delivery calls")
    pump(alpha, beta)
    assert decisions(alpha) == [("retry-backoff", "ADOPT")]
    assert (tmp / "alpha" / "retry.py").read_text() == (tmp / "beta" / "retry" / "retry.py").read_text()

    write(tmp / "beta", {"retry/retry.py": RETRY_V2})
    assert beta.publish_update("retry-backoff") == 1
    pump(alpha, beta)
    assert decisions(alpha)[-1] == ("retry-backoff", "ADOPT")
    assert "downloaded 1 of 2 layers" in events(alpha, "fetch")[-1]
    assert "jitter" in (tmp / "alpha" / "retry.py").read_text()
    assert beta.provenance()[-1]["decision"] == "ADOPT"


def test_query_hides_parts_the_peer_cannot_request(pair):
    make, _ = pair
    alpha, beta = make()
    alpha.ask("beta", "billing api keys")
    pump(alpha, beta)
    assert "billing-keys" not in alpha.last_answer("beta", "billing api keys")["sources"]


def test_check_statuses(pair):
    make, tmp = pair
    alpha, beta = make()
    alpha.request("beta", "billing-keys", "need it for invoicing", "call billing directly")
    alpha.request("beta", "nope", "does this exist at all", "read for reference")
    alpha.request("beta", "slugify", "consistent slugs everywhere", "?")  # no real intent
    pump(alpha, beta)
    write(tmp / "beta", {"geo/geo.py": "changed = True\n"})
    alpha.request("beta", "geo-capitals", "dashboard needs capitals", "import in the UI")
    pump(alpha, beta)
    statuses = [t.split(": ")[1].split(" ")[0] for t in events(alpha, "check")]
    assert statuses == ["DENIED", "NOT_FOUND", "DENIED", "STALE"]
    assert decisions(alpha) == []


def test_conflict_leads_to_adapt_in_vendor(pair):
    make, tmp = pair
    write(tmp / "alpha", {"slugify.py": "mine = True\n"})
    alpha, beta = make()
    alpha.request("beta", "slugify", "consistent slugs across projects", "replace ours later")
    pump(alpha, beta)
    assert decisions(alpha) == [("slugify", "ADAPT")]
    assert (tmp / "alpha" / "slugify.py").read_text() == "mine = True\n"
    assert "Built on beta's slugify v1" in (tmp / "alpha/vendor/beta/slugify/BUILT_ON.md").read_text()


def test_failing_sandbox_tests_reject(pair):
    make, tmp = pair
    alpha, beta = make()
    alpha.request("beta", "geo-capitals", "dashboard needs capitals", "import in the UI")
    pump(alpha, beta)
    assert decisions(alpha) == [("geo-capitals", "REJECT")]
    assert not (tmp / "alpha" / "geo.py").exists()


def test_never_runs_or_installs_without_approval(pair):
    make, tmp = pair
    asked = []
    alpha, beta = make(alpha_approver=lambda action, detail: asked.append(action) or False)
    alpha.request("beta", "retry-backoff", "webhook sender drops events", "wrap delivery calls")
    pump(alpha, beta)
    assert asked == ["run-tests"]
    assert decisions(alpha) == [("retry-backoff", "REJECT")]
    assert not (tmp / "alpha" / "retry.py").exists()


def test_tampered_blob_on_broker_is_rejected(pair):
    make, tmp = pair
    alpha, beta = make()
    alpha.request("beta", "retry-backoff", "webhook sender drops events", "wrap delivery calls")
    # Let beta upload, then corrupt the stored blobs before alpha fetches them.
    beta.poll()  # REQUEST -> CHECK, OFFER
    alpha.poll()  # -> ACCEPT
    beta.poll()  # uploads, -> BUNDLE
    for blob in (tmp / "broker" / "blobs").iterdir():
        blob.write_bytes(b"evil")
    pump(alpha, beta)
    assert decisions(alpha) == [("retry-backoff", "REJECT")]
    assert "integrity" in alpha.provenance()[-1]["reason"]
    assert not (tmp / "alpha" / "retry.py").exists()


def test_unpaired_or_forged_messages_are_dropped(pair):
    make, tmp = pair
    alpha, beta = make()
    mallory = Agent("mallory", tmp / "mallory", beta.broker.url, log=None)
    mallory.register()
    mallory.state["peers"]["beta"] = beta.identity.public()  # knows beta's key, but beta never paired
    mallory._send("beta", "QUERY", {"text": "anything secret?"})
    beta.poll()
    assert any("mallory" in t for t in events(beta, "dropped"))


def test_summary_cache_reuses_by_meaning_and_invalidates_on_note_change(pair):
    make, _ = pair
    alpha, beta = make()
    calls = []
    real = beta.summarizer.summarize
    beta.summarizer.summarize = lambda q, notes: calls.append(q) or real(q, notes)

    alpha.ask("beta", "how do I retry flaky HTTP calls with backoff?")
    alpha.ask("beta", "how to retry flaky http calls using backoff")  # same meaning, new wording
    pump(alpha, beta)
    assert len(calls) == 1
    assert "summary cache hit" in events(beta, "answered")[-1]

    entry = beta.memory.get("retry-backoff")
    entry.notes = "Now distinguishes 4xx from 5xx."
    beta.share(entry)  # note changed -> cached summary must not be reused
    alpha.ask("beta", "retry flaky HTTP calls with backoff please")
    pump(alpha, beta)
    assert len(calls) == 2
    assert "4xx" in alpha.last_answer("beta", "retry flaky HTTP calls with backoff please")["summary"]


def test_summary_cache_respects_visibility(pair):
    make, tmp = pair
    alpha, beta = make()
    alpha.ask("beta", "retry flaky calls with backoff")
    pump(alpha, beta)
    entry = beta.memory.get("retry-backoff")
    entry.allow = []  # alpha loses access; the cached answer names this part
    beta.memory.save()
    alpha.ask("beta", "retry flaky calls with backoff!!")
    pump(alpha, beta)
    assert "retry-backoff" not in alpha.last_answer("beta", "retry flaky calls with backoff!!")["sources"]


def test_update_available_sends_diff_and_receiver_can_decline(pair):
    make, tmp = pair
    alpha, beta = make()
    alpha.request("beta", "retry-backoff", "webhook sender drops events", "wrap delivery calls")
    pump(alpha, beta)
    alpha.judge.want_update = lambda manifest, changed, diff: (False, "frozen for release")
    write(tmp / "beta", {"retry/retry.py": RETRY_V2})
    blobs_before = set((tmp / "broker" / "blobs").iterdir())
    beta.publish_update("retry-backoff")
    pump(alpha, beta)

    update = alpha.state["updates"]["beta/retry-backoff"]
    assert update["changed"] == ["retry.py"] and "+import random" in update["diff"]
    assert set((tmp / "broker" / "blobs").iterdir()) == blobs_before  # nothing uploaded on decline
    assert "jitter" not in (tmp / "alpha" / "retry.py").read_text()
    assert beta.provenance()[-1]["event"] == "declined"


def test_missing_dependency_rejects(pair):
    make, tmp = pair
    alpha, beta = make()
    entry = beta.memory.get("retry-backoff")
    entry.deps = ["definitely-not-installed-pkg>=1"]
    beta.memory.save()
    alpha.request("beta", "retry-backoff", "webhook sender drops events", "wrap delivery calls")
    pump(alpha, beta)
    assert decisions(alpha) == [("retry-backoff", "REJECT")]
    assert "definitely-not-installed-pkg" in alpha.provenance()[-1]["reason"]


def test_owner_logs_intent(pair):
    make, _ = pair
    alpha, beta = make()
    alpha.request("beta", "retry-backoff", "webhook sender drops events", "wrap delivery calls")
    pump(alpha, beta)
    assert beta.provenance()[-1]["intent"] == "wrap delivery calls"


def test_evicted_cache_still_uploads_and_repeat_request_is_local(pair):
    make, tmp = pair
    alpha, beta = make()
    alpha.request("beta", "retry-backoff", "webhook sender drops events", "wrap delivery calls")
    beta.poll()  # offer packed into beta's cache
    for blob in beta.blobs.path.iterdir():
        blob.unlink()  # evicted before alpha accepts
    pump(alpha, beta)
    assert decisions(alpha) == [("retry-backoff", "ADOPT")]

    # Same part again: every layer is in alpha's cache, nothing downloaded.
    alpha.request("beta", "retry-backoff", "re-checking after a local revert", "wrap delivery calls")
    pump(alpha, beta)
    assert "downloaded 0 of 2 layers" in events(alpha, "fetch")[-1]


def test_blob_store_evicts_least_recently_used(tmp_path):
    from ack.agent import BlobStore
    from ack.crypto import sha256_hex
    import os, time
    store = BlobStore(tmp_path, max_bytes=250)
    ids = []
    for i in range(3):
        data = bytes([i]) * 100
        ids.append(sha256_hex(data))
        store.put(ids[-1], data)
        os.utime(tmp_path / ids[-1], (time.time() + i, time.time() + i))
    assert not store.has(ids[0]) and store.has(ids[1]) and store.has(ids[2])


def test_what_do_you_share_lists_only_visible_parts(pair):
    make, _ = pair
    alpha, beta = make()
    for q in ("hi", "what do you share?"):
        alpha.ask("beta", q)
    pump(alpha, beta)
    answer = alpha.last_answer("beta", "what do you share?")
    assert set(answer["sources"]) == {"retry-backoff", "slugify", "geo-capitals"}  # not billing-keys
    assert "billing" not in answer["summary"]
    assert alpha.last_answer("beta", "hi")["sources"] == answer["sources"]
    beta.ask("alpha", "what do you share?")  # alpha shares nothing in this setup
    pump(alpha, beta)
    assert beta.last_answer("alpha", "what do you share?")["summary"] == "I'm not sharing anything with you yet."


def test_misspelled_questions_are_fixed_before_sending(pair):
    make, _ = pair
    alpha, beta = make()
    alpha.ask("beta", "how do you retry falky http calls with backof?")
    assert ("falky", "flaky") in alpha.last_correction and ("backof", "backoff") in alpha.last_correction
    pump(alpha, beta)
    answer = alpha.last_answer("beta", "how do you retry flaky http calls with backoff?")
    assert answer["sources"][0] == "retry-backoff"
    assert answer["typed"] == "how do you retry falky http calls with backof?"
    alpha.ask("beta", "falky", exact=True)  # exact: sent as typed
    assert alpha.last_correction == []
