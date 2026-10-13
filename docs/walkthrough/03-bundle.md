# 03 · `ack/bundle.py`, line by line

**Job of this file:** turn a part of a project into a **signed manifest** plus one **encrypted blob per file**, check a received manifest, and unpack blobs back into files safely.

Primer references: [paths](00-python-primer.md#paths-pathlib), [regex](00-python-primer.md#regular-expressions-re), [comprehensions](00-python-primer.md#comprehensions-building-a-collection-in-one-line).

---

## Lines 8–14 · imports

```python
import re
import time
from collections.abc import Mapping
from pathlib import Path, PurePosixPath

from .crypto import CryptoError, Identity, canonical, seal_content, sha256_hex, unseal_content, verify
```

- **8** Regular expressions, to validate hash strings.
- **9** For the manifest's `issued` timestamp.
- **10** `Mapping` is a type hint meaning "anything dict-like you can read from".
- **11** `PurePosixPath` handles paths as text with `/` separators, without touching the disk. Manifest paths are always `/`-style, whatever the OS.
- **13** Functions from our crypto module.

## Lines 15–17 · constants

```python
MANIFEST_KIND = "ack.manifest/v1"
HEX64 = re.compile(r"^[0-9a-f]{64}$")
PART_ID = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9_.-]{0,63}$")
```

- **15** A tag stored in every manifest, so something that isn't one (or is a future version) is never mistaken for a manifest.
- **16** A compiled pattern: `^` start, exactly 64 lowercase hex characters, `$` end. That's what every SHA-256 hex string looks like.
- **17** What a part id may look like: 1–64 letters, digits, `_`, `-` or `.`, and **not starting with a dot**. Part ids become folder names (`vendor/<peer>/<id>`), so `../x` or `.git` must never pass. Added in the bug-fix pass (bug B).

## Lines 20–21 · error type

```python
class BundleError(Exception):
    pass
```

Raised for anything wrong with a manifest or its blobs.

## Lines 24–35 · path safety

```python
def safe_relpath(path: str) -> str:
    """Reject anything that could escape the unpack directory."""
    if not isinstance(path, str) or not path or "\\" in path or "\0" in path:
        raise BundleError(f"unsafe path {path!r}")
    pure = PurePosixPath(path)
    if pure.is_absolute() or any(part in ("", ".", "..") for part in pure.parts):
        raise BundleError(f"unsafe path {path!r}")
    # No hidden files or folders: adopting ".git/hooks/pre-commit" or ".github/workflows/x.yml"
    # would make the receiver's own tools run the code, and ".ack/" is the agent's own state.
    if any(part.startswith(".") for part in pure.parts):
        raise BundleError(f"hidden path not allowed {path!r}")
    return str(pure)
```

**Two threats.** First, a malicious manifest names a file `../../.bashrc` or `/etc/cron.d/x`, and unpacking writes it outside quarantine. Second, a path that *stays* inside the project but lands somewhere your own tools execute: `.git/hooks/pre-commit` runs on your next commit, `.github/workflows/x.yml` runs in CI, and `.ack/state.json` is the agent's own memory. If you don't already have a file there, there's no conflict, so ADOPT would install it straight into place.

- **26** Reject anything that isn't a string, an empty string, any backslash (Windows-style separators could slip past `/` checks), and the NUL byte (`\0`), which can truncate paths at the operating-system level.
- **28** Parse it as a `/`-separated path.
- **29–30** Reject:
  - an absolute path (starts with `/`)
  - any part that is empty, `.` (current folder) or `..` (parent folder)

  `pure.parts` splits `a/b/c.py` into `("a", "b", "c.py")`, and `any(...)` is true if any one part is bad.
- **31–34** Reject **any hidden part**, i.e. any folder or file name starting with a dot. This closes the second threat for every tool that keeps config in dot-folders, not just git. The cost is that bundles can't carry dotfiles like `.gitignore`, which is an acceptable trade. Added in the bug-fix pass (bug A).
- **35** Return the cleaned path as a string.

## Lines 38–39 · the manifest without its signature

```python
def unsigned(manifest: dict) -> dict:
    return {k: v for k, v in manifest.items() if k != "sig"}
```

- **39** Copy every field except `sig`. A signature can't sign itself, so signing and verifying both use this version.

## Lines 42–43 · the manifest's identity

```python
def manifest_hash(manifest: dict) -> str:
    return sha256_hex(canonical(unsigned(manifest)))
```

- **43** Hash the canonical JSON of the unsigned manifest. This hash *is* the manifest's ID: in ACCEPT, BUNDLE, ACK, provenance logs and the registry.

## Lines 46–47 · list the blobs

```python
def blob_ids(manifest: dict) -> list[str]:
    return [f["blob"] for f in manifest["files"]]
```

- **47** A list comprehension: one blob ID per file entry.

## Lines 50–70 · pack a part

```python
def pack(identity: Identity, pair_key: bytes, owner: str, root: Path, entry, test_status: str):
    """Returns (signed manifest, {blob_id: ciphertext})."""
    files, blobs = [], {}
    for rel in sorted(entry.files):
        data = (root / safe_relpath(rel)).read_bytes()
        ciphertext = seal_content(pair_key, data)
        blob_id = sha256_hex(ciphertext)
        blobs[blob_id] = ciphertext
        files.append({"path": rel, "sha256": sha256_hex(data), "size": len(data), "blob": blob_id})
    manifest = {
        "kind": MANIFEST_KIND,
        "owner": owner,
        "source_id": entry.id,
        "version": entry.version,
        "files": files,
        "deps": list(entry.deps),
        "tests": {"cmd": entry.tests, "status": test_status},
        "issued": time.time(),
    }
    manifest["sig"] = identity.sign(canonical(manifest))
    return manifest, blobs
```

- **50** Inputs:
  - `identity`: the owner's keys, to sign with
  - `pair_key`: the key shared with this particular receiver
  - `root`: the part's folder
  - `entry`: the memory entry describing the part
  - `test_status`: e.g. `"passed"`
- **52** Start with an empty file list and an empty blob map.
- **53** Go through the part's files in **sorted** order, so the same part always produces the same manifest layout.
- **54** Read the file's bytes. The path is checked even on the owner's side, in case the memory entry itself is bad.
- **55** Encrypt it with convergent encryption (see 01-crypto).
- **56** The blob's name is **the hash of its ciphertext**. The broker can check it without the key.
- **57** Keep the ciphertext, keyed by its name, for uploading later.
- **58** Record the file in the manifest:
  - its path
  - the hash of the **plaintext**, which the receiver checks after decrypting
  - its size
  - its blob name
- **59–68** Build the manifest:
  - the kind tag, the owner, which part, which version
  - the files list
  - a copy of the dependencies
  - the test command and the owner's test result
  - when it was issued
- **69** Sign the canonical JSON of everything above, and add the signature as `sig`.
- **70** Return both: the manifest goes in the OFFER message, and the blobs go to the broker on ACCEPT.

## Lines 73–97 · verify a received manifest

```python
def verify_manifest(manifest: dict, owner: str, owner_public: dict) -> str:
    """Checks shape, owner and signature. Returns the manifest hash."""
    try:
        if manifest.get("kind") != MANIFEST_KIND:
            raise BundleError("not a manifest")
        if not isinstance(manifest.get("source_id"), str) or not PART_ID.match(manifest["source_id"]):
            # The id becomes a folder name (vendor/<peer>/<id>), so "../x" must never get through.
            raise BundleError(f"bad part id {manifest.get('source_id')!r}")
        if manifest.get("owner") != owner:
            raise BundleError(f"manifest owner {manifest.get('owner')!r} is not the sender {owner!r}")
        if not verify(owner_public, canonical(unsigned(manifest)), manifest.get("sig", "")):
            raise BundleError("manifest signature is invalid")
        paths = set()
        for f in manifest["files"]:
            path = safe_relpath(f["path"])
            if path in paths:
                raise BundleError(f"duplicate path {path}")
            paths.add(path)
            if not HEX64.match(f["blob"]) or not HEX64.match(f["sha256"]):
                raise BundleError(f"bad hash for {path}")
        if not isinstance(manifest["version"], int) or not isinstance(manifest["tests"], dict):
            raise BundleError("bad manifest fields")
    except (KeyError, TypeError) as e:
        raise BundleError(f"malformed manifest: {e}") from e
    return manifest_hash(manifest)
```

- **73** `owner` is who *sent* the message; `owner_public` is the key we pinned for them at pairing.
- **76–77** Must carry the manifest tag.
- **78–80** The part id must be a string matching `PART_ID`. The receiver later uses it as a folder name (`vendor/<peer>/<id>`), so an id like `../../etc` could otherwise write outside `vendor/`. Added in the bug-fix pass (bug B).
- **81–82** The manifest must claim the **same owner as the sender**. Otherwise beta could forward something signed by someone else as if it were its own.
- **83–84** The signature must verify against the sender's pinned key, over the canonical unsigned manifest. Any change to any field, even one byte, fails here.
- **85** A set to catch duplicate paths.
- **86–92** For every file:
  - **87** the path must be safe (no escaping, nothing hidden)
  - **88–90** each path may appear only once, since two entries for the same path could hide one file behind another
  - **91–92** both hashes must look like real SHA-256 hex strings
- **93–94** The version must be an integer and `tests` must be a dict.
- **95–96** If any expected key was missing (`KeyError`) or had the wrong shape (`TypeError`), report it as a malformed manifest instead of crashing.
- **97** Everything passed: return the manifest's hash (its ID).

## Lines 100–116 · unpack into quarantine

```python
def unpack(pair_key: bytes, manifest: dict, blobs: Mapping[str, bytes], dest: Path) -> None:
    dest = dest.resolve()
    for f in manifest["files"]:
        ciphertext = blobs.get(f["blob"])
        if ciphertext is None:
            raise BundleError(f"missing blob for {f['path']}")
        if sha256_hex(ciphertext) != f["blob"]:
            raise BundleError(f"blob hash mismatch for {f['path']}")
        try:
            data = unseal_content(pair_key, f["sha256"], ciphertext)
        except CryptoError as e:
            raise BundleError(f"{f['path']}: {e}") from e
        target = (dest / safe_relpath(f["path"])).resolve()
        if not target.is_relative_to(dest):
            raise BundleError(f"path escapes quarantine: {f['path']}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
```

- **101** Make `dest` (the quarantine folder) absolute, with any `..` or symlinks worked out, so the containment check on line 105 is reliable.
- **102** Go through every file in the manifest.
- **103–105** Find its ciphertext. If it's missing, stop.
- **106–107** **Check 1:** the ciphertext's hash must equal the blob name the owner signed.
- **108–111** **Check 2:** decrypt with the plaintext hash the owner signed. Decryption itself checks integrity, and `unseal_content` also re-hashes the result. A crypto failure becomes a `BundleError` naming the file.
- **112** Build the target path, then `.resolve()` it. If the path somehow escapes via a symlink, this exposes it.
- **113–114** **Check 3:** the final location must still be inside quarantine.
- **115** Create any sub-folders the file needs.
- **116** Write it. Only bytes that passed all three checks ever reach the disk.

## Lines 119–123 · what changed between versions

```python
def changed_paths(old_files: Mapping[str, str], manifest: dict) -> list[str]:
    """old_files maps path -> sha256 of the version the receiver already has."""
    new_files = {f["path"]: f["sha256"] for f in manifest["files"]}
    paths = old_files.keys() | new_files.keys()
    return sorted(p for p in paths if old_files.get(p) != new_files.get(p))
```

- **121** A path → hash map for the new version.
- **122** Every path in either version (set union), so added and removed files are both included.
- **123** Keep paths whose hash differs. A file missing on one side gives `None` from `.get`, which also counts as different. Sorted, for stable output. This becomes the `changed` field of UPDATE_AVAILABLE.
