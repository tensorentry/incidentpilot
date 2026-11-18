"""A fake model for the SDK compatibility tests: it plays back one response per turn.

The tests run the pinned SDK for real, offline. Only the provider is replaced.
"""

import json
from collections.abc import AsyncIterator, Sequence
from typing import Any

from agents import Model, ModelResponse, Usage
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputItem,
    ResponseOutputMessage,
    ResponseOutputRefusal,
    ResponseOutputText,
)

type Turn = Sequence[ResponseOutputItem] | Exception


def tool_call(name: str, arguments: dict[str, Any], number: int) -> ResponseFunctionToolCall:
    return ResponseFunctionToolCall(
        type="function_call",
        name=name,
        arguments=json.dumps(arguments),
        call_id=f"call-{number}",
        id=f"item-{number}",
    )


def message(text: str) -> ResponseOutputMessage:
    return ResponseOutputMessage(
        id="message",
        type="message",
        role="assistant",
        status="completed",
        content=[ResponseOutputText(type="output_text", text=text, annotations=[])],
    )


def refusal(text: str) -> ResponseOutputMessage:
    return ResponseOutputMessage(
        id="message",
        type="message",
        role="assistant",
        status="completed",
        content=[ResponseOutputRefusal(type="refusal", refusal=text)],
    )


class FakeModel(Model):
    """Answers each request with the next turn, or raises it if it's an exception."""

    def __init__(self, turns: Sequence[Turn], usage: Usage | None = None) -> None:
        self.turns = list(turns)
        self.usage = usage or Usage(requests=1, input_tokens=100, output_tokens=20)
        self.requests = 0
        self.tool_schemas: list[dict[str, Any]] = []

    async def get_response(self, *args: Any, **kwargs: Any) -> ModelResponse:
        self.requests += 1
        tools = kwargs.get("tools", args[3] if len(args) > 3 else [])
        self.tool_schemas = [tool.params_json_schema for tool in tools]
        turn = self.turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        return ModelResponse(output=list(turn), usage=self.usage, response_id=None)

    def stream_response(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        raise NotImplementedError("the worker never streams")
