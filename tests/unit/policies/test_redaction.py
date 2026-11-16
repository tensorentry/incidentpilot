"""The minimal sanitizer (spec D04, \u00a714.5, \u00a715.1)."""

import re
import time
import unicodedata
from random import Random

import pytest

from incidentpilot.domain.canonical import sha256_digest
from incidentpilot.policies.redaction import (
    DROPPED_CATEGORIES,
    KEPT_CONTROLS,
    LINE_BREAKS,
    LOOKAHEAD,
    RULES,
    SANITIZER_VERSION,
    TRUNCATED,
    _cut,
    sanitize,
)

LIMIT = 2_000
FILLER = "ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"


def clean(text: str, limit: int = LIMIT) -> str:
    return sanitize(text, limit=limit).text


@pytest.mark.parametrize(
    ("text", "expected", "kind"),
    [
        (
            "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY----- after",
            "[REDACTED-private_key] after",
            "private_key",
        ),
        (
            "before -----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXk with no end",
            "before [REDACTED-private_key]",
            "private_key",
        ),
        (
            "postgres://app:s3cret@db:5432/orders",
            "postgres://[REDACTED-url_credentials]@db:5432/orders",
            "url_credentials",
        ),
        (
            "jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N",
            "jwt [REDACTED-jwt]",
            "jwt",
        ),
        ("key AKIAIOSFODNN7EXAMPLE used", "key [REDACTED-aws_access_key] used", "aws_access_key"),
        (f"ghp_{FILLER}abcd", "[REDACTED-github_token]", "github_token"),
        (f"github_pat_{FILLER}", "[REDACTED-github_token]", "github_token"),
        (f"sk-proj-{FILLER}", "[REDACTED-provider_key]", "provider_key"),
        (f"sk-ant-{FILLER}", "[REDACTED-provider_key]", "provider_key"),
        ("xoxb-123456789012-abcdef", "[REDACTED-slack_token]", "slack_token"),
        (
            "Authorization: Bearer abc.def-ghi_jkl",
            "Authorization: Bearer [REDACTED-bearer_token]",
            "bearer_token",
        ),
        ("password=hunter2&next=1", "password=[REDACTED-credential]&next=1", "credential"),
        ('API_KEY: "s3cr3t", ok', 'API_KEY: "[REDACTED-credential]", ok', "credential"),
        ("client_secret = abc;", "client_secret = [REDACTED-credential];", "credential"),
        ("access-token:xyz", "access-token:[REDACTED-credential]", "credential"),
        ("canary ip-canary-0123456789abcdef.", "canary [REDACTED-canary].", "canary"),
        ("glued_ip-canary-0123456789abcdefff", "glued_[REDACTED-canary]ff", "canary"),
        ("ping ops.oncall+pager@example.co.uk now", "ping [REDACTED-email] now", "email"),
    ],
)
def test_each_rule_redacts_its_shape(text: str, expected: str, kind: str) -> None:
    result = sanitize(text, limit=LIMIT)

    assert result.text == expected
    assert result.redactions == {kind: 1}


@pytest.mark.parametrize(
    "text",
    [
        "token count rose after the deploy",
        "sk-short",
        "AKIA1234",
        "ghp_tooshort",
        "the password policy changed",
        "https://example.com/packages/lib@2.0",
        "Bearer of bad news",
        "ip-canary-0123",
        "alerts@localhost",
        "p95 latency 340ms at 12:05:00",
    ],
)
def test_near_misses_pass_unchanged(text: str) -> None:
    assert sanitize(text, limit=LIMIT) == sanitize(text, limit=LIMIT)
    assert clean(text) == text
    assert sanitize(text, limit=LIMIT).redactions == {}


def test_a_generic_key_redacts_whatever_follows_even_prose() -> None:
    # Over-redacting a word is cheaper than missing a credential.
    assert clean("token: expired at noon") == "token: [REDACTED-credential] at noon"


def test_every_match_is_counted_by_kind() -> None:
    result = sanitize("password=a password=b mail a@example.com", limit=LIMIT)

    assert result.redactions == {"credential": 2, "email": 1}


