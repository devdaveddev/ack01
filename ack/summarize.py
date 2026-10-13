"""Quick answers: compress the matched notes, and cache by meaning, not exact text.

The summariser is where the owner's LLM goes. ExtractiveSummarizer is the no-API-key
stand-in: it keeps the note sentences that share the most words with the query.

SummaryCache stores each answer under its query embedding. A new query close
enough to an old one reuses the answer, so the LLM only runs on a miss. Each
entry remembers a fingerprint of the notes it was built from and is dropped as
soon as any of them changes.
"""

import json
import re
from collections.abc import Callable
from pathlib import Path

from .crypto import canonical, sha256_hex
from .memory import Entry, cosine, tokens


def note_fingerprint(entry: Entry) -> str:
    return sha256_hex(canonical([entry.version, entry.summary, entry.notes]))


class ExtractiveSummarizer:
    max_chars = 400

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
        out, used = [], 0
        for _, _, _, sent in sorted(sentences, reverse=True):
            if used + len(sent) > self.max_chars and out:
                break
            out.append(sent)
            used += len(sent) + 1
        return " ".join(out)


class SummaryCache:
    def __init__(self, path: Path, threshold: float = 0.85, max_items: int = 500):
        self.path = path
        self.threshold = threshold
        self.max_items = max_items
        self.items: list[dict] = json.loads(path.read_text()) if path.exists() else []

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.items))

    def get(self, query_vec: list[float], current: Callable[[str], Entry | None],
            visible: Callable[[Entry], bool]) -> dict | None:
        """Best match at or above the threshold whose sources are unchanged and visible."""
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

    def put(self, query_vec: list[float], packet: dict, sources: list[Entry]) -> None:
        self.items.append({"vec": query_vec, "packet": packet,
                           "sources": {e.id: note_fingerprint(e) for e in sources}})
        self.items = self.items[-self.max_items:]
        self._save()
