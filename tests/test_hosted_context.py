"""The public context-pack endpoint: only public addresses are read, and every limit holds."""

import socket
import urllib.request
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from ack.context import ContextError
from ack.hosted import api as hosted
from ack.hosted import safe_fetch
from ack.hosted.api import create_app
from ack.hosted.safe_fetch import _CheckedRedirects, check_url


def fake_dns(ip):
    def getaddrinfo(host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]
    return getaddrinfo


@pytest.fixture
def public_dns(monkeypatch):
    monkeypatch.setattr(safe_fetch.socket, "getaddrinfo", fake_dns("93.184.216.34"))


@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "ftp://example.com/x",
    "https://user:secret@example.com/",
    "http://example.com:8080/",
])
def test_unsafe_addresses_are_refused_before_any_lookup(url):
    with pytest.raises(ContextError):
        check_url(url)


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.5", "192.168.1.9", "169.254.169.254", "::1"])
def test_private_and_loopback_hosts_are_refused(monkeypatch, ip):
    monkeypatch.setattr(safe_fetch.socket, "getaddrinfo", fake_dns(ip))
    with pytest.raises(ContextError, match="not a public address"):
        check_url("https://internal.example/")


def test_a_public_host_is_accepted(public_dns):
    check_url("https://example.com/docs")


def test_a_redirect_to_an_internal_address_is_refused():
    handler = _CheckedRedirects()
    req = urllib.request.Request("https://example.com/start")
    with pytest.raises(ContextError):
        handler.redirect_request(req, None, 302, "Found", {}, "http://127.0.0.1/admin")


@pytest.fixture
def client(tmp_path, monkeypatch, public_dns):
    monkeypatch.setattr(hosted, "CONTEXT_PER_HOUR", 1000)
    monkeypatch.setattr(hosted, "CONTEXT_PER_DAY", 1000)
    return TestClient(create_app(f"sqlite:///{tmp_path / 'ctx.db'}"))


def fake_pack(goal, github=(), sites=(), name="", **kw):
    return SimpleNamespace(name=name or "pack", goal=goal, markdown=f"# {goal}\n", prompt="p",
                           items=[SimpleNamespace(source="github.com/x/y")])


def test_a_valid_request_returns_the_pack(client, monkeypatch):
    monkeypatch.setattr(hosted, "build_from_sources", fake_pack)
    res = client.post("/api/context", json={"goal": "add retry", "github": ["https://github.com/psf/requests"]})
    assert res.status_code == 200
    body = res.json()
    assert body["markdown"].startswith("# add retry")
    assert body["items"] == 1


@pytest.mark.parametrize("payload", [
    {"goal": "add retry"},  # no source
    {"goal": "x", "github": ["https://github.com/psf/requests"]},  # goal too short
    {"goal": "add retry", "github": ["https://example.com/not-a-repo"]},
    {"goal": "add retry", "github": ["a", "b", "c"]},  # too many repos
    {"goal": "add retry", "sites": ["https://example.com", "https://example.org", "https://example.net"]},
])
def test_invalid_requests_are_rejected(client, payload):
    assert client.post("/api/context", json=payload).status_code == 422


def test_a_site_on_a_private_address_is_refused(client, monkeypatch):
    monkeypatch.setattr(safe_fetch.socket, "getaddrinfo", fake_dns("10.1.2.3"))
    res = client.post("/api/context", json={"goal": "add retry", "sites": ["https://intranet.example/"]})
    assert res.status_code == 422
    assert "not a public address" in res.json()["error"]


def test_the_builder_error_is_returned_as_a_clear_message(client, monkeypatch):
    def fail(*args, **kwargs):
        raise ContextError("github.com/psf/nothing: HTTP 404 Not Found", 404)
    monkeypatch.setattr(hosted, "build_from_sources", fail)
    res = client.post("/api/context", json={"goal": "add retry", "github": ["https://github.com/psf/nothing"]})
    assert res.status_code == 422
    assert "404" in res.json()["error"]


def test_one_address_is_limited_per_hour(tmp_path, monkeypatch, public_dns):
    monkeypatch.setattr(hosted, "CONTEXT_PER_HOUR", 1)
    monkeypatch.setattr(hosted, "CONTEXT_PER_DAY", 1000)
    monkeypatch.setattr(hosted, "build_from_sources", fake_pack)
    client = TestClient(create_app(f"sqlite:///{tmp_path / 'limit.db'}"))
    body = {"goal": "add retry", "github": ["https://github.com/psf/requests"]}
    assert client.post("/api/context", json=body).status_code == 200
    assert client.post("/api/context", json=body).status_code == 429


def test_the_site_is_limited_per_day(tmp_path, monkeypatch, public_dns):
    monkeypatch.setattr(hosted, "CONTEXT_PER_HOUR", 1000)
    monkeypatch.setattr(hosted, "CONTEXT_PER_DAY", 2)
    monkeypatch.setattr(hosted, "build_from_sources", fake_pack)
    client = TestClient(create_app(f"sqlite:///{tmp_path / 'day.db'}"))
    body = {"goal": "add retry", "github": ["https://github.com/psf/requests"]}
    assert [client.post("/api/context", json=body).status_code for _ in range(3)] == [200, 200, 429]


def test_the_builder_itself_cannot_reach_a_private_host(monkeypatch):
    from ack import context
    monkeypatch.setattr(safe_fetch.socket, "getaddrinfo", fake_dns("192.168.0.10"))
    monkeypatch.setattr(context, "_OPENER", safe_fetch.SafeOpener())
    with pytest.raises(ContextError, match="not a public address"):
        context._get("https://router.local/admin", 1024)
