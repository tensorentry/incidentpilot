"""§9.3 items 2, 5 and 6: tool calls go through the gateway one at a time, the model
can't write the trusted context, and a tool's failure ends the run.

Each test of a setting has a control that runs the SDK's default, so a pass means the
setting does the work, not that the default already behaved.
"""

import asyncio
from dataclasses import dataclass, field

import pytest
from agents import (
    Agent,
    FunctionTool,
    ModelBehaviorError,
    RunConfig,
    RunContextWrapper,
    Runner,
    function_tool,
)

from incidentpilot.domain.enums import StopReason
from incidentpilot.model_runtime.sdk import ToolFailed, gateway_tool, run_config, stop_reason
from incidentpilot.persistence.fence import StaleLease
from tests.sdk_compat.fake import FakeModel, message, tool_call


@dataclass
class Desk:
    """The application's side of a run: who it runs for, and the tool quota."""

    owner: str
    quota: int = 2
    served: list[tuple[str, str]] = field(default_factory=list)
    refused: int = 0
    running: int = 0
    most_at_once: int = 0


async def lookup(context: RunContextWrapper[Desk], service: str) -> str:
    """Look up a service. The quota check and the count straddle an await, as they will
    around a database round trip."""
    desk = context.context
    desk.running += 1
    desk.most_at_once = max(desk.most_at_once, desk.running)
    try:
        if len(desk.served) >= desk.quota:
            desk.refused += 1
            return "quota_exceeded"
        await asyncio.sleep(0.01)
        desk.served.append((desk.owner, service))
        return f"looked up {service}"
    finally:
        desk.running -= 1


async def lost(context: RunContextWrapper[Desk], service: str) -> str:
    raise StaleLease(f"lost the lease while looking up {service}")


def three_lookups() -> FakeModel:
    return FakeModel(
        [
            [tool_call("lookup", {"service": name}, n) for n, name in enumerate("abc")],
            [message("done")],
        ]
    )


async def run(tool: FunctionTool, model: FakeModel, desk: Desk, config: RunConfig) -> None:
    agent = Agent[Desk](name="investigator", model=model, tools=[tool])
    await Runner.run(agent, "investigate", context=desk, run_config=config)


async def test_every_call_in_one_response_goes_through_the_gateway_in_turn() -> None:
    desk = Desk("owner-a")
    model = three_lookups()

    await run(gateway_tool(lookup), model, desk, run_config())

    assert desk.served == [("owner-a", "a"), ("owner-a", "b")]
    assert (desk.refused, desk.most_at_once, model.requests) == (1, 1, 2)


async def test_by_default_the_calls_run_at_once_and_overdraw_the_quota() -> None:
    desk = Desk("owner-a")

    await run(gateway_tool(lookup), three_lookups(), desk, RunConfig(tracing_disabled=True))

    assert (len(desk.served), desk.refused, desk.most_at_once) == (3, 0, 3)


async def test_the_context_is_not_an_argument_the_model_sees() -> None:
    model = FakeModel([[message("done")]])

    await run(gateway_tool(lookup), model, Desk("owner-a"), run_config())

    assert model.tool_schemas == [
        {
            "type": "object",
            "title": "lookup_args",
            "properties": {"service": {"type": "string", "title": "Service"}},
            "required": ["service"],
            "additionalProperties": False,
        }
    ]


async def test_an_argument_the_model_adds_does_not_reach_the_tool() -> None:
    desk = Desk("owner-a")
    model = FakeModel(
        [[tool_call("lookup", {"service": "a", "owner": "owner-b"}, 1)], [message("done")]]
    )

    await run(gateway_tool(lookup), model, desk, run_config())

    assert desk.served == [("owner-a", "a")]


async def test_a_failing_tool_ends_the_run_with_its_own_exception() -> None:
    model = FakeModel([[tool_call("lost", {"service": "a"}, 1)], [message("done")]])

    with pytest.raises(ToolFailed) as raised:
        await run(gateway_tool(lost), model, Desk("owner-a"), run_config())

    assert model.requests == 1
    assert str(raised.value) == "a tool failed: lost the lease while looking up a"
    with pytest.raises(StaleLease, match=r"^lost the lease while looking up a$"):
        stop_reason(raised.value)


async def test_by_default_a_failing_tool_becomes_text_for_the_model() -> None:
    model = FakeModel([[tool_call("lost", {"service": "a"}, 1)], [message("done")]])

    await run(function_tool(lost), model, Desk("owner-a"), run_config())

    assert model.requests == 2


async def test_cancelling_a_run_reaches_the_running_tool() -> None:
    started = asyncio.Event()
    stopped: list[BaseException] = []

    async def slow(context: RunContextWrapper[Desk], service: str) -> str:
        started.set()
        try:
            await asyncio.sleep(60)
        except BaseException as stop:
            stopped.append(stop)
            raise
        return service

    model = FakeModel([[tool_call("slow", {"service": "a"}, 1)], [message("done")]])
    running = asyncio.create_task(run(gateway_tool(slow), model, Desk("owner-a"), run_config()))
    await started.wait()
    running.cancel()

    with pytest.raises(asyncio.CancelledError):
        await running
    assert [type(stop) for stop in stopped] == [asyncio.CancelledError]
    assert model.requests == 1


async def test_a_call_to_an_unknown_tool_is_a_provider_failure() -> None:
    model = FakeModel([[tool_call("delete_database", {}, 1)], [message("done")]])

    with pytest.raises(ModelBehaviorError) as raised:
        await run(gateway_tool(lookup), model, Desk("owner-a"), run_config())

    assert model.requests == 1
    assert stop_reason(raised.value) is StopReason.PROVIDER_FAILURE
