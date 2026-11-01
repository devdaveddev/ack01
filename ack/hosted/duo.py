"""Two live agents on the hosted site: independent work first, then the real protocol between them.

Each agent is a real `ack.agent.Agent` with its own project folder, identity and thread, talking to a
real broker over HTTP on this machine. Nothing here is a replay:

  1. alpha and beta each build something on their own and run its tests.
  2. They pair. Each then needs what the other built, so each asks, requests, quarantines the bundle,
     runs the owner's tests in a sandbox and decides. beta also asks for something alpha keeps private.
  3. Each finishes its own task using the other's code, and runs its tests again.

Visitors watch one shared run through `snapshot`. Nothing a visitor sends reaches the agents, so
the only code that ever runs is the code in this file. `pace` slows the narration down for people;
tests set it to 0.
"""

import json
import shutil
import tempfile
import textwrap
import threading
import time
from pathlib import Path

from .. import quarantine
from ..agent import Agent
from ..broker import start_in_thread
from ..memory import Entry
from ..pairing import Invite
from ..spelling import correct

STEP_TIMEOUT = 45  # seconds one agent waits on the other before the run is called failed

PAUSE_LIMIT = 600  # a viewer who walks away does not hold the run forever
STEP_LIMIT = 1800  # the most a stepped run may take, so reading time is not counted as a timeout

# Shown in step mode, one at a time, when the run freezes at that moment. "normal" is the usual way two
# projects share code today: pasting it, or sharing a repository. The claims stay within what this run shows.
CARDS = {
    "alone": {
        "title": "Two agents, nothing shared yet",
        "what": "Alpha and beta each wrote their own code and ran their own tests, in separate folders, with separate keys. "
                "Neither has spoken to the other, and the broker has seen no messages.",
        "why": "Sharing only makes sense between parties that each have something the other lacks. Each agent also decided "
               "what it is willing to share: alpha offers webhook-signing to beta only, and keeps payment-keys private.",
        "normal": "Normally the sharing decision is made by whoever can read the repository or the chat: access is all or nothing.",
    },
    "pair": {
        "title": "Pairing pins each side's keys",
        "what": "Beta made a one-time invite carrying its public keys. Alpha redeemed it and proved it had the invite with an "
                "HMAC. From now on every message is sealed with a key only these two agents can derive (X25519, then AES-GCM).",
        "why": "The broker only passes messages along. Without pinned keys, the broker or anyone who got between them could "
               "pose as beta and hand alpha code. With them, a message that was not sealed by beta cannot be opened.",
        "normal": "A pasted snippet or a Slack message carries no identity at all. You trust the channel.",
    },
    "ask": {
        "title": "Ask first, fetch second",
        "what": "Alpha asked a plain question. Beta matched it against the one-line keys of the parts alpha is allowed to see, "
                "summarised only those, and answered. No file has moved.",
        "why": "A cheap answer first means an agent only pulls code it actually wants, and the owner never reveals that private "
               "parts exist. Misspelled words are fixed before sending, so a typo does not look like no match.",
        "normal": "Normally you grep someone's repository or paste their whole file into a prompt to find out if it helps.",
    },
    "denied": {
        "title": "The owner refuses, not the broker",
        "what": "Beta asked alpha for payment-keys. Alpha checked its own allow list and replied DENIED. The file never left "
                "alpha's folder and was never offered.",
        "why": "Policy lives with the owner of the code. A broker bug or a stolen broker credential cannot release a part the "
               "owner never agreed to share.",
        "normal": "With a shared repository, read access to one file is usually read access to all of them.",
    },
    "quarantine": {
        "title": "Verify before anything runs",
        "what": "Alpha received a signed manifest: the owner's Ed25519 signature over every file's hash, test command and "
                "dependencies. It downloaded sealed layers, re-hashed each one against that manifest, and unpacked them into a "
                "quarantine folder, not its project. Now it asks to run the owner's tests in a sandbox.",
        "why": "Integrity (the bytes are what beta signed), provenance (beta signed them), and behaviour (the tests pass here, "
               "not just on beta's machine) are checked before the code touches the project. A missing file or a failing test "
               "means REJECT.",
        "normal": "Pasted code arrives with no hash, no signature, and no record of whether it ran anywhere else. "
                  "A repository gives you history, but still not a check on your own machine before you adopt it.",
    },
    "decide": {
        "title": "Adopt, adapt or reject, on the record",
        "what": "The tests passed and nothing conflicted with alpha's files, so the decision is ADOPT. Had alpha owned a file at "
                "the same path, it would have ADAPTed instead, vendoring the copy beside its own with a note on where it came from. "
                "Alpha then sent a signed verdict, and both sides wrote the decision to a provenance log.",
        "why": "Every adoption can be traced afterwards: who sent it, which version, which hash, what the tests said, and why "
               "the receiver agreed. Nothing is written to the project without that approval step (auto-approved here; a "
               "person clicks it in the real app).",
        "normal": "A copy-paste leaves no trace of where a function came from or whether anyone checked it.",
    },
}
MAX_FILE_CHARS = 1800
MAX_FILES_SHOWN = 8


