# 12 · Command line, demo, research and tests

These files **drive** the protocol rather than implement it. The CLI is short, so it's explained line by line. The demo, research harness and tests are mostly calls into code explained in earlier pages, so they're explained function by function, with every line that does something new called out.

---

# `ack/__main__.py`, the command line

`python -m ack ...` runs this file. It uses `argparse`, the standard library's command-line parser: you declare the commands and options, and it reads `sys.argv`, validates it, writes `--help` text, and hands back an object with one attribute per option.

## Lines 26–27 · ask the human

```python
def _confirm(action: str, detail: str) -> bool:
    return input(f"approve {action}: {detail}? [y/N] ").strip().lower() == "y"
```

The approver used by `poll --approve`. `input()` prints the question and waits for a typed line. Only `y` (any case, extra spaces ignored) means yes, so just pressing Enter means **no**, which is the safe default.

## Lines 30–81 · declare the commands

```python
    p = argparse.ArgumentParser(prog="ack")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("demo")
    rs = sub.add_parser("research")
    rs.add_argument("--llm", action="store_true", help="...")
    rs.add_argument("--models", nargs=2, default=["claude-opus-5-5", "claude-sonnet-5-5"], help="...")
    rs.add_argument("--out", default="research-results.json")
    w = sub.add_parser("web", help="open the browser interface for one agent")
    w.add_argument("--root", help="your project folder")
    ...
    w.add_argument("--demo", action="store_true", help="local broker plus two demo agents")
    b = sub.add_parser("broker")
    b.add_argument("--port", type=int, default=8765)
    ...
```

- **31** The top-level parser. `prog="ack"` is the name shown in help.
- **32** **Subcommands** (`demo`, `research`, `web`, `broker`, `agent`). Whichever is chosen is stored as `args.cmd`, and `required=True` means one must be given.
- **35** `action="store_true"`: a flag that's `False` unless present.
- **36** `nargs=2`: takes exactly two values.
- **39–45** `web`: the browser interface. Either `--root` and `--name` for your own project, or `--demo` for a ready-made local setup. `--port` defaults to 8800, and `--open` opens the browser.
- **48** `type=int` converts the text `"8765"` to the number 8765.
- **51–55** `agent` has its own options (`--root`, `--name`, `--broker`) and its own sub-subcommands, stored as `args.action`.
- **57–65** `share`:
  - `id` is **positional** (no dashes), so it's required and given in order
  - `nargs="+"` means one or more values
  - `nargs="*"` means zero or more
  - `--tests` is one string that gets split later
- **72–76** `request` takes four positionals, in order: peer, part, reason, intent.
- **80** `poll --approve` switches on the keyboard approver.

## Lines 83–105 · run the chosen command

```python
    args = p.parse_args(argv)
    if args.cmd == "demo":
        from .demo import run
        run()
    elif args.cmd == "research":
        from .research.run import run
        run(llm=args.llm, models=tuple(args.models), out=Path(args.out))
    elif args.cmd == "web":
        from . import web
        if args.demo:
            servers = web.demo(args.port, args.open)
        else:
            if not args.root or not args.name:
                p.error("web needs --root and --name (or --demo)")
            servers = [web.serve(web.WebApp(args.name, Path(args.root), args.broker), args.port, args.open)]
        web.run_forever(servers)
    elif args.cmd == "broker":
        from .broker import make_server
        server = make_server(Path(args.data), args.host, args.port)
        print(...)
        server.serve_forever()
    else:
        _agent(args)
```

- **83** Parse. `argv=None` means "use the real command line". Tests can pass a list instead.
- **84–105** Each branch imports only what it needs (so starting the CLI stays fast) and runs it. `serve_forever()` blocks until you press Ctrl-C.
- **90–98** `web`: start the demo, or check that `--root` and `--name` were given (`p.error` prints the message and usage, then exits) and serve one agent. `run_forever` keeps the program alive until Ctrl-C. The web app itself is explained in [13-web.md](13-web.md).

## Lines 108–142 · agent actions

