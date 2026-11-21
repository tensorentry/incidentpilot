"""The provider call gate against PostgreSQL (spec §11.1, D14; errata E9 and E18): each
request is reserved at a cost bound, committed as DISPATCHED before it is sent, and
settled at the reported usage, or kept as unknown cost."""

import asyncio
import json
from typing import Any

import httpx2
import openai
import pytest
from agents import (
    Agent,
    AgentOutputSchema,
    ModelResponse,
    ModelSettings,
    ModelTracing,
    Runner,
    Usage,
)
from openai.types.responses.response_usage import InputTokensDetails

from incidentpilot.domain.enums import AttemptStatus
from incidentpilot.domain.report import ReportCandidate
from incidentpilot.model_runtime.gate import GatedModel, request_size
from incidentpilot.model_runtime.pricing import Pricing, charge, estimate, input_token_bound
from incidentpilot.model_runtime.sdk import gateway_tool, run_config
from incidentpilot.persistence.database import Connection, owner_transaction
from incidentpilot.persistence.fence import Lease, StaleLease
from incidentpilot.persistence.ledger import BudgetDenied
from incidentpilot.workflows.lease import LeaseKeeper
from incidentpilot.workflows.recovery import close_attempt
from tests.integration.accounting import counters
from tests.integration.harness import OWNER_A, ScratchDatabase
from tests.integration.seeding import claimed, update
from tests.integration.test_gateway import steal
from tests.sdk_compat.fake import FakeModel, Turn, message, tool_call

# Round numbers, not any provider's prices.
PRICING = Pricing(
    version="test-1",
    input_per_million=2_000_000,
    cached_input_per_million=500_000,
    cache_write_per_million=2_500_000,
    output_per_million=8_000_000,
)
CAP = 1_000
USAGE = Usage(requests=1, input_tokens=4_000, output_tokens=500)
COST = charge(PRICING, 4_000, 0, 0, 500)


class Provider(FakeModel):
    """A fake provider that notes what each request carried and the ledger when it came."""

    def __init__(self, turns: list[Turn], usage: Usage = USAGE, probe: Connection | None = None):
        super().__init__(turns, usage)
        self.probe = probe
        self.settings: list[ModelSettings] = []
        self.sizes: list[tuple[int, int]] = []
        self.states_seen: list[list[str]] = []

    async def get_response(self, *args: Any, **kwargs: Any) -> ModelResponse:
        instructions, given, settings, tools, output = args[:5]
        self.settings.append(settings)
        self.sizes.append(request_size(instructions, given, tools, output))
        if self.probe is not None:
            self.states_seen.append(await states(self.probe))
        response = await super().get_response(*args, **kwargs)
        return ModelResponse(
            output=response.output,
            usage=response.usage,
            response_id="resp-1",
            request_id=f"req-{self.requests}",
        )


async def states(connection: Connection) -> list[str]:
    async with owner_transaction(connection, OWNER_A):
        cursor = await connection.execute("SELECT state FROM model_calls ORDER BY created_at")
        return [state for (state,) in await cursor.fetchall()]


async def calls(connection: Connection, lease: Lease) -> list[tuple[Any, ...]]:
    async with owner_transaction(connection, lease.owner_id):
        cursor = await connection.execute(
            "SELECT state, reserved_cost_micro_usd, input_tokens, output_tokens,"
            " actual_cost_micro_usd, provider_request_id, failure_category"
            " FROM model_calls WHERE investigation_id = %s ORDER BY created_at",
            (lease.investigation_id,),
        )
        return list(await cursor.fetchall())


async def echo(service: str) -> str:
    """Repeat a service name."""
    return service


async def ask(model: GatedModel) -> str:
    agent = Agent(name="investigator", model=model, tools=[gateway_tool(echo)])
    result = await Runner.run(agent, "investigate", run_config=run_config())
    return str(result.final_output)


def gated(
    provider: FakeModel, connection: Connection, keeping: Connection, lease: Lease
) -> GatedModel:
    return GatedModel(provider, connection, lease, LeaseKeeper(keeping, lease), PRICING, CAP)


