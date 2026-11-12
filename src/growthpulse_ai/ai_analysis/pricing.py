"""A versioned price table for estimating Anthropic API spend before and after a call.

These are list prices for standard-tier, global-inference requests. They are an estimate
for the local spend ledger, not a bill. Never assume the ledger equals the account
balance: only the Anthropic Console shows that.

Before any live run, check these against https://www.anthropic.com/pricing. A price change
needs a new PRICING_VERSION, so every ledger entry and artifact records which table
priced it.
"""

from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal

PRICING_VERSION = "2026-11-12.1"
PRICING_SOURCE = (
    "Anthropic list prices per million tokens (standard tier, global inference), as "
    "documented on 2026-11-12. Verify at https://www.anthropic.com/pricing before a live run."
)

MILLION = Decimal(1_000_000)
USD_QUANTUM = Decimal("0.000001")

# A request served with a price modifier (regional inference, priority tier, fast mode)
# costs more than these list prices. The reservation adds this allowance on top, and a
# response that reports any modifier is held at its full reservation, never re-priced.
MODIFIER_ALLOWANCE = Decimal("1.10")

# Structured outputs inject the schema as a system prompt, and the API wraps each turn in
# formatting tokens. This covers that overhead in the input bound.
INPUT_OVERHEAD_TOKENS = 2048


@dataclass(frozen=True)
class ModelPrice:
    """USD per million tokens."""

    input: Decimal
    output: Decimal
    cache_write_5m: Decimal
    cache_write_1h: Decimal
    cache_read: Decimal

    @property
    def max_input_rate(self) -> Decimal:
        """The highest rate an input token of a GrowthPulse request can be billed at.

        Requests set no cache_control, so the 1-hour write rate (which needs an explicit
        ttl) cannot apply; the 5-minute write rate covers any default caching. Should a
        response still cost more than its reservation, the ledger blocks further calls.
        """
        return max(self.input, self.cache_write_5m)


MODEL_PRICES: dict[str, ModelPrice] = {
    "claude-opus-5-5": ModelPrice(
        input=Decimal("4.00"),
        output=Decimal("20.00"),
        cache_write_5m=Decimal("5.00"),
        cache_write_1h=Decimal("8.00"),
        cache_read=Decimal("0.20"),
    ),
}


class UnpricedModelError(LookupError):
    """The price table has no entry for this model, so its cost cannot be estimated."""


def price_for(model: str) -> ModelPrice:
    try:
        return MODEL_PRICES[model]
    except KeyError:
        raise UnpricedModelError(f"no price for model {model!r} in {PRICING_VERSION}") from None


def usd(value: Decimal) -> Decimal:
    """Round up to a millionth of a dollar, so estimates never understate."""
    return value.quantize(USD_QUANTUM, rounding=ROUND_CEILING)


def input_token_upper_bound(*texts: str) -> int:
    """A conservative ceiling on input tokens, with no API call.

    Claude's tokenizer encodes at least one byte per token, so the UTF-8 byte count of
    everything sent bounds the token count from above. INPUT_OVERHEAD_TOKENS covers
    message framing and the injected structured-output instructions.
    """
    return sum(len(text.encode("utf-8")) for text in texts) + INPUT_OVERHEAD_TOKENS


def max_request_cost(model: str, max_input_tokens: int, max_output_tokens: int) -> Decimal:
    """The most one request can cost: every input token at the highest input rate, the
    full output allowance (thinking is billed as output and counts toward max_tokens),
    and the price-modifier allowance on top."""
    price = price_for(model)
    base = (
        Decimal(max_input_tokens) * price.max_input_rate + Decimal(max_output_tokens) * price.output
    ) / MILLION
    return usd(base * MODIFIER_ALLOWANCE)


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int
    output_tokens: int
    cache_creation_5m_input_tokens: int = 0
    cache_creation_1h_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_creation_5m_input_tokens": self.cache_creation_5m_input_tokens,
            "cache_creation_1h_input_tokens": self.cache_creation_1h_input_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
        }


def usage_cost(model: str, usage: TokenUsage) -> Decimal:
    """Estimated cost of a completed request from its reported usage, at list prices."""
    price = price_for(model)
    total = (
        Decimal(usage.input_tokens) * price.input
        + Decimal(usage.output_tokens) * price.output
        + Decimal(usage.cache_creation_5m_input_tokens) * price.cache_write_5m
        + Decimal(usage.cache_creation_1h_input_tokens) * price.cache_write_1h
        + Decimal(usage.cache_read_input_tokens) * price.cache_read
    ) / MILLION
    return usd(total)
