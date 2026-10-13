# 11 · `ack/audit.py`, line by line

**Job of this file:** answer "can I still trust what I hold?" from the receiver's side. It compares installed files, the provenance log, the broker's registry and the local cache, and returns a list of findings. It's plain code with no AI.

Primer references: [dataclasses](00-python-primer.md#decorators-something-above-a-def-or-class), [comprehensions](00-python-primer.md#comprehensions-building-a-collection-in-one-line).

---

## Lines 1–15 · docstring

Lists the six kinds of finding:

| Kind | Means |
|---|---|
| `drift` | an installed file changed after we adopted it |
| `missing` | an installed file was deleted |
| `stale` | the owner has published a newer version than ours |
| `mismatch` | our log, our install records and the broker registry disagree |
| `unissued` | we hold a manifest the owner has no record of issuing |
| `cache` | a cached blob fails its hash |

## Lines 17–26 · imports and the Finding record

```python
from dataclasses import dataclass

from .crypto import sha256_hex


@dataclass
class Finding:
    kind: str
    source: str
    detail: str
```

- **23–26** One finding: its kind (from the table above), which part (`"beta/retry-backoff"`), and a readable detail.

## Lines 29–33 · inputs

```python
def audit(agent, holdings: list[dict] | None = None, owner_provenance: dict[str, list[dict]] | None = None
          ) -> list[Finding]:
    findings: list[Finding] = []
    installs = agent.state["installs"]
    holdings = holdings if holdings is not None else agent.broker.holdings()
```

- **29** Inputs:
  - `agent`: the receiver being audited
  - `holdings`: the broker registry; if not given, it's fetched
  - `owner_provenance`: optionally, the owners' own provenance logs, keyed by owner name. These are available within one team, or in research.
- **32** What we installed: `"peer/part"` → record.
- **33** Use the holdings passed in (tests can pass a fixed list), or ask the broker.

## Lines 35–41 · check 1: are installed files still what we adopted?

```python
    for key, inst in installs.items():
        for path, f in inst["files"].items():
            target = agent.root / f["installed_at"]
            if not target.is_file():
                findings.append(Finding("missing", key, f"{f['installed_at']} was deleted"))
            elif sha256_hex(target.read_bytes()) != f["sha256"]:
                findings.append(Finding("drift", key, f"{f['installed_at']} changed since v{inst['version']}"))
```

- **35–36** For every installed part, and every file in it…
- **37** …find where it was installed (the project root for ADOPT, `vendor/...` for ADAPT).
- **38–39** Gone → `missing`.
- **40–41** Present, but its hash differs from the hash in the owner's signed manifest → `drift`. Someone edited it after adoption.

## Lines 43–50 · read the registry

```python
    latest_owner = {}
    ours_at_broker = {}
    for h in holdings:
        key = f"{h['owner']}/{h['source_id']}"
        if h["holder"] == h["owner"] and h["status"] == "OWNER":
            latest_owner[key] = max(latest_owner.get(key, 0), h["version"])
        if h["holder"] == agent.name:
            ours_at_broker[key] = h
```

- **43** For each part: the owner's latest published version.
- **44** For each part: what the registry says **we** hold.
- **46** Build the same `"owner/part"` key the install records use.
- **47–48** A record the owner made about its own part with status `OWNER` announces a version. Keep the highest version seen. `.get(key, 0)` starts from 0.
- **49–50** Records where we're the holder.

## Lines 51–56 · check 2: stale, and check 3a: registry mismatch

```python
    for key, inst in installs.items():
        if latest_owner.get(key, 0) > inst["version"]:
            findings.append(Finding("stale", key, f"hold v{inst['version']}, owner is at v{latest_owner[key]}"))
        reg = ours_at_broker.get(key)
        if reg is None or reg["manifest_hash"] != inst["manifest_hash"] or reg["status"] == "REJECT":
            findings.append(Finding("mismatch", key, "broker registry does not show this install"))
```

- **52–53** The owner has published a higher version than the one we hold → `stale`. This works even when we declined the update, or were offline when it was announced, because the owner registers each version as soon as it's published.
- **54–56** Our own install record and the registry's record about us must agree: the registry must show a record, with the same manifest hash, and not marked REJECT. Anything else is a `mismatch`: either our local state or the registry was tampered with, or something went wrong mid-transfer.

## Lines 58–64 · check 3b: does every install have a matching decision in our log?

```python
    last_decision = {}
    for rec in agent.provenance():
        if rec.get("role") == "receiver" and rec.get("decision") in ("ADOPT", "ADAPT"):
            last_decision[f"{rec['peer']}/{rec['source_id']}"] = rec["manifest_hash"]
    for key, inst in installs.items():
        if last_decision.get(key) != inst["manifest_hash"]:
            findings.append(Finding("mismatch", key, "install has no matching ADOPT/ADAPT in our provenance log"))
```

- **59–61** Read our provenance log in order, keeping the **latest** ADOPT or ADAPT manifest for each part. A later entry overwrites an earlier one.
- **62–64** Every install record must match that latest logged decision. An install with no logged decision, or a different manifest, means the install records were edited outside the protocol.

## Lines 66–71 · check 4: did the owner actually issue this? (optional)

```python
    for owner, records in (owner_provenance or {}).items():
        issued = {r["manifest_hash"] for r in records
                  if r.get("event") == "acked" and r.get("peer") == agent.name and r.get("decision") != "REJECT"}
        for key, inst in installs.items():
            if inst["peer"] == owner and inst["manifest_hash"] not in issued:
                findings.append(Finding("unissued", key, f"{owner} has no record of {inst['manifest_hash'][:12]}"))
```

- **66** Only runs if owner logs were provided. `(x or {})` makes "none given" an empty loop.
- **67–68** A **set comprehension**: every manifest hash this owner logged as acknowledged **by us** and not rejected. That's the set of things the owner agrees it gave us.
- **69–71** Any install from this owner whose manifest isn't in that set → `unissued`. This catches a forged install record that claims a manifest the owner never sent.

## Lines 73–76 · check 5: is the cache intact?

```python
    for blob in agent.blobs.path.iterdir():
        if blob.is_file() and sha256_hex(blob.read_bytes()) != blob.name:
            findings.append(Finding("cache", blob.name[:12], "cached blob fails its hash"))
    return findings
```

- **73–75** Every cached blob must hash to its own filename. A corrupt one isn't dangerous (every use re-checks it and throws bad copies away), but reporting it shows the disk or someone tampered with it.
- **76** Return all findings. An empty list means everything checks out.

## How it's run

```sh
python -m ack agent --root ~/webhooks --name alpha audit
```

The command-line `audit` action calls `audit(agent)` and prints one line per finding. The research harness (`research/provenance.py`) injects one fault at a time and checks that the expected kind appears. All seven faults are caught, with no findings on a clean run.
