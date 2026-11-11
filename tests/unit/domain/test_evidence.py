import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from incidentpilot.domain.canonical import sha256_digest
from incidentpilot.domain.enums import EvidenceKind
from incidentpilot.domain.evidence import (
    EVIDENCE_SELECTOR_ADAPTER,
    DeploymentRecordSelector,
    EvidenceRecord,
    EvidenceSelector,
    LogRecordSelector,
    MetricSeriesWindowSelector,
    SelectorError,
    SpanSummarySelector,
    check_selector,
)
from incidentpilot.domain.ids import EvidenceId, OwnerId, SnapshotId
from incidentpilot.domain.window import TimeWindow

REPO_ROOT = Path(__file__).resolve().parents[3]
ERRATA = REPO_ROOT / "docs" / "spec-errata.md"

EVIDENCE_ID = EvidenceId(UUID("00000000-0000-4000-8000-000000000001"))
OTHER_EVIDENCE_ID = EvidenceId(UUID("00000000-0000-4000-8000-000000000002"))
OWNER = OwnerId(UUID("00000000-0000-4000-8000-0000000000a1"))
OTHER_OWNER = OwnerId(UUID("00000000-0000-4000-8000-0000000000b2"))
SNAPSHOT = SnapshotId("snap-case-a")
OTHER_SNAPSHOT = SnapshotId("snap-case-b")
START = datetime(2026, 11, 4, 18, 0, tzinfo=UTC)
END = START + timedelta(minutes=30)
SERIES = TimeWindow(start_at=START, end_at=END)
CONTENT: dict[str, Any] = {"metric": "request_count", "points": [{"t": "18:15", "v": 240}]}
WINDOWED = {EvidenceKind.METRIC_SERIES_WINDOW, EvidenceKind.SPAN_SUMMARY}


def record(
    kind: EvidenceKind = EvidenceKind.METRIC_SERIES_WINDOW, /, **changes: Any
) -> EvidenceRecord:
    fields: dict[str, Any] = {
        "evidence_id": EVIDENCE_ID,
        "owner_id": OWNER,
        "snapshot_id": SNAPSHOT,
        "kind": kind,
        "source_ref": "metric_series_window:checkout-api:request_count:60s",
        "adapter_version": "fixture-v1",
        "redaction_version": "redact-v1",
        "window": SERIES if kind in WINDOWED else None,
        "content": CONTENT,
        "content_sha256": sha256_digest(CONTENT),
    }
    return EvidenceRecord.model_validate(fields | changes)


def selector_json(kind: str, **fields: Any) -> str:
    return json.dumps({"kind": kind, "evidence_id": str(EVIDENCE_ID), **fields})


def parse(text: str) -> EvidenceSelector:
    return EVIDENCE_SELECTOR_ADAPTER.validate_json(text)


SERIES_WINDOW = {"start_at": "2026-11-04T18:00:00Z", "end_at": "2026-11-04T18:30:00Z"}
SELECTORS: dict[EvidenceKind, EvidenceSelector] = {
    EvidenceKind.METRIC_SERIES_WINDOW: MetricSeriesWindowSelector(
        kind=EvidenceKind.METRIC_SERIES_WINDOW, evidence_id=EVIDENCE_ID, window=SERIES
    ),
    EvidenceKind.LOG_RECORD: LogRecordSelector(
        kind=EvidenceKind.LOG_RECORD, evidence_id=EVIDENCE_ID
    ),
    EvidenceKind.DEPLOYMENT_RECORD: DeploymentRecordSelector(
        kind=EvidenceKind.DEPLOYMENT_RECORD, evidence_id=EVIDENCE_ID
    ),
    EvidenceKind.SPAN_SUMMARY: SpanSummarySelector(
        kind=EvidenceKind.SPAN_SUMMARY, evidence_id=EVIDENCE_ID
    ),
}


def e15_kinds() -> set[str]:
    section = ERRATA.read_text().split("## E15.")[1].split("## E16.")[0]
    return set(re.findall(r"^\|[^|]+\|[^|]+\| `(\w+)` \|", section, re.MULTILINE))


def test_evidence_kinds_match_errata_e15() -> None:
    assert {kind.value for kind in EvidenceKind} == e15_kinds()


def test_every_kind_has_exactly_one_selector() -> None:
    assert set(SELECTORS) == set(EvidenceKind)
    assert {type(selector) for selector in SELECTORS.values()} == {
        MetricSeriesWindowSelector,
        LogRecordSelector,
        DeploymentRecordSelector,
        SpanSummarySelector,
    }


@pytest.mark.parametrize("kind", list(EvidenceKind))
def test_selectors_parse_from_json_by_kind(kind: EvidenceKind) -> None:
    extra = {"window": SERIES_WINDOW} if kind is EvidenceKind.METRIC_SERIES_WINDOW else {}

    assert parse(selector_json(kind.value, **extra)) == SELECTORS[kind]