@pytest.mark.parametrize(
    ("character", "name"),
    [
        ("\u202e", "right-to-left override"),
        ("\u2066", "left-to-right isolate"),
        ("\u200b", "zero-width space"),
        ("\u200d", "zero-width joiner"),
        ("\ufeff", "byte order mark"),
        ("\U000e0041", "tag letter A"),
        ("\x00", "NUL"),
        ("\x1b", "escape"),
        ("\x07", "bell"),
        ("\ud800", "unpaired surrogate"),
    ],
)
def test_hidden_and_unstorable_characters_are_dropped(character: str, name: str) -> None:
    result = sanitize(f"a{character}b", limit=LIMIT)

    assert (result.text, result.dropped_characters) == ("ab", 1), name


def test_tabs_and_line_breaks_survive_as_tabs_and_newlines() -> None:
    result = sanitize("a\tb\r\nc\rd\u2028e\u2029f\x85g\nh", limit=LIMIT)

    assert (result.text, result.dropped_characters) == ("a\tb\nc\nd\ne\nf\ng\nh", 0)


def test_text_is_normalized_to_composed_form() -> None:
    assert clean("cafe\u0301") == "caf\u00e9"


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        (f"sk-\u200b{FILLER}", "provider_key"),
        ("pass\u200bword=hunter2", "credential"),
        ("ip-canary-\u202e0123456789abcdef", "canary"),
    ],
)
def test_hidden_characters_cannot_split_a_secret(text: str, kind: str) -> None:
    assert sanitize(text, limit=LIMIT).redactions == {kind: 1}


def test_text_at_the_limit_is_kept_whole() -> None:
    result = sanitize("x" * 50, limit=50)

    assert (result.text, result.truncated) == ("x" * 50, False)


def test_text_over_the_limit_ends_with_a_marker_and_fits() -> None:
    result = sanitize("x" * 51, limit=50)

    assert result.text == "x" * (50 - len(TRUNCATED)) + TRUNCATED
    assert len(result.text) == 50
    assert result.truncated


def test_text_past_the_lookahead_is_truncated_even_if_redaction_shortens_it() -> None:
    result = sanitize("x" + "y" * (40 + LOOKAHEAD), limit=40)

    assert result.truncated
    assert result.text == "x" + "y" * (39 - len(TRUNCATED)) + TRUNCATED


def test_text_is_cut_before_a_marker_rather_than_through_it() -> None:
    # Half a marker isn't recognised, so a second pass would redact it again.
    result = sanitize("password=" + "y" * (40 + LOOKAHEAD), limit=40)

    assert result.text == "password=" + TRUNCATED


def test_text_is_cut_at_the_limit_when_a_marker_ends_before_it() -> None:
    result = sanitize("password=x " + "z" * 60, limit=50)

    assert result.text == "password=[REDACTED-credential] " + "z" * (19 - len(TRUNCATED)) + (
        TRUNCATED
    )


@pytest.mark.parametrize(
    ("text", "end", "expected"),
    [
        ("[REDACTED-email] and [REDACTED-canary] tail", 30, 21),
        ("[REDACTED-canary] tail", 5, 0),
        ("[REDACTED-email] [REDACTED-jwt]", 30, 17),
        ("x [REDACTED-jwt]", 3, 2),
        ("x [REDACTED-jwt]", 2, 2),
        ("x [REDACTED-jwt]", 1, 1),
        ("[REDACTED-email] yy ] zz", 19, 19),
        ("[REDACTED-email] zz", 15, 0),
        ("[REDACTED-email] zz", 16, 16),
        ("plain text ] here", 8, 8),
    ],
    ids=[
        "inside the second of two markers",
        "inside a marker at the start",
        "just before a marker's last bracket",
        "inside a marker's prefix",
        "just before a marker",
        "one character before a marker",
        "after a marker, before a stray bracket",
        "at a marker's closing bracket",
        "just after a marker",
        "no marker",
    ],
)
def test_a_cut_moves_back_to_the_start_of_a_marker_it_would_split(
    text: str, end: int, expected: int
) -> None:
    assert _cut(text, end) == expected


