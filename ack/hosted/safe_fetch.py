"""Fetching on behalf of a visitor, safely: the public site reads only public web addresses.

Every request the context builder makes goes through `SafeOpener.open`. It refuses anything that is
not plain http or https to the default ports, any URL with credentials in it, and any host that
resolves to a private, loopback, link-local or otherwise non-public address. Redirects are checked the
same way, so a public site can't bounce the server onto an internal address.

Known gap: the address is checked just before connecting, so a host whose DNS answer changes between
the check and the connection could still get past it. Keep the server's network free of internal
services it could reach.
"""

import ipaddress
import socket
import urllib.error
import urllib.parse
import urllib.request

from ack.context import ContextError

ALLOWED_SCHEMES = ("http", "https")
ALLOWED_PORTS = (80, 443)


def check_url(url: str) -> None:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ALLOWED_SCHEMES:
        raise ContextError(f"only http and https addresses can be read: {url}")
    if parts.username or parts.password:
        raise ContextError("addresses with a username or password are not read")
    host = parts.hostname
    if not host:
        raise ContextError(f"no host in {url}")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    if port not in ALLOWED_PORTS:
        raise ContextError(f"only ports 80 and 443 are read: {url}")
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        raise ContextError(f"cannot find {host}") from None
    for info in infos:
        if not ipaddress.ip_address(info[4][0]).is_global:
            raise ContextError(f"{host} is not a public address")


class _CheckedRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(urllib.parse.urljoin(req.full_url, newurl))
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class SafeOpener:
    def __init__(self) -> None:
        self._opener = urllib.request.build_opener(_CheckedRedirects)

    def open(self, req: urllib.request.Request, timeout: float):
        check_url(req.full_url)
        try:
            return self._opener.open(req, timeout=timeout)
        except urllib.error.HTTPError:
            raise  # the builder reads status codes from these
        except urllib.error.URLError as e:
            raise ContextError(f"{req.full_url}: {getattr(e, 'reason', e)}") from None
