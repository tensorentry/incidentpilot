"""The tools registered with the investigator (spec §8.1, E16).

Each tool takes only model-writable arguments. The trusted context, meaning who the
call is for and under which lease, comes from the gateway the run carries. A tool turns
its arguments into a query; arguments that don't make a valid query are refused before
anything is reserved.
"""

import json
from typing import Any

from agents import FunctionTool
from agents.tool_context import ToolContext
from pydantic import ValidationError

from incidentpilot.domain.enums import ToolErrorCode, ToolName
from incidentpilot.domain.telemetry import MetricsQuery
from incidentpilot.domain.tools import ToolResponse
from incidentpilot.model_runtime.sdk import gateway_tool
from incidentpilot.tools.gateway import ToolGateway


def _invalid(gateway: ToolGateway, call_id: str, error: ValidationError) -> ToolResponse:
    first = error.errors()[0]
    where = ".".join(str(part) for part in first["loc"])
    return gateway.refused(call_id, ToolErrorCode.INVALID_ARGUMENT, f"{where}: {first['msg']}")


def _query[Q: MetricsQuery](kind: type[Q], fields: dict[str, Any]) -> Q:
    # Through JSON, so strict validation reads the model's timestamps as text.
    return kind.model_validate_json(json.dumps(fields))


async def get_service_metrics(
    context: ToolContext[ToolGateway],
    service: str,
    start_time: str,
    end_time: str,
    metrics: list[str],
    resolution_seconds: int,
) -> str:
    """Metric series for one service over a UTC window [start_time, end_time).

    Args:
        service: A service in the incident's service graph.
        start_time: ISO 8601 UTC start, on the resolution grid.
        end_time: ISO 8601 UTC end, on the resolution grid.
        metrics: Metric names, such as request_count, error_count or request_latency.
        resolution_seconds: Seconds between points. The fixtures record every 60.
    """
    gateway: ToolGateway = context.context
    call_id = context.tool_call_id
    try:
        query = _query(
            MetricsQuery,
            {
                "service": service,
                "window": {"start_at": start_time, "end_at": end_time},
                "metrics": metrics,
                "resolution_seconds": resolution_seconds,
            },
        )
    except ValidationError as error:
        return _invalid(gateway, call_id, error).model_dump_json()
    response = await gateway.call(
        ToolName.GET_SERVICE_METRICS,
        call_id,
        query,
        (query.service,),
        query.window,
        lambda telemetry: telemetry.get_metrics(query),
    )
    return response.model_dump_json()


TOOLS: tuple[FunctionTool, ...] = (gateway_tool(get_service_metrics),)
