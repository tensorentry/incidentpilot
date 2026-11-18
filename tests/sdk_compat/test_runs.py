"""§9.3 items 3, 4, 7 and 8: a model adapter can reserve before each request and
settle after it, runs stop on turns and deadlines, nothing is traced, and the settings
match the pinned release.
"""

import asyncio
import tomllib
from importlib import metadata
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx2
import openai
import pytest
from agents import (
    Agent,
    MaxTurnsExceeded,
    ModelBehaviorError,
    ModelRefusalError,
    ModelResponse,
    ModelTimeoutError,
    RunConfig,
    Runner,
    Usage,
    set_trace_processors,
    set_tracing_disabled,
)
from agents.tracing import Span, Trace, TracingProcessor

from incidentpilot.domain.enums import StopReason
from incidentpilot.model_runtime.sdk import (
    SDK_DISTRIBUTION,
    SDK_VERSION,
    gateway_tool,
    model_settings,
    provider_client,
    run_config,
    stop_reason,
)
from incidentpilot.persistence.ledger import BudgetDenied
from tests.sdk_compat.fake import FakeModel, Turn, message, tool_call

ROOT = Path(__file__).resolve().parents[2]


class GatedModel(FakeModel):
    """Writes down what a call gate would do around each request."""

    def __init__(self, turns: list[Turn], usage: Usage | None = None) -> None:
        super().__init__(turns, usage)
        self.ledger: list[tuple[str, int]] = []

    async def get_response(self, *args: Any, **kwargs: Any) -> ModelResponse:
        number = self.requests + 1
        self.ledger.append(("reserved", number))
        try:
            response = await super().get_response(*args, **kwargs)
        except BaseException:
            self.ledger.append(("unknown", number))
            raise
        self.ledger.append(("settled", response.usage.input_tokens))
        return response


async def echo(service: str) -> str:
    return service


def agent(model: FakeModel) -> Agent[None]:
    return Agent(name="investigator", model=model, tools=[gateway_tool(echo)])


async def test_each_request_is_reserved_before_and_settled_after() -> None:
    model = GatedModel(
        [[tool_call("echo", {"service": "a"}, 1)], [message("done")]],
        Usage(requests=1, input_tokens=321, output_tokens=12),
    )

    result = await Runner.run(agent(model), "investigate", run_config=run_config())

    assert model.ledger == [("reserved", 1), ("settled", 321), ("reserved", 2), ("settled", 321)]
    assert (result.context_wrapper.usage.requests, result.context_wrapper.usage.input_tokens) == (
        2,
        642,
    )


async def test_a_failed_request_is_sent_once_and_left_for_the_gate_to_settle() -> None:
    failure = openai.APIConnectionError(request=httpx2.Request("POST", "https://provider.test"))
    model = GatedModel([failure, [message("never sent")]])

    with pytest.raises(openai.APIConnectionError) as raised:
        await Runner.run(agent(model), "investigate", run_config=run_config())

    assert model.ledger == [("reserved", 1), ("unknown", 1)]
    assert stop_reason(raised.value) is StopReason.PROVIDER_FAILURE


async def test_a_denied_budget_reaches_the_caller_unwrapped() -> None:
    denied = BudgetDenied(uuid4(), past_deadline=False)  # type: ignore[arg-type]
    model = GatedModel([denied])

    with pytest.raises(BudgetDenied) as raised:
        await Runner.run(agent(model), "investigate", run_config=run_config())

    assert raised.value is denied
    assert model.requests == 1


async def test_a_cancelled_request_is_left_unknown() -> None:
    started = asyncio.Event()

    class Unanswered(FakeModel):
        async def get_response(self, *args: Any, **kwargs: Any) -> ModelResponse:
            started.set()
            await asyncio.sleep(60)
            raise AssertionError("never answers")

    class GatedUnanswered(GatedModel, Unanswered):
        pass

    model = GatedUnanswered([])
    running = asyncio.create_task(Runner.run(agent(model), "investigate", run_config=run_config()))
    await started.wait()
    running.cancel()

    with pytest.raises(asyncio.CancelledError):
        await running
    assert model.ledger == [("reserved", 1), ("unknown", 1)]


async def test_a_run_stops_at_its_turn_limit() -> None:
    model = FakeModel([[tool_call("echo", {"service": "a"}, n)] for n in range(5)])

    with pytest.raises(MaxTurnsExceeded) as raised:
        await Runner.run(agent(model), "investigate", max_turns=2, run_config=run_config())

    assert model.requests == 2
    assert stop_reason(raised.value) is StopReason.TURN_LIMIT_REACHED


