"""Queries still find the right part when the words are misspelled, and still find nothing for unrelated text."""

import pytest

from ack.agent import MIN_MATCH
from ack.memory import Entry, ProjectMemory, fix_typos, tokens
from ack.spelling import TECH_WORDS, correct


@pytest.fixture
def memory(tmp_path):
    mem = ProjectMemory(tmp_path)
    parts = [
        Entry(id="retry-backoff", base="retry", files=["retry.py"], topic="retry flaky calls with exponential backoff",
              tags=["http", "resilience", "retries"], summary="retry(fn, attempts, base): sleeping base*2^i between failures"),
        Entry(id="slugify", base="text", files=["slugify.py"], topic="turn titles into url slugs", tags=["text", "url"],
              summary="slugify(s): lowercase, runs of non-alphanumerics become '-'"),
        Entry(id="geo-capitals", base="geo", files=["geo.py"], topic="country capital lookup", tags=["geo"],
              summary="capitals(): dict of ISO country code to capital city"),
        Entry(id="webhook-sign", base="hooks", files=["sign.py"], topic="sign and verify outgoing webhooks",
              tags=["webhook", "hmac", "security", "signature"], summary="sign(secret, body): HMAC-SHA256 webhook signatures"),
    ]
    for part in parts:
        mem.add(part)
    return mem


def top_hit(memory, query):
    # The same two stages ask() runs: spelling first, then matching against the parts.
    fixed, _ = correct(query, memory.vocabulary())
    score, entry = memory.search(fixed, k=3)[0]
    return entry.id, score


@pytest.mark.parametrize("query, want", [
    ("retry flaky calls with exponential backoff", "retry-backoff"),
    ("retry flaky calls with exponental backof", "retry-backoff"),
    ("retyr flaky calls", "retry-backoff"),
    ("exponentail backof", "retry-backoff"),
    ("slugifiy titles", "slugify"),
    ("sluglify titles", "slugify"),
    ("geo capitls", "geo-capitals"),
    ("geocapitals", "geo-capitals"),
    ("capital lookp", "geo-capitals"),
    ("webook signtures hmac", "webhook-sign"),
    ("web hook sgn", "webhook-sign"),
    ("hmc signatur", "webhook-sign"),
])
def test_typo_still_finds_the_part(memory, query, want):
    entry_id, score = top_hit(memory, query)
    assert entry_id == want
    assert score >= MIN_MATCH


@pytest.mark.parametrize("query", ["kubernetes cluster autoscaler", "ratelimt"])
def test_unrelated_query_still_finds_nothing(memory, query):
    _, score = top_hit(memory, query)
    assert score < MIN_MATCH


def test_known_words_pass_through_unchanged():
    known = {"retry", "backoff"}
    assert fix_typos(["retry", "backoff"], known) == ["retry", "backoff"]


def test_short_words_are_not_guessed():
    # Words under four letters are never rewritten, and a 4-5 letter word may change by one edit only.
    assert fix_typos(["abc"], {"abd"}) == ["abc"]
    assert fix_typos(["tast"], {"test", "toast"}) == ["test"]
    assert fix_typos(["xqzv"], {"test"}) == ["xqzv"]


def test_compound_is_split_into_known_parts():
    assert fix_typos(["geocapitals"], {"geo", "capitals"}) == ["geo", "capitals"]


def test_technical_vocabulary_is_known_without_a_part(memory):
    assert "webhook" in TECH_WORDS
    assert fix_typos(tokens("webhok"), {t for w in TECH_WORDS for t in tokens(w)}) == ["webhook"]
