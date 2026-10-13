"""Keys, hashing and encryption. Plain code, no AI.

Each agent has two long-lived keys:
  kx  - X25519, used to derive a shared pair key with each peer
  sig - Ed25519, used to sign manifests so provenance survives past the pair

Messages are sealed with AES-GCM under the pair key. File contents use
convergent encryption (key derived from pair key + content hash), so the same
file sent to the same peer always produces the same blob. That is what lets an
update ship only the layers that changed.
"""

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


class CryptoError(Exception):
    pass


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(obj) -> bytes:
    """Deterministic JSON bytes, used for hashing, signing and AEAD associated data."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


_RAW_PUBLIC = dict(encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)
_RAW_PRIVATE = dict(
    encoding=serialization.Encoding.Raw,
    format=serialization.PrivateFormat.Raw,
    encryption_algorithm=serialization.NoEncryption(),
)


class Identity:
    def __init__(self, kx: X25519PrivateKey, sig: Ed25519PrivateKey):
        self.kx = kx
        self.sig = sig
        self._pair_keys: dict[str, bytes] = {}

    @classmethod
    def generate(cls) -> "Identity":
        return cls(X25519PrivateKey.generate(), Ed25519PrivateKey.generate())

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

    def public(self) -> dict:
        return {
            "kx": b64e(self.kx.public_key().public_bytes(**_RAW_PUBLIC)),
            "sig": b64e(self.sig.public_key().public_bytes(**_RAW_PUBLIC)),
        }

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

    def sign(self, data: bytes) -> str:
        return b64e(self.sig.sign(data))


def verify(public: dict, data: bytes, signature: str) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(b64d(public["sig"])).verify(b64d(signature), data)
        return True
    except (InvalidSignature, ValueError, KeyError):
        return False


def fingerprint(public: dict) -> str:
    return sha256_hex(canonical(public))[:16]


def seal(key: bytes, plaintext: bytes, aad: bytes = b"") -> bytes:
    nonce = os.urandom(12)
    return nonce + AESGCM(key).encrypt(nonce, plaintext, aad)


def unseal(key: bytes, blob: bytes, aad: bytes = b"") -> bytes:
    try:
        return AESGCM(key).decrypt(blob[:12], blob[12:], aad)
    except (InvalidTag, ValueError) as e:
        raise CryptoError("message failed authentication") from e


def _content_key(key: bytes, plain_hash: str) -> bytes:
    return hmac.new(key, b"ack/blob/v1|" + plain_hash.encode(), hashlib.sha256).digest()


def seal_content(key: bytes, plaintext: bytes) -> bytes:
    # Each content key encrypts exactly one plaintext, so a fixed nonce is safe.
    # The trade-off: the broker can tell when the same file is sent twice to the same peer.
    return AESGCM(_content_key(key, sha256_hex(plaintext))).encrypt(bytes(12), plaintext, None)


def unseal_content(key: bytes, plain_hash: str, blob: bytes) -> bytes:
    try:
        plaintext = AESGCM(_content_key(key, plain_hash)).decrypt(bytes(12), blob, None)
    except (InvalidTag, ValueError) as e:
        raise CryptoError("blob failed authentication") from e
    if sha256_hex(plaintext) != plain_hash:
        raise CryptoError("blob content does not match manifest hash")
    return plaintext
