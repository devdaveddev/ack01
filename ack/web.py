"""The web app: a local browser interface for one agent.

    python -m ack web --root ~/myproject --name alice --broker http://broker:8765
    python -m ack web --demo        # try it: a broker and two agents, two browser tabs

It runs on your machine because the agent's keys, project files and quarantine
live there. It only listens on 127.0.0.1.

How the pieces fit:
  - One Agent, guarded by one lock (a Condition). Every API call and every poll takes it.
  - A background thread polls the broker every couple of seconds.
  - The approver doesn't answer by itself. It files an approval request, then waits
    (releasing the lock, so the page keeps working) until you click Approve or Reject
    in the browser. Unanswered requests are refused after APPROVAL_TIMEOUT.

Security: every /api call needs the per-run token that's baked into the page, so
another website open in your browser can't drive the agent; the Host header must be
localhost, which blocks DNS-rebinding tricks; and the page renders all peer-supplied
text as text, never as HTML.
"""

import dataclasses
import json
import os
import secrets
import shlex
import tempfile
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import context, quarantine
from .agent import Agent, UnknownPeer
from .audit import audit
from .bundle import BundleError, safe_relpath
from .client import BrokerError
from .crypto import CryptoError, fingerprint
from .memory import Entry
from .pairing import Invite, qr_svg
from .spelling import correct as spell_correct

APPROVAL_TIMEOUT = 15 * 60
POLL_EVERY = 2.0
MAX_EVENTS = 200
MAX_TREE = 2000
MAX_CONTEXT_ITEMS = 80  # candidates sent to the page; the rest are low matches
SKIP_DIRS = {"__pycache__", "vendor", "node_modules", "venv", "dist", "build"}
# The React frontend (site/), once built with `npm run build`. Without it the classic page is served.
SITE = Path(__file__).resolve().parent.parent / "site" / "dist"
ASSET_TYPES = {".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml", ".png": "image/png",
               ".woff2": "font/woff2", ".ico": "image/x-icon", ".json": "application/json"}
MAX_ANNOUNCE_FILE = 200_000
# What the landing page's "Submit a handoff" announces: real text, real digest, real signatures.
HANDOFF_FILES = {"billing/charge.py": "def charge(order):\n    for attempt in range(3):\n"
                                      "        if gateway.send(order):\n            return True\n    return False\n"}
UI = Path(__file__).with_name("webui.html")


