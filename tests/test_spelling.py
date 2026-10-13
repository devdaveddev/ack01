"""The spell corrector: fixes typos, leaves code and technical words alone."""

import pytest

from ack.spelling import correct


@pytest.mark.parametrize("typed, expected", [
    ("how do you retry falky https cklass", "how do you retry flaky https class"),
    ("how do you sign outgong webhoks?", "how do you sign outgoing webhooks?"),
    ("do you have a jiter backof helper", "do you have a jitter backoff helper"),
    ("Teh recieve funtion", "The receive function"),
])
def test_fixes_typos(typed, expected):
    assert correct(typed)[0] == expected


@pytest.mark.parametrize("text", [
    "use JSON over https with an api key and HMAC",
    "see src/retry_utils.py and fetchData() in v2.1",
    "parse `yamll` files from https://example.com/a_b",
    "ok so",
])
def test_leaves_code_and_tech_words_alone(text):
    assert correct(text) == (text, [])


def test_project_words_count_as_spelled_right_and_as_fixes():
    assert correct("whats your slugfy helper", extra={"slugify"})[0] == "whats your slugify helper"
    assert correct("ask about frobnicate", extra={"frobnicate"}) == ("ask about frobnicate", [])


def test_reports_each_change_and_keeps_punctuation_and_case():
    fixed, changes = correct("Waht about it, falky?")
    assert fixed == "What about it, flaky?" and changes == [("Waht", "What"), ("falky", "flaky")]
