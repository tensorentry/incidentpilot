"""Validation and atomic terminal publication (T3.12)."""

import asyncio
import re
from typing import Any
from uuid import uuid4

import pytest
from psycopg import errors

from incidentpilot.application.errors import Conflict
from incidentpilot.application.investigations import cancel_investigation
from incidentpilot.domain.canonical import sha256_digest
from incidentpilot.domain.enums import (
    EvidenceKind,
    ReportAuthor,
    TerminationReason,
)
from incidentpilot.domain.evidence import EvidenceDraft, LogRecordSelector
from incidentpilot.domain.ids import CandidateId, EvidenceId
from incidentpilot.domain.report import ReportCandidate
from incidentpilot.domain.transitions import IllegalTransitionError
from incidentpilot.persistence.database import Connection, owner_transaction
from incidentpilot.persistence.events import list_events
from incidentpilot.persistence.evidence import commit_evidence, visible_evidence
from incidentpilot.persistence.fence import Lease, StaleLease, fenced_transaction
from incidentpilot.persistence.ledger import Usage, dispatch_call, reserve_call, settle_call
from incidentpilot.workflows.claim import claim_next
from incidentpilot.workflows.publication import (
    VALIDATOR_VERSION,
    Published,
    ReportRejected,
    publish_report,
    resume_validation,
    submit_candidate,
)
from incidentpilot.workflows.recovery import sweep_once
from incidentpilot.workflows.scripted import SCRIPT_PROMPT_SHA256, abstain
from tests.integration.harness import OWNER_A, OWNER_B, ScratchDatabase, still_blocked
from tests.integration.seeding import (
    MANIFEST,
    OFFLINE,
    bound_slots,
    claimed,
    expire,
    update,
)

DRAFT = EvidenceDraft(
    kind=EvidenceKind.LOG_RECORD,
    source_ref="log/checkout-api/1",
    adapter_version="fixture-1",
    window=None,
    content={"message": "upstream timeout"},
)


async def investigated(connection: Connection, lease: Lease) -> ReportCandidate:
    """Spend one model call and commit one record, as an investigator would."""
    async with fenced_transaction(connection, lease):
        call_id = await reserve_call(connection, lease, 30_000)
    async with fenced_transaction(connection, lease):
        await dispatch_call(connection, lease, call_id)
    async with fenced_transaction(connection, lease):
        await settle_call(connection, lease, call_id, Usage(120, 30, 4_000))
        await commit_evidence(connection, lease, DRAFT)
        return abstain(await visible_evidence(connection, lease.investigation_id))


async def submitted(connection: Connection, lease: Lease) -> CandidateId:
    candidate = await investigated(connection, lease)
    async with fenced_transaction(connection, lease):
        return await submit(connection, lease, candidate)


async def submit(
    connection: Connection,
    lease: Lease,
    candidate: ReportCandidate,
    author: ReportAuthor = ReportAuthor.MODEL,
    reason: TerminationReason = TerminationReason.INVESTIGATOR_FINISHED,
) -> CandidateId:
    return await submit_candidate(
        connection,
        lease,
        candidate,
        author=author,
        reason=reason,
        prompt_sha256=SCRIPT_PROMPT_SHA256,
    )


async def publish(connection: Connection, lease: Lease, candidate_id: CandidateId) -> Published:
    async with fenced_transaction(connection, lease):
        return await publish_report(connection, lease, candidate_id)


async def recovered(connection: Connection, lease: Lease) -> Lease:
    """Lose the lease, sweep it, and claim the recovery attempt."""
    await expire(connection, lease)
    await sweep_once(connection)
    assert (await investigation(connection, lease))["status"] == "RETRY_WAIT"
    await update(
        connection,
        lease.owner_id,
        "UPDATE investigations SET eligible_at = pg_catalog.clock_timestamp() WHERE id = %s",
        lease.investigation_id,
    )
    retry = await claim_next(connection, "rescuer", OFFLINE)
    assert retry is not None
    return retry


async def investigation(connection: Connection, lease: Lease) -> dict[str, Any]:
    async with owner_transaction(connection, lease.owner_id):
        cursor = await connection.execute(
            "SELECT status, termination_reason, current_attempt_id, lease_expires_at"
            " FROM investigations WHERE id = %s",
            (lease.investigation_id,),
        )
        row = await cursor.fetchone()
    assert row is not None
    return dict(zip(("status", "reason", "attempt", "expires"), row, strict=True))


