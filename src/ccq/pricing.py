"""Per-model token pricing for cost estimation.

Claude Code transcripts do not store a dollar cost, only token counts in
``message.usage``. We price them with the published Anthropic per-million-token
rates and the standard cache multipliers.

Caveats baked in deliberately:
- ``<synthetic>`` and any unknown model price to 0 (no real API spend).
- Cost is *main-loop only*. Subagent (Agent tool) token totals live in
  ``toolUseResult.totalTokens`` and have no input/output split, so they cannot
  be priced - they are surfaced separately as a token count, never as dollars.
"""

from __future__ import annotations

from dataclasses import dataclass

# Anthropic standard cache multipliers, relative to the model's input price.
CACHE_WRITE_MULT = 1.25  # 5-minute ephemeral cache write
CACHE_READ_MULT = 0.10  # cache hit read


@dataclass(frozen=True)
class ModelPrice:
    """USD per million tokens."""

    input_per_mtok: float
    output_per_mtok: float


# Sources: reference_opus_4_8 (Opus $5/$25, 1.67x Sonnet => Sonnet $3/$15),
# reference_fable_5 ($10/$50). Opus 4.7 is the same tier/price as 4.8.
# Haiku 4.5 uses the standard $1/$5 small-model rate.
PRICING: dict[str, ModelPrice] = {
    "claude-opus-4-8": ModelPrice(5.0, 25.0),
    "claude-opus-4-7": ModelPrice(5.0, 25.0),
    "claude-sonnet-4-6": ModelPrice(3.0, 15.0),
    "claude-haiku-4-5-20251001": ModelPrice(1.0, 5.0),
    "claude-fable-5": ModelPrice(10.0, 50.0),
}


def cost_for(
    model: str | None,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_creation_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> float:
    """Return the estimated USD cost of one usage record.

    Unknown/synthetic models price to 0.0 - they have no real API spend.
    """
    price = PRICING.get(model or "")
    if price is None:
        return 0.0
    return (
        input_tokens * price.input_per_mtok
        + cache_creation_tokens * price.input_per_mtok * CACHE_WRITE_MULT
        + cache_read_tokens * price.input_per_mtok * CACHE_READ_MULT
        + output_tokens * price.output_per_mtok
    ) / 1_000_000


def pricing_rows() -> list[tuple[str, float, float]]:
    """Pricing as ``(model, input_per_mtok, output_per_mtok)`` rows for SQL registration."""
    return [(m, p.input_per_mtok, p.output_per_mtok) for m, p in PRICING.items()]
