"""Announcements: one publisher, many signatories, a quorum.

Paired request/offer (agent.py) is one-to-one: B asks A for a part, A decides. An
announcement is one-to-many: the publisher states "here is a change, its digest is D",
signs that statement, and the broker fans a copy out to every subscriber. Each
subscriber recomputes the digest on the copy it actually received and signs ACK or NAK.
The publisher set the quorum (k of n ACKs); the broker counts signed votes, and anyone
involved can see whether the change is sealed, held, or still waiting on someone.

    publisher                     broker                         subscriber (x n)
    statement + sig + copies  ->  ANNOUNCE (fan out)          ->  verify sig, fetch copy,
                                                                  decrypt, recompute digest
                              <-  SIGNED (forwarded)         <-   vote {ACK|NAK} + sig
                                  tally: sealed / held / pending

Two modes:
  open       one plain copy everyone fetches. Anyone with the blob id can read it.
  encrypted  one copy per subscriber, sealed with that pair's key (X25519 + AES-GCM, the
             announcement id bound in as associated data). Needs a pairing with each one.

Received content is data. It is shown, never written into the project and never run.
"""

import json
import re
import secrets
import time

from . import schema
from .client import BrokerError
from .crypto import CryptoError, canonical, seal, sha256_hex, unseal, verify

PAYLOAD_KIND = "ack.announcement/v1"
MAX_ANNOUNCE_BYTES = 2_000_000
SIGN_POLICIES = ("auto", "manual")  # auto: ACK when signature and digest check out


def build_payload(title: str, files: dict[str, str]) -> bytes:
    return canonical({"kind": PAYLOAD_KIND, "title": title, "files": files})


def _tampered(payload: bytes) -> bytes:
    """What a meddling relay might do: quietly change the first number in the first file
    (range(3) becomes range(30)), or add a line if there is no number."""
    doc = json.loads(payload)
    path = sorted(doc["files"])[0]
    text = doc["files"][path]
    altered = re.sub(r"\d+", lambda m: m.group(0) + "0", text, count=1)
    doc["files"][path] = altered if altered != text else text + "\n# altered in transit\n"
    return canonical(doc)