@pytest.mark.parametrize("kind", list(EvidenceKind))
def test_selectors_round_trip_through_json(kind: EvidenceKind) -> None:
    selector = SELECTORS[kind]

    assert parse(EVIDENCE_SELECTOR_ADAPTER.dump_json(selector).decode()) == selector


@pytest.mark.parametrize("kind", ["row", "metric", "", "LOG_RECORD"])
def test_unknown_kinds_are_rejected(kind: str) -> None:
    with pytest.raises(ValidationError, match="union_tag_invalid"):
        parse(selector_json(kind))


def test_a_selector_without_a_kind_is_rejected() -> None:
    with pytest.raises(ValidationError, match="union_tag_not_found"):
        parse(json.dumps({"evidence_id": str(EVIDENCE_ID)}))


@pytest.mark.parametrize(
    "smuggled",
    [
        {"path": "$.points[0]"},
        {"owner_id": str(OTHER_OWNER)},
        {"snapshot_id": OTHER_SNAPSHOT},
        {"window": SERIES_WINDOW},
        {"start_at": "2026-11-04T18:00:00Z"},
    ],
    ids=["path", "owner", "snapshot", "window", "start"],
)
def test_record_selectors_reject_extra_fields(smuggled: dict[str, Any]) -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        parse(selector_json("log_record", **smuggled))


@pytest.mark.parametrize(
    "smuggled",
    [{"owner_id": str(OTHER_OWNER)}, {"snapshot_id": OTHER_SNAPSHOT}, {"path": "$.points"}],
    ids=["owner", "snapshot", "path"],
)
def test_series_selectors_reject_extra_fields(smuggled: dict[str, Any]) -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        parse(selector_json("metric_series_window", window=SERIES_WINDOW, **smuggled))


def test_a_series_selector_needs_a_window() -> None:
    with pytest.raises(ValidationError, match="window"):
        parse(selector_json("metric_series_window"))


