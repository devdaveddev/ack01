import json
import urllib.request

import pytest

from ack import bundle, schema
from ack.broker import start_in_thread
from ack.client import BrokerClient, BrokerError
from ack.crypto import CryptoError, Identity, seal_content, sha256_hex, unseal_content
from ack.memory import Entry, ProjectMemory
from ack.pairing import Invite


def test_content_encryption_is_convergent_and_checked():
    key = b"k" * 32
    ct = seal_content(key, b"hello")
    assert ct == seal_content(key, b"hello")
    assert unseal_content(key, sha256_hex(b"hello"), ct) == b"hello"
    with pytest.raises(CryptoError):
        unseal_content(key, sha256_hex(b"hello"), ct[:-1] + bytes([ct[-1] ^ 1]))


def test_pair_key_is_symmetric():
    a, b = Identity.generate(), Identity.generate()
    assert a.pair_key(b.public()) == b.pair_key(a.public())


def test_schema_rejects_bad_messages():
    msg = schema.make("CHECK", "a", "b", {"source_id": "x", "status": "READY", "detail": ""})
    with pytest.raises(schema.SchemaError):
        schema.validate(msg | {"body": {**msg["body"], "status": "MAYBE"}})
    with pytest.raises(schema.SchemaError):
        schema.validate(msg | {"body": {**msg["body"], "extra": 1}})
    with pytest.raises(schema.SchemaError):
        schema.validate(msg | {"type": "NOPE"})


@pytest.fixture
def packed(tmp_path):
    (tmp_path / "m.py").write_text("x = 1\n")
    owner, peer = Identity.generate(), Identity.generate()
    key = owner.pair_key(peer.public())
    entry = Entry(id="m", topic="t", summary="s", files=["m.py"], tests=["python", "-c", "pass"])
    manifest, blobs = bundle.pack(owner, key, "owner", tmp_path, entry, "passed")
    return owner, key, manifest, blobs


def test_bundle_roundtrip(packed, tmp_path):
    owner, key, manifest, blobs = packed
    bundle.verify_manifest(manifest, "owner", owner.public())
    bundle.unpack(key, manifest, blobs, tmp_path / "q")
    assert (tmp_path / "q" / "m.py").read_text() == "x = 1\n"


def test_manifest_tamper_and_traversal_rejected(packed):
    owner, key, manifest, blobs = packed
    with pytest.raises(bundle.BundleError, match="signature"):
        bundle.verify_manifest(manifest | {"version": 99}, "owner", owner.public())
    with pytest.raises(bundle.BundleError, match="owner"):
        bundle.verify_manifest(manifest, "mallory", owner.public())
    for bad in ("../evil.py", "/etc/passwd", "a/../../b", "a\\b"):
        with pytest.raises(bundle.BundleError):
            bundle.safe_relpath(bad)


def test_memory_search_and_staleness(tmp_path):
    (tmp_path / "r.py").write_text("v1")
    mem = ProjectMemory(tmp_path)
    mem.add(Entry(id="retry", topic="retry with backoff", summary="retries flaky calls", files=["r.py"]))
    mem.add(Entry(id="geo", topic="capital cities", summary="country lookup", files=["r.py"]))
    assert mem.search("how to retry flaky calls")[0][1].id == "retry"
    assert mem.is_current(mem.get("retry"))
    (tmp_path / "r.py").write_text("v2")
    assert not mem.is_current(mem.get("retry"))
    assert mem.bump("retry").version == 2 and mem.is_current(mem.get("retry"))


def test_invite_uri_roundtrip():
    inv = Invite("http://h:1", "tok", "beta", {"kx": "a", "sig": "b"}, "abc")
    assert Invite.from_uri(inv.to_uri()) == inv


@pytest.fixture
def broker(tmp_path):
    server, url = start_in_thread(tmp_path / "broker")
    yield url
    server.shutdown()


def test_broker_checks_hashes_auth_and_invites(broker, tmp_path):
    a = BrokerClient(broker, "a", tmp_path / "a.json")
    b = BrokerClient(broker, "b", tmp_path / "b.json")
    a.register({"kx": "x", "sig": "y"})
    b.register({"kx": "x", "sig": "y"})

    a.put_blob(sha256_hex(b"data"), b"data")
    assert a.missing([sha256_hex(b"data"), "0" * 64]) == ["0" * 64]
    with pytest.raises(BrokerError) as e:
        a.put_blob("0" * 64, b"data")
    assert e.value.status == 400

    req = urllib.request.Request(broker + "/messages")
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == 401

    with pytest.raises(BrokerError) as e:  # can't send as someone else
        a.post_message({"from": "b", "to": "a"})
    assert e.value.status == 403

    a.create_invite("1" * 64)
    assert b.redeem_invite("1" * 64) == "a"
    with pytest.raises(BrokerError) as e:
        b.redeem_invite("1" * 64)
    assert e.value.status == 410
