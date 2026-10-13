"""RQ3: does the provenance trail catch bad or stale transfers?

Each trial runs the real protocol (broker + two agents, alpha adopts beta's
retry-backoff), injects one fault, then runs `ack.audit.audit`. A fault counts as
caught if the expected finding kind appears. The clean trial measures false alarms.
"""

import shutil
import tempfile
from pathlib import Path

from ..agent import Agent
from ..audit import audit
from ..broker import start_in_thread
from ..demo import BETA_FILES, BETA_PARTS, RETRY_V2, pump, write
from ..memory import Entry
from ..pairing import Invite

INTENT = "wrap webhook delivery calls in retry()"


def _setup(tmp: Path):
    server, url = start_in_thread(tmp / "broker")
    write(tmp / "beta", BETA_FILES)
    beta = Agent("beta", tmp / "beta", url, log=None)
    alpha = Agent("alpha", tmp / "alpha", url, approver=lambda a, d: True, log=None)
    alpha.register()
    beta.register()
    for entry in BETA_PARTS:
        beta.share(Entry(**vars(entry)))
    alpha.join(Invite.from_uri(beta.invite().to_uri()))
    pump(alpha, beta)
    alpha.request("beta", "retry-backoff", "webhook sender drops events", INTENT)
    pump(alpha, beta)
    return server, alpha, beta


def _drift(alpha, beta, tmp):
    (tmp / "alpha" / "retry.py").write_text("# hand-edited after adoption\n")


def _missing(alpha, beta, tmp):
    (tmp / "alpha" / "retry.py").unlink()


def _stale(alpha, beta, tmp):
    alpha.judge.want_update = lambda manifest, changed, diff: (False, "skipping updates")
    write(tmp / "beta", {"retry/retry.py": RETRY_V2})
    beta.publish_update("retry-backoff")
    pump(alpha, beta)


def _stale_offline(alpha, beta, tmp):
    # The update is published while alpha is away; alpha audits before polling.
    write(tmp / "beta", {"retry/retry.py": RETRY_V2})
    beta.publish_update("retry-backoff")


def _forged(alpha, beta, tmp):
    # Someone edits alpha's state to claim a manifest beta never issued.
    inst = alpha.state["installs"]["beta/retry-backoff"]
    inst["manifest_hash"] = "f" * 64
    alpha._save()


def _registry(alpha, beta, tmp):
    alpha.broker.record_holding(owner="beta", source_id="retry-backoff", version=1,
                                manifest_hash="e" * 64, status="ADOPT")


def _cache(alpha, beta, tmp):
    blob = next(alpha.blobs.path.iterdir())
    blob.write_bytes(b"bit rot")


FAULTS = {
    "clean": (None, None),
    "local-drift": (_drift, "drift"),
    "file-deleted": (_missing, "missing"),
    "declined-update": (_stale, "stale"),
    "update-while-offline": (_stale_offline, "stale"),
    "forged-install-record": (_forged, "unissued"),
    "registry-mismatch": (_registry, "mismatch"),
    "corrupt-cache": (_cache, "cache"),
}


def run() -> dict:
    trials = {}
    for name, (inject, expected) in FAULTS.items():
        tmp = Path(tempfile.mkdtemp(prefix="ack-prov-"))
        server = None
        try:
            server, alpha, beta = _setup(tmp)
            if inject:
                inject(alpha, beta, tmp)
            findings = audit(alpha, owner_provenance={"beta": beta.provenance()})
            kinds = sorted({f.kind for f in findings})
            trials[name] = {
                "expected": expected,
                "found": kinds,
                "caught": (expected in kinds) if expected else not kinds,
                "details": [f"{f.kind}: {f.source}: {f.detail}" for f in findings],
            }
        finally:
            if server:
                server.shutdown()
            shutil.rmtree(tmp, ignore_errors=True)
    faults = [t for n, t in trials.items() if t["expected"]]
    return {
        "trials": trials,
        "detection_rate": sum(t["caught"] for t in faults) / len(faults),
        "false_alarm": not trials["clean"]["caught"],
    }
