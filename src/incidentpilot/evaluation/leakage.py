"""Non-leakage (spec §14.4): nothing an investigator can see may carry a case's answer.

Everything in an observation bundle can reach the model. The generator checks each
bundle against its oracle before writing it, so a leaking case is never committed.
Matching is by text: it catches names and labels, not an answer implied by data.
"""

from incidentpilot.domain.enums import CauseCategory
from incidentpilot.evaluation.oracle import Oracle

ANSWER_WORDS = ("oracle", "root cause", "ground truth", "culprit", "answer key")


class LeakError(ValueError):
    """A bundle would show the investigator part of its answer."""


def forbidden_terms(family: str, oracle: Oracle) -> set[str]:
    """The case's family name, every cause category, its oracle's tests, answer words."""
    terms = {*ANSWER_WORDS, family, family.replace("-", " "), family.replace("-", "_")}
    for category in CauseCategory:
        terms |= {category.value, category.value.replace("_", " ")}
    terms |= set(oracle.discriminating_tests)
    return {term.lower() for term in terms}


def leaks(visible: str, terms: set[str]) -> list[str]:
    text = visible.lower()
    return sorted(term for term in terms if term in text)