```python
    approver = _confirm if getattr(args, "approve", False) else deny_all
    agent = Agent(args.name, Path(args.root), args.broker, approver=approver)
```

- **113** Only `poll` has an `--approve` option, so `getattr(args, "approve", False)` reads it if present, or defaults to `False`. Without `--approve`, the agent uses `deny_all`.
- **114** Load (or create) the agent for that project folder. All state comes from `<root>/.ack/`, so separate CLI runs carry on where the last one stopped.
- **115–142** One branch per action, each a single method call plus a printout. Notable lines:
  - **121** `shlex.split(args.tests)` splits a command string the way a shell would, quotes included, into a list: `"python -m unittest"` → `["python", "-m", "unittest"]`.
  - **124** `invite` prints the QR code, or the link if `qrcode` isn't installed.
  - **129** `ask` prints a cached answer immediately; otherwise it says to poll.
  - **139–142** `audit` prints one aligned line per finding (`{f.kind:9}` pads to 9 characters), then the count.

## Lines 145–146

```python
if __name__ == "__main__":
    main()
```

Run `main()` when this file is executed directly.

---

# `ack/demo.py`, the two-agent story

## Lines 17–119 · fixtures as data

- **17** `TESTS`: the test command each shared part uses: run `unittest`, discovering `test_*.py` files in the current folder.
- **19–44** `RETRY_V1` and `RETRY_V2`: two versions of a retry helper, as strings. V2 adds jitter (random extra delay). The update step writes V2 over V1.
- **46–94** `BETA_FILES`: beta's whole project as `{path: file content}`. It includes `geo/`, whose test needs `capitals.json`, which is deliberately **left out of the shared file list**. That's what makes the REJECT step happen.
- **96–98** `ALPHA_FILES`: alpha already has its own `slugify.py`, which makes the ADAPT step happen.
- **100–119** `BETA_PARTS`: the four `Entry` records beta shares. `billing-keys` has `allow=[]`, so it's private, which makes the DENIED step happen.

## Lines 122–126 · write

```python
def write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text).lstrip())
```

Creates each file. `textwrap.dedent` removes the shared indentation the strings have inside the Python source, and `.lstrip()` removes the leading newline.

## Lines 129–133 · pump

```python
def pump(*agents: Agent, rounds: int = 20) -> None:
    for _ in range(rounds):
        if sum(a.poll() for a in agents) == 0:
            return
```

- `*agents` accepts any number of agents.
- Each round, every agent polls. When a whole round brings no messages, the conversation has settled.
- The limit of 20 rounds guards against an endless ping-pong. In real use, each agent polls on its own schedule; this just simulates that quickly.

## Lines 140–142 · approve

The demo's approver prints the request and says yes. It exists so the demo can show both approval prompts without a human at the keyboard.

## Lines 145–157 · run

```python
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
```

- **148–150** With no folder given, make a temporary one.
- **151** Start a broker in the background on a free port.
- **152–157** `try/finally`: whether the story succeeds or crashes, **always** stop the broker and delete the temporary folder.

## Lines 160 onward · `_story`

Each `step(...)` is one scene, and each scene is a few method calls followed by `pump(...)`:

