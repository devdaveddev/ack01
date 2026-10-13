# 01 · `ack/crypto.py`, line by line

**Job of this file:** give every agent an identity, derive a shared secret with each peer, sign manifests, and encrypt messages and files. Every other file that needs security calls into this one.

**Concepts first, in plain words:**

- **Hash (SHA-256):** a fingerprint of some bytes. The same bytes always give the same 64-hex-character fingerprint; any change gives a completely different one, and you can't work backwards. We use it to name blobs and detect tampering.
- **Public/private key pair:** the private key stays secret, and the public key can be shared freely.
- **X25519 (key agreement):** A's private key + B's public key gives the *same* secret as B's private key + A's public key. Nobody watching can compute it. That gives the two agents a shared secret without ever sending it.
- **Ed25519 (signatures):** sign with the private key; anyone with the public key can check that the signature came from that key and that the data wasn't changed.
- **HKDF:** turns the raw shared secret into a clean 32-byte key, and lets us label it for one purpose.
- **AES-GCM (authenticated encryption):** encrypts data, and also detects any change to the ciphertext, or to extra "associated data" (AAD) that's checked but not encrypted. It needs a **nonce**, a number that must never repeat under the same key.
- **HMAC:** a keyed hash. Only someone with the key can produce it.

Primer references: [imports](00-python-primer.md#files-modules-imports), [bytes](00-python-primer.md#values-and-types-youll-see), [classes](00-python-primer.md#classes-and-objects), [decorators](00-python-primer.md#decorators-something-above-a-def-or-class), [exceptions](00-python-primer.md#errors-exceptions).

---

## Lines 1–11 · module docstring

Documentation only. It says what the two keys are for and why files use convergent encryption. Nothing here runs.

## Lines 13–25 · imports

```python
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
```

- **13** `base64`: turns bytes into printable text, so keys and ciphertext can live inside JSON.
- **14** `hashlib`: the standard library's SHA-256.
- **15** `hmac`: keyed hashing (used for content keys).
- **16** `json`: reading and writing JSON.
- **17** `os`: low-level file creation (to set permissions atomically) and secure random bytes (`os.urandom`).
- **18** `Path`: file paths as objects (see primer).
- **20** The two error types the `cryptography` library raises: a bad signature, and a ciphertext that failed its integrity check ("tag").
- **21** `hashes` (SHA256 for HKDF) and `serialization` (how to export keys as raw bytes).
- **22–23** The key classes for signatures (Ed25519) and key agreement (X25519).
- **24** `AESGCM`: authenticated encryption.
- **25** `HKDF`: key derivation.

> `hazmat` stands for "hazardous materials". It's the low-level layer of the library, used here on purpose with standard, well-reviewed constructions only.

## Lines 28–29 · our own error type

```python
class CryptoError(Exception):
    pass
```

- **28** Defines a new kind of error, based on Python's `Exception`.
- **29** `pass`: the class needs no extra behaviour. Other files catch `CryptoError` to mean "something about the crypto was wrong", whatever the library-level reason.

## Lines 32–33 · fingerprint some bytes

```python
def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
```

- **32** Takes bytes and returns a string.
- **33** `hashlib.sha256(data)` computes the hash, and `.hexdigest()` returns it as 64 hex characters (`"3f9a…"`). Blob names, manifest hashes and file hashes all come from here.

## Lines 36–38 · canonical JSON

```python
def canonical(obj) -> bytes:
    """Deterministic JSON bytes, used for hashing, signing and AEAD associated data."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
```

- **38** Converts any JSON-able object to bytes in exactly one way:
  - `sort_keys=True` puts keys in alphabetical order, so `{"a":1,"b":2}` and `{"b":2,"a":1}` give the same bytes.
  - `separators=(",", ":")` removes the optional spaces.
  - `ensure_ascii=False` keeps non-English characters as they are, instead of `\u` escapes.
  - `.encode()` turns the text into bytes.

  **Why it matters:** a signature or hash over JSON is useless unless both sides produce identical bytes for the same data.

## Lines 41–46 · base64 without padding

```python
def b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
```

- **42** Encode bytes with the URL-safe alphabet (`-` and `_` instead of `+` and `/`), so the result can go in a QR link. `.rstrip(b"=")` drops the `=` padding, and `.decode()` gives a `str`.
- **46** Reverses it. Base64 text must be a multiple of 4 characters long, so `-len(text) % 4` works out how many `=` to put back (0–3), then it decodes.

## Lines 49–54 · how to export keys

```python
_RAW_PUBLIC = dict(encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)
_RAW_PRIVATE = dict(
    encoding=serialization.Encoding.Raw,
    format=serialization.PrivateFormat.Raw,
    encryption_algorithm=serialization.NoEncryption(),
)
```

- **49** Settings for exporting a *public* key as its raw 32 bytes. Stored as a dict so it can be spread into calls with `**` (see primer).
- **50–54** The same for *private* keys. `NoEncryption()` means the exported bytes aren't password-protected. The file is protected by permissions instead (line 77).
- The leading `_` marks these as internal to this module.

## Lines 57–61 · the Identity class

```python
class Identity:
    def __init__(self, kx: X25519PrivateKey, sig: Ed25519PrivateKey):
        self.kx = kx
        self.sig = sig
        self._pair_keys: dict[str, bytes] = {}
```

- **57** An `Identity` is one agent's pair of private keys.
- **58** The constructor takes the key-agreement key (`kx`) and the signing key (`sig`).
- **59–60** Stores them on the object.
- **61** An empty cache: peer public key → derived pair key. Deriving is cheap but not free, and an agent may talk to the same peer thousands of times.

## Lines 63–65 · make a brand-new identity

```python
    @classmethod
    def generate(cls) -> "Identity":
        return cls(X25519PrivateKey.generate(), Ed25519PrivateKey.generate())
```

- **63** `@classmethod`: called on the class (`Identity.generate()`), not on an existing object.
- **65** Creates two fresh random private keys and builds an `Identity` from them. `cls` is `Identity` itself.

## Lines 67–86 · load from disk, or create and save

```python
    @classmethod
    def load_or_create(cls, path: Path) -> "Identity":
        if path.exists():
            data = json.loads(path.read_text())
            return cls(
                X25519PrivateKey.from_private_bytes(b64d(data["kx"])),
                Ed25519PrivateKey.from_private_bytes(b64d(data["sig"])),
            )
        identity = cls.generate()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(
                {
                    "kx": b64e(identity.kx.private_bytes(**_RAW_PRIVATE)),
                    "sig": b64e(identity.sig.private_bytes(**_RAW_PRIVATE)),
                },
                f,
            )
        return identity
```

- **69** If this project already has an identity file…
- **70** …read it and parse the JSON into a dict.
- **71–74** …rebuild both private keys from their base64 text and return the `Identity`. The agent keeps the same keys across restarts, so peers still recognise it.
- **75** Otherwise, make a new identity.
- **76** Make sure the `.ack/` folder exists.
- **77** Open the file the careful way:
  - `O_WRONLY`: open for writing.
  - `O_CREAT`: create the file.
  - `O_EXCL`: **fail if it already exists**. This prevents a race where two processes each create an identity and one silently overwrites the other.
  - `0o600`: permissions so only the owner can read or write it, and **the file is created with them already set**. There's no moment when it exists with looser permissions.
- **78** Wrap the raw file descriptor as a normal Python file. `with` closes it afterwards.
- **79–85** Write `{"kx": ..., "sig": ...}`: each private key exported raw (`**_RAW_PRIVATE` spreads the settings) and base64-encoded.
- **86** Return the new identity.

## Lines 88–92 · the public half

```python
    def public(self) -> dict:
        return {
            "kx": b64e(self.kx.public_key().public_bytes(**_RAW_PUBLIC)),
            "sig": b64e(self.sig.public_key().public_bytes(**_RAW_PUBLIC)),
        }
```

- **90** Get the public key that matches the private `kx`, export it raw, and base64 it.
- **91** The same for the signing key.

This dict is what goes into QR invites and PAIR messages, and what peers store.

## Lines 94–105 · the shared pair key

```python
    def pair_key(self, peer_public: dict) -> bytes:
        cached = self._pair_keys.get(peer_public["kx"])
        if cached:
            return cached
        try:
            shared = self.kx.exchange(X25519PublicKey.from_public_bytes(b64d(peer_public["kx"])))
        except ValueError as e:
            raise CryptoError(f"bad peer key: {e}") from e
        info = b"ack/pair/v1|" + "|".join(sorted([self.public()["kx"], peer_public["kx"]])).encode()
        key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(shared)
        self._pair_keys[peer_public["kx"]] = key
        return key
```

- **95–97** If we already derived a key for this peer, return it.
- **99** Decode the peer's public key and run X25519: **our private key + their public key → the shared secret**. The peer computes the exact same secret from their private key + our public key.
- **100–101** A malformed key raises `ValueError`. We turn it into our own `CryptoError`, keeping the original as the cause (`from e`).
- **102** Build HKDF's "info" label:
  - `b"ack/pair/v1|"` names the purpose and version, so this key can never be confused with a key derived for something else.
  - Both public keys are added, **sorted**, so both sides build the same label regardless of who is "us" and who is "them".
- **103** HKDF with SHA-256 turns the raw secret into exactly 32 bytes, the size of an AES-256 key.
- **104–105** Cache it and return it.

## Lines 107–108 · sign

```python
    def sign(self, data: bytes) -> str:
        return b64e(self.sig.sign(data))
```

- **108** Sign the bytes with our Ed25519 private key; the 64-byte signature comes back as base64 text. Used to sign manifests.

## Lines 111–116 · verify a signature

```python
def verify(public: dict, data: bytes, signature: str) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(b64d(public["sig"])).verify(b64d(signature), data)
        return True
    except (InvalidSignature, ValueError, KeyError):
        return False
```

- **113** Rebuild the signer's public key from their public dict, then check the signature against the data. The library *raises* on failure rather than returning `False`.
- **114** Reaching this line means the check passed.
- **115–116** A bad signature, malformed base64 or key, or a missing `"sig"` field all mean "not verified". This function never crashes; it just answers yes or no.

## Lines 119–120 · short key fingerprint

```python
def fingerprint(public: dict) -> str:
    return sha256_hex(canonical(public))[:16]
```

- **120** Hash the public keys and keep the first 16 hex characters: a short, human-comparable ID for a key.

## Lines 123–125 · encrypt a message

```python
def seal(key: bytes, plaintext: bytes, aad: bytes = b"") -> bytes:
    nonce = os.urandom(12)
    return nonce + AESGCM(key).encrypt(nonce, plaintext, aad)
```

- **124** A fresh, random 12-byte nonce for every message. With random 96-bit nonces, a repeat is practically impossible at our message volumes.
- **125** Encrypt. The result is the ciphertext plus a 16-byte integrity tag that also covers the `aad` (associated data). We put the nonce in front so the receiver knows which one was used. The nonce isn't secret.

## Lines 128–132 · decrypt a message

```python
def unseal(key: bytes, blob: bytes, aad: bytes = b"") -> bytes:
    try:
        return AESGCM(key).decrypt(blob[:12], blob[12:], aad)
    except (InvalidTag, ValueError) as e:
        raise CryptoError("message failed authentication") from e
```

- **130** Split off the first 12 bytes (the nonce) from the rest (ciphertext + tag), then decrypt. If **one bit** of the ciphertext or the `aad` changed, or the key is wrong, decryption fails instead of returning garbage.
- **131–132** Report that as our own error.

## Lines 135–136 · per-file content key

```python
def _content_key(key: bytes, plain_hash: str) -> bytes:
    return hmac.new(key, b"ack/blob/v1|" + plain_hash.encode(), hashlib.sha256).digest()
```

- **136** HMAC-SHA256, keyed by the pair key, over a label plus the file's own hash, gives a 32-byte key **specific to this pair and this exact file content**. Without the pair key, nobody can compute it, even knowing the file.

## Lines 139–142 · encrypt a file (convergent)

```python
def seal_content(key: bytes, plaintext: bytes) -> bytes:
    # Each content key encrypts exactly one plaintext, so a fixed nonce is safe.
    # The trade-off: the broker can tell when the same file is sent twice to the same peer.
    return AESGCM(_content_key(key, sha256_hex(plaintext))).encrypt(bytes(12), plaintext, None)
```

- **142**, read from the inside out:
  1. Hash the file.
  2. Derive the content key from the pair key and that hash.
  3. Encrypt with an all-zero nonce (`bytes(12)`) and no AAD.

  **Why a fixed nonce is safe here:** the rule is "never reuse a nonce *with the same key for different data*". Each content key only ever encrypts one specific file, so that can't happen.

  **What this buys:** the same file always produces the same ciphertext, and therefore the same blob name. Unchanged files in an update are already on the broker and in the receiver's cache, so they never move again.

## Lines 145–152 · decrypt a file and prove it's the right one

```python
def unseal_content(key: bytes, plain_hash: str, blob: bytes) -> bytes:
    try:
        plaintext = AESGCM(_content_key(key, plain_hash)).decrypt(bytes(12), blob, None)
    except (InvalidTag, ValueError) as e:
        raise CryptoError("blob failed authentication") from e
    if sha256_hex(plaintext) != plain_hash:
        raise CryptoError("blob content does not match manifest hash")
    return plaintext
```

- **145** `plain_hash` comes from the **owner's signed manifest**, not from the blob.
- **147** Re-derive the content key from that hash and decrypt. A wrong key or a changed blob fails here.
- **150–151** Belt and braces: hash what came out and compare it with the manifest. This proves the plaintext is exactly the file the owner signed for.
- **152** Only now is it handed back.
