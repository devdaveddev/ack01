"""The Ack wire: how agents and the broker talk without HTTP.

One TCP connection carries any number of request/reply frames, one at a time.

    frame  = magic  header-length  body-length  header  body
             "ACK1" uint32 (BE)    uint32 (BE)  JSON    bytes

A request header is {"op": "GET"|"POST"|"PUT", "path": "/messages", "agent": name, "auth": secret}.
A reply header is   {"status": 200, "raw": false, "ctype": ""}.
The body is JSON for operations and raw bytes for blobs. The operations themselves are
the broker's (see ack/broker.py); the wire only frames them, so the framing is built for
handoffs: no URL encoding, no chunking, no cookies, one fixed header shape.

Limits keep a bad peer from exhausting memory: headers are at most 64 KB, bodies at most
what the reader allows. Anything that isn't a well-formed frame closes the connection.
"""

import json
import socket
import struct

MAGIC = b"ACK1"
PREFIX = struct.Struct(">4sII")
MAX_HEADER = 64 << 10
DEFAULT_PORT = 7468


class WireError(Exception):
    pass


def _read_exact(sock: socket.socket, n: int) -> bytes:
    chunks, got = [], 0
    while got < n:
        chunk = sock.recv(min(n - got, 1 << 20))
        if not chunk:
            raise WireError("connection closed")
        chunks.append(chunk)
        got += len(chunk)
    return b"".join(chunks)


def read_frame(sock: socket.socket, max_body: int) -> tuple[dict, bytes]:
    magic, hlen, blen = PREFIX.unpack(_read_exact(sock, PREFIX.size))
    if magic != MAGIC:
        raise WireError("not an Ack wire frame")
    if hlen > MAX_HEADER or blen > max_body:
        raise WireError("frame too large")
    try:
        header = json.loads(_read_exact(sock, hlen))
    except (ValueError, UnicodeDecodeError) as e:
        raise WireError(f"bad frame header: {e}") from None
    if not isinstance(header, dict):
        raise WireError("bad frame header")
    return header, _read_exact(sock, blen)


def write_frame(sock: socket.socket, header: dict, body: bytes = b"") -> None:
    head = json.dumps(header, separators=(",", ":")).encode()
    sock.sendall(PREFIX.pack(MAGIC, len(head), len(body)) + head + body)


def parse_url(url: str) -> tuple[str, int]:
    """ack://host:port -> (host, port)."""
    rest = url.removeprefix("ack://").rstrip("/")
    host, _, port = rest.rpartition(":")
    if not host:
        host, port = rest, str(DEFAULT_PORT)
    return host.strip("[]"), int(port)
