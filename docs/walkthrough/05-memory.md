# 05 · `ack/memory.py`, line by line

**Job of this file:** keep the list of parts a project is willing to talk about, find the parts that best match a question, and tell when a part's files have changed since its version was recorded.

**The idea behind the embedding:** turn text into a list of 1024 numbers (a vector) so that texts sharing words point in similar directions. Then "how similar are these two texts?" becomes simple arithmetic (cosine similarity), with no LLM call. Both agents must compute vectors the same way, and this one is deterministic.

Primer references: [dataclasses](00-python-primer.md#decorators-something-above-a-def-or-class), [comprehensions](00-python-primer.md#comprehensions-building-a-collection-in-one-line), [bits and bytes](00-python-primer.md#bits-and-bytes), [lambda](00-python-primer.md#functions).

---

## Lines 8–15 · imports

```python
import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .crypto import sha256_hex
```

- **8** SHA-256, used here to place words in vector slots, not for security.
- **9** To save and load `memory.json`.
- **10** `math.sqrt`, for the vector's length.
- **11** Regex, to split text into words.
- **12** `dataclass` to define `Entry`; `field` for per-object default lists; `asdict` to turn an `Entry` back into a plain dict for saving.
- **13** Paths.
- **15** File hashing, to detect changed files.

## Line 17 · vector size

```python
DIM = 1024
```

Each text becomes a list of 1024 numbers. More slots mean fewer words landing in the same slot by chance.

## Lines 20–21 · words to ignore

```python
STOPWORDS = set("""a an and are as at be by can do does for from how i in into is it me my of on or our
should that the this to use using want we what when where which with you your""".split())
```

- A triple-quoted string can span lines. `.split()` breaks it on whitespace into a list of words, and `set(...)` makes checking "is this word in here?" fast.
- **Why:** "how do I retry calls" and "retry calls using backoff" should match on *retry* and *calls*, not on filler. Without this, rewordings scored only 0.71 and missed the summary cache.

## Lines 24–26 · text → words

```python
def tokens(text: str) -> list[str]:
    words = [w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in STOPWORDS]
    return [w[:-1] if len(w) > 3 and w.endswith("s") else w for w in words]
```

- **25**, from the inside out:
  1. Lowercase the text.
  2. `re.findall(r"[a-z0-9]+", ...)` pulls out every run of letters and digits, so punctuation disappears (`"HTTP calls?"` → `["http", "calls"]`).
  3. Keep only words that aren't stopwords.
- **26** A crude plural fix: if a word is longer than 3 letters and ends in `s`, drop the `s` (`calls` → `call`, `letters` → `letter`). Short words like `is` and `has` are left alone. A conditional expression inside a list comprehension.

## Lines 29–39 · text → vector

```python
def embed(text: str) -> list[float]:
    # Each token lands in two independent signed slots, so one unlucky collision
    # (same slot, opposite sign) can't cancel a shared word out entirely.
    vec = [0.0] * DIM
    for tok in tokens(text):
        h = hashlib.sha256(tok.encode()).digest()
        for slot in (0, 1):
            index = int.from_bytes(h[slot * 4:slot * 4 + 4], "big") % DIM
            vec[index] += 1.0 if h[8] >> slot & 1 else -1.0
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]
```

- **32** Start with 1024 zeros. `[0.0] * DIM` repeats the list.
- **33** For each meaningful word…
- **34** …hash it into 32 pseudo-random bytes. The same word always gives the same bytes, on every machine.
- **35** Each word is placed **twice** (slot 0 and slot 1):
  - **36** Slot 0 uses hash bytes 0–3 (`h[0:4]`) and slot 1 uses bytes 4–7 (`h[4:8]`). `int.from_bytes(..., "big")` turns 4 bytes into a number, and `% DIM` wraps it into the range 0–1023. That's the position.
  - **37** Hash byte 8 supplies the signs: `h[8] >> slot & 1` takes bit 0 for slot 0 and bit 1 for slot 1. Add `+1` if the bit is 1, `-1` if it's 0. Random signs keep unrelated words from piling up in one direction.
- **Why two slots:** with one slot, two different words can hit the same position with opposite signs and cancel to zero. That happened to "slugify" and "text", so slugify questions matched nothing. With two independent slots, both would have to collide at once.
- **38** The vector's length: the square root of the sum of squares. `or 1.0` avoids dividing by zero when the text had no words.
- **39** Divide every number by the length, so every vector has length 1. Long and short texts are then compared fairly.

## Lines 42–43 · similarity

```python
def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))
```

- **43** `zip(a, b)` pairs up the two lists position by position. Multiply each pair and add them all up. Because both vectors have length 1, this *is* the cosine similarity:
  - `1.0`: same words
  - `0`: nothing in common
  - in between: partial overlap

## Lines 46–60 · one shareable part

```python
@dataclass
class Entry:
    id: str
    topic: str
    summary: str
    files: list[str]
    base: str = ""
    tags: list[str] = field(default_factory=list)
    version: int = 1
    tests: list[str] | None = None
    deps: list[str] = field(default_factory=list)
    allow: list[str] = field(default_factory=list)
    recorded: dict[str, str | None] = field(default_factory=dict)
    notes: str = ""
    embedding: list[float] = field(default_factory=list)
```

- **48** `id`: the name peers use in REQUEST, e.g. `retry-backoff`.
- **49–50** `topic`, `summary`: part of the matching **key**. The summary is the one-line description.
- **51** `files`: the files that make up the part, relative to `base`.
- **52** `base`: the folder the part lives in. Its tests run from there, so the bundle is self-contained.
- **53** `tags`: also part of the key.
- **54** `version`: increases when the owner records a change.
- **55** `tests`: the command to run them, as a list (e.g. `["python", "-m", "unittest", ...]`), or `None`.
- **56** `deps`: requirement strings such as `"requests>=2.31"`.
- **57** `allow`: which agents may see and request it. `"*"` means everyone.
- **58** `recorded`: file → hash at the current version. Comparing it with the files now tells us whether the part is stale.
- **59** `notes`: longer notes. They're summarised on demand, never embedded.
- **60** `embedding`: the key's vector, computed by `add()`.
- Fields after `files` have defaults, so you only pass what you need. `field(default_factory=list)` gives each `Entry` its own new list (see primer for why).

## Lines 62–63 · the key text

```python
    def key_text(self) -> str:
        return " ".join([self.topic, *self.tags, self.summary])
```

- **63** Topic, then every tag (`*self.tags` spreads the list in), then the summary, joined by spaces. This text is what gets embedded.

## Lines 66–74 · load the memory

```python
class ProjectMemory:
    def __init__(self, root: Path):
        self.root = root
        self.path = root / ".ack" / "memory.json"
        self.entries: dict[str, Entry] = {}
        if self.path.exists():
            for raw in json.loads(self.path.read_text()):
                entry = Entry(**raw)
                self.entries[entry.id] = entry
```

- **68** The project folder.
- **69** Where the memory is saved.
- **70** id → `Entry`.
- **71–74** If a saved file exists, read the JSON list. Each item is a plain dict; `Entry(**raw)` spreads its keys into the constructor to rebuild the `Entry`. Then store it by ID.

## Lines 76–78 · save

```python
    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps([asdict(e) for e in self.entries.values()], indent=2))
```

- **77** Make sure `.ack/` exists.
- **78** Turn every `Entry` into a plain dict (`asdict`), and write the list as indented, human-readable JSON.

## Lines 80–91 · share a part

```python
    def add(self, entry: Entry) -> Entry:
        from .bundle import PART_ID, safe_relpath  # imported here: bundle imports nothing from memory

        if not PART_ID.match(entry.id):
            raise ValueError(f"part id {entry.id!r} must be letters, digits, '-', '_' or '.'")
        for rel in entry.files:
            safe_relpath(rel)
        entry.embedding = embed(entry.key_text())
        entry.recorded = self.current_hashes(entry)
        self.entries[entry.id] = entry
        self.save()
        return entry
```

- **81** Borrow the part-id pattern and path checker from `bundle.py`. The import is inside the function, which keeps the import graph simple: `memory.py` is loaded before anything needs `bundle.py`.
- **83–84** Refuse a bad id **when it's shared**, not later when a peer asks for it. Receivers reject bad ids too, but the owner should never produce one.
- **85–86** The same for file paths: `safe_relpath` raises if a path escapes the folder or is hidden. (These two checks were added in the bug-fix pass, bug B.)
- **87** Compute the key's vector.
- **88** Record the files' hashes **now**. This snapshot is the current version.
- **89–91** Store it (replacing any earlier entry with the same ID), save, and return it. Re-adding an entry with new notes is how notes get updated. That changes its fingerprint, which drops related cached summaries.

## Lines 93–94 · find by ID

```python
    def get(self, entry_id: str) -> Entry | None:
        return self.entries.get(entry_id)
```

Returns the entry, or `None` if there's no such part (which becomes a `NOT_FOUND` reply).

## Lines 96–99 · find by meaning

```python
    def search(self, text: str, k: int = 3) -> list[tuple[float, Entry]]:
        query = embed(text)
        scored = [(cosine(query, e.embedding), e) for e in self.entries.values()]
        return sorted(scored, key=lambda pair: pair[0], reverse=True)[:k]
```

- **97** Embed the question.
- **98** Score every entry: a list of `(similarity, entry)` pairs.
- **99** Sort by the score (`pair[0]`), highest first, and keep the top `k`.

## Lines 101–102 · the part's folder

```python
    def part_root(self, entry: Entry) -> Path:
        return self.root / entry.base if entry.base else self.root
```

The project root plus `base`, or just the root if there's no base.

## Lines 104–109 · hash the files as they are now

```python
    def current_hashes(self, entry: Entry) -> dict[str, str | None]:
        out = {}
        for rel in entry.files:
            path = self.part_root(entry) / rel
            out[rel] = sha256_hex(path.read_bytes()) if path.is_file() else None
        return out
```

- **106–108** For each file, hash its bytes, or record `None` if it has been deleted. A deleted file is a change too.

## Lines 111–113 · is it stale?

```python
    def is_current(self, entry: Entry) -> bool:
        return self.current_hashes(entry) == entry.recorded
```

- **113** Compare the two dicts. Any edited, added or deleted file makes them unequal, so the owner replies `STALE` until it records a new version.

## Lines 115–120 · record a new version

```python
    def bump(self, entry_id: str) -> Entry:
        entry = self.entries[entry_id]
        entry.version += 1
        entry.recorded = self.current_hashes(entry)
        self.save()
        return entry
```

- **117** `+= 1` adds one to the version.
- **118** Re-snapshot the hashes, so the part is current again.
- **119–120** Save and return it. Called by `publish_update`.
