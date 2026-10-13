# 14 · The Ack wire and announcements, line by line

**Job of this code:** two additions that make Ack its own protocol rather than a set of HTTP calls.

1. **The wire** (`ack/wire.py`): agents reach the broker at `ack://host:port`, in length-prefixed frames over one TCP connection.
2. **Announcements** (`ack/announce.py`, plus the announcement half of `ack/broker.py`): one publisher, many signatories, a quorum. The publisher signs a statement with a digest, the broker fans a copy out to every subscriber, each subscriber recomputes the digest on the copy it received and signs ACK or NAK, and the broker counts the votes.

The broker was also split in two so that both transports share one implementation: `BrokerCore` does the work, and `Handler` (HTTP) and `WireHandler` (Ack wire) only translate requests into calls to it.

Primer references: [classes and inheritance](00-python-primer.md#classes-and-objects), [with](00-python-primer.md#with-context-managers), [dicts](00-python-primer.md#dicts-the-newer-operators).

---

# Part 1: `wire.py`

## Lines 22–25 · the constants

```python
MAGIC = b"ACK1"
PREFIX = struct.Struct(">4sII")
MAX_HEADER = 64 << 10
DEFAULT_PORT = 7468
```

- **22** Every frame starts with these four bytes. A frame that doesn't is not Ack wire (an HTTP request, a port scanner), and the connection is closed. The `1` is the framing version.
- **23** `struct.Struct` describes a fixed binary layout. `>` means big-endian (network byte order), `4s` is four raw bytes, and each `I` is an unsigned 32-bit integer. So the prefix is 12 bytes: magic, header length, body length. Packing a layout once and reusing it is faster and clearer than building bytes by hand.
- **24** `64 << 10` is 64 × 1024 = 64 KB, the most a header may be. Headers are a few dozen bytes; the cap stops a peer from claiming a 4 GB header and making us allocate it.
- **25** The default port when a URL leaves it out.

## Lines 32–40 · read exactly n bytes

```python
def _read_exact(sock: socket.socket, n: int) -> bytes:
    chunks, got = [], 0
    while got < n:
        chunk = sock.recv(min(n - got, 1 << 20))
        if not chunk:
            raise WireError("connection closed")
        chunks.append(chunk)
        got += len(chunk)
    return b"".join(chunks)
```

- **35** `recv(k)` returns *up to* k bytes, whatever has arrived. TCP is a stream, not a sequence of messages, so a 10 KB body can arrive as one piece or forty. This loop keeps reading until it has exactly `n`. Each read asks for at most 1 MB at a time.
- **36–37** `recv` returning `b""` means the other side closed the connection. Mid-frame, that is an error.
- **40** Joining a list of chunks once is linear; `data += chunk` in a loop would copy the growing buffer each time.

## Lines 43–55 · read one frame

```python
def read_frame(sock: socket.socket, max_body: int) -> tuple[dict, bytes]:
    magic, hlen, blen = PREFIX.unpack(_read_exact(sock, PREFIX.size))
    if magic != MAGIC:
        raise WireError("not an Ack wire frame")
    if hlen > MAX_HEADER or blen > max_body:
        raise WireError("frame too large")
    ...
    return header, _read_exact(sock, blen)
```

- **44** Read the 12-byte prefix and unpack it into three values in one line (tuple unpacking).
- **45–48** The size checks run **before** reading the header or body, so an oversized frame is refused without buffering it. The caller sets `max_body`: the broker allows 64 MB (blobs), and the client allows replies up to 1 GB.
- **49–54** The header must be a JSON object. Any decoding problem becomes a `WireError`, which closes the connection.

## Lines 58–60 · write one frame

```python
def write_frame(sock: socket.socket, header: dict, body: bytes = b"") -> None:
    head = json.dumps(header, separators=(",", ":")).encode()
    sock.sendall(PREFIX.pack(MAGIC, len(head), len(body)) + head + body)
```

- **59** `separators=(",", ":")` drops the spaces `json.dumps` adds by default. The header is small, and compact is enough.
- **60** `sendall` (not `send`) keeps writing until every byte is out, the mirror image of `_read_exact`.

## Lines 63–69 · `ack://host:port`

`rpartition(":")` splits at the **last** colon, so an IPv6 address in brackets (`ack://[::1]:7468`) still works; `strip("[]")` removes the brackets.

---

# Part 2: `broker.py`, the shared core

## Lines 69–78 · `tally`, the quorum rule

```python
def tally(record: dict) -> dict:
    ...
    n, k = len(st["subscribers"]), st["quorum"]
    state = "sealed" if acks >= k else "held" if n - naks < k else "pending"
```

- **sealed** once at least `k` signatories have ACKed.
- **held** once so many have refused that `k` ACKs are impossible. `n - naks` is the most ACKs that could still arrive; below `k`, the result is settled. With a quorum of 3 out of 3, one NAK holds it immediately. With 2 of 3, it takes two.
- **pending** otherwise, with `waiting` listing whose reply is missing.

This one function is used by the broker when votes arrive, by `GET /announcements`, and in the tests (`test_tally_math`).

## Lines 134–138 · `Reply`

A `@dataclass` holding what any transport needs to answer: status code, payload, whether the payload is raw bytes (a blob) or JSON, and a content type.

## Lines 146–175 · `BrokerCore.handle`

```python
def handle(self, method, path, agent_name, auth, body) -> Reply:
    try:
        ...
        with self.st.lock:
            agent = self.st.authenticate(agent_name, auth)
        return self._route(method, path, agent, body)
    except HTTPError as e:
        return Reply(e.status, {"error": str(e)})
```

Every operation enters here, whichever transport carried it. Registration (`POST /agents`) and the plain-text note for browsers are the only things that don't need the agent's name and secret. `HTTPError` keeps its name for compatibility, but it is just "a refusal with a status code" now, and the wire uses the same codes.

## Lines 177–241 · `_route`

A flat list of `if route == (method, path)` checks. Two use regular expressions to pull an id out of the path: `/agents/<name>/key` and `/announcements/<id>(/sign)?`. `re.fullmatch` must match the **whole** path, so `/announcements/<id>/sign/extra` is a 404, not a sign.

## Lines 331–344 · subscribe

The broker stores `subscriptions` as `publisher -> [subscribers]`. You can only subscribe yourself (the authenticated agent), and only to an agent that exists and isn't you.

## Lines 346–382 · `_announce`, the fan-out

```python
if not verify(st.agents[agent]["public"], canonical(stmt), sig):
    raise HTTPError(403, "statement signature does not verify")
not_subscribed = [s for s in subs if s not in st.subscriptions.get(agent, [])]
...
for sub in subs:
    st.enqueue(sub, {... "type": "ANNOUNCE", "from": agent, "to": sub, ...
                     "body": {"statement": stmt, "sig": sig, "blob": copies[sub]}})
st.audit(agent, "ANNOUNCE", ...)
st.audit(None, "FANOUT", ...)
```

- The statement must be well formed: a 32-hex id, the publisher is the caller, a 64-hex digest, a non-empty subscriber list, `1 <= quorum <= len(subscribers)`, and exactly one copy per subscriber.
- The broker checks the publisher's Ed25519 signature with the key registered at sign-up. So no other agent can announce in someone else's name.
- Every named signatory must actually subscribe to the publisher. You can't conscript strangers into your quorum.
- Every copy must already be uploaded (`/blobs/missing` then `PUT`, as for bundles), so a subscriber never receives an announcement it can't fetch.
- The fan-out is one queued ANNOUNCE per subscriber, each naming **that subscriber's** copy. Open announcements give everyone the same blob id; sealed ones give each a different one.
- `agent=None` in the audit marks entries written by the broker itself. The page's carbon copy shows them as "broker".

## Lines 384–412 · `_sign`, collecting a vote

```python
if agent in rec["votes"]:
    raise HTTPError(409, "already signed")
if (... vote.get("announcement") != ann_id or vote.get("signer") != agent
        or vote.get("verdict") not in VERDICTS ...):
    raise HTTPError(400, "malformed vote")
if not verify(st.agents[agent]["public"], canonical(vote), sig):
    raise HTTPError(403, "vote signature does not verify")
```

- One vote per signatory, and it can't be changed afterwards.
- The vote names the announcement and the signer **inside the signed text**, so a signature can't be replayed onto another announcement or passed off as someone else's.
- After recording it, the broker queues a SIGNED message to the publisher with the vote, the signature and the new tally. If the tally just moved to sealed or held, it writes a SEALED or HELD audit entry for everyone involved.

## Lines 422–455 · the HTTP transport

`Handler._dispatch` reads the body, passes method, path, the `X-Ack-Agent` and `Authorization` headers and the body to `core.handle`, and writes the `Reply` back as an HTTP response. That is all HTTP does now.

## Lines 465–481 · the wire transport

```python
def handle(self) -> None:
    sock = self.request
    while True:
        try:
            header, body = wire.read_frame(sock, MAX_BODY)
        except (wire.WireError, OSError):
            return
        reply = self.core.handle(str(header.get("op", "")), str(header.get("path", "")), header.get("agent"),
                                 header.get("auth"), body)
        ...
        wire.write_frame(sock, {"status": reply.status, "raw": reply.raw, "ctype": reply.ctype}, data)
```

- `socketserver.BaseRequestHandler.handle` runs once per TCP connection, in its own thread (`ThreadingMixIn`). The `while True` serves frame after frame on the same connection, so an agent that polls every two seconds keeps one connection open instead of opening a new one each time.
- Anything that isn't a valid frame ends the connection (`return`), with no reply: there is nothing sensible to reply to.
- `str(...)` around `op` and `path` means a header with a number or a list there just becomes a 404, not a crash.

## Lines 495–504 · `start_in_thread`

Picks the transport and returns a URL whose **scheme** says which one: `ack://` or `http://`. The client decides how to talk from that scheme alone.

---

# Part 3: `client.py`, speaking the wire

```python
with self._sock_lock:
    for attempt in (0, 1):
        try:
            if self._sock is None:
                self._sock = socket.create_connection(wire.parse_url(self.url), timeout=30)
            wire.write_frame(self._sock, header, data)
            reply, payload = wire.read_frame(self._sock, 1 << 30)
            break
        except (OSError, wire.WireError) as e:
            ...
            if attempt:
                raise BrokerError(503, ...)
```

- One connection per client, reused. The lock means two threads never interleave frames on it (the web app polls in the background while you click).
- `for attempt in (0, 1)`: if the broker restarted, the old connection is dead. The first failure drops it and the loop reconnects once and resends. A second failure is reported as "broker unreachable", like the HTTP path.
- Error replies (status 400 and up) raise the same `BrokerError` the HTTP path raises, so the rest of the code doesn't know or care which transport it used.

---

# Part 4: `announce.py`

`Announcements` is a **mixin**: a class that only adds methods, combined into `Agent` with `class Agent(Announcements)`. It uses the agent's `state`, `broker`, `identity`, `_key` and `_event`. Keeping it in its own file keeps `agent.py` about the paired flow.

## Lines 38–50 · the payload, and the tamper test

```python
def build_payload(title: str, files: dict[str, str]) -> bytes:
    return canonical({"kind": PAYLOAD_KIND, "title": title, "files": files})
```

The announced bytes are canonical JSON (sorted keys, no spaces) of the title and the files' text. Canonical means the publisher and every subscriber compute the digest over exactly the same bytes.

`_tampered` is for demonstrations only: it appends a `0` to the first number in the first file (`range(3)` becomes `range(30)`). The publisher applies it to the named subscribers' copies **after** taking the digest, which is what a meddling relay would do. It is how the landing page's "Alter alpha's copy" works, and how the tests check that a mismatch is caught.

## Lines 62–71 · whose key?

```python
def public_of(self, name: str) -> dict:
    if name in self.state["peers"]:
        return self.state["peers"][name]
    if name not in self.state["keys"]:
        self.state["keys"][name] = self.broker.public_key(name)
        self._save()
    return self.state["keys"][name]
```

A paired agent's key came from the QR invite, so it doesn't depend on the broker. An unpaired publisher's key is fetched from the broker once and pinned. That is weaker (see Known limits in the README), which is why sealed announcements require a pairing.

## Lines 89–136 · `announce`

- **98–104** The signatories default to everyone subscribed to you. The quorum defaults to all of them.
- **105–108** Sealed copies need a pair key per signatory, so every one must be paired.
- **113** A random 128-bit id, as 32 hex characters.
- **115–120** One body per signatory. For a sealed announcement each body is `seal(pair_key, payload, aad=ann_id)`: AES-GCM with a fresh nonce, with the announcement id bound in as associated data, so a sealed copy can't be moved to a different announcement. Identical open copies share one blob id, so the dict `uploads` holds it once.
- **121–123** Upload only what the broker doesn't already have.
- **125–128** The statement is what gets signed: id, publisher, title, digest of the **original** payload, size, file names, signatories, quorum, mode, time.
- **129–131** Send it, and remember what we published (including which copies we altered, so the page can say so).

## Lines 169–204 · `_on_announce`, the subscriber's check

```python
if not verify(self.public_of(msg["from"]), canonical(stmt), sig):
    raise CryptoError("publisher's signature does not verify")
data = self.broker.get_blob(blob)
if stmt["encrypted"]:
    data = unseal(self._key(msg["from"]), data, ann_id.encode())
check["digest_seen"] = sha256_hex(data)
check["match"] = check["digest_seen"] == stmt["digest"]
```

- **172–173** The statement must name this publisher and include us.
- **178–180** Check the publisher's signature over the statement first. If the broker or anyone else changed the digest, the quorum or the signatory list, this fails.
- **181** `get_blob` already checks the blob against its own hash (its id). That catches storage corruption, not tampering: a tampered copy simply has a different id.
- **182–183** Sealed copies are decrypted with our pair key. A copy sealed for someone else, or moved from another announcement, fails here.
- **184–185** The real check: the digest of what we received against the digest the publisher signed.
- **188–190** Keep the files' text to show in the page. It is never written into the project and never run.
- **191–194** Any failure becomes a recorded problem, not a crash.
- **201–204** A failed check is always a NAK, whatever the policy. A passing check is an ACK straight away under the `auto` policy, or waits for a person under `manual`.

## Lines 206–224 · `vote`

```python
if verdict == "ACK" and not (rec["check"]["signature"] and rec["check"]["match"]):
    raise ValueError("can't acknowledge a copy that failed its check")
vote = {"announcement": ann_id, "signer": self.name, "verdict": verdict,
        "digest_seen": rec["check"]["digest_seen"] or "", ...}
rec["status"] = self.broker.sign(ann_id, vote, self.identity.sign(canonical(vote)))
```

You can refuse anything, but you can't ACK a copy that failed its check, not even by hand. The vote carries the digest you actually computed, so the publisher and the carbon copy show exactly what each signatory saw.

## Lines 138–151 · `_on_signed`, the publisher's side

The publisher re-verifies each forwarded vote with the signer's key rather than trusting the broker's word for it, then stores the vote and the tally that came with it.

---

# Part 5: the frontend (`site/`)

The web app (`ack/web.py`) serves `site/dist/index.html` with two `<meta>` tags added: `ack-token` (the per-run token every API call must send) and `ack-agent` (this agent's name). `site/src/lib/api.jsx` reads them; if they are missing (opened from `npm run dev` or a static host), `LIVE` is false and the landing page's handoff runs as a labelled simulation.

- `StatusProvider` polls `/api/status` for the whole page, pausing while the tab is hidden. Only two failed polls in a row show the agent as offline.
- `Handoff.jsx` has two hooks with the same output shape: `useSimulated` steps through a scripted story, and `useLive` calls `/api/handoff` (which runs `announce` with the fixed `billing/charge.py` payload) and then polls `/api/handoff/status` until nobody is waiting. `lib/handoff.js` turns either into the same `view` (sheet, signatories, stamps, carbon copy), so the design is identical in both modes.
- The carbon copy in live mode is the broker's own audit log, filtered to that announcement: ANNOUNCE, FANOUT, each ACK/NAK with the digest the signer computed, and SEALED or HELD.
- The console (`site/src/console/`) is a hash route (`#/announce`, `#/inbox`, ...), so plain anchors like `#submit` still scroll the landing page. All text from peers is rendered by React as text, never as HTML. The only generated markup shown is the pairing QR code, which the agent itself drew, loaded as an `<img>` from a `data:` URL.
