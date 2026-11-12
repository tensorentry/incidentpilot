import json
import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from pydantic import BaseModel, ValidationError
from pydantic_core import to_jsonable_python

from incidentpilot.domain.canonical import sha256_digest
from incidentpilot.domain.enums import (
    Aggregation,
    AlternativeStatus,
    ClaimKind,
    MeasurementUnit,
    ReportAuthor,
    StopReason,
    TerminationReason,
)
from incidentpilot.domain.measurement import MAX_ITEMS
from incidentpilot.domain.report import (
    MAX_OBSERVATIONS,
    MAX_TEXT,
    Alternative,
    CausalHypothesis,
    DiagnosisReport,
    Observation,
    Recommendation,
    ReportCandidate,
    ReportExecution,
    Stop,
)
from incidentpilot.domain.window import TimeWindow

REPO_ROOT = Path(__file__).resolve().parents[3]
SPEC = REPO_ROOT / "docs" / "IncidentPilot_v3_Technical_Design.md"
ERRATA = REPO_ROOT / "docs" / "spec-errata.md"

START = datetime(2026, 11, 4, 18, 15, tzinfo=UTC)
WINDOW = TimeWindow(start_at=START, end_at=START + timedelta(minutes=15))
DIGEST = "a" * 64
SERIES = {
    "kind": "metric_series_window",
    "evidence_id": UUID("00000000-0000-4000-8000-000000000001"),
    "window": WINDOW,
}
DEPLOYMENT = {
    "kind": "deployment_record",
    "evidence_id": UUID("00000000-0000-4000-8000-000000000002"),
}
MEASUREMENT = {
    "metric": "latency",
    "aggregation": Aggregation.P95,
    "value": 840,
    "unit": MeasurementUnit.MILLISECONDS,
    "window": WINDOW,
    "inputs": [SERIES],
    "numerator": None,
    "denominator": None,
}


def exactly(message: str) -> str:
    """Match the whole validation message, so a changed message fails the test."""
    return rf"Value error, {re.escape(message)} \["


QUANTITY_IN_TEXT = exactly("quantities belong in a measurement, not in text")
SUPPORTS_AND_CONTRADICTS = exactly("an observation can't both support and contradict")


def parse[M: BaseModel](model: type[M], fields: dict[str, Any]) -> M:
    """Validate as the model's JSON output would be: strict, with strings for enums."""
    return model.model_validate_json(json.dumps(to_jsonable_python(fields)))


def observation(**changes: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "observation_id": "obs-deploy-01",
        "claim": "Checkout was deployed during the frozen window",
        "claim_kind": ClaimKind.OBSERVED,
        "references": [DEPLOYMENT],
        "measurement": None,
    }
    return fields | changes


def computed(**changes: Any) -> dict[str, Any]:
    return (
        observation(
            observation_id="obs-latency-01",
            claim="Checkout request latency increased during the frozen window",
            claim_kind=ClaimKind.COMPUTED,
            references=[SERIES],
            measurement=MEASUREMENT,
        )
        | changes
    )


def hypothesis(**changes: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "cause_id": "hyp-01",
        "component": "checkout-api",
        "category": "deployment_regression",
        "mechanism": "A synchronous inventory call was added to checkout",
        "role": "trigger",
        "confidence": "medium",
        "supports": ["obs-latency-01", "obs-deploy-01"],
        "contradicts": [],
        "unresolved_tests": ["Check checkout-to-inventory span durations"],
    }
    return fields | changes


def alternative(**changes: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "description": "Payment dependency degradation",
        "status": AlternativeStatus.UNRESOLVED,
        "supports": [],
        "contradicts": [],
    }
    return fields | changes


def recommendation(**changes: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "kind": "diagnostic",
        "description": "Collect the checkout-to-inventory latency breakdown",
        "references": ["obs-latency-01"],
        "preconditions": ["Trace sampling is enabled for checkout"],
        "risk": "None, the step only reads telemetry",
        "verification": "Span timings show where checkout waits",
    }
    return fields | changes


def candidate(**changes: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "diagnostic_outcome": "diagnosed",
        "observations": [observation(), computed()],
        "hypotheses": [hypothesis()],
        "root_causes": ["hyp-01"],
        "impact": {"affected_services": ["checkout-api"], "measurements": [MEASUREMENT]},
        "alternatives": [alternative()],
        "recommendations": [recommendation()],
        "limitations": ["Trace-level dependency timing is sampled"],
    }
    return fields | changes


