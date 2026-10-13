# 04 · `ack/pairing.py`, line by line

**Job of this file:** create and read the one-time invite that links two agents, as an `ack://pair?...` link or a QR code, and compute the proof that the joiner really scanned it.

Primer references: [dataclasses, @property, @classmethod](00-python-primer.md#decorators-something-above-a-def-or-class), [exceptions](00-python-primer.md#errors-exceptions).

---

## Lines 8–14 · imports

```python
import hashlib
import hmac
import io
from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode, urlsplit

from .crypto import canonical, sha256_hex
```

- **8–9** For the HMAC proof.
- **10** `io.StringIO` is an in-memory text "file", used to capture the QR drawing.
- **11** `@dataclass`, to write the `Invite` class with almost no boilerplate.
- **12** Tools to build and parse URL query strings (`?a=1&b=2`).
- **14** Canonical JSON and SHA-256 from our crypto module.

## Lines 17–23 · what an invite holds

```python
@dataclass
class Invite:
    broker: str
    token: str
    agent: str
    public: dict
    bundle: str | None = None
```

- **17** `@dataclass` writes `__init__` for us, so `Invite(broker, token, agent, public)` just works, plus equality and printing.
- **19** The broker's URL, so the joiner knows where to connect.
- **20** The one-time secret token. It exists only in the invite itself.
- **21** The inviter's agent name.
- **22** The inviter's public keys. Carrying them **in the invite** means the joiner never has to ask the broker for them, so the broker can't substitute its own.
- **23** Optionally, a bundle hash this invite is about. It defaults to `None`.

## Lines 25–27 · the token's hash

```python
    @property
    def token_hash(self) -> str:
        return token_hash(self.token)
```

- **25** `@property` lets you write `invite.token_hash` with no parentheses.
- **27** Calls the module-level `token_hash` function defined at line 63. This hash is all the broker ever sees.

## Lines 29–34 · invite → link

```python
    def to_uri(self) -> str:
        params = {"b": self.broker, "t": self.token, "a": self.agent,
                  "kx": self.public["kx"], "sig": self.public["sig"]}
        if self.bundle:
            params["h"] = self.bundle
        return "ack://pair?" + urlencode(params)
```

- **30–31** Short parameter names keep the link small, so the QR code stays easy to scan.
- **32–33** Add the bundle hash only if there is one.
- **34** `urlencode` turns the dict into `b=...&t=...`, escaping special characters such as `:` and `/` in the broker URL. The result is something like `ack://pair?b=http%3A%2F%2F...&t=...`.

## Lines 36–46 · link → invite

```python
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
```

- **36** A class method, called as `Invite.from_uri(link)`.
- **38** Split the link into scheme (`ack`), network location (`pair`), query, and so on.
- **39–40** Refuse anything that isn't our invite format.
- **41** `parse_qs` returns each parameter as a *list* of values, because a URL may repeat a key. We take the first value of each: `v[0]`.
- **43–44** Build the `Invite`. The required fields use `q[...]`, which raises `KeyError` if missing; the optional bundle uses `.get`.
- **45–46** Turn a missing field into a clear error.

## Lines 49–50 · hash a token

```python
def token_hash(token: str) -> str:
    return sha256_hex(token.encode())
```

- **50** The broker stores and redeems this, never the token itself.

## Lines 53–54 · the joiner's proof

```python
def proof(token: str, public: dict) -> str:
    return hmac.new(token.encode(), canonical(public), hashlib.sha256).hexdigest()
```

- **54** An HMAC **keyed by the token**, over the joiner's public keys (as canonical JSON). Only someone who saw the invite knows the token, so only they can produce this. It also binds the proof to *these* keys: a broker that swapped the keys in the PAIR message would break the proof.

## Lines 57–67 · draw a QR code

```python
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
```

- **58–61** `qrcode` is optional. If it isn't installed, fall back to returning the plain link with a hint. Importing inside the function means the rest of the package works without it.
- **62** Create a QR builder with a thin border.
- **63–64** Add the link and let it pick the smallest QR size that fits.
- **65** An in-memory text buffer…
- **66** …that the QR is drawn into as text blocks. `invert=True` reads better on dark terminals.
- **67** Return the drawing followed by the link, so it can be copied as well as scanned.