def tests_for(pattern: str) -> list[str]:
    return ["python", "-m", "unittest", "discover", "-s", ".", "-p", pattern]


SIGNING = '''
import hashlib, hmac, time

def sign(secret, body, now=None):
    ts = str(int(now if now is not None else time.time()))
    mac = hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"

def verify(secret, body, signature, tolerance=300, now=None):
    parts = dict(p.split("=", 1) for p in signature.split(","))
    now = now if now is not None else time.time()
    if abs(now - int(parts["t"])) > tolerance:
        return False
    expected = sign(secret, body, now=int(parts["t"])).split("v1=")[1]
    return hmac.compare_digest(expected, parts["v1"])
'''

SIGNING_TEST = '''
import unittest
from signing import sign, verify

class T(unittest.TestCase):
    def test_roundtrip(self):
        sig = sign("s3cret", b'{"id": 1}', now=1000)
        self.assertTrue(verify("s3cret", b'{"id": 1}', sig, now=1010))

    def test_tampered_or_old(self):
        sig = sign("s3cret", b'{"id": 1}', now=1000)
        self.assertFalse(verify("s3cret", b'{"id": 2}', sig, now=1010))
        self.assertFalse(verify("s3cret", b'{"id": 1}', sig, now=2000))
'''

RETRY = '''
import time

def retry(fn, attempts=3, base=0.01):
    for i in range(attempts):
        try:
            return fn()
        except Exception:
            if i == attempts - 1:
                raise
            time.sleep(base * 2 ** i)
'''

RETRY_TEST = '''
import unittest
from retry import retry

class T(unittest.TestCase):
    def test_recovers(self):
        calls = []
        def flaky():
            calls.append(1)
            if len(calls) < 3:
                raise IOError("boom")
            return "ok"
        self.assertEqual(retry(flaky), "ok")
        self.assertEqual(len(calls), 3)

    def test_gives_up(self):
        def broken():
            raise IOError("down")
        with self.assertRaises(IOError):
            retry(broken, attempts=2)
'''

# What each agent builds once it has the other's code.
DELIVER = '''
from retry import retry
from signing import sign

def deliver(send, body, secret):
    """Send a webhook: signed, and retried when the receiver is flaky."""
    return retry(lambda: send(body, sign(secret, body)))
'''

DELIVER_TEST = '''
import unittest
from deliver import deliver
from signing import verify

class T(unittest.TestCase):
    def test_signed_and_survives_a_flaky_receiver(self):
        seen = []
        def send(body, signature):
            seen.append((body, signature))
            if len(seen) < 3:
                raise IOError("receiver down")
            return "delivered"
        self.assertEqual(deliver(send, b'{"order": 7}', "s3cret"), "delivered")
        body, signature = seen[-1]
        self.assertTrue(verify("s3cret", body, signature))
        self.assertEqual(len(seen), 3)
'''

EVENTS = '''
from retry import retry
from signing import sign

def publish(post, event, secret):
    """Publish an event to the audit service: signed, and retried on transient errors."""
    return retry(lambda: post(event, sign(secret, event)))
'''