def reserved_for(size: tuple[int, int]) -> int:
    return estimate(PRICING, input_token_bound(*size), CAP)


async def test_each_request_is_reserved_dispatched_and_settled(database: ScratchDatabase) -> None:
    async with (
        database.runtime() as connection,
        database.runtime() as keeping,
        database.runtime() as probe,
    ):
        lease = await claimed(connection)
        provider = Provider(
            [[tool_call("echo", {"service": "a"}, 1)], [message("done")]], probe=probe
        )

        assert await ask(gated(provider, connection, keeping, lease)) == "done"

        first, second = (reserved_for(size) for size in provider.sizes)
        assert first < second  # the second request carries the first turn
        assert await calls(connection, lease) == [
            ("SETTLED", first, 4_000, 500, COST, "req-1", None),
            ("SETTLED", second, 4_000, 500, COST, "req-2", None),
        ]
        assert await counters(connection, lease) == {
            "model_requests_reserved": 2,
            "model_requests_dispatched": 2,
            "model_requests_settled": 2,
            "cost_known_micro_usd": 2 * COST,
            "input_tokens_actual": 8_000,
            "output_tokens_actual": 1_000,
        }
        # Each request was already committed as sent when the provider saw it.
        assert provider.states_seen == [["DISPATCHED"], ["SETTLED", "DISPATCHED"]]


async def test_the_output_cap_is_imposed_on_every_request(database: ScratchDatabase) -> None:
    async with database.runtime() as connection, database.runtime() as keeping:
        lease = await claimed(connection)
        provider = Provider([[message("done")]])

        agent = Agent(
            name="investigator",
            model=gated(provider, connection, keeping, lease),
            model_settings=ModelSettings(max_tokens=50_000, temperature=0.5),
        )
        await Runner.run(agent, "investigate", run_config=run_config())

        (settings,) = provider.settings
        assert (settings.max_tokens, settings.temperature) == (CAP, 0.5)


async def test_cache_reads_and_writes_are_charged_at_their_own_prices(
    database: ScratchDatabase,
) -> None:
    async with database.runtime() as connection, database.runtime() as keeping:
        lease = await claimed(connection)
        usage = Usage(
            requests=1,
            input_tokens=4_000,
            input_tokens_details=InputTokensDetails(cached_tokens=3_000, cache_write_tokens=500),
            output_tokens=500,
        )

        await ask(gated(Provider([[message("done")]], usage), connection, keeping, lease))

        ((*_, cost, _, _),) = await calls(connection, lease)
        assert cost == charge(PRICING, 4_000, 3_000, 500, 500) == 1_000 + 1_500 + 1_250 + 4_000


async def test_an_answer_without_usage_keeps_its_whole_reservation(
    database: ScratchDatabase,
) -> None:
    async with database.runtime() as connection, database.runtime() as keeping:
        lease = await claimed(connection)
        provider = Provider([[message("done")]], Usage(requests=1))

        assert await ask(gated(provider, connection, keeping, lease)) == "done"

        reserved = reserved_for(provider.sizes[0])
        assert await calls(connection, lease) == [
            ("UNKNOWN", reserved, None, None, None, None, None)
        ]
        assert await counters(connection, lease) == {
            "model_requests_reserved": 1,
            "model_requests_dispatched": 1,
            "model_requests_unknown": 1,
            "cost_unknown_micro_usd": reserved,
        }


async def test_a_provider_error_fails_the_call_and_keeps_its_reservation(
    database: ScratchDatabase,
) -> None:
    error = openai.BadRequestError(
        "rejected",
        response=httpx2.Response(400, request=httpx2.Request("POST", "https://provider.test")),
        body=None,
    )
    async with database.runtime() as connection, database.runtime() as keeping:
        lease = await claimed(connection)
        provider = Provider([error])

        with pytest.raises(openai.BadRequestError) as raised:
            await ask(gated(provider, connection, keeping, lease))

        assert raised.value is error
        reserved = reserved_for(provider.sizes[0])
        assert await calls(connection, lease) == [
            ("FAILED", reserved, None, None, None, None, "PROVIDER_ERROR")
        ]
        assert await counters(connection, lease) == {
            "model_requests_reserved": 1,
            "model_requests_dispatched": 1,
            "cost_unknown_micro_usd": reserved,
        }


