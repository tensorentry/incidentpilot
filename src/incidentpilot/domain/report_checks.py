"""Checks across a report's records, and its citations against visible evidence.

Each record checks its own fields when parsed (T1.5). These checks need several
records at once: E3 references, E4 outcome rules, invariant 9 for application
reports, and invariant 4 for citations. They return every issue rather than the
first, so a single repair can address them all (spec §10.2). Messages never
repeat the cited value.
"""

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass

from incidentpilot.domain.enums import Confidence, DiagnosticOutcome, ReportAuthor
from incidentpilot.domain.evidence import (
    EvidenceRecord,
    EvidenceSelector,
    SelectorError,
    check_selector,
)
from incidentpilot.domain.ids import EvidenceId, OwnerId, SnapshotId
from incidentpilot.domain.measurement import Measurement
from incidentpilot.domain.report import DiagnosisReport, ReportCandidate


@dataclass(frozen=True, slots=True)
class Issue:
    path: str
    message: str


def candidate_issues(candidate: ReportCandidate) -> list[Issue]:
    issues: list[Issue] = []
    observations = [observation.observation_id for observation in candidate.observations]
    causes = [hypothesis.cause_id for hypothesis in candidate.hypotheses]
    _require_unique(issues, "observations", "observation_id", observations)
    _require_unique(issues, "hypotheses", "cause_id", causes)
    for index, hypothesis in enumerate(candidate.hypotheses):
        path = f"hypotheses[{index}]"
        _require_known(issues, f"{path}.supports", hypothesis.supports, observations)
        _require_known(issues, f"{path}.contradicts", hypothesis.contradicts, observations)
    for index, alternative in enumerate(candidate.alternatives):
        path = f"alternatives[{index}]"
        _require_known(issues, f"{path}.supports", alternative.supports, observations)
        _require_known(issues, f"{path}.contradicts", alternative.contradicts, observations)
    for index, recommendation in enumerate(candidate.recommendations):
        path = f"recommendations[{index}].references"
        _require_known(issues, path, recommendation.references, observations)
    _require_known(issues, "root_causes", candidate.root_causes, causes, "hypothesis")
    issues.extend(_outcome_issues(candidate))
    return issues


def citation_issues(
    candidate: ReportCandidate,
    evidence: Mapping[EvidenceId, EvidenceRecord],
    *,
    owner_id: OwnerId,
    snapshot_id: SnapshotId,
) -> list[Issue]:
    """`evidence` holds only the rows this investigation may see."""
    issues = []
    for path, selector in _selectors(candidate):
        record = evidence.get(selector.evidence_id)
        if record is None:
            issues.append(Issue(path, "cites evidence this investigation can't see"))
            continue
        try:
            check_selector(selector, record, owner_id=owner_id, snapshot_id=snapshot_id)
        except SelectorError as error:
            issues.append(Issue(path, str(error)))
    return issues


def report_issues(
    report: DiagnosisReport,
    evidence: Mapping[EvidenceId, EvidenceRecord],
    *,
    owner_id: OwnerId,
) -> list[Issue]:
    """Every structural issue in a report. The owner comes from the investigation."""
    candidate = report.candidate
    issues = [
        *candidate_issues(candidate),
        *citation_issues(
            candidate, evidence, owner_id=owner_id, snapshot_id=report.identity.snapshot_id
        ),
    ]
    issues = [Issue(f"candidate.{issue.path}", issue.message) for issue in issues]
    if report.execution.author is ReportAuthor.APPLICATION:
        # Invariant 9 and spec §9.4: a fallback abstains and invents no causes.
        if candidate.diagnostic_outcome is not DiagnosticOutcome.INSUFFICIENT:
            issues.append(
                Issue("candidate.diagnostic_outcome", "an application report can only abstain")
            )
        if candidate.hypotheses:
            issues.append(Issue("candidate.hypotheses", "an application report proposes no causes"))
        if candidate.alternatives:
            issues.append(
                Issue("candidate.alternatives", "an application report weighs no alternatives")
            )
    return issues


