"""Validation and atomic terminal publication (spec §6.4, §6.5, D06; errata E1, E5, E6, E9).

A worker hands in its candidate with `submit_candidate`, which moves the investigation
from RUNNING to VALIDATING in the same fenced transaction (E1). A recovery attempt
finds that candidate with `resume_validation` and validates it again, with no new model
or tool calls.

`publish_report` is the terminal transaction. Under the lease fence it checks the
report, inserts the investigation's one report, closes the attempt, frees its slot, sets
COMPLETED and appends REPORT_PUBLISHED, all at once. Cancellation takes the same row
lock, so whichever commits first wins and the other finds the investigation ended.

Run each function inside `fenced_transaction`, alone: publication must commit by itself.
"""

from dataclasses import dataclass
from typing import Literal
from uuid import uuid4

from psycopg.types.json import Jsonb

from incidentpilot.domain.canonical import sha256_digest
from incidentpilot.domain.enums import (
    AttemptStatus,
    EventType,
    InvestigationStatus,
    ReportAuthor,
    RunMode,
    TerminationReason,
    ValidationStageStatus,
)
from incidentpilot.domain.ids import CandidateId, InvestigationId, ReportId
from incidentpilot.domain.report import (
    DiagnosisReport,
    ReportCandidate,
    ReportExecution,
    ReportIdentity,
    ReportValidation,
    UsageSummary,
    check_authorship,
)
from incidentpilot.domain.report_checks import Issue, report_issues
from incidentpilot.domain.transitions import require_investigation_transition
from incidentpilot.persistence.database import Connection
from incidentpilot.persistence.events import append_event
from incidentpilot.persistence.evidence import visible_evidence
from incidentpilot.persistence.fence import Lease
from incidentpilot.workflows.recovery import close_attempt, end_investigation

# The checks a published report passed. Numeric checks arrive with M5 (E6: `not_run`).
VALIDATOR_VERSION = "structural-1"
SCHEMA_VERSION: Literal["3.0"] = "3.0"

_STATUS = "SELECT status FROM investigations WHERE id = %s"
_VALIDATING = (
    "UPDATE investigations SET status = 'VALIDATING', row_version = row_version + 1 WHERE id = %s"
)
_INSERT_CANDIDATE = """
INSERT INTO report_candidates (
    owner_id, id, investigation_id, attempt_id, author, termination_reason, prompt_sha256,
    content, content_sha256
) VALUES (current_owner_id(), %s, %s, %s, %s, %s, %s, %s, %s)
"""
_LATEST_CANDIDATE = """
SELECT id FROM report_candidates WHERE investigation_id = %s
ORDER BY created_at DESC, id DESC LIMIT 1
"""
_CANDIDATE = """
SELECT author, termination_reason, prompt_sha256, content::text
FROM report_candidates WHERE id = %s AND investigation_id = %s
"""
_INVESTIGATION = """
SELECT status, snapshot_id, config_sha256, mode, attempts_started, repair_requests_used,
       model_requests_settled, model_requests_unknown, input_tokens_actual,
       output_tokens_actual
FROM investigations WHERE id = %s
"""
_INSERT_REPORT = """
INSERT INTO diagnosis_reports (
    owner_id, id, investigation_id, attempt_id, candidate_id, diagnostic_outcome, report,
    report_sha256, validator_version
) VALUES (current_owner_id(), %s, %s, %s, %s, %s, %s, %s, %s)
"""


class ReportRejected(Exception):
    """The report failed its checks, so nothing was published."""

    def __init__(self, issues: list[Issue]) -> None:
        self.issues = issues
        super().__init__(f"the report has {len(issues)} structural issues")


@dataclass(frozen=True)
class Published:
    report_id: ReportId
    report_sha256: str
    report: DiagnosisReport


async def submit_candidate(
    connection: Connection,
    lease: Lease,
    candidate: ReportCandidate,
    *,
    author: ReportAuthor,
    reason: TerminationReason,
    prompt_sha256: str,
) -> CandidateId:
    """Store the candidate and move RUNNING -> VALIDATING with it (E1)."""
    check_authorship(author, reason)
    await _start_validation(connection, lease.investigation_id)
    content = candidate.model_dump(mode="json")
    candidate_id = CandidateId(uuid4())
    await connection.execute(
        _INSERT_CANDIDATE,
        (
            candidate_id,
            lease.investigation_id,
            lease.attempt_id,
            author,
            reason,
            prompt_sha256,
            Jsonb(content),
            sha256_digest(content),
        ),
    )
    await _validation_started(connection, lease, candidate_id, resumed=False)
    return candidate_id