def execution(**changes: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "mode": "offline_demo",
        "author": ReportAuthor.MODEL,
        "termination_reason": TerminationReason.INVESTIGATOR_FINISHED,
        "attempt_count": 1,
        "usage": {
            "model_requests_settled": 3,
            "model_requests_unknown": 0,
            "tool_calls_completed": 5,
            "input_tokens": 9000,
            "output_tokens": 1200,
        },
    }
    return fields | changes


def report(**changes: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "identity": {
            "schema_version": "3.0",
            "investigation_id": UUID("00000000-0000-4000-8000-000000000003"),
            "snapshot_id": "snap-case-a",
            "config_sha256": DIGEST,
            "prompt_sha256": "b" * 64,
        },
        "execution": execution(),
        "validation": {
            "structural": "passed",
            "numeric": "not_run",
            "semantic": "not_assessed",
            "repair_used": False,
        },
        "candidate": candidate(),
    }
    return fields | changes


def string_paths(value: Any, path: tuple[str | int, ...] = ()) -> Iterator[tuple[str | int, ...]]:
    if isinstance(value, str):
        yield path
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from string_paths(item, (*path, key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from string_paths(item, (*path, index))


def replaced(document: Any, path: tuple[str | int, ...], value: Any) -> Any:
    copy = json.loads(json.dumps(document))
    target = copy
    for step in path[:-1]:
        target = target[step]
    target[path[-1]] = value
    return copy


def object_schemas(schema: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    yield "root", schema
    yield from schema.get("$defs", {}).items()


CANDIDATE_JSON = json.loads(parse(ReportCandidate, candidate()).model_dump_json())
CANDIDATE_STRINGS = list(string_paths(CANDIDATE_JSON))


def test_a_full_candidate_is_accepted() -> None:
    parsed = parse(ReportCandidate, candidate())

    assert parsed.observations[1].measurement is not None
    assert parsed.observations[1].measurement.value == 840.0


def test_a_candidate_round_trips_through_python_and_json() -> None:
    parsed = parse(ReportCandidate, candidate())

    assert ReportCandidate.model_validate(parsed.model_dump()) == parsed
    assert ReportCandidate.model_validate_json(parsed.model_dump_json()) == parsed


def test_candidate_fields_are_the_ones_errata_e5_lists() -> None:
    match = re.search(
        r"`ReportCandidate` is the only schema the model sees: ([^.]+)\.", ERRATA.read_text()
    )
    assert match is not None

    assert list(ReportCandidate.model_fields) == re.findall(r"`(\w+)`", match.group(1))


def test_fields_errata_e5_moves_out_of_the_candidate_are_application_written() -> None:
    match = re.search(r"([^.]+) move there\.", ERRATA.read_text())
    assert match is not None
    moved = set(re.findall(r"`(\w+)`", match.group(1)))
    sections = DiagnosisReport.model_fields
    application_written = {
        name
        for section in ("identity", "execution", "validation")
        for name in _model(sections[section].annotation).model_fields
    }

    assert moved == {"schema_version", "investigation_id", "snapshot_id", "termination_reason"}
    assert moved <= application_written
    assert not moved & set(_all_field_names(ReportCandidate))


def _model(annotation: Any) -> type[BaseModel]:
    assert isinstance(annotation, type)
    assert issubclass(annotation, BaseModel)
    return annotation


def _all_field_names(model: type[BaseModel]) -> Iterator[str]:
    for _, schema in object_schemas(model.model_json_schema()):
        yield from schema.get("properties", {})


def test_the_spec_sample_hypothesis_is_accepted() -> None:
    sample = re.search(r'"hypotheses": \[(\{.*?\})\]', SPEC.read_text())
    assert sample is not None

    CausalHypothesis.model_validate_json(sample.group(1))


@pytest.mark.parametrize("model", [ReportCandidate, DiagnosisReport])
def test_every_object_forbids_unknown_fields_and_requires_every_field(
    model: type[BaseModel],
) -> None:
    # Strict structured output needs both, and T1.6 depends on this schema.
    for name, schema in object_schemas(model.model_json_schema()):
        if schema.get("type") == "object":
            assert schema.get("additionalProperties") is False, name
            assert set(schema.get("required", [])) == set(schema["properties"]), name


def test_unknown_fields_are_rejected_at_every_level() -> None:
    objects = list(_object_paths(CANDIDATE_JSON))
    assert len(objects) > 10
    for path in objects:
        document = json.loads(json.dumps(CANDIDATE_JSON))
        target = document
        for step in path:
            target = target[step]
        target["requires_approval"] = False
        with pytest.raises(ValidationError, match="extra"):
            ReportCandidate.model_validate_json(json.dumps(document))


def _object_paths(value: Any, path: tuple[str | int, ...] = ()) -> Iterator[tuple[str | int, ...]]:
    if isinstance(value, dict):
        yield path
        for key, item in value.items():
            yield from _object_paths(item, (*path, key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _object_paths(item, (*path, index))


@pytest.mark.parametrize("path", CANDIDATE_STRINGS, ids=lambda path: ".".join(map(str, path)))
def test_no_model_written_string_can_carry_a_quantity(path: tuple[str | int, ...]) -> None:
    # Prose rejects the quantity; every other string is an enum, pattern, UUID or time.
    with pytest.raises(ValidationError):
        ReportCandidate.model_validate_json(json.dumps(replaced(CANDIDATE_JSON, path, "40%")))


def test_the_quantity_check_covers_many_strings() -> None:
    assert len(CANDIDATE_STRINGS) > 25


@pytest.mark.parametrize(
    "claim",
    [
        "Latency rose by 40%",
        "Latency rose by 40 percent",
        "Latency rose by 40 Percent",
        "p95 reached 300ms",
        "p95 reached 1.2 s",
        "Errors began 5 minutes after the deploy",
        "Traffic was 3x the baseline",
        "Traffic was 3\u00d7 the baseline",
        "Payloads grew to 2,048 KB",
        "Throughput fell to 120 req/s",
        "Pods restarted after 2 hours",
    ],
)
def test_quantities_in_prose_are_rejected(claim: str) -> None:
    with pytest.raises(ValidationError, match=QUANTITY_IN_TEXT):
        parse(Observation, observation(claim=claim))


@pytest.mark.parametrize(
    "claim",
    [
        "Checkout returned HTTP 503 responses",
        "Checkout v2 was deployed",
        "p95 latency rose sharply",
        "5xx responses increased",
        "Requests went to 10.0.0.12",
        "The deploy finished at 18:15 UTC",
        "Build 2026.11.04 introduced the call",
    ],
)
def test_identifiers_in_prose_are_allowed(claim: str) -> None:
    parse(Observation, observation(claim=claim))


@pytest.mark.parametrize("claim", ["", "   ", "\n\t"])
def test_blank_prose_is_rejected(claim: str) -> None:
    with pytest.raises(ValidationError):
        parse(Observation, observation(claim=claim))


def test_whitespace_only_prose_names_the_problem() -> None:
    with pytest.raises(ValidationError, match=exactly("text must not be blank")):
        parse(Observation, observation(claim="  "))


def test_prose_length_is_bounded() -> None:
    parse(Observation, observation(claim="a" * MAX_TEXT))
    with pytest.raises(ValidationError):
        parse(Observation, observation(claim="a" * (MAX_TEXT + 1)))


def test_a_computed_claim_needs_a_measurement() -> None:
    with pytest.raises(ValidationError, match="a computed claim needs a measurement"):
        parse(Observation, computed(measurement=None))


def test_an_observed_claim_has_no_measurement() -> None:
    with pytest.raises(ValidationError, match="an observed claim has no measurement"):
        parse(Observation, observation(measurement=MEASUREMENT))


@pytest.mark.parametrize(
    "references",
    [[], [DEPLOYMENT, DEPLOYMENT], ["obs-latency-01"], [{"evidence_id": SERIES["evidence_id"]}]],
    ids=["empty", "repeated", "observation-id", "no-kind"],
)
def test_observations_cite_distinct_evidence_selectors(references: list[Any]) -> None:
    with pytest.raises(ValidationError):
        parse(Observation, observation(references=references))


@pytest.mark.parametrize(
    "observation_id",
    ["", "Obs-01", "obs 01", "-obs", "obs-01\n", "o" * 65, "obs/01"],
)
def test_local_ids_are_constrained(observation_id: str) -> None:
    with pytest.raises(ValidationError):
        parse(Observation, observation(observation_id=observation_id))


def test_the_longest_local_id_is_accepted() -> None:
    parse(Observation, observation(observation_id="o" * 64))


def test_an_observation_cannot_both_support_and_contradict_a_hypothesis() -> None:
    with pytest.raises(ValidationError, match=SUPPORTS_AND_CONTRADICTS):
        parse(CausalHypothesis, hypothesis(contradicts=["obs-deploy-01"]))


@pytest.mark.parametrize(
    "changes",
    [
        {"component": "Checkout API"},
        {"category": "Deployment-Regression"},
        {"role": "root"},
        {"confidence": "certain"},
        {"confidence": 0.9},
        {"supports": ["obs-latency-01", "obs-latency-01"]},
        {"supports": [f"obs-{n}" for n in range(MAX_ITEMS + 1)]},
        {"unresolved_tests": ["Check spans"] * (MAX_ITEMS + 1)},
    ],
)
def test_malformed_hypotheses_are_rejected(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        parse(CausalHypothesis, hypothesis(**changes))


def test_a_hypothesis_may_reach_the_list_bounds() -> None:
    parse(
        CausalHypothesis,
        hypothesis(
            supports=[f"obs-{n}" for n in range(MAX_ITEMS)],
            unresolved_tests=["Check spans"] * MAX_ITEMS,
        ),
    )


@pytest.mark.parametrize(
    ("status", "supports", "contradicts"),
    [
        ("unresolved", [], []),
        ("unresolved", ["obs-1"], ["obs-2"]),
        ("supported", ["obs-1"], []),
        ("supported", ["obs-1"], ["obs-2"]),
        ("contradicted", [], ["obs-2"]),
        ("contradicted", ["obs-1"], ["obs-2"]),
    ],
)
def test_backed_alternatives_are_accepted(
    status: str, supports: list[str], contradicts: list[str]
) -> None:
    parse(Alternative, alternative(status=status, supports=supports, contradicts=contradicts))


def test_missing_evidence_is_not_contradiction() -> None:
    with pytest.raises(ValidationError, match="needs a contradicting observation"):
        parse(Alternative, alternative(status="contradicted", supports=["obs-1"]))


def test_a_supported_alternative_needs_support() -> None:
    with pytest.raises(ValidationError, match="needs a supporting observation"):
        parse(Alternative, alternative(status="supported", contradicts=["obs-1"]))


def test_an_observation_cannot_both_support_and_contradict_an_alternative() -> None:
    with pytest.raises(ValidationError, match=SUPPORTS_AND_CONTRADICTS):
        parse(
            Alternative, alternative(status="unresolved", supports=["obs-1"], contradicts=["obs-1"])
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"references": []},
        {"references": ["obs-1", "obs-1"]},
        {"references": [SERIES]},
        {"kind": "rollback"},
        {"requires_approval": False},
    ],
    ids=["no-reference", "repeated", "selector", "unknown-kind", "approval-field"],
)
def test_malformed_recommendations_are_rejected(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        parse(Recommendation, recommendation(**changes))


def test_a_simulation_candidate_is_a_recommendation_kind() -> None:
    parse(Recommendation, recommendation(kind="simulation_candidate"))


@pytest.mark.parametrize(
    "changes",
    [
        {
            "observations": [
                observation(observation_id=f"obs-{n}") for n in range(MAX_OBSERVATIONS + 1)
            ]
        },
        {"hypotheses": [hypothesis(cause_id=f"hyp-{n}") for n in range(MAX_ITEMS + 1)]},
        {"root_causes": ["hyp-01", "hyp-01"]},
        {"impact": {"affected_services": ["checkout-api", "checkout-api"], "measurements": []}},
        {"impact": {"affected_services": ["checkout-api"]}},
        {"limitations": ["Trace timing is sampled"] * (MAX_ITEMS + 1)},
        {"diagnostic_outcome": "resolved"},
    ],
    ids=[
        "too-many-observations",
        "too-many-hypotheses",
        "repeated-root-cause",
        "repeated-service",
        "impact-without-measurements",
        "too-many-limitations",
        "unknown-outcome",
    ],
)
def test_malformed_candidates_are_rejected(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        parse(ReportCandidate, candidate(**changes))


def test_a_candidate_may_reach_the_observation_bound() -> None:
    parse(
        ReportCandidate,
        candidate(
            observations=[observation(observation_id=f"obs-{n}") for n in range(MAX_OBSERVATIONS)]
        ),
    )


def test_an_empty_abstention_is_well_formed() -> None:
    # Whether an outcome's lists may be empty is a cross-record rule (E4, T1.7).
    parse(
        ReportCandidate,
        candidate(
            diagnostic_outcome="insufficient_evidence",
            observations=[],
            hypotheses=[],
            root_causes=[],
            impact={"affected_services": [], "measurements": []},
            alternatives=[],
            recommendations=[],
            limitations=["No telemetry was available"],
        ),
    )


def test_stop_reasons_are_termination_reasons() -> None:
    assert {reason.value for reason in StopReason} <= {reason.value for reason in TerminationReason}


def test_a_stop_carries_a_bounded_detail() -> None:
    parse(Stop, {"reason": "provider_failure", "detail": "x" * MAX_TEXT})
    for detail in ["", "x" * (MAX_TEXT + 1)]:
        with pytest.raises(ValidationError):
            parse(Stop, {"reason": "provider_failure", "detail": detail})
    with pytest.raises(ValidationError):
        parse(Stop, {"reason": "deadline_reached", "detail": "Ran out of time"})


def test_a_full_report_is_accepted_and_digestible() -> None:
    parsed = parse(DiagnosisReport, report())
    reparsed = DiagnosisReport.model_validate_json(parsed.model_dump_json())

    assert reparsed == parsed
    assert DiagnosisReport.model_validate(parsed.model_dump()) == parsed
    assert sha256_digest(reparsed.model_dump()) == sha256_digest(parsed.model_dump())


def test_an_application_abstention_is_accepted() -> None:
    parse(
        ReportExecution, execution(author="application", termination_reason="no_admissible_report")
    )


@pytest.mark.parametrize(
    "reason",
    [
        reason
        for reason in TerminationReason
        if reason is not TerminationReason.INVESTIGATOR_FINISHED
        and reason is not TerminationReason.CANCELLED
    ],
)
def test_the_model_authors_a_report_only_when_it_finished(reason: TerminationReason) -> None:
    parse(ReportExecution, execution(author="application", termination_reason=reason))
    with pytest.raises(ValidationError, match="only a finished investigator's candidate"):
        parse(ReportExecution, execution(termination_reason=reason))


def test_an_application_report_cannot_claim_the_investigator_finished() -> None:
    with pytest.raises(ValidationError, match="only a finished investigator's candidate"):
        parse(ReportExecution, execution(author="application"))


@pytest.mark.parametrize("author", list(ReportAuthor))
def test_a_cancelled_investigation_publishes_no_report(author: ReportAuthor) -> None:
    with pytest.raises(ValidationError, match="a cancelled investigation publishes no report"):
        parse(ReportExecution, execution(author=author, termination_reason="cancelled"))


@pytest.mark.parametrize(
    ("section", "changes"),
    [
        ("validation", {"semantic": "passed"}),
        ("validation", {"structural": "not_run"}),
        ("validation", {"structural": "failed"}),
        ("validation", {"numeric": "failed"}),
        ("validation", {"numeric": "not_assessed"}),
        ("validation", {"repair_used": "no"}),
        ("execution", {"mode": "replay"}),
        ("execution", {"attempt_count": 0}),
        ("execution", {"usage": execution()["usage"] | {"input_tokens": -1}}),
        ("identity", {"schema_version": "2.0"}),
        ("identity", {"config_sha256": "A" * 64}),
        ("identity", {"prompt_sha256": "a" * 63}),
        ("identity", {"snapshot_id": ""}),
        ("identity", {"owner_id": UUID("00000000-0000-4000-8000-0000000000a1")}),
    ],
)
def test_malformed_report_sections_are_rejected(section: str, changes: dict[str, Any]) -> None:
    fields = report()
    with pytest.raises(ValidationError):
        parse(DiagnosisReport, fields | {section: fields[section] | changes})


def test_numeric_validation_may_have_passed() -> None:
    fields = report()
    parse(DiagnosisReport, fields | {"validation": fields["validation"] | {"numeric": "passed"}})


def test_live_reports_are_accepted() -> None:
    parse(ReportExecution, execution(mode="live"))
