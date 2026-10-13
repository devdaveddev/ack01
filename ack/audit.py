"""Provenance audit: does what we hold still match what the owner issued?

Plain code, no AI. Runs from the receiver's side using its own state, its local
cache, and the broker's holdings registry. If the owner's provenance log is
available too (same team, or for research), it also checks that every manifest
we installed is one the owner actually issued and acknowledged.

Each finding is (kind, source, detail). Kinds:
  drift      an installed file no longer matches the hash we adopted
  missing    an installed file is gone
  stale      the owner has published a newer version than we hold
  mismatch   our log, our installs, and the broker registry disagree
  unissued   we hold a manifest the owner has no record of issuing
  cache      a cached blob no longer matches its hash
"""

from dataclasses import dataclass

from .crypto import sha256_hex


@dataclass
class Finding:
    kind: str
    source: str
    detail: str


def audit(agent, holdings: list[dict] | None = None, owner_provenance: dict[str, list[dict]] | None = None
          ) -> list[Finding]:
    findings: list[Finding] = []
    installs = agent.state["installs"]
    holdings = holdings if holdings is not None else agent.broker.holdings()

    for key, inst in installs.items():
        for path, f in inst["files"].items():
            target = agent.root / f["installed_at"]
            if not target.is_file():
                findings.append(Finding("missing", key, f"{f['installed_at']} was deleted"))
            elif sha256_hex(target.read_bytes()) != f["sha256"]:
                findings.append(Finding("drift", key, f"{f['installed_at']} changed since v{inst['version']}"))

    latest_owner = {}
    ours_at_broker = {}
    for h in holdings:
        key = f"{h['owner']}/{h['source_id']}"
        if h["holder"] == h["owner"] and h["status"] == "OWNER":
            latest_owner[key] = max(latest_owner.get(key, 0), h["version"])
        if h["holder"] == agent.name:
            ours_at_broker[key] = h
    for key, inst in installs.items():
        if latest_owner.get(key, 0) > inst["version"]:
            findings.append(Finding("stale", key, f"hold v{inst['version']}, owner is at v{latest_owner[key]}"))
        reg = ours_at_broker.get(key)
        if reg is None or reg["manifest_hash"] != inst["manifest_hash"] or reg["status"] == "REJECT":
            findings.append(Finding("mismatch", key, "broker registry does not show this install"))

    last_decision = {}
    for rec in agent.provenance():
        if rec.get("role") == "receiver" and rec.get("decision") in ("ADOPT", "ADAPT"):
            last_decision[f"{rec['peer']}/{rec['source_id']}"] = rec["manifest_hash"]
    for key, inst in installs.items():
        if last_decision.get(key) != inst["manifest_hash"]:
            findings.append(Finding("mismatch", key, "install has no matching ADOPT/ADAPT in our provenance log"))

    for owner, records in (owner_provenance or {}).items():
        issued = {r["manifest_hash"] for r in records
                  if r.get("event") == "acked" and r.get("peer") == agent.name and r.get("decision") != "REJECT"}
        for key, inst in installs.items():
            if inst["peer"] == owner and inst["manifest_hash"] not in issued:
                findings.append(Finding("unissued", key, f"{owner} has no record of {inst['manifest_hash'][:12]}"))

    for blob in agent.blobs.path.iterdir():
        if blob.is_file() and sha256_hex(blob.read_bytes()) != blob.name:
            findings.append(Finding("cache", blob.name[:12], "cached blob fails its hash"))
    return findings
