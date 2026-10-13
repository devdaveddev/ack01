"""End-to-end demo: two agents, one broker, every branch of the flow. No API key needed.

  beta  owns a small library repo and shares parts of it.
  alpha asks about it, requests parts, and adopts, adapts or rejects them.
"""

import tempfile
import textwrap
from collections import Counter
from pathlib import Path

from .agent import Agent
from .broker import start_in_thread
from .memory import Entry
from .pairing import Invite, render_qr

TESTS = ["python", "-m", "unittest", "discover", "-s", ".", "-p", "test_*.py"]

RETRY_V1 = """
    import time

    def retry(fn, attempts=3, base=0.01):
        for i in range(attempts):
            try:
                return fn()
            except Exception:
                if i == attempts - 1:
                    raise
                time.sleep(base * 2 ** i)
"""

RETRY_V2 = """
    import random
    import time

    def retry(fn, attempts=3, base=0.01):
        for i in range(attempts):
            try:
                return fn()
            except Exception:
                if i == attempts - 1:
                    raise
                time.sleep(base * 2 ** i * (1 + random.random()))  # jitter
"""

BETA_FILES = {
    "retry/retry.py": RETRY_V1,
    "retry/test_retry.py": """
        import unittest
        from retry import retry

        class T(unittest.TestCase):
            def test_recovers(self):
                calls = []
                def flaky():
                    calls.append(1)
                    if len(calls) < 3:
                        raise IOError("boom")
                    return "ok"
                self.assertEqual(retry(flaky), "ok")
    """,
    "text/slugify.py": """
        import re

        def slugify(s):
            return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    """,
    "text/test_slugify.py": """
        import unittest
        from slugify import slugify

        class T(unittest.TestCase):
            def test_basic(self):
                self.assertEqual(slugify("Hello, World!"), "hello-world")
    """,
    # The test needs capitals.json, which isn't in the shared file list: it passes
    # for beta and fails in alpha's sandbox. A realistic "forgot a dependency" bundle.
    "geo/geo.py": """
        import json, pathlib

        def capitals():
            return json.loads(pathlib.Path("capitals.json").read_text())
    """,
    "geo/test_geo.py": """
        import unittest
        from geo import capitals

        class T(unittest.TestCase):
            def test_load(self):
                self.assertEqual(capitals()["FR"], "Paris")
    """,
    "geo/capitals.json": '{"FR": "Paris"}\n',
    "billing/keys.py": "API_KEY = 'not for sharing'\n",
}

ALPHA_FILES = {
    "slugify.py": "def slugify(s):\n    return s.replace(' ', '_')  # alpha's own version\n",
}

BETA_PARTS = [
    Entry(id="retry-backoff", base="retry", files=["retry.py", "test_retry.py"], tests=TESTS,
          topic="retry flaky calls with exponential backoff", tags=["http", "resilience", "retries"],
          summary="retry(fn, attempts, base): calls fn, sleeping base*2^i between failures", allow=["alpha"],
          notes="We wrap every outbound HTTP call in retry(). It retries on any exception, three attempts by "
                "default. Backoff is exponential from 10ms. It does not distinguish 4xx from 5xx, so callers "
                "should raise only on retryable errors. Used by the webhook and billing clients."),
    Entry(id="slugify", base="text", files=["slugify.py", "test_slugify.py"], tests=TESTS,
          topic="turn titles into url slugs", tags=["text", "url"],
          summary="slugify(s): lowercase, runs of non-alphanumerics become '-'", allow=["alpha"],
          notes="Used for blog and docs URLs. ASCII only: accented letters are dropped, not transliterated. "
                "Callers must dedupe slugs themselves."),
    Entry(id="geo-capitals", base="geo", files=["geo.py", "test_geo.py"], tests=TESTS,
          topic="country capital lookup", tags=["geo"],
          summary="capitals(): dict of ISO country code to capital city", allow=["alpha"],
          notes="Data is a static snapshot, refreshed by hand each quarter. Keys are two-letter ISO 3166 codes."),
    Entry(id="billing-keys", base="billing", files=["keys.py"], tests=TESTS,
          topic="billing api keys", tags=["billing", "secrets"],
          summary="production billing credentials", allow=[]),
]


def write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text).lstrip())


def pump(*agents: Agent, rounds: int = 20) -> None:
    """Let agents handle messages until nobody has anything left."""
    for _ in range(rounds):
        if sum(a.poll() for a in agents) == 0:
            return


