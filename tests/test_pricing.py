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


@pytest.mark.parametrize("model", ["<synthetic>", "claude-unknown-9", None, ""])
def test_unknown_models_are_free(model: str | None) -> None:
    assert cost_for(model, input_tokens=10_000, output_tokens=10_000) == 0.0
