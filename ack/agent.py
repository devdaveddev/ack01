"""An agent: one project, one identity, talks to peers through the broker.

Everything is message-driven. Calls like `ask` and `request` send a message and
return; `poll` fetches and handles whatever has arrived. All state lives in
<project>/.ack/ so an agent can stop and resume.

Owner side (B):  QUERY -> ANSWER;  REQUEST -> CHECK (+ OFFER);  ACCEPT -> upload + BUNDLE;  ACK
                 part changed -> UPDATE_AVAILABLE (manifest + diff)
Receiver (A):    ANSWER -> cache;  OFFER / UPDATE_AVAILABLE -> ACCEPT or DECLINE;
                 BUNDLE -> quarantine -> decide -> ACK
"""

import hmac
import json
import secrets
import shutil
import time
from collections.abc import Callable
from pathlib import Path

from . import bundle, quarantine, schema
from .announce import Announcements
from .bundle import BundleError
from .client import BrokerClient, BrokerError
from .crypto import CryptoError, Identity, b64d, b64e, canonical, seal, seal_content, sha256_hex, unseal, unseal_content
from .judge import Decision, RuleJudge
from .memory import Entry, ProjectMemory, embed, tokens
from .pairing import Invite, proof, token_hash
from .spelling import correct as spell_correct
from .summarize import ExtractiveSummarizer, SummaryCache

# approver(action, detail) -> bool. Actions: "run-tests", "install".
Approver = Callable[[str, str], bool]
MIN_MATCH = 0.10  # one shared rare word scores ~0.12 with the stand-in embedding; unrelated text ~0
TOP_K = 3
MAX_UPDATE_DIFF = 20_000
ANSWER_TTL = 3600  # seconds a cached quick answer is reused before asking again
# A question made only of these words ("hi", "what do you share?", "list your parts") gets the
# catalogue of parts this peer may request, instead of an empty answer.
CATALOG_WORDS = {"hi", "hello", "hey", "share", "shared", "sharing", "have", "got", "list", "part", "offer",
                 "available", "anything", "everything", "catalog", "catalogue", "help", "thing", "stuff", "good"}


class UnknownPeer(Exception):
    pass


def deny_all(action: str, detail: str) -> bool:
    return False


class BlobStore:
    """Local cache of recent blobs, least recently used evicted first.

    It makes repeat requests and offline work instant, but it is never trusted:
    every read is re-checked against its hash, and the hash always comes from
    the owner's signed manifest.
    """

    def __init__(self, path: Path, max_bytes: int = 256 << 20):
        self.path = path
        self.max_bytes = max_bytes
        path.mkdir(parents=True, exist_ok=True)

    def has(self, blob_id: str) -> bool:
        return (self.path / blob_id).is_file()

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

    def put(self, blob_id: str, data: bytes) -> None:
        (self.path / blob_id).write_bytes(data)
        self._evict(keep=blob_id)

    def _evict(self, keep: str) -> None:
        files = sorted((p.stat().st_mtime, p) for p in self.path.iterdir() if p.is_file())
        total = sum(p.stat().st_size for _, p in files)
        for _, path in files:
            if total <= self.max_bytes:
                break
            if path.name != keep:
                total -= path.stat().st_size
                path.unlink(missing_ok=True)