async def test_a_deadline_cancels_the_run_without_another_request() -> None:
    async def stall(service: str) -> str:
        await asyncio.sleep(60)
        return service

    model = FakeModel([[tool_call("stall", {"service": "a"}, 1)], [message("done")]])
    staller = Agent(name="investigator", model=model, tools=[gateway_tool(stall)])

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.05):
            await Runner.run(staller, "investigate", run_config=run_config())
    assert model.requests == 1


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (MaxTurnsExceeded("too many"), StopReason.TURN_LIMIT_REACHED),
        (ModelRefusalError("no"), StopReason.MODEL_REFUSED),
        (ModelTimeoutError(5), StopReason.PROVIDER_FAILURE),
        (ModelBehaviorError("bad json"), StopReason.PROVIDER_FAILURE),
        (
            openai.APIConnectionError(request=httpx2.Request("POST", "https://provider.test")),
            StopReason.PROVIDER_FAILURE,
        ),
    ],
)
def test_each_sdk_ending_has_a_stop_reason(error: Exception, reason: StopReason) -> None:
    assert stop_reason(error) is reason


def test_anything_else_is_raised_unchanged() -> None:
    error = KeyError("bug")

    with pytest.raises(KeyError) as raised:
        stop_reason(error)

    assert raised.value is error


class Spy(TracingProcessor):
    def __init__(self) -> None:
        self.seen: list[str] = []

    def on_trace_start(self, trace: Trace) -> None:
        self.seen.append(trace.name)

    def on_trace_end(self, trace: Trace) -> None:
        pass

    def on_span_start(self, span: Span[Any]) -> None:
        self.seen.append(type(span.span_data).__name__)

    def on_span_end(self, span: Span[Any]) -> None:
        pass

    def shutdown(self) -> None:
        pass

    def force_flush(self) -> None:
        pass


# The spy replaces the SDK's exporter, so even a test that switches tracing on can't
# send a trace anywhere.
SPY = Spy()
set_trace_processors([SPY])


@pytest.fixture
def spy() -> Spy:
    SPY.seen.clear()
    return SPY


async def test_nothing_is_traced(spy: Spy) -> None:
    model = FakeModel([[tool_call("echo", {"service": "a"}, 1)], [message("done")]])

    await Runner.run(agent(model), "investigate", run_config=run_config())

    assert spy.seen == []


async def test_nothing_is_traced_even_without_the_run_setting(spy: Spy) -> None:
    await Runner.run(agent(FakeModel([[message("done")]])), "investigate", run_config=RunConfig())

    assert spy.seen == []


async def test_with_tracing_switched_on_the_spy_would_see_the_run(spy: Spy) -> None:
    set_tracing_disabled(False)
    try:
        await Runner.run(
            agent(FakeModel([[message("done")]])), "investigate", run_config=RunConfig()
        )
    finally:
        set_tracing_disabled(True)

    assert spy.seen[0] == "Agent workflow"


def test_the_sdk_does_not_retry_a_request() -> None:
    settings = run_config().model_settings
    assert settings is not None
    assert settings.retry is not None
    assert settings.retry.max_retries == 0
    assert model_settings() == settings


def test_tool_calls_run_one_at_a_time_and_unknown_tools_end_the_run() -> None:
    config = run_config()

    assert config.tool_execution is not None
    assert config.tool_execution.max_function_tool_concurrency == 1
    assert config.tool_not_found_behavior == "raise_error"
    assert (config.tracing_disabled, config.trace_include_sensitive_data) == (True, False)


def server_errors(requests: list[httpx2.Request]) -> httpx2.AsyncClient:
    def answer(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        # Asks for an immediate retry, so a client that retries does so without a wait.
        return httpx2.Response(
            500, headers={"retry-after-ms": "1"}, json={"error": {"message": "unavailable"}}
        )

    return httpx2.AsyncClient(transport=httpx2.MockTransport(answer))


async def send(client: openai.AsyncOpenAI) -> None:
    with pytest.raises(openai.InternalServerError):
        await client.responses.create(model="test-model", input="investigate")


async def test_the_provider_client_sends_each_request_once() -> None:
    requests: list[httpx2.Request] = []
    client = provider_client(
        "test-key", base_url="https://provider.test/v1", http_client=server_errors(requests)
    )

    await send(client)

    assert [str(request.url) for request in requests] == ["https://provider.test/v1/responses"]
    assert requests[0].headers["authorization"] == "Bearer test-key"


async def test_by_default_the_provider_client_retries_twice() -> None:
    requests: list[httpx2.Request] = []
    client = openai.AsyncOpenAI(
        api_key="test-key", base_url="https://provider.test/v1", http_client=server_errors(requests)
    )

    await send(client)

    assert len(requests) == 3


def test_the_installed_sdk_is_the_pinned_release() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())

    assert metadata.version(SDK_DISTRIBUTION) == SDK_VERSION
    assert f"{SDK_DISTRIBUTION}=={SDK_VERSION}" in project["project"]["dependencies"]
