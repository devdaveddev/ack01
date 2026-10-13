"""The web app over real HTTP: guards, then a full pair -> ask -> request -> approve -> install."""

import json
import time
import urllib.error
import urllib.request

import pytest

from ack import web
from ack.broker import start_in_thread
from ack.demo import ALPHA_FILES, BETA_FILES, BETA_PARTS, write
from ack.memory import Entry


class Client:
    def __init__(self, server, app):
        self.base = f"http://127.0.0.1:{server.server_address[1]}"
        self.token = app.token

    def call(self, path, body=None, token=True, host=None):
        req = urllib.request.Request(self.base + path, method="POST" if body is not None else "GET",
                                     data=json.dumps(body).encode() if body is not None else None)
        if token:
            req.add_header("X-Ack-Token", self.token)
        if host:
            req.add_header("Host", host)
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def api(self, action, body=None):
        status, data = self.call("/api/" + action, body)
        assert status == 200, data
        return json.loads(data)


def wait_for(fn, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        value = fn()
        if value:
            return value
        time.sleep(0.2)
    raise AssertionError("timed out")


@pytest.fixture
def apps(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "POLL_EVERY", 0.2)
    broker, url = start_in_thread(tmp_path / "broker")
    write(tmp_path / "beta", BETA_FILES)
    write(tmp_path / "alpha", ALPHA_FILES)
    beta_app, alpha_app = web.WebApp("beta", tmp_path / "beta", url), web.WebApp("alpha", tmp_path / "alpha", url)
    for entry in BETA_PARTS:
        beta_app.agent.share(Entry(**vars(entry)))
    servers = [web.serve(beta_app, 0, quiet=True), web.serve(alpha_app, 0, quiet=True)]
    yield Client(servers[0], beta_app), Client(servers[1], alpha_app), tmp_path
    for app in (beta_app, alpha_app):
        app.stop()
    for server in [*servers, broker]:
        server.shutdown()


def test_guards(apps):
    beta, _, _ = apps
    status, page = beta.call("/", token=False)
    assert status == 200 and beta.token.encode() in page  # the page carries the token
    assert beta.call("/api/status", token=False)[0] == 403  # other sites can't call the API
    assert beta.call("/", host="evil.example:80")[0] == 403  # DNS rebinding
    assert beta.call("/api/nope", {})[0] == 400


def test_full_flow_through_the_browser_api(apps):
    beta, alpha, tmp = apps
    uri = beta.api("invite", {})["uri"]
    alpha.api("join", {"uri": uri})
    wait_for(lambda: beta.api("status")["peers"])

    alpha.api("ask", {"peer": "beta", "text": "how do you retry flaky http calls?"})
    answers = wait_for(lambda: alpha.api("status")["answers"])
    assert answers[0]["sources"][0] == "retry-backoff"

    alpha.api("request", {"peer": "beta", "source_id": "retry-backoff", "reason": "webhook sender drops events",
                          "intent": "wrap delivery calls in retry()"})
    first = wait_for(lambda: alpha.api("status")["approvals"])[0]
    assert first["action"] == "run-tests" and "retry.py" in first["context"]["preview"]
    alpha.api("approve", {"id": first["id"], "approve": True})

    second = wait_for(lambda: [a for a in alpha.api("status")["approvals"] if a["action"] == "install"])[0]
    assert second["context"]["decision"]["action"] == "ADOPT"
    assert second["context"]["report"]["tests_passed"] is True
    alpha.api("approve", {"id": second["id"], "approve": True})

    installs = wait_for(lambda: alpha.api("status")["installs"])
    assert installs[0]["action"] == "ADOPT" and (tmp / "alpha" / "retry.py").exists()
    wait_for(lambda: beta.api("status")["parts"][0]["holders"])
    assert alpha.api("audit", {})["findings"] == []


def test_rejecting_in_the_browser_installs_nothing(apps):
    beta, alpha, tmp = apps
    alpha.api("join", {"uri": beta.api("invite", {})["uri"]})
    wait_for(lambda: beta.api("status")["peers"])
    alpha.api("request", {"peer": "beta", "source_id": "retry-backoff", "reason": "webhook sender drops events",
                          "intent": "wrap delivery calls in retry()"})
    first = wait_for(lambda: alpha.api("status")["approvals"])[0]
    alpha.api("approve", {"id": first["id"], "approve": False})
    wait_for(lambda: [p for p in alpha.api("status")["provenance"] if p.get("decision") == "REJECT"])
    assert not (tmp / "alpha" / "retry.py").exists()


def test_share_form_validates(apps):
    beta, _, _ = apps
    status, body = beta.call("/api/share", {"id": "x", "topic": "t", "summary": "s", "files": "nope.py"})
    assert status == 400 and b"not found" in body
    status, body = beta.call("/api/share", {"id": "../x", "topic": "t", "summary": "s", "base": "retry", "files": "retry.py"})
    assert status == 400
    assert beta.api("share", {"id": "geo2", "topic": "t", "summary": "s", "base": "geo", "files": "geo.py\ntest_geo.py",
                              "allow": "alpha"})["ok"]
    assert "geo/geo.py" in beta.api("tree")["files"]


def test_malformed_requests_get_clean_errors(apps):
    beta, _, _ = apps
    req = urllib.request.Request(beta.base + "/api/status", headers={"X-Ack-Token": "tökén"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == 403
    status, body = beta.call("/api/ask", ["not", "an", "object"])
    assert status == 400 and b"JSON object" in body


def test_invite_comes_with_a_server_drawn_qr(apps):
    beta, _, _ = apps
    r = beta.api("invite", {})
    assert r["uri"].startswith("ack://pair?") and r["svg"].startswith("<svg")


def stop_all(servers):
    for s in servers:
        if hasattr(s, "RequestHandlerClass"):
            s.RequestHandlerClass.app.stop()
        s.shutdown()


def test_demo_starts_paired_and_can_share_straight_away(monkeypatch):
    monkeypatch.setattr(web, "POLL_EVERY", 0.2)
    servers = web.demo(0, False)
    try:
        beta, alpha = (Client(s, s.RequestHandlerClass.app) for s in servers[:2])
        assert [p["name"] for p in alpha.api("status")["peers"]] == ["beta"]
        assert sorted(p["name"] for p in beta.api("status")["peers"]) == ["alpha", "delta", "gamma"]
        assert alpha.api("status")["hints"]
        alpha.api("ask", {"peer": "beta", "text": "how do you retry flaky HTTP calls?"})
        answers = wait_for(lambda: alpha.api("status")["answers"])
        assert "retry-backoff" in answers[0]["sources"]
    finally:
        stop_all(servers)


def test_context_packs_through_the_browser_api(apps, monkeypatch):
    from ack import context
    from tests.test_context import Fake, GOAL
    from http.server import ThreadingHTTPServer
    import threading as th
    fake = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    fake.head = None
    th.Thread(target=fake.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{fake.server_address[1]}"
    monkeypatch.setattr(context, "GITHUB_API", base)
    try:
        _, alpha, tmp = apps
        status, body = alpha.call("/api/context/start", {"goal": GOAL})
        assert status == 400 and b"at least one" in body
        status, body = alpha.call("/api/context/start", {"goal": GOAL, "github": "https://gitlab.com/x/y"})
        assert status == 400 and b"not a GitHub" in body

        alpha.api("context/start", {"goal": GOAL, "github": "https://github.com/o/r",
                                    "sites": base + "/docs/webhooks.html", "budget": "4000"})
        ctx = wait_for(lambda: (lambda c: c if c["state"] != "running" else None)(alpha.api("status")["context"]))
        assert ctx["state"] == "ready" and ctx["total"] >= 3 and not ctx["errors"]
        flagged = [i for i in ctx["items"] if i["flagged"]]
        assert flagged and not flagged[0]["selected"]
        top = ctx["items"][0]
        assert "text" not in top  # bulky text only comes with a preview
        assert alpha.api("context/preview", {"id": top["id"]})["text"]

        chosen = [i["id"] for i in ctx["items"] if i["selected"]]
        pack = alpha.api("context/build", {"ids": chosen, "name": "Webhook retries"})
        assert pack["items"] == len(chosen) and "## Prompt" in pack["markdown"]
        assert (tmp / "alpha" / ".ack" / "context" / "webhook-retries.md").exists()
        saved = alpha.api("status")["context"]["saved"]
        assert saved[0]["name"] == "Webhook retries"
        assert alpha.api("context/open", {"file": saved[0]["file"]})["prompt"].startswith("You're working in")
        assert {r["status"] for r in alpha.api("context/check", {"file": saved[0]["file"]})["results"]} == {"current"}
        # nothing was installed or shared: context packs never touch the agent's protocol state
        assert alpha.api("status")["installs"] == [] and alpha.api("status")["approvals"] == []
    finally:
        fake.shutdown()


def test_broker_address_explains_itself_in_a_browser(tmp_path):
    from ack.broker import start_in_thread
    server, url = start_in_thread(tmp_path / "b")
    try:
        with urllib.request.urlopen(url + "/") as r:
            assert r.status == 200 and b"This is an Ack broker" in r.read()
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(url + "/messages")
        assert e.value.code == 401  # the API itself still needs an agent's credentials
    finally:
        server.shutdown()


def test_demo_works_both_ways(monkeypatch):
    monkeypatch.setattr(web, "POLL_EVERY", 0.2)
    servers = web.demo(0, False)
    try:
        beta, alpha = (Client(s, s.RequestHandlerClass.app) for s in servers[:2])
        assert beta.api("status")["hints"]
        beta.api("ask", {"peer": "alpha", "text": "how do you sign outgoing webhooks?"})
        answer = wait_for(lambda: beta.api("status")["answers"])[0]
        assert answer["sources"] == ["webhook-signing"] and "HMAC" in answer["summary"]
        beta.api("request", {"peer": "alpha", "source_id": "webhook-signing", "reason": "our webhooks are unsigned",
                             "intent": "sign every outgoing webhook"})
        first = wait_for(lambda: beta.api("status")["approvals"])[0]
        assert first["action"] == "run-tests" and "signing.py" in first["context"]["preview"]
    finally:
        stop_all(servers)


def test_spelling_preview_and_exact(apps):
    beta, alpha, _ = apps
    r = alpha.api("spell", {"text": "how do you retry falky calls"})
    assert r["text"] == "how do you retry flaky calls" and r["changes"] == [["falky", "flaky"]]
    alpha.api("join", {"uri": beta.api("invite", {})["uri"]})
    wait_for(lambda: beta.api("status")["peers"])
    assert alpha.api("ask", {"peer": "beta", "text": "retry falky calls"})["changes"] == [["falky", "flaky"]]
    assert alpha.api("ask", {"peer": "beta", "text": "retry falky calls", "exact": "1"})["changes"] == []


def test_demo_handoff_fans_out_and_tallies(monkeypatch):
    servers = web.demo(0, False)
    try:
        beta, alpha = (Client(s, s.RequestHandlerClass.app) for s in servers[:2])
        st = beta.api("status")
        assert st["transport"] == "ack wire"
        assert wait_for(lambda: beta.api("status")["subscriptions"]["subscribers"]) == ["alpha", "delta", "gamma"]
        view = beta.api("handoff", {"tamper": True})
        assert view["statement"]["subscribers"] == ["alpha", "delta", "gamma"] and view["tampered"] == ["alpha"]
        aid = view["statement"]["id"]
        done = wait_for(lambda: (v := beta.api("handoff/status", {"id": aid}))["status"]["waiting"] == [] and v)
        assert done["status"]["state"] == "held" and done["votes"]["alpha"]["vote"]["verdict"] == "NAK"
        verbs = [e["action"] for e in done["carbon"]]
        assert verbs[:2] == ["ANNOUNCE", "FANOUT"] and "HELD" in verbs
        # alpha's own inbox shows what it refused and why.
        inbox = wait_for(lambda: alpha.api("status")["inbox"])
        assert inbox[0]["vote"]["verdict"] == "NAK" and not inbox[0]["check"]["match"]
        # Encrypted, untampered: sealed.
        aid = beta.api("handoff", {"encrypted": True})["statement"]["id"]
        done = wait_for(lambda: (v := beta.api("handoff/status", {"id": aid}))["status"]["state"] == "sealed" and v)
        assert done["statement"]["encrypted"] and done["status"]["acks"] == 3
    finally:
        stop_all(servers)


def test_announce_from_project_files_and_manual_signing(apps):
    beta, alpha, _ = apps
    alpha.api("subscribe", {"publisher": "beta"})
    alpha.api("sign-policy", {"policy": "manual"})
    rec = beta.api("announce", {"title": "notes", "paste": {"path": "notes/plan.md", "content": "ship it\n"}})
    assert rec["status"]["waiting"] == ["alpha"]
    item = wait_for(lambda: alpha.api("status")["inbox"])[0]
    assert item["check"]["match"] and item["vote"] is None and item["files"] == {"notes/plan.md": "ship it\n"}
    assert alpha.api("vote", {"id": rec["id"], "verdict": "ACK", "reason": "read it"})["status"]["state"] == "sealed"
    assert beta.call("/api/announce", {"title": "x", "files": ["../etc/passwd"]})[0] == 400
