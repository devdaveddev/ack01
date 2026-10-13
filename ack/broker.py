"""Broker: stores blobs by hash, queues messages for offline agents, fans announcements
out to subscribers and keeps their signed votes, tracks who holds which version, and
writes an audit log (the "carbon copy").

It never approves anything. For announcements it verifies signatures and counts votes
against the quorum the publisher set, so anyone can see whether a change is sealed,
held, or still pending; the decisions themselves are the subscribers'.

Two transports serve the same core:
  ack://host:port   the Ack wire: length-prefixed frames over TCP (see ack/wire.py)
  http://host:port  plain HTTP, kept for compatibility and for curl

Operations (method path). Everything but POST /agents needs the agent's name and secret.
  POST /agents                       {agent, public} -> {secret}
  GET  /agents/<name>/key            -> {agent, public}       (for checking signatures)
  POST /invites                      {token_hash}
  POST /invites/redeem               {token_hash} -> {inviter}   (one-time)
  POST /messages                     wire envelope
  GET  /messages                     -> [envelopes]               (drains your queue)
  POST /blobs/missing                {ids} -> {missing}
  PUT  /blobs/<sha256>               raw bytes; hash must match
  GET  /blobs/<sha256>               raw bytes
  POST /holdings                     {owner, source_id, version, manifest_hash, status}
  GET  /holdings                     -> [records]
  POST /subscriptions                {publisher}          subscribe to a publisher's announcements
  POST /subscriptions/remove         {publisher}
  GET  /subscriptions                -> {publishers: [...], subscribers: [...]}
  POST /announcements                {statement, sig, copies: {subscriber: blob_id}} -> {id, status}
  POST /announcements/<id>/sign      {vote, sig} -> {status}
  GET  /announcements                -> [records you published or were asked to sign]
  GET  /announcements/<id>           -> record
  GET  /audit                        -> [entries about you]
"""

import hashlib
import hmac
import json
import re
import secrets
import socketserver
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .crypto import canonical, verify
from . import wire

MAX_BODY = 64 << 20
HEX64 = re.compile(r"^[0-9a-f]{64}$")
NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
ANN_ID = re.compile(r"^[0-9a-f]{32}$")
VERDICTS = ("ACK", "NAK")


class HTTPError(Exception):
    """A refusal with a status code. (Named for HTTP, used by both transports.)"""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def tally(record: dict) -> dict:
    """Sealed once ACKs reach the quorum; held once enough NAKs make that impossible."""
    st = record["statement"]
    votes = record["votes"].values()
    acks = sum(1 for v in votes if v["vote"]["verdict"] == "ACK")
    naks = sum(1 for v in votes if v["vote"]["verdict"] == "NAK")
    n, k = len(st["subscribers"]), st["quorum"]
    state = "sealed" if acks >= k else "held" if n - naks < k else "pending"
    waiting = [s for s in st["subscribers"] if s not in record["votes"]]
    return {"state": state, "acks": acks, "naks": naks, "quorum": k, "of": n, "waiting": waiting}


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
        self.subscriptions: dict = state.get("subscriptions", {})  # publisher -> [subscribers]
        self.announcements: dict = state.get("announcements", {})  # id -> record

    def save(self) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"agents": self.agents, "invites": self.invites, "queues": self.queues,
                                   "holdings": self.holdings, "subscriptions": self.subscriptions,
                                   "announcements": self.announcements}))
        tmp.replace(self.state_path)

    def audit(self, agent: str | None, action: str, **detail) -> None:
        entry = {"ts": time.time(), "agent": agent, "action": action, **detail}
        with self.audit_path.open("a") as f:
            f.write(json.dumps(entry) + "\n")

    def authenticate(self, agent: str | None, auth: str | None) -> str:
        record = self.agents.get(agent or "")
        secret = (auth or "").removeprefix("Bearer ")
        if not record or not hmac.compare_digest(record["secret_hash"], _sha(secret.encode())):
            raise HTTPError(401, "unauthorized")
        return agent

    def visible_holdings(self, agent: str) -> list[dict]:
        """Records you made, records about parts you own, and owners' version records
        for parts you hold. Not who else holds what."""
        recs = list(self.holdings.values())
        mine = {(r["owner"], r["source_id"]) for r in recs if r["holder"] == agent}
        return [r for r in recs if r["holder"] == agent or r["owner"] == agent
                or (r["holder"] == r["owner"] and (r["owner"], r["source_id"]) in mine)]

    def blob_path(self, blob_id: str) -> Path:
        if not HEX64.match(blob_id):
            raise HTTPError(400, "bad blob id")
        return self.blob_dir / blob_id

    def enqueue(self, to: str, wire_msg: dict) -> None:
        self.queues.setdefault(to, []).append(wire_msg)


