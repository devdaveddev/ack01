# Code walkthrough: every line, and the syntax behind it

[EXPLAINED.md](../EXPLAINED.md) covers **what** each part does and **why**. These pages cover **how**: they go through the code line by line, saying what each line does, what its Python syntax means, and how it serves the protocol.

## How to read these pages

- Each page follows one source file from top to bottom. A heading gives the **line numbers** (`Lines 94–105`), then the code, then bullets keyed by line number (**99**, **102–103**, …).
- Syntax that appears everywhere (f-strings, comprehensions, `with`, decorators, `|` on dicts…) is explained **once** in the primer. The pages link back to it rather than repeat it.
- Code blocks are copied from the source. In a few places, long logging lines or repeated branches are shortened with `...`; the bullets still cover those lines.
- Start with the primer if Python syntax is new to you. Otherwise follow the order below: it builds from the bottom layer (crypto) up to the protocol (agent).

## Pages

| # | Page | Source | What you'll understand after reading it |
|---|---|---|---|
| 00 | [Python primer](00-python-primer.md) | n/a | every piece of syntax the code uses |
| 01 | [crypto](01-crypto.md) | `ack/crypto.py` | keys, pair keys, signing, sealing, convergent encryption |
| 02 | [schema](02-schema.md) | `ack/schema.py` | what a message is and how strict validation works |
| 03 | [bundle](03-bundle.md) | `ack/bundle.py` | manifests, blobs, path safety, safe unpacking |
| 04 | [pairing](04-pairing.md) | `ack/pairing.py` | QR invites and the HMAC proof |
| 05 | [memory](05-memory.md) | `ack/memory.py` | the embedding, search, versions and staleness |
| 06 | [summarize](06-summarize.md) | `ack/summarize.py` | the summariser and the meaning-keyed cache |
| 07 | [judge and llm](07-judge-and-llm.md) | `ack/judge.py`, `ack/llm.py` | the judgment calls, the rule floor, the LLM panel |
| 08 | [quarantine](08-quarantine.md) | `ack/quarantine.py` | the sandbox, conflicts, dependencies, diffs |
| 09 | [broker and client](09-broker-and-client.md) | `ack/broker.py`, `ack/client.py` | the HTTP server, auth, queues, registry |
| 10 | [agent](10-agent.md) | `ack/agent.py` | the whole protocol, handler by handler |
| 11 | [audit](11-audit.md) | `ack/audit.py` | how stale, drifted and forged state is caught |
| 12 | [CLI, demo, research, tests](12-cli-demo-research-tests.md) | `__main__.py`, `demo.py`, `research/`, `tests/` | how everything is driven and verified |
| 13 | [web app](13-web.md) | `ack/web.py`, `ack/webui.html` | the browser interface: approvals that wait for a click, and how the page stays safe |
| 14 | [wire and announcements](14-wire-and-announcements.md) | `ack/wire.py`, `ack/announce.py`, `ack/broker.py` (core), `site/` | the Ack wire framing, fan-out with quorum signing, open vs sealed copies, the React frontend |

## One message, traced through the code

To see how the pages connect, here's a single OFFER from beta to alpha, with the page and line numbers for each step:

1. beta `_on_request` checks the part, then calls `_offer` ([10](10-agent.md#lines-297319--the-owners-check), lines 297–319).
2. `_pack` → `bundle.pack` hashes and encrypts each file and signs the manifest ([03](03-bundle.md#lines-5070--pack-a-part), lines 50–70; [01](01-crypto.md#lines-139142--encrypt-a-file-convergent), lines 139–142).
3. `_send` builds the message (`schema.make`, [02](02-schema.md#lines-4961--build-a-message)), then seals the body with the header as associated data ([10](10-agent.md#lines-196201--send-a-message), line 199).
4. The broker checks who's sending and queues it ([09](09-broker-and-client.md#lines-209221--accept-a-message-for-delivery), lines 209–221).
5. alpha `poll` → `_open` decrypts and validates it → `getattr` dispatches to `_on_offer` ([10](10-agent.md#lines-217231--poll-handle-everything-waiting), line 223).
6. `_on_offer` matches it to alpha's request, verifies the signature ([03](03-bundle.md#lines-7397--verify-a-received-manifest)), asks the judge ([07](07-judge-and-llm.md#lines-4859--accept_offer-receiver-side)), and replies ACCEPT.
