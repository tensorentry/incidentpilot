"""Incidents as a user reports them (spec §7.2, §12.1).

Creating an incident never starts an investigation. The description is untrusted, so
it's sanitized before it's stored, and the stored record says which sanitizer did it.
"""

from datetime import datetime, timedelta
from typing import Annotated, Self

from pydantic import Field, model_validator

from incidentpilot.domain.contract import Contract
from incidentpilot.domain.enums import IncidentSeverity
from incidentpilot.domain.ids import IncidentId
from incidentpilot.domain.names import ServiceName
from incidentpilot.domain.window import TimeWindow

MAX_DESCRIPTION = 2_000
# Long enough for a bad day, short enough to keep requests bounded.
MAX_INCIDENT_WINDOW = timedelta(hours=24)


class CreateIncident(Contract):
    description: Annotated[str, Field(min_length=1, max_length=MAX_DESCRIPTION)]
    service: ServiceName
    severity: IncidentSeverity
    window: TimeWindow

    @model_validator(mode="after")
    def _window_is_bounded(self) -> Self:
        if self.window.end_at - self.window.start_at > MAX_INCIDENT_WINDOW:
            raise ValueError("an incident window can't exceed 24 hours")
        return self


class IncidentRecord(Contract):
    id: IncidentId
    description: Annotated[str, Field(min_length=1, max_length=MAX_DESCRIPTION)]
    service: ServiceName
    severity: IncidentSeverity
    window: TimeWindow
    sanitizer_version: Annotated[str, Field(pattern=r"^[0-9]+$")]
    created_at: datetime
