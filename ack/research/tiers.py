"""RQ1: how small can the shared packet get before the receiver's answers get worse?

Four tiers of what the receiver gets back for a question:
  none           nothing; the receiver answers blind
  summary        the ANSWER packet's summary (the owner's real answer_packet path)
  summary+fetch  summary plus the one file from the top part most relevant to the question
  full           summary plus every file in the top part (what a whole bundle delivers)

Each question has gold facts. The offline score is fact coverage: the share of gold
facts present in what the receiver got, which is an upper bound on what any answerer
could say. With --llm, a model answers from that context and the answer is graded on
the same facts. Reported alongside bytes, so the curve shows where extra bytes stop
buying better answers. The question set mixes facts that live only in notes (the
summary can carry them) with facts that live only in code (only a fetch can).
"""

import tempfile
from pathlib import Path

from ..agent import Agent
from ..demo import BETA_FILES, BETA_PARTS, write
from ..memory import Entry, cosine, embed

TIERS = ("none", "summary", "summary+fetch", "full")

# (question, [gold facts]); a fact is a list of acceptable phrasings, matched case-insensitively.
QUESTIONS = [
    ("how many attempts does retry make by default?", [["three attempts", "attempts=3"]]),
    ("does retry treat 4xx and 5xx errors differently?", [["does not distinguish 4xx"]]),
    ("what base delay does the retry backoff start from?", [["10ms", "base=0.01"]]),
    ("which clients use the retry helper?", [["webhook"], ["billing"]]),
    ("what separator does slugify put between words?", [["'-'", '"-"']]),
    ("does slugify transliterate accented letters?", [["dropped, not transliterated"]]),
    ("does slugify strip leading and trailing separators?", [['strip("-")']]),
    ("which file does the capitals lookup read its data from?", [["capitals.json"]]),
    ("how often is the capital city data refreshed?", [["each quarter"]]),
    ("what are the keys of the capitals dictionary?", [["iso"]]),
]

ANSWER_SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}


def contexts(owner: Agent, peer: str, question: str) -> dict[str, str]:
    packet, _ = owner.answer_packet(peer, question)
    summary = packet["summary"]
    if not packet["sources"]:
        return {t: "" for t in TIERS}
    entry = owner.memory.get(packet["sources"][0])
    root = owner.memory.part_root(entry)
    files = {rel: (root / rel).read_text() for rel in entry.files}
    q = embed(question)
    best = max(files, key=lambda rel: cosine(q, embed(rel + " " + files[rel])))
    block = lambda rel: f"\n--- {rel}\n{files[rel]}"
    return {
        "none": "",
        "summary": summary,
        "summary+fetch": summary + block(best),
        "full": summary + "".join(block(rel) for rel in files),
    }


def coverage(text: str, facts: list[list[str]]) -> float:
    low = text.lower()
    return sum(any(p.lower() in low for p in fact) for fact in facts) / len(facts)


def run(answerer=None) -> dict:
    """answerer(question, context) -> str. Default grades the context itself."""
    with tempfile.TemporaryDirectory(prefix="ack-tiers-") as tmp:
        write(Path(tmp) / "beta", BETA_FILES)
        owner = Agent("beta", Path(tmp) / "beta", "http://unused.invalid", log=None)
        for entry in BETA_PARTS:
            owner.share(Entry(**vars(entry)))
        rows = []
        for question, facts in QUESTIONS:
            ctx = contexts(owner, "alpha", question)
            for tier in TIERS:
                answer = answerer(question, ctx[tier]) if answerer else ctx[tier]
                rows.append({"question": question, "tier": tier, "bytes": len(ctx[tier].encode()),
                             "score": coverage(answer, facts)})
    summary = {}
    for tier in TIERS:
        sel = [r for r in rows if r["tier"] == tier]
        mean_bytes = sum(r["bytes"] for r in sel) / len(sel)
        mean_score = sum(r["score"] for r in sel) / len(sel)
        summary[tier] = {"score": mean_score, "bytes": mean_bytes,
                         "score_per_kb": mean_score / (mean_bytes / 1024) if mean_bytes else 0.0}
    return {"tiers": summary, "rows": rows}


def llm_answerer(complete):
    system = ("Answer the question using only the context. If the context doesn't contain the answer, "
              "say you don't know. Quote exact names, values and phrases from the context where you can.")

    def answer(question: str, context: str) -> str:
        out = complete(system, f"<context>\n{context or '(no context)'}\n</context>\nQuestion: {question}",
                       ANSWER_SCHEMA)
        return out["answer"]

    return answer
