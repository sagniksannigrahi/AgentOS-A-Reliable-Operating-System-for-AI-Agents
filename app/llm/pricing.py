"""Token pricing used for cost *estimates*.

Providers report token counts, not money, so every cost figure in AgentOS is an estimate derived
from this table. Update ``PRICING_VERSION`` whenever prices change so stored records stay
interpretable. Source: Anthropic list prices (USD per million tokens) as cached on 2026-10-06.
Where a cache-read price is not listed, cached tokens are charged at the input price (a
conservative over-estimate).
"""

from __future__ import annotations

from decimal import Decimal

PRICING_VERSION = "anthropic-list-2026-10-06"

# model -> (input, output) USD per 1M tokens
PRICES: dict[str, tuple[Decimal, Decimal]] = {
    "claude-fable-5-1": (Decimal("10.00"), Decimal("50.00")),
    "claude-opus-5-5": (Decimal("4.00"), Decimal("20.00")),
    "claude-opus-5": (Decimal("5.00"), Decimal("25.00")),
    "claude-sonnet-5-5": (Decimal("2.00"), Decimal("10.00")),
    "claude-sonnet-5": (Decimal("2.00"), Decimal("10.00")),
    "claude-haiku-5-5": (Decimal("0.10"), Decimal("0.50")),
    # Notional price for the deterministic fake provider so cost accounting is exercised end to
    # end. Fake costs are simulated and must never be read as real spend.
    "fake-llm": (Decimal("2.00"), Decimal("10.00")),
}

CACHE_READ_PRICES: dict[str, Decimal] = {
    "claude-fable-5-1": Decimal("0.25"),
    "claude-opus-5-5": Decimal("0.20"),
    "claude-sonnet-5-5": Decimal("0.20"),
}

_MTOK = Decimal(1_000_000)


def estimate_cost(model: str, input_tokens: int, output_tokens: int, cache_read_tokens: int = 0) -> Decimal | None:
    price = PRICES.get(model)
    if price is None:
        return None
    inp, out = price
    cache = CACHE_READ_PRICES.get(model, inp)
    # Anthropic convention: ``input_tokens`` excludes tokens served from the prompt cache, which
    # are reported separately as ``cache_read_input_tokens``.
    cost = (Decimal(input_tokens) * inp + Decimal(output_tokens) * out + Decimal(cache_read_tokens) * cache) / _MTOK
    return cost.quantize(Decimal("0.00000001"))
