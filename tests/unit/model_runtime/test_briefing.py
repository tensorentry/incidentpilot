"""The bounded briefing (spec §9.1, D07, D12): built from durable state only, evidence
shown exactly as committed with its selector, and never over its token budget."""

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from pydantic import JsonValue

from incidentpilot.domain.canonical import sha256_digest
from incidentpilot.domain.enums import EvidenceKind, IncidentSeverity
from incidentpilot.domain.evidence import EvidenceRecord
from incidentpilot.domain.ids import EvidenceId, IncidentId, OwnerId, SnapshotId
from incidentpilot.domain.incident import IncidentRecord
from incidentpilot.domain.snapshot import Dependency, ServiceGraph
from incidentpilot.domain.window import TimeWindow
from incidentpilot.model_runtime.briefing import (
    PRIORITY,
    Briefing,
    BriefingTooLarge,
    Remaining,
    build_briefing,
)

START = datetime(2026, 11, 4, 18, 15, tzinfo=UTC)
WINDOW = TimeWindow(start_at=START, end_at=START + timedelta(minutes=30))
SCOPE = TimeWindow(start_at=START - timedelta(hours=1), end_at=START + timedelta(minutes=45))
INCIDENT = IncidentRecord(
    id=IncidentId(UUID("00000000-0000-4000-8000-00000000000a")),
    description="Checkout requests are slow — customers see timeouts",
    service="checkout-api",
    severity=IncidentSeverity.SEV2,
    window=WINDOW,
    sanitizer_version="1",
    created_at=START,
)
GRAPH = ServiceGraph(
    services=["checkout-api", "inventory", "payments"],
    dependencies=[
        Dependency(caller="checkout-api", callee="inventory"),
        Dependency(caller="checkout-api", callee="payments"),
    ],
)
REMAINING = Remaining(
    tool_calls=12,
    calls_per_tool={"search_logs": 4, "get_service_metrics": 6},
    model_requests=7,
    seconds=140,
)
OWNER = OwnerId(UUID("00000000-0000-4000-8000-0000000000a1"))
WINDOWED = (EvidenceKind.METRIC_SERIES_WINDOW, EvidenceKind.SPAN_SUMMARY)


def record(n: int, kind: EvidenceKind, size: int = 10) -> EvidenceRecord:
    content: dict[str, JsonValue] = {"n": n, "text": "x" * size}
    return EvidenceRecord(
        evidence_id=EvidenceId(UUID(f"00000000-0000-4000-8000-{n:012d}")),
        owner_id=OWNER,
        snapshot_id=SnapshotId("snap-a"),
        kind=kind,
        source_ref=f"{kind.value}/{n}",
        adapter_version="fixture-v1",
        redaction_version="1",
        window=WINDOW if kind in WINDOWED else None,
        content=content,
        content_sha256=sha256_digest(content),
    )


def brief(evidence: list[EvidenceRecord], budget: int = 100_000) -> Briefing:
    return build_briefing(
        INCIDENT,
        ["payments", "checkout-api", "inventory"],
        GRAPH,
        SCOPE,
        REMAINING,
        evidence,
        budget,
    )


def parsed(briefing: Briefing) -> dict[str, Any]:
    value = json.loads(briefing.text)
    assert isinstance(value, dict)
    return value


def test_the_briefing_holds_the_incident_scope_and_what_is_left() -> None:
    briefing = parsed(brief([]))

    assert briefing == {
        "incident": {
            "service": "checkout-api",
            "severity": "sev2",
            "window": {
                "start_at": "2026-11-04T18:15:00+00:00",
                "end_at": "2026-11-04T18:45:00+00:00",
            },
            "description": "Checkout requests are slow — customers see timeouts",
        },
        "scope": {
            "services": ["checkout-api", "inventory", "payments"],
            "dependencies": [["checkout-api", "inventory"], ["checkout-api", "payments"]],
            "window": {
                "start_at": "2026-11-04T17:15:00+00:00",
                "end_at": "2026-11-04T19:00:00+00:00",
            },
        },
        "remaining": {
            "tool_calls": 12,
            "calls_per_tool": {"get_service_metrics": 6, "search_logs": 4},
            "model_requests": 7,
            "seconds": 140,
        },
        "evidence": [],
        "omitted": {},
    }


def test_evidence_is_shown_as_committed_with_the_selector_that_cites_it() -> None:
    series, log = record(1, EvidenceKind.METRIC_SERIES_WINDOW), record(2, EvidenceKind.LOG_RECORD)

    briefing = brief([series, log])

    assert parsed(briefing)["evidence"] == [
        {
            "cite": {
                "kind": "metric_series_window",
                "evidence_id": str(series.evidence_id),
                "window": {
                    "start_at": "2026-11-04T18:15:00Z",
                    "end_at": "2026-11-04T18:45:00Z",
                },
            },
            "source": "metric_series_window/1",
            "content": series.content,
        },
        {
            "cite": {"kind": "log_record", "evidence_id": str(log.evidence_id)},
            "source": "log_record/2",
            "content": log.content,
        },
    ]
    assert briefing.shown == (series.evidence_id, log.evidence_id)
    assert briefing.omitted == {}


def test_the_text_is_ascii_json() -> None:
    text = brief([]).text

    assert text.isascii()
    assert "slow \\u2014 customers" in text
    assert parsed(brief([]))["incident"]["description"] == INCIDENT.description