@pytest.mark.parametrize(
    "error",
    [
        openai.APIConnectionError(request=httpx2.Request("POST", "https://provider.test")),
        openai.APITimeoutError(request=httpx2.Request("POST", "https://provider.test")),
        RuntimeError("client bug"),
    ],
    ids=["connection", "timeout", "anything else"],
)
async def test_an_unanswered_request_is_left_unknown(
    database: ScratchDatabase, error: Exception
) -> None:
    async with database.runtime() as connection, database.runtime() as keeping:
        lease = await claimed(connection)
        provider = Provider([error])

        with pytest.raises(type(error)) as raised:
            await ask(gated(provider, connection, keeping, lease))

        assert raised.value is error
        reserved = reserved_for(provider.sizes[0])
        assert await calls(connection, lease) == [
            ("UNKNOWN", reserved, None, None, None, None, None)
        ]
        assert await counters(connection, lease) == {
            "model_requests_reserved": 1,
            "model_requests_dispatched": 1,
            "model_requests_unknown": 1,
            "cost_unknown_micro_usd": reserved,
        }


async def test_a_spent_budget_sends_nothing(database: ScratchDatabase) -> None:
    async with database.runtime() as connection, database.runtime() as keeping:
        lease = await claimed(connection)
        await update(
            connection,
            OWNER_A,
            "UPDATE investigations SET spend_cap_micro_usd = 1 WHERE id = %s",
            lease.investigation_id,
        )
        provider = Provider([[message("never")]])

        with pytest.raises(BudgetDenied) as raised:
            await ask(gated(provider, connection, keeping, lease))

        assert raised.value.past_deadline is False
        assert provider.requests == 0
        assert await calls(connection, lease) == []
        assert await counters(connection, lease) == {}


async def test_nothing_is_sent_after_the_deadline(database: ScratchDatabase) -> None:
    async with database.runtime() as connection, database.runtime() as keeping:
        lease = await claimed(connection)
        await update(
            connection,
            OWNER_A,
            "UPDATE investigations SET deadline_at = pg_catalog.clock_timestamp()"
            " - interval '1 second' WHERE id = %s",
            lease.investigation_id,
        )
        provider = Provider([[message("never")]])

        with pytest.raises(BudgetDenied) as raised:
            await ask(gated(provider, connection, keeping, lease))

        assert raised.value.past_deadline is True
        assert provider.requests == 0


async def test_a_lost_lease_sends_nothing(database: ScratchDatabase) -> None:
    async with database.runtime() as connection, database.runtime() as keeping:
        lease = await claimed(connection)
        keeper = LeaseKeeper(keeping, lease)
        keeper.lost.set()
        provider = Provider([[message("never")]])

        with pytest.raises(StaleLease):
            await ask(GatedModel(provider, connection, lease, keeper, PRICING, CAP))

        assert provider.requests == 0
        assert await calls(connection, lease) == []


class Stealing(Provider):
    """A provider that answers only after another worker has taken the investigation."""

    def __init__(self, connection: Connection, lease: Lease) -> None:
        super().__init__([[message("too late")]])
        self.connection = connection
        self.lease = lease

    async def get_response(self, *args: Any, **kwargs: Any) -> ModelResponse:
        await steal(self.connection, self.lease)
        return await super().get_response(*args, **kwargs)


async def test_an_answer_after_the_lease_is_lost_is_not_settled(
    database: ScratchDatabase,
) -> None:
    async with (
        database.runtime() as connection,
        database.runtime() as keeping,
        database.runtime() as sweeping,
    ):
        lease = await claimed(connection)
        provider = Stealing(sweeping, lease)

        with pytest.raises(StaleLease):
            await ask(gated(provider, connection, keeping, lease))

        # The sweeper closed the attempt while the request was out: sent, so UNKNOWN.
        reserved = reserved_for(provider.sizes[0])
        assert await calls(connection, lease) == [
            ("UNKNOWN", reserved, None, None, None, None, None)
        ]


