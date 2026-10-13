# 08 · `ack/quarantine.py`, line by line

**Job of this file:** everything that happens to a received bundle after it's unpacked into quarantine and before any decision. It re-checks hashes, finds conflicts, checks dependencies, builds a diff and file previews, and runs the tests in a sandbox, but only when told it has approval.

Primer references: [paths](00-python-primer.md#paths-pathlib), [unpacking with \*](00-python-primer.md#functions), [exceptions](00-python-primer.md#errors-exceptions).

---

## Lines 9–18 · imports

```python
import difflib
import importlib.metadata
import os
import re
import subprocess
import sys
from pathlib import Path

from .crypto import sha256_hex
from .judge import Report
```

- **9** Builds unified diffs (`---`/`+++`/`@@` format).
- **10** Asks "which version of package X is installed here?".
- **11** `os.name`, to check we're on a Unix-like system.
- **12** Regex, to pull the package name out of a requirement string.
- **13** Runs the test command as a separate process.
- **14** `sys.executable`: the path to the Python that's running us.
- **17–18** Hashing, and the `Report` this file fills in.

## Lines 20–23 · settings

```python
ALLOWED_RUNNERS = {"python", "python3"}
TEST_TIMEOUT = 30
PREVIEW_CHARS = 4000
PREVIEW_FILES = 20
```

- **20** The only test commands accepted. A manifest saying `["bash", "-c", "..."]` is refused outright.
- **21** Seconds before a test run is killed.
- **22–23** How much of each file, and how many files, go into the judge's preview.

## Lines 26–35 · resource limits for the test process

```python
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
```

This isn't a function: it's a **small Python program stored as a string**. Several string pieces side by side in parentheses join into one. The test run starts a fresh Python with `-c _LIMITED`, which:

- **30** imports what it needs
- **31** caps CPU time at 20 seconds, after which the operating system kills it. Each pair is (soft limit, hard limit).
- **32** caps memory at 1 GB. `1 << 30` = 2³⁰ bytes.
- **33** caps any single file it writes at 50 MB. `50 << 20` = 50 × 2²⁰.
- **34** `os.execv` **replaces this launcher process with the real test command**, passed in as its remaining arguments. Limits survive an `exec`, so the tests run under them.

**Why a launcher?** The earlier version set the limits with `subprocess.run(preexec_fn=...)`, a hook that runs in the child just before the command starts. Python's documentation warns it can deadlock if the parent program has threads, and the web app has several. Doing it inside the child, as ordinary code, is thread-safe. (Bug H in the bug-fix pass.)

## Lines 38–58 · run the tests

```python
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
```

- **38** Returns `(passed?, output)`.
- **39–40** No command, or one that doesn't start with `python`/`python3` → refuse without running anything.
- **41** Build the real command:
  - replace whatever `python` the manifest named with **our own** interpreter (`sys.executable`), so the manifest can't point at some other binary
  - add `-I` (isolated mode): Python ignores environment variables like `PYTHONPATH` and won't import from the user's site-packages
  - keep the rest of the arguments (`*cmd[1:]` spreads them in)
- **42–43** On Unix, put the launcher in front: `python -I -c <_LIMITED> <the real command…>`. The launcher sets the limits, then `exec`s the real command (see above).
- **44** A **minimal environment**: the tests inherit none of our variables (API keys, tokens). `HOME` points at quarantine; `PYTHONDONTWRITEBYTECODE` stops `.pyc` files being written.
- **46–55** `subprocess.run` starts the process and waits:
  - **48** working directory = the quarantine folder
  - **49** the minimal environment
  - **50** no input; it can't wait for keyboard input
  - **51–52** capture stdout and stderr as text
  - **53** kill it after `timeout` seconds
  - **54** put it in its own process session, so it's separated from our terminal and signals
- **56–57** Too slow → fail.
- **58** Exit code 0 means the tests passed. Return that and the last 4000 characters of output.

> **Honest limit:** this reduces risk but doesn't isolate. The code can still read files and use the network. A container or VM should replace this function before real untrusted peers are involved.

## Lines 61–62 · hash a file if it exists

```python
def _hash(path: Path) -> str | None:
    return sha256_hex(path.read_bytes()) if path.is_file() else None
```

The file's hash, or `None` if there's no file.

## Lines 65–80 · find conflicts

```python
def find_conflicts(manifest: dict, project: Path, prior: dict | None) -> list[str]:
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
```

- **65** `prior` is our install record for this same part from an earlier version, or `None` if this is the first time.
- **70** `(prior or {})` treats "no prior" as an empty dict, so `.get` is always safe. We want its `files` map.
- **72** For each incoming file…
- **73** …hash our file at the same path, if we have one.
- **74–75** No file there, or an identical one → no conflict.
- **76–78** **The important exception.** If our file is the **unmodified copy we adopted from this same source last time** (installed at that path, with that hash), replacing it is just an update, not a conflict. Without this, every update to an adopted part would wrongly become ADAPT.
- **79** Otherwise it's our own different file → conflict.

## Lines 83–102 · missing dependencies

```python
def missing_deps(deps: list[str]) -> list[str]:
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
            continue
        try:
            if not Requirement(req).specifier.contains(have, prereleases=True):
                missing.append(f"{req} (have {have})")
        except InvalidRequirement:
            missing.append(f"{req} (unparseable)")
    return missing
```

- **87** Get the package name from a requirement like `requests>=2.31; python_version>"3"`: split at the first `<`, `>`, `=`, `!`, `~`, `;`, `[` or whitespace, and take the part before it (`requests`).
- **89** Look up the installed version.
- **90–92** Not installed → missing.
- **93–96** `packaging` is the standard library-for-requirements. If it isn't available, we can only check presence, not version ranges, so skip to the next requirement.
- **98–99** Parse the requirement and ask whether the installed version fits the range (`>=2.31` etc.). If not, report it along with what we have.
- **100–101** A malformed requirement is reported, never ignored.

## Lines 105–111 · diff two byte strings

```python
def diff_bytes(path: str, old: bytes, new: bytes) -> str:
    try:
        old_lines = old.decode().splitlines(keepends=True)
        new_lines = new.decode().splitlines(keepends=True)
    except UnicodeDecodeError:
        return f"binary file {path} changed\n"
    return "".join(difflib.unified_diff(old_lines, new_lines, f"a/{path}", f"b/{path}"))
```

- **107–108** Decode both as text and split into lines, keeping the line endings so the diff reproduces them exactly.
- **109–110** Not text → just say a binary file changed.
- **111** Produce a unified diff with `a/` and `b/` labels, like `git diff`, and join it into one string. Used by the receiver (here) and the owner (for UPDATE_AVAILABLE).

## Lines 114–125 · diff against what we already hold

```python
def text_diff(manifest: dict, qdir: Path, project: Path, prior: dict | None) -> str:
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
```

- **119** Where did we install this file last time? It could be the project root (adopt) or `vendor/...` (adapt).
- **120–121** Never installed → nothing to diff against.
- **122–123** Read our current copy. If it was deleted, treat it as empty.
- **124** Diff our copy against the new one in quarantine. This is the receiver's **own** diff, built from real files, so it doesn't rely on the diff the owner sent.

## Lines 128–137 · previews for the judge

```python
def preview(manifest: dict, qdir: Path) -> dict[str, str]:
    out = {}
    for f in manifest["files"][:PREVIEW_FILES]:
        try:
            # Decode first, then cut: cutting bytes could split a multi-byte character.
            out[f["path"]] = (qdir / f["path"]).read_bytes().decode()[:PREVIEW_CHARS]
        except UnicodeDecodeError:
            out[f["path"]] = "(binary)"
    return out
```

- **131** At most 20 files.
- **134** Read the bytes, decode them to text, **then** take the first 4000 characters. (An earlier version cut the bytes first, which could split an accented character in half and mislabel a text file as binary.)
- **135–136** Files that aren't text are marked `(binary)`.

## Lines 140–159 · assess: put it all together

```python
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
```

- **140–141** Inputs:
  - `run`: whether the user approved running the tests
  - `problems`: anything already wrong from fetching or unpacking
  - `intent`: what we said we'd use the part for
- **142** Copy the list, so we don't modify the caller's.
- **143–146** If unpacking succeeded, **re-hash every file on disk** against the manifest. Belt and braces after `unpack`'s own checks.
- **147–149** Start the report:
  - `hashes_ok` is true only when there are no problems
  - tests are marked "not run" for now
  - `source` is the readable label
- **150–151** Any problem → stop here. There's no point checking conflicts or running tests on bad files, and a judge will reject on `problems`.
- **152** Previews for the judge.
- **153** Conflicts with our files.
- **154** A diff against what we already hold.
- **155** Dependency check. `.get("deps", [])` handles a manifest with no deps field.
- **156–158** **Only if approved:** run the tests in quarantine and record the result. Without approval, `tests_ran` stays `False` and every judge rejects.
- **159** Return the finished report.
