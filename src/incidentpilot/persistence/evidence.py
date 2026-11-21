"""Committed evidence and who may see it (spec §7.3, §8.2; errata E14, E15 and E19).

Workers call `commit_evidence` inside `fenced_transaction`, so a stale worker's evidence
rolls back with the rest of its writes. Content is sanitized before it is hashed or
stored, and the owner and snapshot come from the investigation, never from the caller.
"""

from dataclasses import dataclass
from uuid import uuid4

from psycopg.types.json import Jsonb
from pydantic import JsonValue

from incidentpilot.domain.canonical import sha256_digest
from incidentpilot.domain.enums import EventType, EvidenceKind
from incidentpilot.domain.evidence import EvidenceDraft, EvidenceRecord
from incidentpilot.domain.ids import EvidenceId, InvestigationId
from incidentpilot.domain.window import TimeWindow
from incidentpilot.persistence.database import Connection
from incidentpilot.persistence.events import append_event
from incidentpilot.persistence.fence import Lease
from incidentpilot.policies.redaction import SANITIZER_VERSION, sanitize

# A log message is at most 2,000 characters in a bundle, so this cuts nothing real.
MAX_CONTENT_TEXT = 2_000
MAX_SOURCE_REF = 512
VISIBILITY_REASON = "tool_result"
_SNAPSHOT = "SELECT snapshot_id FROM investigations WHERE id = %s"
_INSERT = """
INSERT INTO evidence (
    owner_id, id, snapshot_id, kind, source_ref, adapter_version, redaction_version,
    window_start, window_end, content, content_sha256
)
VALUES (current_owner_id(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT ON CONSTRAINT evidence_identity DO NOTHING
"""
_EXISTING = """
SELECT id FROM evidence
WHERE snapshot_id = %s AND kind = %s AND source_ref = %s AND adapter_version = %s
  AND redaction_version = %s AND content_sha256 = %s
"""
_GRANT = """
INSERT INTO investigation_evidence
    (owner_id, investigation_id, evidence_id, first_attempt_id, reason)
VALUES (current_owner_id(), %s, %s, %s, %s)
ON CONFLICT DO NOTHING
"""
_VISIBLE = """
SELECT evidence.id, evidence.owner_id, evidence.snapshot_id, evidence.kind,
       evidence.source_ref, evidence.adapter_version, evidence.redaction_version,
       evidence.window_start, evidence.window_end, evidence.content, evidence.content_sha256
FROM investigation_evidence
JOIN evidence ON evidence.id = investigation_evidence.evidence_id
WHERE investigation_evidence.investigation_id = %s
ORDER BY investigation_evidence.created_at, evidence.id
"""


@dataclass(frozen=True)
class Committed:
    evidence_id: EvidenceId
    # False when an identical row was already stored, by this or another investigation.
    stored: bool
    # False when this investigation could already see it, such as after a recovery.
    granted: bool
    # The sanitized content that was stored, which is all a model may be shown.
    content: dict[str, JsonValue]


def sanitize_content(value: JsonValue) -> JsonValue:
    """Sanitize every string in a JSON value, keys included."""
    match value:
        case str():
            return sanitize(value, limit=MAX_CONTENT_TEXT).text
        case dict():
            cleaned = {
                sanitize(key, limit=MAX_CONTENT_TEXT).text: sanitize_content(item)
                for key, item in value.items()
            }
            if len(cleaned) != len(value):
                raise ValueError("two keys are the same once sanitized")
            return cleaned
        case list():
            return [sanitize_content(item) for item in value]
        case _:
            return value


async def commit_evidence(connection: Connection, lease: Lease, draft: EvidenceDraft) -> Committed:
    """Store a source record once per identity, and let the lease's investigation see it."""
    content = sanitize_content(draft.content)
    assert isinstance(content, dict)  # noqa: S101 (a dict sanitizes to a dict)
    source_ref = sanitize(draft.source_ref, limit=MAX_SOURCE_REF).text
    digest = sha256_digest(content)
    row = await (await connection.execute(_SNAPSHOT, (lease.investigation_id,))).fetchone()
    assert row is not None  # noqa: S101 (the fence holds this row locked)
    snapshot_id = row[0]
    identity = (snapshot_id, draft.kind, source_ref, draft.adapter_version, SANITIZER_VERSION)
    window = draft.window
    cursor = await connection.execute(
        _INSERT,
        (
            uuid4(),
            *identity,
            window and window.start_at,
            window and window.end_at,
            Jsonb(content),
            digest,
        ),
    )
    stored = cursor.rowcount == 1
    # A concurrent insert of the same identity waits for the other to commit, so the
    # row is visible here whichever transaction stored it.
    found = await (await connection.execute(_EXISTING, (*identity, digest))).fetchone()
    assert found is not None  # noqa: S101 (inserted or already there)
    evidence_id = EvidenceId(found[0])
    granted = await connection.execute(
        _GRANT, (lease.investigation_id, evidence_id, lease.attempt_id, VISIBILITY_REASON)
    )
    if granted.rowcount == 1:
        await append_event(
            connection,
            lease.investigation_id,
            EventType.EVIDENCE_COMMITTED,
            {"evidence_id": str(evidence_id), "kind": draft.kind.value, "reused": not stored},
        )
    return Committed(evidence_id, stored, granted.rowcount == 1, content)


async def visible_evidence(
    connection: Connection, investigation_id: InvestigationId
) -> list[EvidenceRecord]:
    """The evidence an investigation may see, checked against its digests. Run inside
    owner_transaction."""
    cursor = await connection.execute(_VISIBLE, (investigation_id,))
    return [
        EvidenceRecord(
            evidence_id=EvidenceId(evidence_id),
            owner_id=owner_id,
            snapshot_id=snapshot_id,
            kind=EvidenceKind(kind),
            source_ref=source_ref,
            adapter_version=adapter_version,
            redaction_version=redaction_version,
            window=None if start_at is None else TimeWindow(start_at=start_at, end_at=end_at),
            content=content,
            content_sha256=digest,
        )
        for (
            evidence_id,
            owner_id,
            snapshot_id,
            kind,
            source_ref,
            adapter_version,
            redaction_version,
            start_at,
            end_at,
            content,
            digest,
        ) in await cursor.fetchall()
    ]