def _require_unique(issues: list[Issue], path: str, field: str, ids: Sequence[str]) -> None:
    for index, item in enumerate(ids):
        if item in ids[:index]:
            issues.append(Issue(f"{path}[{index}].{field}", "repeats an earlier ID"))


def _require_known(
    issues: list[Issue],
    path: str,
    references: Sequence[str],
    known: Sequence[str],
    kind: str = "observation",
) -> None:
    for index, reference in enumerate(references):
        if reference not in known:
            issues.append(Issue(f"{path}[{index}]", f"no {kind} has this ID"))


def _outcome_issues(candidate: ReportCandidate) -> Iterator[Issue]:
    """Erratum E4. Whether evidence really supports a cause is semantic (E6)."""
    outcome = candidate.diagnostic_outcome
    hypotheses = {hypothesis.cause_id: hypothesis for hypothesis in candidate.hypotheses}
    if outcome is DiagnosticOutcome.DIAGNOSED and not candidate.root_causes:
        yield Issue("root_causes", "a diagnosis needs a root cause")
    if outcome is DiagnosticOutcome.INSUFFICIENT and candidate.root_causes:
        yield Issue("root_causes", "an abstention accepts no root cause")
    if outcome is DiagnosticOutcome.PARTIAL and not any(h.supports for h in candidate.hypotheses):
        yield Issue("hypotheses", "a partial diagnosis needs a supported hypothesis")
    if outcome is not DiagnosticOutcome.DIAGNOSED and not candidate.limitations:
        yield Issue("limitations", "a partial diagnosis or abstention states its limitations")
    for index, cause_id in enumerate(candidate.root_causes):
        accepted = hypotheses.get(cause_id)
        if accepted is None:
            continue
        if not accepted.supports:
            yield Issue(f"root_causes[{index}]", "an accepted cause needs a supporting observation")
        if outcome is DiagnosticOutcome.DIAGNOSED and accepted.confidence is Confidence.LOW:
            yield Issue(
                f"root_causes[{index}]", "a diagnosed cause needs medium or high confidence"
            )
    cited = {
        observation.observation_id: frozenset(s.evidence_id for s in observation.references)
        for observation in candidate.observations
    }
    for index, hypothesis in enumerate(candidate.hypotheses):
        if hypothesis.confidence is not Confidence.HIGH:
            continue
        path = f"hypotheses[{index}].confidence"
        # Two supports that cite the same evidence corroborate nothing.
        evidence_sets = {cited[support] for support in hypothesis.supports if support in cited}
        if len(evidence_sets) < 2:
            yield Issue(path, "high confidence needs two supports citing different evidence")
        if not candidate.alternatives:
            yield Issue(path, "high confidence needs an alternative weighed")


def _selectors(candidate: ReportCandidate) -> Iterator[tuple[str, EvidenceSelector]]:
    for index, observation in enumerate(candidate.observations):
        path = f"observations[{index}]"
        for position, selector in enumerate(observation.references):
            yield f"{path}.references[{position}]", selector
        if observation.measurement is not None:
            yield from _measurement_selectors(f"{path}.measurement", observation.measurement)
    for index, measurement in enumerate(candidate.impact.measurements):
        yield from _measurement_selectors(f"impact.measurements[{index}]", measurement)


def _measurement_selectors(
    path: str, measurement: Measurement
) -> Iterator[tuple[str, EvidenceSelector]]:
    for position, selector in enumerate(measurement.inputs):
        yield f"{path}.inputs[{position}]", selector
    for name, term in (
        ("numerator", measurement.numerator),
        ("denominator", measurement.denominator),
    ):
        if term is not None:
            for position, selector in enumerate(term.inputs):
                yield f"{path}.{name}.inputs[{position}]", selector
