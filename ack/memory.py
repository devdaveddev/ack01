"""Project memory: the parts an agent is willing to talk about, keyed by topic, tags and embedding.

The embedding is a deterministic feature-hashing model, so both sides compute
the same vectors with no API key. Replace `embed` with a real shared model later;
both agents must use the same one.
"""

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .crypto import sha256_hex
from .spelling import TECH_WORDS

DIM = 1024


STOPWORDS = set("""a an and are as at be by can do does for from how i in into is it me my of on or our
should that the this to use using want we what when where which with you your""".split())


def tokens(text: str) -> list[str]:
    words = [w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in STOPWORDS]
    return [w[:-1] if len(w) > 3 and w.endswith("s") else w for w in words]


def _levenshtein(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _allowed_edits(word: str) -> int:
    # Words up to five letters may change by one edit, longer ones by two ("webhok" -> "webhook").
    return 1 if len(word) <= 5 else 2


def fix_typos(words: list[str], known: set[str]) -> list[str]:
    """Map each query token onto the closest known token (stemmed, same form as `tokens`).

    A token that is already known passes through. A run-together pair ("geocapitals") is split
    into two known tokens. Otherwise the nearest known token within the edit budget wins.
    Anything else is left alone, so an unrelated word still finds nothing.
    """
    out = []
    i = 0
    while i < len(words):
        w = words[i]
        if i + 1 < len(words) and w + words[i + 1] in known:
            out.append(w + words[i + 1])  # "web hook" -> "webhook"
            i += 2
            continue
        i += 1
        if w in known or len(w) < 4:
            out.append(w)
            continue
        split = next(((a, w[len(a):]) for a in known if w.startswith(a) and len(a) >= 3
                      and w[len(a):] in known), None)
        if split:
            out.extend(split)
            continue
        budget = _allowed_edits(w)
        close = [(d, k) for k in known if abs(len(k) - len(w)) <= budget
                 for d in [_levenshtein(w, k)] if d <= budget]
        out.append(min(close)[1] if close else w)
    return out


def embed(text: str) -> list[float]:
    return _embed_tokens(tokens(text))


def _embed_tokens(toks: list[str]) -> list[float]:
    # Each token lands in two independent signed slots, so one unlucky collision
    # (same slot, opposite sign) can't cancel a shared word out entirely.
    vec = [0.0] * DIM
    for tok in toks:
        h = hashlib.sha256(tok.encode()).digest()
        for slot in (0, 1):
            index = int.from_bytes(h[slot * 4:slot * 4 + 4], "big") % DIM
            vec[index] += 1.0 if h[8] >> slot & 1 else -1.0
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


@dataclass
class Entry:
    id: str
    topic: str
    summary: str  # the one-line description that, with topic and tags, forms the key
    files: list[str]  # relative to base
    base: str = ""  # folder inside the project this part lives in; tests run from here
    tags: list[str] = field(default_factory=list)
    version: int = 1
    tests: list[str] | None = None
    deps: list[str] = field(default_factory=list)
    allow: list[str] = field(default_factory=list)  # agent names, or "*"
    recorded: dict[str, str | None] = field(default_factory=dict)  # path -> sha256 at this version
    notes: str = ""  # longer project notes; summarised on demand, never embedded
    embedding: list[float] = field(default_factory=list)

    def key_text(self) -> str:
        return " ".join([self.topic, *self.tags, self.summary])


class ProjectMemory:
    def __init__(self, root: Path):
        self.root = root
        self.path = root / ".ack" / "memory.json"
        self.entries: dict[str, Entry] = {}
        if self.path.exists():
            for raw in json.loads(self.path.read_text()):
                entry = Entry(**raw)
                self.entries[entry.id] = entry

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps([asdict(e) for e in self.entries.values()], indent=2))

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

    def get(self, entry_id: str) -> Entry | None:
        return self.entries.get(entry_id)

    def search(self, text: str, k: int = 3) -> list[tuple[float, Entry]]:
        # Typos are fixed against what this project shares plus the technical vocabulary.
        # Parts keep their stored embeddings: only the query side changes.
        known = {t for w in self.vocabulary() | TECH_WORDS for t in tokens(w)}
        query = _embed_tokens(fix_typos(tokens(text), known))
        scored = [(cosine(query, e.embedding), e) for e in self.entries.values()]
        return sorted(scored, key=lambda pair: pair[0], reverse=True)[:k]

    def vocabulary(self) -> set[str]:
        """Every word this project's shared parts use (keys, notes, ids, file names), unstemmed,
        so the spell checker knows them: "slugify", "backoff", "hmac"."""
        words = set()
        for e in self.entries.values():
            text = " ".join([e.key_text(), e.notes, e.id, " ".join(e.files)])
            words.update(w for w in re.findall(r"[a-z]+", text.lower()) if len(w) >= 3)
        return words

    def part_root(self, entry: Entry) -> Path:
        return self.root / entry.base if entry.base else self.root

    def current_hashes(self, entry: Entry) -> dict[str, str | None]:
        out = {}
        for rel in entry.files:
            path = self.part_root(entry) / rel
            out[rel] = sha256_hex(path.read_bytes()) if path.is_file() else None
        return out

    def is_current(self, entry: Entry) -> bool:
        """False when files changed (or vanished) since this version was recorded."""
        return self.current_hashes(entry) == entry.recorded

    def bump(self, entry_id: str) -> Entry:
        entry = self.entries[entry_id]
        entry.version += 1
        entry.recorded = self.current_hashes(entry)
        self.save()
        return entry
