import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from pydantic import BaseModel
from pydantic_core import to_jsonable_python

from incidentpilot.domain.canonical import sha256_digest
from incidentpilot.domain.enums import EvidenceKind
from incidentpilot.domain.evidence import EvidenceRecord
from incidentpilot.domain.ids import EvidenceId, OwnerId, SnapshotId
from incidentpilot.domain.report import DiagnosisReport, ReportCandidate
from incidentpilot.domain.report_checks import (
    Issue,
    candidate_issues,
    citation_issues,
    report_issues,
)
from incidentpilot.domain.window import TimeWindow

OWNER = OwnerId(UUID("00000000-0000-4000-8000-0000000000a1"))
OTHER_OWNER = OwnerId(UUID("00000000-0000-4000-8000-0000000000b2"))
SNAPSHOT = SnapshotId("snap-case-a")
START = datetime(2026, 11, 4, 18, 0, tzinfo=UTC)
SERIES_WINDOW = TimeWindow(start_at=START, end_at=START + timedelta(minutes=30))
WINDOW = TimeWindow(start_at=START + timedelta(minutes=15), end_at=START + timedelta(minutes=30))
OUTSIDE = TimeWindow(start_at=START + timedelta(minutes=15), end_at=START + timedelta(minutes=45))


def evidence_id(n: int) -> EvidenceId:
    return EvidenceId(UUID(f"00000000-0000-4000-8000-{n:012d}"))


def record(n: int, kind: EvidenceKind, **changes: Any) -> EvidenceRecord:
    content = {"n": n}
    fields: dict[str, Any] = {
        "evidence_id": evidence_id(n),
        "owner_id": OWNER,
        "snapshot_id": SNAPSHOT,
        "kind": kind,
        "source_ref": f"{kind}:{n}",
        "adapter_version": "fixture-v1",
        "redaction_version": "redact-v1",
        "window": SERIES_WINDOW if kind is EvidenceKind.METRIC_SERIES_WINDOW else None,
        "content": content,
        "content_sha256": sha256_digest(content),
    }
    return EvidenceRecord.model_validate(fields | changes)


EVIDENCE = {
    evidence_id(1): record(1, EvidenceKind.METRIC_SERIES_WINDOW),
    evidence_id(2): record(2, EvidenceKind.DEPLOYMENT_RECORD),
    evidence_id(3): record(3, EvidenceKind.LOG_RECORD),
    evidence_id(4): record(4, EvidenceKind.METRIC_SERIES_WINDOW),
}


def series(n: int = 1, window: TimeWindow = WINDOW) -> dict[str, Any]:
    return {"kind": "metric_series_window", "evidence_id": evidence_id(n), "window": window}


def single(n: int, kind: str) -> dict[str, Any]:
    return {"kind": kind, "evidence_id": evidence_id(n)}


DEPLOYMENT = single(2, "deployment_record")
LOG = single(3, "log_record")


def quantity(n: int = 1, **changes: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "metric": "latency",
        "aggregation": "p95",
        "value": 840,
        "unit": "ms",
        "window": WINDOW,
        "inputs": [series(n)],
    }
    return fields | changes


def measurement(n: int = 1) -> dict[str, Any]:
    return quantity(n, numerator=None, denominator=None)


def ratio(numerator: int = 1, denominator: int = 4) -> dict[str, Any]:
    return quantity(
        metric="error_ratio",
        aggregation="ratio",
        value=0.05,
        unit="ratio",
        inputs=[series(numerator), series(denominator)],
        numerator=quantity(numerator, metric="error_count", aggregation="sum", unit="count"),
        denominator=quantity(denominator, metric="request_count", aggregation="sum", unit="count"),
    )


def observed(observation_id: str, *references: dict[str, Any]) -> dict[str, Any]:
    return {
        "observation_id": observation_id,
        "claim": "Something was observed in the frozen window",
        "claim_kind": "observed",
        "references": list(references),
        "measurement": None,
    }


def computed(observation_id: str = "obs-latency", n: int = 1) -> dict[str, Any]:
    return {
        "observation_id": observation_id,
        "claim": "Checkout latency increased during the frozen window",
        "claim_kind": "computed",
        "references": [series(n)],
        "measurement": measurement(n),
    }