1. Create both projects and agents, register them, and share beta's parts. `Entry(**vars(entry))` makes a fresh copy of each template, since `vars()` turns an object into a dict of its fields.
2. Pairing: `beta.invite()` → print the QR → `alpha.join(...)` → pump.
3. Quick answer: ask, then ask again (alpha's own cache), then ask a reworded question (beta's summary cache).
4. DENIED and NOT_FOUND.
5. A full request ending in ADOPT.
6. An update: write V2 → `publish_update` → `alpha.poll()` once to show the diff beta sent → pump. Only 1 of 2 layers travels.
7. ADAPT (the slugify conflict), REJECT (geo's missing fixture), STALE (beta edits `geo.py` without recording a version).
8. Print alpha's provenance trail, the broker's holdings, and audit-log counts. `Counter` counts the occurrences of each action.

---

# `ack/research/`, the experiments

## `tiers.py`: RQ1, packet size

- **24** `TIERS`: the four levels of context.
- **27–38** `QUESTIONS`: `(question, gold facts)`. Each fact is a **list of acceptable phrasings**, e.g. `["three attempts", "attempts=3"]`, because the notes and the code say the same thing differently.
- **48–64** `contexts(owner, peer, question)`: builds the four contexts **using the owner's real `answer_packet`**.
  - `none` is empty.
  - `summary` is the packet's summary.
  - For `summary+fetch`, it embeds each file of the top-matching part and picks the one most similar to the question (`max(files, key=...)`).
  - `full` appends every file.
- **67–69** `coverage(text, facts)`: the fraction of facts with at least one phrasing present (lowercase substring match). `any(...)` inside `sum(...)` counts the facts found.
- **72–93** `run(answerer=None)`:
  - Builds beta's project in a temporary folder. The agent never contacts the broker here, so its URL is a placeholder.
  - For every question and tier, gets an answer: by default the context itself (measuring whether the information arrived at all); with an LLM answerer, the model's answer.
  - Scores it, then averages per tier, including score per kilobyte.
- **96–105** `llm_answerer(complete)`: a factory returning an answer function. It tells the model to answer only from the context, wraps the context in `<context>` tags, and uses a one-field JSON schema.

## `judges.py`: RQ2, the judge benchmark

- **17–40** `CLEAN_RETRY` and `RETRY_TEST`: a known-good module and a real test, as strings. Most scenarios start from these.
- **44–49** `Scenario`: a name, the **gold** answer, **why** it's the right answer, and the `Report` to judge.
- **52–57** `_r(...)`: a helper that builds a `Report` with sensible defaults (tests ran and passed, no problems), so each scenario states only what's special about it.
- **60–128** `scenarios()`: the 14 cases. The traps put something nasty into a `preview` file or the `diff` while everything the rules check still passes, e.g. code that posts `os.environ` to a remote URL. The benign look-alikes use the network or a subprocess in a way that **matches their stated intent**.
- **131–133** `RED_FLAGS`: regex patterns such as `\bos\.environ\b`. `\b` is a word boundary, so `os.environ` matches but `myos.environment` doesn't.
- **135–153** `StaticScanJudge.decide`: run the rules first (`super().decide`). If they pass, reject on any red flag in the code or diff; then reject if every test file is hollow.
- **156–160** `_hollow(source)`: collect the arguments of every `self.assertX(...)` and every bare `assert`. If all of them are trivially true (`True`, `1, 1`), the tests check nothing. No assertions at all also counts as hollow, since `all([])` is `True`.
- **163–181** `evaluate(judges)`: run each judge on every scenario and compute three numbers:
  - **accuracy**: matches the gold answer
  - **unsafe_accept**: of the cases that should be rejected, the share it let through
  - **false_reject**: of the safe cases, the share it rejected

  It also reports panel splits and backend errors when present.

## `provenance.py`: RQ3, fault injection

- **22–35** `_setup(tmp)`: a real broker, two agents, pairing, and alpha adopting `retry-backoff`. Every trial starts from this clean, real state.
- **38–73** One small function per fault, each changing exactly one thing:
  - **38–39** `_drift` edits the adopted file.
  - **42–43** `_missing` deletes it.
  - **46–50** `_stale`: alpha's judge declines updates; beta publishes v2.
  - **53–56** `_stale_offline`: beta publishes v2 and alpha never polls.
  - **59–63** `_forged` overwrites alpha's install record with a manifest hash beta never issued.
  - **66–68** `_registry` writes a false holding to the broker.
  - **71–73** `_cache` overwrites a cached blob with junk.
- **76–85** `FAULTS`: name → (injector, the finding kind expected). `clean` has neither; it measures false alarms.
- **88–114** `run()`: for each fault, in a fresh temporary folder: set up, inject, run `audit(alpha, owner_provenance=...)`, and record whether the expected kind was found. The `finally` block shuts the broker down and deletes the folder even when a trial fails. It returns per-trial results, the detection rate, and whether the clean run raised any finding.

## `run.py`: run all three

- **10–11** `_pct(x)`: formats 0.667 as `" 66.7%"`.
- **14–57** `run(llm, models, out)`: runs RQ1, RQ2, RQ3 in order and prints a table for each. String formats like `{name:22}` (pad to 22) and `{x:>8}` (right-align in 8) line the columns up. With `--llm`, it builds two `LLMJudge`s and a `PanelJudge`, and adds them to the candidates with `|=`. At the end it writes everything to JSON.

---

# `tests/`, how the tests work

The tests use **pytest**. Any function named `test_...` in a file named `test_*.py` is a test, and a plain `assert` checks a condition.

```python
@pytest.fixture
def broker(tmp_path):
    server, url = start_in_thread(tmp_path / "broker")
    yield url
    server.shutdown()
```

- **`@pytest.fixture`** marks a setup function. A test that takes a parameter **with the same name** (`def test_x(broker):`) gets the fixture's value automatically.
- **`tmp_path`** is pytest's built-in fixture: a fresh empty folder for each test.
- **`yield`** hands the value to the test. Code after `yield` runs after the test finishes, even if it failed. That's the cleanup.
- **`with pytest.raises(SomeError, match="text"):`** passes only if the block raises that error, with that text in its message.

## `test_units.py`: the building blocks in isolation

| Test | Proves |
|---|---|
| `test_content_encryption_is_convergent_and_checked` | the same file gives the same ciphertext; one flipped bit fails decryption |
| `test_pair_key_is_symmetric` | A and B derive the same key from opposite halves |
| `test_schema_rejects_bad_messages` | a bad status, an extra field, or an unknown type is refused |
| `test_bundle_roundtrip` (fixture `packed`) | pack → verify → unpack gives back the original file |
| `test_manifest_tamper_and_traversal_rejected` | an edited manifest fails its signature; a wrong owner fails; `../`, absolute and backslash paths fail |
| `test_memory_search_and_staleness` | search finds the right part; editing a file makes it stale; `bump` makes it current again |
| `test_invite_uri_roundtrip` | invite → link → invite is lossless |
| `test_broker_checks_hashes_auth_and_invites` | a mislabelled blob → 400, no auth → 401, sending as someone else → 403, a reused invite → 410 |

In the bit-flip test, `ct[-1] ^ 1` uses XOR to flip the lowest bit of the last byte.

## `test_flow.py`: two real agents and a real broker

The `pair` fixture builds a full setup and returns a `make()` function, so each test can first adjust files (e.g. give alpha a conflicting `slugify.py`) and then build the agents. Small helpers, `decisions(agent)` and `events(agent, kind)`, read the provenance log and event list.

| Test | Proves |
|---|---|
| `test_adopt_then_update_ships_only_changed_layer` | an adopt, then an update where only 1 of 2 layers downloads |
| `test_query_hides_parts_the_peer_cannot_request` | a private part never appears in answers |
| `test_check_statuses` | DENIED, NOT_FOUND, DENIED (no intent), STALE, in that order |
| `test_conflict_leads_to_adapt_in_vendor` | our file is untouched; theirs goes to `vendor/` with `BUILT_ON.md` |
| `test_failing_sandbox_tests_reject` | the missing-fixture bundle is rejected |
| `test_never_runs_or_installs_without_approval` | with a refusing approver: only asked once ("run-tests"), REJECT, nothing installed |
| `test_tampered_blob_on_broker_is_rejected` | polls step by step, corrupts the broker's blobs between upload and download → REJECT for integrity |
| `test_unpaired_or_forged_messages_are_dropped` | a third agent who knows beta's key but never paired is dropped |
| `test_summary_cache_reuses_by_meaning_and_invalidates_on_note_change` | wraps the summariser to count calls: a reworded question → no new call; a note change → a new call |
| `test_summary_cache_respects_visibility` | a cached answer isn't served after access is removed |
| `test_update_available_sends_diff_and_receiver_can_decline` | the diff contains `+import random`; on decline, **no blob is uploaded** (the broker's blob folder is unchanged) |
| `test_missing_dependency_rejects` | an uninstalled dependency → REJECT naming it |
| `test_owner_logs_intent` | the owner's provenance records the requester's intent |
| `test_evicted_cache_still_uploads_and_repeat_request_is_local` | the owner re-seals evicted blobs; a repeat request downloads 0 layers |
| `test_blob_store_evicts_least_recently_used` | sets file times with `os.utime` to control "age"; the oldest is evicted |

## `test_research.py`: the LLM judge's safety, with a fake model

```python
def fake(action, reason="model says so"):
    calls = []
    def complete(system, prompt, schema):
        calls.append(prompt)
        return {"action": action, "reason": reason}
    complete.calls = calls
    return complete
```

A stand-in backend that always gives the same answer and records every prompt. Functions are objects in Python, so `complete.calls = calls` attaches the list to the function, where the test can read it. No API key and no network are needed.

| Test | Proves |
|---|---|
| `test_llm_cannot_loosen_the_rule_floor` | a model saying ADOPT can't override failing tests, missing deps or a conflict; when rules reject, the model is never called |
| `test_llm_can_be_stricter_and_fails_closed` | a model REJECT wins; a backend that raises → REJECT "failing closed" |
| `test_untrusted_content_is_fenced_in_the_prompt` | a file's text appears inside `<file path=...>` tags |
| `test_panel_takes_the_most_conservative_vote` | ADOPT + REJECT → REJECT, and one split is counted |
| `test_judge_benchmark_separates_baselines` | rules miss the traps; the static scan catches them but rejects benign code |
| `test_tiers_curve_is_monotonic` | scores never drop as the tier grows, from 0 to 1 |
| `test_provenance_audit_catches_every_injected_fault` | 100% detection, no false alarm |
| `test_backend_errors_are_recorded_not_hidden` | a backend error is kept in `judge.errors` |

## `test_bugfixes.py`: one regression test per bug fixed

Each test is named after the bug's letter, so it's clear what would break if a fix were undone.

| Test | Proves |
|---|---|
| `test_a_hidden_paths_rejected` | `.git/hooks/...`, `.github/workflows/...`, `.ack/...` and `a/.env` are refused, even with a valid signature |
| `test_b_bad_part_ids_rejected` / `test_b_owner_cannot_share_bad_ids_or_paths` | `../../etc`, `a/b`, empty and dotted ids are refused on both sides |
| `test_c_rejected_update_keeps_registry_record` | after refusing an update, the audit says only "stale", with no false "mismatch" |
| `test_d_update_removes_dropped_files` / `test_d_locally_edited_leftovers_are_kept` | an update removes files it dropped, but never one you edited |
| `test_e_one_failure_does_not_lose_other_messages` | a handler that crashes on the first message still lets the second be answered |
| `test_f_unreachable_broker_is_a_broker_error` | a dead broker gives `BrokerError(503)`, not a traceback |
| `test_g_broker_only_shows_what_concerns_you` | a third agent sees none of the other two's messages or holdings |
| `test_i_cached_answers_expire` | an answer older than an hour triggers a new question |
| `test_j_part_without_tests_says_so` | CHECK says "no tests" rather than "tests failing" |

`@pytest.mark.parametrize("path", [...])` runs the same test once per value in the list.

## `test_web.py`: the web app over real HTTP

| Test | Proves |
|---|---|
| `test_guards` | the page carries its token; the API refuses calls without it (403); a foreign `Host` header is refused (403) |
| `test_full_flow_through_the_browser_api` | pair → ask → request → approve tests → approve install → installed, with a clean audit, all through the same calls the page makes |
| `test_rejecting_in_the_browser_installs_nothing` | clicking Reject leaves the project untouched and logs a REJECT |
| `test_share_form_validates` | missing files and bad ids give readable 400 errors; a good part is shared |
