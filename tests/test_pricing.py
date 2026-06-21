"""Exact cost-math checks."""

from __future__ import annotations

import pytest

from ccq.pricing import cost_for


def test_opus_with_cache() -> None:
    # (1000*5 + 500*5*1.25 + 10000*5*0.10 + 2000*25) / 1e6
    cost = cost_for(
        "claude-opus-4-8",
        input_tokens=1000,
        output_tokens=2000,
        cache_creation_tokens=500,
        cache_read_tokens=10000,
    )
    assert cost == pytest.approx(0.063125)


def test_sonnet_no_cache() -> None:
    # (2000*3 + 1000*15) / 1e6
    cost = cost_for("claude-sonnet-4-6", input_tokens=2000, output_tokens=1000)
    assert cost == pytest.approx(0.021)


@pytest.mark.parametrize("model", ["<synthetic>", "gpt-4o", "claude-instant-1", None, ""])
def test_unknown_models_are_free(model: str | None) -> None:
    # Names with no Claude family word (opus/sonnet/haiku/fable) still price to $0.
    assert cost_for(model, input_tokens=10_000, output_tokens=10_000) == 0.0


def test_tier_fallback_prices_dated_sonnet() -> None:
    # An unlisted, date-stamped Sonnet name still prices at the Sonnet tier, not $0.
    assert cost_for("claude-3-5-sonnet-20241022", input_tokens=1_000_000) == pytest.approx(3.0)


def test_tier_fallback_is_opus_generation_aware() -> None:
    # Opus-3 cost 3x Opus-4; the family fallback must not flatten the two.
    assert cost_for("claude-3-opus-20240229", input_tokens=1_000_000) == pytest.approx(15.0)
    assert cost_for("claude-opus-4-1", input_tokens=1_000_000) == pytest.approx(5.0)


def test_tier_fallback_haiku_generations() -> None:
    assert cost_for("claude-3-haiku-20240307", input_tokens=1_000_000) == pytest.approx(0.25)
    assert cost_for("claude-3-5-haiku-20241022", input_tokens=1_000_000) == pytest.approx(0.80)


def test_exact_match_takes_precedence_over_tier() -> None:
    # A listed model uses its exact entry (the exact path runs before the fallback).
    assert cost_for("claude-opus-4-8", input_tokens=1_000_000) == pytest.approx(5.0)
