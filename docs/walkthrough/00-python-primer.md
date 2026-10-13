# 00 · Python primer: every piece of syntax the code uses

The walkthroughs explain each line of the code. This page explains each kind of *syntax* once, so the walkthroughs can say "dict comprehension (see primer)" instead of re-explaining it every time. Each entry shows a real line from the codebase.

---

## Files, modules, imports

```python
import hashlib                                   # load a whole standard-library module; use as hashlib.sha256(...)
from pathlib import Path                         # load one name from a module; use as Path(...)
from .crypto import sha256_hex                   # leading dot = "from this package" (ack/crypto.py)
from ..judge import RuleJudge                    # two dots = "from the parent package" (used in ack/research/)
```

- A **module** is one `.py` file. A **package** is a folder of modules with an `__init__.py`. `ack/` is a package, and `ack/research/` is a package inside it.
- `python -m ack demo` runs `ack/__main__.py` as a program.
- The text in triple quotes at the top of a file (`"""..."""`) is the module's **docstring**. It's documentation, not code that runs. Functions and classes can have docstrings too.

## Values and types you'll see

| Syntax | Type | Example from the code |
|---|---|---|
| `"text"`, `f"..."` | `str` (text) | `"ack/pair/v1"` |
| `b"..."` | `bytes` (raw binary) | `b"ack/blob/v1\|"`; hashes and ciphertext are bytes |
| `1`, `0.15`, `256 << 20` | `int`, `float` | `256 << 20` shifts bits left 20 places = 256 × 1,048,576 = 256 MB |
| `0o600` | `int` written in octal | Unix file permission "owner read+write only" |
| `True`, `False`, `None` | `bool`, "nothing" | `reply_to=None` |
| `[a, b]` | `list` (ordered, changeable) | `files = []` |
| `(a, b)` | `tuple` (ordered, fixed) | `STATUSES = ("READY", "STALE", "DENIED", "NOT_FOUND")` |
| `{"k": v}` | `dict` (key → value) | `{"kx": ..., "sig": ...}` |
| `{a, b}` or `set()` | `set` (unique items, fast "is it in here?") | `paths = set()` |

**Converting between `str` and `bytes`:** `"abc".encode()` turns text into bytes, and `b"abc".decode()` turns bytes into text. Crypto works on bytes; JSON and humans work on text. That's why you'll see `.encode()` and `.decode()` a lot.

## Strings

```python
f"bad peer key: {e}"                 # f-string: {expression} is replaced by its value
f"{where}.{name} has wrong type"
f"unknown message type {msg['type']!r}"    # !r = use repr(): adds quotes, shows hidden characters
f"{name:24}"                         # pad to 24 characters wide (used to line up tables)
f"{x * 100:5.1f}%"                   # 5 wide, 1 decimal place, float
"|".join(["a", "b"])                 # -> "a|b"
text.rstrip("=")                     # remove trailing "=" characters
text.lower(), text.split()           # lowercase; split on whitespace into a list
```

## Indexing and slicing

```python
blob[:12]          # first 12 items (here: the 12-byte nonce)
blob[12:]          # everything from position 12 on
h[:2]              # first 2 bytes of a hash
diff[-6000:]       # last 6000 characters
diff[:MAX]         # first MAX characters
data["kx"]         # dict lookup; raises KeyError if missing
data.get("kx")     # dict lookup; returns None if missing
data.get("h", "")  # ...or the given default
```

## Functions

```python
def seal(key: bytes, plaintext: bytes, aad: bytes = b"") -> bytes:
    ...
```

- `def name(params):` defines a function. The indented block underneath is its body.
- `key: bytes` is a **type hint**. It documents the expected type, but Python doesn't enforce it.
- `aad: bytes = b""` is a parameter with a **default value**, so callers may leave it out.
- `-> bytes` documents what the function returns.
- `return x` sends a value back. A function with no `return` returns `None`.
- `str | None` in a hint means "a string or None".

**Calling with names:** `HKDF(algorithm=..., length=32, salt=None, info=info)`. Naming the arguments makes the call readable and order-independent.

**Unpacking into a call:**

```python
identity.kx.private_bytes(**_RAW_PRIVATE)   # ** spreads a dict into named arguments
argv = [sys.executable, "-I", *cmd[1:]]     # * spreads a list into another list
def _provenance(self, **record):            # **record collects any named arguments into a dict
```

**`lambda`** is a tiny unnamed function on one line:

```python
visible = lambda e: peer in e.allow or "*" in e.allow   # same as: def visible(e): return ...
```

**A function inside a function** (a closure) can use the outer function's variables. `reply()` inside `_on_request` uses `peer` and `sid` from around it. `complete()` inside `anthropic_complete()` remembers `client` and `model`.

## Comprehensions (building a collection in one line)

```python
[f["blob"] for f in manifest["files"]]                    # list: one item per file
{k: msg[k] for k in HEADER_FIELDS}                        # dict: key -> value
{k: v for k, v in manifest.items() if k != "sig"}         # dict, filtered with if
sorted(p for p in paths if old.get(p) != new.get(p))      # generator expression fed into sorted()
any(part in ("", ".", "..") for part in pure.parts)       # True if at least one item is true
all(_hollow(t) for t in tests)                            # True if every item is true
sum(r["bytes"] for r in sel)                              # add up a value across items
```

Read them as "*expression* **for** *item* **in** *collection* **if** *condition*".

## Control flow

```python
if a: ...
elif b: ...
else: ...

for rel in sorted(entry.files): ...          # loop over items
for i, sent in enumerate(parts): ...         # enumerate gives (index, item)
for name, typ in spec.items(): ...           # .items() gives (key, value) pairs from a dict
for _ in range(rounds): ...                  # _ means "I don't need this value"
continue                                     # skip to the next loop iteration
break                                        # leave the loop
```