@dataclass
class Reply:
    status: int
    payload: object
    raw: bool = False
    ctype: str = ""


BROWSER_NOTE = ("This is an Ack broker. It relays messages, files and announcements between agents, "
                "and it has no web page of its own.\n\nOpen your agent's app instead "
                "(python -m ack web ... prints its address, usually http://127.0.0.1:8800/).\n")


class BrokerCore:
    """Every broker operation, independent of how the request arrived."""

    def __init__(self, state: BrokerState):
        self.st = state

    def handle(self, method: str, path: str, agent_name: str | None, auth: str | None, body: bytes) -> Reply:
        try:
            if len(body) > MAX_BODY:
                raise HTTPError(413, "body too large")
            path = path.split("?", 1)[0].rstrip("/")
            if method == "POST" and path == "/agents":
                return self._register(self._json(body))
            if method == "GET" and path == "":
                return Reply(200, BROWSER_NOTE.encode(), raw=True, ctype="text/plain; charset=utf-8")
            with self.st.lock:
                agent = self.st.authenticate(agent_name, auth)
            return self._route(method, path, agent, body)
        except HTTPError as e:
            return Reply(e.status, {"error": str(e)})

    @staticmethod
    def _json(body: bytes) -> dict:
        try:
            data = json.loads(body or b"{}")
        except json.JSONDecodeError:
            raise HTTPError(400, "invalid JSON")
        if not isinstance(data, dict):
            raise HTTPError(400, "expected an object")
        return data

    def _route(self, method: str, path: str, agent: str, body: bytes) -> Reply:
        st, route = self.st, (method, path)
        if route == ("POST", "/invites"):
            return self._create_invite(agent, self._json(body))
        if route == ("POST", "/invites/redeem"):
            return self._redeem_invite(agent, self._json(body))
        if route == ("POST", "/messages"):
            return self._post_message(agent, self._json(body))
        if route == ("GET", "/messages"):
            with st.lock:
                msgs, st.queues[agent] = st.queues.get(agent, []), []
                if msgs:
                    st.save()
            return Reply(200, msgs)
        if route == ("POST", "/blobs/missing"):
            ids = self._json(body).get("ids")
            if not isinstance(ids, list):
                raise HTTPError(400, "ids must be a list")
            return Reply(200, {"missing": [i for i in ids if not st.blob_path(i).exists()]})
        if path.startswith("/blobs/") and method in ("GET", "PUT"):
            return self._blob(agent, method, path.removeprefix("/blobs/"), body)
        if route == ("POST", "/holdings"):
            return self._record_holding(agent, self._json(body))
        if route == ("GET", "/holdings"):
            with st.lock:
                return Reply(200, st.visible_holdings(agent))
        m = re.fullmatch(r"/agents/([A-Za-z0-9_.-]{1,64})/key", path)
        if m and method == "GET":
            with st.lock:
                rec = st.agents.get(m.group(1))
            if not rec:
                raise HTTPError(404, "no such agent")
            return Reply(200, {"agent": m.group(1), "public": rec["public"]})
        if route == ("POST", "/subscriptions"):
            return self._subscribe(agent, self._json(body), True)
        if route == ("POST", "/subscriptions/remove"):
            return self._subscribe(agent, self._json(body), False)
        if route == ("GET", "/subscriptions"):
            with st.lock:
                return Reply(200, {"publishers": sorted(p for p, subs in st.subscriptions.items() if agent in subs),
                                   "subscribers": sorted(st.subscriptions.get(agent, []))})
        if route == ("POST", "/announcements"):
            return self._announce(agent, self._json(body))
        m = re.fullmatch(r"/announcements/([0-9a-f]{32})(/sign)?", path)
        if m and method == "POST" and m.group(2):
            return self._sign(agent, m.group(1), self._json(body))
        if m and method == "GET" and not m.group(2):
            with st.lock:
                rec = st.announcements.get(m.group(1))
                if not rec or agent not in (rec["statement"]["publisher"], *rec["statement"]["subscribers"]):
                    raise HTTPError(404, "no such announcement")
                return Reply(200, self._view(rec))
        if route == ("GET", "/announcements"):
            with st.lock:
                recs = [self._view(r) for r in st.announcements.values()
                        if agent in (r["statement"]["publisher"], *r["statement"]["subscribers"])]
            return Reply(200, sorted(recs, key=lambda r: -r["statement"]["ts"]))
        if route == ("GET", "/audit"):
            lines = st.audit_path.read_text().splitlines() if st.audit_path.exists() else []
            entries = [json.loads(line) for line in lines]
            # Only entries about you: the full log would show everyone who talks to whom.
            return Reply(200, [e for e in entries if agent in (e.get("agent"), e.get("to"), *e.get("involves", []))])
        raise HTTPError(404, "not found")

    # ---- agents, invites, messages, blobs, holdings ----

    def _register(self, data: dict) -> Reply:
        agent, public = data.get("agent"), data.get("public")
        if not isinstance(agent, str) or not NAME.match(agent) or not isinstance(public, dict):
            raise HTTPError(400, "need agent name and public keys")
        st = self.st
        with st.lock:
            if agent in st.agents:
                raise HTTPError(409, "agent name taken")
            secret = secrets.token_urlsafe(32)
            st.agents[agent] = {"public": public, "secret_hash": _sha(secret.encode())}
            st.queues.setdefault(agent, [])
            st.save()
            st.audit(agent, "register")
        return Reply(201, {"secret": secret})

    def _create_invite(self, agent: str, data: dict) -> Reply:
        th = data.get("token_hash")
        if not isinstance(th, str) or not HEX64.match(th):
            raise HTTPError(400, "bad token hash")
        st = self.st
        with st.lock:
            if th in st.invites:
                raise HTTPError(409, "invite exists")
            st.invites[th] = {"inviter": agent, "used_by": None, "created": time.time()}
            st.save()
            st.audit(agent, "invite.create")
        return Reply(201, {"ok": True})

    def _redeem_invite(self, agent: str, data: dict) -> Reply:
        th = data.get("token_hash")
        st = self.st
        with st.lock:
            invite = st.invites.get(th) if isinstance(th, str) else None
            if invite is None:
                raise HTTPError(404, "no such invite")
            if invite["used_by"] is not None:
                raise HTTPError(410, "invite already used")
            invite["used_by"] = agent
            st.save()
            st.audit(agent, "invite.redeem", inviter=invite["inviter"])
        return Reply(200, {"inviter": invite["inviter"]})

    def _post_message(self, agent: str, msg: dict) -> Reply:
        if msg.get("from") != agent:
            raise HTTPError(403, "from must match the authenticated agent")
        if msg.get("type") in ("ANNOUNCE", "SIGNED"):
            raise HTTPError(403, "announcements and votes go through /announcements")
        st = self.st
        with st.lock:
            if msg.get("to") not in st.agents:
                raise HTTPError(404, "unknown recipient")
            st.enqueue(msg["to"], msg)
            st.save()
            st.audit(agent, "message", id=msg.get("id"), type=msg.get("type"), to=msg["to"], reply_to=msg.get("reply_to"))
        return Reply(202, {"queued": True})

    def _blob(self, agent: str, method: str, blob_id: str, body: bytes) -> Reply:
        st = self.st
        path = st.blob_path(blob_id)
        if method == "GET":
            if not path.exists():
                raise HTTPError(404, "no such blob")
            data = path.read_bytes()
            st.audit(agent, "blob.get", blob=blob_id, size=len(data))
            return Reply(200, data, raw=True)
        if _sha(body) != blob_id:
            raise HTTPError(400, "blob does not match its hash")
        if not path.exists():
            tmp = path.with_suffix(f".{secrets.token_hex(4)}.tmp")
            tmp.write_bytes(body)
            tmp.replace(path)
        st.audit(agent, "blob.put", blob=blob_id, size=len(body))
        return Reply(201, {"ok": True})

    def _record_holding(self, agent: str, data: dict) -> Reply:
        fields = ("owner", "source_id", "version", "manifest_hash", "status")
        if any(k not in data for k in fields):
            raise HTTPError(400, f"need {fields}")
        record = {k: data[k] for k in fields} | {"holder": agent, "ts": time.time()}
        st = self.st
        with st.lock:
            st.holdings[f"{agent}|{data['owner']}|{data['source_id']}"] = record
            st.save()
            st.audit(agent, "holding", **{k: data[k] for k in fields})
        return Reply(201, {"ok": True})

    # ---- announcements: fan out, collect signed votes, keep the tally ----

    def _subscribe(self, agent: str, data: dict, on: bool) -> Reply:
        publisher = data.get("publisher")
        st = self.st
        with st.lock:
            if publisher not in st.agents or publisher == agent:
                raise HTTPError(404, "no such publisher")
            subs = st.subscriptions.setdefault(publisher, [])
            if on and agent not in subs:
                subs.append(agent)
            elif not on and agent in subs:
                subs.remove(agent)
            st.save()
            st.audit(agent, "subscribe" if on else "unsubscribe", publisher=publisher, involves=[publisher])
        return Reply(200, {"ok": True})

    def _announce(self, agent: str, data: dict) -> Reply:
        stmt, sig, copies = data.get("statement"), data.get("sig"), data.get("copies")
        if not isinstance(stmt, dict) or not isinstance(sig, str) or not isinstance(copies, dict):
            raise HTTPError(400, "need statement, sig and copies")
        try:
            ann_id, subs, quorum = stmt["id"], stmt["subscribers"], stmt["quorum"]
            ok = (ANN_ID.match(ann_id) and stmt["publisher"] == agent and HEX64.match(stmt["digest"])
                  and isinstance(subs, list) and subs and isinstance(quorum, int) and 1 <= quorum <= len(subs)
                  and set(copies) == set(subs) and all(HEX64.match(b) for b in copies.values()))
        except (KeyError, TypeError):
            ok = False
        if not ok:
            raise HTTPError(400, "malformed announcement")
        st = self.st
        with st.lock:
            if ann_id in st.announcements:
                raise HTTPError(409, "announcement exists")
            if not verify(st.agents[agent]["public"], canonical(stmt), sig):
                raise HTTPError(403, "statement signature does not verify")
            not_subscribed = [s for s in subs if s not in st.subscriptions.get(agent, [])]
            if not_subscribed:
                raise HTTPError(403, f"not subscribed to you: {', '.join(not_subscribed)}")
            missing = [b for b in copies.values() if not st.blob_path(b).exists()]
            if missing:
                raise HTTPError(409, "upload the copies first")
            record = {"statement": stmt, "sig": sig, "copies": copies, "votes": {}, "created": time.time()}
            st.announcements[ann_id] = record
            for sub in subs:
                st.enqueue(sub, {"v": 1, "id": secrets.token_hex(16), "type": "ANNOUNCE", "from": agent, "to": sub,
                                 "ts": time.time(), "reply_to": None,
                                 "body": {"statement": stmt, "sig": sig, "blob": copies[sub]}})
            st.save()
            st.audit(agent, "ANNOUNCE", announcement=ann_id, title=stmt.get("title"), digest=stmt["digest"],
                     encrypted=stmt.get("encrypted", False), involves=subs)
            st.audit(None, "FANOUT", announcement=ann_id, copies=len(subs), involves=[agent, *subs])
            status = tally(record)
        return Reply(201, {"id": ann_id, "status": status})

    def _sign(self, agent: str, ann_id: str, data: dict) -> Reply:
        vote, sig = data.get("vote"), data.get("sig")
        st = self.st
        with st.lock:
            rec = st.announcements.get(ann_id)
            if not rec or agent not in rec["statement"]["subscribers"]:
                raise HTTPError(404, "no such announcement for you")
            if agent in rec["votes"]:
                raise HTTPError(409, "already signed")
            if (not isinstance(vote, dict) or vote.get("announcement") != ann_id or vote.get("signer") != agent
                    or vote.get("verdict") not in VERDICTS or not isinstance(sig, str)):
                raise HTTPError(400, "malformed vote")
            if not verify(st.agents[agent]["public"], canonical(vote), sig):
                raise HTTPError(403, "vote signature does not verify")
            before = tally(rec)["state"]
            rec["votes"][agent] = {"vote": vote, "sig": sig}
            status = tally(rec)
            publisher = rec["statement"]["publisher"]
            st.enqueue(publisher, {"v": 1, "id": secrets.token_hex(16), "type": "SIGNED", "from": agent,
                                   "to": publisher, "ts": time.time(), "reply_to": None,
                                   "body": {"vote": vote, "sig": sig, "status": status}})
            st.save()
            st.audit(agent, vote["verdict"], announcement=ann_id, digest_seen=vote.get("digest_seen"),
                     reason=vote.get("reason", ""), involves=[publisher])
            if status["state"] != before and status["state"] != "pending":
                st.audit(None, status["state"].upper(), announcement=ann_id,
                         detail=f"{status['acks']} of {status['of']} acknowledged, quorum {status['quorum']}",
                         involves=[publisher, *rec["statement"]["subscribers"]])
        return Reply(200, {"status": status})

    @staticmethod
    def _view(rec: dict) -> dict:
        return {"statement": rec["statement"], "sig": rec["sig"], "votes": rec["votes"], "status": tally(rec),
                "created": rec["created"]}


