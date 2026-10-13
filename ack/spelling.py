"""Spell checking for questions, before they're sent and before they're answered.

    fixed, changes = correct("how do you retry falky https cklass", extra={"backoff"})
    # fixed   == "how do you retry flaky https class"
    # changes == [("falky", "flaky"), ("cklass", "class")]

An English word list alone mangles technical text ("json" -> "son", "https" ->
"http's"), so three layers decide what counts as a word:
  1. a built-in technical vocabulary (always correct, and preferred as a fix)
  2. the caller's own words: the parts a project shares, its file names
  3. an English frequency dictionary (pyspellchecker)

Tokens that look like code are never touched: URLs, paths, file names, numbers,
snake_case, camelCase, ACRONYMS, anything in backticks, and words of two letters
or fewer. Words of up to four letters only get one-letter fixes.
"""

import re
import threading
from collections.abc import Iterable

TECH = """
api apis app apps async await auth authn authz backend backoff backoffs base64 bash boolean branch broker bugfix
cache caching callback callbacks cdn cli codebase config configs cors cpu cron css csv daemon dashboard database
datastore dataset debounce debug decrypt dedupe deduplicate default deps devops diff diffs dns docker dockerfile
docstring dotenv email emails encrypt endpoint endpoints enum env exponential failover fastapi favicon fetch fetcher
filesystem flaky flask frontend gateway git github gitlab golang graphql grpc gzip hash hashes hashing healthcheck hmac
hostname html http https hyperlink idempotency idempotent iframe impl init inline integer io iterator javascript jitter
jitters jpeg json jsonl jwt kafka keyring kubernetes lambda latency linter localhost lockfile login logout lookup
macos markdown metadata microservice middleware mime mixin monorepo mutex mysql namespace nginx nodejs npm nullable oauth
offline onboarding openapi os pagination parser passwordless payload payloads pdf pid pip png postgres postgresql
prefetch preload prod proxy pubsub pytest python queue queues ratelimit ratelimiter react readme realtime redis refactor
regex repo repos resize retries retry retrying retried rpc runtime rust s3 sandbox schema schemas scraper scrapers sdk
serializer serverless sha sha256 signup slug slugify slugs sqlite ssh ssl stderr stdin stdout subdomain subprocess svg
sync tcp templating timeout timeouts timestamp timestamps tls todo token tokens tokenizer toml tsx typescript ui uid
unicode unittest unix upload uploads uri url urls usb utf utf8 uuid validator vendor vendored webhook webhooks websocket
websockets whitelist allowlist wifi workflow workflows xml yaml yml zip
"""
TECH_WORDS = set(TECH.split())

_CODE_LIKE = re.compile(r"[0-9_/\\.@:#=<>{}\[\]()$%^&*+|~]|[a-z][A-Z]|^[A-Z]{2,}s?$")
_WORD = re.compile(r"^([\"'(\[]*)([A-Za-z][A-Za-z']*[A-Za-z]|[A-Za-z])([\"')\].,!?;:]*)$")
_lock = threading.Lock()
_checker = None
_added: set[str] = set()


def _spell(extra: Iterable[str]):
    """The shared checker, with the technical and extra words loaded as very common words."""
    global _checker
    from spellchecker import SpellChecker

    with _lock:
        if _checker is None:
            _checker = SpellChecker(distance=2)
            _add(TECH_WORDS)
        _add({w.lower() for w in extra if w and w.isalpha()} - _added)
        return _checker


def _add(words: set[str]) -> None:
    for w in words:
        _checker.word_frequency.add(w, 10**7)  # outranks any ordinary English word as a fix
    _added.update(words)


def _match_case(original: str, fixed: str) -> str:
    if original.isupper():
        return fixed.upper()
    if original[0].isupper():
        return fixed[0].upper() + fixed[1:]
    return fixed


def correct(text: str, extra: Iterable[str] = ()) -> tuple[str, list[tuple[str, str]]]:
    """Return (corrected text, [(typed, corrected), ...]). Whitespace and punctuation are kept."""
    try:
        sp = _spell(extra)
    except ImportError:  # pyspellchecker missing: never block a question over spelling
        return text, []
    out, changes, in_code = [], [], False
    for token in re.split(r"(\s+)", text):
        if not token or token.isspace():
            out.append(token)
            continue
        if token.count("`") % 2:
            in_code = not in_code
        m = _WORD.match(token)
        if in_code or "`" in token or not m or _CODE_LIKE.search(m.group(2)):
            out.append(token)
            continue
        lead, word, trail = m.groups()
        low = word.lower()
        if len(low) <= 2 or low in TECH_WORDS or low in sp or low.replace("'", "") in sp:
            out.append(token)
            continue
        sp.distance = 1 if len(low) <= 4 else 2
        fix = sp.correction(low)
        sp.distance = 2
        if not fix or fix == low or "'" in fix and "'" not in low:
            out.append(token)
            continue
        fixed = _match_case(word, fix)
        changes.append((word, fixed))
        out.append(lead + fixed + trail)
    return "".join(out), changes
