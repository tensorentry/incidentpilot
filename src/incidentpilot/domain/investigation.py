"""Starting an investigation: the request and the configuration it freezes.

Spec §2.4 gives the limits, §12.2 the request, and erratum E8 says the frozen values
are stored, not only their digest, so a worker reads exactly what was admitted.
"""

from datetime import datetime, timedelta
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, Field, model_validator

from incidentpilot.domain.canonical import sha256_digest
from incidentpilot.domain.contract import Contract
from incidentpilot.domain.enums import InvestigationStatus, RunMode
from incidentpilot.domain.ids import AttemptId, IncidentId, InvestigationId
from incidentpilot.domain.measurement import distinct
from incidentpilot.domain.names import ServiceName
from incidentpilot.domain.snapshot import MAX_SERVICES, Snapshot
from incidentpilot.domain.window import TimeWindow

# §2.4: a telemetry search window is at most 2 hours.
MAX_SEARCH_WINDOW = timedelta(hours=2)
# §2.4 and E11: at most 20 investigations wait in the queue, across all owners.
QUEUE_LIMIT = 20
ModelProfile = Literal["single-investigator-default"]
Positive = Annotated[int, Field(gt=0)]


class InvestigationLimits(Contract):
    """§2.4's initial configuration. These are proposals, not measured SLOs."""

    tool_calls: Positive = 15
    calls_per_tool: Positive = 6
    model_requests: Positive = 10
    recovery_attempts: Annotated[int, Field(ge=0)] = 2
    deadline_seconds: Positive = 180
    finalization_reserve_seconds: Positive = 15
    # $0.50 as an estimate for reservations, not a guarantee about the invoice.
    spend_cap_micro_usd: Positive = 500_000

    @model_validator(mode="after")
    def _limits_fit_together(self) -> Self:
        if self.calls_per_tool > self.tool_calls:
            raise ValueError("a per-tool cap can't exceed the total tool cap")
        if self.finalization_reserve_seconds >= self.deadline_seconds:
            raise ValueError("the finalization reserve must leave time before the deadline")
        return self


class StartInvestigation(Contract):
    mode: RunMode
    snapshot_id: Snapshot
    time_window: TimeWindow
    model_profile: ModelProfile

    @model_validator(mode="after")
    def _window_is_searchable(self) -> Self:
        if self.time_window.end_at - self.time_window.start_at > MAX_SEARCH_WINDOW:
            raise ValueError("an investigation window can't exceed 2 hours")
        return self


class FrozenConfig(Contract):
    """Everything an investigation runs with. It never changes after admission."""

    mode: Literal[RunMode.OFFLINE_DEMO, RunMode.LIVE]
    model_profile: ModelProfile
    snapshot_id: Snapshot
    window: TimeWindow
    allowed_services: Annotated[
        list[ServiceName], Field(min_length=1, max_length=MAX_SERVICES), AfterValidator(distinct)
    ]
    limits: InvestigationLimits

    @property
    def digest(self) -> str:
        return sha256_digest(self.model_dump())


class InvestigationRecord(Contract):
    id: InvestigationId
    incident_id: IncidentId
    parent_investigation_id: InvestigationId | None
    status: InvestigationStatus
    config: FrozenConfig
    current_attempt_id: AttemptId | None
    lease_epoch: Annotated[int, Field(ge=0)]
    next_event_seq: Annotated[int, Field(ge=1)]
    created_at: datetime