def hypothesis(cause_id: str = "hyp-01", **changes: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "cause_id": cause_id,
        "component": "checkout-api",
        "category": "deployment_regression",
        "mechanism": "A synchronous inventory call was added to checkout",
        "role": "trigger",
        "confidence": "medium",
        "supports": ["obs-latency", "obs-deploy"],
        "contradicts": [],
        "unresolved_tests": [],
    }
    return fields | changes


def alternative(**changes: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "description": "Payment dependency degradation",
        "status": "contradicted",
        "supports": [],
        "contradicts": ["obs-log"],
    }
    return fields | changes


def recommendation(**changes: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "kind": "diagnostic",
        "description": "Collect the checkout-to-inventory latency breakdown",
        "references": ["obs-latency"],
        "preconditions": [],
        "risk": "None, the step only reads telemetry",
        "verification": "Span timings show where checkout waits",
    }
    return fields | changes


def candidate(**changes: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "diagnostic_outcome": "diagnosed",
        "observations": [
            computed(),
            observed("obs-deploy", DEPLOYMENT),
            observed("obs-log", LOG),
        ],
        "hypotheses": [hypothesis()],
        "root_causes": ["hyp-01"],
        "impact": {"affected_services": ["checkout-api"], "measurements": [ratio()]},
        "alternatives": [alternative()],
        "recommendations": [recommendation()],
        "limitations": [],
    }
    return fields | changes


def abstention(**changes: Any) -> dict[str, Any]:
    return (
        candidate(
            diagnostic_outcome="insufficient_evidence",
            hypotheses=[],
            root_causes=[],
            alternatives=[],
            limitations=["The model provider refused the request"],
        )
        | changes
    )


def report(author: str = "model", **changes: Any) -> dict[str, Any]:
    reason = "investigator_finished" if author == "model" else "model_refused"
    fields: dict[str, Any] = {
        "identity": {
            "schema_version": "3.0",
            "investigation_id": UUID("00000000-0000-4000-8000-0000000000c3"),
            "snapshot_id": SNAPSHOT,
            "config_sha256": "a" * 64,
            "prompt_sha256": "b" * 64,
        },
        "execution": {
            "mode": "offline_demo",
            "author": author,
            "termination_reason": reason,
            "attempt_count": 1,
            "usage": {
                "model_requests_settled": 1,
                "model_requests_unknown": 0,
                "tool_calls_completed": 3,
                "input_tokens": 100,
                "output_tokens": 50,
            },
        },
        "validation": {
            "structural": "passed",
            "numeric": "not_run",
            "semantic": "not_assessed",
            "repair_used": False,
        },
        "candidate": candidate() if author == "model" else abstention(),
    }
    return fields | changes


def parse[M: BaseModel](model: type[M], fields: dict[str, Any]) -> M:
    return model.model_validate_json(json.dumps(to_jsonable_python(fields)))


def issues_in(fields: dict[str, Any]) -> list[Issue]:
    return candidate_issues(parse(ReportCandidate, fields))


def citations(fields: dict[str, Any], **evidence: EvidenceRecord) -> list[Issue]:
    index = EVIDENCE | {evidence_id(int(key[1:])): value for key, value in evidence.items()}
    return citation_issues(
        parse(ReportCandidate, fields), index, owner_id=OWNER, snapshot_id=SNAPSHOT
    )


def test_a_consistent_diagnosis_has_no_issues() -> None:
    assert issues_in(candidate()) == []
    assert citations(candidate()) == []


# References between records (E3)


def test_repeated_observation_ids_are_reported() -> None:
    observations = [computed(), observed("obs-deploy", DEPLOYMENT), observed("obs-deploy", LOG)]

    assert issues_in(candidate(observations=observations)) == [
        Issue("observations[2].observation_id", "repeats an earlier ID"),
        Issue("alternatives[0].contradicts[0]", "no observation has this ID"),
    ]


def test_repeated_cause_ids_are_reported() -> None:
    assert issues_in(candidate(hypotheses=[hypothesis(), hypothesis()])) == [
        Issue("hypotheses[1].cause_id", "repeats an earlier ID")
    ]


