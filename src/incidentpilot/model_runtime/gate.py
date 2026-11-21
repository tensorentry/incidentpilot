"""The provider call gate: every model request passes through here (spec §11.1, D14;
errata E9 and E18).

For each request, in order:
1. Its cost is bounded: every input token uncached, and the whole output cap, which
   the gate also imposes on the request.
2. One request and that cost are reserved against the investigation, behind the lease
   fence. A spent budget or a passed deadline raises `BudgetDenied` before anything is
   sent.
3. DISPATCHED commits, behind the fence, before the provider is asked.
4. The answer settles the call at the cost of the usage the provider reported. An
   answer without usage keeps the whole reservation as unknown cost rather than
   settling at zero.
5. A provider that answered with an error leaves the call FAILED. Any other failure
   leaves it UNKNOWN, since the request may have been received. Either way the call
   keeps its reservation (E18) and the error is raised again.

Cancellation writes nothing: the attempt then closes and leaves the sent call UNKNOWN
(E9). The gate never retries; the client it wraps must not either.
"""

import json
from collections.abc import AsyncIterator
from typing import Any

import openai
from agents import (
    AgentOutputSchemaBase,
    FunctionTool,
    Handoff,
    Model,
    ModelResponse,
    ModelSettings,
    ModelTracing,
    Tool,
    TResponseInputItem,
)
from openai.types.responses import ResponsePromptParam

from incidentpilot.domain.enums import FailureCategory
from incidentpilot.model_runtime.pricing import Pricing, charge, estimate, input_token_bound
from incidentpilot.persistence.database import Connection
from incidentpilot.persistence.fence import Lease, fenced_transaction
from incidentpilot.persistence.ledger import (
    Usage,
    dispatch_call,
    fail_call,
    mark_call_unknown,
    reserve_call,
    settle_call,
)
from incidentpilot.workflows.lease import LeaseKeeper


def request_size(
    system_instructions: str | None,
    input: str | list[TResponseInputItem],
    tools: list[Tool],
    output_schema: AgentOutputSchemaBase | None,
) -> tuple[int, int]:
    """The request's bytes as ASCII JSON, and how many input items it holds.

    Escaping non-ASCII text makes it longer than its UTF-8 bytes, so the bound holds.
    """
    items = [input] if isinstance(input, str) else input
    parts = [
        system_instructions,
        items,
        [tool.params_json_schema for tool in tools if isinstance(tool, FunctionTool)],
        None if output_schema is None else output_schema.json_schema(),
    ]
    return len(json.dumps(parts)), len(items) + (system_instructions is not None)


class GatedModel(Model):
    """A model whose every request is reserved, dispatched and settled in the ledger."""

    def __init__(
        self,
        model: Model,
        connection: Connection,
        lease: Lease,
        keeper: LeaseKeeper,
        pricing: Pricing,
        max_output_tokens: int,
    ) -> None:
        if max_output_tokens <= 0:
            raise ValueError("a request needs a positive output cap")
        self._model = model
        self._connection = connection
        self._lease = lease
        self._keeper = keeper
        self._pricing = pricing
        self._max_output_tokens = max_output_tokens

    async def get_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff[Any, Any]],
        tracing: ModelTracing,
        *,
        previous_response_id: str | None,
        conversation_id: str | None,
        prompt: ResponsePromptParam | None,
    ) -> ModelResponse:
        connection, lease = self._connection, self._lease
        size, items = request_size(system_instructions, input, tools, output_schema)
        reserved = estimate(self._pricing, input_token_bound(size, items), self._max_output_tokens)
        self._keeper.require()
        async with fenced_transaction(connection, lease):
            call_id = await reserve_call(connection, lease, reserved)
        self._keeper.require()
        async with fenced_transaction(connection, lease):
            await dispatch_call(connection, lease, call_id)
        capped = model_settings.resolve(ModelSettings(max_tokens=self._max_output_tokens))
        try:
            response = await self._model.get_response(
                system_instructions,
                input,
                capped,
                tools,
                output_schema,
                handoffs,
                tracing,
                previous_response_id=previous_response_id,
                conversation_id=conversation_id,
                prompt=prompt,
            )
        except openai.APIStatusError:
            async with fenced_transaction(connection, lease):
                await fail_call(connection, lease, call_id, FailureCategory.PROVIDER_ERROR)
            raise
        except Exception:
            async with fenced_transaction(connection, lease):
                await mark_call_unknown(connection, lease, call_id)
            raise
        usage = self._usage(response)
        async with fenced_transaction(connection, lease):
            if usage is None:
                await mark_call_unknown(connection, lease, call_id)
            else:
                await settle_call(connection, lease, call_id, usage)
        return response

    def stream_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff[Any, Any]],
        tracing: ModelTracing,
        *,
        previous_response_id: str | None,
        conversation_id: str | None,
        prompt: ResponsePromptParam | None,
    ) -> AsyncIterator[Any]:
        raise NotImplementedError("the worker never streams")

    def _usage(self, response: ModelResponse) -> Usage | None:
        """The usage to settle at, or None if the provider reported none.

        Without usage the SDK reports zero tokens, and a real request always has input.
        """
        reported = response.usage
        if reported.input_tokens <= 0:
            return None
        details = reported.input_tokens_details
        return Usage(
            input_tokens=reported.input_tokens,
            output_tokens=reported.output_tokens,
            cost_micro_usd=charge(
                self._pricing,
                reported.input_tokens,
                details.cached_tokens or 0,
                details.cache_write_tokens or 0,
                reported.output_tokens,
            ),
            provider_request_id=response.request_id or response.response_id,
        )
