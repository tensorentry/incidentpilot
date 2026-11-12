"""Report contracts: errata E2 to E6.

`ReportCandidate` is the only schema the model sees. `DiagnosisReport` wraps a
validated candidate with sections only the application writes. Each record here
checks itself; checks across records, such as whether a cited observation exists
or what an outcome requires, belong to the cross-record validator (T1.7).
"""

import re
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, Field, model_validator

from incidentpilot.domain.contract import Contract
from incidentpilot.domain.enums import (
    AlternativeStatus,
    CausalRole,
    ClaimKind,
    Confidence,
    DiagnosticOutcome,
    RecommendationKind,
    ReportAuthor,
    RunMode,
    StopReason,
    TerminationReason,
    ValidationStageStatus,
)
from incidentpilot.domain.ids import InvestigationId, SnapshotId
from incidentpilot.domain.measurement import MAX_ITEMS, Measurement, Selectors, distinct

MAX_TEXT = 500
MAX_OBSERVATIONS = 50

# A number followed by a unit or percent sign (E2). A number inside a word, such as
# v2 or p95, is part of a name; a number with no unit, such as HTTP 503, is an identifier.
_QUANTITY = re.compile(
    r"(?<![\w.])\d+(?:[.,]\d+)*\s*(?:[%\u00d7]|(?:percent|ms|milliseconds?|s|secs?|seconds?"
    r"|mins?|minutes?|h|hrs?|hours?|days?|x|rps|qps|rpm|req/s|b|bytes?|kb|mb|gb|kib|mib|gib)\b)",
    re.IGNORECASE,
)


def _no_quantities(text: str) -> str:
    if not text.strip():
        raise ValueError("text must not be blank")
    if _QUANTITY.search(text):
        raise ValueError("quantities belong in a measurement, not in text")
    return text


Prose = Annotated[str, Field(min_length=1, max_length=MAX_TEXT), AfterValidator(_no_quantities)]
LocalId = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")]
ServiceName = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")]
Category = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
LocalIds = Annotated[list[LocalId], Field(max_length=MAX_ITEMS), AfterValidator(distinct)]
ProseList = Annotated[list[Prose], Field(max_length=MAX_ITEMS)]


def _require_disjoint(supports: list[str], contradicts: list[str]) -> None:
    if set(supports) & set(contradicts):
        raise ValueError("an observation can't both support and contradict")


class Observation(Contract):
    observation_id: LocalId
    claim: Prose
    claim_kind: ClaimKind
    references: Selectors
    measurement: Measurement | None

    @model_validator(mode="after")
    def _measured_when_computed(self) -> Self:
        computed = self.claim_kind is ClaimKind.COMPUTED
        if computed and self.measurement is None:
            raise ValueError("a computed claim needs a measurement")
        if not computed and self.measurement is not None:
            raise ValueError("an observed claim has no measurement")
        return self


class CausalHypothesis(Contract):
    cause_id: LocalId
    component: ServiceName
    category: Category
    mechanism: Prose
    role: CausalRole
    confidence: Confidence
    supports: LocalIds
    contradicts: LocalIds
    unresolved_tests: ProseList

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        _require_disjoint(self.supports, self.contradicts)
        return self


class Alternative(Contract):
    description: Prose
    status: AlternativeStatus
    supports: LocalIds
    contradicts: LocalIds

    @model_validator(mode="after")
    def _status_is_backed(self) -> Self:
        _require_disjoint(self.supports, self.contradicts)
        # Missing evidence is not contradiction (spec §10.1).
        if self.status is AlternativeStatus.CONTRADICTED and not self.contradicts:
            raise ValueError("a contradicted alternative needs a contradicting observation")
        if self.status is AlternativeStatus.SUPPORTED and not self.supports:
            raise ValueError("a supported alternative needs a supporting observation")
        return self


class Recommendation(Contract):
    kind: RecommendationKind
    description: Prose
    references: Annotated[
        list[LocalId], Field(min_length=1, max_length=MAX_ITEMS), AfterValidator(distinct)
    ]
    preconditions: ProseList
    risk: Prose
    verification: Prose


class Impact(Contract):
    affected_services: Annotated[
        list[ServiceName], Field(max_length=MAX_ITEMS), AfterValidator(distinct)
    ]
    measurements: Annotated[list[Measurement], Field(max_length=MAX_ITEMS)]


class ReportCandidate(Contract):
    """What the model writes. Identity, execution and validation are never in here."""

    diagnostic_outcome: DiagnosticOutcome
    observations: Annotated[list[Observation], Field(max_length=MAX_OBSERVATIONS)]
    hypotheses: Annotated[list[CausalHypothesis], Field(max_length=MAX_ITEMS)]
    root_causes: LocalIds
    impact: Impact
    alternatives: Annotated[list[Alternative], Field(max_length=MAX_ITEMS)]
    recommendations: Annotated[list[Recommendation], Field(max_length=MAX_ITEMS)]
    limitations: ProseList


class Stop(Contract):
    """The runner finished without a candidate. The application writes `detail`."""

    reason: StopReason
    detail: Annotated[str, Field(min_length=1, max_length=MAX_TEXT)]


CandidateOrStop = ReportCandidate | Stop


class ReportIdentity(Contract):
    schema_version: Literal["3.0"]
    investigation_id: InvestigationId
    snapshot_id: Annotated[SnapshotId, Field(min_length=1, max_length=128)]
    config_sha256: Sha256
    prompt_sha256: Sha256


class UsageSummary(Contract):
    model_requests_settled: Annotated[int, Field(ge=0)]
    model_requests_unknown: Annotated[int, Field(ge=0)]
    tool_calls_completed: Annotated[int, Field(ge=0)]
    input_tokens: Annotated[int, Field(ge=0)]
    output_tokens: Annotated[int, Field(ge=0)]


class ReportExecution(Contract):
    # Replay is rejected when an investigation starts (E8), so it never publishes.
    mode: Literal[RunMode.OFFLINE_DEMO, RunMode.LIVE]
    author: ReportAuthor
    termination_reason: TerminationReason
    attempt_count: Annotated[int, Field(ge=1)]
    usage: UsageSummary

    @model_validator(mode="after")
    def _author_matches_reason(self) -> Self:
        if self.termination_reason is TerminationReason.CANCELLED:
            raise ValueError("a cancelled investigation publishes no report")
        finished = self.termination_reason is TerminationReason.INVESTIGATOR_FINISHED
        if finished != (self.author is ReportAuthor.MODEL):
            raise ValueError("only a finished investigator's candidate is published as written")
        return self


class ReportValidation(Contract):
    """What was checked before publication (E6). Nothing here claims semantic truth."""

    structural: Literal[ValidationStageStatus.PASSED]
    numeric: Literal[ValidationStageStatus.PASSED, ValidationStageStatus.NOT_RUN]
    semantic: Literal[ValidationStageStatus.NOT_ASSESSED]
    repair_used: bool


class DiagnosisReport(Contract):
    identity: ReportIdentity
    execution: ReportExecution
    validation: ReportValidation
    candidate: ReportCandidate