class Agent(Announcements):
    def __init__(self, name: str, root: Path, broker_url: str, judge=None,
                 approver: Approver = deny_all, log: Callable[[str], None] | None = print,
                 summarizer=None, cache_bytes: int = 256 << 20):
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
        self.last_correction: list[tuple[str, str]] = []
        # What the approver is being asked about, for approvers that show more than one line
        # (the web app). Set just before each approver call in _receive.
        self.approval_context: dict = {}
        self.state = self._load("state.json", {
            "peers": {},       # name -> public keys
            "invites": {},     # token_hash -> token (invites we issued)
            "queries": {},     # query msg id -> cache key
            "answers": {},     # cache key -> answer body
            "requests": {},    # request msg id -> {peer, source_id, reason, intent}
            "offers_in": {},   # manifest hash -> {peer, manifest}  (offers and updates we accepted)
            "offers_out": {},  # manifest hash -> {peer, manifest, intent}
            "updates": {},     # "peer/source_id" -> latest UPDATE_AVAILABLE we saw
            "installs": {},    # "peer/source_id" -> what we adopted and where
            "shares": {},      # source_id -> {peer: {manifest_hash, files, blobs}}
            "published": {},   # announcement id -> {statement, copies, votes, status}
            "inbox": {},       # announcement id -> {statement, check, files, vote, status}
            "keys": {},        # name -> public keys fetched from the broker, pinned on first use
            "sign_policy": "auto",
        })

    # ---- persistence and logging ----

    def _load(self, name: str, default: dict) -> dict:
        path = self.dir / name
        return default | json.loads(path.read_text()) if path.exists() else default

    def _save(self) -> None:
        tmp = self.dir / "state.json.tmp"
        tmp.write_text(json.dumps(self.state, indent=1))
        tmp.replace(self.dir / "state.json")

    def _event(self, kind: str, text: str) -> None:
        self.events.append((kind, text))
        if self.log:
            self.log(f"[{self.name}] {kind}: {text}")

    def _provenance(self, **record) -> None:
        with (self.dir / "provenance.jsonl").open("a") as f:
            f.write(json.dumps({"ts": time.time(), "agent": self.name, **record}) + "\n")

    def provenance(self) -> list[dict]:
        path = self.dir / "provenance.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    # ---- setup and pairing ----

    def register(self) -> None:
        self.broker.register(self.identity.public())

    def share(self, entry: Entry) -> Entry:
        """Add a part of this project to memory so peers can find and request it."""
        return self.memory.add(entry)

    def invite(self, bundle_hash: str | None = None) -> Invite:
        token = secrets.token_urlsafe(16)
        self.broker.create_invite(token_hash(token))
        self.state["invites"][token_hash(token)] = token
        self._save()
        return Invite(self.broker.url, token, self.name, self.identity.public(), bundle_hash)

    def join(self, invite: Invite) -> None:
        inviter = self.broker.redeem_invite(invite.token_hash)
        if inviter != invite.agent:
            raise BrokerError(409, f"invite belongs to {inviter}, not {invite.agent}")
        self._add_peer(invite.agent, invite.public)
        body = {"invite": invite.token_hash, "public": self.identity.public(),
                "proof": proof(invite.token, self.identity.public())}
        self.broker.post_message(schema.make("PAIR", self.name, invite.agent, body))
        self._event("paired", f"joined {invite.agent} via invite")

    def _add_peer(self, peer: str, public: dict) -> None:
        known = self.state["peers"].get(peer)
        if known and known != public:
            raise CryptoError(f"{peer} presented a different key than the one we paired with")
        self.state["peers"][peer] = public
        self._save()

    def _on_pair(self, msg: dict) -> None:
        body = msg["body"]
        token = self.state["invites"].pop(body["invite"], None)
        if token is None or not hmac.compare_digest(proof(token, body["public"]), body["proof"]):
            raise CryptoError("PAIR does not match an open invite")
        self._add_peer(msg["from"], body["public"])
        self._event("paired", f"{msg['from']} accepted our invite")

    # ---- transport ----

    def _key(self, peer: str) -> bytes:
        if peer not in self.state["peers"]:
            raise UnknownPeer(f"not paired with {peer}")
        return self.identity.pair_key(self.state["peers"][peer])

    def _send(self, peer: str, type_: str, body: dict, reply_to: str | None = None) -> dict:
        msg = schema.make(type_, self.name, peer, body, reply_to)
        hdr = schema.header(msg)
        wire = hdr | {"sealed": b64e(seal(self._key(peer), canonical(body), canonical(hdr)))}
        self.broker.post_message(wire)
        return msg

    def _open(self, wire: dict) -> dict:
        if wire.get("type") == "PAIR" or wire.get("type") in schema.FROM_BROKER:
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

    def poll(self) -> int:
        """Handle everything waiting at the broker. Returns how many messages arrived."""
        wires = self.broker.fetch_messages()
        for wire in wires:
            try:
                msg = self._open(wire)
                getattr(self, "_on_" + msg["type"].lower())(msg)
            except (schema.SchemaError, CryptoError, BundleError, UnknownPeer,
                    KeyError, TypeError, ValueError) as e:
                self._event("dropped", f"{wire.get('type')} from {wire.get('from')}: {e}")
            except Exception as e:
                # The queue is already drained, so letting one failure (a broker blip mid-upload,
                # an approver that crashed) escape would silently lose every message after it.
                self._event("error", f"{wire.get('type')} from {wire.get('from')}: {type(e).__name__}: {e}")
        return len(wires)

    # ---- 1-2: query and quick answer ----

    def ask(self, peer: str, text: str, exact: bool = False) -> dict | None:
        """Returns a cached answer if we have one, else sends QUERY and returns None.

        Spelling is fixed first (unless exact=True); self.last_correction says what changed.
        """
        typed = text
        text, self.last_correction = (text, []) if exact else spell_correct(text, self.memory.vocabulary())
        key = f"{peer}|{' '.join(text.lower().split())}"
        if text != typed:
            self.state.setdefault("typed", {})[key] = typed
        cached = self.state["answers"].get(key)
        if cached and time.time() - cached.get("received", 0) < ANSWER_TTL:
            self._event("answer", f"cache hit for {text!r}")
            return cached
        msg = self._send(peer, "QUERY", {"text": text})
        self.state["queries"][msg["id"]] = key
        self._save()
        return None

    def _on_query(self, msg: dict) -> None:
        peer, text = msg["from"], msg["body"]["text"]
        body, how = self.answer_packet(peer, text)
        self._send(peer, "ANSWER", body, reply_to=msg["id"])
        self._event("answered", f"{peer} asked {text!r} -> {body['sources'] or 'nothing'} ({how})")

    def answer_packet(self, peer: str, text: str) -> tuple[dict, str]:
        """Build the ANSWER body for a query: summary cache, else top-k match and summarise."""
        # Only mention parts this peer may request; don't leak that private ones exist.
        visible = lambda e: peer in e.allow or "*" in e.allow
        asked = text
        # The asker already fixed ordinary typos; this pass knows our project's own words too.
        text, corrections = spell_correct(text, self.memory.vocabulary())
        note = ("Reading " + ", ".join(f'"{typed}" as "{read}"' for typed, read in corrections) + ". ") if corrections else ""
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
            elif set(tokens(text)) <= CATALOG_WORDS:
                parts = [e for e in self.memory.entries.values() if visible(e)]
                body = {"summary": ("Parts I can share with you: " + "; ".join(f"{e.id} ({e.topic})" for e in parts)
                                    + ". Ask about one, or request it by name.") if parts
                        else "I'm not sharing anything with you yet.",
                        "sources": [e.id for e in parts], "confidence": 0.0, "version": 0}
                how = "listed what I share"
            else:
                parts = [e for e in self.memory.entries.values() if visible(e)]
                listing = "; ".join(f"{e.id} ({e.topic})" for e in parts)
                body = {"summary": (f"Nothing I share matches that. What I can share: {listing}." if parts
                                    else "Nothing I share matches that, and I'm not sharing anything with you yet."),
                        "sources": [], "confidence": 0.0, "version": 0}
                how = "no match"
        if note and body["sources"] and body["confidence"] > 0:
            body = {**body, "summary": note + body["summary"]}
        self._event("answer-read", f"{asked!r} read as {text!r}") if corrections else None
        return body, how


    def _on_answer(self, msg: dict) -> None:
        key = self.state["queries"].pop(msg["reply_to"] or "", None)
        if key is None:
            raise schema.SchemaError("unsolicited ANSWER")
        self.state["answers"][key] = msg["body"] | {"peer": msg["from"], "received": time.time(),
                                                    "typed": self.state.get("typed", {}).pop(key, None)}
        self._save()
        b = msg["body"]
        self._event("answer", f"from {msg['from']}: {b['sources']} conf={b['confidence']} v{b['version']}")

    def last_answer(self, peer: str, text: str) -> dict | None:
        return self.state["answers"].get(f"{peer}|{' '.join(text.lower().split())}")

    # ---- 3-5: request, check, offer ----

    def request(self, peer: str, source_id: str, reason: str, intent: str) -> str:
        """Ask for a part: why we need it, and what we plan to do with it."""
        body = {"source_id": source_id, "reason": reason, "intent": intent}
        msg = self._send(peer, "REQUEST", body)
        self.state["requests"][msg["id"]] = {"peer": peer, **body}
        self._save()
        return msg["id"]

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

    def _pack(self, peer: str, entry: Entry, intent: str) -> tuple[dict, str]:
        manifest, blobs = bundle.pack(self.identity, self._key(peer), self.name,
                                      self.memory.part_root(entry), entry, "passed")
        for blob_id, data in blobs.items():
            self.blobs.put(blob_id, data)
        mhash = bundle.manifest_hash(manifest)
        self.state["offers_out"][mhash] = {"peer": peer, "manifest": manifest, "intent": intent}
        self._save()
        return manifest, mhash

    def _offer(self, peer: str, entry: Entry, reply_to: str, intent: str) -> None:
        manifest, mhash = self._pack(peer, entry, intent)
        self._send(peer, "OFFER", {"manifest": manifest}, reply_to=reply_to)
        self._event("offer", f"{entry.id} v{entry.version} to {peer}: {len(manifest['files'])} files, {mhash[:12]}")

    def _on_check(self, msg: dict) -> None:
        b = msg["body"]
        if b["status"] != "READY":
            self.state["requests"].pop(msg["reply_to"] or "", None)
            self._save()
        self._event("check", f"{b['source_id']} from {msg['from']}: {b['status']} ({b['detail']})")

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

    # ---- 6: bundle ----

    def _upload(self, peer: str, manifest: dict) -> tuple[int, int]:
        ids = bundle.blob_ids(manifest)
        missing = set(self.broker.missing(ids))
        entry = self.memory.get(manifest["source_id"])
        for f in manifest["files"]:
            if f["blob"] not in missing:
                continue
            data = self.blobs.get(f["blob"])
            if data is None and entry is not None:
                # Evicted from the cache: re-seal from source. Convergent encryption gives
                # the same blob only if the file is unchanged since the offer.
                source = self.memory.part_root(entry) / f["path"]
                data = seal_content(self._key(peer), source.read_bytes()) if source.is_file() else None
            if data is None or sha256_hex(data) != f["blob"]:
                raise BundleError(f"{f['path']} changed or vanished since it was offered")
            self.broker.put_blob(f["blob"], data)
        self.broker.record_holding(owner=self.name, source_id=manifest["source_id"], version=manifest["version"],
                                   manifest_hash=bundle.manifest_hash(manifest), status="OWNER")
        return len(missing), len(ids)

    def _on_accept(self, msg: dict) -> None:
        mhash = msg["body"]["manifest_hash"]
        out = self.state["offers_out"].get(mhash)
        if out is None or out["peer"] != msg["from"]:
            raise schema.SchemaError("ACCEPT for an offer we didn't make")
        sent, total = self._upload(msg["from"], out["manifest"])
        self._send(msg["from"], "BUNDLE", {"manifest_hash": mhash, "blobs": bundle.blob_ids(out["manifest"])},
                   reply_to=msg["id"])
        self._event("bundle", f"uploaded {sent} of {total} layers for {out['manifest']['source_id']}")

    def _on_decline(self, msg: dict) -> None:
        out = self.state["offers_out"].pop(msg["body"]["manifest_hash"], None)
        self._save()
        if out:
            self._provenance(role="owner", event="declined", peer=msg["from"],
                             source_id=out["manifest"]["source_id"], reason=msg["body"]["reason"])
        self._event("declined", f"{msg['from']}: {msg['body']['reason']}")

    def _on_bundle(self, msg: dict) -> None:
        mhash = msg["body"]["manifest_hash"]
        incoming = self.state["offers_in"].pop(mhash, None)
        self._save()
        if incoming is None or incoming["peer"] != msg["from"]:
            raise schema.SchemaError("BUNDLE for an offer we didn't accept")
        if sorted(msg["body"]["blobs"]) != sorted(bundle.blob_ids(incoming["manifest"])):
            raise BundleError("BUNDLE blob list does not match the manifest")
        self._receive(msg["from"], incoming["manifest"], mhash, incoming.get("intent", ""))

    # ---- 7-9: assess, decide, ack ----

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

    def _receive(self, peer: str, manifest: dict, mhash: str, intent: str = ""):
        sid, version = manifest["source_id"], manifest["version"]
        blobs, fetched, problems = self._fetch(manifest)
        self._event("fetch", f"{sid} v{version}: downloaded {fetched} of {len(manifest['files'])} layers")

        qdir = self.dir / "quarantine" / mhash[:16]
        shutil.rmtree(qdir, ignore_errors=True)
        qdir.mkdir(parents=True)
        if not problems:
            try:
                bundle.unpack(self._key(peer), manifest, blobs, qdir)
            except BundleError as e:
                problems.append(str(e))

        prior = self.state["installs"].get(f"{peer}/{sid}")
        self.approval_context = {"peer": peer, "manifest": manifest, "manifest_hash": mhash,
                                 "qdir": str(qdir), "intent": intent, "fetched": fetched}
        run = not problems and self.approver(
            "run-tests", f"run {manifest['tests']['cmd']} for {peer}/{sid} v{version} in {qdir.relative_to(self.root)}")
        report = quarantine.assess(manifest, mhash, qdir, self.root, prior, run, problems, intent)
        decision = self.judge.decide(report)
        self.approval_context = {**self.approval_context, "report": report, "decision": decision}
        if decision.action != "REJECT" and not self.approver(
                "install", f"{decision.action} {peer}/{sid} v{version}: {decision.reason}"):
            decision = Decision("REJECT", "install not approved")
        if decision.action != "REJECT":
            self._install(peer, manifest, mhash, qdir, decision, intent)

        self._send(peer, "ACK", {"manifest_hash": mhash, "decision": decision.action, "reason": decision.reason})
        if decision.action != "REJECT" or prior is None:
            # Rejecting an update leaves the earlier version installed, so the registry
            # should keep saying we hold that, not that we hold nothing.
            self.broker.record_holding(owner=peer, source_id=sid, version=version,
                                       manifest_hash=mhash, status=decision.action)
        self._provenance(role="receiver", event="decided", peer=peer, source_id=sid, version=version,
                         manifest_hash=mhash, decision=decision.action, reason=decision.reason,
                         tests_ran=report.tests_ran, tests_passed=report.tests_passed,
                         conflicts=report.conflicts, missing_deps=report.missing_deps, problems=report.problems)
        self._event("decided", f"{decision.action} {peer}/{sid} v{version}: {decision.reason}")
        self.last_report = report
        self.approval_context = {}
        return report, decision

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
        prior = self.state["installs"].get(f"{peer}/{sid}")
        kept = {f["installed_at"] for f in files.values()}
        for old in (prior or {}).get("files", {}).values():
            path = self.root / old["installed_at"]
            # Remove a file the old version put somewhere this version doesn't, but only if
            # it is still exactly what we installed; never delete local edits.
            if old["installed_at"] not in kept and path.is_file() and sha256_hex(path.read_bytes()) == old["sha256"]:
                path.unlink()
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
        self._provenance(role="owner", event="acked", peer=msg["from"], source_id=manifest["source_id"],
                         version=manifest["version"], manifest_hash=b["manifest_hash"],
                         intent=out.get("intent", ""), decision=b["decision"], reason=b["reason"])
        self._event("ack", f"{msg['from']} {b['decision']} {manifest['source_id']} v{manifest['version']}: {b['reason']}")

    # ---- updates: announce what changed, receiver pulls ----

    def publish_update(self, source_id: str) -> int:
        """Bump a changed part and tell every peer holding it. Returns peers notified.

        Sends UPDATE_AVAILABLE (new manifest + diff) only. Nothing is uploaded until a
        peer accepts, and then only the layers the broker doesn't already have.
        """
        entry = self.memory.get(source_id)
        if entry is None:
            raise KeyError(source_id)
        if self.memory.is_current(entry):
            return 0
        passed, output = quarantine.run_tests(entry.tests, self.memory.part_root(entry))
        if not passed:
            raise RuntimeError(f"not publishing {source_id}: tests fail\n{output}")
        entry = self.memory.bump(source_id)
        # Tell the registry a new version exists now, not on first upload: if every peer
        # declines, nothing is uploaded, and holders would never learn they are behind.
        self.broker.record_holding(owner=self.name, source_id=source_id, version=entry.version,
                                   manifest_hash="", status="OWNER")
        sent = 0
        for peer, held in self.state["shares"].get(source_id, {}).items():
            manifest, mhash = self._pack(peer, entry, intent="update")
            changed = bundle.changed_paths(held["files"], manifest)
            diff = self._update_diff(peer, entry, held, changed)
            self._send(peer, "UPDATE_AVAILABLE", {"base_manifest_hash": held["manifest_hash"],
                                                  "manifest": manifest, "changed": changed, "diff": diff})
            self._event("update", f"{source_id} v{entry.version} available to {peer}: changed {changed}")
            sent += 1
        return sent

    def _update_diff(self, peer: str, entry: Entry, held: dict, changed: list[str]) -> str:
        """Diff the peer's version against ours, using our cached copy of what we sent them."""
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
        self._event("update", f"{manifest['source_id']} v{prior['version']} -> v{manifest['version']} "
                              f"available, changed {b['changed']}")
        ok, why = self.judge.want_update(manifest, b["changed"], b["diff"])
        if not ok:
            self._send(peer, "DECLINE", {"manifest_hash": mhash, "reason": why}, reply_to=msg["id"])
            self._event("declined", f"update {manifest['source_id']}: {why}")
        else:
            self.state["offers_in"][mhash] = {"peer": peer, "manifest": manifest,
                                              "intent": prior.get("intent", "")}
            self._send(peer, "ACCEPT", {"manifest_hash": mhash}, reply_to=msg["id"])
        self._save()
