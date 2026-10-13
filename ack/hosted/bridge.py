"""A live agent that talks through the hosted feed.

It runs the protocol's own answer logic over the parts it shares (spelling fix, matching against
its memory, extractive summary), posts its answers to the shared history, and asks its peer a
question every so often. Run two of them, one per name, and the feed shows them talking.

    python -m ack.hosted.bridge --site https://ack01-xz1h.onrender.com --name bridge-alpha --token-file alpha.token --create
    python -m ack.hosted.bridge --site https://ack01-xz1h.onrender.com --name bridge-beta  --token-file beta.token  --create

Questions and answers are plain text on the feed. Nothing here is signed; the signed exchange is the
local protocol, which these agents do not use yet.
"""

import argparse
import sys
import tempfile
import time
from pathlib import Path

import httpx

from ack.memory import Entry, ProjectMemory
from ack.agent import MIN_MATCH, TOP_K
from ack.spelling import correct
from ack.summarize import ExtractiveSummarizer

# What each bridge shares, and the questions it asks its peer.
PARTS = {
    "bridge-alpha": [
        Entry(id="slugify", base="text", files=[], topic="turn titles into url slugs", tags=["text", "url"],
              summary="slugify(s): lowercase, runs of non-alphanumerics become '-'", allow=["*"]),
    ],
    "bridge-beta": [
        Entry(id="retry-backoff", base="retry", files=[], topic="retry flaky calls with exponential backoff",
              tags=["http", "resilience", "retries"],
              summary="retry(fn, attempts, base): calls fn, sleeping base*2^i between failures", allow=["*"]),
        Entry(id="geo-capitals", base="geo", files=[], topic="country capital lookup", tags=["geo"],
              summary="capitals(): dict of ISO country code to capital city", allow=["*"]),
    ],
}
# Each bridge asks its peer about the peer's own parts, so answers come back with real content.
QUESTIONS = {
    "bridge-alpha": ["how do I retry a flaky http call with backoff", "what is the capital of france"],
    "bridge-beta": ["how do I make a url slug from a title", "turn a title into a slug"],
}
PEER = {"bridge-alpha": "bridge-beta", "bridge-beta": "bridge-alpha"}


class Bridge:
    def __init__(self, site: str, name: str, token: str, every: float = 25.0):
        self.name, self.token, self.every = name, token, every
        self.peer = PEER[name]
        self.http = httpx.Client(base_url=site.rstrip("/"), timeout=30)
        self.memory = ProjectMemory(Path(tempfile.mkdtemp(prefix=f"ack-bridge-{name}-")))
        for part in PARTS[name]:
            self.memory.add(part)
        self.vocab = self.memory.vocabulary()
        self.summarizer = ExtractiveSummarizer()
        self.seen: set[int] = set()
        self.asked = 0
        self.next_question = 0.0

    def say(self, kind: str, body: str) -> None:
        res = self.http.post("/api/events", json={"kind": kind, "body": body[:2000]},
                             headers={"Authorization": f"Bearer {self.token}"})
        if res.status_code == 401:
            # The site was redeployed (its database is wiped on the free plan): register again.
            raise Reregister()
        res.raise_for_status()
        print(f"[{self.name}] {kind}: {body}", flush=True)

    def answer(self, question: str) -> str:
        fixed, _ = correct(question, self.vocab)
        hits = [(s, e) for s, e in self.memory.search(fixed, k=len(self.memory.entries)) if s >= MIN_MATCH][:TOP_K]
        if not hits:
            return "Nothing I share matches that."
        notes = [e for _, e in hits]
        return self.summarizer.summarize(fixed, notes)

    def tick(self) -> None:
        feed = self.http.get("/api/feed", params={"limit": 40}).json()["events"]
        for event in reversed(feed):  # oldest first
            if event["id"] in self.seen:
                continue
            self.seen.add(event["id"])
            if event["agent"] == self.name:
                continue
            body = event["body"]
            prefix = f"@{self.name} "
            if event["kind"] == "question" and body.startswith(prefix):
                self.say("answer", f"@{event['agent']} {self.answer(body[len(prefix):])}")
        if time.monotonic() >= self.next_question:
            questions = QUESTIONS[self.name]
            self.say("question", f"@{self.peer} {questions[self.asked % len(questions)]}")
            self.asked += 1
            self.next_question = time.monotonic() + self.every

    def run(self) -> None:
        shares = ", ".join(e.id for e in PARTS[self.name])
        self.say("note", f"online. I share: {shares}. Ask me a question with @{self.name} in front of it.")
        self.next_question = time.monotonic() + 5
        while True:
            try:
                self.tick()
            except Reregister:
                self.token = register(self.http, self.name)
                print(f"[{self.name}] registered again after a redeploy", flush=True)
            except (httpx.HTTPError, KeyError) as err:
                print(f"[{self.name}] retrying after: {err}", file=sys.stderr, flush=True)
            time.sleep(2)


class Reregister(Exception):
    pass


def register(http: httpx.Client, name: str) -> str:
    # A free host can take a minute to wake up, so give the first request time and retry.
    for attempt in range(1, 6):
        try:
            res = http.post("/api/agents", json={"name": name}, timeout=90)
            break
        except httpx.TimeoutException:
            print(f"[{name}] site is waking up (attempt {attempt}/5)", flush=True)
    else:
        raise SystemExit(f"{http.base_url} did not answer")
    if res.status_code != 201:
        raise SystemExit(f"could not create {name}: {res.text}")
    return res.json()["token"]


def create_or_load(site: str, name: str, token_file: Path, create: bool) -> str:
    if token_file.exists():
        return token_file.read_text().strip()
    if not create:
        raise SystemExit(f"no token in {token_file}; pass --create to register {name}")
    token = register(httpx.Client(base_url=site.rstrip("/"), timeout=90), name)
    token_file.write_text(token + "\n")
    return token


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--site", required=True)
    parser.add_argument("--name", required=True, choices=sorted(PARTS))
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--create", action="store_true", help="register the agent if the token file is missing")
    parser.add_argument("--every", type=float, default=25.0, help="seconds between questions")
    args = parser.parse_args()
    token = create_or_load(args.site, args.name, args.token_file, args.create)
    Bridge(args.site, args.name, token, args.every).run()


if __name__ == "__main__":
    main()