# ---------------------------------------------------------------- HTTP transport

class Handler(BaseHTTPRequestHandler):
    server_version = "ack-broker/0.2"
    core: BrokerCore  # set by make_server

    def log_message(self, *args) -> None:
        pass

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def _dispatch(self, method: str) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            reply = Reply(413, {"error": "body too large"})
        else:
            body = self.rfile.read(length)
            reply = self.core.handle(method, self.path, self.headers.get("X-Ack-Agent"),
                                     self.headers.get("Authorization"), body)
        data = reply.payload if reply.raw else json.dumps(reply.payload).encode()
        self.send_response(reply.status)
        self.send_header("Content-Type", reply.ctype or ("application/octet-stream" if reply.raw else "application/json"))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the client went away mid-reply; it will retry on its next poll


def make_server(data_dir: Path, host: str = "127.0.0.1", port: int = 8765, core: BrokerCore | None = None
                ) -> ThreadingHTTPServer:
    core = core or BrokerCore(BrokerState(data_dir))
    return ThreadingHTTPServer((host, port), type("BoundHandler", (Handler,), {"core": core}))


# ---------------------------------------------------------------- Ack wire transport

class WireHandler(socketserver.BaseRequestHandler):
    core: BrokerCore  # set by make_wire_server

    def handle(self) -> None:
        sock = self.request
        while True:
            try:
                header, body = wire.read_frame(sock, MAX_BODY)
            except (wire.WireError, OSError):
                return  # closed, or not speaking Ack wire
            reply = self.core.handle(str(header.get("op", "")), str(header.get("path", "")), header.get("agent"),
                                     header.get("auth"), body)
            data = reply.payload if reply.raw else json.dumps(reply.payload).encode()
            try:
                wire.write_frame(sock, {"status": reply.status, "raw": reply.raw, "ctype": reply.ctype}, data)
            except OSError:
                return


class WireServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_wire_server(data_dir: Path, host: str = "127.0.0.1", port: int = 7468, core: BrokerCore | None = None
                     ) -> WireServer:
    core = core or BrokerCore(BrokerState(data_dir))
    return WireServer((host, port), type("BoundWireHandler", (WireHandler,), {"core": core}))


def start_in_thread(data_dir: Path, host: str = "127.0.0.1", port: int = 0, transport: str = "http"):
    """Starts a broker on a free port. Returns (server, base_url); the url's scheme says which transport."""
    if transport == "wire":
        server = make_wire_server(data_dir, host, port)
        scheme = "ack"
    else:
        server = make_server(data_dir, host, port)
        scheme = "http"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"{scheme}://{host}:{server.server_address[1]}"
