"""Message schema. Every message is an envelope plus a typed body.

On the wire the header travels in the clear (the broker needs from/to to route,
and type for the audit log) and the body is sealed with the pair key, with the
header bound in as associated data so it can't be swapped. PAIR is the only
unsealed message from a peer, because the pair key doesn't exist yet. ANNOUNCE and
SIGNED come from the broker's fan-out and carry their own signatures instead.
"""

import time
import uuid

VERSION = 1

STATUSES = ("READY", "STALE", "DENIED", "NOT_FOUND")
DECISIONS = ("ADOPT", "ADAPT", "REJECT")

NUMBER = (int, float)

HEADER_FIELDS = {
    "v": int,
    "id": str,
    "type": str,
    "from": str,
    "to": str,
    "ts": NUMBER,
    "reply_to": (str, type(None)),
}

BODY_FIELDS = {
    "PAIR": {"invite": str, "public": dict, "proof": str},
    "QUERY": {"text": str},
    "ANSWER": {"summary": str, "sources": list, "confidence": NUMBER, "version": int},
    "REQUEST": {"source_id": str, "reason": str, "intent": str},
    "CHECK": {"source_id": str, "status": str, "detail": str},
    "OFFER": {"manifest": dict},
    "ACCEPT": {"manifest_hash": str},
    "DECLINE": {"manifest_hash": str, "reason": str},
    "BUNDLE": {"manifest_hash": str, "blobs": list},
    "ACK": {"manifest_hash": str, "decision": str, "reason": str},
    # A short notice: the new manifest plus a diff. The receiver pulls it with ACCEPT, like an OFFER.
    "UPDATE_AVAILABLE": {"base_manifest_hash": str, "manifest": dict, "changed": list, "diff": str},
    # Fanned out by the broker, not sealed: the publisher's and signer's signatures carry the trust.
    "ANNOUNCE": {"statement": dict, "sig": str, "blob": str},
    "SIGNED": {"vote": dict, "sig": str, "status": dict},
}

# Only the broker creates these (it refuses them on POST /messages), so they arrive unsealed.
FROM_BROKER = ("ANNOUNCE", "SIGNED")


class SchemaError(ValueError):
    pass


def make(type_: str, sender: str, to: str, body: dict, reply_to: str | None = None) -> dict:
    msg = {
        "v": VERSION,
        "id": uuid.uuid4().hex,
        "type": type_,
        "from": sender,
        "to": to,
        "ts": time.time(),
        "reply_to": reply_to,
        "body": body,
    }
    validate(msg)
    return msg


def header(msg: dict) -> dict:
    return {k: msg[k] for k in HEADER_FIELDS}


def _check_fields(obj: dict, spec: dict, where: str) -> None:
    if not isinstance(obj, dict):
        raise SchemaError(f"{where} must be an object")
    missing = spec.keys() - obj.keys()
    extra = obj.keys() - spec.keys()
    if missing:
        raise SchemaError(f"{where} missing {sorted(missing)}")
    if extra:
        raise SchemaError(f"{where} has unknown fields {sorted(extra)}")
    for name, typ in spec.items():
        value = obj[name]
        if isinstance(value, bool) or not isinstance(value, typ):
            raise SchemaError(f"{where}.{name} has wrong type")


def validate(msg: dict) -> None:
    if not isinstance(msg, dict):
        raise SchemaError("message must be an object")
    _check_fields({k: v for k, v in msg.items() if k != "body"}, HEADER_FIELDS, "header")
    if msg["v"] != VERSION:
        raise SchemaError(f"unsupported version {msg['v']}")
    spec = BODY_FIELDS.get(msg["type"])
    if spec is None:
        raise SchemaError(f"unknown message type {msg['type']!r}")
    body = msg.get("body")
    _check_fields(body, spec, msg["type"])
    if msg["type"] == "CHECK" and body["status"] not in STATUSES:
        raise SchemaError(f"bad status {body['status']!r}")
    if msg["type"] == "ACK" and body["decision"] not in DECISIONS:
        raise SchemaError(f"bad decision {body['decision']!r}")
    if msg["type"] == "ANSWER" and not 0 <= body["confidence"] <= 1:
        raise SchemaError("confidence must be in [0, 1]")
