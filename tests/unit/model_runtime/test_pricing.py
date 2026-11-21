"""Cost estimates and settlements (spec §11.1): integer micro-USD, rounded up, and an
estimate is never below what the same request can be charged."""

import json

import pytest
from agents import AgentOutputSchema, function_tool

from incidentpilot.domain.report import ReportCandidate
from incidentpilot.model_runtime.gate import request_size
from incidentpilot.model_runtime.pricing import (
    TOKENS_PER_ITEM,
    TOKENS_PER_REQUEST,
    Pricing,
    charge,
    estimate,
    input_token_bound,
)

CACHE_PARTS = "cached and cache-written tokens are part of the input tokens"
# Round numbers, not any provider's prices.
PRICING = Pricing(
    version="test-1",
    input_per_million=2_000_000,
    cached_input_per_million=500_000,
    cache_write_per_million=2_500_000,
    output_per_million=8_000_000,
)


def test_a_charge_prices_each_kind_of_token() -> None:
    # Of 6,000 input: 1,000 plain at $2/M, 3,000 cached at $0.50/M, 2,000 written to
    # the cache at $2.50/M. Then 500 output at $8/M.
    assert charge(PRICING, 6_000, 3_000, 2_000, 500) == 2_000 + 1_500 + 5_000 + 4_000


def test_fractions_of_a_micro_usd_round_up() -> None:
    assert charge(PRICING, 1, 0, 0, 0) == 2
    assert charge(PRICING, 0, 0, 0, 1) == 8
    assert charge(Pricing("test-2", 1, 1, 1, 1), 3, 1, 1, 1) == 4
    assert charge(Pricing("test-3", 1_500_000, 0, 0, 0), 1, 0, 0, 0) == 2


def test_nothing_costs_nothing() -> None:
    assert charge(PRICING, 0, 0, 0, 0) == 0
    assert estimate(PRICING, 0, 0) == 0


def test_an_estimate_prices_input_at_the_dearer_rate_and_the_whole_output_cap() -> None:
    assert estimate(PRICING, 4_000, 500) == 10_000 + 4_000
    cheap_writes = Pricing("test-4", 2_000_000, 0, 1_000_000, 8_000_000)
    assert estimate(cheap_writes, 4_000, 500) == 8_000 + 4_000


@pytest.mark.parametrize(("cached", "written"), [(0, 0), (1_000, 0), (0, 4_000), (1_000, 3_000)])
@pytest.mark.parametrize("output", [0, 1, 499, 500])
def test_an_estimate_covers_any_charge_within_its_bounds(
    cached: int, written: int, output: int
) -> None:
    assert charge(PRICING, 4_000, cached, written, output) <= estimate(PRICING, 4_000, 500)


def test_the_input_bound_adds_framing_to_the_bytes() -> None:
    assert input_token_bound(1_000, 3) == 1_000 + 3 * TOKENS_PER_ITEM + TOKENS_PER_REQUEST
    assert input_token_bound(0, 0) == TOKENS_PER_REQUEST


@pytest.mark.parametrize(
    ("call", "message"),
    [
        (lambda: charge(PRICING, -1, 0, 0, 0), "token counts can't be negative"),
        (lambda: charge(PRICING, 1, -1, 0, 0), "token counts can't be negative"),
        (lambda: charge(PRICING, 1, 0, -1, 0), "token counts can't be negative"),
        (lambda: charge(PRICING, 1, 0, 0, -1), "token counts can't be negative"),
        (lambda: charge(PRICING, 1, 2, 0, 0), CACHE_PARTS),
        (lambda: charge(PRICING, 1, 0, 2, 0), CACHE_PARTS),
        (lambda: charge(PRICING, 2, 1, 2, 0), CACHE_PARTS),
        (lambda: estimate(PRICING, -1, 0), "token counts can't be negative"),
        (lambda: estimate(PRICING, 0, -1), "token counts can't be negative"),
        (lambda: input_token_bound(-1, 0), "a request can't have a negative size"),
        (lambda: input_token_bound(0, -1), "a request can't have a negative size"),
        (lambda: Pricing("", 1, 1, 1, 1), "a price table needs a version"),
        (lambda: Pricing("v", -1, 0, 1, 1), "a price can't be negative"),
        (lambda: Pricing("v", 1, -1, 1, 1), "a price can't be negative"),
        (lambda: Pricing("v", 1, 1, -1, 1), "a price can't be negative"),
        (lambda: Pricing("v", 1, 1, 1, -1), "a price can't be negative"),
        (lambda: Pricing("v", 1, 2, 1, 1), "cached input can't cost more than input"),
    ],
)
def test_impossible_amounts_are_refused(call: object, message: str) -> None:
    with pytest.raises(ValueError, match=f"^{message}$"):
        call()  # type: ignore[operator]


def test_equal_cached_and_uncached_prices_are_allowed() -> None:
    assert Pricing("v", 1, 1, 0, 0).cached_input_per_million == 1


async def lookup(service: str) -> str:
    """Look a service up."""
    return service


def test_a_request_size_counts_bytes_and_items() -> None:
    size, items = request_size("hé", [{"role": "user", "content": "hi"}], [], None)

    assert size == len(r'["h\u00e9", [{"role": "user", "content": "hi"}], [], null]')
    assert items == 2


def test_escaped_text_is_never_shorter_than_its_utf8_bytes() -> None:
    for text in ["é", "—", "日本", "🙂"]:
        assert request_size(text, "", [], None)[0] - request_size("", "", [], None)[0] >= len(
            text.encode()
        )


def test_a_text_input_is_one_item_and_no_instructions_add_none() -> None:
    size, items = request_size(None, "hi", [], None)

    assert (size, items) == (len(b'[null, ["hi"], [], null]'), 1)


def test_a_request_size_includes_tool_and_output_schemas() -> None:
    tool = function_tool(lookup)
    output = AgentOutputSchema(ReportCandidate)
    bare, _ = request_size(None, "hi", [], None)

    with_tool, _ = request_size(None, "hi", [tool], None)
    with_output, _ = request_size(None, "hi", [], output)

    assert with_tool - bare == len(json.dumps(tool.params_json_schema))
    assert with_output - bare == len(json.dumps(output.json_schema())) - len("null")
