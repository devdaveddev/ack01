# 09 · `ack/broker.py` and `ack/client.py`, line by line

> **Updated since this page was written.** The broker's routing and handlers moved into a transport-independent `BrokerCore`, served over both the Ack wire and HTTP, and it gained subscriptions and announcements. The logic of each endpoint below is unchanged, but the line numbers here refer to the earlier, HTTP-only file. [Page 14](14-wire-and-announcements.md) covers the new structure, the wire, and the announcement endpoints.

**Job of these files:** the broker is a small web server that stores encrypted blobs by hash, holds messages for agents that are offline, runs one-time invites, keeps the registry of who holds which version, and writes an audit log. The client is how an agent talks to it. The broker never decrypts anything; it can't.

**A quick HTTP refresher:** a client sends a **request** with a *method* (`GET` to read, `POST` to send data, `PUT` to store something at a path), a *path* (`/blobs/abc…`), *headers* (key: value metadata such as `Content-Type`), and maybe a *body*. The server replies with a **status code**:

| Code | Meaning |
|---|---|
| 200 | OK |
| 201 | created |
| 202 | accepted / queued |
| 400 | bad request |
| 401 | not authenticated |
| 403 | not allowed |
| 404 | not found |
| 409 | conflict |
| 410 | gone |
| 413 | too large |

