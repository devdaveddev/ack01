# 13 · `ack/web.py` and `ack/webui.html`, the web app

**Job of these files:** give one agent a browser interface. `web.py` is a small local web server wrapped around an `Agent`. It polls the broker in the background, turns approvals into cards you click, and exposes a JSON API. `webui.html` is the page: one file of HTML, CSS and JavaScript, with no build step.

```
browser (webui.html) ──fetch /api/...──►  web.py Handler ──► WebApp.act / status ──► Agent
        ▲                                                      │
        └────────── every 2 s: GET /api/status ◄───────────────┘
                                     background thread: WebApp.poll() ──► Agent.poll() ──► broker
```

`web.py` is explained line by line below. The page is explained by function, because most of it is layout. The parts that matter for safety are covered line by line.

Primer references: [classes](00-python-primer.md#classes-and-objects), [with](00-python-primer.md#with-context-managers), [closures](00-python-primer.md#functions).

---

# Part 1: `web.py`

## Lines 1–20 · docstring

How to run it, how the pieces fit, and the three security measures: a per-run token, a Host check, and text-only rendering.

## Lines 22–49 · imports and settings

```python
APPROVAL_TIMEOUT = 15 * 60
POLL_EVERY = 2.0
MAX_EVENTS = 200
MAX_TREE = 2000
SKIP_DIRS = {"__pycache__", "vendor", "node_modules", "venv", "dist", "build"}
UI = Path(__file__).with_name("webui.html")
```

- **26** `shlex`, to split the test command the way a shell would.
- **30** `uuid`, for an ID per approval card.
- **31** `webbrowser`, to open the page when `--open` is given.
- **44** An approval nobody answers is refused after 15 minutes.
- **45** Poll the broker every 2 seconds.
- **46–47** Caps on how much the page is sent: the latest 200 events, and at most 2000 files in the file picker.
- **48** Folders the file picker never walks into.
- **49** The page file, found next to this module (`__file__` is this file's own path).

## Lines 52–59 · the app object

```python
class WebApp:
    def __init__(self, name: str, root: Path, broker_url: str, judge=None):
        self.cond = threading.Condition()
        self.pending: dict[str, dict] = {}
        self.broker_error = ""
        self.agent = Agent(name, root, broker_url, judge=judge, approver=self._approve, log=None)
        self.token = secrets.token_urlsafe(24)
        self._stop = threading.Event()
```

- **54** A **Condition**: a lock that threads can also *wait* on until something changes. Every use of the agent happens while holding it, because the agent isn't thread-safe and several threads use it here (the poller and one per browser request).
- **55** Approval requests waiting for a click: ID → request.
- **56** The last broker error, shown in the header pill.
- **57** The agent. Its approver is **our** `_approve` method, and `log=None` because the page shows events itself.
- **58** A random secret for this run. It's baked into the page, and every API call must send it back (see `_guard`).
- **59** A flag to stop the poller.

## Lines 63–71 · approve: wait for a click

```python
    def _approve(self, action: str, detail: str) -> bool:
        req = {"id": uuid.uuid4().hex, "action": action, "detail": detail, "created": time.time(),
               "context": self._context(action), "answer": None}
        self.pending[req["id"]] = req
        self.cond.notify_all()
        answered = self.cond.wait_for(lambda: req["answer"] is not None, timeout=APPROVAL_TIMEOUT)
        self.pending.pop(req["id"], None)
        return bool(req["answer"]) if answered else False
```

The agent calls this from inside `poll()` → `_receive`, on the poller thread, while the lock is held.

- **65–66** Build an approval request: an ID, what's being asked, and the full **context** (files, previews, report). `answer` starts as `None`, meaning not decided yet.
- **67** Put it where `status()` will show it, so it appears as a card in the browser.
- **68** Wake anyone waiting on the condition.
- **69** **Wait** until `answer` is set or 15 minutes pass. The key detail: `wait_for` **releases the lock while it waits** and takes it back before returning. So while you read the card, the page keeps working: status refreshes, asking questions, sharing parts.
- **70** Remove the card.
- **71** Your answer, or **no** if it timed out. Silence never approves anything.

## Lines 73–85 · what the card shows

```python
    def _context(self, action: str) -> dict:
        ctx = self.agent.approval_context
        manifest = ctx.get("manifest") or {}
        out = {"peer": ..., "source_id": ..., "version": ..., "manifest_hash": ..., "intent": ...,
               "fetched": ..., "tests": ..., "deps": ...,
               "files": [{"path": f["path"], "size": f["size"]} for f in manifest.get("files", [])]}
        if action == "run-tests" and ctx.get("qdir"):
            out["preview"] = quarantine.preview(manifest, Path(ctx["qdir"]))
        if action == "install" and ctx.get("report"):
            out["report"] = dataclasses.asdict(ctx["report"])
            out["decision"] = dataclasses.asdict(ctx["decision"])
        return out
```

- **74** What the agent says it's asking about (set in `_receive`; see page 10).
- **76–79** The basics: who, which part and version, the manifest, your intent, how many files were downloaded, the test command, dependencies, and the file list. (Condensed above.)
- **80–81** Before running tests, include **the start of every file** from quarantine, so you can read the code before letting it run.
- **82–84** Before installing, include the full assessment report and the judge's decision. `dataclasses.asdict` turns those objects into plain dicts that can become JSON.

## Lines 87–93 · record your click

```python
    def answer(self, approval_id: str, approve: bool) -> None:
        with self.cond:
            req = self.pending.get(approval_id)
            if req is None:
                raise ValueError("that approval is no longer pending")
            req["answer"] = bool(approve)
            self.cond.notify_all()
```

- **88** Take the lock. It's free, because `_approve` is waiting and released it.
- **89–91** The card may have timed out already.
- **92** Record yes or no.
- **93** Wake the waiting `_approve`. Its condition is now true, so it continues and the receive goes on.

## Lines 97–126 · background polling

```python
    def start(self) -> None:
        try:
            with self.cond:
                self.agent.register()
        except BrokerError as e:
            self.broker_error = str(e)
        threading.Thread(target=self._poll_loop, daemon=True).start()

    def _poll_loop(self) -> None:
        while not self._stop.wait(POLL_EVERY):
            self.poll()
```

- **98–102** Register with the broker. If it's down, remember the error (it shows in the header) and carry on. `poll` retries registration later.
- **103** Start the poller on its own thread. `daemon=True` means it won't keep the program alive.
- **106** `wait(2.0)` sleeps 2 seconds, or returns `True` early if `stop()` was called. That makes it both the timer and the off switch.

```python
    def poll(self) -> int:
        try:
            with self.cond:
                if self.pending:
                    return 0
                if not self.agent.broker.secret:
                    self.agent.register()
                n = self.agent.poll()
            self.broker_error = ""
            return n
        except BrokerError as e:
            self.broker_error = str(e)
            return 0
```

- **112–115** **While an approval is waiting, don't poll.** The waiting `_approve` released the lock, so a second poll could start another receive alongside the paused one, and the two would interfere. New messages simply wait at the broker until you answer.
- **116–117** Register if we couldn't at start-up.
- **118** Handle everything waiting.
- **119** It worked, so clear any old error.
- **121–123** The broker is unreachable: record that, and try again in 2 seconds.

## Lines 130–158 · status: everything the page shows

```python
    def status(self) -> dict:
        with self.cond:
            a = self.agent
            st = a.state
            parts = [...]
            answers = [{"peer": k.split("|", 1)[0], "question": k.split("|", 1)[1], **v}
                       for k, v in st["answers"].items()]
            snapshot = {"me": {...}, "peers": [...], "open_invites": ..., "parts": parts, "installs": ...,
                        "updates": ..., "answers": ..., "waiting_answers": ..., "requests": ...,
                        "approvals": [{k: v for k, v in r.items() if k != "answer"} for r in self.pending.values()],
                        "events": [...][::-1], "provenance": a.provenance()[-50:][::-1]}
        return json.loads(json.dumps(snapshot, default=str))
```

- **131** Read everything under the lock, so it's a consistent picture.
- **134–139** Each shared part, including whether its files still match the recorded version (`current`), and who holds which version.
- **140–141** Cached answers. The cache key is `"peer|question"`, and `split("|", 1)` splits at the first `|` only, recovering both halves.
- **142–157** One dict with everything. Notable entries:
  - `fingerprint(...)`: short key IDs you can compare with a peer out loud
  - `approvals`: pending cards, without their internal `answer` field
  - `[::-1]`: reverse the lists, so the newest comes first
- **158** Round-trip through JSON: this makes a **detached copy** (nothing in it is shared with the live agent state) and turns anything unusual into text (`default=str`).

## Lines 160–171 · the file picker's list

```python
        root, out = self.agent.root, []
        for folder, dirs, files in os.walk(root):
            dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in SKIP_DIRS)
            for name in sorted(files):
                if not name.startswith("."):
                    out.append(str((Path(folder) / name).relative_to(root)))
                    if len(out) >= MAX_TREE:
                        return out
        return out
```

- **164** `os.walk` visits each folder, giving the subfolder names and file names.
- **165** **Pruning:** assigning to `dirs[:]` (the slice, not the variable) changes the list `os.walk` is about to visit, so skipped folders are **never entered**. Hidden folders, `vendor/` and `node_modules/` cost nothing, however big they are.
- **166–170** Collect non-hidden files as paths relative to the project, and stop at 2000.

## Lines 175–214 · act: every button and form

```python
    def act(self, action: str, body: dict):
        a = self.agent
        if action == "approve":
            return self.answer(body["id"], body["approve"])
        if action == "poll":
            return {"handled": self.poll()}
        with self.cond:
            if action == "invite": ...
            if action == "join": ...
            if action == "ask": ...
            if action == "request": ...
            if action == "share": ...
            if action == "unshare": ...
            if action == "update": ...
            if action == "audit": ...
        raise ValueError(f"unknown action {action!r}")
```

- **177–180** `approve` and `poll` take the lock themselves, so they're handled before the `with`.
- **181** Everything else runs under the lock. Each branch is one agent call:
  - **invite**: returns the `ack://` link, which the page turns into a QR code
  - **join**: `.strip()` removes stray spaces or newlines from a pasted link
  - **ask**: returns a cached answer, or `None` ("sent")
  - **request**: all three text fields are required
  - **share** (lines 193–205):
    - **194** `shlex.split` turns `python -m unittest discover -s . -p "test_*.py"` into a list, respecting quotes. An empty string gives `[]`, so `or None` means "no tests".
    - **195–198** Build the `Entry` from the form. `_list` accepts commas or new lines. `.strip("/")` removes a trailing slash from the folder.
    - **199–203** Refuse with a clear message if no files were picked, or a file doesn't exist in that folder.
    - **204** Share it. This also validates the id and paths, raising readable errors.
  - **unshare**: remove the part from memory. Peers who already have it keep their copy.
  - **update**: record a new version and notify holders; returns how many were notified
  - **audit**: run the audit; `asdict` makes each finding JSON-ready
- **214** Anything else → error.

## Lines 217–227 · form helpers

```python
def _need(body: dict, key: str) -> str:
    value = str(body.get(key, "")).strip()
    if not value:
        raise ValueError(f"{key.replace('_', ' ')} is required")
    return value


def _list(value) -> list[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [v.strip() for v in str(value or "").replace("\n", ",").split(",") if v.strip()]
```

- **217–221** A required text field: missing or blank → `"source id is required"`. `replace('_', ' ')` makes the field name readable.
- **224–227** A list field: accepts a real list, or text separated by commas or new lines. Blank items are dropped.

## Lines 230–298 · the HTTP handler

```python
def make_handler(app: WebApp, port: int):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    page = UI.read_text().replace("__ACK_TOKEN__", app.token)

    class Handler(BaseHTTPRequestHandler):
        ...
    return Handler
```

- **230** A function that **builds a handler class** around this app and port. The server creates one handler per request, so we can't pass arguments to it; instead the class **closes over** `app`, `allowed_hosts` and `page`.
- **231** The only valid `Host` header values.
- **232** Load the page once and put this run's token into it.

```python
        def _reply(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com; ...")
            self.end_headers()
            self.wfile.write(body)
```

Browser safety headers on every response:

- **244** `no-store`: don't cache the token-bearing page or the status data.
- **245** `nosniff`: the browser must trust our `Content-Type`, not guess it.
- **246** `no-referrer`: never leak this local URL to other sites.
- **247** `X-Frame-Options: DENY`: no other site may load this page in a frame. That blocks "clickjacking", where a hidden frame tricks you into clicking Approve.
- **248–251** Content Security Policy:
  - scripts only from this page and cdnjs (where the QR library comes from)
  - network requests only back to this server
  - images only from here or `data:` URLs
  - never framed

  Even if something slipped into the page, it couldn't load code from elsewhere or send data out.

```python
        def _guard(self) -> bool:
            if self.headers.get("Host") not in allowed_hosts:
                self._json(403, {"error": "bad host"})
                return False
            sent = self.headers.get("X-Ack-Token", "").encode("utf-8", "replace")
            if self.path.startswith("/api/") and not secrets.compare_digest(sent, app.token.encode()):
                self._json(403, {"error": "missing or wrong token; reload the page"})
                return False
            return True
```

Two checks run before every request:

- **259–261** **The Host check.** A malicious website can point its own domain name at `127.0.0.1` (DNS rebinding) so that your browser treats this local server as "same site". The `Host` header then contains *their* domain, and we refuse.
- **262–265** **The token check**, for every `/api/` call. Other websites open in your browser can send requests to `localhost`, but they can't read this page, so they never learn the token. `compare_digest` compares in constant time. Both sides are bytes, so an odd non-ASCII header gets a clean 403 instead of crashing the request.

```python
        def do_GET(self):
            if not self._guard():
                return
            path = self.path.split("?", 1)[0]
            if path == "/":
                return self._reply(200, page.encode(), "text/html; charset=utf-8")
            if path == "/api/status":
                return self._json(200, app.status())
            if path == "/api/tree":
                return self._json(200, {"files": app.tree()})
            self._json(404, {"error": "not found"})
```

- **268–278** GET serves the page, the status and the file list. Everything else is 404.

```python
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
                    RuntimeError) as e:
                message = str(e) if not isinstance(e, KeyError) else f"missing field {e}"
                self._json(400, {"error": message})
```

- **287–288** Read at most 1 MB of body (`1 << 20`) and parse it.
- **289–290** It must be a JSON object.
- **291** `/api/ask` → `app.act("ask", body)`.
- **292** Reply with the result, or `{"ok": true}` for actions that return nothing.
- **293–296** Any expected error becomes a 400 with a readable message, which the page shows as a red toast. `KeyError`'s own text is just the key name, so it gets a clearer wording.

## Lines 301–314 · serve

```python
def serve(app: WebApp, port: int, open_browser: bool = False, quiet: bool = False) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), BaseHTTPRequestHandler)
    port = server.server_address[1]
    server.RequestHandlerClass = make_handler(app, port)
    app.start()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    ...
```

- **303** Listen on **127.0.0.1 only**, so other machines on your network can't reach it. A placeholder handler is used for the moment.
- **304–305** With `port=0` the operating system picks a free port. Only now do we know it, so the real handler (whose Host check needs the port) is built and swapped in **before** the server starts accepting requests.
- **306** Register and start polling.
- **307** Serve requests on a background thread.
- **308–314** Print the URL and optionally open a browser. Tests pass `quiet=True`.

## Lines 317–333 · demo mode

```python
    work = Path(tempfile.mkdtemp(prefix="ack-web-demo-"))
    _, broker_url = start_in_thread(work / "broker")
    write(work / "beta", BETA_FILES)
    write(work / "alpha", ALPHA_FILES)
    beta = WebApp("beta", work / "beta", broker_url)
    for entry in BETA_PARTS:
        beta.agent.share(Entry(**vars(entry)))
    alpha = WebApp("alpha", work / "alpha", broker_url)
    servers = [serve(beta, port, open_browser), serve(alpha, port + 1, open_browser)]
```

The same fixtures as `python -m ack demo`, but **nothing is automated**: you pair, ask, request and approve yourself, in two browser tabs (ports 8800 and 8801 by default). It's the quickest way to understand the protocol by using it.

## Lines 336–342 · run until Ctrl-C

`time.sleep(3600)` in a loop keeps the main thread alive (the servers run on background threads). Ctrl-C raises `KeyboardInterrupt`, which shuts each server down.

---

# Part 2: `webui.html`

The page is one file: CSS, HTML and JavaScript, no build step. It follows the "Manifest" design on the design canvas. Line numbers are left out here because the page is mostly layout; the parts that matter for safety are quoted in full.

## Structure

- **CSS tokens** (`:root`): colours for light mode, redefined under `prefers-color-scheme: dark`, plus three font stacks (Bricolage Grotesque, IBM Plex Sans, IBM Plex Mono). Without internet the system fonts are used, and nothing breaks.
- **One global rule matters more than it looks:** `[hidden] { display: none !important; }`. Without it, a layout class like `.stack { display: flex }` overrides the browser's own `hidden` behaviour. That bug once left the "not paired yet" card showing for an agent that was already paired.
- **The shell** is a sidebar and a main area. The sidebar holds your name, key fingerprint and project, the icon navigation (with an approvals count and a NEW tag on Context packs), and the broker status. At 900px and wider the sidebar sticks in place; narrower, it becomes a top bar whose navigation scrolls sideways.
- **One `<section class="view">` per tab.** `showTab()` shows the one named in the URL's `#hash` and scrolls to the top, because the tabs are views, not anchors to jump to.

## The safety rule: `el()`

```js
function el(tag, props, ...kids) {
  const n = document.createElement(tag);
  ...
  for (const kid of kids.flat(Infinity)) {
    if (kid == null || kid === false) continue;
    n.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return n;
}
```

Every string becomes a **text node**, so summaries, diffs, file contents, reasons and part names from another agent are displayed, never parsed as HTML. Icons are drawn by `icon(name)` with `createElementNS`, from a fixed table of our own path shapes, never from data. The only `innerHTML` is the invite QR code's SVG, which the server draws from our own invite link.

## Each view

- **Home** (`renderHome`):
  - a "Needs you" queue, listing each pending approval with a Review button
  - a quick "Ask a peer" form
  - the latest answer, with "Request this part"
  - recent decisions
  - the pairing card while you have no peers
- **Ask & request** (`renderAsk`): question and request forms, one-click example questions, and answer cards whose sources fill in the request form.
- **Approvals** (`drawApproval`): one focused review at a time. Polling pauses while an approval waits, so there's normally only one.
  - The heading, decision tag, and a countdown pill (`data-deadline`, updated by `tickTimers()` without re-rendering).
  - Progress steps: Checked → Run tests? → Install?.
  - A code panel with one tab per file and a **Changes** tab for updates, all with line numbers (`codeView`).
  - A checks list built from the report: signature, hashes, dependencies, tests, conflicts.
  - "What happens if you install" in plain words.
  - Two large buttons whose text says exactly what will happen.
- **Installed** and **My parts:** cards per install and per shared part, and the share form with the project file picker (`loadTree`).
- **Context packs:** three steps, tracked in `ctxStep`. Moving to a new step scrolls to the top.
  1. **Sources:** the form, a pack-size slider, how fetched content is handled, and saved packs.
  2. **Review:** the token meter, filters (All, GitHub, Website, Flagged), and the ranked items with checkboxes and a read-only preview.
  3. **Pack:** the pack text, plus "Use it" (copy prompt, copy pack, download), "Keeping it fresh" and "Sources and licenses".

  New results start from Ack's pre-selection. Your ticks then live in the page (`ctxSel`), so the 2-second refresh doesn't undo them.
- **Peers** (`renderPeers`): the invite card (QR, link, Copy, New invite), the join box, and paired agents. Each fingerprint is shown in 4-character groups to read aloud, with "Mark as checked". The server remembers a check only for the exact key you compared, so if a peer's key ever changed, it would show as unchecked again.
- **Activity** and **Audit:** the decisions log, the event log, and the audit results table.

## Refresh

`refresh()` fetches `/api/status` every 2 seconds and calls `render()`. Each block redraws only when its own data changed (`changed(key, value)`), so open details, scroll positions, half-typed forms and your context-pack ticks survive.
