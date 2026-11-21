"""What a model request costs, before it is sent and after it is answered (spec §11.1).

Prices are an explicit, versioned input, never a guess built into the code. Amounts are
integer micro-USD and every division rounds up, so neither an estimate nor a settlement
comes out below the true price.
"""

from dataclasses import dataclass

MILLION = 1_000_000
# Framing tokens the provider adds that aren't in the request's bytes: role markers
# and separators per input item, and a fixed amount per request. Allowances, not
# measurements; the live run (T4.10) compares estimates with reported usage.
TOKENS_PER_ITEM = 16
TOKENS_PER_REQUEST = 256


@dataclass(frozen=True)
class Pricing:
    """One model's prices, in micro-USD per million tokens."""

    version: str
    input_per_million: int
    cached_input_per_million: int
    # Input written to the provider's cache, which may cost more than plain input.
    cache_write_per_million: int
    output_per_million: int

    def __post_init__(self) -> None:
        if not self.version:
            raise ValueError("a price table needs a version")
        prices = (
            self.input_per_million,
            self.cached_input_per_million,
            self.cache_write_per_million,
            self.output_per_million,
        )
        if min(prices) < 0:
            raise ValueError("a price can't be negative")
        if self.cached_input_per_million > self.input_per_million:
            raise ValueError("cached input can't cost more than input")


def _cost(tokens: int, per_million: int) -> int:
    return -(-tokens * per_million // MILLION)


def input_token_bound(request_bytes: int, items: int) -> int:
    """At most how many input tokens a request of this size can be.

    A byte-level BPE token holds at least one byte, so a request can't have more tokens
    than bytes, plus the framing the provider adds.
    """
    if min(request_bytes, items) < 0:
        raise ValueError("a request can't have a negative size")
    return request_bytes + items * TOKENS_PER_ITEM + TOKENS_PER_REQUEST


def estimate(pricing: Pricing, input_tokens: int, max_output_tokens: int) -> int:
    """The most a request can cost: every input token at the dearer of plain input and a
    cache write, and the full output cap."""
    if min(input_tokens, max_output_tokens) < 0:
        raise ValueError("token counts can't be negative")
    dearest = max(pricing.input_per_million, pricing.cache_write_per_million)
    return _cost(input_tokens, dearest) + _cost(max_output_tokens, pricing.output_per_million)


def charge(
    pricing: Pricing, input_tokens: int, cached: int, cache_written: int, output_tokens: int
) -> int:
    """What a request cost, from the usage the provider reported. `cached` and
    `cache_written` are the parts of the input read from and written to the cache."""
    if min(input_tokens, cached, cache_written, output_tokens) < 0:
        raise ValueError("token counts can't be negative")
    if cached + cache_written > input_tokens:
        raise ValueError("cached and cache-written tokens are part of the input tokens")
    return (
        _cost(input_tokens - cached - cache_written, pricing.input_per_million)
        + _cost(cached, pricing.cached_input_per_million)
        + _cost(cache_written, pricing.cache_write_per_million)
        + _cost(output_tokens, pricing.output_per_million)
    )