@pytest.mark.parametrize(
    ("changes", "path"),
    [
        (
            {"hypotheses": [hypothesis(supports=["obs-latency", "obs-x"])]},
            "hypotheses[0].supports[1]",
        ),
        ({"hypotheses": [hypothesis(contradicts=["obs-x"])]}, "hypotheses[0].contradicts[0]"),
        (
            {"alternatives": [alternative(status="supported", supports=["obs-x"])]},
            "alternatives[0].supports[0]",
        ),
        (
            {"alternatives": [alternative(contradicts=["obs-log", "obs-x"])]},
            "alternatives[0].contradicts[1]",
        ),
        (
            {"recommendations": [recommendation(references=["obs-x"])]},
            "recommendations[0].references[0]",
        ),
    ],
)
def test_references_to_unknown_observations_are_reported(
    changes: dict[str, Any], path: str
) -> None:
    assert issues_in(candidate(**changes)) == [Issue(path, "no observation has this ID")]


def test_root_causes_must_name_a_hypothesis() -> None:
    assert issues_in(candidate(root_causes=["hyp-01", "obs-latency"])) == [
        Issue("root_causes[1]", "no hypothesis has this ID")
    ]


def test_an_observation_id_is_not_a_cause_id() -> None:
    issues = issues_in(candidate(hypotheses=[hypothesis(supports=["hyp-01"])]))

    assert Issue("hypotheses[0].supports[0]", "no observation has this ID") in issues


# Outcome rules (E4)


def test_a_diagnosis_needs_a_root_cause() -> None:
    assert issues_in(candidate(root_causes=[])) == [
        Issue("root_causes", "a diagnosis needs a root cause")
    ]


def test_an_abstention_with_a_root_cause_is_rejected() -> None:
    fields = abstention(hypotheses=[hypothesis()], root_causes=["hyp-01"])

    assert issues_in(fields) == [Issue("root_causes", "an abstention accepts no root cause")]


def test_an_abstention_may_list_what_it_tested() -> None:
    assert issues_in(abstention(hypotheses=[hypothesis(confidence="low")])) == []


@pytest.mark.parametrize("outcome", ["insufficient_evidence", "partially_diagnosed"])
def test_partial_diagnoses_and_abstentions_state_their_limitations(outcome: str) -> None:
    fields = candidate(diagnostic_outcome=outcome, root_causes=[], limitations=[])

    assert issues_in(fields) == [
        Issue("limitations", "a partial diagnosis or abstention states its limitations")
    ]


def test_a_diagnosis_may_omit_limitations() -> None:
    assert issues_in(candidate(limitations=[])) == []


def test_a_partial_diagnosis_needs_a_supported_hypothesis() -> None:
    unsupported = hypothesis(supports=[])
    fields = candidate(
        diagnostic_outcome="partially_diagnosed",
        hypotheses=[unsupported, hypothesis("hyp-02", supports=[])],
        root_causes=[],
        limitations=["Span timings are missing"],
    )

    assert issues_in(fields) == [
        Issue("hypotheses", "a partial diagnosis needs a supported hypothesis")
    ]


def test_one_supported_hypothesis_is_enough_for_a_partial_diagnosis() -> None:
    fields = candidate(
        diagnostic_outcome="partially_diagnosed",
        hypotheses=[hypothesis(supports=[]), hypothesis("hyp-02", confidence="low")],
        root_causes=[],
        limitations=["Span timings are missing"],
    )

    assert issues_in(fields) == []


@pytest.mark.parametrize("outcome", ["diagnosed", "partially_diagnosed"])
def test_an_accepted_cause_needs_support(outcome: str) -> None:
    fields = candidate(
        diagnostic_outcome=outcome,
        hypotheses=[hypothesis("hyp-02"), hypothesis(supports=[])],
        root_causes=["hyp-02", "hyp-01"],
        limitations=["Span timings are missing"],
    )

    assert issues_in(fields) == [
        Issue("root_causes[1]", "an accepted cause needs a supporting observation")
    ]


