"""The `incidentpilot` command (erratum E10).

It calls the application services directly until the HTTP API exists (IP3-010), then
moves to HTTP. Every command acts for one owner, the local owner unless `--owner` names
another, and prints JSON.
"""

import argparse
import asyncio
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import BaseModel, ValidationError

from incidentpilot.application.errors import NotFound, RequestError
from incidentpilot.application.incidents import create_incident
from incidentpilot.application.investigations import start_investigation
from incidentpilot.application.snapshots import OBSERVATIONS, import_directory
from incidentpilot.domain.enums import IncidentSeverity, RunMode
from incidentpilot.domain.ids import IncidentId, InvestigationId, OwnerId
from incidentpilot.domain.incident import CreateIncident
from incidentpilot.domain.investigation import StartInvestigation
from incidentpilot.persistence.database import (
    Connection,
    connect_runtime,
    owner_transaction,
    required,
)
from incidentpilot.persistence.events import list_events
from incidentpilot.persistence.investigations import get_investigation
from incidentpilot.persistence.isolation import LOCAL_OWNER_ID
from incidentpilot.persistence.snapshots import SnapshotError
from incidentpilot.settings import ConfigurationError, Settings, load_settings

PROFILE = "single-investigator-default"

type Output = dict[str, object]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="incidentpilot", description=__doc__)
    parser.add_argument("--owner", type=UUID, default=LOCAL_OWNER_ID)
    commands = parser.add_subparsers(dest="command", required=True)

    imports = commands.add_parser("import-snapshots", help="register observation files")
    imports.add_argument("--root", type=Path, default=OBSERVATIONS)

    incident = commands.add_parser("create-incident", help="report an incident")
    incident.add_argument("--service", required=True)
    incident.add_argument("--severity", required=True, choices=[s.value for s in IncidentSeverity])
    incident.add_argument("--start", required=True, help="UTC start, such as 2026-11-04T18:15:00Z")
    incident.add_argument("--end", required=True)
    incident.add_argument("--description", required=True)
    incident.add_argument("--key", help="idempotency key; reuse it to retry safely")

    start = commands.add_parser("start-investigation", help="queue an investigation")
    start.add_argument("incident_id", type=UUID)
    start.add_argument("--snapshot", required=True)
    start.add_argument("--start", required=True)
    start.add_argument("--end", required=True)
    start.add_argument("--mode", choices=[m.value for m in RunMode], help="default: this process's")
    start.add_argument("--key", help="idempotency key; reuse it to retry safely")

    show = commands.add_parser("show-investigation", help="state, configuration and events")
    show.add_argument("investigation_id", type=UUID)
    return parser


def parse[T: BaseModel](model: type[T], fields: Mapping[str, object]) -> T:
    """Validate through JSON, as the API will, without echoing the input in errors."""
    try:
        return model.model_validate_json(json.dumps(fields))
    except ValidationError as error:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}"
            for item in error.errors()
        )
    raise RequestError(f"invalid request: {problems}")


def window(arguments: argparse.Namespace) -> dict[str, object]:
    return {"start_at": arguments.start, "end_at": arguments.end}


async def execute(
    connection: Connection, settings: Settings, arguments: argparse.Namespace
) -> Output:
    owner = OwnerId(arguments.owner)
    key = getattr(arguments, "key", None) or str(uuid4())
    match arguments.command:
        case "import-snapshots":
            imported = await import_directory(connection, owner, arguments.root)
            return {"snapshots": [{"snapshot_id": s, "created": new} for s, new in imported]}
        case "create-incident":
            request = parse(
                CreateIncident,
                {
                    "description": arguments.description,
                    "service": arguments.service,
                    "severity": arguments.severity,
                    "window": window(arguments),
                },
            )
            created = await create_incident(connection, owner, key, request)
            return {
                "idempotency_key": key,
                "created": created.created,
                "incident": created.incident.model_dump(mode="json"),
            }
        case "start-investigation":
            started_request = parse(
                StartInvestigation,
                {
                    "mode": arguments.mode or settings.mode.value,
                    "snapshot_id": arguments.snapshot,
                    "time_window": window(arguments),
                    "model_profile": PROFILE,
                },
            )
            started = await start_investigation(
                connection,
                owner,
                settings.mode,
                IncidentId(arguments.incident_id),
                key,
                started_request,
            )
            return {
                "idempotency_key": key,
                "created": started.created,
                "investigation": started.investigation.model_dump(mode="json"),
            }
        case _:
            return await show(connection, owner, InvestigationId(arguments.investigation_id))


async def show(connection: Connection, owner: OwnerId, investigation_id: InvestigationId) -> Output:
    async with owner_transaction(connection, owner):
        investigation = await get_investigation(connection, investigation_id)
        if investigation is None:
            raise NotFound(f"investigation {investigation_id} doesn't exist")
        events = await list_events(connection, investigation_id)
    return {
        "investigation": investigation.model_dump(mode="json"),
        "events": [
            {"seq": seq, "type": kind, "payload": payload, "at": str(at)}
            for seq, kind, payload, at in events
        ],
    }


async def run(settings: Settings, arguments: argparse.Namespace) -> Output:
    connection = await connect_runtime(
        required(settings.database_url, "INCIDENTPILOT_DATABASE_URL")
    )
    async with connection:
        return await execute(connection, settings, arguments)


def main(argv: Sequence[str] | None = None) -> None:
    arguments = build_parser().parse_args(argv)
    try:
        output = asyncio.run(run(load_settings(), arguments))
    except (ConfigurationError, RequestError, SnapshotError) as error:
        raise SystemExit(f"incidentpilot: {error}") from None
    print(json.dumps(output, indent=2, sort_keys=True))
