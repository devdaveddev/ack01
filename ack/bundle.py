"""Bundle packer. A bundle is a signed manifest plus one encrypted blob per file.

Blobs are named by the hash of their ciphertext, so the broker can check what it
stores without being able to read it, and receivers only download blobs they
don't already have.
"""

import re
import time
from collections.abc import Mapping
from pathlib import Path, PurePosixPath

from .crypto import CryptoError, Identity, canonical, seal_content, sha256_hex, unseal_content, verify

MANIFEST_KIND = "ack.manifest/v1"
HEX64 = re.compile(r"^[0-9a-f]{64}$")
PART_ID = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9_.-]{0,63}$")


class BundleError(Exception):
    pass


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


def unsigned(manifest: dict) -> dict:
    return {k: v for k, v in manifest.items() if k != "sig"}


def manifest_hash(manifest: dict) -> str:
    return sha256_hex(canonical(unsigned(manifest)))


def blob_ids(manifest: dict) -> list[str]:
    return [f["blob"] for f in manifest["files"]]


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


def changed_paths(old_files: Mapping[str, str], manifest: dict) -> list[str]:
    """old_files maps path -> sha256 of the version the receiver already has."""
    new_files = {f["path"]: f["sha256"] for f in manifest["files"]}
    paths = old_files.keys() | new_files.keys()
    return sorted(p for p in paths if old_files.get(p) != new_files.get(p))
