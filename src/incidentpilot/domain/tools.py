"""The tool envelope of spec §8.2, and the trusted context of §8.1.

Every tool returns the same envelope. `ToolContext` holds what the application
injects into a call; none of it is ever a model-writable argument. Each tool's
arguments and `data` shape arrive with the tool itself (T4.3, T4.4).
"""

from typing import Annotated, Literal, Self

from pydantic import Field, JsonValue, model_validator

from incidentpilot.domain.contract import Contract
from incidentpilot.domain.enums import ToolErrorCode, ToolName, ToolStatus
from incidentpilot.domain.evidence import EvidenceSelector
from incidentpilot.domain.ids import (
    AttemptId,
    InvestigationId,
    InvocationId,
    LeaseEpoch,
    OwnerId,
    SnapshotId,
)
from incidentpilot.domain.window import TimeWindow

MAX_MESSAGE = 500
MAX_RETRY_AFTER_MS = 60_000
MAX_GAPS = 50
MAX_EVIDENCE_REFS = 200

Snapshot = Annotated[SnapshotId, Field(min_length=1, max_length=128)]
Version = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")]


class ToolContext(Contract):
    """Injected by the application for each call (§8.1). The gateway fences with it."""

    owner_id: OwnerId
    investigation_id: InvestigationId
    attempt_id: AttemptId
    lease_epoch: Annotated[LeaseEpoch, Field(ge=1)]
    snapshot_id: Snapshot
    invocation_id: InvocationId
    tool: ToolName


class ToolError(Contract):
    code: ToolErrorCode
    retryable: bool
    message: Annotated[str, Field(min_length=1, max_length=MAX_MESSAGE)]
    retry_after_ms: Annotated[int, Field(ge=1, le=MAX_RETRY_AFTER_MS)] | None

    @model_validator(mode="after")
    def _retry_matches_code(self) -> Self:
        if self.retryable is not self.code.retryable:
            raise ValueError(
                f"{self.code} is never retryable" if self.retryable else f"{self.code} is retryable"
            )
        if self.retry_after_ms is not None and not self.retryable:
            raise ValueError("only a retryable error suggests when to retry")
        return self


class Coverage(Contract):
    """How much of the asked-for data the source had. Missing data never means healthy."""

    fraction: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
    samples: Annotated[int, Field(ge=0)]
    gaps: Annotated[list[TimeWindow], Field(max_length=MAX_GAPS)]
    clock_caveat: Annotated[str, Field(min_length=1, max_length=MAX_MESSAGE)] | None

    @model_validator(mode="after")
    def _gaps_reduce_coverage(self) -> Self:
        if self.gaps and self.fraction >= 1:
            raise ValueError("coverage with gaps can't be complete")
        return self

    @property
    def complete(self) -> bool:
        return self.fraction >= 1 and not self.gaps


class ToolSource(Contract):
    adapter: Version
    version: Version
    snapshot_id: Snapshot


class ToolResponse(Contract):
    schema_version: Literal["3.0"]
    invocation_id: InvocationId
    status: ToolStatus
    source: ToolSource | None
    data: dict[str, JsonValue] | None
    coverage: Coverage | None
    evidence_refs: Annotated[list[EvidenceSelector], Field(max_length=MAX_EVIDENCE_REFS)]
    truncated: bool
    next_cursor: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,512}$")] | None
    error: ToolError | None

    @model_validator(mode="after")
    def _status_fits(self) -> Self:
        if self.next_cursor is not None and not self.truncated:
            raise ValueError("a result with a next page is truncated")
        if self.status is ToolStatus.ERROR:
            if self.error is None:
                raise ValueError("an error response needs an error")
            # No evidence ID leaves the gateway unless its row committed (§8.4).
            if self.data is not None or self.coverage is not None or self.evidence_refs:
                raise ValueError("an error response carries no data, coverage or evidence")
            if self.truncated:
                raise ValueError("an error response has no pages")
            return self
        if self.error is not None:
            raise ValueError(f"status {self.status} has no error")
        if self.source is None or self.data is None or self.coverage is None:
            raise ValueError(f"status {self.status} needs a source, data and coverage")
        if self.status is ToolStatus.PARTIAL:
            if self.coverage.complete:
                raise ValueError("a partial response has incomplete coverage")
            return self
        # An empty result with complete coverage differs from a failed query (§8.2).
        if not self.coverage.complete:
            raise ValueError(f"status {self.status} needs complete coverage")
        if self.status is ToolStatus.OK and not self.evidence_refs:
            raise ValueError("an ok response cites the evidence it returned")
        if self.status is ToolStatus.EMPTY and (self.evidence_refs or self.truncated):
            raise ValueError("an empty response has no evidence and no more pages")
        return self
