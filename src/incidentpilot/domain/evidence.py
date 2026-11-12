"""Evidence rows and the selectors that cite them: errata E3, E15 and E19.

A selector is the only way to point at evidence. It carries no owner or snapshot,
because the model never writes either: both come from the investigation.
"""

from typing import Annotated, Literal, Self

from pydantic import Field, JsonValue, TypeAdapter, model_validator

from incidentpilot.domain.canonical import sha256_digest
from incidentpilot.domain.contract import Contract
from incidentpilot.domain.enums import EvidenceKind
from incidentpilot.domain.ids import EvidenceId, OwnerId, SnapshotId
from incidentpilot.domain.window import TimeWindow

# E15 defines these kinds over a window. The others are single source records.
_WINDOWED = frozenset({EvidenceKind.METRIC_SERIES_WINDOW, EvidenceKind.SPAN_SUMMARY})


class SelectorError(ValueError):
    """A selector doesn't resolve to the evidence row it is checked against."""


class MetricSeriesWindowSelector(Contract):
    kind: Literal[EvidenceKind.METRIC_SERIES_WINDOW]
    evidence_id: EvidenceId
    window: TimeWindow


class LogRecordSelector(Contract):
    kind: Literal[EvidenceKind.LOG_RECORD]
    evidence_id: EvidenceId


class DeploymentRecordSelector(Contract):
    kind: Literal[EvidenceKind.DEPLOYMENT_RECORD]
    evidence_id: EvidenceId


class SpanSummarySelector(Contract):
    kind: Literal[EvidenceKind.SPAN_SUMMARY]
    evidence_id: EvidenceId


EvidenceSelector = Annotated[
    MetricSeriesWindowSelector | LogRecordSelector | DeploymentRecordSelector | SpanSummarySelector,
    Field(discriminator="kind"),
]
EVIDENCE_SELECTOR_ADAPTER: TypeAdapter[EvidenceSelector] = TypeAdapter(EvidenceSelector)


class EvidenceRecord(Contract):
    """One evidence row: the smallest citable source record (E15), owned per E19."""

    evidence_id: EvidenceId
    owner_id: OwnerId
    snapshot_id: Annotated[SnapshotId, Field(min_length=1, max_length=128)]
    kind: EvidenceKind
    source_ref: Annotated[str, Field(min_length=1, max_length=512)]
    adapter_version: Annotated[str, Field(min_length=1, max_length=64)]
    redaction_version: Annotated[str, Field(min_length=1, max_length=64)]
    window: TimeWindow | None
    content: dict[str, JsonValue]
    content_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.kind in _WINDOWED and self.window is None:
            raise ValueError(f"{self.kind} evidence needs a window")
        if self.kind not in _WINDOWED and self.window is not None:
            raise ValueError(f"{self.kind} evidence has no window")
        if sha256_digest(self.content) != self.content_sha256:
            raise ValueError("content doesn't match its digest")
        return self


def check_selector(
    selector: EvidenceSelector,
    record: EvidenceRecord,
    *,
    owner_id: OwnerId,
    snapshot_id: SnapshotId,
) -> None:
    """Raise unless the selector resolves to this row for this owner and snapshot."""
    if selector.evidence_id != record.evidence_id:
        raise SelectorError("selector names a different evidence row")
    # Checked before the snapshot: E19 lets two owners hold the same snapshot ID.
    if record.owner_id != owner_id:
        raise SelectorError("evidence belongs to another owner")
    if record.snapshot_id != snapshot_id:
        raise SelectorError("evidence is from another snapshot")
    if selector.kind is not record.kind:
        raise SelectorError(
            f"selector kind {selector.kind} doesn't match evidence kind {record.kind}"
        )
    if isinstance(selector, MetricSeriesWindowSelector) and not (
        record.window is not None and record.window.contains(selector.window)
    ):
        raise SelectorError("selected window is outside the series")