def records_of_every_kind() -> list[EvidenceRecord]:
    kinds = [
        EvidenceKind.LOG_RECORD,
        EvidenceKind.METRIC_SERIES_WINDOW,
        EvidenceKind.DEPLOYMENT_RECORD,
        EvidenceKind.SPAN_SUMMARY,
    ]
    return [record(n, kinds[n % 4], size=200) for n in range(1, 25)]


@pytest.mark.parametrize("budget", [1_500, 2_000, 3_000, 5_000, 8_000])
def test_a_briefing_never_exceeds_its_budget(budget: int) -> None:
    evidence = records_of_every_kind()

    briefing = brief(evidence, budget)

    assert len(briefing.text.encode()) <= budget
    assert len(briefing.shown) + sum(briefing.omitted.values()) == len(evidence)
    assert parsed(briefing)["omitted"] == {
        kind.value: count for kind, count in briefing.omitted.items()
    }


def test_lower_priority_kinds_are_left_out_first() -> None:
    evidence = records_of_every_kind()
    deployments = [e for e in evidence if e.kind is EvidenceKind.DEPLOYMENT_RECORD]
    # Room for every deployment, and less than one more record of any kind. The slack
    # covers the room the size check keeps for the largest possible omitted counts.
    room = len(brief(deployments).text.encode()) + 150

    briefing = brief(evidence, room)

    assert briefing.shown == tuple(e.evidence_id for e in deployments)
    assert briefing.omitted == {
        EvidenceKind.SPAN_SUMMARY: 6,
        EvidenceKind.METRIC_SERIES_WINDOW: 6,
        EvidenceKind.LOG_RECORD: 6,
    }


def test_spans_come_before_series_and_series_before_logs() -> None:
    evidence = records_of_every_kind()
    wanted = [
        e
        for e in evidence
        if e.kind
        in (
            EvidenceKind.DEPLOYMENT_RECORD,
            EvidenceKind.SPAN_SUMMARY,
            EvidenceKind.METRIC_SERIES_WINDOW,
        )
    ]
    room = len(brief(wanted).text.encode()) + 150

    briefing = brief(evidence, room)

    assert briefing.shown == tuple(e.evidence_id for e in wanted)
    assert briefing.omitted == {EvidenceKind.LOG_RECORD: 6}


def test_within_a_kind_the_newest_stay() -> None:
    logs = [record(n, EvidenceKind.LOG_RECORD, size=200) for n in range(1, 7)]
    room = len(brief([]).text.encode()) + 3 * 300

    briefing = brief(logs, room)

    assert briefing.shown == tuple(log.evidence_id for log in logs[-len(briefing.shown) :])
    assert 0 < len(briefing.shown) < len(logs)
    assert briefing.omitted == {EvidenceKind.LOG_RECORD: len(logs) - len(briefing.shown)}


def test_what_is_shown_stays_in_commit_order() -> None:
    evidence = records_of_every_kind()

    briefing = brief(evidence)

    assert briefing.shown == tuple(e.evidence_id for e in evidence)
    cited = [item["cite"]["evidence_id"] for item in parsed(briefing)["evidence"]]
    assert cited == [str(e.evidence_id) for e in evidence]


def test_a_smaller_record_still_fits_after_a_larger_one_does_not() -> None:
    big = record(1, EvidenceKind.DEPLOYMENT_RECORD, size=5_000)
    small = record(2, EvidenceKind.LOG_RECORD)
    room = len(brief([]).text.encode()) + 500

    briefing = brief([big, small], room)

    assert briefing.shown == (small.evidence_id,)
    assert briefing.omitted == {EvidenceKind.DEPLOYMENT_RECORD: 1}


def test_the_budget_is_a_hard_limit_at_the_edge() -> None:
    log = record(1, EvidenceKind.LOG_RECORD)

    def shows(budget: int) -> bool:
        try:
            return bool(brief([log], budget).shown)
        except BriefingTooLarge:
            return False

    low, high = 0, 100_000
    while high - low > 1:  # the smallest budget that shows the record
        middle = (low + high) // 2
        low, high = (low, middle) if shows(middle) else (middle, high)

    assert brief([log], high).shown == (log.evidence_id,)
    assert len(brief([log], high).text.encode()) <= high
    assert brief([log], high - 1).shown == ()


def test_the_smallest_budget_is_exactly_what_the_size_check_counts() -> None:
    logs = [record(1, EvidenceKind.LOG_RECORD), record(2, EvidenceKind.LOG_RECORD)]
    data = parsed(brief(logs))
    # The size check keeps room for the most omitted counts there could be.
    data["omitted"] = {kind.value: len(logs) for kind in PRIORITY}
    counted = len(json.dumps(data, separators=(",", ":")))

    assert brief(logs, counted).shown == tuple(log.evidence_id for log in logs)
    assert len(brief(logs, counted - 1).shown) == 1


def test_counts_per_tool_are_listed_in_name_order() -> None:
    assert list(parsed(brief([]))["remaining"]["calls_per_tool"]) == [
        "get_service_metrics",
        "search_logs",
    ]


def test_a_header_over_budget_is_refused() -> None:
    with pytest.raises(
        BriefingTooLarge, match=r"^the briefing needs \d+ tokens before any evidence$"
    ):
        brief([], 100)


def test_a_header_that_exactly_fills_the_budget_is_allowed() -> None:
    data = parsed(brief([]))
    data["omitted"] = {kind.value: 0 for kind in PRIORITY}
    header = len(json.dumps(data, separators=(",", ":")))

    assert brief([], header).shown == ()
    with pytest.raises(BriefingTooLarge):
        brief([], header - 1)


def test_every_kind_has_a_priority() -> None:
    assert sorted(PRIORITY) == sorted(EvidenceKind)