class WebApp:
    def __init__(self, name: str, root: Path, broker_url: str, judge=None, hints: list[str] | None = None,
                 poll_every: float | None = None):
        self.cond = threading.Condition()
        self.pending: dict[str, dict] = {}
        self.broker_error = ""
        self.agent = Agent(name, root, broker_url, judge=judge, approver=self._approve, log=None)
        self.token = secrets.token_urlsafe(24)
        self.hints = hints or []  # example questions shown under the Ask box (the demo sets these)
        self.ctx: dict = {"state": "idle"}  # the current context-pack job (fetching runs off the agent lock)
        self.subs: dict = {"publishers": [], "subscribers": []}  # refreshed from the broker on every poll
        self.poll_every = poll_every
        self._stop = threading.Event()

    # ---- approvals: wait for a click in the browser ----

    def _approve(self, action: str, detail: str) -> bool:
        # Called from inside agent.poll(), so we already hold self.cond.
        req = {"id": uuid.uuid4().hex, "action": action, "detail": detail, "created": time.time(),
               "context": self._context(action), "answer": None}
        self.pending[req["id"]] = req
        self.cond.notify_all()
        answered = self.cond.wait_for(lambda: req["answer"] is not None, timeout=APPROVAL_TIMEOUT)
        self.pending.pop(req["id"], None)
        return bool(req["answer"]) if answered else False

    def _context(self, action: str) -> dict:
        ctx = self.agent.approval_context
        manifest = ctx.get("manifest") or {}
        out = {"peer": ctx.get("peer"), "source_id": manifest.get("source_id"), "version": manifest.get("version"),
               "manifest_hash": ctx.get("manifest_hash"), "intent": ctx.get("intent"),
               "fetched": ctx.get("fetched"), "tests": manifest.get("tests"), "deps": manifest.get("deps"),
               "files": [{"path": f["path"], "size": f["size"]} for f in manifest.get("files", [])]}
        if action == "run-tests" and ctx.get("qdir"):
            out["preview"] = quarantine.preview(manifest, Path(ctx["qdir"]))
        if action == "install" and ctx.get("report"):
            out["report"] = dataclasses.asdict(ctx["report"])
            out["decision"] = dataclasses.asdict(ctx["decision"])
        return out

    def answer(self, approval_id: str, approve: bool) -> None:
        with self.cond:
            req = self.pending.get(approval_id)
            if req is None:
                raise ValueError("that approval is no longer pending")
            req["answer"] = bool(approve)
            self.cond.notify_all()

    # ---- background polling ----

    def start(self) -> None:
        try:
            with self.cond:
                self.agent.register()
        except BrokerError as e:
            self.broker_error = str(e)
        threading.Thread(target=self._poll_loop, daemon=True).start()

    def _poll_loop(self) -> None:
        while not self._stop.wait(self.poll_every or POLL_EVERY):
            self.poll()

    def poll(self) -> int:
        try:
            with self.cond:
                if self.pending:
                    # A receive is paused waiting for you. Polling now would start a second one
                    # alongside it, so incoming messages wait at the broker until you answer.
                    return 0
                if not self.agent.broker.secret:
                    self.agent.register()
                n = self.agent.poll()
                self.subs = self.agent.subscriptions()
            self.broker_error = ""
            return n
        except BrokerError as e:
            self.broker_error = str(e)
            return 0

    def stop(self) -> None:
        self._stop.set()

    # ---- what the page shows ----

    def status(self) -> dict:
        with self.cond:
            a = self.agent
            st = a.state
            parts = []
            for e in a.memory.entries.values():
                parts.append({"id": e.id, "topic": e.topic, "summary": e.summary, "tags": e.tags, "base": e.base,
                              "files": e.files, "version": e.version, "allow": e.allow, "tests": e.tests,
                              "deps": e.deps, "notes": e.notes, "current": a.memory.is_current(e),
                              "holders": {p: h["manifest_hash"][:12] for p, h in st["shares"].get(e.id, {}).items()}})
            answers = [{"peer": k.split("|", 1)[0], "question": k.split("|", 1)[1], **v}
                       for k, v in st["answers"].items()]
            snapshot = {
                "me": {"name": a.name, "fingerprint": fingerprint(a.identity.public()), "root": str(a.root),
                       "broker": a.broker.url, "registered": bool(a.broker.secret),
                       "broker_error": self.broker_error},
                "peers": [{"name": n, "fingerprint": fingerprint(p),
                           # "checked" only while the key is still the one you compared
                           "checked": st.get("checked", {}).get(n) == fingerprint(p)}
                          for n, p in st["peers"].items()],
                "open_invites": len(st["invites"]),
                "parts": parts,
                "installs": list(st["installs"].values()),
                "updates": st["updates"],
                "answers": sorted(answers, key=lambda x: -x.get("received", 0)),
                "waiting_answers": len(st["queries"]),
                "requests": list(st["requests"].values()),
                "approvals": [{k: v for k, v in r.items() if k != "answer"} for r in self.pending.values()],
                "events": [{"kind": k, "text": t} for k, t in a.events[-MAX_EVENTS:]][::-1],
                "provenance": a.provenance()[-50:][::-1],
                "hints": self.hints,
                "context": self._context_status(),
                "transport": "ack wire" if a.broker.is_wire else "http",
                "subscriptions": self.subs,
                "sign_policy": st.get("sign_policy", "auto"),
                "published": sorted(({k: v for k, v in r.items() if k != "copies"} for r in st.get("published", {}).values()),
                                    key=lambda r: -r["statement"]["ts"]),
                "inbox": sorted(st.get("inbox", {}).values(), key=lambda r: -r["statement"]["ts"]),
            }
        return json.loads(json.dumps(snapshot, default=str))  # a detached copy, safe to serialise later

    def tree(self) -> list[str]:
        """Project files the share form can pick from. Skips hidden, vendored and build folders
        without walking into them, so a big node_modules doesn't stall the page."""
        root, out = self.agent.root, []
        for folder, dirs, files in os.walk(root):
            dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in SKIP_DIRS)
            for name in sorted(files):
                if not name.startswith("."):
                    out.append(str((Path(folder) / name).relative_to(root)))
                    if len(out) >= MAX_TREE:
                        return out
        return out

    # ---- context packs ----

    def _context_dir(self) -> Path:
        return self.agent.dir / "context"

    def _context_status(self) -> dict:
        c = self.ctx
        items = c.get("items", [])
        shown = items[:MAX_CONTEXT_ITEMS]
        saved = []
        for meta in sorted(self._context_dir().glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:20]:
            try:
                m = json.loads(meta.read_text())
                saved.append({"file": meta.stem, "name": m["name"], "goal": m["goal"], "created": m["created"],
                              "items": len(m["items"])})
            except (ValueError, KeyError, OSError):
                continue
        return {"state": c["state"], "job": c.get("job"), "goal": c.get("goal", ""), "budget": c.get("budget", 0),
                "error": c.get("error", ""), "errors": c.get("errors", []), "total": len(items),
                "items": [{k: v for k, v in dataclasses.asdict(it).items() if k != "text"} for it in shown],
                "pack": c.get("pack"), "saved": saved}

    def context_start(self, body: dict) -> dict:
        if self.ctx.get("state") == "running":
            raise ValueError("still fetching the last request; wait for it to finish")
        goal = _need(body, "goal")
        github, sites = _list(body.get("github")), _list(body.get("sites"))
        if not github and not sites:
            raise ValueError("add at least one GitHub repository or website")
        for url in github:
            context.parse_github(url)  # fail fast on a typo, before starting
        pages, budget = int(body.get("pages") or 10), int(body.get("budget") or 12000)
        job = uuid.uuid4().hex[:8]
        self.ctx = {"state": "running", "job": job, "goal": goal, "budget": budget, "items": [], "errors": []}

        def work():
            try:
                items, errors = context.gather(goal, github, sites, only=_list(body.get("only")),
                                               max_pages=pages, budget=budget)
                result = {"state": "ready", "items": items, "errors": errors}
            except context.ContextError as e:
                result = {"state": "error", "error": str(e)}
            except Exception as e:  # never leave the page stuck on "fetching"
                result = {"state": "error", "error": f"{type(e).__name__}: {e}"}
            if self.ctx.get("job") == job:
                self.ctx.update(result)

        threading.Thread(target=work, daemon=True).start()
        return {"job": job}

    def context_preview(self, item_id: str) -> dict:
        for it in self.ctx.get("items", []):
            if it.id == item_id:
                return {"text": it.text, "url": it.url, "start": it.start, "lang": it.lang}
        raise ValueError("that item is no longer available; fetch again")

    def context_build(self, body: dict) -> dict:
        items = self.ctx.get("items") or []
        if self.ctx.get("state") != "ready" or not items:
            raise ValueError("fetch some sources first")
        chosen = set(body.get("ids") or [])
        for it in items:
            it.selected = it.id in chosen
        name = str(body.get("name") or "").strip() or context.slug(self.ctx["goal"])
        pack = context.render(name, self.ctx["goal"], items, str(self.agent.root))
        path = context.save(pack, self._context_dir())
        self.ctx["pack"] = self._pack_info(pack.name, pack.markdown, pack.prompt, path, pack.meta()["items"])
        return self.ctx["pack"]

    @staticmethod
    def _pack_info(name: str, markdown: str, prompt: str, path: Path, items: list[dict]) -> dict:
        licenses: dict[str, int] = {}
        for it in items:
            licenses[it.get("license") or "unknown"] = licenses.get(it.get("license") or "unknown", 0) + 1
        return {"name": name, "markdown": markdown, "prompt": prompt, "path": str(path), "items": len(items),
                "tokens": sum(i.get("tokens", 0) for i in items), "licenses": licenses,
                "sources": sorted({i["source"] for i in items}), "file": path.stem}

    def context_open(self, file: str) -> dict:
        path = self._context_dir() / f"{context.slug(file)}.md"
        if not path.is_file():
            raise ValueError("no such pack")
        meta = json.loads(path.with_suffix(".json").read_text())
        md = path.read_text()
        prompt = md.split("## Prompt", 1)[-1].split("## Key points", 1)[0].strip()
        self.ctx["pack"] = self._pack_info(meta["name"], md, prompt, path, meta["items"])
        return self.ctx["pack"]

    def context_check(self, file: str) -> dict:
        path = self._context_dir() / f"{context.slug(file)}.json"
        if not path.is_file():
            raise ValueError("no such pack")
        return {"results": context.check(json.loads(path.read_text()))}

    # ---- actions ----

    def act(self, action: str, body: dict):
        a = self.agent
        if action == "approve":
            return self.answer(body["id"], body["approve"])
        if action == "poll":
            return {"handled": self.poll()}
        # Context packs touch the network and their own folder, never the agent's state,
        # so they run without the agent lock and can't stall polling or approvals.
        if action == "context/start":
            return self.context_start(body)
        if action == "context/preview":
            return self.context_preview(str(body.get("id", "")))
        if action == "context/build":
            return self.context_build(body)
        if action == "context/open":
            return self.context_open(str(body.get("file", "")))
        if action == "context/check":
            return self.context_check(str(body.get("file", "")))
        with self.cond:
            if action == "invite":
                uri = a.invite().to_uri()
                return {"uri": uri, "svg": qr_svg(uri)}
            if action == "join":
                a.join(Invite.from_uri(body["uri"].strip()))
                return {"ok": True}
            if action == "ask":
                exact = str(body.get("exact", "")).lower() in ("1", "true", "on", "yes")
                cached = a.ask(body["peer"], _need(body, "text"), exact=exact)
                return {"cached": cached, "changes": a.last_correction}
            if action == "spell":
                fixed, changes = spell_correct(str(body.get("text", "")), a.memory.vocabulary())
                return {"text": fixed, "changes": changes}
            if action == "request":
                return {"id": a.request(body["peer"], _need(body, "source_id"), _need(body, "reason"),
                                        _need(body, "intent"))}
            if action == "share":
                tests = shlex.split(body.get("tests", "")) or None
                entry = Entry(id=_need(body, "id"), topic=_need(body, "topic"), summary=_need(body, "summary"),
                              files=_list(body.get("files")), base=body.get("base", "").strip().strip("/"),
                              tags=_list(body.get("tags")), allow=_list(body.get("allow")),
                              notes=body.get("notes", ""), tests=tests, deps=_list(body.get("deps")))
                if not entry.files:
                    raise ValueError("pick at least one file")
                missing = [f for f in entry.files if not (a.memory.part_root(entry) / f).is_file()]
                if missing:
                    raise ValueError(f"not found under {entry.base or 'the project root'}: {', '.join(missing)}")
                a.share(entry)
                return {"ok": True}
            if action == "check-peer":
                peer = body["peer"]
                if peer not in a.state["peers"]:
                    raise ValueError(f"not paired with {peer}")
                a.state.setdefault("checked", {})[peer] = fingerprint(a.state["peers"][peer])
                a._save()
                return {"ok": True}
            if action == "unshare":
                a.memory.entries.pop(body["id"], None)
                a.memory.save()
                return {"ok": True}
            if action == "update":
                return {"notified": a.publish_update(body["source_id"])}
            if action == "audit":
                return {"findings": [dataclasses.asdict(f) for f in audit(a)]}
            # ---- announcements: fan out, sign, quorum ----
            if action == "subscribe":
                a.subscribe(_need(body, "publisher"), on=body.get("on", True) is not False)
                self.subs = a.subscriptions()
                return {"ok": True}
            if action == "sign-policy":
                a.set_sign_policy(_need(body, "policy"))
                return {"ok": True}
            if action == "announce":
                files = self._announce_files(body)
                rec = a.announce(_need(body, "title"), files, encrypted=bool(body.get("encrypted")),
                                 subscribers=_list(body.get("subscribers")) or None,
                                 quorum=int(body["quorum"]) if body.get("quorum") else None,
                                 tamper=_list(body.get("tamper")))
                return {"id": rec["statement"]["id"], "status": rec["status"]}
            if action == "vote":
                return {"status": a.vote(_need(body, "id"), _need(body, "verdict"), str(body.get("reason", "")))}
            if action == "refresh":
                a.refresh_announcements()
                return {"ok": True}
            if action == "carbon":
                return {"entries": a.broker.audit()[-300:][::-1]}
            if action == "handoff":
                subs = self.subs["subscribers"] or a.subscriptions()["subscribers"]
                if not subs:
                    raise ValueError(f"nobody subscribes to {a.name} yet")
                tamper = [str(body.get("tamper_for") or subs[0])] if body.get("tamper") else []
                rec = a.announce("billing/charge.py retry fix", HANDOFF_FILES, encrypted=bool(body.get("encrypted")),
                                 subscribers=subs, tamper=tamper)
                return self._handoff_view(rec["statement"]["id"])
            if action == "handoff/status":
                return self._handoff_view(_need(body, "id"))
        raise ValueError(f"unknown action {action!r}")


    def _announce_files(self, body: dict) -> dict[str, str]:
        """Project files picked by path, plus an optional pasted file. Text only: an announcement
        is something people and agents read and compare, not a binary drop."""
        files = {}
        for rel in _list(body.get("files")):
            path = self.agent.root / safe_relpath(rel)
            if not path.is_file():
                raise ValueError(f"no file {rel}")
            data = path.read_bytes()
            if len(data) > MAX_ANNOUNCE_FILE:
                raise ValueError(f"{rel} is too large to announce")
            try:
                files[rel] = data.decode()
            except UnicodeDecodeError:
                raise ValueError(f"{rel} is not text") from None
        pasted = body.get("paste") or {}
        if isinstance(pasted, dict) and str(pasted.get("content", "")).strip():
            files[safe_relpath(str(pasted.get("path") or "note.txt").strip())] = str(pasted["content"])
        if not files:
            raise ValueError("pick a file or paste one")
        return files

    def _handoff_view(self, ann_id: str) -> dict:
        """One announcement as the publisher sees it: statement, signed votes, tally, and the
        broker's carbon copy of everything that happened to it. Called with the lock held."""
        a = self.agent
        rec = a.broker.announcement(ann_id)
        mine = a.state.get("published", {}).get(ann_id, {})
        carbon = [e for e in a.broker.audit() if e.get("announcement") == ann_id]
        return {"statement": rec["statement"], "votes": rec["votes"], "status": rec["status"],
                "tampered": mine.get("tampered", []), "files": HANDOFF_FILES if
                rec["statement"]["title"] == "billing/charge.py retry fix" else {}, "carbon": carbon}