def test_a_diagnosed_cause_needs_medium_or_high_confidence() -> None:
    fields = candidate(hypotheses=[hypothesis(confidence="low")])

    assert issues_in(fields) == [
        Issue("root_causes[0]", "a diagnosed cause needs medium or high confidence")
    ]


def test_a_partial_diagnosis_may_accept_a_low_confidence_cause() -> None:
    fields = candidate(
        diagnostic_outcome="partially_diagnosed",
        hypotheses=[hypothesis(confidence="low")],
        limitations=["Span timings are missing"],
    )

    assert issues_in(fields) == []


def test_unaccepted_hypotheses_may_have_low_confidence_in_a_diagnosis() -> None:
    fields = candidate(hypotheses=[hypothesis(), hypothesis("hyp-02", confidence="low")])

    assert issues_in(fields) == []


def test_high_confidence_with_corroboration_and_an_alternative_is_accepted() -> None:
    assert issues_in(candidate(hypotheses=[hypothesis(confidence="high")])) == []


@pytest.mark.parametrize(
    "supports",
    [
        ["obs-latency"],
        ["obs-latency", "obs-latency-again"],
        ["obs-latency", "obs-unknown"],
    ],
    ids=["one-support", "same-evidence", "unknown-second-support"],
)
def test_high_confidence_needs_two_supports_citing_different_evidence(
    supports: list[str],
) -> None:
    observations = [*candidate()["observations"], computed("obs-latency-again")]
    fields = candidate(
        observations=observations, hypotheses=[hypothesis(confidence="high", supports=supports)]
    )
    expected = Issue(
        "hypotheses[0].confidence", "high confidence needs two supports citing different evidence"
    )

    assert expected in issues_in(fields)


def test_high_confidence_needs_an_alternative() -> None:
    fields = candidate(hypotheses=[hypothesis(confidence="high")], alternatives=[])

    assert issues_in(fields) == [
        Issue("hypotheses[0].confidence", "high confidence needs an alternative weighed")
    ]


def test_high_confidence_rules_apply_to_unaccepted_hypotheses() -> None:
    fields = candidate(
        hypotheses=[hypothesis(), hypothesis("hyp-02", confidence="high", supports=["obs-log"])]
    )

    assert issues_in(fields) == [
        Issue(
            "hypotheses[1].confidence",
            "high confidence needs two supports citing different evidence",
        )
    ]


def test_every_issue_is_reported_at_once() -> None:
    fields = abstention(
        hypotheses=[hypothesis(supports=["obs-x"])], root_causes=["hyp-01"], limitations=[]
    )

    assert len(issues_in(fields)) == 3


# Citations against visible evidence (invariant 4)


def test_evidence_the_investigation_cannot_see_is_reported() -> None:
    index = {key: value for key, value in EVIDENCE.items() if key != evidence_id(2)}
    issues = citation_issues(
        parse(ReportCandidate, candidate()), index, owner_id=OWNER, snapshot_id=SNAPSHOT
    )

    assert issues == [
        Issue("observations[1].references[0]", "cites evidence this investigation can't see")
    ]


@pytest.mark.parametrize(
    ("row", "message"),
    [
        (
            record(2, EvidenceKind.DEPLOYMENT_RECORD, owner_id=OTHER_OWNER),
            "evidence belongs to another owner",
        ),
        (
            record(2, EvidenceKind.DEPLOYMENT_RECORD, snapshot_id="snap-case-b"),
            "evidence is from another snapshot",
        ),
        (
            record(2, EvidenceKind.LOG_RECORD),
            "selector kind deployment_record doesn't match evidence kind log_record",
        ),
        (record(5, EvidenceKind.DEPLOYMENT_RECORD), "selector names a different evidence row"),
    ],
    ids=["other-owner", "other-snapshot", "wrong-kind", "wrong-row"],
)
def test_selectors_are_checked_against_their_rows(row: EvidenceRecord, message: str) -> None:
    assert citations(candidate(), e2=row) == [Issue("observations[1].references[0]", message)]


def test_a_selected_window_outside_the_series_is_reported() -> None:
    fields = candidate(
        observations=[*candidate()["observations"], observed("obs-wide", series(4, OUTSIDE))]
    )

    assert citations(fields) == [
        Issue("observations[3].references[0]", "selected window is outside the series")
    ]


