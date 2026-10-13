# 02 · `ack/schema.py`, line by line

**Job of this file:** define what a valid message looks like, build new messages, and reject anything malformed, from us or from a peer, before any handler sees it.

Primer references: [dicts and set operators](00-python-primer.md#dicts-the-newer-operators), [comprehensions](00-python-primer.md#comprehensions-building-a-collection-in-one-line), [f-strings](00-python-primer.md#strings).

---

## Lines 9–10 · imports

```python
import time
import uuid
```

- **9** `time.time()` gives the current time as seconds since 1970, used for the message timestamp.
- **10** `uuid.uuid4()` makes a random unique ID for each message.

## Lines 12–17 · constants

```python
VERSION = 1

STATUSES = ("READY", "STALE", "DENIED", "NOT_FOUND")
DECISIONS = ("ADOPT", "ADAPT", "REJECT")

NUMBER = (int, float)
```

- **12** The protocol version. A message with any other `v` is refused, so a future v2 can't be misread as v1.
- **14** The only four answers allowed in a CHECK.
- **15** The only three outcomes allowed in an ACK.
- **17** A tuple of types. `isinstance(x, (int, float))` accepts either, so `NUMBER` means "any number".

## Lines 19–27 · the header every message has

```python
HEADER_FIELDS = {
    "v": int,
    "id": str,
    "type": str,
    "from": str,
    "to": str,
    "ts": NUMBER,
    "reply_to": (str, type(None)),
}
```

A dict from field name to the type(s) it must have:

- **20** `v`: protocol version.
- **21** `id`: this message's unique ID.
- **22** `type`: which of the 11 message kinds this is.
- **23–24** `from` / `to`: sender and recipient names. The broker routes on these.
- **25** `ts`: timestamp.
- **26** `reply_to`: the `id` of the message this answers, or `None`. `type(None)` is the type of `None`, so this field may be a string *or* None. It's how an OFFER is tied to the REQUEST that caused it.

## Lines 29–42 · the body each message type must have

```python
BODY_FIELDS = {
    "PAIR": {"invite": str, "public": dict, "proof": str},
    "QUERY": {"text": str},
    ...
    "UPDATE_AVAILABLE": {"base_manifest_hash": str, "manifest": dict, "changed": list, "diff": str},
}
```

A dict of dicts: message type → (field → type). Each line is the complete contract for one message:

- **30** `PAIR`: which invite (token hash), the joiner's public keys, and the HMAC proof.
- **31** `QUERY`: just plain text. Text is the common language between different LLMs.
- **32** `ANSWER`: the summary, which parts it came from, how confident, which version.
- **33** `REQUEST`: which part, why it's needed, and what the requester will do with it.
- **34** `CHECK`: the status and a human-readable detail.
- **35** `OFFER`: the signed manifest.
- **36–37** `ACCEPT` / `DECLINE`: which manifest, and for a decline, why.
- **38** `BUNDLE`: which manifest, and the blob IDs to download.
- **39** `ACK`: the final decision and its reason.
- **40–41** `UPDATE_AVAILABLE`: what the receiver has now, the new manifest, what changed, and a diff.

## Lines 45–46 · error type

```python
class SchemaError(ValueError):
    pass
```

- **45** Based on `ValueError`, Python's general "this value is wrong" error. Raised for any malformed message.

## Lines 49–61 · build a message

```python
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
```

- **49** `type_` has a trailing underscore because `type` is a built-in Python name we don't want to shadow.
- **52** `.hex` is the UUID as 32 hex characters.
- **50–59** Fill in every header field and attach the body.
- **60** **Validate our own outgoing messages too.** A bug in our code fails loudly here instead of sending garbage to a peer.
- **61** Return the finished message dict.

## Lines 64–65 · just the header

```python
def header(msg: dict) -> dict:
    return {k: msg[k] for k in HEADER_FIELDS}
```

- **65** A dict comprehension: for each header field name, copy that field from the message. Looping over a dict gives its keys. The agent encrypts the body and binds this header in as associated data.

## Lines 68–80 · the strict field checker

```python
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
```

- **68** `where` is just for error messages ("header", "OFFER", …).
- **69–70** The thing must be a dict at all.
- **71** Set difference: fields the spec requires that the object lacks.
- **72** The reverse: fields the object has that the spec doesn't know.
- **73–74** Any missing field → reject. `sorted()` gives a stable, readable list in the message.
- **75–76** Any **unknown** field → reject. This strictness stops a peer smuggling extra data into a handler that might accidentally use it.
- **77** Go through each required field and its expected type.
- **79** Two checks:
  - `isinstance(value, bool)`: in Python, `True` counts as an `int`, so without this, `"version": true` would pass as a number. We reject booleans outright. No field in the protocol is a boolean.
  - `not isinstance(value, typ)`: wrong type → reject.

## Lines 83–99 · validate a whole message

```python
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
```

- **84–85** Must be a dict.
- **86** Check the header: everything except `body`, built with a filtered dict comprehension.
- **87–88** Wrong protocol version → reject.
- **89** Look up the body spec for this type. `.get` returns `None` if the type is unknown.
- **90–91** Unknown type → reject. `!r` shows the value in quotes, so an empty or odd type is visible in the error.
- **92–93** Check the body against its spec.
- **94–99** Value checks the type system can't express:
  - a CHECK's status must be one of the four
  - an ACK's decision must be one of the three
  - confidence must be between 0 and 1 (a chained comparison)