async def row_version(connection: Connection, lease: Lease) -> int:
    async with owner_transaction(connection, lease.owner_id):
        cursor = await connection.execute(
            "SELECT row_version FROM investigations WHERE id = %s", (lease.investigation_id,)
        )
        row = await cursor.fetchone()
    assert row is not None
    return int(row[0])


async def reports(connection: Connection, lease: Lease) -> list[tuple[Any, ...]]:
    async with owner_transaction(connection, lease.owner_id):
        cursor = await connection.execute(
            "SELECT id, attempt_id, candidate_id, diagnostic_outcome, report, report_sha256,"
            " validator_version FROM diagnosis_reports WHERE investigation_id = %s",
            (lease.investigation_id,),
        )
        return list(await cursor.fetchall())


async def attempt_status(connection: Connection, lease: Lease) -> str:
    async with owner_transaction(connection, lease.owner_id):
        cursor = await connection.execute(
            "SELECT status FROM attempts WHERE id = %s", (lease.attempt_id,)
        )
        row = await cursor.fetchone()
    assert row is not None
    return str(row[0])


async def events(connection: Connection, lease: Lease) -> list[tuple[str, object]]:
    async with owner_transaction(connection, lease.owner_id):
        listed = await list_events(connection, lease.investigation_id)
    return [(kind, payload) for _, kind, payload, _ in listed]


# Handing in a candidate


async def test_a_candidate_moves_the_investigation_to_validating(
    database: ScratchDatabase,
) -> None:
    async with database.runtime() as connection:
        lease = await claimed(connection)
        candidate = await investigated(connection, lease)
        version = await row_version(connection, lease)
        async with fenced_transaction(connection, lease):
            candidate_id = await submit(connection, lease, candidate)

        assert (await investigation(connection, lease))["status"] == "VALIDATING"
        assert await row_version(connection, lease) == version + 1
        async with owner_transaction(connection, OWNER_A):
            cursor = await connection.execute(
                "SELECT attempt_id, author, termination_reason, prompt_sha256, content,"
                " content_sha256 FROM report_candidates WHERE id = %s",
                (candidate_id,),
            )
            row = await cursor.fetchone()
        content = candidate.model_dump(mode="json")
        assert row == (
            lease.attempt_id,
            "model",
            "investigator_finished",
            SCRIPT_PROMPT_SHA256,
            content,
            sha256_digest(content),
        )
        assert (await events(connection, lease))[-1] == (
            "VALIDATION_STARTED",
            {
                "attempt_id": str(lease.attempt_id),
                "candidate_id": str(candidate_id),
                "resumed": False,
            },
        )


