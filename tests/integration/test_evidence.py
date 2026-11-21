"""Committed evidence and its visibility (spec §7.3; errata E14, E15 and E19)."""

import asyncio
from contextlib import suppress
from datetime import UTC, datetime, timedelta

import pytest
from psycopg import errors
from pydantic import JsonValue, ValidationError

from incidentpilot.application.investigations import start_investigation
from incidentpilot.application.snapshots import import_file, read_bundle
from incidentpilot.domain.canonical import sha256_digest
from incidentpilot.domain.enums import EvidenceKind
from incidentpilot.domain.evidence import EvidenceDraft
from incidentpilot.domain.ids import OwnerId
from incidentpilot.domain.window import TimeWindow
from incidentpilot.persistence.database import Connection, owner_transaction
from incidentpilot.persistence.events import list_events
from incidentpilot.persistence.evidence import Committed, commit_evidence, visible_evidence
from incidentpilot.persistence.fence import Lease, StaleLease, fenced_transaction
from incidentpilot.policies.redaction import SANITIZER_VERSION
from incidentpilot.workflows.claim import claim_next
from incidentpilot.workflows.recovery import sweep_once
from tests.integration.harness import OWNER_A, OWNER_B, ScratchDatabase, still_blocked
from tests.integration.seeding import (
    MANIFEST,
    OBSERVATIONS,
    OFFLINE,
    claimed,
    expire,
    seed,
    start_request,
    update,
)

START = datetime(2026, 11, 4, 18, 15, tzinfo=UTC)
WINDOW = TimeWindow(start_at=START, end_at=START + timedelta(minutes=30))
CANARY = "ip-canary-0123456789abcdef"
REDACTED = "[REDACTED-canary]"


def series(value: float = 412.5, adapter: str = "fixture-1") -> EvidenceDraft:
    points: list[JsonValue] = [{"t": START.isoformat(), "v": value}]
    return EvidenceDraft(
        kind=EvidenceKind.METRIC_SERIES_WINDOW,
        source_ref="metric/checkout-api/latency_p95_ms/60s",
        adapter_version=adapter,
        window=WINDOW,
        content={"unit": "ms", "points": points},
    )


def log(message: str = "upstream timeout", ref: str = "log/checkout-api/7") -> EvidenceDraft:
    return EvidenceDraft(
        kind=EvidenceKind.LOG_RECORD,
        source_ref=ref,
        adapter_version="fixture-1",
        window=None,
        content={"message": message, "severity": "error"},
    )


async def commit(connection: Connection, lease: Lease, draft: EvidenceDraft) -> Committed:
    async with fenced_transaction(connection, lease):
        return await commit_evidence(connection, lease, draft)


async def all_evidence(connection: Connection, owner: OwnerId) -> list[tuple[object, ...]]:
    async with owner_transaction(connection, owner):
        cursor = await connection.execute(
            "SELECT id, owner_id, snapshot_id, source_ref FROM evidence ORDER BY created_at"
        )
        return list(await cursor.fetchall())


async def grants(connection: Connection, lease: Lease) -> list[tuple[object, ...]]:
    async with owner_transaction(connection, lease.owner_id):
        cursor = await connection.execute(
            "SELECT evidence_id, first_attempt_id, reason FROM investigation_evidence"
            " WHERE investigation_id = %s ORDER BY created_at",
            (lease.investigation_id,),
        )
        return list(await cursor.fetchall())


async def committed_events(connection: Connection, lease: Lease) -> list[object]:
    async with owner_transaction(connection, lease.owner_id):
        listed = await list_events(connection, lease.investigation_id)
    return [payload for _, kind, payload, _ in listed if kind == "EVIDENCE_COMMITTED"]