def test_measurement_inputs_are_cited_evidence() -> None:
    observation = computed() | {"references": [series(4)], "measurement": measurement(5)}
    fields = candidate(observations=[observation, *candidate()["observations"][1:]])

    assert citations(fields) == [
        Issue(
            "observations[0].measurement.inputs[0]", "cites evidence this investigation can't see"
        )
    ]


def test_both_terms_of_a_ratio_are_cited_evidence() -> None:
    fields = candidate(impact={"affected_services": [], "measurements": [ratio(5, 6)]})
    unseen = "cites evidence this investigation can't see"

    assert citations(fields) == [
        Issue("impact.measurements[0].inputs[0]", unseen),
        Issue("impact.measurements[0].inputs[1]", unseen),
        Issue("impact.measurements[0].numerator.inputs[0]", unseen),
        Issue("impact.measurements[0].denominator.inputs[0]", unseen),
    ]


def test_every_measurement_in_impact_is_checked() -> None:
    fields = candidate(impact={"affected_services": [], "measurements": [ratio(), measurement(5)]})

    assert citations(fields) == [
        Issue("impact.measurements[1].inputs[0]", "cites evidence this investigation can't see")
    ]


# Whole reports


def test_a_consistent_report_has_no_issues() -> None:
    assert report_issues(parse(DiagnosisReport, report()), EVIDENCE, owner_id=OWNER) == []


def test_report_issues_name_their_place_in_the_report() -> None:
    fields = report(candidate=candidate(root_causes=[]))
    issues = report_issues(parse(DiagnosisReport, fields), EVIDENCE, owner_id=OWNER)

    assert issues == [Issue("candidate.root_causes", "a diagnosis needs a root cause")]


def test_citations_use_the_reports_snapshot_and_the_callers_owner() -> None:
    parsed = parse(DiagnosisReport, report())
    elsewhere = parse(
        DiagnosisReport, report(identity=report()["identity"] | {"snapshot_id": "snap-case-b"})
    )

    assert report_issues(parsed, EVIDENCE, owner_id=OTHER_OWNER)[0].message == (
        "evidence belongs to another owner"
    )
    assert report_issues(elsewhere, EVIDENCE, owner_id=OWNER)[0].message == (
        "evidence is from another snapshot"
    )


def test_an_application_abstention_has_no_issues() -> None:
    parsed = parse(DiagnosisReport, report("application"))

    assert report_issues(parsed, EVIDENCE, owner_id=OWNER) == []


@pytest.mark.parametrize(
    ("changes", "issue"),
    [
        (
            {"diagnostic_outcome": "partially_diagnosed", "hypotheses": [hypothesis()]},
            Issue("candidate.diagnostic_outcome", "an application report can only abstain"),
        ),
        (
            {"hypotheses": [hypothesis(confidence="low")]},
            Issue("candidate.hypotheses", "an application report proposes no causes"),
        ),
        (
            {"alternatives": [alternative()]},
            Issue("candidate.alternatives", "an application report weighs no alternatives"),
        ),
    ],
)
def test_an_application_report_invents_no_causes(changes: dict[str, Any], issue: Issue) -> None:
    parsed = parse(DiagnosisReport, report("application", candidate=abstention(**changes)))

    assert issue in report_issues(parsed, EVIDENCE, owner_id=OWNER)


def test_a_model_abstention_may_list_hypotheses_and_alternatives() -> None:
    fields = report(
        candidate=abstention(
            hypotheses=[hypothesis(confidence="low")], alternatives=[alternative()]
        )
    )

    assert report_issues(parse(DiagnosisReport, fields), EVIDENCE, owner_id=OWNER) == []


def test_an_unknown_root_cause_does_not_hide_later_ones() -> None:
    fields = candidate(hypotheses=[hypothesis(supports=[])], root_causes=["hyp-x", "hyp-01"])

    assert issues_in(fields) == [
        Issue("root_causes[0]", "no hypothesis has this ID"),
        Issue("root_causes[1]", "an accepted cause needs a supporting observation"),
    ]