**Conditional expression** (an inline if/else):

```python
base = self.root if decision.action == "ADOPT" else self.root / "vendor" / peer / sid
```

**Chained comparison:** `0 <= body["confidence"] <= 1` means "between 0 and 1".

**Truthiness:** empty things are false (`""`, `[]`, `{}`, `0`, `None`), so `if missing:` means "if the set isn't empty".

**Short-circuit trick:** `calls.append(q) or real(q, notes)`. `append` returns `None` (false), so `or` goes on to run and return `real(...)`. It's used in a test to record a call and still pass it through.

## Errors (exceptions)

```python
try:
    shared = self.kx.exchange(...)
except ValueError as e:                       # catch one kind of error, name it e
    raise CryptoError(f"bad peer key: {e}") from e   # raise our own, keeping the original as the cause
except (InvalidTag, ValueError) as e:         # catch several kinds at once

class CryptoError(Exception):                 # a custom error type
    pass                                      # "pass" = an empty block
```

We raise **our own** error types (`CryptoError`, `BundleError`, `SchemaError`) so the agent can catch "anything wrong with this message" in one place and drop it, without crashing.

## Classes and objects

```python
class Identity:
    def __init__(self, kx, sig):     # runs when you create one: Identity(kx, sig)
        self.kx = kx                 # self = this object; self.kx is an attribute stored on it
        self._pair_keys = {}         # leading underscore = "internal, don't use from outside"

    def public(self) -> dict:        # a method: called as identity.public()
        ...
```

- `class LLMJudge(RuleJudge):` means **inheritance**: `LLMJudge` gets all of `RuleJudge`'s methods and overrides some of them.
- `super().decide(report)` calls the parent class's version of the method. `LLMJudge` uses this to run the rule floor first.
- `type("BoundHandler", (Handler,), {"state": ...})` builds a subclass at runtime. The broker uses it to attach state to the request handler class.

## Decorators (`@something` above a def or class)

```python
@classmethod
def generate(cls) -> "Identity":     # called on the class: Identity.generate(); cls is the class itself
    return cls(...)

@property
def token_hash(self) -> str:         # read like an attribute: invite.token_hash (no parentheses)

@dataclass
class Invite:                        # auto-writes __init__, ==, and repr from the field list below
    broker: str
    token: str
    bundle: str | None = None        # field with a default
```

`field(default_factory=list)` in a dataclass gives each object its *own* new empty list. Writing `= []` directly would share one list between every object, a classic Python bug.

The return hint `-> "Identity"` is in quotes because the class isn't finished being defined at that point.

## `with` (context managers)

```python
with os.fdopen(fd, "w") as f:        # open something; it's closed automatically at the end of the block
    json.dump(data, f)

with urllib.request.urlopen(req, timeout=30) as resp:
    payload = resp.read()

with st.lock:                        # take a thread lock; released at the end, even on error
    ...
```

## Dicts: the newer operators

```python
default | json.loads(text)           # merge: right side wins on clashes (Python 3.9+)
hdr | {"sealed": ...}                # a new dict = header plus one more key
candidates |= {"panel": ...}         # merge in place
spec.keys() - obj.keys()             # keys in spec but not in obj (set difference)
old.keys() | new.keys()              # keys in either (set union)
d.setdefault(k, {})[peer] = v        # get d[k], creating {} first if missing, then set inside it
d.pop(k, None)                       # remove and return d[k], or None if absent
```

## Bits and bytes

```python
os.O_WRONLY | os.O_CREAT | os.O_EXCL   # | on ints = bitwise OR: combine flags
h[8] >> slot & 1                       # shift right by slot, keep the lowest bit -> 0 or 1
int.from_bytes(h[:4], "big")           # turn 4 bytes into a number
bytes(12)                              # 12 zero bytes
-len(text) % 4                         # how many characters to reach the next multiple of 4
```

## Paths (`pathlib`)

```python
root / "retry" / "retry.py"     # the / operator joins path parts
path.read_text(), path.read_bytes(), path.write_text(s), path.write_bytes(b)
path.exists(), path.is_file(), path.parent, path.name, path.suffix
path.mkdir(parents=True, exist_ok=True)   # create folders; don't fail if they exist
path.resolve()                  # absolute path with ".." and symlinks worked out
target.is_relative_to(dest)     # is target inside dest?
path.iterdir()                  # the items in a folder
tmp.replace(final)              # atomic rename: readers see the old file or the new one, never half
```

## Regular expressions (`re`)

```python
re.compile(r"^[0-9a-f]{64}$")        # exactly 64 lowercase hex characters, nothing else
re.findall(r"[a-z0-9]+", text)        # every run of letters/digits -> list of words
re.split(r"(?<=[.!?])\s+|\n+", text)  # split into sentences (after . ! ? or at newlines)
re.sub(r"[^a-z0-9]+", "-", s)         # replace runs of other characters with "-"
```

The `r"..."` prefix means a *raw string*: backslashes are kept as typed, which regexes need.

## Small idioms

- `x = x or default`: use `x` if it's truthy, otherwise `default`. Example: `self.judge = judge or RuleJudge()`.
- `a, b = [], {}`: assign two variables at once.
- `msgs, st.queues[agent] = st.queues.get(agent, []), []` takes the queue and empties it in one statement.
- `if __name__ == "__main__":` runs this only when the file is executed directly, not when it's imported.
- `json.dumps(obj)` turns a Python object into JSON text; `json.loads(text)` goes back.
- `sorted(items, key=..., reverse=True)` sorts by a computed key, largest first.
- `min(votes, key=lambda v: CONSERVATIVE[v.action])` finds the item with the smallest computed key.
