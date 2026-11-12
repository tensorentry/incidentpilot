"""The oracle for one case: what a correct investigation should conclude (D17).

Oracles live under `scenarios/oracle/`, keyed by snapshot ID. Only the grader
reads them, after a report is published (E6).
"""

from typing import Annotated, Literal, Self

from pydantic import AfterValidator, Field, model_validator

from incidentpilot.domain.contract import Contract
from incidentpilot.domain.enums import CausalRole, DiagnosticOutcome
from incidentpilot.domain.measurement import distinct
from incidentpilot.domain.names import Category, ServiceName
from incidentpilot.domain.snapshot import Snapshot


class OracleCause(Contract):
    component: ServiceName
    category: Category
    role: CausalRole


class Oracle(Contract):
    schema_version: Literal["1"]
    snapshot_id: Snapshot
    causes: Annotated[list[OracleCause], Field(max_length=10), AfterValidator(distinct)]
    acceptable_outcomes: Annotated[
        list[DiagnosticOutcome], Field(min_length=1, max_length=3), AfterValidator(distinct)
    ]
    discriminating_tests: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=500)]], Field(max_length=10)
    ]

    @model_validator(mode="after")
    def _outcomes_fit_causes(self) -> Self:
        # A case with no recoverable cause is unanswerable: only abstaining or a
        # clearly qualified partial report is right (§14.1, case 3).
        if not self.causes and DiagnosticOutcome.DIAGNOSED in self.acceptable_outcomes:
            raise ValueError("an unanswerable case can't accept a diagnosis")
        if self.causes and DiagnosticOutcome.INSUFFICIENT in self.acceptable_outcomes:
            raise ValueError("an answerable case can't accept an abstention")
        return self

    @property
    def answerable(self) -> bool:
        return bool(self.causes)
