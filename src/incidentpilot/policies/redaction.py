"""The minimal sanitizer (spec D04, \u00a714.5, \u00a715.1, \u00a715.2).

Untrusted text passes through `sanitize` before it's stored or sent to a provider.
That's incident descriptions now, and logs and model output later. The sanitizer:

- drops characters that hide text from a reader, such as bidirectional controls,
  zero-width characters and Unicode tag characters, as well as controls PostgreSQL
  can't store, such as NUL;
- redacts credential shapes, email addresses and the test canary;
- bounds the length.

It's a safety net over synthetic data (\u00a715.1), not a guarantee: it recognises the
shapes below and nothing else. Any change to what it does needs a new
SANITIZER_VERSION, because stored evidence records the version that produced it.
"""

import re
import unicodedata
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass

SANITIZER_VERSION = "1"
TRUNCATED = "…[truncated]"
# Text past the limit is still read this far, so a secret that straddles the limit is
# recognised and redacted rather than cut into an unrecognisable prefix.
LOOKAHEAD = 512
# Everything else in these categories is dropped: controls, format characters (which
# include bidirectional overrides, zero-width characters and tag characters), and
# unpaired surrogates, which can't be encoded as UTF-8.
DROPPED_CATEGORIES = frozenset({"Cc", "Cf", "Cs"})
KEPT_CONTROLS = frozenset({"\t", "\n"})
LINE_BREAKS = (("\r\n", "\n"), ("\r", "\n"), ("\u2028", "\n"), ("\u2029", "\n"), ("\x85", "\n"))


@dataclass(frozen=True)
class Rule:
    kind: str
    pattern: re.Pattern[str]
    # Groups the replacement keeps, such as the key in `password=...`.
    keep: str = ""


MARKER_START = "[REDACTED-"


def marker(kind: str) -> str:
    # No colon or @, so a marker never looks like the credentials it replaced.
    return f"{MARKER_START}{kind}]"


# Specific shapes come before generic ones, so counts name the most specific kind.
RULES: tuple[Rule, ...] = (
    Rule(
        "private_key",
        re.compile(
            r"-----BEGIN [A-Z0-9 ]{0,40}PRIVATE KEY-----.*?"
            r"(?:-----END [A-Z0-9 ]{0,40}PRIVATE KEY-----|\Z)",
            re.DOTALL,
        ),
    ),
    Rule(
        "url_credentials",
        re.compile(r"\b([A-Za-z][A-Za-z0-9+.-]{0,31}://)[^\s/:@]{1,256}:[^\s/@]{1,256}(?=@)"),
        keep=r"\g<1>",
    ),
    Rule("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    Rule("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    Rule("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_\w{22,})")),
    Rule("provider_key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")),
    Rule("slack_token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}")),
    Rule(
        "bearer_token",
        re.compile(r"\b([Bb]earer[ \t]+)[A-Za-z0-9._~+/=-]{8,}"),
        keep=r"\g<1>",
    ),
    Rule(
        "credential",
        re.compile(
            r"(?i)\b(password|passwd|pwd|secret|client_secret|api[_-]?key"
            r"|access[_-]?token|auth[_-]?token|token)"
            r"([ \t]{0,8}[:=][ \t]{0,8}[\"']?)(?!\[REDACTED-|…\[truncated\])[^\s\"',;&]+"
        ),
        keep=r"\g<1>\g<2>",
    ),
    # No word boundaries: the canary goes whatever it's glued to.
    Rule("canary", re.compile(r"ip-canary-[0-9a-f]{16}")),
    Rule(
        "email",
        re.compile(
            r"\b[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63}){0,8}"
            r"\.[A-Za-z]{2,24}\b"
        ),
    ),
)


@dataclass(frozen=True)
class Sanitized:
    text: str
    # How many of each kind were redacted. Never the values themselves.
    redactions: Mapping[str, int]
    dropped_characters: int
    truncated: bool


def sanitize(text: str, *, limit: int) -> Sanitized:
    if limit <= len(TRUNCATED):
        raise ValueError(f"limit must exceed {len(TRUNCATED)} characters")
    examined = text[: limit + LOOKAHEAD]
    visible, dropped = _visible(examined)
    counts: Counter[str] = Counter()
    redacted = _redact(visible, counts)
    redactions = {kind: count for kind, count in counts.items() if count}
    truncated = len(examined) < len(text) or len(redacted) > limit
    if truncated:
        redacted = redacted[: _cut(redacted, limit - len(TRUNCATED))] + TRUNCATED
    return Sanitized(redacted, redactions, dropped, truncated)


def _cut(text: str, end: int) -> int:
    """Where to cut so no marker is split, since half a marker isn't recognised as one."""
    # Any marker that starts before the cut, even one whose own prefix runs past it.
    start = text[: end + len(MARKER_START) - 1].rfind(MARKER_START)
    if start != -1 and text.find("]", start) >= end:
        return start
    return end


def _visible(text: str) -> tuple[str, int]:
    for line_break, replacement in LINE_BREAKS:
        text = text.replace(line_break, replacement)
    kept = [
        character
        for character in text
        if character in KEPT_CONTROLS or unicodedata.category(character) not in DROPPED_CATEGORIES
    ]
    return unicodedata.normalize("NFC", "".join(kept)), len(text) - len(kept)


def _redact(text: str, counts: Counter[str]) -> str:
    """Apply every rule, then again until the text stops changing.

    One pass isn't enough: a redaction can create the word boundary another rule needs,
    as when a canary glued to `password=...` is replaced. Markers never match a rule, so
    each pass consumes original text only, and the recursion ends.
    """
    redacted = text
    for rule in RULES:
        redacted, found = rule.pattern.subn(rule.keep + marker(rule.kind), redacted)
        counts[rule.kind] += found
    return text if redacted == text else _redact(redacted, counts)
