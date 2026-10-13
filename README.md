# Ack

A protocol for AI agents on different projects to share work safely, over its own wire (`ack://`, not HTTP). It has two ways to hand off:

- **Paired, one to one.** One agent asks, the owner verifies and ships a signed, encrypted bundle through a broker, and the receiver quarantines it, tests it in a sandbox, and decides to adopt, adapt or reject.
- **Announced, one to many.** A publisher signs a statement ("this change, this digest, these signatories, this quorum"). The broker fans a copy out to every subscriber: one open copy, or one sealed copy per subscriber. Each subscriber recomputes the digest on the copy it actually received and signs ACK or NAK. The broker counts the signed votes: **sealed** at quorum, **held** once the quorum can't be reached.

**Start with [docs/EXPLAINED.md](docs/EXPLAINED.md)** (what and why), then [docs/walkthrough/](docs/walkthrough/README.md) (every line and its syntax): what each part does, why it's built that way, the library choices, what's novel, and the full user lifecycle.

This first build runs with no API key. The judgment calls are rule-based stand-ins that an LLM can replace later.

```sh
pip install -e '.[dev,qr]'
(cd site && npm install && npm run build)   # the web frontend; without it you get the classic page
python -m ack web --demo --open   # broker on the Ack wire; beta and alpha in two tabs, gamma and delta signing headless
python -m ack demo      # two agents + broker, every branch of the paired flow, in the terminal
python -m ack research  # the three research experiments (add --llm for Claude judges)
pytest                  # 97 tests
```

In the demo, open beta's tab (`http://127.0.0.1:8800/`) and press **Submit a handoff**. That is a real announcement: beta signs it, the broker fans it out over the Ack wire to alpha, gamma and delta, each one checks its copy and signs, and the carbon copy on the right is the broker's own audit log. Press **Alter alpha's copy** first and alpha refuses, so the handoff is held. **Open copy / Copies sealed** switches between one plain copy and one copy per subscriber encrypted with that pair's key. The **Console** link has everything else.

## The Ack wire

Agents talk to the broker over one TCP connection, in frames:

```
"ACK1"  uint32 header length  uint32 body length  JSON header  body
```

A request header is `{"op", "path", "agent", "auth"}`; a reply header is `{"status", "raw", "ctype"}`. Bodies are JSON for operations and raw bytes for blobs. `ack/wire.py` is the whole framing (70 lines). The broker serves the wire on port 7468 and, if you ask, HTTP too (`--http-port 8765`) from the same state; agents pick the transport from the URL scheme (`ack://` or `http://`).

## Announcements: fan out, sign, quorum

```sh
python -m ack agent --root ~/b --name beta  register
python -m ack agent --root ~/a --name alpha register
python -m ack agent --root ~/a --name alpha subscribe beta
python -m ack agent --root ~/b --name beta  announce "retry fix" --files billing/charge.py [--encrypted] [--quorum 2]
python -m ack agent --root ~/a --name alpha announcements           # polls, checks, signs (auto policy)
python -m ack agent --root ~/b --name beta  announcements           # sealed / held / pending with the tally
python -m ack agent --root ~/a --name alpha policy manual           # then: sign ID ACK|NAK "reason"
```

What each side checks:

- **Subscriber:** the publisher's Ed25519 signature over the statement; then fetches its copy, decrypts it if sealed (AES-GCM, the announcement id as associated data), and compares SHA-256 against the announced digest. A failed check is always a NAK. With the `auto` policy a passing check is an ACK; with `manual` it waits for you. The files are shown as text and never written into the project or run.
- **Broker:** that the statement is signed by the publisher, that every signatory subscribed to the publisher, that the copies were uploaded, and that each vote is signed by its signer, for this announcement, once. It records ANNOUNCE, FANOUT, every ACK/NAK and the SEALED/HELD moment in the audit log.
- **Publisher:** re-verifies every forwarded vote signature against the signer's key.

## Context packs: learn from a GitHub repo or a website

Point your agent at public code or docs, say what you're building, and get back a context pack: a Markdown file with a ready prompt, key points, and the relevant excerpts, each cited and pinned to a commit or fetch time.

```sh
.venv/bin/python -m ack context --goal "add retries with backoff to our webhook sender" \
    --github https://github.com/jd/tenacity --site https://docs.example.com/retries --out context-packs
.venv/bin/python -m ack context-check context-packs/add-retries-with-backoff-to-our-webhook-sender.json
```

Or use the **Context packs** tab in the web app to review and pick excerpts before building. Fetched code is read, never run. Text that tries to instruct an AI is flagged and left out by default, and the prompt tells the model to treat every reference as data. Set `GITHUB_TOKEN` for private repositories or higher rate limits.

## Using it from the browser

Each person runs the web app next to their own project. Their keys, files and quarantine stay on their machine, and the app only listens on `127.0.0.1`:

