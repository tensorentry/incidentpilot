"""The tools registered with the investigator (spec §8.1, E16, E20).

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

from incidentpilot.domain.enums import ToolErrorCode
from incidentpilot.domain.telemetry import (
    DependencyQuery,
    DeploymentsQuery,
    LogQuery,
    MetricsQuery,
    Query,
    TraceQuery,
)
from incidentpilot.domain.tools import ToolResponse
from incidentpilot.model_runtime.sdk import gateway_tool
from incidentpilot.tools.gateway import ToolGateway


def _invalid(gateway: ToolGateway, call_id: str, error: ValidationError) -> ToolResponse:
    first = error.errors()[0]
    where = ".".join(str(part) for part in first["loc"])
    return gateway.refused(call_id, ToolErrorCode.INVALID_ARGUMENT, f"{where}: {first['msg']}")


async def _call(
    context: ToolContext[ToolGateway], kind: type[Query], fields: dict[str, Any]
) -> str:
    gateway: ToolGateway = context.context
    call_id = context.tool_call_id
    try:
        # Through JSON, so strict validation reads the model's timestamps as text.
        query = kind.model_validate_json(json.dumps(fields))
    except ValidationError as error:
        return _invalid(gateway, call_id, error).model_dump_json()
    response = await gateway.call(call_id, query)
    return response.model_dump_json()


def _window(start_time: str, end_time: str) -> dict[str, str]:
    return {"start_at": start_time, "end_at": end_time}


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
    return await _call(
        context,
        MetricsQuery,
        {
            "service": service,
            "window": _window(start_time, end_time),
            "metrics": metrics,
            "resolution_seconds": resolution_seconds,
        },
    )


async def search_logs(
    context: ToolContext[ToolGateway],
    service: str,
    start_time: str,
    end_time: str,
    min_severity: str,
    contains: str | None,
    limit: int,
    cursor: str | None,
) -> str:
    """Log records from one service over a UTC window [start_time, end_time), oldest first.

    Args:
        service: A service in the incident's service graph.
        start_time: ISO 8601 UTC start.
        end_time: ISO 8601 UTC end.
        min_severity: The lowest severity to return: debug, info, warn, error or fatal.
        contains: Text the message must contain, ignoring case, or null for any message.
        limit: Records per page, from 1 to 100.
        cursor: The next_cursor of the previous page of this same query, or null.
    """
    return await _call(
        context,
        LogQuery,
        {
            "service": service,
            "window": _window(start_time, end_time),
            "min_severity": min_severity,
            "contains": contains,
            "limit": limit,
            "cursor": cursor,
        },
    )


async def get_deployments(
    context: ToolContext[ToolGateway],
    service: str,
    start_time: str,
    end_time: str,
    limit: int,
    cursor: str | None,
) -> str:
    """Deployments of one service over a UTC window [start_time, end_time), oldest first.

    Args:
        service: A service in the incident's service graph.
        start_time: ISO 8601 UTC start.
        end_time: ISO 8601 UTC end.
        limit: Records per page, from 1 to 100.
        cursor: The next_cursor of the previous page of this same query, or null.
    """
    return await _call(
        context,
        DeploymentsQuery,
        {
            "service": service,
            "window": _window(start_time, end_time),
            "limit": limit,
            "cursor": cursor,
        },
    )


async def get_dependency_health(
    context: ToolContext[ToolGateway],
    service: str,
    dependency: str,
    start_time: str,
    end_time: str,
    metrics: list[str],
    resolution_seconds: int,
) -> str:
    """Metrics a service recorded for its calls to one dependency, over [start_time, end_time).

    Args:
        service: The calling service.
        dependency: A service it calls, per the incident's service graph.
        start_time: ISO 8601 UTC start, on the resolution grid.
        end_time: ISO 8601 UTC end, on the resolution grid.
        metrics: Metric names, such as request_count or request_latency.
        resolution_seconds: Seconds between points. The fixtures record every 60.
    """
    return await _call(
        context,
        DependencyQuery,
        {
            "service": service,
            "dependency": dependency,
            "window": _window(start_time, end_time),
            "metrics": metrics,
            "resolution_seconds": resolution_seconds,
        },
    )


async def get_trace_summary(
    context: ToolContext[ToolGateway],
    service: str,
    start_time: str,
    end_time: str,
    limit: int,
    cursor: str | None,
) -> str:
    """Span timing summaries for one service's operations that overlap [start_time, end_time).

    Each summary splits an operation's latency into time spent in the service itself and
    in each downstream call.

    Args:
        service: A service in the incident's service graph.
        start_time: ISO 8601 UTC start.
        end_time: ISO 8601 UTC end.
        limit: Summaries per page, from 1 to 100.
        cursor: The next_cursor of the previous page of this same query, or null.
    """
    return await _call(
        context,
        TraceQuery,
        {
            "service": service,
            "window": _window(start_time, end_time),
            "limit": limit,
            "cursor": cursor,
        },
    )


TOOLS: tuple[FunctionTool, ...] = (
    gateway_tool(get_service_metrics),
    gateway_tool(search_logs),
    gateway_tool(get_deployments),
    gateway_tool(get_dependency_health),
    gateway_tool(get_trace_summary),
)
