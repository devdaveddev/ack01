# 10 · `ack/agent.py`, line by line

**Job of this file:** the protocol itself. An `Agent` owns one project and one identity. It sends messages, handles every message type, keeps all its state in `<project>/.ack/`, and ties together every other module.

**How to read it.** Methods named `_on_<type>` are **handlers**: `poll()` calls `_on_offer` for an OFFER message, `_on_bundle` for a BUNDLE, and so on. Public methods (`ask`, `request`, `publish_update`) start a conversation by sending a message and returning straight away. The reply arrives on a later `poll()`.

```
alpha (receiver)                         broker                          beta (owner)
ask()  ── QUERY ───────────────────────►  queue  ───────────────────►  _on_query
_on_answer  ◄────────────────────────────  queue  ◄──── ANSWER ─────────
request() ── REQUEST ─────────────────►                              ►  _on_request ─ CHECK + OFFER
_on_check / _on_offer ── ACCEPT ──────►                              ►  _on_accept ─ upload blobs, BUNDLE
_on_bundle → _receive → ACK ──────────►                              ►  _on_ack
                                                    publish_update() ─ UPDATE_AVAILABLE
_on_update_available ── ACCEPT ───────►                              ►  _on_accept ─ BUNDLE …
```

Primer references: [closures](00-python-primer.md#functions), [dict operators](00-python-primer.md#dicts-the-newer-operators), [exceptions](00-python-primer.md#errors-exceptions), [paths](00-python-primer.md#paths-pathlib).

---

## Lines 13–28 · imports

```python
import hmac
import json
import secrets
import shutil
import time
from collections.abc import Callable
from pathlib import Path

from . import bundle, quarantine, schema
from .bundle import BundleError
from .client import BrokerClient, BrokerError
from .crypto import CryptoError, Identity, b64d, b64e, canonical, seal, seal_content, sha256_hex, unseal, unseal_content
from .judge import Decision, RuleJudge
from .memory import Entry, ProjectMemory, embed
from .pairing import Invite, proof, token_hash
from .summarize import ExtractiveSummarizer, SummaryCache
```

- **13** Constant-time comparison for the pairing proof.
- **15** Random invite tokens.
- **16** `shutil`: copy files, and delete whole folders (`rmtree`).
- **17** Timestamps in the provenance log.
- **18** `Callable`, for the approver type.
- **21** Whole modules (`bundle.pack(...)`, `quarantine.assess(...)`, `schema.make(...)`), so it's clear where each function comes from.
- **22–28** Specific names from the other modules.

## Lines 30–35 · types and settings

```python
# approver(action, detail) -> bool. Actions: "run-tests", "install".
Approver = Callable[[str, str], bool]
MIN_MATCH = 0.10
TOP_K = 3
MAX_UPDATE_DIFF = 20_000
ANSWER_TTL = 3600
```

- **31** An **approver** is a function `(action, detail) → yes/no`. The agent asks it before running tests and before installing. The command line passes one that asks you at the keyboard; the web app passes one that waits for a click; the demo passes one that always says yes; the default says no.
- **32** The minimum similarity for a part to count as a match for a question.
- **33** At most 3 parts per quick answer.
- **34** Update diffs longer than 20,000 characters are cut short.
- **35** A cached quick answer is reused for an hour, then the question is asked again. (Before the bug-fix pass it was cached forever, so answers never reflected changes; bug I.)

# approver(action, detail) -> bool. Actions: "run-tests", "install".
Approver = Callable[[str, str], bool]
MIN_MATCH = 0.10
TOP_K = 3
MAX_UPDATE_DIFF = 20_000
```

- **31** An **approver** is a function `(action, detail) → yes/no`. The agent asks it before running tests and before installing. The command line passes one that asks you at the keyboard; the demo passes one that always says yes; the default says no.
- **32** The minimum similarity for a part to count as a match for a question.
- **33** At most 3 parts per quick answer.
- **34** Update diffs longer than 20,000 characters are cut short.

## Lines 38–43 · an error and the safe default approver

```python
class UnknownPeer(Exception):
    pass


def deny_all(action: str, detail: str) -> bool:
    return False
```

- **38** Raised when a message comes from someone we never paired with.
- **42–43** **The default approver refuses everything.** Unless you explicitly opt in, received code never runs and never gets installed.

## Lines 46–86 · BlobStore: the local cache

```python
class BlobStore:
    def __init__(self, path: Path, max_bytes: int = 256 << 20):
        self.path = path
        self.max_bytes = max_bytes
        path.mkdir(parents=True, exist_ok=True)
```

- **54–57** A folder of blob files, named by hash, with a 256 MB size cap.

```python
    def has(self, blob_id: str) -> bool:
        return (self.path / blob_id).is_file()
```

- **60** Is the blob present? (It says nothing about whether it's intact.)

```python
    def get(self, blob_id: str) -> bytes | None:
        path = self.path / blob_id
        try:
            data = path.read_bytes()
        except (FileNotFoundError, IsADirectoryError):
            return None
        if sha256_hex(data) != blob_id:
            path.unlink(missing_ok=True)
            return None
        path.touch()  # mark as recently used
        return data
```

- **64–67** Read it. Not there → `None`.
- **68–70** **Never trust the cache.** If the bytes don't hash to the name, the copy is corrupt or tampered with: delete it and act as if it were missing, so it gets downloaded again. `missing_ok=True` means don't fail if it's already gone.
- **71** `touch()` updates the file's "last modified" time. That time is how eviction decides what was used least recently.
- **72** Return the verified bytes.

```python
    def put(self, blob_id: str, data: bytes) -> None:
        (self.path / blob_id).write_bytes(data)
        self._evict(keep=blob_id)
```

- **75–76** Save, then trim the cache, but never the blob just added.

```python
    def _evict(self, keep: str) -> None:
        files = sorted((p.stat().st_mtime, p) for p in self.path.iterdir() if p.is_file())
        total = sum(p.stat().st_size for _, p in files)
        for _, path in files:
            if total <= self.max_bytes:
                break
            if path.name != keep:
                total -= path.stat().st_size
                path.unlink(missing_ok=True)
```

- **79** Every blob, as `(last-modified time, path)`, sorted oldest first.
- **80** The total size.
- **81–86** Delete oldest first until under the cap, skipping `keep`. This is **least recently used** eviction.

## Lines 89–119 · creating an Agent

```python
class Agent:
    def __init__(self, name: str, root: Path, broker_url: str, judge=None,
                 approver: Approver = deny_all, log: Callable[[str], None] | None = print,
                 summarizer=None, cache_bytes: int = 256 << 20):
```

- **90–92** Parameters:
  - **required:** the agent's name, its project folder, the broker URL
  - **optional:** a judge (rules by default), an approver (deny all by default), a log function (`print` by default; `None` for silence), a summariser, and the cache size

```python
        self.name = name
        self.root = Path(root)
        self.dir = self.root / ".ack"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.identity = Identity.load_or_create(self.dir / "identity.json")
        self.memory = ProjectMemory(self.root)
        self.judge = judge or RuleJudge()
        self.approver = approver
        self.log = log
        self.broker = BrokerClient(broker_url, name, self.dir / "broker.json")
        self.blobs = BlobStore(self.dir / "blobs", cache_bytes)
        self.summarizer = summarizer or ExtractiveSummarizer()
        self.summaries = SummaryCache(self.dir / "summaries.json")
        self.events: list[tuple[str, str]] = []
        self.last_report = None
        self.approval_context: dict = {}
```

- **94–96** The project folder, and its `.ack/` state folder.
- **97** Load this project's keys, or create them on first run.
- **98** The parts it's willing to share.
- **99** `judge or RuleJudge()`: use the given judge, or the rule-based one.
- **102** The broker connection. Its saved secret lives in `.ack/broker.json`.
- **103** The local blob cache.
- **104–105** The summariser, and the summary cache.
- **106** An in-memory list of `(kind, text)` events. The tests read these.
- **107** The most recent assessment report, kept for the demo to print a diff.
- **108–110** `approval_context`: details of whatever the approver is currently being asked about (the manifest, the quarantine folder, later the full report). A plain yes/no approver ignores it; the web app reads it to show you the code and the diff on the approval card.

```python
        self.state = self._load("state.json", {
            "peers": {}, "invites": {}, "queries": {}, "answers": {}, "requests": {},
            "offers_in": {}, "offers_out": {}, "updates": {}, "installs": {}, "shares": {},
        })
```

- **111–122** All conversation state, in one saved dict:

| Key | Holds |
|---|---|
| `peers` | name → public keys we pinned at pairing |
| `invites` | token hash → token, for invites we issued and nobody has used |
| `queries` | QUERY id → cache key, for questions awaiting an answer |
| `answers` | cache key → answer: our exact-text answer cache |
| `requests` | REQUEST id → what we asked for and why |
| `offers_in` | manifest hash → offers and updates we accepted, awaiting their BUNDLE |
| `offers_out` | manifest hash → offers we made, awaiting ACCEPT, DECLINE or ACK |
| `updates` | "peer/part" → the latest UPDATE_AVAILABLE seen (including the diff) |
| `installs` | "peer/part" → what we adopted, which version, where each file went |
| `shares` | part → which peers hold which version (owner side), so updates know whom to tell |

Every reply is checked against these tables. **An unsolicited message has nothing to match, and is dropped.**

## Lines 126–146 · persistence and logging

```python
    def _load(self, name: str, default: dict) -> dict:
        path = self.dir / name
        return default | json.loads(path.read_text()) if path.exists() else default
```

- **128** If the file exists, merge the saved state over the defaults (`|`), so a key added in a newer version of the code still exists. Otherwise use the defaults. (The `if … else` applies to the whole `default | …` expression.)

```python
    def _save(self) -> None:
        tmp = self.dir / "state.json.tmp"
        tmp.write_text(json.dumps(self.state, indent=1))
        tmp.replace(self.dir / "state.json")
```

- **131–133** Write to a temporary file, then rename it over the real one atomically, so a crash never leaves half-written state.

```python
    def _event(self, kind: str, text: str) -> None:
        self.events.append((kind, text))
        if self.log:
            self.log(f"[{self.name}] {kind}: {text}")
```

- **136–138** Record an event, and print it like `[alpha] fetch: …` unless logging is off.

```python
    def _provenance(self, **record) -> None:
        with (self.dir / "provenance.jsonl").open("a") as f:
            f.write(json.dumps({"ts": time.time(), "agent": self.name, **record}) + "\n")

    def provenance(self) -> list[dict]:
        path = self.dir / "provenance.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
```

- **141–142** Append one line to the **provenance log**: the time, which agent, plus whatever named fields were passed in. It's append-only, so history is never rewritten.
- **145–146** Read the whole log back as a list of dicts.

## Lines 150–155 · register, and share a part

```python
    def register(self) -> None:
        self.broker.register(self.identity.public())

    def share(self, entry: Entry) -> Entry:
        return self.memory.add(entry)
```

- **151** Register our public keys with the broker. Safe to call every time.
- **155** Add a part to memory, which embeds its key and records its file hashes as the current version.

## Lines 157–162 · create an invite (inviter side)

```python
    def invite(self, bundle_hash: str | None = None) -> Invite:
        token = secrets.token_urlsafe(16)
        self.broker.create_invite(token_hash(token))
        self.state["invites"][token_hash(token)] = token
        self._save()
        return Invite(self.broker.url, token, self.name, self.identity.public(), bundle_hash)
```

- **158** A random 16-byte token, as URL-safe text.
- **159** Give the broker only its **hash**.
- **160–161** Remember the token itself, so we can check the joiner's proof later.
- **162** Build the invite: broker URL, token, our name, **our public keys**, and an optional bundle hash. Turn it into a QR code with `render_qr(invite.to_uri())`.

## Lines 164–172 · join using an invite (joiner side)

```python
    def join(self, invite: Invite) -> None:
        inviter = self.broker.redeem_invite(invite.token_hash)
        if inviter != invite.agent:
            raise BrokerError(409, f"invite belongs to {inviter}, not {invite.agent}")
        self._add_peer(invite.agent, invite.public)
        body = {"invite": invite.token_hash, "public": self.identity.public(),
                "proof": proof(invite.token, self.identity.public())}
        self.broker.post_message(schema.make("PAIR", self.name, invite.agent, body))
        self._event("paired", f"joined {invite.agent} via invite")
```

- **165** Redeem the invite at the broker. It succeeds once only, and tells us who issued it.
- **166–167** That must match the name written in the invite.
- **168** Pin the inviter's public keys **from the invite itself**, not from the broker.
- **169–170** The PAIR body: which invite, our public keys, and the HMAC proof that we know the token.
- **171** Send PAIR **unsealed**: there's no pair key yet, since the inviter doesn't have our public key until it reads this. `schema.make` still validates it.

## Lines 174–179 · pin a peer's keys

```python
    def _add_peer(self, peer: str, public: dict) -> None:
        known = self.state["peers"].get(peer)
        if known and known != public:
            raise CryptoError(f"{peer} presented a different key than the one we paired with")
        self.state["peers"][peer] = public
        self._save()
```

- **176–177** **Trust on first use.** If we already know this peer under different keys, refuse. Someone may be impersonating them.
- **178–179** Save the keys.

## Lines 181–187 · receive PAIR (inviter side)

```python
    def _on_pair(self, msg: dict) -> None:
        body = msg["body"]
        token = self.state["invites"].pop(body["invite"], None)
        if token is None or not hmac.compare_digest(proof(token, body["public"]), body["proof"]):
            raise CryptoError("PAIR does not match an open invite")
        self._add_peer(msg["from"], body["public"])
        self._event("paired", f"{msg['from']} accepted our invite")
```

- **183** Find and **remove** our record of that invite (`pop`), so it can't be used twice even locally.
- **184–185** Recompute the proof from our token and the joiner's keys, and compare in constant time. A wrong proof means the sender never saw the QR code, or the keys were swapped in transit.
- **186** Pin the joiner's keys. Both sides now hold each other's keys and can derive the same pair key.

## Lines 191–194 · the key for a peer

```python
    def _key(self, peer: str) -> bytes:
        if peer not in self.state["peers"]:
            raise UnknownPeer(f"not paired with {peer}")
        return self.identity.pair_key(self.state["peers"][peer])
```

The shared key with a paired peer, or an error if we never paired.

## Lines 196–201 · send a message

```python
    def _send(self, peer: str, type_: str, body: dict, reply_to: str | None = None) -> dict:
        msg = schema.make(type_, self.name, peer, body, reply_to)
        hdr = schema.header(msg)
        wire = hdr | {"sealed": b64e(seal(self._key(peer), canonical(body), canonical(hdr)))}
        self.broker.post_message(wire)
        return msg
```

- **197** Build and validate the message.
- **198** Take its header.
- **199**, step by step:
  1. `canonical(body)`: the body as bytes
  2. `seal(key, body_bytes, canonical(hdr))`: encrypt it, with the **header as associated data**. The header isn't encrypted, but it's covered by the integrity check, so if the broker changed `from`, `to` or `type`, decryption would fail.
  3. `b64e(...)`: as text
  4. `hdr | {"sealed": ...}`: the wire message is the header plus the sealed body
- **200** Hand it to the broker.
- **201** Return the plain message. Callers use its `id` to match replies.

## Lines 203–215 · open a received message

```python
    def _open(self, wire: dict) -> dict:
        if wire.get("type") == "PAIR":
            msg = dict(wire)
        else:
            hdr = {k: wire.get(k) for k in schema.HEADER_FIELDS}
            if not isinstance(wire.get("sealed"), str):
                raise schema.SchemaError("message is not sealed")
            body = json.loads(unseal(self._key(hdr["from"]), b64d(wire["sealed"]), canonical(hdr)))
            msg = hdr | {"body": body}
        schema.validate(msg)
        if msg["to"] != self.name:
            raise schema.SchemaError("message is not addressed to us")
        return msg
```

- **204–205** PAIR is the one unsealed type; take it as is.
- **207** Rebuild the header from the wire fields.
- **208–209** Every other type **must** be sealed. An unsealed QUERY is refused.
- **210** Decrypt with the sender's pair key, using the header as associated data, then parse the JSON body. An unknown sender raises `UnknownPeer`; tampering raises `CryptoError`.
- **211** Rebuild the full message.
- **212** Strict schema validation.
- **213–214** It must be addressed to us.

## Lines 217–231 · poll: handle everything waiting

```python
    def poll(self) -> int:
        wires = self.broker.fetch_messages()
        for wire in wires:
            try:
                msg = self._open(wire)
                getattr(self, "_on_" + msg["type"].lower())(msg)
            except (schema.SchemaError, CryptoError, BundleError, UnknownPeer,
                    KeyError, TypeError, ValueError) as e:
                self._event("dropped", f"{wire.get('type')} from {wire.get('from')}: {e}")
            except Exception as e:
                self._event("error", f"{wire.get('type')} from {wire.get('from')}: {type(e).__name__}: {e}")
        return len(wires)
```

- **219** Fetch our queue from the broker. **This empties the queue at the broker**, so from here on, these messages exist only in this list.
- **222** Open and validate each message.
- **223** **Dispatch by name.** `"OFFER"` becomes `"_on_offer"`, and `getattr(self, name)` fetches that method, which is then called with the message. Adding a new message type only needs a new `_on_<type>` method.
- **224–226** A **bad message** (bad schema, bad crypto, bad bundle, unknown peer, a malformed field) is dropped, with the reason logged.
- **227–230** **Anything else that goes wrong** while handling one message (the broker failing mid-upload, an approver that crashed) is logged as an `error`, and the loop **moves on to the next message**. Before the bug-fix pass, such an error escaped `poll()`, and because the queue was already drained, every message after it was silently lost (bug E). `type(e).__name__` puts the error's class name in the log.
- **231** How many arrived. The demo loops until this is 0.

## Lines 235–245 · ask a question

```python
    def ask(self, peer: str, text: str) -> dict | None:
        key = f"{peer}|{' '.join(text.lower().split())}"
        cached = self.state["answers"].get(key)
        if cached and time.time() - cached.get("received", 0) < ANSWER_TTL:
            self._event("answer", f"cache hit for {text!r}")
            return cached
        msg = self._send(peer, "QUERY", {"text": text})
        self.state["queries"][msg["id"]] = key
        self._save()
        return None
```

- **237** A cache key: peer plus the question, lowercased with whitespace normalised (`split()` then `join` collapses runs of spaces).
- **238** Look for a cached answer.
- **239–241** Reuse it only if it arrived **less than `ANSWER_TTL` seconds ago** (an hour). `.get("received", 0)` treats an answer with no timestamp as very old. Older answers are asked again, because the owner's notes may have changed.
- **242** Otherwise send a QUERY…
- **243** …and remember its ID, so the ANSWER can be matched to it.
- **245** `None` means "sent; the answer arrives on a later poll".

## Lines 247–251 · answer a question (owner side)

```python
    def _on_query(self, msg: dict) -> None:
        peer, text = msg["from"], msg["body"]["text"]
        body, how = self.answer_packet(peer, text)
        self._send(peer, "ANSWER", body, reply_to=msg["id"])
        self._event("answered", f"{peer} asked {text!r} -> {body['sources'] or 'nothing'} ({how})")
```

- **249** Build the answer (next function).
- **250** Reply, with `reply_to` set to the question's ID.

## Lines 253–273 · build the quick answer

```python
    def answer_packet(self, peer: str, text: str) -> tuple[dict, str]:
        visible = lambda e: peer in e.allow or "*" in e.allow
        query_vec = embed(text)
        body = self.summaries.get(query_vec, self.memory.get, visible)
        how = "summary cache hit"
        if body is None:
            hits = [(s, e) for s, e in self.memory.search(text, k=len(self.memory.entries))
                    if s >= MIN_MATCH and visible(e)][:TOP_K]
            if hits:
                score, best = hits[0]
                notes = [e for _, e in hits]
                body = {"summary": self.summarizer.summarize(text, notes), "sources": [e.id for e in notes],
                        "confidence": round(min(1.0, score), 3), "version": best.version}
                self.summaries.put(query_vec, body, notes)
                how = "summarised on cache miss"
            else:
                body = {"summary": "", "sources": [], "confidence": 0.0, "version": 0}
                how = "no match"
        return body, how
```

- **256** A small function: may this peer see this entry? Answers only ever mention visible parts, so **a private part's existence isn't revealed**.
- **257** Embed the question.
- **258** Try the summary cache first, passing `memory.get` (to check freshness) and `visible`.
- **260** On a cache miss:
- **261–262** Score **all** entries, keep those that are similar enough **and** visible, then take the top 3. Searching all entries first, rather than just the top 3, matters: otherwise invisible parts could crowd out visible ones.
- **264** The best match and its score.
- **265** The matched entries, best first.
- **266–267** Summarise **only those notes** (the expensive step, done only on a miss). List their IDs. Confidence is the best score, capped at 1 and rounded to 3 decimals. Version is the best match's version.
- **268** Cache it for similar future questions.
- **270–272** Nothing matched → an empty answer.
- **273** Return the answer and how it was produced. The research harness calls this method directly, so experiments use the exact same code path.

## Lines 275–285 · receive an answer

```python
    def _on_answer(self, msg: dict) -> None:
        key = self.state["queries"].pop(msg["reply_to"] or "", None)
        if key is None:
            raise schema.SchemaError("unsolicited ANSWER")
        self.state["answers"][key] = msg["body"] | {"peer": msg["from"], "received": time.time()}
        self._save()
        b = msg["body"]
        self._event("answer", f"from {msg['from']}: {b['sources']} conf={b['confidence']} v{b['version']}")

    def last_answer(self, peer: str, text: str) -> dict | None:
        return self.state["answers"].get(f"{peer}|{' '.join(text.lower().split())}")
```

- **276** Find and remove the pending question this answers. `msg["reply_to"] or ""` turns `None` into a key that's never present.
- **277–278** No matching question → drop it. We didn't ask.
- **279** Cache it, noting who answered and **when** (`received`). `ask()` uses that time to expire it.
- **284–285** Look up a cached answer by question.

## Lines 289–295 · request a part

```python
    def request(self, peer: str, source_id: str, reason: str, intent: str) -> str:
        body = {"source_id": source_id, "reason": reason, "intent": intent}
        msg = self._send(peer, "REQUEST", body)
        self.state["requests"][msg["id"]] = {"peer": peer, **body}
        self._save()
        return msg["id"]
```

- **291–292** Send which part, why, and what we'll do with it.
- **293** Remember the request. `{"peer": peer, **body}` copies the body's fields alongside the peer. An OFFER is only accepted if it matches this.

## Lines 297–319 · the owner's CHECK

```python
    def _on_request(self, msg: dict) -> None:
        peer, b = msg["from"], msg["body"]
        sid = b["source_id"]

        def reply(status: str, detail: str) -> None:
            self._send(peer, "CHECK", {"source_id": sid, "status": status, "detail": detail}, reply_to=msg["id"])
            self._event("check", f"{peer} wants {sid}: {status} ({detail})")

        entry = self.memory.get(sid)
        if entry is None:
            return reply("NOT_FOUND", "no such part")
        allowed, why = self.judge.may_share(entry, peer, b["reason"], b["intent"])
        if not allowed:
            return reply("DENIED", why)
        if not self.memory.is_current(entry):
            return reply("STALE", f"files changed since v{entry.version} was recorded")
        if not entry.tests:
            return reply("STALE", "part has no tests, so it can't be verified")
        passed, _ = quarantine.run_tests(entry.tests, self.memory.part_root(entry))
        if not passed:
            return reply("STALE", "owner tests are failing")
        reply("READY", f"v{entry.version}")
        self._offer(peer, entry, reply_to=msg["id"], intent=b["intent"])
```

- **301–303** A helper **defined inside** the method (a closure). It uses `peer`, `sid` and `msg` from around it, so each check below is one short line.
- **305–307** Does the part exist?
- **308–310** Is this peer allowed it, for this reason and intent? This is a judge call, so an LLM judge can weigh in here.
- **311–312** Do the files still match the recorded version? If someone edited them without recording a new version, refuse: we won't ship something we haven't versioned.
- **313–314** Does the part have tests at all? If not, say so plainly. Before the bug-fix pass, this case fell through to the next check and was misreported as "owner tests are failing" (bug J).
- **315–317** Do the owner's own tests pass? (This is the owner's own code, on its own machine.) `passed, _ =` ignores the output.
- **318–319** All good: reply READY, then send the offer.

## Lines 321–334 · pack and offer

```python
    def _pack(self, peer: str, entry: Entry, intent: str) -> tuple[dict, str]:
        manifest, blobs = bundle.pack(self.identity, self._key(peer), self.name,
                                      self.memory.part_root(entry), entry, "passed")
        for blob_id, data in blobs.items():
            self.blobs.put(blob_id, data)
        mhash = bundle.manifest_hash(manifest)
        self.state["offers_out"][mhash] = {"peer": peer, "manifest": manifest, "intent": intent}
        self._save()
        return manifest, mhash
```

- **322–323** Build the signed manifest and encrypted blobs for this peer. The pair key is per-peer, so blobs are too.
- **324–325** Keep the blobs in our cache until the peer accepts. Nothing goes to the broker yet.
- **326–328** Remember the outstanding offer under its manifest hash, including the requester's intent for the provenance log.

```python
    def _offer(self, peer: str, entry: Entry, reply_to: str, intent: str) -> None:
        manifest, mhash = self._pack(peer, entry, intent)
        self._send(peer, "OFFER", {"manifest": manifest}, reply_to=reply_to)
        self._event("offer", ...)
```

- **332–333** Pack, then send **only the manifest**: the label before the box.

## Lines 336–341 · receive a CHECK

```python
    def _on_check(self, msg: dict) -> None:
        b = msg["body"]
        if b["status"] != "READY":
            self.state["requests"].pop(msg["reply_to"] or "", None)
            self._save()
        self._event("check", f"{b['source_id']} from {msg['from']}: {b['status']} ({b['detail']})")
```

- **338–340** Anything other than READY ends that request, so forget it. On READY we keep it, because an OFFER follows.

## Lines 343–357 · receive an OFFER: read the label

```python
    def _on_offer(self, msg: dict) -> None:
        peer, manifest = msg["from"], msg["body"]["manifest"]
        request = self.state["requests"].pop(msg["reply_to"] or "", None)
        if request is None or request["peer"] != peer or request["source_id"] != manifest.get("source_id"):
            raise schema.SchemaError("OFFER does not match a request we made")
        mhash = bundle.verify_manifest(manifest, peer, self.state["peers"][peer])
        ok, why = self.judge.accept_offer(manifest)
        if not ok:
            self._send(peer, "DECLINE", {"manifest_hash": mhash, "reason": why}, reply_to=msg["id"])
            self._event("declined", f"{manifest['source_id']}: {why}")
        else:
            self.state["offers_in"][mhash] = {"peer": peer, "manifest": manifest, "intent": request["intent"]}
            self._send(peer, "ACCEPT", {"manifest_hash": mhash}, reply_to=msg["id"])
            self._event("accepted", f"offer {manifest['source_id']} v{manifest['version']} ({why})")
        self._save()
```

- **345–347** The offer must answer **our** request, from **that** peer, for **that** part. Otherwise it's unsolicited, and dropped.
- **348** Check the signature (against the key pinned at pairing), the owner, the paths and the hashes.
- **349** The judge reads the label: size, file count, tests.
- **350–352** No → DECLINE with the reason. Nothing is downloaded.
- **354–356** Yes → remember the accepted offer (with our intent) and send ACCEPT.

## Lines 361–379 · upload only what's missing (owner side)

```python
    def _upload(self, peer: str, manifest: dict) -> tuple[int, int]:
        ids = bundle.blob_ids(manifest)
        missing = set(self.broker.missing(ids))
        entry = self.memory.get(manifest["source_id"])
        for f in manifest["files"]:
            if f["blob"] not in missing:
                continue
            data = self.blobs.get(f["blob"])
            if data is None and entry is not None:
                source = self.memory.part_root(entry) / f["path"]
                data = seal_content(self._key(peer), source.read_bytes()) if source.is_file() else None
            if data is None or sha256_hex(data) != f["blob"]:
                raise BundleError(f"{f['path']} changed or vanished since it was offered")
            self.broker.put_blob(f["blob"], data)
        self.broker.record_holding(owner=self.name, source_id=manifest["source_id"], version=manifest["version"],
                                   manifest_hash=bundle.manifest_hash(manifest), status="OWNER")
        return len(missing), len(ids)
```

- **363** Ask the broker which of these blobs it lacks.
- **366–367** Skip any it already has. That's why unchanged files in an update never move.
- **368** Take it from our cache…
- **369–373** …and if the cache evicted it, **re-encrypt from the source file**. Thanks to convergent encryption, that gives the same blob, but only if the file hasn't changed since the offer.
- **374–375** Missing, or different from what was offered → stop. A quiet edit can't slip into an offer that's already been accepted.
- **376** Upload. The broker re-checks the hash too.
- **377–378** Register in the registry that we own this version.
- **379** Report "uploaded X of Y".

## Lines 381–389 · receive ACCEPT

```python
    def _on_accept(self, msg: dict) -> None:
        mhash = msg["body"]["manifest_hash"]
        out = self.state["offers_out"].get(mhash)
        if out is None or out["peer"] != msg["from"]:
            raise schema.SchemaError("ACCEPT for an offer we didn't make")
        sent, total = self._upload(msg["from"], out["manifest"])
        self._send(msg["from"], "BUNDLE", {"manifest_hash": mhash, "blobs": bundle.blob_ids(out["manifest"])},
                   reply_to=msg["id"])
        self._event("bundle", f"uploaded {sent} of {total} layers for {out['manifest']['source_id']}")
```

- **383–385** It must be an offer we made, to this peer.
- **386** Upload what's missing.
- **387–388** Tell them it's ready: the manifest hash and the blob list. The offer stays in `offers_out` until the ACK arrives.

## Lines 391–397 · receive DECLINE

```python
    def _on_decline(self, msg: dict) -> None:
        out = self.state["offers_out"].pop(msg["body"]["manifest_hash"], None)
        self._save()
        if out:
            self._provenance(role="owner", event="declined", peer=msg["from"],
                             source_id=out["manifest"]["source_id"], reason=msg["body"]["reason"])
        self._event("declined", f"{msg['from']}: {msg['body']['reason']}")
```

- **392** Forget the offer.
- **394–396** Log the decline in provenance, so the owner's record shows what was refused and why.

## Lines 399–407 · receive BUNDLE

```python
    def _on_bundle(self, msg: dict) -> None:
        mhash = msg["body"]["manifest_hash"]
        incoming = self.state["offers_in"].pop(mhash, None)
        self._save()
        if incoming is None or incoming["peer"] != msg["from"]:
            raise schema.SchemaError("BUNDLE for an offer we didn't accept")
        if sorted(msg["body"]["blobs"]) != sorted(bundle.blob_ids(incoming["manifest"])):
            raise BundleError("BUNDLE blob list does not match the manifest")
        self._receive(msg["from"], incoming["manifest"], mhash, incoming.get("intent", ""))
```

- **401–404** It must be for an offer **we accepted**, from that peer.
- **405–406** The blob list must be exactly the one in the signed manifest (order doesn't matter, hence `sorted`).
- **407** Run the receive pipeline, using the **manifest we verified at offer time**, not anything new from this message.

## Lines 411–424 · fetch blobs: cache first

```python
    def _fetch(self, manifest: dict) -> tuple[dict[str, bytes], int, list[str]]:
        blobs, fetched, problems = {}, 0, []
        for blob_id in bundle.blob_ids(manifest):
            data = self.blobs.get(blob_id)
            if data is None:
                try:
                    data = self.broker.get_blob(blob_id)
                except BrokerError as e:
                    problems.append(str(e))
                    continue
                self.blobs.put(blob_id, data)
                fetched += 1
            blobs[blob_id] = data
        return blobs, fetched, problems
```

- **414** Try the local cache. `get` re-hashes the blob and discards a bad copy.
- **415–422** Not cached → download it (the client re-hashes it too), cache it, and count it as fetched. A download failure, including a hash mismatch, is **recorded as a problem** rather than raised, so the flow continues and ends in a logged REJECT instead of a crash.
- **424** The blobs, how many were downloaded, and any problems.

## Lines 426–467 · receive: quarantine, assess, decide, acknowledge

```python
    def _receive(self, peer: str, manifest: dict, mhash: str, intent: str = ""):
        sid, version = manifest["source_id"], manifest["version"]
        blobs, fetched, problems = self._fetch(manifest)
        self._event("fetch", f"{sid} v{version}: downloaded {fetched} of {len(manifest['files'])} layers")
```

- **428–429** Get every blob, and report how many actually crossed the network.

```python
        qdir = self.dir / "quarantine" / mhash[:16]
        shutil.rmtree(qdir, ignore_errors=True)
        qdir.mkdir(parents=True)
        if not problems:
            try:
                bundle.unpack(self._key(peer), manifest, blobs, qdir)
            except BundleError as e:
                problems.append(str(e))
```

- **431** A quarantine folder named after the manifest, e.g. `.ack/quarantine/8f70428e5ecc6724`.
- **432–433** Start from a clean, empty folder.
- **434–438** Unpack (decrypting and checking every file) **into quarantine only**. Failures become problems.

```python
        prior = self.state["installs"].get(f"{peer}/{sid}")
        self.approval_context = {"peer": peer, "manifest": manifest, "manifest_hash": mhash,
                                 "qdir": str(qdir), "intent": intent, "fetched": fetched}
        run = not problems and self.approver(
            "run-tests", f"run {manifest['tests']['cmd']} for {peer}/{sid} v{version} in {qdir.relative_to(self.root)}")
        report = quarantine.assess(manifest, mhash, qdir, self.root, prior, run, problems, intent)
        decision = self.judge.decide(report)
        self.approval_context = {**self.approval_context, "report": report, "decision": decision}
```

- **440** What we installed from this source before, if anything. It's used for conflict and diff checks, and below.
- **441–442** Record what's about to be approved, so an approver that shows more than one line (the web app) can display the files.
- **443–444** **Approval 1.** Only if there were no problems, ask the approver whether to run the tests. `and` short-circuits: with problems, we don't even ask.
- **445** Build the full report.
- **446** The judge decides.
- **447** Add the report and decision to the context, for the second approval. `{**old, ...}` copies the old dict and adds keys.

```python
        if decision.action != "REJECT" and not self.approver(
                "install", f"{decision.action} {peer}/{sid} v{version}: {decision.reason}"):
            decision = Decision("REJECT", "install not approved")
        if decision.action != "REJECT":
            self._install(peer, manifest, mhash, qdir, decision, intent)
```

- **448–450** **Approval 2.** Even after a positive decision, ask before anything touches the real project. A no turns it into a REJECT with that reason.
- **451–452** Install only for ADOPT or ADAPT.

```python
        self._send(peer, "ACK", {"manifest_hash": mhash, "decision": decision.action, "reason": decision.reason})
        if decision.action != "REJECT" or prior is None:
            self.broker.record_holding(owner=peer, source_id=sid, version=version,
                                       manifest_hash=mhash, status=decision.action)
        self._provenance(role="receiver", event="decided", ...)
        self._event("decided", f"{decision.action} {peer}/{sid} v{version}: {decision.reason}")
        self.last_report = report
        self.approval_context = {}
        return report, decision
```

- **454** Tell the owner the outcome and the reason.
- **455–459** Record the outcome in the broker's registry, **except** when we rejected an *update* to something we already hold. In that case the earlier version is still installed, and the registry must keep saying so. Before the bug-fix pass, a rejected update overwrote that record with "REJECT", which hid what we hold and made the audit report a false mismatch (bug C).
- **460–463** Write the full provenance record: what, which version, which manifest, the decision and why, plus the evidence behind it (tests, conflicts, dependencies, problems).
- **466** Clear the approval context. Nothing is pending any more.

## Lines 469–496 · install: adopt or adapt

```python
    def _install(self, peer: str, manifest: dict, mhash: str, qdir: Path, decision: Decision,
                 intent: str = "") -> None:
        sid = manifest["source_id"]
        base = self.root if decision.action == "ADOPT" else self.root / "vendor" / peer / sid
        files = {}
        for f in manifest["files"]:
            dst = base / f["path"]
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(qdir / f["path"], dst)
            files[f["path"]] = {"sha256": f["sha256"], "installed_at": str(dst.relative_to(self.root))}
```

- **472** **ADOPT** puts files at their own paths in the project. **ADAPT** puts them under `vendor/<peer>/<part>/`, so they don't collide with our own files. (`sid` is safe to use as a folder name: `verify_manifest` checked it.)
- **474–478** Copy each verified file out of quarantine, and record where it went and its hash. The audit later uses that to detect drift.

```python
        prior = self.state["installs"].get(f"{peer}/{sid}")
        kept = {f["installed_at"] for f in files.values()}
        for old in (prior or {}).get("files", {}).values():
            path = self.root / old["installed_at"]
            if old["installed_at"] not in kept and path.is_file() and sha256_hex(path.read_bytes()) == old["sha256"]:
                path.unlink()
```

**Cleaning up after the previous version** (added in the bug-fix pass, bug D). An update can drop a file, or move everything (ADOPT last time, ADAPT this time). Before this fix, the old copies stayed behind, orphaned and untracked.

- **479** What the previous version installed, if anything.
- **480** A set of the locations this version just wrote.
- **481–486** For each file the previous version installed: if this version **didn't** write to the same place, and the file is **still exactly what we installed** (its hash matches), delete it. A file someone edited locally is never deleted, so no local work is lost.

```python
        if decision.action == "ADAPT":
            (base / "BUILT_ON.md").write_text(
                f"# Built on {peer}'s {sid} v{manifest['version']}\n\n"
                f"Manifest: {mhash}\n\n"
                f"Vendored here instead of the project root because of: {decision.reason}.\n"
                f"Write any wrapper against this copy; updates from {peer} land here.\n")
        self.state["installs"][f"{peer}/{sid}"] = {
            "peer": peer, "source_id": sid, "version": manifest["version"],
            "manifest_hash": mhash, "action": decision.action, "intent": intent, "files": files}
        self._save()
```

- **487–492** For ADAPT, write a provenance note next to the vendored files: "built on beta's slugify v1", with the manifest hash and why it was vendored.
- **493–496** Save the install record. Updates, conflict checks and the audit all read it.

## Lines 498–513 · receive ACK (owner side)

```python
    def _on_ack(self, msg: dict) -> None:
        b = msg["body"]
        out = self.state["offers_out"].pop(b["manifest_hash"], None)
        if out is None or out["peer"] != msg["from"]:
            raise schema.SchemaError("ACK for a bundle we didn't send")
        manifest = out["manifest"]
        if b["decision"] != "REJECT":
            self.state["shares"].setdefault(manifest["source_id"], {})[msg["from"]] = {
                "manifest_hash": b["manifest_hash"],
                "files": {f["path"]: f["sha256"] for f in manifest["files"]},
                "blobs": {f["path"]: f["blob"] for f in manifest["files"]}}
        self._save()
        self._provenance(role="owner", event="acked", ...)
        self._event("ack", ...)
```

- **500–502** It must acknowledge something we sent to that peer. The offer is now complete, so remove it.
- **504–508** If they took it, remember that **this peer holds this version**: its manifest hash, each file's hash (to compute what changed later), and each file's blob ID (to build a diff later from our cached copy). `setdefault(...)[peer] = ...` creates the part's entry if needed, then sets this peer inside it.
- **510–512** Log it in the owner's provenance, including the requester's intent.

## Lines 517–545 · publish an update (owner side)

```python
    def publish_update(self, source_id: str) -> int:
        entry = self.memory.get(source_id)
        if entry is None:
            raise KeyError(source_id)
        if self.memory.is_current(entry):
            return 0
        passed, output = quarantine.run_tests(entry.tests, self.memory.part_root(entry))
        if not passed:
            raise RuntimeError(f"not publishing {source_id}: tests fail\n{output}")
        entry = self.memory.bump(source_id)
```

- **526–527** If nothing changed, there's nothing to publish.
- **528–530** Never publish a version whose own tests fail.
- **531** Record the new version: bump it and re-snapshot the hashes.

```python
        self.broker.record_holding(owner=self.name, source_id=source_id, version=entry.version,
                                   manifest_hash="", status="OWNER")
```

- **534–535** **Register the new version now.** If every holder declines the update, nothing is ever uploaded, and the registry would never learn that v2 exists, so holders' stale copies would go undetected. (The provenance experiment found this bug.)

```python
        sent = 0
        for peer, held in self.state["shares"].get(source_id, {}).items():
            manifest, mhash = self._pack(peer, entry, intent="update")
            changed = bundle.changed_paths(held["files"], manifest)
            diff = self._update_diff(peer, entry, held, changed)
            self._send(peer, "UPDATE_AVAILABLE", {"base_manifest_hash": held["manifest_hash"],
                                                  "manifest": manifest, "changed": changed, "diff": diff})
            self._event("update", ...)
            sent += 1
        return sent
```

- **537** For every peer that holds this part…
- **538** …pack the new version for them. It's stored as an outstanding offer, but nothing is uploaded yet.
- **539** Which files differ from what they hold.
- **540** A readable diff.
- **541–542** Send the short notice: what they had (`base_manifest_hash`), the new signed manifest, the changed paths and the diff.

## Lines 547–561 · build the update diff

```python
    def _update_diff(self, peer: str, entry: Entry, held: dict, changed: list[str]) -> str:
        key, root, chunks = self._key(peer), self.memory.part_root(entry), []
        for path in changed:
            old = b""
            if path in held["files"]:
                blob = self.blobs.get(held.get("blobs", {}).get(path, ""))
                if blob is None:
                    chunks.append(f"(old {path} no longer cached; diff unavailable)\n")
                    continue
                old = unseal_content(key, held["files"][path], blob)
            new = (root / path).read_bytes() if path in entry.files and (root / path).is_file() else b""
            chunks.append(quarantine.diff_bytes(path, old, new))
        diff = "".join(chunks)
        return diff if len(diff) <= MAX_UPDATE_DIFF else diff[:MAX_UPDATE_DIFF] + "\n... (diff truncated)\n"
```

**The problem:** the owner's files have already changed, so where does the *old* version come from? **The answer:** the encrypted blob we sent the peer is still in our cache, and we can decrypt it ourselves.

- **551** A newly added file has no old version: empty.
- **552** Look up the old blob ID for this path, then fetch the blob from our cache (it's re-hashed on the way).
- **553–555** If it was evicted, say so instead of failing.
- **556** Decrypt it with the pair key and the old file hash.
- **557** The new version from disk, or empty if the file was removed.
- **558** Diff old against new.
- **560–561** Join the pieces, capping the length.

## Lines 563–583 · receive UPDATE_AVAILABLE (receiver side)

```python
    def _on_update_available(self, msg: dict) -> None:
        peer, b = msg["from"], msg["body"]
        manifest = b["manifest"]
        mhash = bundle.verify_manifest(manifest, peer, self.state["peers"][peer])
        key = f"{peer}/{manifest['source_id']}"
        prior = self.state["installs"].get(key)
        if prior is None or prior["manifest_hash"] != b["base_manifest_hash"]:
            raise schema.SchemaError("UPDATE_AVAILABLE base does not match what we hold")
        self.state["updates"][key] = {"manifest_hash": mhash, "version": manifest["version"],
                                      "changed": b["changed"], "diff": b["diff"]}
        self._event("update", ...)
        ok, why = self.judge.want_update(manifest, b["changed"], b["diff"])
        if not ok:
            self._send(peer, "DECLINE", {"manifest_hash": mhash, "reason": why}, reply_to=msg["id"])
            self._event("declined", ...)
        else:
            self.state["offers_in"][mhash] = {"peer": peer, "manifest": manifest,
                                              "intent": prior.get("intent", "")}
            self._send(peer, "ACCEPT", {"manifest_hash": mhash}, reply_to=msg["id"])
        self._save()
```

- **566** Verify the new manifest's signature and owner, exactly as for an offer.
- **568–570** It must be an update to **what we actually hold**. Otherwise drop it.
- **571–572** Keep the notice and its diff, so a human or tool can review it.
- **575** The judge decides whether to pull it. An LLM judge could read the diff here.
- **576–578** No → DECLINE. Nothing is uploaded or downloaded, and the registry still knows we're behind.
- **579–582** Yes → treat it exactly like an accepted offer: remember it (keeping our original intent) and send ACCEPT. From here the normal path takes over: `_on_accept` (owner uploads only the changed blobs) → `_on_bundle` → `_receive` (quarantine, approvals, judge) → ACK.
