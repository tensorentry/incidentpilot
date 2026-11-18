"""The pinned agent SDK and the settings this project relies on (spec §9.3).

The SDK's defaults suit a chat app, not a fenced worker. Each setting below departs
from a default that tests/sdk_compat shows against the pinned release:

- Tool calls from one response run one at a time. The SDK starts them all at once,
  but a worker's tools share its one database connection.
- A tool's exception ends the run. The SDK turns it into text for the model by
  default, which would hide a lost lease behind a message the model reads.
- The SDK doesn't retry a model request, and neither does the provider client. Every
  request has to pass the call gate, which reserves budget before each one.
- No trace leaves the process. SDK tracing is on by default.

Where a default already suits, such as strict schemas and a run that stops on an
unknown tool, the tests pin the default instead. An upgrade that changes any of this
fails tests/sdk_compat instead of changing what the worker does.
"""

import functools
from collections.abc import Awaitable, Callable

import httpx2
import openai
from agents import (
    AgentOutputSchema,
    AgentsException,
    FunctionTool,
    MaxTurnsExceeded,
    ModelBehaviorError,
    ModelRefusalError,
    ModelRetrySettings,
    ModelSettings,
    ModelTimeoutError,
    RunConfig,
    ToolExecutionConfig,
    function_tool,
    set_tracing_disabled,
)

from incidentpilot.domain.enums import StopReason
from incidentpilot.domain.report import ReportCandidate

SDK_DISTRIBUTION = "openai-agents"
SDK_VERSION = "0.24.0"

# Tracing has a process-wide switch as well as the per-run one. Both are off.
set_tracing_disabled(True)


def model_settings() -> ModelSettings:
    return ModelSettings(retry=ModelRetrySettings(max_retries=0))


def run_config() -> RunConfig:
    return RunConfig(
        model_settings=model_settings(),
        tracing_disabled=True,
        trace_include_sensitive_data=False,
        tool_execution=ToolExecutionConfig(max_function_tool_concurrency=1),
    )


def candidate_output() -> AgentOutputSchema:
    """The model's final output: a report candidate, under strict structured output."""
    return AgentOutputSchema(ReportCandidate)


class ToolFailed(AgentsException):
    """A tool raised. The SDK passes its own exceptions through unwrapped."""

    def __init__(self, failure: Exception) -> None:
        super().__init__(f"a tool failed: {failure}")
        self.failure = failure


def gateway_tool[**P, R](function: Callable[P, Awaitable[R]]) -> FunctionTool:
    """A tool whose exceptions end the run instead of reaching the model as text."""

    @functools.wraps(function)
    async def run(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return await function(*args, **kwargs)
        except Exception as failure:
            raise ToolFailed(failure) from failure

    return function_tool(run, failure_error_function=None)


def provider_client(
    api_key: str, *, base_url: str | None = None, http_client: httpx2.AsyncClient | None = None
) -> openai.AsyncOpenAI:
    """The provider client, which sends each request once."""
    return openai.AsyncOpenAI(
        api_key=api_key, base_url=base_url, http_client=http_client, max_retries=0
    )


def stop_reason(error: Exception) -> StopReason:
    """Why a run that raised `error` ended without a candidate (§9.3 item 4).

    A tool's own exception, such as a lost lease, is raised again as itself: it isn't
    a reason to stop, it's the worker's failure. Anything else the runtime doesn't
    recognise is raised unchanged.
    """
    match error:
        case ToolFailed():
            raise error.failure
        case MaxTurnsExceeded():
            return StopReason.TURN_LIMIT_REACHED
        case ModelRefusalError():
            return StopReason.MODEL_REFUSED
        case ModelTimeoutError() | ModelBehaviorError() | openai.APIError():
            return StopReason.PROVIDER_FAILURE
    raise error
