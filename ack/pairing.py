"""QR pairing invites. The invite starts the connection; it never carries files.

The invite carries the inviter's public keys, so the joiner doesn't have to trust
the broker for them. The token itself never reaches the broker (only its hash), and
the joiner proves it scanned the invite with an HMAC over its own public key.
"""

import hashlib
import hmac
import io
from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode, urlsplit

from .crypto import canonical, sha256_hex


@dataclass
class Invite:
    broker: str
    token: str
    agent: str
    public: dict
    bundle: str | None = None

    @property
    def token_hash(self) -> str:
        return token_hash(self.token)

    def to_uri(self) -> str:
        params = {"b": self.broker, "t": self.token, "a": self.agent,
                  "kx": self.public["kx"], "sig": self.public["sig"]}
        if self.bundle:
            params["h"] = self.bundle
        return "ack://pair?" + urlencode(params)

    @classmethod
    def from_uri(cls, uri: str) -> "Invite":
        parts = urlsplit(uri)
        if parts.scheme != "ack" or parts.netloc != "pair":
            raise ValueError("not an ack pairing invite")
        q = {k: v[0] for k, v in parse_qs(parts.query).items()}
        try:
            return cls(broker=q["b"], token=q["t"], agent=q["a"],
                       public={"kx": q["kx"], "sig": q["sig"]}, bundle=q.get("h"))
        except KeyError as e:
            raise ValueError(f"invite missing {e}") from e


def token_hash(token: str) -> str:
    return sha256_hex(token.encode())


def proof(token: str, public: dict) -> str:
    return hmac.new(token.encode(), canonical(public), hashlib.sha256).hexdigest()


def qr_svg(uri: str) -> str | None:
    """The invite as an SVG QR code, drawn locally (no internet needed). None if qrcode is missing."""
    try:
        import qrcode
        import qrcode.image.svg
    except ImportError:
        return None
    img = qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage, border=2)
    return img.to_string(encoding="unicode")


def render_qr(uri: str) -> str:
    try:
        import qrcode
    except ImportError:
        return f"(pip install qrcode to draw this as a QR code)\n{uri}"
    qr = qrcode.QRCode(border=1)
    qr.add_data(uri)
    qr.make(fit=True)
    out = io.StringIO()
    qr.print_ascii(out=out, invert=True)
    return out.getvalue() + uri
