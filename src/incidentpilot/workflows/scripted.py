"""A scripted stand-in for the model, so the durable slice runs with no provider (spec
Phase 1, §17.1).

It makes the same durable writes a real investigator will: each step reserves a model
call, commits DISPATCHED before "sending", then settles the call and commits the step's
evidence, all through the lease fence. Only the provider is fake. Faults can be planted
at the points where a real worker can die, so recovery runs against real ledger rows.

Publishing a result is T3.12's. A script that runs out of steps simply returns.
"""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum, auto

from incidentpilot.domain.enums import FailureCategory
from incidentpilot.domain.evidence import EvidenceDraft
from incidentpilot.persistence.database import Connection
from incidentpilot.persistence.evidence import commit_evidence
from incidentpilot.persistence.fence import Lease, fenced_transaction
from incidentpilot.persistence.ledger import (
    BudgetDenied,
    Usage,
    dispatch_call,
    fail_call,
    reserve_call,
    settle_call,
)
from incidentpilot.workflows.lease import LeaseKeeper

log = logging.getLogger(__name__)


class Fault(Enum):
    """Where a planted fault strikes during a step."""

    # The process dies with the reservation committed but nothing sent.
    CRASH_AFTER_RESERVE = auto()
    # The process dies after DISPATCHED commits, so nobody knows if the request landed.
    CRASH_AFTER_DISPATCH = auto()
    # The provider answers with a definite error. The step fails and the script goes on.
    PROVIDER_ERROR = auto()


class Crash(Exception):
    """A planted crash. The worker logs it and the lease lapses, as if the process died."""


class ProviderError(Exception):
    """A planted provider error. The script catches it at once, so it carries no message."""


@dataclass(frozen=True)
class Step:
    """One model request and the evidence its tool calls would have returned."""

    estimate_micro_usd: int
    usage: Usage
    evidence: Sequence[EvidenceDraft] = ()


@dataclass
class ScriptedModel:
    """A worker Job that plays a script. Each planted fault strikes once per instance,
    like a crash that a restarted process doesn't repeat."""

    script: Sequence[Step]
    faults: Mapping[int, Fault] = field(default_factory=dict)
    _struck: set[int] = field(default_factory=set, init=False)

    async def __call__(self, connection: Connection, lease: Lease, keeper: LeaseKeeper) -> None:
        for index, step in enumerate(self.script):
            keeper.require()
            try:
                await self._play(connection, lease, index, step)
            except BudgetDenied as denied:
                log.info("script stopped on investigation %s: %s", lease.investigation_id, denied)
                return

    async def _play(self, connection: Connection, lease: Lease, index: int, step: Step) -> None:
        async with fenced_transaction(connection, lease):
            call_id = await reserve_call(connection, lease, step.estimate_micro_usd)
        self._strike(index, Fault.CRASH_AFTER_RESERVE)
        async with fenced_transaction(connection, lease):
            await dispatch_call(connection, lease, call_id)
        self._strike(index, Fault.CRASH_AFTER_DISPATCH)
        try:
            usage = self._send(index, step)
        except ProviderError:
            async with fenced_transaction(connection, lease):
                await fail_call(connection, lease, call_id, FailureCategory.PROVIDER_ERROR)
            return
        async with fenced_transaction(connection, lease):
            await settle_call(connection, lease, call_id, usage)
            for draft in step.evidence:
                await commit_evidence(connection, lease, draft)

    def _send(self, index: int, step: Step) -> Usage:
        """The fake provider: it answers with the step's usage."""
        self._strike(index, Fault.PROVIDER_ERROR)
        return step.usage

    def _strike(self, index: int, fault: Fault) -> None:
        if self.faults.get(index) is not fault or index in self._struck:
            return
        self._struck.add(index)
        if fault is Fault.PROVIDER_ERROR:
            raise ProviderError
        raise Crash(f"planted {fault.name.lower()} at step {index}")