async def resume_validation(connection: Connection, lease: Lease) -> CandidateId | None:
    """E1: if an earlier attempt stored a candidate, validate it again. None if not."""
    row = await (await connection.execute(_LATEST_CANDIDATE, (lease.investigation_id,))).fetchone()
    if row is None:
        return None
    candidate_id = CandidateId(row[0])
    await _start_validation(connection, lease.investigation_id)
    await _validation_started(connection, lease, candidate_id, resumed=True)
    return candidate_id


async def publish_report(
    connection: Connection, lease: Lease, candidate_id: CandidateId
) -> Published:
    """VALIDATING -> COMPLETED with exactly one report, or ReportRejected and no change."""
    investigation = await (
        await connection.execute(_INVESTIGATION, (lease.investigation_id,))
    ).fetchone()
    assert investigation is not None  # noqa: S101 (the fence holds this row locked)
    status = InvestigationStatus(investigation[0])
    require_investigation_transition(status, InvestigationStatus.COMPLETED)
    stored = await (
        await connection.execute(_CANDIDATE, (candidate_id, lease.investigation_id))
    ).fetchone()
    if stored is None:
        raise LookupError(
            f"candidate {candidate_id} doesn't belong to investigation {lease.investigation_id}"
        )
    author, reason = ReportAuthor(stored[0]), TerminationReason(stored[1])
    mode = RunMode(investigation[3])
    assert mode is not RunMode.REPLAY  # noqa: S101 (the schema stores no replay runs, E8)
    report = DiagnosisReport(
        identity=ReportIdentity(
            schema_version=SCHEMA_VERSION,
            investigation_id=lease.investigation_id,
            snapshot_id=investigation[1],
            config_sha256=investigation[2],
            prompt_sha256=stored[2],
        ),
        execution=ReportExecution(
            mode=mode,
            author=author,
            termination_reason=reason,
            attempt_count=investigation[4],
            usage=UsageSummary(
                model_requests_settled=investigation[6],
                model_requests_unknown=investigation[7],
                # Tool calls arrive with the gateway (T4.3).
                tool_calls_completed=0,
                input_tokens=investigation[8],
                output_tokens=investigation[9],
            ),
        ),
        validation=ReportValidation(
            structural=ValidationStageStatus.PASSED,
            numeric=ValidationStageStatus.NOT_RUN,
            semantic=ValidationStageStatus.NOT_ASSESSED,
            repair_used=investigation[5] > 0,
        ),
        candidate=ReportCandidate.model_validate_json(stored[3]),
    )
    evidence = await visible_evidence(connection, lease.investigation_id)
    issues = report_issues(
        report, {record.evidence_id: record for record in evidence}, owner_id=lease.owner_id
    )
    if issues:
        raise ReportRejected(issues)
    content = report.model_dump(mode="json")
    report_id, digest = ReportId(uuid4()), sha256_digest(content)
    await connection.execute(
        _INSERT_REPORT,
        (
            report_id,
            lease.investigation_id,
            lease.attempt_id,
            candidate_id,
            report.candidate.diagnostic_outcome,
            Jsonb(content),
            digest,
            VALIDATOR_VERSION,
        ),
    )
    await close_attempt(connection, lease.attempt_id, AttemptStatus.SUCCEEDED)
    await end_investigation(
        connection,
        lease.investigation_id,
        status,
        InvestigationStatus.COMPLETED,
        reason,
        EventType.REPORT_PUBLISHED,
        {"report_id": str(report_id), "report_sha256": digest},
    )
    return Published(report_id, digest, report)


async def _start_validation(connection: Connection, investigation_id: InvestigationId) -> None:
    row = await (await connection.execute(_STATUS, (investigation_id,))).fetchone()
    assert row is not None  # noqa: S101 (the fence holds this row locked)
    require_investigation_transition(InvestigationStatus(row[0]), InvestigationStatus.VALIDATING)
    await connection.execute(_VALIDATING, (investigation_id,))


async def _validation_started(
    connection: Connection, lease: Lease, candidate_id: CandidateId, *, resumed: bool
) -> None:
    await append_event(
        connection,
        lease.investigation_id,
        EventType.VALIDATION_STARTED,
        {
            "attempt_id": str(lease.attempt_id),
            "candidate_id": str(candidate_id),
            "resumed": resumed,
        },
    )