async def test_only_running_work_takes_a_candidate(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        lease = await claimed(connection)
        candidate = await investigated(connection, lease)
        async with fenced_transaction(connection, lease):
            await submit(connection, lease, candidate)
        with pytest.raises(IllegalTransitionError):
            async with fenced_transaction(connection, lease):
                await submit(connection, lease, candidate)

        async with owner_transaction(connection, OWNER_A):
            cursor = await connection.execute("SELECT count(*) FROM report_candidates")
            assert await cursor.fetchone() == (1,)


@pytest.mark.parametrize(
    ("author", "reason", "message"),
    [
        (
            ReportAuthor.APPLICATION,
            TerminationReason.INVESTIGATOR_FINISHED,
            "only a finished investigator's candidate is published as written",
        ),
        (
            ReportAuthor.MODEL,
            TerminationReason.BUDGET_EXHAUSTED,
            "only a finished investigator's candidate is published as written",
        ),
        (
            ReportAuthor.APPLICATION,
            TerminationReason.CANCELLED,
            "a cancelled investigation publishes no report",
        ),
    ],
)
async def test_a_candidate_must_match_its_authorship(
    database: ScratchDatabase, author: ReportAuthor, reason: TerminationReason, message: str
) -> None:
    async with database.runtime() as connection:
        lease = await claimed(connection)
        candidate = await investigated(connection, lease)
        with pytest.raises(ValueError, match=rf"^{re.escape(message)}$"):
            async with fenced_transaction(connection, lease):
                await submit(connection, lease, candidate, author, reason)
        assert (await investigation(connection, lease))["status"] == "RUNNING"


# Publishing


async def test_publication_ends_everything_in_one_transaction(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        lease = await claimed(connection)
        candidate_id = await submitted(connection, lease)
        published = await publish(connection, lease, candidate_id)

        assert await investigation(connection, lease) == {
            "status": "COMPLETED",
            "reason": "investigator_finished",
            "attempt": None,
            "expires": None,
        }
        assert await attempt_status(connection, lease) == "SUCCEEDED"
        assert await bound_slots(database) == []

        content = published.report.model_dump(mode="json")
        assert await reports(connection, lease) == [
            (
                published.report_id,
                lease.attempt_id,
                candidate_id,
                "insufficient_evidence",
                content,
                sha256_digest(content),
                VALIDATOR_VERSION,
            )
        ]
        assert published.report_sha256 == sha256_digest(content)
        assert (await events(connection, lease))[-1] == (
            "REPORT_PUBLISHED",
            {
                "reason": "investigator_finished",
                "report_id": str(published.report_id),
                "report_sha256": published.report_sha256,
            },
        )


async def test_the_report_is_written_from_the_investigations_own_rows(
    database: ScratchDatabase,
) -> None:
    async with database.runtime() as connection:
        lease = await claimed(connection)
        candidate_id = await submitted(connection, lease)
        async with owner_transaction(connection, OWNER_A):
            cursor = await connection.execute(
                "SELECT config_sha256 FROM investigations WHERE id = %s",
                (lease.investigation_id,),
            )
            (config_sha256,) = await cursor.fetchone() or ("",)
        report = (await publish(connection, lease, candidate_id)).report

        assert report.identity.model_dump(mode="json") == {
            "schema_version": "3.0",
            "investigation_id": str(lease.investigation_id),
            "snapshot_id": MANIFEST.snapshot_id,
            "config_sha256": config_sha256,
            "prompt_sha256": SCRIPT_PROMPT_SHA256,
        }
        assert report.execution.model_dump(mode="json") == {
            "mode": "offline_demo",
            "author": "model",
            "termination_reason": "investigator_finished",
            "attempt_count": 1,
            "usage": {
                "model_requests_settled": 1,
                "model_requests_unknown": 0,
                "tool_calls_completed": 0,
                "input_tokens": 120,
                "output_tokens": 30,
            },
        }
        assert report.validation.model_dump(mode="json") == {
            "structural": "passed",
            "numeric": "not_run",
            "semantic": "not_assessed",
            "repair_used": False,
        }


async def test_a_used_repair_is_reported(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        lease = await claimed(connection)
        candidate_id = await submitted(connection, lease)
        await update(
            connection,
            OWNER_A,
            "UPDATE investigations SET repair_requests_used = 1 WHERE id = %s",
            lease.investigation_id,
        )
        report = (await publish(connection, lease, candidate_id)).report
        assert report.validation.repair_used is True


async def test_publication_closes_calls_still_in_flight(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        lease = await claimed(connection)
        candidate_id = await submitted(connection, lease)
        async with fenced_transaction(connection, lease):
            sent = await reserve_call(connection, lease, 1_000)
            await dispatch_call(connection, lease, sent)
            unsent = await reserve_call(connection, lease, 1_000)
        report = (await publish(connection, lease, candidate_id)).report

        async with owner_transaction(connection, OWNER_A):
            cursor = await connection.execute(
                "SELECT id, state FROM model_calls WHERE id IN (%s, %s)", (sent, unsent)
            )
            assert dict(await cursor.fetchall()) == {sent: "UNKNOWN", unsent: "FAILED"}
        # The report's usage is read before the attempt closes them.
        assert report.execution.usage.model_requests_unknown == 0


async def test_only_validating_work_publishes(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        lease = await claimed(connection)
        await investigated(connection, lease)
        with pytest.raises(IllegalTransitionError):
            await publish(connection, lease, CandidateId(uuid4()))
        assert (await investigation(connection, lease))["status"] == "RUNNING"
        assert await reports(connection, lease) == []


async def test_a_candidate_from_another_investigation_is_refused(
    database: ScratchDatabase,
) -> None:
    async with database.runtime() as connection:
        lease = await claimed(connection, tag="first")
        other = await claimed(connection, tag="second")
        foreign = await submitted(connection, other)
        await submitted(connection, lease)

        message = f"candidate {foreign} doesn't belong to investigation {lease.investigation_id}"
        with pytest.raises(LookupError, match=rf"^{re.escape(message)}$"):
            await publish(connection, lease, foreign)
        assert await reports(connection, lease) == []


async def test_a_report_that_fails_its_checks_publishes_nothing(
    database: ScratchDatabase,
) -> None:
    async with database.runtime() as connection:
        lease = await claimed(connection)
        candidate = await investigated(connection, lease)
        unseen = LogRecordSelector(kind=EvidenceKind.LOG_RECORD, evidence_id=EvidenceId(uuid4()))
        observation = candidate.observations[0].model_copy(update={"references": [unseen]})
        broken = candidate.model_copy(update={"observations": [observation]})
        async with fenced_transaction(connection, lease):
            candidate_id = await submit(connection, lease, broken)

        with pytest.raises(ReportRejected, match=r"^the report has 1 structural issues$") as raised:
            await publish(connection, lease, candidate_id)
        assert [(issue.path, issue.message) for issue in raised.value.issues] == [
            (
                "candidate.observations[0].references[0]",
                "cites evidence this investigation can't see",
            )
        ]
        assert (await investigation(connection, lease))["status"] == "VALIDATING"
        assert await attempt_status(connection, lease) == "RUNNING"
        assert await reports(connection, lease) == []


# Recovery during validation (E1)


async def test_a_recovery_attempt_publishes_the_stored_candidate(
    database: ScratchDatabase,
) -> None:
    async with database.runtime() as connection:
        lease = await claimed(connection)
        candidate_id = await submitted(connection, lease)
        retry = await recovered(connection, lease)

        # The lost worker's late publication is fenced out.
        with pytest.raises(StaleLease):
            await publish(connection, lease, candidate_id)

        async with fenced_transaction(connection, retry):
            assert await resume_validation(connection, retry) == candidate_id
        assert (await events(connection, retry))[-1] == (
            "VALIDATION_STARTED",
            {
                "attempt_id": str(retry.attempt_id),
                "candidate_id": str(candidate_id),
                "resumed": True,
            },
        )
        report = (await publish(connection, retry, candidate_id)).report
        assert report.execution.attempt_count == 2
        assert [row[1] for row in await reports(connection, retry)] == [retry.attempt_id]


async def test_the_newest_candidate_is_resumed(database: ScratchDatabase) -> None:
    # A repair (T4.9) hands in a second candidate in the same attempt.
    async with database.runtime() as connection:
        lease = await claimed(connection)
        await submitted(connection, lease)
        newest = CandidateId(uuid4())
        await update(
            connection,
            OWNER_A,
            "INSERT INTO report_candidates SELECT owner_id, %s, investigation_id, attempt_id,"
            " author, termination_reason, prompt_sha256, content, content_sha256,"
            " created_at + interval '1 second' FROM report_candidates",
            newest,
        )
        retry = await recovered(connection, lease)
        async with fenced_transaction(connection, retry):
            assert await resume_validation(connection, retry) == newest


async def test_without_a_candidate_nothing_resumes(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        # Another investigation's candidate is not this one's.
        await submitted(connection, await claimed(connection, tag="other"))
        lease = await claimed(connection)
        async with fenced_transaction(connection, lease):
            assert await resume_validation(connection, lease) is None
        assert (await investigation(connection, lease))["status"] == "RUNNING"


# Races: one terminal outcome


@pytest.mark.parametrize("first", ["cancel", "publish"])
async def test_cancel_and_publication_race_to_one_outcome(
    database: ScratchDatabase, first: str
) -> None:
    async with (
        database.runtime() as connection,
        database.runtime() as holding,
        database.runtime() as cancelling,
        database.runtime() as publishing,
    ):
        lease = await claimed(connection)
        candidate_id = await submitted(connection, lease)

        async def cancel() -> object:
            return await cancel_investigation(cancelling, OWNER_A, lease.investigation_id)

        async def publish_now() -> object:
            return await publish(publishing, lease, candidate_id)

        start = {"cancel": cancel, "publish": publish_now}
        second = "publish" if first == "cancel" else "cancel"
        # Hold the row so both wait on it. The first to queue takes it first.
        async with owner_transaction(holding, OWNER_A):
            await holding.execute(
                "SELECT 1 FROM investigations WHERE id = %s FOR UPDATE",
                (lease.investigation_id,),
            )
            leader = asyncio.create_task(start[first]())
            assert await still_blocked(leader)
            follower = asyncio.create_task(start[second]())
            assert await still_blocked(follower)
        await leader

        state = await investigation(connection, lease)
        if first == "cancel":
            with pytest.raises(StaleLease):
                await follower
            assert state["status"] == "CANCELLED"
            assert await reports(connection, lease) == []
            assert await attempt_status(connection, lease) == "CANCELLED"
        else:
            with pytest.raises(Conflict, match=r"has already ended: COMPLETED$"):
                await follower
            assert state["status"] == "COMPLETED"
            assert len(await reports(connection, lease)) == 1
            assert await attempt_status(connection, lease) == "SUCCEEDED"
        assert await bound_slots(database) == []
        terminal = [
            kind
            for kind, _ in await events(connection, lease)
            if kind in {"REPORT_PUBLISHED", "INVESTIGATION_CANCELLED"}
        ]
        winner = "INVESTIGATION_CANCELLED" if first == "cancel" else "REPORT_PUBLISHED"
        assert terminal == [winner]


async def test_two_publications_race_to_one_report(database: ScratchDatabase) -> None:
    async with (
        database.runtime() as connection,
        database.runtime() as racing,
    ):
        lease = await claimed(connection)
        candidate_id = await submitted(connection, lease)

        async with fenced_transaction(connection, lease):
            await publish_report(connection, lease, candidate_id)
            second = asyncio.create_task(publish(racing, lease, candidate_id))
            assert await still_blocked(second)
        with pytest.raises(StaleLease):
            await second
        assert len(await reports(connection, lease)) == 1


# The schema


async def test_the_schema_allows_one_report_per_investigation(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        lease = await claimed(connection)
        await publish(connection, lease, await submitted(connection, lease))
        with pytest.raises(errors.UniqueViolation):
            await update(
                connection,
                OWNER_A,
                "INSERT INTO diagnosis_reports SELECT owner_id, gen_random_uuid(),"
                " investigation_id, attempt_id, candidate_id, diagnostic_outcome, report,"
                " report_sha256, validator_version, published_at"
                " FROM diagnosis_reports WHERE investigation_id = %s",
                lease.investigation_id,
            )


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE diagnosis_reports SET validator_version = 'forged'",
        "DELETE FROM diagnosis_reports",
        "UPDATE report_candidates SET author = 'application'",
        "DELETE FROM report_candidates",
    ],
)
async def test_the_runtime_role_cannot_change_reports_or_candidates(
    database: ScratchDatabase, statement: str
) -> None:
    async with database.runtime() as connection:
        lease = await claimed(connection)
        await publish(connection, lease, await submitted(connection, lease))
        with pytest.raises(errors.InsufficientPrivilege):
            await update(connection, OWNER_A, statement)


@pytest.mark.parametrize(
    ("author", "reason", "constraint"),
    [
        ("application", "investigator_finished", "report_candidates_author_matches_reason"),
        ("model", "budget_exhausted", "report_candidates_author_matches_reason"),
        ("application", "cancelled", "report_candidates_not_cancelled"),
    ],
)
async def test_the_schema_ties_a_candidates_author_to_its_reason(
    database: ScratchDatabase, author: str, reason: str, constraint: str
) -> None:
    async with database.runtime() as connection:
        lease = await claimed(connection)
        await submitted(connection, lease)
        with pytest.raises(errors.CheckViolation, match=rf'"{constraint}"'):
            await update(
                connection,
                OWNER_A,
                "INSERT INTO report_candidates SELECT owner_id, gen_random_uuid(),"
                " investigation_id, attempt_id, %s, %s, prompt_sha256, content, content_sha256,"
                " created_at FROM report_candidates",
                author,
                reason,
            )


@pytest.mark.parametrize(
    ("status", "reason"),
    [("COMPLETED", None), ("RUNNING", "investigator_finished"), ("FAILED", "made_up")],
)
async def test_the_schema_ties_a_termination_reason_to_an_ended_status(
    database: ScratchDatabase, status: str, reason: str | None
) -> None:
    async with database.runtime() as connection:
        lease = await claimed(connection)
        with pytest.raises(errors.CheckViolation):
            await update(
                connection,
                OWNER_A,
                "UPDATE investigations SET status = %s, termination_reason = %s WHERE id = %s",
                status,
                reason,
                lease.investigation_id,
            )


async def test_another_owner_sees_no_report(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        lease = await claimed(connection)
        await publish(connection, lease, await submitted(connection, lease))
        async with owner_transaction(connection, OWNER_B):
            cursor = await connection.execute(
                "SELECT (SELECT count(*) FROM diagnosis_reports),"
                " (SELECT count(*) FROM report_candidates)"
            )
            assert await cursor.fetchone() == (0, 0)