async def test_committed_evidence_is_stored_and_visible(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        lease = await claimed(connection)
        committed = await commit(connection, lease, series())

        assert (committed.stored, committed.granted) == (True, True)
        async with owner_transaction(connection, OWNER_A):
            (record,) = await visible_evidence(connection, lease.investigation_id)
        assert record.model_dump(exclude={"content_sha256"}) == {
            "evidence_id": committed.evidence_id,
            "owner_id": OWNER_A,
            "snapshot_id": MANIFEST.snapshot_id,
            "kind": EvidenceKind.METRIC_SERIES_WINDOW,
            "source_ref": "metric/checkout-api/latency_p95_ms/60s",
            "adapter_version": "fixture-1",
            "redaction_version": SANITIZER_VERSION,
            "window": WINDOW.model_dump(),
            "content": series().content,
        }
        assert record.content_sha256 == sha256_digest(series().content)
        assert await grants(connection, lease) == [
            (committed.evidence_id, lease.attempt_id, "tool_result")
        ]
        assert await committed_events(connection, lease) == [
            {
                "evidence_id": str(committed.evidence_id),
                "kind": "metric_series_window",
                "reused": False,
            }
        ]


async def test_secrets_are_redacted_before_hashing(database: ScratchDatabase) -> None:
    draft = EvidenceDraft(
        kind=EvidenceKind.LOG_RECORD,
        source_ref=f"log/{CANARY}",
        adapter_version="fixture-1",
        window=None,
        content={"message": f"key {CANARY}", CANARY: [CANARY, 1]},
    )
    async with database.runtime() as connection:
        lease = await claimed(connection)
        committed = await commit(connection, lease, draft)
        async with owner_transaction(connection, OWNER_A):
            (record,) = await visible_evidence(connection, lease.investigation_id)

    clean: dict[str, JsonValue] = {"message": f"key {REDACTED}", REDACTED: [REDACTED, 1]}
    assert record.content == committed.content == clean
    assert record.source_ref == f"log/{REDACTED}"
    assert record.content_sha256 == sha256_digest(clean)


async def test_committing_the_same_record_again_changes_nothing(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        lease = await claimed(connection)
        first = await commit(connection, lease, log())
        again = await commit(connection, lease, log())

        assert again == Committed(
            first.evidence_id, stored=False, granted=False, content=first.content
        )
        assert len(await grants(connection, lease)) == 1
        assert len(await committed_events(connection, lease)) == 1


async def test_a_recovery_attempt_reuses_what_the_lost_one_committed(
    database: ScratchDatabase,
) -> None:
    async with database.runtime() as connection:
        lease = await claimed(connection)
        first = await commit(connection, lease, log())
        await expire(connection, lease)
        await sweep_once(connection)
        await update(
            connection,
            OWNER_A,
            "UPDATE investigations SET eligible_at = pg_catalog.clock_timestamp() WHERE id = %s",
            lease.investigation_id,
        )
        retry = await claim_next(connection, "rescuer", OFFLINE)
        assert retry is not None

        again = await commit(connection, retry, log())
        assert again == Committed(
            first.evidence_id, stored=False, granted=False, content=first.content
        )
        # Visibility remembers the attempt that first committed it.
        assert await grants(connection, retry) == [
            (first.evidence_id, lease.attempt_id, "tool_result")
        ]


async def test_investigations_of_one_snapshot_share_a_row(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        lease = await claimed(connection)
        other = await claimed(connection, tag="other")
        first = await commit(connection, lease, log())
        shared = await commit(connection, other, log())

        assert shared == Committed(
            first.evidence_id, stored=False, granted=True, content=first.content
        )
        assert await committed_events(connection, other) == [
            {"evidence_id": str(first.evidence_id), "kind": "log_record", "reused": True}
        ]
        assert len(await all_evidence(connection, OWNER_A)) == 1


async def test_evidence_belongs_to_its_investigations_snapshot(database: ScratchDatabase) -> None:
    bundle = sorted(OBSERVATIONS.glob("*.json"))[1]
    elsewhere = read_bundle(bundle).manifest.snapshot_id
    assert elsewhere != MANIFEST.snapshot_id
    async with database.runtime() as connection:
        lease = await claimed(connection)
        await import_file(connection, OWNER_A, bundle)
        seeded = await seed(connection, OWNER_A, key="seed-q")
        await start_investigation(
            connection,
            OWNER_A,
            OFFLINE,
            seeded.incident_id,
            "elsewhere",
            start_request(snapshot_id=elsewhere),
        )
        other = await claim_next(connection, "w", OFFLINE)
        assert other is not None

        await commit(connection, lease, log())
        committed = await commit(connection, other, log())
        assert committed.stored
        async with owner_transaction(connection, OWNER_A):
            mine = await visible_evidence(connection, lease.investigation_id)
            theirs = await visible_evidence(connection, other.investigation_id)
        assert [r.snapshot_id for r in mine + theirs] == [MANIFEST.snapshot_id, elsewhere]


async def test_evidence_is_visible_only_where_it_was_granted(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        lease = await claimed(connection)
        other = await claimed(connection, tag="other")
        await commit(connection, lease, log())
        async with owner_transaction(connection, OWNER_A):
            assert await visible_evidence(connection, other.investigation_id) == []


@pytest.mark.parametrize(
    "different",
    [
        log(message="connection reset"),
        log(ref="log/checkout-api/8"),
        series(adapter="fixture-2"),
        series(value=413.0),
        EvidenceDraft(
            kind=EvidenceKind.DEPLOYMENT_RECORD,
            source_ref=log().source_ref,
            adapter_version=log().adapter_version,
            window=None,
            content=log().content,
        ),
    ],
    ids=["content", "source", "adapter", "points", "kind"],
)
@pytest.mark.parametrize("different_first", [True, False], ids=["first", "last"])
async def test_any_identity_difference_stores_a_new_row(
    database: ScratchDatabase, different: EvidenceDraft, different_first: bool
) -> None:
    # Both orders, so a lookup that ignored the difference would find the wrong row in
    # at least one of them.
    drafts = [log(), series(), different]
    if different_first:
        drafts.reverse()
    async with database.runtime() as connection:
        lease = await claimed(connection)
        committed = [await commit(connection, lease, draft) for draft in drafts]
        assert [(c.stored, c.granted) for c in committed] == [(True, True)] * 3
        stored = [row[0] for row in await all_evidence(connection, OWNER_A)]
        assert [c.evidence_id for c in committed] == stored


async def test_evidence_from_an_older_sanitizer_is_not_reused(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        lease = await claimed(connection)
        other = await claimed(connection, tag="other")
        old = await commit(connection, lease, log())
    async with database.migrator() as connection, connection.transaction():
        await connection.execute("SELECT set_config('app.owner_id', %s, true)", (str(OWNER_A),))
        await connection.execute("UPDATE evidence SET redaction_version = '0'")
    async with database.runtime() as connection:
        current = await commit(connection, other, log())
        assert current.stored
        assert current.evidence_id != old.evidence_id
        async with owner_transaction(connection, OWNER_A):
            (record,) = await visible_evidence(connection, other.investigation_id)
        assert record.redaction_version == SANITIZER_VERSION


async def test_owners_never_share_evidence(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        mine = await claimed(connection, OWNER_A)
        theirs = await claimed(connection, OWNER_B)
        a = await commit(connection, mine, log())
        b = await commit(connection, theirs, log())

        assert b.stored
        assert a.evidence_id != b.evidence_id
        assert [row[:2] for row in await all_evidence(connection, OWNER_A)] == [
            (a.evidence_id, OWNER_A)
        ]
        assert [row[:2] for row in await all_evidence(connection, OWNER_B)] == [
            (b.evidence_id, OWNER_B)
        ]


class Abandon(Exception):
    pass


@pytest.mark.parametrize("first_commits", [True, False])
async def test_concurrent_commits_of_one_record_store_it_once(
    database: ScratchDatabase, first_commits: bool
) -> None:
    async with database.runtime() as connection, database.runtime() as racing:
        lease = await claimed(connection)
        other = await claimed(connection, tag="other")
        second: asyncio.Task[Committed] | None = None
        with suppress(Abandon):
            async with fenced_transaction(connection, lease):
                await commit_evidence(connection, lease, log())
                second = asyncio.create_task(commit(racing, other, log()))
                # The second insert waits to see whether the first commits.
                assert await still_blocked(second)
                if not first_commits:
                    raise Abandon
        assert second is not None
        raced = await second
        # If the first rolled back, the waiting insert stores the row itself.
        assert (raced.stored, raced.granted) == (not first_commits, True)
        assert len(await all_evidence(connection, OWNER_A)) == 1


async def test_a_stale_worker_commits_nothing(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        lease = await claimed(connection)
        await expire(connection, lease)
        with pytest.raises(StaleLease):
            await commit(connection, lease, log())
        assert await all_evidence(connection, OWNER_A) == []
        assert await grants(connection, lease) == []


async def test_changed_content_is_detected_on_read(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        lease = await claimed(connection)
        await commit(connection, lease, log())
    async with database.migrator() as connection, connection.transaction():
        await connection.execute("SELECT set_config('app.owner_id', %s, true)", (str(OWNER_A),))
        await connection.execute(
            'UPDATE evidence SET content = content || \'{"message": "edited"}\''
        )
    async with database.runtime() as connection, owner_transaction(connection, OWNER_A):
        with pytest.raises(ValidationError, match="digest"):
            await visible_evidence(connection, lease.investigation_id)


@pytest.mark.parametrize(
    "change",
    [
        "UPDATE evidence SET source_ref = 'x'",
        "DELETE FROM evidence",
        "UPDATE investigation_evidence SET reason = 'tool_result'",
        "DELETE FROM investigation_evidence",
    ],
)
async def test_the_runtime_role_cannot_change_evidence(
    database: ScratchDatabase, change: str
) -> None:
    async with database.runtime() as connection:
        lease = await claimed(connection)
        await commit(connection, lease, log())
        with pytest.raises(errors.InsufficientPrivilege):
            await update(connection, OWNER_A, change)


@pytest.mark.parametrize(
    ("kind", "start", "end"),
    [
        ("log_record", START, START + timedelta(minutes=30)),
        ("metric_series_window", None, None),
        ("span_summary", START + timedelta(minutes=30), START),
    ],
)
async def test_the_schema_ties_windows_to_kinds(
    database: ScratchDatabase, kind: str, start: datetime | None, end: datetime | None
) -> None:
    async with database.runtime() as connection:
        await claimed(connection)
    async with database.migrator() as connection, connection.transaction():
        await connection.execute("SELECT set_config('app.owner_id', %s, true)", (str(OWNER_A),))
        with pytest.raises(errors.CheckViolation, match="evidence_window_by_kind"):
            await connection.execute(
                "INSERT INTO evidence (owner_id, id, snapshot_id, kind, source_ref,"
                " adapter_version, redaction_version, window_start, window_end, content,"
                " content_sha256) VALUES (%s, gen_random_uuid(), %s, %s, 'r', 'a', '1', %s, %s,"
                " '{}', repeat('0', 64))",
                (OWNER_A, MANIFEST.snapshot_id, kind, start, end),
            )
