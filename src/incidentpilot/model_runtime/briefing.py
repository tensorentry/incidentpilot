"""The bounded briefing the model reads (spec §5.3 D07, §6.3, §9.1, §8.5 D12).

A briefing is built only from durable state: the frozen incident and scope, what the
budget has left, and the committed evidence the investigation may see. A recovery
attempt therefore rebuilds the same briefing instead of trusting any SDK state.

Evidence appears exactly as committed, with the selector that cites it. The briefing
computes nothing from it, since a derived number would be a claim nothing stored.
When everything doesn't fit the token budget, whole records are left out: lower
priority kinds first, and within a kind the oldest first. The briefing then says how
many of each kind it left out, so the model knows it isn't seeing everything.
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from incidentpilot.domain.enums import EvidenceKind
from incidentpilot.domain.evidence import EvidenceRecord, whole_selector
from incidentpilot.domain.ids import EvidenceId
from incidentpilot.domain.incident import IncidentRecord
from incidentpilot.domain.snapshot import ServiceGraph
from incidentpilot.domain.window import TimeWindow
from incidentpilot.persistence.database import Connection, owner_transaction
from incidentpilot.persistence.evidence import visible_evidence
from incidentpilot.persistence.fence import Lease
from incidentpilot.persistence.incidents import get_incident
from incidentpilot.tools.gateway import load_scope

_REMAINING = """
SELECT incident_id,
       max_tool_calls - tool_calls_reserved,
       max_model_requests - model_requests_reserved,
       GREATEST(0, floor(extract(epoch FROM deadline_at - pg_catalog.clock_timestamp())))::integer
FROM investigations WHERE id = %s
"""
_PER_TOOL = (
    "SELECT tool_name, cap - reserved FROM investigation_tool_quota WHERE investigation_id = %s"
)

# Rare, decisive kinds first; the most numerous last.
PRIORITY = (
    EvidenceKind.DEPLOYMENT_RECORD,
    EvidenceKind.SPAN_SUMMARY,
    EvidenceKind.METRIC_SERIES_WINDOW,
    EvidenceKind.LOG_RECORD,
)


class BriefingTooLarge(Exception):
    """The incident, scope and budget alone exceed the token budget."""


@dataclass(frozen=True)
class Remaining:
    """What the investigation may still spend."""

    tool_calls: int
    calls_per_tool: Mapping[str, int]
    model_requests: int
    seconds: int


@dataclass(frozen=True)
class Briefing:
    text: str
    shown: tuple[EvidenceId, ...]
    omitted: Mapping[EvidenceKind, int]


def _text(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"))


def _size(value: Any) -> int:
    """At most how many tokens `value` is. The text is ASCII, so its length is its bytes,
    and a byte-level BPE token holds at least one byte."""
    return len(_text(value))


def _window(window: TimeWindow) -> dict[str, str]:
    return {"start_at": window.start_at.isoformat(), "end_at": window.end_at.isoformat()}


def _record(record: EvidenceRecord) -> dict[str, Any]:
    cite = whole_selector(record, record.evidence_id).model_dump(mode="json")
    return {"cite": cite, "source": record.source_ref, "content": record.content}


def build_briefing(
    incident: IncidentRecord,
    services: Sequence[str],
    graph: ServiceGraph,
    window: TimeWindow,
    remaining: Remaining,
    evidence: Sequence[EvidenceRecord],
    budget_tokens: int,
) -> Briefing:
    """The briefing for `evidence`, in at most `budget_tokens` tokens.

    `evidence` is in commit order, which the briefing keeps for what it shows.
    """
    header: dict[str, Any] = {
        "incident": {
            "service": incident.service,
            "severity": incident.severity.value,
            "window": _window(incident.window),
            "description": incident.description,
        },
        "scope": {
            "services": sorted(services),
            "dependencies": [[edge.caller, edge.callee] for edge in graph.dependencies],
            "window": _window(window),
        },
        "remaining": {
            "tool_calls": remaining.tool_calls,
            "calls_per_tool": dict(sorted(remaining.calls_per_tool.items())),
            "model_requests": remaining.model_requests,
            "seconds": remaining.seconds,
        },
        "evidence": [],
        # Room for any count: the size check below uses the most there could be.
        "omitted": {kind.value: len(evidence) for kind in PRIORITY},
    }
    used = _size(header)
    if used > budget_tokens:
        raise BriefingTooLarge(f"the briefing needs {used} tokens before any evidence")
    rendered = [_record(record) for record in evidence]
    order = sorted(
        range(len(evidence)),
        key=lambda i: (PRIORITY.index(evidence[i].kind), -i),
    )
    chosen: set[int] = set()
    for i in order:
        # Each record after the first also needs a comma.
        cost = _size(rendered[i]) + (1 if chosen else 0)
        if used + cost <= budget_tokens:
            chosen.add(i)
            used += cost
    shown = sorted(chosen)
    omitted = dict.fromkeys(PRIORITY, 0)
    for i in set(range(len(evidence))) - chosen:
        omitted[evidence[i].kind] += 1
    header["evidence"] = [rendered[i] for i in shown]
    header["omitted"] = {kind.value: count for kind, count in omitted.items() if count}
    return Briefing(
        text=_text(header),
        shown=tuple(evidence[i].evidence_id for i in shown),
        omitted={kind: count for kind, count in omitted.items() if count},
    )


async def load_briefing(connection: Connection, lease: Lease, budget_tokens: int) -> Briefing:
    """The briefing for the leased investigation, read from what has committed."""
    scope = await load_scope(connection, lease)
    async with owner_transaction(connection, lease.owner_id):
        cursor = await connection.execute(_REMAINING, (lease.investigation_id,))
        row = await cursor.fetchone()
        assert row is not None  # noqa: S101 (a lease's investigation always exists)
        incident_id, tool_calls, model_requests, seconds = row
        cursor = await connection.execute(_PER_TOOL, (lease.investigation_id,))
        per_tool: dict[str, int] = dict(await cursor.fetchall())
        incident = await get_incident(connection, incident_id)
        assert incident is not None  # noqa: S101 (an investigation's incident always exists)
        evidence = await visible_evidence(connection, lease.investigation_id)
    remaining = Remaining(tool_calls, per_tool, model_requests, seconds)
    return build_briefing(
        incident,
        sorted(scope.services),
        scope.graph,
        scope.window,
        remaining,
        evidence,
        budget_tokens,
    )
