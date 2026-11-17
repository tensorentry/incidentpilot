"""A scripted stand-in for the model, so the durable slice runs with no provider (spec
Phase 1, §17.1).

It makes the same durable writes a real investigator will: each step reserves a model
call, commits DISPATCHED before "sending", then settles the call and commits the step's
evidence, all through the lease fence. Only the provider is fake. Faults can be planted
at the points where a real worker can die, so recovery runs against real ledger rows.

When the script ends, it hands in a candidate built from the evidence it can see, then
publishes it (T3.12). A recovery attempt that finds a stored candidate publishes that
instead, without replaying the script (E1). A script stopped by its budget returns with
no candidate: the application's fallback report arrives in T4.8.
"""

import asyncio
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from enum import Enum, auto

from incidentpilot.domain.canonical import sha256_digest
from incidentpilot.domain.enums import (
    ClaimKind,
    DiagnosticOutcome,
    EvidenceKind,
    FailureCategory,
    ReportAuthor,
    TerminationReason,
)
from incidentpilot.domain.evidence import (
    EVIDENCE_SELECTOR_ADAPTER,
    EvidenceDraft,
    EvidenceRecord,
    EvidenceSelector,
)
from incidentpilot.domain.ids import CandidateId
from incidentpilot.domain.report import Impact, Observation, ReportCandidate
from incidentpilot.persistence.database import Connection
from incidentpilot.persistence.evidence import commit_evidence, visible_evidence
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
from incidentpilot.workflows.publication import (
    publish_report,
    resume_validation,
    submit_candidate,
)

log = logging.getLogger(__name__)

# A scripted model has no prompt. This stands in for its digest, so reports can still
# say which "prompt" wrote them.
SCRIPT_PROMPT_SHA256 = sha256_digest("incidentpilot scripted model 1")

type Finish = Callable[[Sequence[EvidenceRecord]], ReportCandidate]


class Fault(Enum):
    """Where a planted fault strikes during a step."""

    # The process dies with the reservation committed but nothing sent.
    CRASH_AFTER_RESERVE = auto()
    # The process dies after DISPATCHED commits, so nobody knows if the request landed.
    CRASH_AFTER_DISPATCH = auto()
    # The provider answers with a definite error. The step fails and the script goes on.
    PROVIDER_ERROR = auto()
    # The process dies after the candidate commits, before publication. Planted at the
    # index just past the last step.
    CRASH_BEFORE_PUBLISH = auto()


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


def cite(record: EvidenceRecord) -> EvidenceSelector:
    """A selector for a whole evidence row."""
    fields: dict[str, object] = {"kind": record.kind, "evidence_id": record.evidence_id}
    if record.window is not None:
        fields["window"] = record.window
    return EVIDENCE_SELECTOR_ADAPTER.validate_python(fields)


def abstain(evidence: Sequence[EvidenceRecord]) -> ReportCandidate:
    """The default ending: one observation per record seen, and no conclusion."""
    return ReportCandidate(
        diagnostic_outcome=DiagnosticOutcome.INSUFFICIENT,
        observations=[
            Observation(
                observation_id=f"obs-{number}",
                claim="The scripted investigator saw this record.",
                claim_kind=ClaimKind.OBSERVED,
                references=[cite(record)],
                measurement=None,
            )
            for number, record in enumerate(evidence, 1)
        ],
        hypotheses=[],
        root_causes=[],
        impact=Impact(affected_services=[], measurements=[]),
        alternatives=[],
        recommendations=[],
        limitations=["A scripted investigator draws no conclusions."],
    )