async def test_a_cancelled_request_is_left_for_the_attempt_to_close(
    database: ScratchDatabase,
) -> None:
    started = asyncio.Event()

    class Unanswered(Provider):
        async def get_response(self, *args: Any, **kwargs: Any) -> ModelResponse:
            started.set()
            await asyncio.sleep(60)
            raise AssertionError("never answers")

    async with database.runtime() as connection, database.runtime() as keeping:
        lease = await claimed(connection)
        running = asyncio.create_task(ask(gated(Unanswered([]), connection, keeping, lease)))
        await started.wait()
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running

        assert [state for state, *_ in await calls(connection, lease)] == ["DISPATCHED"]
        async with owner_transaction(connection, OWNER_A):
            await close_attempt(connection, lease.attempt_id, AttemptStatus.CANCELLED, None)
        assert [state for state, *_ in await calls(connection, lease)] == ["UNKNOWN"]


class Echoing(FakeModel):
    """A provider that keeps exactly what it was asked with."""

    def __init__(self) -> None:
        super().__init__([[message("done")]], USAGE)
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def get_response(self, *args: Any, **kwargs: Any) -> ModelResponse:
        self.calls.append((args, kwargs))
        return await super().get_response(*args, **kwargs)


async def test_every_argument_reaches_the_provider_and_counts_toward_the_estimate(
    database: ScratchDatabase,
) -> None:
    tool = gateway_tool(echo)
    output = AgentOutputSchema(ReportCandidate)
    handoffs: list[Any] = [object()]
    asked: dict[str, Any] = {
        "previous_response_id": "resp-0",
        "conversation_id": "conv-1",
        "prompt": {"id": "prompt-1"},
    }
    async with database.runtime() as connection, database.runtime() as keeping:
        lease = await claimed(connection)
        provider = Echoing()

        await gated(provider, connection, keeping, lease).get_response(
            "Investigate the incident.",
            [{"role": "user", "content": "briefing"}],
            ModelSettings(temperature=0.25),
            [tool],
            output,
            handoffs,
            ModelTracing.DISABLED,
            **asked,
        )

        ((args, kwargs),) = provider.calls
        instructions, given, settings, tools, schema, passed, tracing = args
        assert (instructions, given, tools, schema, passed, tracing) == (
            "Investigate the incident.",
            [{"role": "user", "content": "briefing"}],
            [tool],
            output,
            handoffs,
            ModelTracing.DISABLED,
        )
        assert kwargs == asked
        assert (settings.temperature, settings.max_tokens) == (0.25, CAP)
        size = request_size(instructions, given, tools, schema)
        ((_, reserved, *_),) = await calls(connection, lease)
        assert reserved == reserved_for(size)
        assert size[0] > request_size(None, given, [], None)[0] + len(
            json.dumps(output.json_schema())
        )


async def test_one_input_token_is_reported_usage(database: ScratchDatabase) -> None:
    async with database.runtime() as connection, database.runtime() as keeping:
        lease = await claimed(connection)
        provider = Provider([[message("done")]], Usage(requests=1, input_tokens=1))

        await ask(gated(provider, connection, keeping, lease))

        ((state, *_),) = await calls(connection, lease)
        assert state == "SETTLED"


def test_an_output_cap_of_one_is_allowed() -> None:
    model = GatedModel(FakeModel([]), None, None, None, PRICING, 1)  # type: ignore[arg-type]

    assert isinstance(model, GatedModel)


def test_the_gate_needs_an_output_cap() -> None:
    with pytest.raises(ValueError, match=r"^a request needs a positive output cap$"):
        GatedModel(FakeModel([]), None, None, None, PRICING, 0)  # type: ignore[arg-type]


async def test_the_gate_never_streams() -> None:
    model = GatedModel(FakeModel([]), None, None, None, PRICING, CAP)  # type: ignore[arg-type]

    with pytest.raises(NotImplementedError, match=r"^the worker never streams$"):
        model.stream_response(
            None,
            "x",
            ModelSettings(),
            [],
            None,
            [],
            ModelTracing.DISABLED,
            previous_response_id=None,
            conversation_id=None,
            prompt=None,
        )
