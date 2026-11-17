from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from pydantic import JsonValue

from incidentpilot.domain.canonical import sha256_digest
from incidentpilot.domain.enums import EvidenceKind
from incidentpilot.domain.evidence import EvidenceRecord
from incidentpilot.domain.ids import EvidenceId, OwnerId, SnapshotId
from incidentpilot.domain.window import TimeWindow
from incidentpilot.workflows.scripted import abstain, cite

START = datetime(2026, 11, 4, 18, 0, tzinfo=UTC)
WINDOW = TimeWindow(start_at=START, end_at=START + timedelta(minutes=30))


def record(n: int, kind: EvidenceKind) -> EvidenceRecord:
    content: dict[str, JsonValue] = {"n": n}
    return EvidenceRecord(
        evidence_id=EvidenceId(UUID(f"00000000-0000-4000-8000-{n:012d}")),
        owner_id=OwnerId(UUID("00000000-0000-4000-8000-0000000000a1")),
        snapshot_id=SnapshotId("snap-case-a"),
        kind=kind,
        source_ref=f"{kind}:{n}",
        adapter_version="fixture-v1",
        redaction_version="redact-v1",
        window=WINDOW if kind is EvidenceKind.METRIC_SERIES_WINDOW else None,
        content=content,
        content_sha256=sha256_digest(content),
    )


SERIES = record(1, EvidenceKind.METRIC_SERIES_WINDOW)
LOG = record(2, EvidenceKind.LOG_RECORD)


def test_a_series_is_cited_over_its_whole_window() -> None:
    assert cite(SERIES).model_dump() == {
        "kind": EvidenceKind.METRIC_SERIES_WINDOW,
        "evidence_id": SERIES.evidence_id,
        "window": WINDOW.model_dump(),
    }


def test_a_single_record_is_cited_by_its_id() -> None:
    assert cite(LOG).model_dump() == {
        "kind": EvidenceKind.LOG_RECORD,
        "evidence_id": LOG.evidence_id,
    }


def test_the_abstention_observes_each_record_once_and_concludes_nothing() -> None:
    candidate = abstain([SERIES, LOG])

    expected: dict[str, Any] = {
        "diagnostic_outcome": "insufficient_evidence",
        "observations": [
            {
                "observation_id": f"obs-{n}",
                "claim": "The scripted investigator saw this record.",
                "claim_kind": "observed",
                "references": [cite(seen).model_dump(mode="json")],
                "measurement": None,
            }
            for n, seen in [(1, SERIES), (2, LOG)]
        ],
        "hypotheses": [],
        "root_causes": [],
        "impact": {"affected_services": [], "measurements": []},
        "alternatives": [],
        "recommendations": [],
        "limitations": ["A scripted investigator draws no conclusions."],
    }
    assert candidate.model_dump(mode="json") == expected