```sh
python -m ack broker --host 0.0.0.0 --port 7468          # once, somewhere everyone can reach (see Known limits on TLS)
python -m ack web --root ~/myproject --name alice --broker ack://broker.example:7468 --open
```

The page opens on the landing page, whose handoff runs live against your agent once someone subscribes to you. The **Console** has:

| Tab | What you do there |
|---|---|
| **Desk** | Counts, identity, recent activity |
| **Announce** | Pick project files or paste one, choose signatories, open or sealed copies, quorum; watch each signature land |
| **Sign** | Announcements sent to you: the signature and digest checks, the files as text, ACK or refuse; your signing policy; subscriptions |
| **Ask & request** | Ask a paired agent (spelling fixes shown before sending); click a source in the answer to request that part, with why and what you'll do with it |
| **Peers** | Issue a one-time permit (QR plus link), redeem someone else's, compare fingerprints |
| **Parts** | Share a folder of your project, see who holds it, send updates |
| **Carbon copy** | The broker's audit entries that involve you, plus this agent's activity |

Approvals for received parts appear at the top of every console view: read the code, approve a sandboxed test run, then install (adopt, or adapt into `vendor/`) or reject. They wait up to 15 minutes, then count as a no. Nothing received runs or is installed without both clicks. Context packs and the provenance log live in the classic page at `/classic`.

`site/` is a Vite + React app. `npm run dev` there shows the landing page with a simulated handoff; the agent serves the built `site/dist` with its token injected, and everything becomes live.

## Layout

