"""Assess an unpacked bundle before anything touches the project.

The sandbox here is a subprocess with an empty environment, resource limits, an
isolated interpreter and the quarantine folder as its working directory. It is
NOT a security boundary: the code can still read the filesystem and use the
network. Swap `run_tests` for a container or VM runner before trusting real peers.
"""

import difflib
import importlib.metadata
import os
import re
import subprocess
import sys
from pathlib import Path

from .crypto import sha256_hex
from .judge import Report

ALLOWED_RUNNERS = {"python", "python3"}
TEST_TIMEOUT = 30
PREVIEW_CHARS = 4000
PREVIEW_FILES = 20


# A tiny launcher: set the limits, then replace itself with the real test command.
# preexec_fn would do the same, but it can deadlock when the parent has threads
# (the web app does), so the limits are applied inside the child instead.
_LIMITED = (
    "import os, resource, sys\n"
    "resource.setrlimit(resource.RLIMIT_CPU, (20, 20))\n"
    "resource.setrlimit(resource.RLIMIT_AS, (1 << 30, 1 << 30))\n"
    "resource.setrlimit(resource.RLIMIT_FSIZE, (50 << 20, 50 << 20))\n"
    "os.execv(sys.argv[1], sys.argv[1:])\n"
)


def run_tests(cmd: list[str] | None, cwd: Path, timeout: int = TEST_TIMEOUT) -> tuple[bool, str]:
    if not cmd or cmd[0] not in ALLOWED_RUNNERS:
        return False, f"test command not allowed: {cmd!r}"
    argv = [sys.executable, "-I", *cmd[1:]]
    if os.name == "posix":
        argv = [sys.executable, "-I", "-c", _LIMITED, *argv]
    env = {"PATH": "/usr/bin:/bin", "HOME": str(cwd), "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"}
    try:
        proc = subprocess.run(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            start_new_session=True,
        )
    except subprocess.TimeoutExpired:
        return False, f"timed out after {timeout}s"
    return proc.returncode == 0, (proc.stdout + proc.stderr)[-4000:]


def _hash(path: Path) -> str | None:
    return sha256_hex(path.read_bytes()) if path.is_file() else None


def find_conflicts(manifest: dict, project: Path, prior: dict | None) -> list[str]:
    """A path conflicts if the project has its own different file there.

    A file we previously adopted from this same source, unmodified, is not a conflict.
    """
    prior_files = (prior or {}).get("files", {})
    conflicts = []
    for f in manifest["files"]:
        local = _hash(project / f["path"])
        if local is None or local == f["sha256"]:
            continue
        adopted = prior_files.get(f["path"], {})
        if adopted.get("installed_at") == f["path"] and adopted.get("sha256") == local:
            continue
        conflicts.append(f["path"])
    return conflicts


def missing_deps(deps: list[str]) -> list[str]:
    """Deps are Python requirement strings ("requests>=2.31"), checked against this environment."""
    missing = []
    for req in deps:
        name = re.split(r"[<>=!~;\[\s]", req.strip(), maxsplit=1)[0]
        try:
            have = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            missing.append(req)
            continue
        try:
            from packaging.requirements import InvalidRequirement, Requirement
        except ImportError:
            continue  # can't check version ranges without packaging; presence will do
        try:
            if not Requirement(req).specifier.contains(have, prereleases=True):
                missing.append(f"{req} (have {have})")
        except InvalidRequirement:
            missing.append(f"{req} (unparseable)")
    return missing


def diff_bytes(path: str, old: bytes, new: bytes) -> str:
    try:
        old_lines = old.decode().splitlines(keepends=True)
        new_lines = new.decode().splitlines(keepends=True)
    except UnicodeDecodeError:
        return f"binary file {path} changed\n"
    return "".join(difflib.unified_diff(old_lines, new_lines, f"a/{path}", f"b/{path}"))


def text_diff(manifest: dict, qdir: Path, project: Path, prior: dict | None) -> str:
    """Human-readable diff against the copy we already have, if any."""
    prior_files = (prior or {}).get("files", {})
    chunks = []
    for f in manifest["files"]:
        old_rel = prior_files.get(f["path"], {}).get("installed_at")
        if not old_rel:
            continue
        old_path = project / old_rel
        old = old_path.read_bytes() if old_path.is_file() else b""
        chunks.append(diff_bytes(f["path"], old, (qdir / f["path"]).read_bytes()))
    return "".join(chunks)


def preview(manifest: dict, qdir: Path) -> dict[str, str]:
    """The start of each text file, so a judge can read the code it is deciding on."""
    out = {}
    for f in manifest["files"][:PREVIEW_FILES]:
        try:
            # Decode first, then cut: cutting bytes could split a multi-byte character.
            out[f["path"]] = (qdir / f["path"]).read_bytes().decode()[:PREVIEW_CHARS]
        except UnicodeDecodeError:
            out[f["path"]] = "(binary)"
    return out


def assess(manifest: dict, mhash: str, qdir: Path, project: Path, prior: dict | None,
           run: bool, problems: list[str], intent: str = "") -> Report:
    problems = list(problems)
    if not problems:
        for f in manifest["files"]:
            if _hash(qdir / f["path"]) != f["sha256"]:
                problems.append(f"hash mismatch after unpack: {f['path']}")
    report = Report(manifest_hash=mhash, hashes_ok=not problems, tests_ran=False,
                    tests_passed=None, problems=problems, intent=intent,
                    source=f"{manifest['owner']}/{manifest['source_id']} v{manifest['version']}")
    if problems:
        return report
    report.preview = preview(manifest, qdir)
    report.conflicts = find_conflicts(manifest, project, prior)
    report.diff = text_diff(manifest, qdir, project, prior)
    report.missing_deps = missing_deps(manifest.get("deps", []))
    if run:
        report.tests_ran = True
        report.tests_passed, report.test_output = run_tests(manifest["tests"].get("cmd"), qdir)
    return report