@pytest.mark.parametrize(
    "window",
    [
        {"start_at": "2026-11-04T18:00:00", "end_at": "2026-11-04T18:30:00Z"},
        {"start_at": "2026-11-04T18:30:00Z", "end_at": "2026-11-04T18:00:00Z"},
        {"start_at": "2026-11-04T18:00:00Z"},
        {"start_at": "yesterday", "end_at": "2026-11-04T18:30:00Z"},
    ],
    ids=["naive", "inverted", "open-ended", "not-a-time"],
)
def test_malformed_series_windows_are_rejected(window: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        parse(selector_json("metric_series_window", window=window))


@pytest.mark.parametrize("evidence_id", ["not-a-uuid", "", "12345", None, 1])
def test_malformed_evidence_ids_are_rejected(evidence_id: object) -> None:
    with pytest.raises(ValidationError, match="evidence_id"):
        parse(json.dumps({"kind": "log_record", "evidence_id": evidence_id}))


def test_selectors_are_immutable() -> None:
    selector = SELECTORS[EvidenceKind.LOG_RECORD]

    with pytest.raises(ValidationError, match="frozen"):
        selector.evidence_id = OTHER_EVIDENCE_ID  # type: ignore[misc]


def test_selector_schema_has_no_optional_or_open_fields() -> None:
    definitions = EVIDENCE_SELECTOR_ADAPTER.json_schema()["$defs"]
    variants = {name: schema for name, schema in definitions.items() if name.endswith("Selector")}

    assert len(variants) == len(EvidenceKind)
    for schema in variants.values():
        assert set(schema["required"]) == set(schema["properties"])
        assert schema["additionalProperties"] is False


@pytest.mark.parametrize("kind", list(EvidenceKind))
def test_records_are_valid_for_every_kind(kind: EvidenceKind) -> None:
    assert record(kind).kind is kind


@pytest.mark.parametrize("kind", sorted(WINDOWED))
def test_windowed_kinds_need_a_window(kind: EvidenceKind) -> None:
    with pytest.raises(ValidationError, match=f"{kind} evidence needs a window"):
        record(kind, window=None)


@pytest.mark.parametrize("kind", sorted(set(EvidenceKind) - WINDOWED))
def test_single_records_have_no_window(kind: EvidenceKind) -> None:
    with pytest.raises(ValidationError, match=f"{kind} evidence has no window"):
        record(kind, window=SERIES)


def test_content_must_match_its_digest() -> None:
    with pytest.raises(ValidationError, match="content doesn't match its digest"):
        record(content={**CONTENT, "metric": "error_count"})


def test_digest_uses_canonical_json() -> None:
    reordered = {"points": [{"v": 240, "t": "18:15"}], "metric": "request_count"}

    assert record(content=reordered).content_sha256 == sha256_digest(CONTENT)


@pytest.mark.parametrize(
    "digest", ["", "abc", sha256_digest(CONTENT).upper(), sha256_digest(CONTENT) + "0"]
)
def test_malformed_digests_are_rejected(digest: str) -> None:
    with pytest.raises(ValidationError, match="content_sha256"):
        record(content_sha256=digest)


def test_content_without_a_canonical_encoding_is_rejected() -> None:
    with pytest.raises(ValidationError, match="floats must be finite"):
        record(content={"v": float("nan")}, content_sha256="0" * 64)


@pytest.mark.parametrize(
    "field", ["source_ref", "adapter_version", "redaction_version", "snapshot_id"]
)
def test_identity_fields_cannot_be_empty(field: str) -> None:
    with pytest.raises(ValidationError, match=field):
        record(**{field: ""})


@pytest.mark.parametrize(
    ("field", "length"),
    [("source_ref", 513), ("adapter_version", 65), ("redaction_version", 65), ("snapshot_id", 129)],
)
def test_identity_fields_are_bounded(field: str, length: int) -> None:
    with pytest.raises(ValidationError, match=field):
        record(**{field: "x" * length})


def test_records_reject_unknown_fields() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        record(path="$.points")


def test_records_are_immutable() -> None:
    with pytest.raises(ValidationError, match="frozen"):
        record().snapshot_id = OTHER_SNAPSHOT  # type: ignore[misc]


@pytest.mark.parametrize("kind", list(EvidenceKind))
def test_a_matching_selector_resolves(kind: EvidenceKind) -> None:
    check_selector(SELECTORS[kind], record(kind), owner_id=OWNER, snapshot_id=SNAPSHOT)


def test_cross_snapshot_selectors_are_rejected() -> None:
    with pytest.raises(SelectorError, match=r"^evidence is from another snapshot$"):
        check_selector(
            SELECTORS[EvidenceKind.LOG_RECORD],
            record(EvidenceKind.LOG_RECORD, snapshot_id=OTHER_SNAPSHOT),
            owner_id=OWNER,
            snapshot_id=SNAPSHOT,
        )


def test_another_owners_evidence_is_rejected_even_with_the_same_snapshot_id() -> None:
    # E19 imports a fixture once per owner, so two owners can hold the same snapshot ID.
    with pytest.raises(SelectorError, match=r"^evidence belongs to another owner$"):
        check_selector(
            SELECTORS[EvidenceKind.LOG_RECORD],
            record(EvidenceKind.LOG_RECORD, owner_id=OTHER_OWNER),
            owner_id=OWNER,
            snapshot_id=SNAPSHOT,
        )


def test_a_selector_must_name_the_record_it_is_checked_against() -> None:
    with pytest.raises(SelectorError, match=r"^selector names a different evidence row$"):
        check_selector(
            SELECTORS[EvidenceKind.LOG_RECORD],
            record(EvidenceKind.LOG_RECORD, evidence_id=OTHER_EVIDENCE_ID),
            owner_id=OWNER,
            snapshot_id=SNAPSHOT,
        )


@pytest.mark.parametrize(
    ("selected", "stored"),
    [
        (EvidenceKind.LOG_RECORD, EvidenceKind.DEPLOYMENT_RECORD),
        (EvidenceKind.SPAN_SUMMARY, EvidenceKind.METRIC_SERIES_WINDOW),
        (EvidenceKind.METRIC_SERIES_WINDOW, EvidenceKind.SPAN_SUMMARY),
    ],
)
def test_a_selector_must_match_the_evidence_kind(
    selected: EvidenceKind, stored: EvidenceKind
) -> None:
    message = rf"^selector kind {selected} doesn't match evidence kind {stored}$"

    with pytest.raises(SelectorError, match=message):
        check_selector(SELECTORS[selected], record(stored), owner_id=OWNER, snapshot_id=SNAPSHOT)


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (START, END),
        (START + timedelta(minutes=10), START + timedelta(minutes=15)),
    ],
    ids=["whole-series", "sub-window"],
)
def test_a_series_selector_may_narrow_the_window(start: datetime, end: datetime) -> None:
    selector = MetricSeriesWindowSelector(
        kind=EvidenceKind.METRIC_SERIES_WINDOW,
        evidence_id=EVIDENCE_ID,
        window=TimeWindow(start_at=start, end_at=end),
    )

    check_selector(selector, record(), owner_id=OWNER, snapshot_id=SNAPSHOT)


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (START - timedelta(seconds=1), END),
        (START, END + timedelta(seconds=1)),
        (END, END + timedelta(minutes=5)),
    ],
    ids=["starts-before", "ends-after", "after"],
)
def test_a_series_selector_cannot_reach_outside_the_series(start: datetime, end: datetime) -> None:
    selector = MetricSeriesWindowSelector(
        kind=EvidenceKind.METRIC_SERIES_WINDOW,
        evidence_id=EVIDENCE_ID,
        window=TimeWindow(start_at=start, end_at=end),
    )

    with pytest.raises(SelectorError, match=r"^selected window is outside the series$"):
        check_selector(selector, record(), owner_id=OWNER, snapshot_id=SNAPSHOT)
