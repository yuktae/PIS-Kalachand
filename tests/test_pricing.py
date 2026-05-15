"""
Tests for utils.pricing — rate-table presence and the three cost
calculators. Pure unit tests, no DB or app context needed.

Costs are anchored to the rates in utils/pricing.py. If a vendor changes
their price and you bump the table, the assertion below will fail and
remind you to also bump the comment block at the top of pricing.py.
"""
from decimal import Decimal

import pytest

from utils import pricing


# ── Table-presence guards ───────────────────────────────────────────────────


def test_known_text_models_all_have_rates():
    """Pricing should cover every text model the app actually calls. If
    you add a model in ai_generation.py or image_processing.py, add it
    here too so a missing-rate regression fails fast."""
    for model in ("gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.5-pro"):
        assert model in pricing.TEXT_MODEL_PRICING, model


def test_known_image_models_all_have_rates():
    assert "gemini-2.5-flash-image" in pricing.IMAGE_MODEL_PRICING


def test_known_search_providers_all_have_rates():
    for provider in ("google_cse", "brave_search", "duckduckgo", "web_scraper"):
        assert provider in pricing.SEARCH_PRICING, provider


def test_is_image_model_distinguishes_text_from_image():
    assert pricing.is_image_model("gemini-2.5-flash-image") is True
    assert pricing.is_image_model("gemini-2.5-flash") is False
    assert pricing.is_image_model("anything-else") is False


# ── cost_for_text_call ──────────────────────────────────────────────────────


def test_text_cost_unknown_model_returns_zero():
    assert pricing.cost_for_text_call("not-a-model", 1000, 1000) == Decimal("0")


def test_text_cost_uses_listed_per_million_rates():
    # 1,000,000 input + 1,000,000 output on Flash = $0.075 + $0.30 = $0.375
    cost = pricing.cost_for_text_call("gemini-2.5-flash", 1_000_000, 1_000_000)
    assert cost == Decimal("0.375000")


def test_text_cost_subtracts_cached_tokens_from_input():
    """Cached tokens are billed at zero, so 1M input with 600k cached
    should bill on the remaining 400k input only."""
    # Flash: 0.075 USD per 1M input. 400k -> 0.030.
    cost = pricing.cost_for_text_call(
        "gemini-2.5-flash",
        input_tokens=1_000_000,
        output_tokens=0,
        cached_tokens=600_000,
    )
    assert cost == Decimal("0.030000")


def test_text_cost_handles_zero_tokens_cleanly():
    assert pricing.cost_for_text_call("gemini-2.5-flash", 0, 0) == Decimal("0E-6")


# ── cost_for_image_call ─────────────────────────────────────────────────────


def test_image_cost_multiplies_count_by_rate():
    cost = pricing.cost_for_image_call("gemini-2.5-flash-image", 5)
    assert cost == Decimal("0.195000")  # 5 * 0.039


def test_image_cost_unknown_model_returns_zero():
    assert pricing.cost_for_image_call("not-a-model", 5) == Decimal("0")


# ── cost_for_search_call ────────────────────────────────────────────────────


def test_search_cost_paid_provider():
    # Brave: $3 / 1000 queries. 10 queries -> $0.03.
    cost = pricing.cost_for_search_call("brave_search", 10)
    assert cost == Decimal("0.030000")


def test_search_cost_free_provider_is_zero():
    assert pricing.cost_for_search_call("duckduckgo", 1000) == Decimal("0E-6")


def test_search_cost_unknown_provider_returns_zero():
    assert pricing.cost_for_search_call("not-a-provider", 1000) == Decimal("0")


@pytest.mark.parametrize("queries", [0, 1, 10, 1000, 5000])
def test_search_cost_scales_linearly_with_query_count(queries):
    rate_per_1k = pricing.SEARCH_PRICING["google_cse"]["per_1k_queries"]
    expected = (Decimal(queries) / Decimal("1000") * rate_per_1k).quantize(Decimal("0.000001"))
    assert pricing.cost_for_search_call("google_cse", queries) == expected