# The script `incidentpilot worker` plays. Its evidence is written into the script, not
# read from the snapshot: the telemetry adapter that reads snapshots arrives in T4.2.
_DEMO_LOGS = [
    EvidenceDraft(
        kind=EvidenceKind.LOG_RECORD,
        source_ref=f"log/checkout-api/demo-{n}",
        adapter_version="scripted-1",
        window=None,
        content={"service": "checkout-api", "message": message},
    )
    for n, message in enumerate(
        [
            "upstream timeout calling payments-api",
            "retrying payments-api request",
            "payments-api pool exhausted",
        ],
        1,
    )
]
DEMO_SCRIPT = (
    Step(30_000, Usage(1_200, 150, 21_000), tuple(_DEMO_LOGS[:2])),
    Step(30_000, Usage(1_500, 180, 25_500), tuple(_DEMO_LOGS[2:])),
    Step(30_000, Usage(1_800, 400, 33_000)),
)


@dataclass
class ScriptedModel:
    """A worker Job that plays a script. Each planted fault strikes once per instance,
    like a crash that a restarted process doesn't repeat."""

    script: Sequence[Step]
    faults: Mapping[int, Fault] = field(default_factory=dict)
    finish: Finish = abstain
    # How long the fake provider takes to answer, so a demo can catch a call in flight.
    provider_delay: timedelta = timedelta(0)
    _struck: set[int] = field(default_factory=set, init=False)

    async def __call__(self, connection: Connection, lease: Lease, keeper: LeaseKeeper) -> None:
        keeper.require()
        async with fenced_transaction(connection, lease):
            candidate_id = await resume_validation(connection, lease)
        if candidate_id is None:
            candidate_id = await self._investigate(connection, lease, keeper)
            if candidate_id is None:
                return
        self._strike(len(self.script), Fault.CRASH_BEFORE_PUBLISH)
        keeper.require()
        async with fenced_transaction(connection, lease):
            await publish_report(connection, lease, candidate_id)

    async def _investigate(
        self, connection: Connection, lease: Lease, keeper: LeaseKeeper
    ) -> CandidateId | None:
        """Play the script and hand in a candidate, or None if the budget ran out."""
        for index, step in enumerate(self.script):
            keeper.require()
            try:
                await self._play(connection, lease, index, step)
            except BudgetDenied as denied:
                log.info("script stopped on investigation %s: %s", lease.investigation_id, denied)
                return None
        keeper.require()
        async with fenced_transaction(connection, lease):
            evidence = await visible_evidence(connection, lease.investigation_id)
            return await submit_candidate(
                connection,
                lease,
                self.finish(evidence),
                author=ReportAuthor.MODEL,
                reason=TerminationReason.INVESTIGATOR_FINISHED,
                prompt_sha256=SCRIPT_PROMPT_SHA256,
            )

    async def _play(self, connection: Connection, lease: Lease, index: int, step: Step) -> None:
        async with fenced_transaction(connection, lease):
            call_id = await reserve_call(connection, lease, step.estimate_micro_usd)
        self._strike(index, Fault.CRASH_AFTER_RESERVE)
        async with fenced_transaction(connection, lease):
            await dispatch_call(connection, lease, call_id)
        self._strike(index, Fault.CRASH_AFTER_DISPATCH)
        try:
            usage = await self._send(index, step)
        except ProviderError:
            async with fenced_transaction(connection, lease):
                await fail_call(connection, lease, call_id, FailureCategory.PROVIDER_ERROR)
            return
        async with fenced_transaction(connection, lease):
            await settle_call(connection, lease, call_id, usage)
            for draft in step.evidence:
                await commit_evidence(connection, lease, draft)

    async def _send(self, index: int, step: Step) -> Usage:
        """The fake provider: it answers with the step's usage after `provider_delay`."""
        self._strike(index, Fault.PROVIDER_ERROR)
        await asyncio.sleep(self.provider_delay.total_seconds())
        return step.usage

    def _strike(self, index: int, fault: Fault) -> None:
        if self.faults.get(index) is not fault or index in self._struck:
            return
        self._struck.add(index)
        if fault is Fault.PROVIDER_ERROR:
            raise ProviderError
        raise Crash(f"planted {fault.name.lower()} at step {index}")
