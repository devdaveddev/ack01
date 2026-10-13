"""The judgment calls: what's safe to share, whether to take an offer, and adopt/adapt/reject.

These are the only places an LLM belongs. RuleJudge is the baseline that runs with
no API key; an LLM-backed judge implements the same three methods.
"""

from dataclasses import dataclass, field


@dataclass
class Report:
    """Everything a judge sees. Kept self-contained so judge runs are reproducible."""

    manifest_hash: str
    hashes_ok: bool
    tests_ran: bool
    tests_passed: bool | None
    test_output: str = ""
    conflicts: list[str] = field(default_factory=list)
    diff: str = ""
    missing_deps: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    source: str = ""  # "owner/source_id vN"
    intent: str = ""  # what the receiver said it would do with the part
    preview: dict[str, str] = field(default_factory=dict)  # path -> start of file text


@dataclass
class Decision:
    action: str  # ADOPT, ADAPT or REJECT
    reason: str


class RuleJudge:
    max_files = 200
    max_bytes = 5_000_000
    min_reason = 10

    def may_share(self, entry, requester: str, reason: str, intent: str) -> tuple[bool, str]:
        if requester not in entry.allow and "*" not in entry.allow:
            return False, f"{requester} is not on the allow list for {entry.id}"
        if len(reason.strip()) < self.min_reason:
            return False, "request needs a reason"
        if len(intent.strip()) < self.min_reason:
            return False, "request needs to say what it will do with the part"
        return True, "allowed"

    def accept_offer(self, manifest: dict) -> tuple[bool, str]:
        files = manifest["files"]
        if len(files) > self.max_files:
            return False, f"too many files ({len(files)})"
        size = sum(f["size"] for f in files)
        if size > self.max_bytes:
            return False, f"too large ({size} bytes)"
        if not manifest["tests"].get("cmd"):
            return False, "offer has no tests, so it can't be verified"
        if manifest["tests"].get("status") != "passed":
            return False, "owner reports tests not passing"
        return True, "within limits"

    def want_update(self, manifest: dict, changed: list[str], diff: str) -> tuple[bool, str]:
        """Pull an UPDATE_AVAILABLE? The diff is the owner's claim; assess re-checks the real files."""
        return self.accept_offer(manifest)

    def decide(self, report: Report) -> Decision:
        if report.problems or not report.hashes_ok:
            return Decision("REJECT", "integrity check failed: " + "; ".join(report.problems or ["hash mismatch"]))
        if report.missing_deps:
            return Decision("REJECT", "missing dependencies: " + ", ".join(report.missing_deps))
        if not report.tests_ran:
            return Decision("REJECT", "not verified: sandbox test run was not approved")
        if not report.tests_passed:
            return Decision("REJECT", "tests failed in sandbox")
        if report.conflicts:
            return Decision("ADAPT", "conflicts with local files: " + ", ".join(report.conflicts))
        return Decision("ADOPT", "verified, tests pass, no conflicts")
