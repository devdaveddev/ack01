"""Client for the broker. Speaks the Ack wire for ack:// URLs and HTTP for http:// ones."""

import json
import os
import socket
import threading
import urllib.error
import urllib.request
from pathlib import Path

from .crypto import sha256_hex
from . import wire


class BrokerError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"broker {status}: {message}")
        self.status = status


class BrokerClient:
    def __init__(self, url: str, agent: str, cred_path: Path):
        self.url = url.rstrip("/")
        self.agent = agent
        self.cred_path = cred_path
        self.secret = None
        self.is_wire = self.url.startswith("ack://")
        self._sock: socket.socket | None = None
        self._sock_lock = threading.Lock()
        if cred_path.exists():
            creds = json.loads(cred_path.read_text())
            if creds.get("url") == self.url:
                self.secret = creds["secret"]

    def _call(self, method: str, path: str, body=None, raw: bytes | None = None, auth: bool = True):
        if auth and not self.secret:
            raise BrokerError(401, "not registered with this broker")
        if self.is_wire:
            return self._call_wire(method, path, body, raw, auth)
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.url + path, data=data, method=method)
        req.add_header("Content-Type", "application/octet-stream" if raw is not None else "application/json")
        if auth:
            req.add_header("X-Ack-Agent", self.agent)
            req.add_header("Authorization", f"Bearer {self.secret}")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = resp.read()
                if resp.headers.get("Content-Type") == "application/octet-stream":
                    return payload
                return json.loads(payload or b"null")
        except urllib.error.HTTPError as e:
            try:
                message = json.loads(e.read()).get("error", e.reason)
            except (json.JSONDecodeError, AttributeError):
                message = e.reason
            raise BrokerError(e.code, message) from None
        except (urllib.error.URLError, OSError) as e:
            reason = getattr(e, "reason", e)
            raise BrokerError(503, f"broker unreachable at {self.url} ({reason})") from None

    def _call_wire(self, method: str, path: str, body, raw: bytes | None, auth: bool):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else b"")
        header = {"op": method, "path": path}
        if auth:
            header |= {"agent": self.agent, "auth": self.secret}
        with self._sock_lock:
            # One connection, reused; if the broker restarted, reconnect once and resend.
            for attempt in (0, 1):
                try:
                    if self._sock is None:
                        self._sock = socket.create_connection(wire.parse_url(self.url), timeout=30)
                    wire.write_frame(self._sock, header, data)
                    reply, payload = wire.read_frame(self._sock, 1 << 30)
                    break
                except (OSError, wire.WireError) as e:
                    if self._sock is not None:
                        self._sock.close()
                        self._sock = None
                    if attempt:
                        raise BrokerError(503, f"broker unreachable at {self.url} ({e})") from None
        status = int(reply.get("status", 500))
        if status >= 400:
            try:
                message = json.loads(payload).get("error", "error")
            except (ValueError, AttributeError):
                message = "error"
            raise BrokerError(status, message)
        if reply.get("raw"):
            return payload
        return json.loads(payload or b"null")

    def close(self) -> None:
        with self._sock_lock:
            if self._sock is not None:
                self._sock.close()
                self._sock = None

    def register(self, public: dict) -> None:
        if self.secret:
            return
        self.secret = self._call("POST", "/agents", {"agent": self.agent, "public": public}, auth=False)["secret"]
        self.cred_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.cred_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"url": self.url, "secret": self.secret}, f)

    def create_invite(self, token_hash: str) -> None:
        self._call("POST", "/invites", {"token_hash": token_hash})

    def redeem_invite(self, token_hash: str) -> str:
        return self._call("POST", "/invites/redeem", {"token_hash": token_hash})["inviter"]

    def post_message(self, wire: dict) -> None:
        self._call("POST", "/messages", wire)

    def fetch_messages(self) -> list[dict]:
        return self._call("GET", "/messages")

    def missing(self, ids: list[str]) -> list[str]:
        return self._call("POST", "/blobs/missing", {"ids": ids})["missing"]

    def put_blob(self, blob_id: str, data: bytes) -> None:
        self._call("PUT", f"/blobs/{blob_id}", raw=data)

    def get_blob(self, blob_id: str) -> bytes:
        data = self._call("GET", f"/blobs/{blob_id}")
        if sha256_hex(data) != blob_id:
            raise BrokerError(502, f"blob {blob_id[:12]} failed hash check")
        return data

    def record_holding(self, **record) -> None:
        self._call("POST", "/holdings", record)

    def holdings(self) -> list[dict]:
        return self._call("GET", "/holdings")

    def audit(self) -> list[dict]:
        return self._call("GET", "/audit")

    def public_key(self, agent: str) -> dict:
        return self._call("GET", f"/agents/{agent}/key")["public"]

    def subscribe(self, publisher: str, on: bool = True) -> None:
        self._call("POST", "/subscriptions" if on else "/subscriptions/remove", {"publisher": publisher})

    def subscriptions(self) -> dict:
        return self._call("GET", "/subscriptions")

    def announce(self, statement: dict, sig: str, copies: dict) -> dict:
        return self._call("POST", "/announcements", {"statement": statement, "sig": sig, "copies": copies})

    def sign(self, ann_id: str, vote: dict, sig: str) -> dict:
        return self._call("POST", f"/announcements/{ann_id}/sign", {"vote": vote, "sig": sig})["status"]

    def announcements(self) -> list[dict]:
        return self._call("GET", "/announcements")

    def announcement(self, ann_id: str) -> dict:
        return self._call("GET", f"/announcements/{ann_id}")
