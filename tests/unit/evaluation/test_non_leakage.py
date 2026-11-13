"""Spec §14.4 and IP3-003: no oracle metadata reaches what an investigator can see.

The committed files are read as a worker would mount them, so these tests hold
even if someone edits a file by hand instead of regenerating it.
"""

import dataclasses
import re
from functools import cache
from pathlib import Path

import pytest

from incidentpilot.adapters.fixture_bundle import ObservationBundle
from incidentpilot.domain.enums import CauseCategory
from incidentpilot.evaluation.cases import CASES, GeneratedCase
from incidentpilot.evaluation.generate import write_case
from incidentpilot.evaluation.leakage import ANSWER_WORDS, LeakError, forbidden_terms, leaks
from incidentpilot.evaluation.oracle import Oracle

REPO_ROOT = Path(__file__).resolve().parents[3]
SPEC = REPO_ROOT / "docs" / "IncidentPilot_v3_Technical_Design.md"
OBSERVATIONS = REPO_ROOT / "scenarios" / "observations"
VISIBLE_FILES = sorted(OBSERVATIONS.iterdir())
BUNDLE_FILES = sorted(OBSERVATIONS.glob("*.json"))


@cache
def cases() -> list[GeneratedCase]:
    return [build(seed) for build, seed in CASES]


@cache
def every_forbidden_term() -> set[str]:
    """Any case's answer is forbidden in every bundle, not only its own."""
    terms = {term for case in cases() for term in forbidden_terms(case.family, case.oracle)}
    spec_case_ids = re.findall(r"`(case-[0-9A-Z]{3})`", SPEC.read_text())
    assert len(spec_case_ids) == 3
    return terms | {case_id.lower() for case_id in spec_case_ids}


def test_only_bundles_and_a_readme_are_visible() -> None:
    names = {path.name for path in VISIBLE_FILES} - {"README.md"}

    assert len(VISIBLE_FILES) == len(CASES) + 1
    assert names == {path.name for path in BUNDLE_FILES}
    assert all(re.fullmatch(r"snap-[0-9a-f]{12}\.json", name) for name in names)


@pytest.mark.parametrize("path", VISIBLE_FILES, ids=lambda path: path.name)
def test_no_visible_file_names_an_answer(path: Path) -> None:
    assert leaks(path.name + "\n" + path.read_text(), every_forbidden_term()) == []


@pytest.mark.parametrize("path", BUNDLE_FILES, ids=lambda path: path.name)
def test_visible_files_are_bundles_with_no_room_for_more(path: Path) -> None:
    # Unknown fields are rejected, so no file can carry an oracle section.
    bundle = ObservationBundle.model_validate_json(path.read_bytes())

    assert f"{bundle.manifest.snapshot_id}.json" == path.name


@pytest.mark.parametrize("path", BUNDLE_FILES, ids=lambda path: path.name)
def test_every_bundle_has_an_oracle_kept_elsewhere(path: Path) -> None:
    oracle_file = REPO_ROOT / "scenarios" / "oracle" / path.name

    assert Oracle.model_validate_json(oracle_file.read_bytes()).snapshot_id == path.stem
    assert "scenarios/oracle" not in path.read_text()


def test_the_terms_cover_names_categories_tests_and_answer_words() -> None:
    case = cases()[0]
    terms = forbidden_terms(case.family, case.oracle)

    assert {"synchronous-dependency", "synchronous dependency", "synchronous_dependency"} <= terms
    assert {category.value for category in CauseCategory} <= terms
    assert {category.value.replace("_", " ") for category in CauseCategory} <= terms
    assert {test.lower() for test in case.oracle.discriminating_tests} <= terms
    assert set(ANSWER_WORDS) <= terms
    assert all(term == term.lower() for term in terms)


def test_an_unanswerable_case_still_forbids_its_name_and_tests() -> None:
    case = cases()[2]
    terms = forbidden_terms(case.family, case.oracle)

    assert case.oracle.causes == []
    assert "missing dependency telemetry" in terms
    assert "dependency latency during the incident window" in terms


def test_leaks_ignores_case_and_lists_every_term_in_order() -> None:
    found = leaks(
        "The ROOT CAUSE was a Dependency Degradation",
        {"root cause", "dependency degradation", "oracle"},
    )

    assert found == ["dependency degradation", "root cause"]
    assert leaks("Checkout requests are slow", {"root cause"}) == []


def planted(term: str) -> GeneratedCase:
    case = cases()[1]
    log = case.bundle.logs[0].model_copy(update={"message": f"Suspected {term} upstream"})
    bundle = case.bundle.model_copy(update={"logs": [log, *case.bundle.logs[1:]]})
    return dataclasses.replace(case, bundle=bundle)


@pytest.mark.parametrize(
    "term",
    ["dependency_degradation", "resource saturation", "Root cause", "dependency-degradation"],
)
def test_the_generator_refuses_to_write_a_leaking_case(term: str, tmp_path: Path) -> None:
    with pytest.raises(LeakError, match=re.escape(term.lower())):
        write_case(planted(term), tmp_path)

    assert list(tmp_path.iterdir()) == []


def test_the_generator_writes_a_case_that_mentions_only_telemetry(tmp_path: Path) -> None:
    write_case(planted("slowness"), tmp_path)

    assert sorted(path.name for path in tmp_path.iterdir()) == ["observations", "oracle"]


def test_a_leak_error_names_the_file_and_every_term() -> None:
    case = planted("traffic surge, by the root cause")
    expected = f"{case.bundle.manifest.snapshot_id}.json would show the investigator: "
    with pytest.raises(LeakError, match=rf"^{re.escape(expected)}root cause, traffic surge$"):
        write_case(case, Path("unused"))
