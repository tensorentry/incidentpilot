"""Stored investigations. Run inside owner_transaction."""

from typing import Literal

from incidentpilot.domain.enums import InvestigationStatus, RunMode, ToolName
from incidentpilot.domain.ids import IncidentId, InvestigationId
from incidentpilot.domain.investigation import (
    FrozenConfig,
    InvestigationLimits,
    InvestigationRecord,
)
from incidentpilot.domain.window import TimeWindow
from incidentpilot.persistence.database import Connection

_COLUMNS = """
    id, incident_id, parent_investigation_id, status, mode, model_profile, snapshot_id,
    window_start, window_end, allowed_services, max_tool_calls, max_calls_per_tool,
    max_model_requests, max_recovery_attempts, deadline_seconds, finalization_reserve_seconds,
    spend_cap_micro_usd, config_sha256, current_attempt_id, lease_epoch, next_event_seq,
    created_at
"""
_SELECT = f"SELECT {_COLUMNS} FROM investigations WHERE id = %s"  # noqa: S608 (fixed text)


# The table's CHECK allows only these, since replay never creates an investigation (E8).
_MODES: dict[str, Literal[RunMode.OFFLINE_DEMO, RunMode.LIVE]] = {
    RunMode.OFFLINE_DEMO: RunMode.OFFLINE_DEMO,
    RunMode.LIVE: RunMode.LIVE,
}


class InvestigationIntegrityError(Exception):
    """A stored investigation's configuration no longer matches its digest."""


async def insert_investigation(
    connection: Connection,
    investigation_id: InvestigationId,
    incident_id: IncidentId,
    config: FrozenConfig,
) -> None:
    limits = config.limits
    await connection.execute(
        """
        INSERT INTO investigations (
            owner_id, id, incident_id, snapshot_id, mode, model_profile, window_start,
            window_end, allowed_services, max_tool_calls, max_calls_per_tool,
            max_model_requests, max_recovery_attempts, deadline_seconds,
            finalization_reserve_seconds, spend_cap_micro_usd, config_sha256)
        VALUES (current_owner_id(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s)
        """,
        (
            investigation_id,
            incident_id,
            config.snapshot_id,
            config.mode,
            config.model_profile,
            config.window.start_at,
            config.window.end_at,
            config.allowed_services,
            limits.tool_calls,
            limits.calls_per_tool,
            limits.model_requests,
            limits.recovery_attempts,
            limits.deadline_seconds,
            limits.finalization_reserve_seconds,
            limits.spend_cap_micro_usd,
            config.digest,
        ),
    )
    # E13: every tool's quota starts with the investigation.
    await connection.execute(
        """
        INSERT INTO investigation_tool_quota (owner_id, investigation_id, tool_name, cap)
        SELECT current_owner_id(), %s, tool, %s FROM unnest(%s::text[]) AS tool
        """,
        (investigation_id, limits.calls_per_tool, [tool.value for tool in ToolName]),
    )


async def get_investigation(
    connection: Connection, investigation_id: InvestigationId
) -> InvestigationRecord | None:
    row = await (await connection.execute(_SELECT, (investigation_id,))).fetchone()
    if row is None:
        return None
    (
        id_,
        incident_id,
        parent_id,
        status,
        mode,
        model_profile,
        snapshot_id,
        start_at,
        end_at,
        services,
        tool_calls,
        calls_per_tool,
        model_requests,
        recovery_attempts,
        deadline,
        reserve,
        spend_cap,
        config_sha256,
        attempt_id,
        epoch,
        next_seq,
        created_at,
    ) = row
    config = FrozenConfig(
        mode=_MODES[mode],
        model_profile=model_profile,
        snapshot_id=snapshot_id,
        window=TimeWindow(start_at=start_at, end_at=end_at),
        allowed_services=services,
        limits=InvestigationLimits(
            tool_calls=tool_calls,
            calls_per_tool=calls_per_tool,
            model_requests=model_requests,
            recovery_attempts=recovery_attempts,
            deadline_seconds=deadline,
            finalization_reserve_seconds=reserve,
            spend_cap_micro_usd=spend_cap,
        ),
    )
    if config.digest != config_sha256:
        raise InvestigationIntegrityError(
            f"investigation {id_} no longer matches the configuration it was admitted with"
        )
    return InvestigationRecord(
        id=InvestigationId(id_),
        incident_id=IncidentId(incident_id),
        # Validated as UUID or None by the strict model.
        parent_investigation_id=parent_id,
        status=InvestigationStatus(status),
        config=config,
        current_attempt_id=attempt_id,
        lease_epoch=epoch,
        next_event_seq=next_seq,
        created_at=created_at,
    )