def test_a_separator_never_reaches_the_next_line() -> None:
    assert clean("password=\nnext line") == "password=\nnext line"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "ip-canary-0123456789abcdefpassword=hunter2",
            "[REDACTED-canary]password=[REDACTED-credential]",
        ),
        (
            "ip-canary-0123456789abcdefBearer abcdefghij",
            "[REDACTED-canary]Bearer [REDACTED-bearer_token]",
        ),
    ],
)
def test_a_redaction_that_creates_a_boundary_exposes_the_next_secret(
    text: str, expected: str
) -> None:
    assert clean(text) == expected


def test_a_secret_straddling_the_limit_leaves_no_fragment() -> None:
    result = sanitize("x" * 90 + f" sk-{FILLER}", limit=100)

    assert result.redactions == {"provider_key": 1}
    assert not set(FILLER[:4]) & set(result.text.removeprefix("x" * 90))


@pytest.mark.parametrize("limit", [0, len(TRUNCATED)])
def test_a_limit_must_leave_room_after_the_marker(limit: int) -> None:
    message = f"limit must exceed {len(TRUNCATED)} characters"
    with pytest.raises(ValueError, match=rf"^{re.escape(message)}$"):
        sanitize("x", limit=limit)


def test_the_smallest_limit_holds_one_character_and_the_marker() -> None:
    assert clean("xy" * 20, limit=len(TRUNCATED) + 1) == "x" + TRUNCATED


POOL = [
    *"abcXYZ019 .,:=@/-_\t\n\r",
    "a",
    "ip-canary-",
    "\u202e",
    "\u200b",
    "\x00",
    "\ud800",
    "\u0301",
    "\U000e0041",
    "\u2028",
    "password=",
    "token: ",
    "Bearer ",
    "sk-",
    FILLER,
    "postgres://u:p@h",
    "a@example.com",
    "ip-canary-0123456789abcdef",
    "[REDACTED-email]",
    "-----BEGIN EC PRIVATE KEY-----",
]


@pytest.mark.parametrize("seed", range(200))
def test_sanitized_text_is_stable_bounded_and_storable(seed: int) -> None:
    rng = Random(seed)  # noqa: S311 (reproducible test input, not a secret)
    text = "".join(rng.choice(POOL) for _ in range(rng.randint(0, 120)))
    limit = rng.randint(len(TRUNCATED) + 1, 400)
    once = sanitize(text, limit=limit)
    twice = sanitize(once.text, limit=limit)

    assert (twice.text, twice.redactions, twice.dropped_characters) == (once.text, {}, 0)
    assert len(once.text) <= limit
    assert "\x00" not in once.text
    once.text.encode("utf-8")
    assert all(
        c in KEPT_CONTROLS or unicodedata.category(c) not in DROPPED_CATEGORIES for c in once.text
    )
    assert "ip-canary-0123456789abcdef" not in once.text


@pytest.mark.parametrize(
    "text",
    [
        "a." * 100_000,
        "a@" * 100_000,
        "password=" * 50_000,
        "postgres://" + "a:" * 100_000,
        "-----BEGIN RSA PRIVATE KEY-----" + "x" * 200_000,
    ],
    ids=["dots", "at-signs", "keys", "userinfo", "unterminated key"],
)
def test_pathological_text_is_fast(text: str) -> None:
    started = time.perf_counter()
    sanitize(text, limit=len(text))

    assert time.perf_counter() - started < 2


def test_changing_the_rules_needs_a_new_version() -> None:
    rules = [(r.kind, r.pattern.pattern, r.pattern.flags, r.keep) for r in RULES]
    behaviour = [rules, sorted(DROPPED_CATEGORIES), sorted(KEPT_CONTROLS), LINE_BREAKS]
    behaviour += [TRUNCATED, LOOKAHEAD]

    # If this fails, bump SANITIZER_VERSION and update both values here together. It
    # covers the rule tables only; a change to the functions needs the same bump.
    assert (SANITIZER_VERSION, sha256_digest(behaviour)) == (
        "1",
        "db61a4353f1e67b6815b3374e5b6999c4706b52ddcdbc586713244ec4fc1bd5e",
    )