class Announcements:
    """Mixed into Agent. Uses self.state, self.broker, self.identity, self._event, self._save."""

    def _ann_state(self) -> None:
        for key, default in (("published", {}), ("inbox", {}), ("keys", {}), ("sign_policy", "auto")):
            self.state.setdefault(key, default)

    # ---- keys: paired key if we have one, else the broker's copy pinned on first use ----

    def public_of(self, name: str) -> dict:
        self._ann_state()
        if name in self.state["peers"]:
            return self.state["peers"][name]
        if name not in self.state["keys"]:
            self.state["keys"][name] = self.broker.public_key(name)
            self._save()
        return self.state["keys"][name]

    # ---- subscriptions ----

    def subscribe(self, publisher: str, on: bool = True) -> None:
        self.broker.subscribe(publisher, on)
        self._event("subscribed" if on else "unsubscribed", publisher)

    def subscriptions(self) -> dict:
        return self.broker.subscriptions()

    def set_sign_policy(self, policy: str) -> None:
        if policy not in SIGN_POLICIES:
            raise ValueError(f"policy must be one of {SIGN_POLICIES}")
        self._ann_state()
        self.state["sign_policy"] = policy
        self._save()

    # ---- publisher ----

    def announce(self, title: str, files: dict[str, str], *, encrypted: bool = False,
                 subscribers: list[str] | None = None, quorum: int | None = None,
                 tamper: list[str] | tuple = ()) -> dict:
        """Announce a change to subscribers. `tamper` alters the named subscribers' copies
        after the digest is taken: a demonstration of what the check catches."""
        self._ann_state()
        title = title.strip()
        if not title or not files:
            raise ValueError("an announcement needs a title and at least one file")
        subs = subscribers if subscribers is not None else self.subscriptions()["subscribers"]
        subs = sorted(set(subs))
        if not subs:
            raise ValueError("nobody is subscribed to you yet")
        quorum = len(subs) if quorum is None else quorum
        if not 1 <= quorum <= len(subs):
            raise ValueError(f"quorum must be between 1 and {len(subs)}")
        if encrypted:
            unpaired = [s for s in subs if s not in self.state["peers"]]
            if unpaired:
                raise ValueError(f"encrypted announcements need a pairing with: {', '.join(unpaired)}")

        payload = build_payload(title, files)
        if len(payload) > MAX_ANNOUNCE_BYTES:
            raise ValueError("announcement too large")
        ann_id = secrets.token_hex(16)
        copies, uploads = {}, {}
        for sub in subs:
            body = _tampered(payload) if sub in tamper else payload
            if encrypted:
                body = seal(self._key(sub), body, ann_id.encode())
            blob_id = sha256_hex(body)
            copies[sub], uploads[blob_id] = blob_id, body
        missing = set(self.broker.missing(list(uploads)))
        for blob_id in missing:
            self.broker.put_blob(blob_id, uploads[blob_id])

        statement = {"id": ann_id, "publisher": self.name, "title": title, "digest": sha256_hex(payload),
                     "size": len(payload), "files": sorted(files), "subscribers": subs, "quorum": quorum,
                     "encrypted": encrypted, "ts": time.time()}
        sig = self.identity.sign(canonical(statement))
        result = self.broker.announce(statement, sig, copies)
        self.state["published"][ann_id] = {"statement": statement, "copies": copies, "tampered": sorted(tamper),
                                           "votes": {}, "status": result["status"]}
        self._save()
        mode = "encrypted" if encrypted else "open"
        self._event("announce", f"{title}: {mode}, to {', '.join(subs)}, quorum {quorum} of {len(subs)}, "
                                f"digest {statement['digest'][:12]}")
        return self.state["published"][ann_id]

    def _on_signed(self, msg: dict) -> None:
        self._ann_state()
        vote, sig = msg["body"]["vote"], msg["body"]["sig"]
        rec = self.state["published"].get(vote.get("announcement"))
        if rec is None or vote.get("signer") != msg["from"] or msg["from"] not in rec["statement"]["subscribers"]:
            raise schema.SchemaError("SIGNED for an announcement we didn't make")
        if not verify(self.public_of(msg["from"]), canonical(vote), sig):
            raise CryptoError(f"vote from {msg['from']} has a bad signature")
        rec["votes"][msg["from"]] = {"vote": vote, "sig": sig}
        rec["status"] = msg["body"]["status"]
        self._save()
        st = rec["status"]
        self._event(vote["verdict"].lower(), f"{msg['from']} on {rec['statement']['title']}: {vote['reason']} "
                                             f"({st['acks']} of {st['of']}, quorum {st['quorum']}: {st['state']})")

    def refresh_announcements(self) -> None:
        """Pull the broker's tallies (a vote may have landed while a SIGNED was in flight)."""
        self._ann_state()
        for rec in self.broker.announcements():
            mine = self.state["published"].get(rec["statement"]["id"])
            if mine is not None:
                for signer, v in rec["votes"].items():
                    if signer not in mine["votes"] and verify(self.public_of(signer), canonical(v["vote"]), v["sig"]):
                        mine["votes"][signer] = v
                mine["status"] = rec["status"]
            elif rec["statement"]["id"] in self.state["inbox"]:
                self.state["inbox"][rec["statement"]["id"]]["status"] = rec["status"]
        self._save()

    # ---- subscriber ----

    def _on_announce(self, msg: dict) -> None:
        self._ann_state()
        stmt, sig, blob = msg["body"]["statement"], msg["body"]["sig"], msg["body"]["blob"]
        if stmt.get("publisher") != msg["from"] or self.name not in stmt.get("subscribers", []):
            raise schema.SchemaError("ANNOUNCE not addressed to us")
        ann_id = stmt["id"]
        check = {"signature": False, "digest_seen": None, "match": False, "problem": ""}
        files: dict[str, str] = {}
        try:
            if not verify(self.public_of(msg["from"]), canonical(stmt), sig):
                raise CryptoError("publisher's signature does not verify")
            check["signature"] = True
            data = self.broker.get_blob(blob)
            if stmt["encrypted"]:
                data = unseal(self._key(msg["from"]), data, ann_id.encode())
            check["digest_seen"] = sha256_hex(data)
            check["match"] = check["digest_seen"] == stmt["digest"]
            if not check["match"]:
                check["problem"] = "copy does not match the announced digest"
            doc = json.loads(data)
            if isinstance(doc, dict) and isinstance(doc.get("files"), dict):
                files = {str(k): str(v) for k, v in doc["files"].items()}
        except (CryptoError, BrokerError, ValueError) as e:
            check["problem"] = str(e)
        except Exception as e:  # an undecryptable or malformed copy is a refusal, not a crash
            check["problem"] = f"{type(e).__name__}: {e}"
        self.state["inbox"][ann_id] = {"statement": stmt, "check": check, "files": files, "vote": None,
                                       "status": None, "received": time.time()}
        self._save()
        mode = "encrypted" if stmt["encrypted"] else "open"
        self._event("announce", f"{msg['from']} announced {stmt['title']} ({mode}); "
                                + ("digest matches" if check["match"] else check["problem"]))
        if not (check["signature"] and check["match"]):
            self.vote(ann_id, "NAK", check["problem"] or "check failed")
        elif self.state["sign_policy"] == "auto":
            self.vote(ann_id, "ACK", "digest matches the announcement")

    def vote(self, ann_id: str, verdict: str, reason: str = "") -> dict:
        self._ann_state()
        rec = self.state["inbox"].get(ann_id)
        if rec is None:
            raise KeyError(f"no announcement {ann_id}")
        if rec["vote"] is not None:
            raise ValueError("already signed")
        if verdict not in ("ACK", "NAK"):
            raise ValueError("verdict must be ACK or NAK")
        if verdict == "ACK" and not (rec["check"]["signature"] and rec["check"]["match"]):
            raise ValueError("can't acknowledge a copy that failed its check")
        vote = {"announcement": ann_id, "signer": self.name, "verdict": verdict,
                "digest_seen": rec["check"]["digest_seen"] or "", "reason": reason.strip() or verdict.lower(),
                "ts": time.time()}
        rec["status"] = self.broker.sign(ann_id, vote, self.identity.sign(canonical(vote)))
        rec["vote"] = vote
        self._save()
        self._event("signed", f"{verdict} on {rec['statement']['title']} from {rec['statement']['publisher']}")
        return rec["status"]