def _need(body: dict, key: str) -> str:
    value = str(body.get(key, "")).strip()
    if not value:
        raise ValueError(f"{key.replace('_', ' ')} is required")
    return value


def _list(value) -> list[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [v.strip() for v in str(value or "").replace("\n", ",").split(",") if v.strip()]


def make_handler(app: WebApp, port: int):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    classic = UI.read_text().replace("__ACK_TOKEN__", app.token)
    page = classic
    if (SITE / "index.html").is_file():
        meta = (f'<meta name="ack-token" content="{app.token}">'
                f'<meta name="ack-agent" content="{app.agent.name}">')
        page = (SITE / "index.html").read_text().replace("<head>", "<head>" + meta, 1)

    class Handler(BaseHTTPRequestHandler):
        server_version = "ack-web/0.1"
        app = None  # set below; lets callers (and tests) get from a server back to its app

        def log_message(self, *args):
            pass

        def _reply(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com; "
                             "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
                             "img-src 'self' data: blob:; connect-src 'self'; "
                             "frame-ancestors 'none'")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass  # the browser closed the tab or moved on; nothing to report

        def _asset(self, path: str) -> None:
            target = (SITE / path.lstrip("/")).resolve()
            if not target.is_file() or SITE.resolve() not in target.parents:
                return self._json(404, {"error": "not found"})
            self._reply(200, target.read_bytes(), ASSET_TYPES.get(target.suffix, "application/octet-stream"))

        def _json(self, status: int, obj) -> None:
            self._reply(status, json.dumps(obj).encode(), "application/json")

        def _guard(self) -> bool:
            if self.headers.get("Host") not in allowed_hosts:
                self._json(403, {"error": "bad host"})
                return False
            sent = self.headers.get("X-Ack-Token", "").encode("utf-8", "replace")
            if self.path.startswith("/api/") and not secrets.compare_digest(sent, app.token.encode()):
                self._json(403, {"error": "missing or wrong token; reload the page"})
                return False
            return True

        def do_GET(self):
            if not self._guard():
                return
            path = self.path.split("?", 1)[0]
            if path == "/":
                return self._reply(200, page.encode(), "text/html; charset=utf-8")
            if path == "/classic":
                return self._reply(200, classic.encode(), "text/html; charset=utf-8")
            if path.startswith("/assets/") or path in ("/favicon.svg", "/favicon.ico"):
                return self._asset(path)
            if path == "/api/status":
                return self._json(200, app.status())
            if path == "/api/tree":
                return self._json(200, {"files": app.tree()})
            self._json(404, {"error": "not found"})

        def do_POST(self):
            if not self._guard():
                return
            path = self.path.split("?", 1)[0]
            if not path.startswith("/api/"):
                return self._json(404, {"error": "not found"})
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(min(length, 1 << 20)) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError("expected a JSON object")
                result = app.act(path.removeprefix("/api/"), body)
                self._json(200, result if result is not None else {"ok": True})
            except (ValueError, KeyError, TypeError, BrokerError, UnknownPeer, CryptoError, BundleError,
                    RuntimeError, context.ContextError) as e:
                message = str(e) if not isinstance(e, KeyError) else f"missing field {e}"
                self._json(400, {"error": message})

    Handler.app = app
    return Handler


class _Server(ThreadingHTTPServer):
    request_queue_size = 64  # a page opens several requests at once; the default backlog is 5
    daemon_threads = True


def serve(app: WebApp, port: int, open_browser: bool = False, quiet: bool = False) -> ThreadingHTTPServer:
    """Serve the app on 127.0.0.1. port=0 picks a free port (server.server_address[1] says which)."""
    server = _Server(("127.0.0.1", port), BaseHTTPRequestHandler)
    port = server.server_address[1]
    server.RequestHandlerClass = make_handler(app, port)  # the Host check needs the real port
    app.start()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}/"
    if quiet:
        return server
    print(f"{app.agent.name}: {url}  (project {app.agent.root})")
    if open_browser:
        webbrowser.open(url)
    return server