EVENTS_TEST = '''
import unittest
from events import publish
from signing import verify

class T(unittest.TestCase):
    def test_published_event_verifies(self):
        sent = []
        def post(event, signature):
            sent.append((event, signature))
            if len(sent) == 1:
                raise IOError("busy")
            return "stored"
        self.assertEqual(publish(post, b'{"kind": "login"}', "k"), "stored")
        event, signature = sent[-1]
        self.assertTrue(verify("k", event, signature))
'''

PRIVATE = "PAYMENT_KEY = 'sk_live_not_for_sharing'\n"

# A line of narration per task, in the order the agents do them.
ALPHA_TASK = "Build webhook signing, then make delivery reliable"
BETA_TASK = "Build a retry helper, then publish signed audit events"


def clean(text: str) -> str:
    return textwrap.dedent(text).lstrip()


class Duo:
    def __init__(self, pace: float = 1.0):
        self.pace = pace
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        # The spell checker's dictionary takes ~10s to load on first use, which would stall the first run.
        self._warm = threading.Thread(target=lambda: correct("warm up", set()), daemon=True)
        self._warm.start()
        self.step = False
        self.paused: str | None = None
        self._open = threading.Event()  # set while the run may move; cleared while a card is on screen
        self._open.set()
        self._cp_lock = threading.Lock()
        self._seen: set[str] = set()
        self._reset()

    def _reset(self) -> None:
        self.state = "idle"  # idle, running, done, failed
        self.run_id = 0
        self.events: list[dict] = []
        self.status = {"alpha": "waiting", "beta": "waiting"}
        self.roots: dict[str, Path] = {}
        self.agents: dict[str, Agent] = {}
        self.summary: dict = {}
        self.frozen: dict | None = None  # the files as the run ended; the folders are deleted after
        self.started = 0.0
        self.error = ""

    # ---- what the page reads ----

    def running(self) -> bool:
        return self.state == "running"

    def snapshot(self, after: int = 0) -> dict:
        with self._lock:
            events = [e for e in self.events if e["n"] > after]
            return {"state": self.state, "run": self.run_id, "status": dict(self.status),
                    "step": self.step, "paused": self.paused, "events": events, "files": self._files(), "summary": self.summary, "error": self.error,
                    "elapsed": round(time.time() - self.started, 1) if self.started else 0}

    def _files(self) -> dict:
        if self.frozen is not None:
            return self.frozen
        out = {}
        for name, root in self.roots.items():
            shown = {}
            for path in sorted(root.rglob("*")):
                rel = path.relative_to(root)
                if not path.is_file() or ".ack" in rel.parts or "__pycache__" in rel.parts:
                    continue
                if len(shown) >= MAX_FILES_SHOWN:
                    break
                try:
                    shown[str(rel)] = path.read_text()[:MAX_FILE_CHARS]
                except (OSError, UnicodeDecodeError):
                    pass
            out[name] = shown
        return out

    def _emit(self, agent: str, kind: str, text: str, to: str = "", card: dict | None = None) -> None:
        with self._lock:
            event = {"n": len(self.events) + 1, "t": round(time.time() - self.started, 2),
                     "agent": agent, "kind": kind, "text": text, "to": to}
            if card:
                event["card"] = card
            self.events.append(event)

    def _checkpoint(self, key: str) -> None:
        """Step mode: freeze the whole run on a card until the viewer continues. Each card shows once."""
        if not self.step:
            return
        with self._cp_lock:  # two agents reaching a card together take turns
            if key in self._seen:
                return
            self._seen.add(key)
            self._emit("broker", "explain", CARDS[key]["title"], card={"key": key, **CARDS[key]})
            with self._lock:
                self.paused = key
            self._open.clear()
            self._open.wait(PAUSE_LIMIT)
            with self._lock:
                self.paused = None
            self._open.set()

    def resume(self) -> None:
        self._open.set()

    def _hold(self) -> float:
        """Block while a card is showing. Returns the seconds spent, so waits are not timed out by reading."""
        began = time.monotonic()
        self._open.wait(PAUSE_LIMIT)
        return time.monotonic() - began

    def _set(self, agent: str, text: str) -> None:
        with self._lock:
            self.status[agent] = text

    def _pause(self, factor: float = 1.0) -> None:
        if self.pace:
            time.sleep(self.pace * factor)

    # ---- one run ----

    def start(self, step: bool = False) -> bool:
        """Begin a run. False if one is already going. step=True freezes it on an explanation card at each key moment."""
        with self._lock:
            if self.state == "running":
                return False
            run_id = self.run_id + 1
            self._reset()
            self.step, self.paused = step, None
            self._seen = set()
            self._open.set()
            self.run_id, self.state, self.started = run_id, "running", time.time()
        self._thread = threading.Thread(target=self._run, args=(run_id,), daemon=True)
        self._thread.start()
        return True

    def wait(self, timeout: float = 120) -> None:
        if self._thread:
            self._thread.join(timeout)

    def _run(self, run_id: int) -> None:
        work = Path(tempfile.mkdtemp(prefix="ack-duo-"))
        server = None
        try:
            self._warm.join()
            self.started = time.time()
            server, url = start_in_thread(work / "broker")
            self._emit("broker", "start", "broker up; every message between the agents passes through it, sealed")
            self._story(work, url)
            self._finish(work, "done")
        except Exception as e:  # a failed run must not leave the page spinning
            self.error = f"{type(e).__name__}: {e}"
            self._emit("broker", "error", self.error)
            self._finish(work, "failed")
        finally:
            if server:
                server.shutdown()
            time.sleep(0.2)
            shutil.rmtree(work, ignore_errors=True)

    def _finish(self, work: Path, state: str) -> None:
        self._open.set()
        audit = work / "broker" / "audit.jsonl"
        lines = [json.loads(line) for line in audit.read_text().splitlines()] if audit.exists() else []
        types: dict[str, int] = {}
        for entry in lines:
            if entry.get("action") == "message":
                types[entry["type"]] = types.get(entry["type"], 0) + 1
        decisions = []
        for name in ("alpha", "beta"):
            agent = self.agents.get(name)
            for rec in (agent.provenance() if agent else []):
                if rec.get("role") == "receiver":
                    decisions.append({"agent": name, "part": f"{rec['peer']}/{rec['source_id']}",
                                      "decision": rec["decision"], "reason": rec["reason"],
                                      "tests_passed": rec.get("tests_passed")})
        with self._lock:
            self.frozen = self._files()
            self.summary = {"messages": sum(types.values()), "by_type": types, "decisions": decisions}
            self.state = state
            self.status = {"alpha": "finished" if state == "done" else "stopped",
                           "beta": "finished" if state == "done" else "stopped"}

    # ---- the story ----

    def _story(self, work: Path, url: str) -> None:
        for name in ("alpha", "beta"):
            root = work / name
            root.mkdir()
            self.roots[name] = root
            self.agents[name] = Agent(name, root, url, approver=self._approver(name),
                                      log=self._logger(name))
            self.agents[name].register()
        self.limit = STEP_LIMIT if self.step else STEP_TIMEOUT
        self.barrier = threading.Barrier(2, timeout=self.limit)
        self.invite_ready = threading.Event()
        self.finished = {"alpha": threading.Event(), "beta": threading.Event()}
        self.failures: list[str] = []
        self.invite_uri = ""

        watcher_stop = threading.Event()
        watcher = threading.Thread(target=self._watch_wire, args=(work / "broker" / "audit.jsonl", watcher_stop),
                                   daemon=True)
        watcher.start()
        threads = [threading.Thread(target=self._guard, args=(self._alpha,), name="alpha"),
                   threading.Thread(target=self._guard, args=(self._beta,), name="beta")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(self.limit * 4)
        time.sleep(0.5)  # let the watcher read the last audit lines
        watcher_stop.set()
        watcher.join(2)
        if self.failures:
            raise RuntimeError("; ".join(self.failures))

    def _guard(self, fn) -> None:
        try:
            fn()
        except Exception as e:
            self.failures.append(f"{threading.current_thread().name}: {type(e).__name__}: {e}")
            self.barrier.abort()
            for event in (self.invite_ready, *self.finished.values()):
                event.set()

    def _approver(self, name: str):
        def approve(action: str, detail: str) -> bool:
            self._checkpoint("quarantine" if action == "run-tests" else "decide")
            self._emit(name, "approval", f"{action}: {detail}. Auto-approved in this demo; a person clicks here normally")
            return True
        return approve

    def _logger(self, name: str):
        prefix = f"[{name}] "

        def log(line: str) -> None:
            kind, _, text = line.removeprefix(prefix).partition(": ")
            self._emit(name, kind, text)
        return log

    def _watch_wire(self, audit: Path, stop: threading.Event) -> None:
        """Tail the broker's own audit log, so the page shows what the broker saw: type, sender, recipient."""
        seen = 0
        while True:
            done = stop.is_set()
            lines = audit.read_text().splitlines() if audit.exists() else []
            for line in lines[seen:]:
                entry = json.loads(line)
                if entry.get("action") == "message":
                    self._emit(entry["agent"], "wire", entry["type"], to=entry["to"])
                elif entry.get("action") == "blob.put":
                    self._emit(entry["agent"], "blob", f"uploaded {entry['size']} sealed bytes")
            seen = len(lines)
            if done:
                return
            time.sleep(0.15)

    def _write(self, name: str, files: dict[str, str]) -> None:
        for rel, text in files.items():
            path = self.roots[name] / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(clean(text))

    def _test(self, name: str, pattern: str, label: str) -> None:
        passed, output = quarantine.run_tests(tests_for(pattern), self.roots[name])
        count = next((ln for ln in output.splitlines() if ln.startswith("Ran ")), "")
        self._emit(name, "test", f"{label}: {'passed' if passed else 'FAILED'} ({count or output.strip()[-80:]})")
        if not passed:
            raise RuntimeError(f"{name}'s own tests failed: {output[-300:]}")

    def _wait(self, name: str, condition, what: str) -> None:
        """Poll as `name` until condition() holds. Polling is also how the agent serves the other one."""
        agent = self.agents[name]
        deadline = time.monotonic() + self.limit
        while not condition():
            deadline += self._hold()
            if self.failures:
                raise RuntimeError("the other agent failed")
            if time.monotonic() > deadline:
                raise TimeoutError(f"{name} gave up waiting for {what}")
            agent.poll()
            time.sleep(0.15 + 0.5 * self.pace)  # one hop of the exchange per beat, so a person can follow it

    def _decided(self, name: str, source_id: str) -> bool:
        return any(r.get("role") == "receiver" and r["source_id"] == source_id
                   for r in self.agents[name].provenance())

    def _linger(self, name: str) -> None:
        """Keep serving the peer until both agents are done: acks and requests may still arrive."""
        me, other = name, "beta" if name == "alpha" else "alpha"
        self.finished[me].set()
        self._wait(me, lambda: self.finished[other].is_set(), f"{other} to finish")
        for _ in range(8):
            self.agents[me].poll()
            time.sleep(0.15)

    # ---- the two agents, each in its own thread ----

    def _alpha(self) -> None:
        me, peer = "alpha", "beta"
        agent = self.agents[me]
        self._set(me, ALPHA_TASK)

        self._emit(me, "task", "Task 1 (alone): write HMAC webhook signing with a replay window")
        self._pause()
        self._write(me, {"signing.py": SIGNING, "test_signing.py": SIGNING_TEST,
                         "payment_keys.py": PRIVATE})
        self._pause(0.6)
        self._test(me, "test_signing.py", "signing tests")
        agent.share(Entry(
            id="webhook-signing", files=["signing.py", "test_signing.py"], tests=tests_for("test_signing.py"),
            topic="sign and verify outgoing webhooks", tags=["webhook", "hmac", "security", "signature"],
            summary="sign(secret, body) and verify(secret, body, signature): HMAC-SHA256 webhook signatures",
            notes="Every webhook carries a signature: HMAC-SHA256 of a timestamp and the raw body with a per-customer "
                  "secret. Receivers recompute it and compare in constant time. Anything older than five minutes "
                  "is refused, to stop replays.", allow=[peer]))
        agent.share(Entry(id="payment-keys", files=["payment_keys.py"], tests=tests_for("test_none.py"),
                          topic="payment provider credentials", tags=["billing", "secrets"],
                          summary="live payment keys", allow=[]))
        self._emit(me, "share", "shares webhook-signing with beta only; payment-keys stays private")
        self.barrier.wait()  # both have finished their own work; neither has spoken to the other yet
        self._checkpoint("alone")

        self._set(me, "pairing with beta")
        self._wait(me, self.invite_ready.is_set, "beta's invite")
        agent.join(Invite.from_uri(self.invite_uri))
        self._checkpoint("pair")

        self._set(me, "needs a retry helper")
        self._emit(me, "task", "Task 2: make webhook delivery survive a flaky receiver. I have no retry code")
        question = "how do I retry flaky http calls with backoff?"
        agent.ask(peer, question)
        self._wait(me, lambda: agent.last_answer(peer, question) is not None, "beta's answer")
        self._checkpoint("ask")
        agent.request(peer, "retry-backoff", "webhook delivery drops events on transient errors",
                      "wrap webhook sends in retry()")
        self._set(me, "checking beta's bundle in quarantine")
        self._wait(me, lambda: self._decided(me, "retry-backoff"), "the retry bundle")

        self._set(me, "building delivery on beta's retry")
        self._write(me, {"deliver.py": DELIVER, "test_deliver.py": DELIVER_TEST})
        self._pause(0.6)
        self._test(me, "test_deliver.py", "delivery tests using beta's retry and my signing")
        self._emit(me, "task", "Done: webhook delivery is signed and retried")
        self._linger(me)

    def _beta(self) -> None:
        me, peer = "beta", "alpha"
        agent = self.agents[me]
        self._set(me, BETA_TASK)

        self._emit(me, "task", "Task 1 (alone): write a retry helper with exponential backoff")
        self._pause()
        self._write(me, {"retry.py": RETRY, "test_retry.py": RETRY_TEST})
        self._pause(0.6)
        self._test(me, "test_retry.py", "retry tests")
        agent.share(Entry(
            id="retry-backoff", files=["retry.py", "test_retry.py"], tests=tests_for("test_retry.py"),
            topic="retry flaky calls with exponential backoff", tags=["http", "resilience", "retries"],
            summary="retry(fn, attempts, base): calls fn, sleeping base*2^i between failures",
            notes="Every outbound HTTP call goes through retry(). It retries on any exception, three attempts by "
                  "default, with exponential backoff from 10ms.", allow=[peer]))
        self._emit(me, "share", "shares retry-backoff with alpha")
        self.barrier.wait()

        self._set(me, "pairing with alpha")
        self.invite_uri = agent.invite().to_uri()
        self.invite_ready.set()
        self._wait(me, lambda: peer in agent.state["peers"], "alpha to join")

        self._set(me, "needs request signing")
        self._emit(me, "task", "Task 2: publish audit events that the audit service can verify. I have no signing code")
        agent.request(peer, "payment-keys", "want to test against the live payment API", "read the keys")
        self._wait(me, lambda: any(k == "check" and "payment-keys" in t and "DENIED" in t for k, t in agent.events),
                   "alpha's answer on payment-keys")
        self._checkpoint("denied")
        question = "how do you sign outgoing webhooks?"
        agent.ask(peer, question)
        self._wait(me, lambda: agent.last_answer(peer, question) is not None, "alpha's answer")
        agent.request(peer, "webhook-signing", "audit events must be verifiable by the receiver",
                      "sign every published event")
        self._set(me, "checking alpha's bundle in quarantine")
        self._wait(me, lambda: self._decided(me, "webhook-signing"), "the signing bundle")

        self._set(me, "building publishing on alpha's signing")
        self._write(me, {"events.py": EVENTS, "test_events.py": EVENTS_TEST})
        self._pause(0.6)
        self._test(me, "test_events.py", "event tests using alpha's signing and my retry")
        self._emit(me, "task", "Done: audit events are signed and retried")
        self._linger(me)