Primer references: [classes and inheritance](00-python-primer.md#classes-and-objects), [with](00-python-primer.md#with-context-managers), [dicts](00-python-primer.md#dicts-the-newer-operators).

---

# Part 1: `broker.py`

## Lines 1–19 · docstring

Lists every endpoint. It's the broker's API reference.

## Lines 21–29 · imports

```python
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
```

- **22** `hmac.compare_digest`: comparison that takes the same time whatever the input (explained at line 74).
- **25** `secrets`: cryptographically secure random tokens, for agent passwords.
- **26** `threading`: a lock, since requests are handled in parallel threads, and running the server in the background.
- **28** The standard library's HTTP server. `BaseHTTPRequestHandler` is the class we extend to handle each request. `ThreadingHTTPServer` handles each request in its own thread.

## Lines 31–33 · limits and patterns

```python
MAX_BODY = 64 << 20
HEX64 = re.compile(r"^[0-9a-f]{64}$")
NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
```

- **31** Refuse request bodies over 64 MB.
- **32** A valid SHA-256 hex string (the blob IDs and token hashes).
- **33** A valid agent name: letters, digits, `_`, `.` and `-`, 1 to 64 characters. This keeps names safe to use in keys and logs.

## Lines 36–39 · an error that carries an HTTP status

```python
class HTTPError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
```

- **38** Pass the message up to `Exception`, so `str(e)` gives it.
- **39** Also keep the status code. Any handler can `raise HTTPError(404, "...")`, and the dispatcher turns it into a proper response (line 153).

## Lines 42–43 · hash helper

```python
def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
```

A local SHA-256 helper. It's defined here so the broker doesn't depend on the agents' crypto module.

## Lines 46–58 · BrokerState: all the broker remembers

```python
class BrokerState:
    def __init__(self, data_dir: Path):
        self.dir = Path(data_dir)
        self.blob_dir = self.dir / "blobs"
        self.blob_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.dir / "state.json"
        self.audit_path = self.dir / "audit.jsonl"
        self.lock = threading.Lock()
        state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
        self.agents: dict = state.get("agents", {})
        self.invites: dict = state.get("invites", {})
        self.queues: dict = state.get("queues", {})
        self.holdings: dict = state.get("holdings", {})
```

- **48–50** The data folder, and a `blobs/` folder inside it, created if needed.
- **51** Everything except blobs is saved in one JSON file.
- **52** The audit log, in **JSON Lines** format: one JSON object per line. Appending a line never rewrites earlier ones.
- **53** A lock. Many request threads run at once, and only one may change state at a time.
- **54** Load saved state if there is any, so a broker restart keeps registered agents, queued messages and so on.
- **55–58** The four tables:
  - `agents`: name → `{public keys, hash of its secret}`
  - `invites`: token hash → `{inviter, used_by, created}`
  - `queues`: agent → list of messages waiting for it
  - `holdings`: `"holder|owner|source"` → the latest record

## Lines 60–64 · save atomically

```python
    def save(self) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"agents": self.agents, "invites": self.invites,
                                   "queues": self.queues, "holdings": self.holdings}))
        tmp.replace(self.state_path)
```

- **61** `state.tmp`, next to `state.json`.
- **62–63** Write everything to the temporary file first.
- **64** Rename it over the real file. A rename is **atomic**: anyone reading sees the old file or the new one, never half of each, even if the broker crashes mid-write.

## Lines 66–69 · write an audit entry

```python
    def audit(self, agent: str | None, action: str, **detail) -> None:
        entry = {"ts": time.time(), "agent": agent, "action": action, **detail}
        with self.audit_path.open("a") as f:
            f.write(json.dumps(entry) + "\n")
```

- **66** `**detail` collects any extra named arguments (`blob=..., size=...`) into a dict.
- **67** Build the entry: time, who, what, then the details spread in.
- **68–69** Open in **append** mode (`"a"`) and add one line.

## Lines 71–76 · check who's calling

```python
    def authenticate(self, agent: str | None, auth: str | None) -> str:
        record = self.agents.get(agent or "")
        secret = (auth or "").removeprefix("Bearer ")
        if not record or not hmac.compare_digest(record["secret_hash"], _sha(secret.encode())):
            raise HTTPError(401, "unauthorized")
        return agent
```

- **71** `agent` comes from the `X-Ack-Agent` header and `auth` from `Authorization`. Either may be missing (`None`).
- **72** Find the registered agent. `agent or ""` avoids looking up `None`.
- **73** The header looks like `Bearer <secret>`. Strip the prefix to get the secret.
- **74** Reject if the agent is unknown, or if the hash of the presented secret doesn't match the stored hash. Two details:
  - the broker stores only **hashes** of secrets, so a stolen `state.json` doesn't give away passwords
  - `compare_digest` takes the same time however many characters match, so an attacker can't guess the secret one character at a time by timing responses
- **76** Return the now-verified agent name.

## Lines 78–84 · what each agent may see in the registry

```python
    def visible_holdings(self, agent: str) -> list[dict]:
        """Records you made, records about parts you own, and owners' version records
        for parts you hold. Not who else holds what."""
        recs = list(self.holdings.values())
        mine = {(r["owner"], r["source_id"]) for r in recs if r["holder"] == agent}
        return [r for r in recs if r["holder"] == agent or r["owner"] == agent
                or (r["holder"] == r["owner"] and (r["owner"], r["source_id"]) in mine)]
```

The registry used to be handed to every agent in full, showing who holds what across the whole network. Now each agent sees only what it needs (bug G in the bug-fix pass):

- **81** Every record.
- **82** A **set comprehension** of the `(owner, part)` pairs this agent holds.
- **83–84** Keep a record if any of these is true:
  - **you made it** (`holder == agent`)
  - **it's about a part you own** (`owner == agent`), so owners can see who holds their work
  - **it's the owner's own version record for a part you hold**. That's exactly what the audit needs to tell you you're behind, and nothing more.

## Lines 86–89 · where a blob lives

```python
    def blob_path(self, blob_id: str) -> Path:
        if not HEX64.match(blob_id):
            raise HTTPError(400, "bad blob id")
        return self.blob_dir / blob_id
```

- **87–88** Accept only 64 hex characters. This blocks names like `../state.json` from reaching the filesystem.
- **89** The blob's file is named by its hash.

## Lines 92–106 · the request handler

```python
class Handler(BaseHTTPRequestHandler):
    server_version = "ack-broker/0.1"
    state: BrokerState  # set by make_server

    def log_message(self, *args) -> None:
        pass

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")
```

- **92** The HTTP server creates one `Handler` per request and calls `do_GET`, `do_POST` or `do_PUT` depending on the method.
- **93** Sent in the `Server` response header.
- **94** A type hint only. The real `state` is attached by `make_server` (line 261).
- **96–97** Override the default logging, which prints every request to the terminal. We have our own audit log.
- **99–106** All three methods go through one dispatcher.

## Lines 108–112 · read the request body

```python
    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise HTTPError(413, "body too large")
        return self.rfile.read(length)
```

- **109** How many bytes the client says it's sending, or 0.
- **110–111** Refuse oversized bodies **before** reading them, so nobody can fill the broker's memory.
- **112** Read exactly that many bytes from the connection.

## Lines 114–121 · read a JSON body

```python
    def _json(self) -> dict:
        try:
            data = json.loads(self._body() or b"{}")
        except json.JSONDecodeError:
            raise HTTPError(400, "invalid JSON")
        if not isinstance(data, dict):
            raise HTTPError(400, "expected an object")
        return data
```

- **116** Parse the body. An empty body counts as `{}`.
- **117–118** Not valid JSON → 400.
- **119–120** Valid JSON, but not an object (e.g. a list) → 400.

## Lines 123–129 · send a response

```python
    def _send(self, status: int, payload, raw: bool = False) -> None:
        data = payload if raw else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/octet-stream" if raw else "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
```

- **124** Raw bytes for blob downloads; everything else becomes JSON bytes.
- **125** The status line.
- **126** Say what kind of content it is: `octet-stream` means raw bytes. The client relies on this (client.py line 41).
- **127** How long it is.
- **128** End of headers.
- **129** Write the body.

## Lines 131–164 · route each request

```python
    def _dispatch(self, method: str) -> None:
        try:
            path = self.path.split("?", 1)[0].rstrip("/")
            st = self.state
            if method == "POST" and path == "/agents":
                return self._register()
            with st.lock:
                agent = st.authenticate(self.headers.get("X-Ack-Agent"), self.headers.get("Authorization"))
            route = (method, path)
            if route == ("POST", "/invites"):
                return self._create_invite(agent)
            ...
            if route == ("GET", "/holdings"):
                with st.lock:
                    return self._send(200, st.visible_holdings(agent))
            if route == ("GET", "/audit"):
                lines = st.audit_path.read_text().splitlines() if st.audit_path.exists() else []
                entries = [json.loads(line) for line in lines]
                # Only entries about you: the full log would show everyone who talks to whom.
                return self._send(200, [e for e in entries if agent in (e.get("agent"), e.get("to"))])
            raise HTTPError(404, "not found")
        except HTTPError as e:
            self._send(e.status, {"error": str(e)})
```

- **133** Drop any `?query` part and any trailing `/`, so `/messages/` and `/messages` are the same route.
- **135–136** Registration is the **only** route that doesn't need authentication, since you can't have a secret before you register.
- **137–138** Everything else: authenticate first, while holding the lock. Failure raises 401, caught at line 163.
- **139** Combine method and path into a tuple for easy matching.
- **140–153** Match each route and call its handler. `/blobs/<id>` with GET or PUT uses `removeprefix` to pull out the ID.
- **154–156** The registry: **only the records that concern the caller** (see `visible_holdings` above).
- **157–161** The audit log: read every line back into an object, then keep only entries **this agent made, or that were addressed to it** (`agent in (who acted, who it was sent to)`). Before the bug-fix pass, any agent could read the whole log and see who talks to whom (bug G).
- **162** Nothing matched → 404.
- **163–164** **Every** `HTTPError` raised anywhere in a handler ends up here and becomes a JSON error response with the right status. Handlers stay short: they just raise.

## Lines 166–180 · register an agent

```python
    def _register(self) -> None:
        data = self._json()
        agent, public = data.get("agent"), data.get("public")
        if not isinstance(agent, str) or not NAME.match(agent) or not isinstance(public, dict):
            raise HTTPError(400, "need agent name and public keys")
        st = self.state
        with st.lock:
            if agent in st.agents:
                raise HTTPError(409, "agent name taken")
            secret = secrets.token_urlsafe(32)
            st.agents[agent] = {"public": public, "secret_hash": _sha(secret.encode())}
            st.queues.setdefault(agent, [])
            st.save()
            st.audit(agent, "register")
        self._send(201, {"secret": secret})
```

- **168** Read both fields at once.
- **169–170** Check the name format, and that public keys were given.
- **173–174** Names are first-come, first-served. Nobody can take over an existing name.
- **175** A random 32-byte secret, as URL-safe text.
- **176** Store only its **hash**.
- **177** Create an empty message queue for the new agent.
- **178–179** Save and log.
- **180** Return the secret **once**. The client must save it; the broker can't show it again.

## Lines 182–193 · create an invite

```python
    def _create_invite(self, agent: str) -> None:
        th = self._json().get("token_hash")
        if not isinstance(th, str) or not HEX64.match(th):
            raise HTTPError(400, "bad token hash")
        st = self.state
        with st.lock:
            if th in st.invites:
                raise HTTPError(409, "invite exists")
            st.invites[th] = {"inviter": agent, "used_by": None, "created": time.time()}
            st.save()
            st.audit(agent, "invite.create")
        self._send(201, {"ok": True})
```

- **183–185** The broker receives only the token's **hash**, and checks it looks like one.
- **188–189** Each invite can be created only once.
- **190** Record who issued it, and that nobody has used it yet.

## Lines 195–207 · redeem an invite (once only)

```python
    def _redeem_invite(self, agent: str) -> None:
        th = self._json().get("token_hash")
        st = self.state
        with st.lock:
            invite = st.invites.get(th) if isinstance(th, str) else None
            if invite is None:
                raise HTTPError(404, "no such invite")
            if invite["used_by"] is not None:
                raise HTTPError(410, "invite already used")
            invite["used_by"] = agent
            st.save()
            st.audit(agent, "invite.redeem", inviter=invite["inviter"])
        self._send(200, {"inviter": invite["inviter"]})
```

- **199** Look it up, but only if the hash is a string.
- **200–201** Unknown → 404.
- **202–203** Already used → 410 (gone). This is what makes invites one-time.
- **204** Mark it used by this agent. It happens inside the lock, so two agents redeeming at the same moment can't both succeed.
- **207** Tell the joiner who issued it, so it can check that matches the name in the QR code.

## Lines 209–221 · accept a message for delivery

```python
    def _post_message(self, agent: str) -> None:
        wire = self._json()
        if wire.get("from") != agent:
            raise HTTPError(403, "from must match the authenticated agent")
        st = self.state
        with st.lock:
            if wire.get("to") not in st.agents:
                raise HTTPError(404, "unknown recipient")
            st.queues.setdefault(wire["to"], []).append(wire)
            st.save()
            st.audit(agent, "message", id=wire.get("id"), type=wire.get("type"),
                     to=wire["to"], reply_to=wire.get("reply_to"))
        self._send(202, {"queued": True})
```

- **210** `wire` is the message as sent: header in the clear, body sealed.
- **211–212** **An agent can only send as itself.** The authenticated name must equal the message's `from`.
- **215–216** The recipient must be registered.
- **217** Add the message to the recipient's queue. It waits there until they poll, which is how offline delivery works.
- **219–220** Audit the **header only**. The broker can't read the sealed body.
- **221** 202: queued, not yet delivered.

## Lines 223–229 · deliver queued messages

```python
    def _fetch_messages(self, agent: str) -> None:
        st = self.state
        with st.lock:
            msgs, st.queues[agent] = st.queues.get(agent, []), []
            if msgs:
                st.save()
        self._send(200, msgs)
```

- **226** In one statement: take the agent's queue, and replace it with an empty list. Inside the lock, no message can arrive between the two halves and get lost.
- **227–228** Save only if something changed.
- **229** Send them all.

## Lines 231–235 · which blobs are missing?

```python
    def _missing(self, agent: str) -> None:
        ids = self._json().get("ids")
        if not isinstance(ids, list):
            raise HTTPError(400, "ids must be a list")
        self._send(200, {"missing": [i for i in ids if not self.state.blob_path(i).exists()]})
```

- **235** Of the IDs asked about, return those the broker doesn't have. The owner then uploads only those, which is how only changed layers travel.

## Lines 237–254 · get or store a blob

```python
    def _blob(self, agent: str, method: str, blob_id: str) -> None:
        st = self.state
        path = st.blob_path(blob_id)
        if method == "GET":
            if not path.exists():
                raise HTTPError(404, "no such blob")
            data = path.read_bytes()
            st.audit(agent, "blob.get", blob=blob_id, size=len(data))
            return self._send(200, data, raw=True)
        data = self._body()
        if _sha(data) != blob_id:
            raise HTTPError(400, "blob does not match its hash")
        if not path.exists():
            tmp = path.with_suffix(f".{secrets.token_hex(4)}.tmp")
            tmp.write_bytes(data)
            tmp.replace(path)
        st.audit(agent, "blob.put", blob=blob_id, size=len(data))
        self._send(201, {"ok": True})
```

- **239** Validates the ID and builds the path.
- **240–245** **GET**: send the raw bytes, logging who fetched what.
- **246** **PUT**: read the uploaded bytes.
- **247–248** **The upload must hash to its own name.** Nobody can store data under a false name, and the broker checks this without being able to read the content.
- **249–252** Store it only if it's new. Writes go to a uniquely named temporary file, then an atomic rename, so two simultaneous uploads of the same blob can't corrupt it. (Same name means same content, so if it already exists there's nothing to do.)

## Lines 256–267 · record who holds what

```python
    def _record_holding(self, agent: str) -> None:
        data = self._json()
        fields = ("owner", "source_id", "version", "manifest_hash", "status")
        if any(k not in data for k in fields):
            raise HTTPError(400, f"need {fields}")
        record = {k: data[k] for k in fields} | {"holder": agent, "ts": time.time()}
        st = self.state
        with st.lock:
            st.holdings[f"{agent}|{data['owner']}|{data['source_id']}"] = record
            st.save()
            st.audit(agent, "holding", **{k: data[k] for k in fields})
        self._send(201, {"ok": True})
```

- **259–260** All five fields are required.
- **261** Keep just those fields, then add `holder`, which is always the **authenticated** caller, so nobody can record a holding in someone else's name. Plus a timestamp.
- **264** The key is `holder|owner|source`. A newer record replaces the older one, so the registry always shows each holder's latest version. When an owner records its own holding with status `OWNER`, that's how others learn a new version exists.
- **266** `**{...}` spreads the fields into the audit call as named arguments.

## Lines 270–272 · build a server

```python
def make_server(data_dir: Path, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"state": BrokerState(data_dir)})
    return ThreadingHTTPServer((host, port), handler)
```

- **271** The HTTP server creates handlers itself, so we can't pass `state` to them. Instead we **create a subclass on the fly**: `type(name, parents, attributes)` makes a new class `BoundHandler`, based on `Handler`, with `state` set as a class attribute. Every handler instance then sees `self.state`.
- **272** Listen on `host:port`, one thread per request. `127.0.0.1` means local connections only by default.

## Lines 275–279 · run in the background (demo and tests)

```python
def start_in_thread(data_dir: Path, host: str = "127.0.0.1", port: int = 0):
    server = make_server(data_dir, host, port)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://{host}:{server.server_address[1]}"
```

- **275** `port=0` asks the operating system for any free port, so tests never clash.
- **278** Run `serve_forever` in a background thread. `daemon=True` means it won't keep the program alive at exit.
- **279** `server_address[1]` is the port that was actually chosen. Return the server (to shut it down later) and its URL.

---

# Part 2: `client.py`

## Lines 3–9 · imports

```python
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

from .crypto import sha256_hex
```

- **5–6** The standard library's HTTP client and its error types.
- **9** To re-check downloaded blobs.

## Lines 12–15 · a broker error on the client side

```python
class BrokerError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"broker {status}: {message}")
        self.status = status
```

It carries the HTTP status, so callers can react to e.g. 410 "invite already used".

## Lines 18–27 · set up, loading saved credentials

```python
class BrokerClient:
    def __init__(self, url: str, agent: str, cred_path: Path):
        self.url = url.rstrip("/")
        self.agent = agent
        self.cred_path = cred_path
        self.secret = None
        if cred_path.exists():
            creds = json.loads(cred_path.read_text())
            if creds.get("url") == self.url:
                self.secret = creds["secret"]
```

- **20** Drop a trailing `/`, so paths join cleanly.
- **24–27** If we registered before, reuse the saved secret, but only if it was **for this same broker URL**. A secret for one broker means nothing to another.

## Lines 29–52 · one HTTP call

```python
    def _call(self, method: str, path: str, body=None, raw: bytes | None = None, auth: bool = True):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.url + path, data=data, method=method)
        req.add_header("Content-Type", "application/octet-stream" if raw is not None else "application/json")
        if auth:
            if not self.secret:
                raise BrokerError(401, "not registered with this broker")
            req.add_header("X-Ack-Agent", self.agent)
            req.add_header("Authorization", f"Bearer {self.secret}")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = resp.read()
                if resp.headers.get("Content-Type") == "application/octet-stream":
                    return payload
                return json.loads(payload or b"null")
        except urllib.error.HTTPError as e:
            try:
                message = json.loads(e.read()).get("error", e.reason)
            except (json.JSONDecodeError, AttributeError):
                message = e.reason
            raise BrokerError(e.code, message) from None
        except (urllib.error.URLError, OSError) as e:
            reason = getattr(e, "reason", e)
            raise BrokerError(503, f"broker unreachable at {self.url} ({reason})") from None
```

- **30** What to send: raw bytes for blob uploads, JSON bytes for a body, or nothing (for GETs). A nested conditional expression.
- **31** Build the request.
- **32** Label the content type.
- **33–37** Most calls need authentication: refuse early if we never registered, otherwise add our name and secret as headers.
- **39** Send it and wait, up to 30 seconds.
- **40** Read the reply.
- **41–42** Raw bytes for blob downloads…
- **43** …otherwise parse JSON. An empty reply becomes `None`.
- **44** The server answered with an error code…
- **45–48** …so try to read our broker's `{"error": ...}` message, falling back to the standard HTTP reason text.
- **49** Raise our own error. `from None` hides the noisy library traceback, since ours already says everything.
- **50–52** The broker **didn't answer at all**: refused connection, unknown host, timeout. `URLError` and `OSError` cover those. Report it as a `BrokerError` with status 503 ("unavailable") and a readable reason, so callers handle one error type and users don't see a raw traceback. `getattr(e, "reason", e)` uses the error's `reason` if it has one. Added in the bug-fix pass (bug F).

## Lines 54–61 · register once and save the secret

```python
    def register(self, public: dict) -> None:
        if self.secret:
            return
        self.secret = self._call("POST", "/agents", {"agent": self.agent, "public": public}, auth=False)["secret"]
        self.cred_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.cred_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"url": self.url, "secret": self.secret}, f)
```

- **55–56** Already registered → nothing to do. That makes `register()` safe to call every time.
- **57** Register without auth, and keep the returned secret.
- **59** Create the file with owner-only permissions (`0o600`). `O_TRUNC` empties it if it exists.
- **60–61** Save the URL and secret.

## Lines 63–94 · one small method per endpoint

```python
    def create_invite(self, token_hash): self._call("POST", "/invites", {"token_hash": token_hash})
    def redeem_invite(self, token_hash): return self._call("POST", "/invites/redeem", {...})["inviter"]
    def post_message(self, wire):        self._call("POST", "/messages", wire)
    def fetch_messages(self):            return self._call("GET", "/messages")
    def missing(self, ids):              return self._call("POST", "/blobs/missing", {"ids": ids})["missing"]
    def put_blob(self, blob_id, data):   self._call("PUT", f"/blobs/{blob_id}", raw=data)
    def get_blob(self, blob_id): ...
    def record_holding(self, **record):  self._call("POST", "/holdings", record)
    def holdings(self):                  return self._call("GET", "/holdings")
    def audit(self):                     return self._call("GET", "/audit")
```

(Condensed above; each is a one-liner in the file.) Each method names one broker endpoint, so the agent code reads as `self.broker.missing(ids)` rather than raw HTTP.

- **87** `**record` collects named arguments into the JSON body. Callers write `record_holding(owner=..., source_id=..., ...)`.

The one with extra logic:

```python
    def get_blob(self, blob_id: str) -> bytes:
        data = self._call("GET", f"/blobs/{blob_id}")
        if sha256_hex(data) != blob_id:
            raise BrokerError(502, f"blob {blob_id[:12]} failed hash check")
        return data
```

- **83–84** **Re-hash everything downloaded.** The broker is trusted to deliver, not to be honest. A swapped or corrupted blob is caught right here. It's reported as 502 ("bad gateway"), and the receiver turns it into a REJECT.