def demo(port: int, open_browser: bool, pair: bool = True) -> list:
    """A broker on the Ack wire and four agents. beta and alpha get browser tabs; gamma and
    delta run headless as extra signatories.

    alpha, gamma and delta subscribe to beta's announcements (and beta to alpha's), so beta's
    "Submit a handoff" fans out to three signatories. By default beta is paired with all of
    them, so it can ask, request, and send encrypted announcements straight away. pair=False
    leaves pairing to you (Peers tab in each); open announcements still work.
    """
    from .broker import start_in_thread
    from .demo import ALPHA_FILES, BETA_FILES, BETA_PARTS, write

    ALPHA_EXTRA = {
        "webhooks/signing.py": """
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
        """,
        "webhooks/test_signing.py": """
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
        """,
    }

    work = Path(tempfile.mkdtemp(prefix="ack-web-demo-"))
    _, broker_url = start_in_thread(work / "broker", transport="wire")
    write(work / "beta", BETA_FILES)
    write(work / "alpha", {**ALPHA_FILES, **ALPHA_EXTRA})
    fast = 0.8  # demo agents poll often so the handoff plays out in a few seconds
    beta = WebApp("beta", work / "beta", broker_url, poll_every=fast, hints=[
        "what do you share?", "how do you sign outgoing webhooks?", "how do receivers verify a webhook signature?"])
    for entry in BETA_PARTS:
        beta.agent.share(Entry(**vars(entry)))
    alpha = WebApp("alpha", work / "alpha", broker_url, poll_every=fast, hints=[
        "what do you share?", "how do you retry flaky HTTP calls?", "do you have a helper for URL slugs?"])
    alpha.agent.share(Entry(
        id="webhook-signing", base="webhooks", files=["signing.py", "test_signing.py"],
        tests=["python", "-m", "unittest", "discover", "-s", ".", "-p", "test_*.py"],
        topic="sign and verify outgoing webhooks", tags=["webhook", "hmac", "security", "signature"],
        summary="sign(secret, body) and verify(secret, body, signature): HMAC-SHA256 webhook signatures",
        notes="Every webhook we send carries an X-Signature header: HMAC-SHA256 of the raw body with a per-customer "
              "secret. Receivers recompute it and compare in constant time, so a forged or edited payload is rejected. "
              "We include a timestamp in the signed text and refuse anything older than five minutes, to stop replays.",
        allow=["beta"]))
    signers = [WebApp(n, work / n, broker_url, poll_every=fast) for n in ("gamma", "delta")]
    for app in (beta, alpha, *signers):
        app.agent.register()
    for app in (alpha, *signers):
        app.agent.subscribe("beta")
    beta.agent.subscribe("alpha")
    if pair:
        # The same invite -> join -> PAIR exchange you'd do by hand, done once up front.
        for app in (alpha, *signers):
            app.agent.join(Invite.from_uri(beta.agent.invite().to_uri()))
        beta.agent.poll()
    for app in signers:
        app.start()  # headless: polls and signs, no page
    print(f"demo projects in {work}")
    print(f"(the broker runs at {broker_url}; it's for the agents, not your browser)")
    servers = [serve(beta, port, open_browser), serve(alpha, port + 1 if port else 0, open_browser)]
    for app in signers:
        servers.append(_Headless(app))
    if pair:
        print("beta is paired with alpha, gamma and delta. Each tab can ask the other; beta's "
              "'Submit a handoff' fans out to all three signatories.")
    else:
        print("Next: in beta's tab, Peers -> Create invite -> Copy link; in alpha's tab, Peers -> paste -> Join.")
    return servers


class _Headless:
    """Lets run_forever stop a page-less agent the same way it shuts down a server."""

    def __init__(self, app: WebApp):
        self.app = app

    def shutdown(self) -> None:
        self.app.stop()


def run_forever(servers: list) -> None:
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        for server in servers:
            server.shutdown()