| File | Part |
|---|---|
| `ack/schema.py` | Message envelope and the message types (11 paired, plus UPDATE_AVAILABLE, and the broker's ANNOUNCE and SIGNED), strictly validated |
| `ack/wire.py` | The Ack wire: length-prefixed frames over TCP |
| `ack/announce.py` | Announcements: publish with a digest and quorum, verify a copy, sign ACK/NAK |
| `ack/crypto.py` | Identities (X25519 + Ed25519), pair keys, AES-GCM sealing, convergent blob encryption |
| `ack/bundle.py` | Packer: signed manifest + one encrypted, hash-named blob per file; safe unpack |
| `ack/broker.py` | Broker: one transport-independent core (blobs by hash, offline queues, holdings, subscriptions, announcements and vote tallies, audit log) served over the Ack wire and HTTP |
| `ack/client.py` | Broker client for `ack://` and `http://` |
| `ack/memory.py` | Project memory: parts keyed by topic, tags and a one-line summary, plus an embedding of that key; longer `notes` are never embedded |
| `ack/summarize.py` | Owner-side quick answers: summariser (where the owner's LLM goes) and a summary cache keyed by query embedding |
| `ack/judge.py` | Judgment calls: `RuleJudge` with may_share, accept_offer, want_update, decide |
| `ack/quarantine.py` | Hash re-check, dependency check, conflict detection, diff, sandboxed test run |
| `ack/pairing.py` | QR / `ack://pair?...` invites |
| `ack/agent.py` | The agent: wires the flow together, message-driven, state in `<project>/.ack/` |
| `ack/audit.py` | Provenance audit: drift, missing, stale, mismatch, unissued, corrupt cache |
| `ack/llm.py` | Optional LLM judges (rules are a floor the model can't lower) and a two-model panel |
| `ack/web.py` | The local web server for one agent (`python -m ack web`) |
| `site/` | The frontend: landing page with the live handoff, and the console |
| `ack/webui.html` | The classic single-file page, served at `/classic` (and at `/` if `site/` isn't built) |
| `ack/research/` | The three experiments: packet tiers, judge comparison, provenance fault injection |

## Flow → messages

| Step | Messages | Where |
|---|---|---|
| Pair | `PAIR` (only unsealed message) | `Agent.invite / join / _on_pair` |
| 1–2 Query, quick answer | `QUERY` (plain text) → `ANSWER` (summary, sources, confidence, version) | `ask`, `_on_query` |
| 3–4 Request, check | `REQUEST` (part, reason, intent) → `CHECK` READY / STALE / DENIED / NOT_FOUND | `request`, `_on_request` |
| 5 Offer | `OFFER` (signed manifest) → `ACCEPT` or `DECLINE` | `_offer`, `_on_offer` |
| 6 Bundle | owner uploads only blobs the broker lacks → `BUNDLE` | `_on_accept` |
| 7–8 Assess, decide | fetch missing blobs → quarantine → hashes, deps, conflicts → approved sandbox run → judge → install | `_receive` |
| 9 Ack | `ACK`; owner logs the outcome and the requester's intent | `_on_ack` |
| Drift | `UPDATE_AVAILABLE` (new manifest + changed paths + diff) → `ACCEPT` / `DECLINE` → `BUNDLE` with only changed layers | `publish_update`, `_on_update_available` |

**Quick answers.** The owner embeds the query and takes the top-k keys the asker is allowed to see. It summarises only those notes, and only on a cache miss. The summary is cached under the query embedding: a later query within the similarity threshold reuses it, so a reworded question gets the cached answer. A cached summary is dropped as soon as any note it came from changes (version, summary or notes), and it is never served to a peer who can't see all its sources. The asker also keeps its own exact-text cache.

**Caches.** Each agent keeps recent blobs in `.ack/blobs/`, least recently used evicted first (256 MB by default). Repeat requests and re-assessment need no download. The cache is never trusted: every read is re-hashed against the owner's signed manifest, and a bad copy is deleted. If the owner's cache evicted a blob before upload, it re-seals the blob from source, which only works if the file hasn't changed since it was offered.

## Security model

- **The broker can't read anything.** Message bodies are sealed with a per-pair key (X25519 → HKDF → AES-GCM), with the header bound in as associated data. Blobs are encrypted and named by the hash of their ciphertext, so the broker verifies what it stores without reading it.
- **The owner issues the manifest.** Manifests are Ed25519-signed by the owner and checked against the key pinned at pairing. Every blob is hash-checked on download and when read from the local cache, and every file is checked again after unpacking.
- **Pairing doesn't trust the broker.** The invite carries the inviter's public keys. The broker only sees a hash of the one-time token. The joiner proves it scanned the invite with an HMAC over its own key.
- **Nothing runs or installs without approval.** `approver("run-tests", …)` gates the sandbox run and `approver("install", …)` gates writing into the project. The default approver denies everything, and anything unverified is rejected.
- **Adopt and adapt.** Adopt writes the files at their paths. Adapt vendors them under `vendor/<peer>/<part>/` with a `BUILT_ON.md` provenance note, which happens when the receiver already has its own file at that path.
- **Updates.** `UPDATE_AVAILABLE` carries a diff, which the owner builds from its cached copy of what the peer holds. The receiver's judge decides whether to pull it. Nothing is uploaded until the receiver accepts. Content is encrypted convergently, so unchanged files map to blobs the receiver already has. The owner's diff is only a claim: the receiver assesses the real files and computes its own diff.

## Known limits of this build

- **Bundles are per-file layers, not one zip or tar.** The second design note says "one light archive" but the first says "layers, only missing pieces downloaded", and the two conflict. I kept layers, because that's what makes updates cheap. The signed manifest plays the role of the archive's table of contents.
- **Dependencies are Python requirement strings** (`requests>=2.31`), checked against the receiver's environment. Version ranges are checked when `packaging` is installed. Other ecosystems need their own checker.
- **The summariser is extractive.** It picks note sentences by word overlap. Replace `ExtractiveSummarizer` with the owner's LLM; it only runs on a cache miss.

- **The sandbox is not a security boundary.** It's a subprocess with an empty environment, `python -I`, rlimits and a timeout, but it can still read the disk and use the network. Replace `quarantine.run_tests` with a container or VM runner before using real peers. Only `python` test commands are allowed for now.
- **The embedding is a stub.** It's feature hashing, which is deterministic and matches both sides, but it's weak. Swap `memory.embed` for a real shared model, and both agents must use the same one.
- **The Ack wire has no TLS of its own.** The agent's broker secret travels in the frame header, so across machines run it inside a tunnel (WireGuard, SSH, stunnel) or use the HTTP transport behind a TLS proxy. Pair-sealed bodies and sealed announcements stay unreadable either way.
- **Open announcements are readable by the broker** and by anyone holding the blob id. Use sealed copies for anything private.
- **Keys of unpaired publishers come from the broker.** A subscriber that never paired with a publisher fetches its key from the broker once and pins it. A dishonest broker could substitute a key at that first fetch; pair first (sealed announcements require it) when that matters.
- **Message headers are visible to the broker.** It can see type, from, to and ids. That's needed for routing and the audit log, but it is metadata leakage.
- **The broker is single-node.** State is a JSON file and nothing is garbage-collected.
- **Peer keys are pinned on first pairing.** There is no key rotation or revocation yet.

## Research

`python -m ack research` runs all three offline; `--llm` adds Claude-backed judges (Opus 5.5 and Sonnet 5.5, plus a panel of both) and answerers. Results and how to read them are in [docs/EXPLAINED.md](docs/EXPLAINED.md#research-the-three-experiments).

## Research hooks (earlier notes)

- **Packet size vs. answer quality:** `ANSWER` is the "summary" tier and `REQUEST`→bundle is "full". Add a "summary + fetch" tier by letting the judge request just one file from a manifest.
- **Two LLMs vs. one vs. rules:** implement `may_share / accept_offer / decide` on an LLM-backed judge and compare it with `RuleJudge`. The `Report` dataclass is the judge's whole input, so runs are reproducible.
- **Provenance:** every decision is appended to `.ack/provenance.jsonl` on both sides, and the broker keeps `audit.jsonl` and holdings. `tests/test_flow.py` covers tampered blobs, stale parts, failing sandbox tests and forged senders.