def step(title: str) -> None:
    print(f"\n=== {title} ===")


def approve(action: str, detail: str) -> bool:
    print(f"  [approval] {action}: {detail} -> yes (demo auto-approves)")
    return True


def run(workdir: Path | None = None) -> list[tuple[str, str]]:
    """Runs the whole story. Returns alpha's (source_id, decision) list."""
    tmp = None
    if workdir is None:
        tmp = tempfile.TemporaryDirectory(prefix="ack-demo-")
        workdir = Path(tmp.name)
    server, url = start_in_thread(workdir / "broker")
    try:
        return _story(workdir, url)
    finally:
        server.shutdown()
        if tmp:
            tmp.cleanup()


def _story(workdir: Path, url: str) -> list[tuple[str, str]]:
    print(f"broker at {url}, projects in {workdir}")
    write(workdir / "beta", BETA_FILES)
    write(workdir / "alpha", ALPHA_FILES)
    beta = Agent("beta", workdir / "beta", url)
    alpha = Agent("alpha", workdir / "alpha", url, approver=approve)
    for agent in (alpha, beta):
        agent.register()
    for entry in BETA_PARTS:
        beta.share(Entry(**vars(entry)))

    step("QR pairing: beta shows an invite, alpha scans it")
    uri = beta.invite().to_uri()
    print(render_qr(uri))
    alpha.join(Invite.from_uri(uri))
    pump(alpha, beta)

    step("1-2. Query and quick answer")
    question = "how do I retry flaky HTTP calls with backoff?"
    alpha.ask("beta", question)
    pump(alpha, beta)
    print("  answer:", alpha.last_answer("beta", question))
    alpha.ask("beta", question)  # alpha's own cache: no message sent
    alpha.ask("beta", "how to retry flaky http calls using backoff")  # new wording, beta's summary cache
    pump(alpha, beta)

    step("3-4. Requests that don't pass the owner's check")
    alpha.request("beta", "billing-keys", "we also need to bill customers", "call the billing API directly")
    alpha.request("beta", "does-not-exist", "curious whether this exists", "read it for reference")
    pump(alpha, beta)

    step("3-9. Full request: check, offer, bundle, assess, ADOPT, ack")
    alpha.request("beta", "retry-backoff", "our webhook sender drops events on transient errors",
                  "wrap webhook delivery calls in retry()")
    pump(alpha, beta)

    step("UPDATE_AVAILABLE: beta changes retry.py, sends a diff; alpha pulls only the changed layer")
    write(workdir / "beta", {"retry/retry.py": RETRY_V2})
    beta.publish_update("retry-backoff")
    alpha.poll()  # UPDATE_AVAILABLE arrives
    print("  diff beta sent:")
    print(textwrap.indent(alpha.state["updates"]["beta/retry-backoff"]["diff"], "    "))
    pump(alpha, beta)

    step("ADAPT: alpha already has its own slugify.py")
    alpha.request("beta", "slugify", "need consistent URL slugs across both projects",
                  "replace our slugify once callers are migrated")
    pump(alpha, beta)

    step("REJECT: bundle is missing a file its tests need")
    alpha.request("beta", "geo-capitals", "show capital cities on the dashboard", "import capitals() in the UI")
    pump(alpha, beta)

    step("STALE: beta edited geo.py without recording a new version")
    write(workdir / "beta", {"geo/geo.py": BETA_FILES["geo/geo.py"] + "\n# wip\n"})
    alpha.request("beta", "geo-capitals", "trying again after the fixture fix", "import capitals() in the UI")
    pump(alpha, beta)

    step("Provenance trail (alpha)")
    decisions = []
    for rec in alpha.provenance():
        decisions.append((rec["source_id"], rec["decision"]))
        print(f"  {rec['source_id']} v{rec['version']} {rec['decision']}: {rec['reason']}")

    step("Broker view: who holds what, and the audit log")
    for h in beta.broker.holdings():
        print(f"  {h['holder']:>5} holds {h['owner']}/{h['source_id']} v{h['version']} ({h['status']})")
    counts = Counter(e["action"] if e["action"] != "message" else f"message:{e['type']}"
                     for e in beta.broker.audit())
    print("  audit:", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    print("\nalpha's project now contains:",
          sorted(str(p.relative_to(workdir / "alpha")) for p in (workdir / "alpha").rglob("*")
                 if p.is_file() and ".ack" not in p.parts))
    return decisions


if __name__ == "__main__":
    run()
