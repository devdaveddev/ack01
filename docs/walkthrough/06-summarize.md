# 06 · `ack/summarize.py`, line by line

**Job of this file:** build the short summary in a quick ANSWER, and cache those summaries **by meaning**, so a reworded question reuses an earlier answer. A cached summary is thrown away the moment a note it came from changes.

Primer references: [classes](00-python-primer.md#classes-and-objects), [comprehensions](00-python-primer.md#comprehensions-building-a-collection-in-one-line), [regex](00-python-primer.md#regular-expressions-re).

---

## Lines 12–18 · imports

```python
import json
import re
from collections.abc import Callable
from pathlib import Path

from .crypto import canonical, sha256_hex
from .memory import Entry, cosine, tokens
```

- **12** Save and load the cache.
- **13** Split notes into sentences.
- **14** `Callable` is a type hint for "a function". `get()` takes two functions as parameters.
- **17** Canonical JSON and hashing, for fingerprints.
- **18** The `Entry` type, similarity, and the same word-splitting the embedding uses.

## Lines 21–22 · fingerprint of a note

```python
def note_fingerprint(entry: Entry) -> str:
    return sha256_hex(canonical([entry.version, entry.summary, entry.notes]))
```

- **22** Hash the three things a summary depends on: version, one-line summary, notes. If any of them changes, the fingerprint changes, and any summary built from the old one is stale.

## Lines 25–26 · the stand-in summariser

```python
class ExtractiveSummarizer:
    max_chars = 400
```

- **25** "Extractive" means it picks existing sentences instead of writing new ones. This is the slot where the owner's LLM would go; any object with a `summarize(query, notes)` method works.
- **26** A class attribute, shared by all instances: a length limit for the summary.

## Lines 28–37 · score every sentence

```python
    def summarize(self, query: str, notes: list[Entry]) -> str:
        words = set(tokens(query))
        sentences = []
        for rank, entry in enumerate(notes):
            text = entry.notes or entry.summary
            for i, sent in enumerate(re.split(r"(?<=[.!?])\s+|\n+", text)):
                if sent.strip():
                    overlap = len(words & set(tokens(sent)))
                    label = f"[{entry.id}] " if len(notes) > 1 else ""
                    sentences.append((overlap, -rank, -i, label + sent.strip()))
```

- **28** `notes` are the matched entries, best match first.
- **29** The question's words, as a set.
- **31** `enumerate` gives each entry's position: `rank` 0 is the best match.
- **32** Use the long notes if there are any; otherwise the one-line summary.
- **33** Split into sentences. The regex splits on whitespace that follows `.`, `!` or `?` (`(?<=...)` means "preceded by", without consuming the punctuation), or on line breaks. `i` is the sentence's position.
- **34** Skip empty pieces.
- **35** `&` on sets gives the intersection: the words the sentence shares with the question. `len` counts them.
- **36** If more than one part matched, prefix each sentence with its part ID, so A knows which part said what.
- **37** Store a tuple: `(overlap, -rank, -i, text)`. Tuples sort item by item, so sorting these largest-first means:
  1. most shared words first
  2. ties go to the better-matching part (`-rank`: a smaller rank gives a larger value)
  3. then to sentences earlier in the note (`-i`)

## Lines 38–44 · pick sentences up to the limit

```python
        out, used = [], 0
        for _, _, _, sent in sorted(sentences, reverse=True):
            if used + len(sent) > self.max_chars and out:
                break
            out.append(sent)
            used += len(sent) + 1
        return " ".join(out)
```

- **38** The chosen sentences, and the characters used so far.
- **39** Best first. The underscores ignore the sorting fields; only the text is needed.
- **40–41** Stop once the next sentence would pass 400 characters. `and out` makes sure at least one sentence is always included, even a long one.
- **42–43** Take it, and count its length plus one for the joining space.
- **44** Join into one string.

## Lines 47–52 · the summary cache

```python
class SummaryCache:
    def __init__(self, path: Path, threshold: float = 0.85, max_items: int = 500):
        self.path = path
        self.threshold = threshold
        self.max_items = max_items
        self.items: list[dict] = json.loads(path.read_text()) if path.exists() else []
```

- **48** `threshold`: how similar a new question must be to a cached one (0.85 out of 1.0) to reuse its answer. `max_items`: a size cap.
- **52** Load saved items if the file exists, otherwise start empty. Each item looks like `{"vec": [...], "packet": {...}, "sources": {part_id: fingerprint}}`.

## Lines 54–56 · save

```python
    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.items))
```

Write the whole cache as JSON.

## Lines 58–77 · look up a cached answer

```python
    def get(self, query_vec: list[float], current: Callable[[str], Entry | None],
            visible: Callable[[Entry], bool]) -> dict | None:
        best, best_score = None, self.threshold
        stale = []
        for item in self.items:
            score = cosine(query_vec, item["vec"])
            if score < best_score:
                continue
            entries = {sid: current(sid) for sid in item["sources"]}
            if any(entries[sid] is None or note_fingerprint(entries[sid]) != fp
                   for sid, fp in item["sources"].items()):
                stale.append(item)
                continue
            if all(visible(e) for e in entries.values()):
                best, best_score = item, score
        if stale:
            self.items = [i for i in self.items if i not in stale]
            self._save()
        return dict(best["packet"]) if best else None
```

- **58–59** Parameters:
  - `query_vec`: the new question's embedding
  - `current`: a function that returns the *current* `Entry` for a part ID (the agent passes `memory.get`)
  - `visible`: a function that says whether the asking peer may see an entry

  Passing functions in keeps this cache independent of how the agent stores things.
- **61** Nothing found yet. Starting `best_score` at the threshold means only items at or above it can win.
- **62** Items we discover are stale, to delete afterwards. We don't delete from a list while looping over it.
- **63–66** For each cached item, compute its similarity to the new question. If it's below the best so far, skip it.
- **67** Look up the current version of every part this cached answer came from.
- **68–71** **Freshness check.** If any source part was deleted (`None`) or its fingerprint no longer matches the one stored with the answer, the answer is stale: mark it and skip it.
- **72–73** **Visibility check.** Only accept the item if the asking peer may see **every** part it mentions. An answer cached for one peer must never leak a private part to another. If it passes, it becomes the best so far.
- **74–76** Remove all stale items and save.
- **77** Return a **copy** of the best packet (`dict(...)` copies it, so the caller can't modify the cached original), or `None` for a miss.

## Lines 79–83 · store an answer

```python
    def put(self, query_vec: list[float], packet: dict, sources: list[Entry]) -> None:
        self.items.append({"vec": query_vec, "packet": packet,
                           "sources": {e.id: note_fingerprint(e) for e in sources}})
        self.items = self.items[-self.max_items:]
        self._save()
```

- **80–81** Store the question's vector, the answer, and a fingerprint for each part it was built from. The fingerprints are what make invalidation possible.
- **82** Keep only the newest 500. A negative slice takes from the end.
- **83** Save.
