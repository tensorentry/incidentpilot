"""The investigator runner: one agent run for one attempt (spec §9.1, D07, §11.3; E5).

The runner reads the briefing from what has committed, then lets the SDK run the
turn loop. Every model request passes the call gate and every tool call the tool
gateway, both under the attempt's lease. It returns a candidate or a `Stop` that
names why there is none. Diagnostics stop when only the finalization reserve is left,
so the application still has time to publish a fallback (§11.3).

A lost lease, a tool's own failure and anything the SDK doesn't recognise are raised,
not turned into a `Stop`: they are the worker's failure, and recovery takes over.
"""

import asyncio
from dataclasses import dataclass

from agents import Agent, Model, Runner

from incidentpilot.domain.enums import StopReason
from incidentpilot.domain.report import CandidateOrStop, ReportCandidate, Stop
from incidentpilot.domain.telemetry import TelemetryProvider
from incidentpilot.model_runtime.briefing import load_briefing
from incidentpilot.model_runtime.gate import GatedModel
from incidentpilot.model_runtime.pricing import Pricing
from incidentpilot.model_runtime.prompt import INSTRUCTIONS
from incidentpilot.model_runtime.sdk import (
    candidate_output,
    run_config,
    stop_reason,
)
from incidentpilot.persistence.database import Connection, owner_transaction
from incidentpilot.persistence.fence import Lease
from incidentpilot.persistence.ledger import BudgetDenied
from incidentpilot.tools.catalog import TOOLS
from incidentpilot.tools.gateway import ToolGateway, load_scope
from incidentpilot.workflows.lease import LeaseKeeper

_UNTIL_RESERVE = """
SELECT GREATEST(0, extract(epoch FROM deadline_at
                - make_interval(secs => finalization_reserve_seconds)
                - pg_catalog.clock_timestamp()))
FROM investigations WHERE id = %s
"""
# Only a label: with tracing off, nothing reads it.
AGENT_NAME = "investigator"
# Written by the application, never by the model (E5).
DETAILS = {
    StopReason.MODEL_REFUSED: "The model declined to write a report.",
    StopReason.PROVIDER_FAILURE: "The model provider failed or answered outside the schema.",
    StopReason.TURN_LIMIT_REACHED: "The investigation used all of its model turns.",
    StopReason.BUDGET_EXHAUSTED: "The investigation spent its model budget.",
    StopReason.DEADLINE_REACHED: "The investigation reached its deadline.",
    StopReason.FINALIZATION_RESERVE_REACHED: (
        "Diagnostics stopped to leave time for the final report."
    ),
}


@dataclass(frozen=True)
class RunnerLimits:
    max_turns: int
    max_output_tokens: int
    briefing_tokens: int

    def __post_init__(self) -> None:
        if min(self.max_turns, self.max_output_tokens, self.briefing_tokens) <= 0:
            raise ValueError("runner limits must be positive")


def _stop(reason: StopReason) -> Stop:
    return Stop(reason=reason, detail=DETAILS[reason])


class InvestigatorRunner:
    def __init__(
        self, model: Model, pricing: Pricing, telemetry: TelemetryProvider, limits: RunnerLimits
    ) -> None:
        self._model = model
        self._pricing = pricing
        self._telemetry = telemetry
        self._limits = limits

    async def investigate(
        self, connection: Connection, lease: Lease, keeper: LeaseKeeper
    ) -> CandidateOrStop:
        """Run the investigator once, under `lease`."""
        seconds = await _until_reserve(connection, lease)
        if seconds <= 0:
            return _stop(StopReason.FINALIZATION_RESERVE_REACHED)
        briefing = await load_briefing(connection, lease, self._limits.briefing_tokens)
        scope = await load_scope(connection, lease)
        tools = ToolGateway(connection, lease, keeper, self._telemetry, scope)
        agent = Agent[ToolGateway](
            name=AGENT_NAME,
            instructions=INSTRUCTIONS,
            model=GatedModel(
                self._model,
                connection,
                lease,
                keeper,
                self._pricing,
                self._limits.max_output_tokens,
            ),
            tools=list(TOOLS),
            output_type=candidate_output(),
        )
        clock = asyncio.timeout(seconds)
        try:
            async with clock:
                result = await Runner.run(
                    agent,
                    briefing.text,
                    context=tools,
                    max_turns=self._limits.max_turns,
                    run_config=run_config(),
                )
        except TimeoutError:
            if not clock.expired():
                raise
            return _stop(StopReason.FINALIZATION_RESERVE_REACHED)
        except BudgetDenied as denied:
            return _stop(
                StopReason.DEADLINE_REACHED if denied.past_deadline else StopReason.BUDGET_EXHAUSTED
            )
        except Exception as error:
            return _stop(stop_reason(error))
        candidate = result.final_output
        assert isinstance(candidate, ReportCandidate)  # noqa: S101 (the output type is fixed)
        return candidate


async def _until_reserve(connection: Connection, lease: Lease) -> float:
    """Seconds until only the finalization reserve is left before the deadline."""
    async with owner_transaction(connection, lease.owner_id):
        cursor = await connection.execute(_UNTIL_RESERVE, (lease.investigation_id,))
        row = await cursor.fetchone()
    assert row is not None  # noqa: S101 (a lease's investigation always exists)
    return float(row[0])
